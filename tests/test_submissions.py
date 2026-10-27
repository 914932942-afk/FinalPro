"""Business-level submission identity across refresh, different keys, crashes and processes."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.api import create_app
from app.audit import AuditStore
from app.auth import Principal
from app.errors import IdempotencyConflict, StorageError
from app.http_actions import IncidentHTTPAdapter
from app.models import Ticket
from app.service import TicketService
from app.workflow import EnterpriseAgent
import test_case_flow as flow_fixtures
import test_http_actions as http_fixtures
from auth_helpers import auth_headers


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.flow = flow_fixtures.CaseFlowTests("test_registered_incident_remains_open_and_can_be_resolved_without_report")
        self.flow.setUp()
        self.agent, self.audit, self.service = self.flow.agent, self.flow.audit, self.flow.service
        self.body = {"title": "ERP 故障", "description": "ERP 报错 503", "severity": "medium"}

    def tearDown(self):
        self.flow.tearDown()

    def create(self, key=None, owner=None, **changes):
        return self.service.create(owner or self.flow.user, **{**self.body, **changes}, idempotency_key=key)

    def rows(self, table):
        assert table in {"submission_cases", "submission_keys", "operations", "tickets"}
        with self.audit._db() as db:
            return db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_different_keys_reuse_active_case_without_a_second_action(self):
        first = self.create("first")
        duplicate = self.create("new-after-refresh")
        self.assertEqual(first.ticket.id, duplicate.ticket.id)
        self.assertEqual(duplicate.submission_result["reason"], "active_case")
        self.assertNotIn("submission_result", duplicate.to_dict())
        self.assertEqual(len(self.flow.actions.registrations), 1)
        self.assertEqual(self.rows("submission_keys"), 2)
        self.assertEqual(self.rows("tickets"), 1)

    def test_missing_keys_and_formatting_whitespace_still_reuse_case(self):
        first = self.create()
        duplicate = self.create(title=" ERP   故障 ", description="ERP\n报错  503 ")
        self.assertEqual(first.ticket.id, duplicate.ticket.id)
        self.assertEqual(duplicate.ticket.description, "ERP 报错 503")
        self.assertEqual(len(self.flow.actions.registrations), 1)

    def test_exact_request_replay_retains_existing_contract_and_client_key_cannot_change_body(self):
        first = self.create("original")
        second = self.create("original")
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertIsNone(second.submission_result)
        with self.assertRaises(IdempotencyConflict):
            self.create("original", description="另一个错误 504")
        self.create("alias")
        with self.assertRaises(IdempotencyConflict):
            self.create("alias", description="另一个错误 504")
        self.assertEqual(self.rows("tickets"), 1)

    def test_owners_are_isolated_even_with_same_client_key_and_content(self):
        alice = self.create("same-key")
        bob = self.create("same-key", owner=Principal(user_id="bob", role="employee", department="IT"))
        self.assertNotEqual(alice.ticket.id, bob.ticket.id)
        self.assertEqual(bob.ticket.owner_id, "bob")
        self.assertEqual(self.rows("tickets"), 2)

    def test_meaningful_content_and_impact_changes_are_not_silently_merged(self):
        cases = [self.create("a"), self.create("b", description="ERP 报错 504"), self.create("c", severity="high")]
        self.assertEqual(len({case.ticket.id for case in cases}), 3)
        self.assertEqual(cases[-1].ticket.severity, "high")

    def test_reauthentication_after_department_or_role_change_reads_old_request_without_reexecution(self):
        first = self.create("before-transfer")
        path = self.flow.identities.path
        records = json.loads(path.read_text(encoding="utf-8"))
        next(row for row in records if row["user_id"] == "alice").update(department="Finance", role="manager", support_groups=[])
        path.write_text(json.dumps(records), encoding="utf-8")
        actor = self.flow.identities.principal("alice")
        duplicate = self.create("before-transfer", owner=actor)
        self.assertEqual(duplicate.ticket.id, first.ticket.id)
        self.assertEqual(duplicate.ticket.department, "IT")
        self.assertEqual(len(self.flow.actions.registrations), 1)
        fresh = self.create("after-transfer", owner=actor)
        self.assertEqual(fresh.ticket.department, "Finance")
        self.assertNotEqual(fresh.ticket.id, first.ticket.id)

    def test_business_alias_remains_bound_after_case_ends_but_new_request_can_proceed(self):
        first = self.create("first")
        self.create("alias")
        self.service.cancel(self.flow.user, first.ticket.id, "不再需要", case_revision=first.ticket.case_revision)
        replay = self.create("alias")
        self.assertEqual(replay.ticket.id, first.ticket.id)
        self.assertEqual(replay.ticket.status, "cancelled")
        fresh = self.create("fresh")
        self.assertNotEqual(fresh.ticket.id, first.ticket.id)
        self.assertEqual(len(self.flow.actions.registrations), 2)

    def test_recent_success_is_reused_but_expiry_uses_receipt_time_not_last_update(self):
        first = self.create("password-a", title="重置密码", description="忘记密码")
        second = self.create("password-b", title="重置密码", description="忘记密码")
        self.assertEqual(first.ticket.id, second.ticket.id)
        self.assertEqual(second.submission_result["reason"], "recent_completion")
        first.resolution["at"] = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
        self.agent._persist(first, "test_old_receipt")
        fresh = self.create("password-c", title="重置密码", description="忘记密码")
        self.assertNotEqual(fresh.ticket.id, first.ticket.id)
        self.assertEqual(self.create("password-b", title="重置密码", description="忘记密码").ticket.id, first.ticket.id)
        self.assertEqual(len(self.flow.actions.registrations), 2)

    def test_recent_permission_grant_does_not_run_twice_and_invalidated_application_can_restart(self):
        body = {"title": "VPN 访问申请", "description": "申请 VPN 普通使用权限"}
        first = self.create("grant-a", **body)
        done = self.service.approve(self.flow.manager, first.approval.id, True)
        duplicate = self.create("grant-b", **body)
        self.assertEqual(done.ticket.id, duplicate.ticket.id)
        self.assertEqual(len(self.flow.actions.registrations), 1)
        old = self.create("expired-a", title="ERP 访问申请", description="申请 ERP 普通使用权限")
        self.agent.authorization.invalidate(old)
        fresh = self.create("expired-b", title="ERP 访问申请", description="申请 ERP 普通使用权限")
        self.assertNotEqual(old.ticket.id, fresh.ticket.id)
        self.assertNotEqual(old.approval.id, fresh.approval.id)
        self.assertEqual(fresh.ticket.status, "awaiting_approval")

    def test_resolved_incident_can_be_submitted_again_as_a_new_fault(self):
        first = self.flow.claim(self.create("fault-a"))
        done = self.service.resolve(self.flow.manager, first.ticket.id, "resolved", "支持侧已恢复", "test-record", case_revision=first.ticket.case_revision)
        done = self.service.verify_case(self.flow.user, done.ticket.id, done.ticket.case_revision, "confirmed")
        fresh = self.create("fault-b")
        self.assertNotEqual(done.ticket.id, fresh.ticket.id)
        self.assertEqual(self.audit.get_ticket(done.ticket.id)["status"], "resolved")
        self.assertEqual(len(self.flow.actions.registrations), 2)
        from scripts.check_investigation_workflow import archive_synthetic_case
        archive_synthetic_case(self.agent, self.flow.identities, fresh.ticket.id, "test-fixture")
        self.assertEqual(self.audit.get_ticket(fresh.ticket.id)["status"], "archived")
        self.assertNotEqual(self.create("after-archive").ticket.id, fresh.ticket.id)

    def test_concurrent_different_keys_reserve_only_one_case_before_model_execution(self):
        started, release = threading.Event(), threading.Event()
        results, errors = [], []
        intake = self.agent.nodes["intake"]
        def paused(state):
            started.set()
            if not release.wait(10):
                raise RuntimeError("test timeout")
            return intake(state)
        def worker():
            try:
                results.append(self.create("first"))
            except BaseException as error:
                errors.append(error)
        with patch.dict(self.agent.nodes, intake=paused):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                duplicate = self.create("second")
                same_key = self.create("first")
                self.assertEqual(duplicate.ticket.id, same_key.ticket.id)
                self.assertEqual(duplicate.ticket.status, "new")
                self.assertFalse(self.flow.actions.registrations)
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0].ticket.id, duplicate.ticket.id)
        self.assertEqual(len(self.flow.actions.registrations), 1)

    def test_metadata_and_initial_snapshot_roll_back_together_on_storage_failure(self):
        with patch.object(self.audit, "_write_snapshot", side_effect=sqlite3.OperationalError("injected failure")):
            with self.assertRaises(StorageError):
                self.create("failed")
        self.assertEqual(self.rows("tickets"), 0)
        self.assertEqual(self.rows("submission_keys"), 0)
        self.assertEqual(self.rows("submission_cases"), 0)
        self.assertEqual(self.rows("operations"), 0)
        self.create("retry")
        self.assertEqual(len(self.flow.actions.registrations), 1)

    def test_crash_after_reservation_and_admin_recovery_keep_one_case(self):
        with patch.object(self.agent, "_continue", side_effect=SystemExit("crash before first node")):
            with self.assertRaises(SystemExit):
                self.create("crashed")
        original = self.audit.list_tickets()[0]
        duplicate = self.create("new-key")
        self.assertEqual(duplicate.ticket.id, original["id"])
        self.assertFalse(self.flow.actions.registrations)
        recovered = self.service.recover(self.flow.manager, original["id"])
        self.assertEqual(recovered.ticket.status, "awaiting_support")
        self.assertEqual(self.create("crashed").ticket.id, original["id"])
        self.assertEqual(len(self.flow.actions.registrations), 1)

    def test_restart_and_pre_upgrade_cases_are_deduplicated_without_changing_old_receipts(self):
        old = self.agent.run(Ticket(**self.body, requester="alice", department="IT", owner_id="alice"))
        before = self.audit.get_operation(old.action_result.idempotency_key)
        restarted = EnterpriseAgent(self.agent.tools, AuditStore(self.audit.db_path), settings=self.agent.settings)
        service = TicketService(restarted, self.flow.identities)
        duplicate = service.create(self.flow.user, **self.body, idempotency_key="after-upgrade")
        self.assertEqual(old.ticket.id, duplicate.ticket.id)
        self.assertEqual(before, self.audit.get_operation(old.action_result.idempotency_key))
        self.assertEqual(len(self.flow.actions.registrations), 1)

    def test_legacy_client_key_keeps_original_receipt(self):
        key = "request:" + hashlib.sha256(json.dumps(["alice", "legacy"], ensure_ascii=False).encode()).hexdigest()
        old = self.agent.run(Ticket(**self.body, requester="alice", department="IT", owner_id="alice", idempotency_key=key))
        replay = self.create("legacy")
        self.assertEqual(old.to_dict(), replay.to_dict())
        self.assertEqual(len(self.flow.actions.registrations), 1)

    def test_http_and_cli_projections_explain_reuse_without_internal_payload(self):
        with TestClient(create_app(self.agent, identities=self.flow.identities)) as client:
            headers = auth_headers(self.flow.keys)
            first = client.post("/portal/tickets", headers={**headers, "Idempotency-Key": "portal"}, json=self.body)
            self.assertEqual(first.status_code, 201, first.text)
            duplicate = client.post("/portal/tickets", headers={**headers, "Idempotency-Key": "refresh"}, json=self.body)
            self.assertEqual(duplicate.status_code, 200, duplicate.text)
            self.assertEqual(duplicate.json()["id"], first.json()["id"])
            self.assertTrue(duplicate.json()["submission"]["reused"])
            legacy = client.post("/tickets", headers=headers, json=self.body)
            self.assertEqual(legacy.status_code, 200, legacy.text)
            self.assertEqual(legacy.json()["ticket"]["id"], first.json()["id"])
            self.assertNotIn("trace", legacy.json())
            self.assertNotIn("business_fingerprint", legacy.text)
        output = self.service.response(self.flow.user, self.create("cli"))
        self.assertTrue(output["submission"]["reused"])

    def test_separate_cli_processes_with_different_keys_create_one_case(self):
        env = {**os.environ, "AGENT_DATA_DIR": str(self.flow.root), "AGENT_DB_PATH": str(self.audit.db_path),
               "AGENT_AUTH_FILE": str(self.flow.identities.path), "AGENT_KB_DIR": str(self.flow.root / "kb"),
               "AGENT_LLM_MODE": "offline", "AGENT_RAG_BACKEND": "lexical", "AGENT_DIAGNOSIS_MODE": "off",
               "AGENT_ACTION_BACKEND": "simulated", "DEMO_API_KEY": self.flow.keys["alice"]}
        processes = [subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", "app.cli", "run",
            "--title", self.body["title"], "--description", self.body["description"], "--idempotency-key", str(index)],
            cwd=Path(__file__).resolve().parents[1], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for index in range(3)]
        results = []
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=40)
                self.assertEqual(process.returncode, 0, stderr)
                results.append(json.loads(stdout))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=10)
        self.assertEqual(len({result["ticket"]["id"] for result in results}), 1)
        self.assertEqual(self.rows("tickets"), 1)
        events = self.audit.list_events(results[0]["ticket"]["id"])
        self.assertEqual(sum(event["node"] == "execute" for event in events), 1)

    def test_response_loss_after_downstream_commit_does_not_repeat_http_write(self):
        remote = http_fixtures.HTTPActionTests("test_concurrent_same_key_only_creates_one_record")
        remote.setUp()
        try:
            with remote.server("--test-after-commit-delay", "0.8") as base:
                self.agent.tools.adapter = IncidentHTTPAdapter(base, remote.key, timeout=.15)
                first = self.create("timeout")
                self.assertEqual(first.action_result.status, "unknown")
                duplicate = self.create("new-after-timeout")
                self.assertEqual(first.ticket.id, duplicate.ticket.id)
                self.assertEqual(duplicate.action_result.status, "unknown")
                self.assertEqual(remote.count("incidents"), 1)
        finally:
            remote.tearDown()

    def test_completed_window_can_be_disabled_without_disabling_request_idempotency(self):
        self.agent.settings = self.agent.settings.model_copy(update={"submission_completed_window_seconds": 0})
        body = {"title": "重置密码", "description": "忘记密码"}
        first = self.create("one", **body)
        second = self.create("two", **body)
        self.assertNotEqual(first.ticket.id, second.ticket.id)
        self.assertEqual(self.create("one", **body).ticket.id, first.ticket.id)
        self.assertEqual(len(self.flow.actions.registrations), 2)


if __name__ == "__main__":
    unittest.main()
