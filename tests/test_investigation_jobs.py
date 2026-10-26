"""Worker failure, durable deadlines, bounded retries and stale-writer fencing."""
from copy import deepcopy
from pathlib import Path
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.action_adapter import SimulatedActionAdapter
from app.api import create_app
from app.audit import AuditStore
from app.auth import Principal
from app.config import get_settings
from app.errors import AuthorizationError, IdempotencyConflict
from app.investigation_jobs import InvestigationSupervisor
from app.knowledge import KnowledgeBase
from app.models import AgentState
from app.portal import PortalService
from app.service import TicketService
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities, auth_headers


class RecordingActions(SimulatedActionAdapter):
    def __init__(self):
        self.registrations = 0
        self.attachments = 0

    def execute(self, ticket, action, *, idempotency_key):
        self.registrations += 1
        return super().execute(ticket, action, idempotency_key=idempotency_key)

    def attach_evidence(self, ticket, *, idempotency_key):
        self.attachments += 1
        return super().attach_evidence(ticket, idempotency_key=idempotency_key)


class InvestigationJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = get_settings(_env_file=None, data_dir=self.root, llm_mode="offline", rag_backend="lexical",
            diagnosis_mode="react", diagnosis_background=False, diagnosis_worker_poll=.02)
        self.audit = AuditStore(self.cfg.db_path)
        self.actions = RecordingActions()
        self.agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(self.root / "kb", settings=self.cfg), self.audit, self.actions),
            self.audit, settings=self.cfg, diagnostic_runner=object())
        self.service = TicketService(self.agent)
        self.owner = Principal(user_id="alice", role="employee", department="IT")
        self.manager = Principal(user_id="manager", role="manager", department="IT", support_groups=["network", "applications", "data", "service_desk"])
        self.portal = PortalService(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def create(self):
        return self.service.create(self.owner, title="VPN 故障", description="VPN 报错超时")

    def load(self, ticket_id):
        return AgentState.from_dict(self.audit.get_state(ticket_id))

    def expire(self, ticket_id, *, queued=False):
        with self.audit.ticket_lock(ticket_id):
            state = self.load(ticket_id)
            state.ticket.investigation_job["queued_at" if queued else "deadline_at"] = 0
            self.audit.save_checkpoint(state.to_dict(), "clock_fixture")
        self.agent.investigation_jobs.expire()
        return self.load(ticket_id)

    def claim(self, state):
        return self.service.claim(self.manager, state.ticket.id, state.ticket.assignment["revision"])

    def test_exception_immediately_hands_off_and_unassigned_staff_must_claim_before_retry(self):
        state = self.create()
        with patch.object(self.agent, "_diagnose", side_effect=RuntimeError("private model detail")):
            failed = self.agent.investigate(state.ticket.id)
        self.assertEqual(failed.ticket.status, "awaiting_support")
        self.assertEqual(failed.ticket.investigation_status, "failed")
        self.assertEqual(failed.next_node, "diagnose")
        self.assertTrue(self.portal.get(self.manager, state.ticket.id, "support")["support"]["can_claim"])
        with self.assertRaises(IdempotencyConflict):
            self.service.retry_investigation(self.manager, state.ticket.id, failed.ticket.case_revision)
        with self.assertRaises(AuthorizationError):
            self.service.retry_investigation(self.owner, state.ticket.id, failed.ticket.case_revision)
        self.assertNotIn("private model detail", str(failed.to_dict()))
        self.assertEqual(self.actions.registrations, 1)

    def test_hung_read_times_out_without_holding_the_case_lock_and_late_checkpoint_cannot_overwrite_resolution(self):
        state = self.create()
        entered, release = Event(), Event()
        def blocked(current, *, persist):
            entered.set()
            release.wait(5)
            current.result = "late worker overwrote resolution"
            persist(current, "late_checkpoint")
            return current
        with patch.object(self.agent, "_diagnose", side_effect=blocked):
            thread = Thread(target=self.agent.investigate, args=(state.ticket.id,))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                failed = self.expire(state.ticket.id)
                self.assertEqual(failed.ticket.investigation_job["reason"], "worker_timeout")
                failed = self.claim(failed)
                resolved = self.service.resolve(self.manager, state.ticket.id, "resolved", "人工已核实恢复", "record-1")
                release.set()
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(self.load(state.ticket.id).to_dict(), resolved.to_dict())
            finally:
                release.set()
                thread.join(3)
        self.assertEqual(self.actions.registrations, 1)
        self.assertEqual(self.actions.attachments, 0)

    def test_duplicate_delivery_does_not_start_a_second_live_worker(self):
        state = self.create()
        entered, release = Event(), Event()
        def blocked(current, *, persist):
            entered.set(); release.wait(5)
            return current
        with patch.object(self.agent, "_diagnose", side_effect=blocked) as diagnose:
            thread = Thread(target=self.agent.investigate, args=(state.ticket.id,))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.agent.investigate(state.ticket.id)
                self.assertEqual(diagnose.call_count, 1)
            finally:
                release.set(); thread.join(3)
        self.assertEqual(self.load(state.ticket.id).ticket.investigation_job["runs"], 1)

    def test_retry_budget_is_durable_and_exhaustion_escalates_to_manual_work(self):
        state = self.create()
        with patch.object(self.agent, "_diagnose", side_effect=RuntimeError()):
            failed = self.agent.investigate(state.ticket.id)
            self.claim(failed)
            for expected in (2, 3):
                current = self.load(state.ticket.id)
                queued = self.service.retry_investigation(self.manager, state.ticket.id, current.ticket.case_revision)
                replay = self.service.retry_investigation(self.manager, state.ticket.id, current.ticket.case_revision)
                self.assertEqual(queued.to_dict(), replay.to_dict())
                failed = self.agent.investigate(state.ticket.id)
                self.assertEqual(failed.ticket.investigation_job["runs"], expected)
        self.assertTrue(failed.ticket.investigation_job["exhausted"])
        support = self.portal.get(self.manager, state.ticket.id, "support")["support"]
        self.assertFalse(support["can_retry_investigation"])
        self.assertTrue(support["can_resolve"])
        self.assertTrue(support["worker_issue"]["escalated"])
        with self.assertRaises(IdempotencyConflict):
            self.service.retry_investigation(self.manager, state.ticket.id, failed.ticket.case_revision)
        self.assertEqual(self.actions.registrations, 1)

    def test_queued_work_also_has_a_deadline_and_consumes_the_retry_budget(self):
        state = self.create()
        failed = self.expire(state.ticket.id, queued=True)
        self.assertEqual(failed.ticket.investigation_job["reason"], "queue_timeout")
        self.claim(failed)
        for expected in (2, 3):
            current = self.load(state.ticket.id)
            self.service.retry_investigation(self.manager, state.ticket.id, current.ticket.case_revision)
            failed = self.expire(state.ticket.id, queued=True)
            self.assertEqual(failed.ticket.investigation_job["runs"], expected)
        self.assertTrue(failed.ticket.investigation_job["exhausted"])

    def test_crashed_worker_releases_os_lease_and_resumes_without_registering_again(self):
        state = self.create()
        with patch.object(self.agent, "_diagnose", side_effect=SystemExit()), self.assertRaises(SystemExit):
            self.agent.investigate(state.ticket.id)
        with patch.object(self.agent, "_diagnose", side_effect=lambda current, **_: current):
            done = self.agent.investigate(state.ticket.id)
        self.assertEqual(done.ticket.investigation_job["runs"], 2)
        self.assertEqual(done.ticket.status, "awaiting_support")
        self.assertEqual(self.actions.registrations, 1)

    def test_watchdog_detects_an_interrupted_worker_without_waiting_for_its_deadline(self):
        state = self.create()
        with patch.object(self.agent, "_diagnose", side_effect=SystemExit()), self.assertRaises(SystemExit):
            self.agent.investigate(state.ticket.id)
        self.agent.investigation_jobs.expire()
        failed = self.load(state.ticket.id)
        self.assertEqual(failed.ticket.investigation_job["reason"], "worker_interrupted")
        self.assertEqual(failed.ticket.status, "awaiting_support")

    def test_a_new_retry_generation_also_rejects_the_old_workers_late_result(self):
        state = self.create()
        entered, release = Event(), Event()
        calls = []
        def blocked(current, *, persist):
            calls.append(1)
            if len(calls) == 1:
                entered.set(); release.wait(5)
                current.result = "late result"
                persist(current, "late_checkpoint")
            return current
        with patch.object(self.agent, "_diagnose", side_effect=blocked):
            thread = Thread(target=self.agent.investigate, args=(state.ticket.id,))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                failed = self.expire(state.ticket.id)
                old_token = failed.ticket.investigation_job["token"]
                failed = self.claim(failed)
                self.service.retry_investigation(self.manager, state.ticket.id, failed.ticket.case_revision)
                done = self.agent.investigate(state.ticket.id)
                self.assertNotEqual(done.ticket.investigation_job["token"], old_token)
                release.set(); thread.join(3)
                self.assertEqual(self.load(state.ticket.id).to_dict(), done.to_dict())
                self.assertEqual(done.ticket.investigation_job["runs"], 2)
            finally:
                release.set(); thread.join(3)
        self.assertEqual(self.actions.registrations, 1)

    def test_timeout_during_attachment_preserves_unknown_write_and_blocks_retry(self):
        state = self.create()
        state.next_node = "enrich"
        state.ticket.diagnostic_report = {"summary": "需人工核查", "assessment": "insufficient"}
        self.audit.save_checkpoint(state.to_dict(), "attachment_fixture")
        entered, release = Event(), Event()
        original = self.actions.attach_evidence
        def blocked(*args, **kwargs):
            entered.set(); release.wait(5)
            return original(*args, **kwargs)
        with patch.object(self.actions, "attach_evidence", side_effect=blocked):
            thread = Thread(target=self.agent.investigate, args=(state.ticket.id,))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                failed = self.expire(state.ticket.id)
                self.assertEqual(failed.ticket.investigation_status, "attachment_unknown")
                failed = self.claim(failed)
                with self.assertRaises(IdempotencyConflict):
                    self.service.retry_investigation(self.manager, state.ticket.id, failed.ticket.case_revision)
                with self.assertRaises(IdempotencyConflict):
                    self.service.resolve(self.manager, state.ticket.id, "resolved", "未核实", "")
            finally:
                release.set(); thread.join(3)
        self.assertEqual(self.load(state.ticket.id).ticket.investigation_status, "attachment_unknown")
        self.assertEqual(self.actions.attachments, 1)

    def test_http_supervisor_expires_a_blocked_job_and_support_can_queue_retry(self):
        self.agent.settings = self.cfg.model_copy(update={"diagnosis_background": True, "diagnosis_worker_timeout": .1})
        identities, keys = demo_identities(self.root)
        state = self.create()
        entered, release = Event(), Event()
        calls = []
        def blocked(current, *, persist):
            calls.append(1)
            if len(calls) == 1:
                entered.set(); release.wait(5)
                persist(current, "late_checkpoint")
            return current
        with patch.object(self.agent, "_diagnose", side_effect=blocked), TestClient(create_app(self.agent, identities=identities)) as client:
            def post_as_manager(suffix, body):
                path = "/portal/tickets/" + state.ticket.id + suffix
                response = client.post(path, headers=auth_headers(keys, "manager"), json=body)
                retry_until = time.monotonic() + 2
                while response.status_code == 409 and response.json().get("error") == "operation_pending" and time.monotonic() < retry_until:
                    time.sleep(.02)
                    response = client.post(path, headers=auth_headers(keys, "manager"), json=body)
                return response
            try:
                self.assertTrue(entered.wait(2))
                url = "/portal/tickets/" + state.ticket.id
                start = time.monotonic()
                explicit = client.post("/tickets/" + state.ticket.id + "/investigate", headers=auth_headers(keys))
                self.assertEqual(explicit.status_code, 200)
                self.assertLess(time.monotonic() - start, 1)
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    view = client.get(url, headers=auth_headers(keys)).json()
                    if view["status"]["code"] == "pending_review": break
                    time.sleep(.02)
                self.assertEqual(view["status"]["code"], "pending_review")
                self.assertEqual(client.post(url + "/retry-investigation", headers=auth_headers(keys), json={"revision": 0}).status_code, 403)
                claimed = post_as_manager("/claim", {"revision": 0})
                self.assertEqual(claimed.status_code, 200)
                response = post_as_manager("/retry-investigation", {"revision": claimed.json()["case_revision"]})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"]["code"], "investigating")
            finally:
                release.set()
                until = time.monotonic() + 2
                while client.app.state.investigation_supervisor.pending and time.monotonic() < until:
                    time.sleep(.02)
        self.assertEqual(self.actions.registrations, 1)

    def test_shutdown_does_not_wait_forever_for_a_blocked_dependency(self):
        state = self.create()
        self.agent.settings = self.cfg.model_copy(update={"diagnosis_background": True})
        entered, release = Event(), Event()
        def blocked(current, **_):
            entered.set(); release.wait(5)
            return current
        supervisor = InvestigationSupervisor(self.agent)
        with patch.object(self.agent, "_diagnose", side_effect=blocked):
            supervisor.start()
            try:
                self.assertTrue(entered.wait(2))
                before = time.monotonic()
                supervisor.close()
                self.assertLess(time.monotonic() - before, 1)
                self.assertEqual(self.load(state.ticket.id).ticket.investigation_status, "failed")
            finally:
                release.set()
                for thread in supervisor.threads: thread.join(2)


if __name__ == "__main__":
    unittest.main()
