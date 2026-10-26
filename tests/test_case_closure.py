"""Business closure, version checks, wait expiry and multi-report delivery."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import unittest

from fastapi.testclient import TestClient

import test_case_flow as fixtures
import test_http_actions as http_fixtures
from app.api import create_app
from app.case_management import CaseLifecycle
from app.demo_downstream import IncidentStore, create_app as downstream_app
from app.errors import AuthorizationError, IdempotencyConflict
from app.http_actions import IncidentHTTPAdapter
from app.models import AgentState
from app.policy import input_digest
from auth_helpers import auth_headers


class CaseClosureTests(unittest.TestCase):
    def setUp(self):
        self.env = fixtures.CaseFlowTests("test_registered_incident_remains_open_and_can_be_resolved_without_report")
        self.env.setUp()
        self.addCleanup(self.env.tearDown)

    def load(self, ticket_id):
        return AgentState.from_dict(self.env.audit.get_state(ticket_id))

    def assigned(self):
        return self.env.claim(self.env.create())

    def propose(self, state):
        return self.env.service.resolve(self.env.manager, state.ticket.id, "resolved", "支持人员已处理", "record-1", case_revision=state.ticket.case_revision)

    def force_wait(self, state, *, expired=False):
        waiting = state.ticket.waiting
        waiting["remind_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        waiting["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=-.5 if expired else 1)).isoformat()
        self.env.audit.save_checkpoint(state.to_dict(), "wait_clock_fixture")

    def test_support_resolution_requires_owner_verification_and_replays_safely(self):
        state = self.assigned()
        pending = self.propose(state)
        self.assertEqual(pending.ticket.status, "awaiting_verification")
        self.assertIsNone(pending.resolution["external_issue_resolved"])
        self.assertTrue(pending.resolution["support_issue_resolved"])
        self.assertEqual(self.propose(state).to_dict(), pending.to_dict())
        with self.assertRaises(AuthorizationError):
            self.env.service.verify_case(self.env.manager, state.ticket.id, pending.ticket.case_revision, "confirmed")
        done = self.env.service.verify_case(self.env.user, state.ticket.id, pending.ticket.case_revision, "confirmed")
        self.assertEqual(done.ticket.status, "resolved")
        self.assertTrue(done.resolution["external_issue_resolved"])
        self.assertEqual(done.ticket.assignment["assignee"], "manager")
        self.assertEqual(self.env.service.verify_case(self.env.user, state.ticket.id, pending.ticket.case_revision, "confirmed").to_dict(), done.to_dict())
        self.assertEqual(len(self.env.actions.registrations), 1)

    def test_still_failing_keeps_receipt_plan_report_and_processing_history(self):
        original = self.assigned()
        pending = self.propose(original)
        resumed = self.env.service.verify_case(self.env.user, original.ticket.id, pending.ticket.case_revision, "still_failing", "实际验证仍报错")
        self.assertEqual(resumed.ticket.status, "in_progress")
        self.assertFalse(resumed.resolution["external_issue_resolved"])
        self.assertEqual(resumed.action_result.to_dict(), original.action_result.to_dict())
        self.assertEqual(resumed.ticket.execution_plan, original.ticket.execution_plan)
        self.assertTrue(resumed.ticket.resolution_history)
        self.assertEqual(input_digest(original.ticket, original.conversation_history), input_digest(resumed.ticket, resumed.conversation_history))
        self.assertIsNone(resumed.ticket.assignment["paused_at"])

    def test_recurrence_reopens_same_case_and_an_expired_window_rejects(self):
        pending = self.propose(self.assigned())
        closed = self.env.service.verify_case(self.env.user, pending.ticket.id, pending.ticket.case_revision, "confirmed")
        reopened = self.env.service.verify_case(self.env.user, closed.ticket.id, closed.ticket.case_revision, "recurred", "今天再次发生")
        self.assertEqual(reopened.ticket.status, "in_progress")
        self.assertGreater(datetime.fromisoformat(reopened.ticket.assignment["due_at"]), datetime.now(timezone.utc))
        self.assertEqual(len(self.env.actions.registrations), 1)
        pending = self.propose(reopened)
        closed = self.env.service.verify_case(self.env.user, pending.ticket.id, pending.ticket.case_revision, "confirmed")
        closed.resolution["reopen_until"] = "2020-01-01T00:00:00+00:00"
        self.env.audit.save_checkpoint(closed.to_dict(), "expired_window_fixture")
        with self.assertRaises(IdempotencyConflict):
            self.env.service.verify_case(self.env.user, closed.ticket.id, closed.ticket.case_revision, "recurred", "已过重开窗口")

    def test_waiting_for_info_has_one_reminder_then_returns_to_support(self):
        state = self.assigned()
        waiting = self.env.service.request_info(self.env.manager, state.ticket.id, "occurred_at", state.ticket.assignment["revision"])
        self.force_wait(waiting)
        self.env.agent.case_interactions.maintain(); self.env.agent.case_interactions.maintain()
        reminded = self.load(state.ticket.id)
        self.assertEqual(sum(row["kind"] == "wait_reminder" for row in reminded.ticket.case_events), 1)
        self.force_wait(reminded, expired=True)
        self.env.agent.case_interactions.maintain()
        resumed = self.load(state.ticket.id)
        self.assertEqual(resumed.ticket.status, "in_progress")
        self.assertIsNone(resumed.ticket.waiting)
        self.assertIsNone(resumed.ticket.assignment["paused_at"])
        with self.assertRaises(IdempotencyConflict):
            self.env.service.follow_up(self.env.user, state.ticket.id, "旧页面回复", waiting.ticket.case_revision)
        answered = self.env.service.follow_up(self.env.user, state.ticket.id, "刷新后补充时间", resumed.ticket.case_revision)
        self.assertEqual(answered.ticket.status, "in_progress")

    def test_verification_timeout_is_archived_unconfirmed_and_can_reopen(self):
        pending = self.propose(self.assigned())
        self.force_wait(pending, expired=True)
        self.env.agent.case_interactions.maintain()
        archived = self.load(pending.ticket.id)
        self.assertEqual(archived.ticket.status, "archived")
        self.assertIsNone(archived.resolution["external_issue_resolved"])
        self.assertTrue(CaseLifecycle.can_reopen(archived))
        reopened = self.env.service.verify_case(self.env.user, archived.ticket.id, archived.ticket.case_revision, "still_failing", "未验证，问题仍在")
        self.assertEqual(reopened.ticket.status, "in_progress")
        self.assertEqual(len(self.env.actions.registrations), 1)

    def test_waiting_user_can_withdraw_or_staff_can_archive_without_claiming_a_fix(self):
        state = self.assigned()
        waiting = self.env.service.request_info(self.env.manager, state.ticket.id, "occurred_at", state.ticket.assignment["revision"])
        cancelled = self.env.service.cancel(self.env.user, state.ticket.id, "我不再需要处理", case_revision=waiting.ticket.case_revision)
        self.assertEqual(cancelled.ticket.status, "cancelled")
        self.assertEqual(cancelled.action_result.to_dict(), state.action_result.to_dict())
        self.assertIsNone(cancelled.resolution["external_issue_resolved"])
        another = self.assigned()
        waiting = self.env.service.request_info(self.env.manager, another.ticket.id, "occurred_at", another.ticket.assignment["revision"])
        archived = self.env.service.resolve(self.env.manager, another.ticket.id, "archived", "无法联系申请人，保留问题未解决记录", case_revision=waiting.ticket.case_revision)
        self.assertEqual(archived.ticket.status, "archived")
        self.assertIsNone(archived.resolution["external_issue_resolved"])

    def test_unknown_operation_cannot_be_hidden_by_withdrawal_or_unproven_archive(self):
        state = self.assigned()
        waiting = self.env.service.request_info(self.env.manager, state.ticket.id, "occurred_at", state.ticket.assignment["revision"])
        def timeout(): raise TimeoutError()
        with self.assertRaises(TimeoutError):
            self.env.audit.once("unknown-write", {}, timeout, ticket_id=state.ticket.id, kind="tool")
        with self.assertRaises(IdempotencyConflict):
            self.env.service.cancel(self.env.user, state.ticket.id, "撤回", case_revision=waiting.ticket.case_revision)
        with self.assertRaises(IdempotencyConflict):
            self.env.service.resolve(self.env.manager, state.ticket.id, "archived", "未核查", case_revision=waiting.ticket.case_revision)
        archived = self.env.service.resolve(self.env.manager, state.ticket.id, "archived", "已核对回执，结束受理", "verified-receipt", case_revision=waiting.ticket.case_revision)
        self.assertEqual(archived.ticket.status, "archived")
        self.assertIsNone(archived.resolution["external_issue_resolved"])

    def test_old_report_page_cannot_feedback_or_resolve_a_newer_report(self):
        self.env.investigative_agent([fixtures.finish().model_copy(update={"summary": "报告 A"}), fixtures.finish().model_copy(update={"summary": "报告 B"})])
        registered = self.env.create("VPN 报错 503", "VPN 故障")
        first = self.env.claim(self.env.agent.investigate(registered.ticket.id))
        self.env.service.follow_up(self.env.user, first.ticket.id, "补充最新线索", first.ticket.case_revision)
        latest = self.env.agent.investigate(first.ticket.id)
        self.assertNotEqual(first.ticket.diagnostic_report, latest.ticket.diagnostic_report)
        with TestClient(create_app(self.env.agent, identities=self.env.identities)) as client:
            headers = auth_headers(self.env.keys, "manager")
            payload = dict(revision=1, verdict="accepted", note="核查 A", evidence="record-A", issue_resolved=True, case_revision=first.ticket.case_revision)
            url = "/tickets/" + first.ticket.id
            self.assertEqual(client.post(url + "/feedback", headers=headers, json=payload).status_code, 409)
            self.assertEqual(client.post(url + "/resolve", headers=headers, json={"outcome":"resolved", "note":"核查 A", "case_revision":first.ticket.case_revision}).status_code, 409)
            without_version = dict(payload); without_version.pop("case_revision")
            self.assertEqual(client.post(url + "/feedback", headers=headers, json=without_version).status_code, 422)
            payload["case_revision"] = latest.ticket.case_revision
            accepted = client.post(url + "/feedback", headers=headers, json=payload)
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(accepted.json()["ticket"]["status"], "awaiting_verification")
            self.assertEqual(self.load(first.ticket.id).ticket.feedback[-1]["report_hash"], CaseLifecycle.report_hash(latest))

    def test_owner_endpoints_enforce_identity_revision_and_replay(self):
        pending = self.propose(self.assigned())
        with TestClient(create_app(self.env.agent, identities=self.env.identities)) as client:
            path = "/portal/tickets/" + pending.ticket.id + "/verify"
            body = {"revision": pending.ticket.case_revision, "verdict": "confirmed"}
            self.assertEqual(client.post(path, json=body).status_code, 401)
            self.assertEqual(client.post(path, headers=auth_headers(self.env.keys, "manager"), json=body).status_code, 403)
            self.assertEqual(client.post(path, headers=auth_headers(self.env.keys), json={**body,"revision":body["revision"]-1}).status_code, 409)
            result = client.post(path, headers=auth_headers(self.env.keys), json=body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["status"]["code"], "resolved")
            self.assertEqual(client.post(path, headers=auth_headers(self.env.keys), json=body).json(), result.json())

    def test_legacy_wait_snapshot_gets_a_bounded_wait(self):
        state = self.assigned()
        state.ticket.status = "awaiting_user_info"
        state.ticket.assignment["paused_at"] = "2020-01-01T00:00:00+00:00"
        state.ticket.waiting = None
        self.env.audit.save_checkpoint(state.to_dict(), "legacy_wait_fixture")
        self.env.agent.case_interactions.maintain()
        self.assertEqual(self.load(state.ticket.id).ticket.status, "in_progress")

    def test_owner_can_report_self_recovery_while_waiting_for_information(self):
        state = self.assigned()
        waiting = self.env.service.request_info(self.env.manager, state.ticket.id, "occurred_at", state.ticket.assignment["revision"])
        done = self.env.service.verify_case(self.env.user, state.ticket.id, waiting.ticket.case_revision, "confirmed", "自己重连后实际验证正常")
        self.assertEqual(done.ticket.status, "resolved")
        self.assertTrue(done.resolution["external_issue_resolved"])
        self.assertEqual(done.action_result.to_dict(), state.action_result.to_dict())


class VersionedAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.env = http_fixtures.HTTPActionTests("test_concurrent_same_key_only_creates_one_record")
        self.env.setUp()
        self.addCleanup(self.env.tearDown)

    def test_actual_http_two_investigations_keep_both_reports_and_one_incident(self):
        flow = fixtures.CaseFlowTests("test_registered_incident_remains_open_and_can_be_resolved_without_report")
        flow.setUp()
        self.addCleanup(flow.tearDown)
        flow.investigative_agent([fixtures.finish().model_copy(update={"summary":"第一轮"}), fixtures.finish().model_copy(update={"summary":"第二轮"})])
        with self.env.server() as base:
            flow.agent.tools.adapter = IncidentHTTPAdapter(base, self.env.key)
            state = flow.create("VPN 报错超时", "VPN 故障")
            first = flow.agent.investigate(state.ticket.id)
            self.assertEqual(first.enrichment_result["status"], "succeeded")
            flow.service.follow_up(flow.user, state.ticket.id, "新的报错线索", first.ticket.case_revision)
            second = flow.agent.investigate(state.ticket.id)
            self.assertEqual(second.enrichment_result["status"], "succeeded")
            self.assertEqual(self.env.count("incidents"), 1)
            self.assertEqual(self.env.count("attachments"), 2)
            with closing(sqlite3.connect(self.env.root/"remote.db")) as db:
                rows = db.execute("SELECT version,payload FROM attachments ORDER BY version").fetchall()
            self.assertEqual([row[0] for row in rows], [1,2])
            self.assertEqual([json.loads(row[1])["payload"]["report"]["summary"] for row in rows], ["第一轮","第二轮"])

    def test_legacy_schema_migration_preserves_payload_and_receipt_replay(self):
        path = self.env.root / "legacy.db"
        old_payload = {"ticket_id":"legacy", "report":{"summary":"原始报告"}}
        encoded = json.dumps({"action":"attach_evidence","payload":old_payload}, sort_keys=True, ensure_ascii=False)
        receipt = dict(action="attach_evidence",status="succeeded",idempotency_key="old-key",ticket_id="legacy",downstream_id="D-legacy",simulated=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("CREATE TABLE incidents(ticket_id TEXT PRIMARY KEY,id TEXT,payload TEXT); CREATE TABLE attachments(ticket_id TEXT PRIMARY KEY,payload TEXT); CREATE TABLE receipts(key TEXT PRIMARY KEY,fingerprint TEXT,payload TEXT);")
            db.execute("INSERT INTO incidents VALUES(?,?,?)",("legacy","D-legacy","{}"))
            db.execute("INSERT INTO attachments VALUES(?,?)",("legacy",encoded))
            db.execute("INSERT INTO receipts VALUES(?,?,?)",("old-key",hashlib.sha256(encoded.encode()).hexdigest(),json.dumps(receipt)))
        store = IncidentStore(path)
        self.assertEqual(store.lookup("old-key"), receipt)
        with TestClient(downstream_app(path, self.env.key)) as client:
            headers={"Authorization":"Bearer "+self.env.key,"Idempotency-Key":"old-key"}
            self.assertEqual(client.post("/attachments",headers=headers,json=old_payload).json(),receipt)
            newer={"ticket_id":"legacy","version":2,"report":{"summary":"第二版"}}
            new_headers={**headers,"Idempotency-Key":"new-key"}
            self.assertEqual(client.post("/attachments",headers=new_headers,json=newer).status_code,200)
            self.assertEqual(client.post("/attachments",headers=new_headers,json=newer).status_code,200)
            self.assertEqual(client.post("/attachments",headers={**headers,"Idempotency-Key":"other"},json={**newer,"report":{"summary":"试图覆盖"}}).status_code,409)
        IncidentStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("SELECT payload FROM attachments WHERE ticket_id='legacy' AND version=1").fetchone()[0],encoded)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0],2)


if __name__ == "__main__":
    unittest.main()
