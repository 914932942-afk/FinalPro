"""Goal-level case flows: ownership, clarification, investigation follow-up and safe closure."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from app.api import create_app
from app.audit import AuditStore
from app.auth import Principal
from app.case_management import CaseLifecycle
from app.config import get_settings
from app.errors import AuthorizationError, IdempotencyConflict, OperationPending, RequestValidationError
from app.investigation import InvestigationTools, InvestigationRunner, InvestigationStep
from app.knowledge import KnowledgeBase
from app.models import AgentState, Ticket
from app.action_adapter import SimulatedActionAdapter
from app.portal import PortalService
from app.service import TicketService
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities, auth_headers

ROOT = Path(__file__).resolve().parents[1]

class CountingActions(SimulatedActionAdapter):
    def __init__(self):
        self.registrations = []
        self.attachments = []
    def execute(self, ticket, action, *, idempotency_key):
        self.registrations.append((ticket.id, action, idempotency_key))
        return super().execute(ticket, action, idempotency_key=idempotency_key)
    def attach_evidence(self, ticket, *, idempotency_key):
        self.attachments.append(idempotency_key)
        return super().attach_evidence(ticket, idempotency_key=idempotency_key)

class ScriptModel:
    source = "test"
    def __init__(self, steps):
        self.steps = iter(steps)
        self.texts = []
    def choose(self, text, *args, **kwargs):
        self.texts.append(text)
        return next(self.steps), {}

def ask(field="occurred_at"):
    return InvestigationStep(kind="ask_user", tool=None, argument="", assessment=None, runbook_id=None,
        summary="", next_check="", citations=[], reason="需要用户提供发生时间", missing_fact=field)

def finish():
    return InvestigationStep(kind="finish", tool=None, argument="", assessment="insufficient", runbook_id=None,
        summary="日志不足，需要支持人员进一步核查。", next_check="核查日志来源", citations=[], reason="证据不足")

class CaseFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.identities, self.keys = demo_identities(self.root)
        self.cfg = get_settings(_env_file=None, data_dir=self.root, llm_mode="offline", rag_backend="lexical",
            runbook_strategy="bm25", diagnosis_mode="off", diagnosis_background=False)
        self.audit = AuditStore(self.cfg.db_path)
        self.actions = CountingActions()
        self.agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(self.root / "kb", settings=self.cfg), self.audit, adapter=self.actions), self.audit, settings=self.cfg)
        self.service = TicketService(self.agent); self.portal = PortalService(self.service)
        self.user = Principal(user_id="alice", role="employee", department="IT")
        self.manager = Principal(user_id="manager", role="manager", department="IT")
        self.admin = Principal(user_id="admin", role="admin", department="IT")
    def tearDown(self): self.temp.cleanup()
    def create(self, description="ERP 报错 503", title="ERP 故障"):
        return self.service.create(self.user, title=title, description=description)
    def claim(self, state, actor=None):
        return self.service.claim(actor or self.manager, state.ticket.id, (state.ticket.assignment or {}).get("revision", 0))
    def investigative_agent(self, steps):
        model = ScriptModel(steps)
        adapter = InvestigationTools(ROOT / "data/diagnostics/it_events.json", ROOT / "data/diagnostics/runbooks.json", "vpn")
        runner = InvestigationRunner(adapter, model, prefetch=True)
        self.agent.settings = self.cfg.model_copy(update={"diagnosis_mode": "react"})
        self.agent.diagnostic_runner = runner
        return model

    def test_registered_incident_remains_open_and_can_be_resolved_without_report(self):
        state = self.create()
        self.assertEqual(state.ticket.status, "awaiting_support")
        self.assertIsNone(state.ticket.diagnostic_report)
        self.assertIsNotNone(state.ticket.assignment["due_at"])
        with self.assertRaises(IdempotencyConflict):
            self.service.resolve(self.manager, state.ticket.id, "resolved", "已恢复", "test-record")
        state = self.claim(state)
        done = self.service.resolve(self.manager, state.ticket.id, "resolved", "支持人员已核查恢复", "test-record")
        self.assertEqual(done.ticket.status, "resolved")
        self.assertTrue(done.resolution["external_issue_resolved"])
        self.assertEqual(len(self.actions.registrations), 1)

    def test_unresolved_feedback_keeps_an_assigned_actionable_case(self):
        state = self.claim(self.create())
        state = self.service.feedback(self.manager, state.ticket.id, 1, "inconclusive", "仍未恢复", "test-record", False)
        self.assertEqual(state.ticket.status, "in_progress")
        support = self.portal.get(self.manager, state.ticket.id, "support")["support"]
        self.assertTrue(support["can_resolve"])
        self.assertTrue(support["can_request_info"])
        state = self.service.feedback(self.manager, state.ticket.id, 2, "accepted", "已核查恢复", "test-record-2", True)
        self.assertEqual(state.ticket.status, "resolved")
        self.assertEqual(len(self.actions.registrations), 1)

    def test_non_assignee_cannot_resolve_or_feedback(self):
        state = self.claim(self.create())
        with self.assertRaises(IdempotencyConflict):
            self.service.resolve(self.admin, state.ticket.id, "resolved", "抢先结案", "evidence")
        with self.assertRaises(IdempotencyConflict):
            self.service.feedback(self.admin, state.ticket.id, 1, "accepted", "抢先反馈", "evidence", True)
        self.assertEqual(self.audit.get_ticket(state.ticket.id)["assignment"]["assignee"], "manager")

    def test_unresolved_feedback_after_manual_resolution_allows_a_new_resolution(self):
        state = self.claim(self.create())
        first = self.service.resolve(self.manager, state.ticket.id, "resolved", "首次核查恢复", "record-1")
        state = self.service.feedback(self.manager, state.ticket.id, 1, "inconclusive", "复查仍未恢复", "record-2", False)
        self.assertEqual(state.ticket.status, "in_progress")
        done = self.service.resolve(self.manager, state.ticket.id, "resolved", "再次处理并核实恢复", "record-3")
        self.assertEqual(done.ticket.status, "resolved")
        self.assertEqual(done.resolution["evidence"], "record-3")
        replay = self.service.resolve(self.manager, state.ticket.id, "resolved", "再次处理并核实恢复", "record-3")
        self.assertEqual(done.to_dict(), replay.to_dict())
        self.assertNotEqual(first.resolution, done.resolution)
        self.assertEqual(len(self.actions.registrations), 1)

    def test_unknown_attachment_cannot_be_closed_by_feedback(self):
        state = self.claim(self.create())
        key = f"evidence:{state.ticket.id}:v1"
        with self.assertRaises(TimeoutError):
            self.audit.once(key, {"test": True}, lambda: (_ for _ in ()).throw(TimeoutError()),
                            ticket_id=state.ticket.id, kind="tool")
        state.ticket.investigation_status = "attachment_unknown"
        self.audit.save_checkpoint(state.to_dict(), "unknown_attachment_fixture")
        self.assertFalse(self.portal.get(self.manager, state.ticket.id, "support")["support"]["can_feedback"])
        with self.assertRaises(IdempotencyConflict):
            self.service.feedback(self.manager, state.ticket.id, 1, "accepted", "声称恢复", "record-1", True)
        self.assertEqual(self.audit.get_ticket(state.ticket.id)["status"], "in_progress")
        with self.assertRaises(IdempotencyConflict):
            self.service.resolve(self.manager, state.ticket.id, "resolved", "恢复", "")
        done = self.service.resolve(self.manager, state.ticket.id, "resolved", "已核查附件及问题恢复", "verified-receipt")
        self.assertEqual(done.ticket.status, "resolved")
        self.assertEqual(self.audit.get_operation(key)["status"], "reconciled")

    def test_case_revisions_are_validated_for_non_http_callers(self):
        state = self.create()
        for revision in (False, -1, "0", 10001):
            with self.subTest(revision=revision), self.assertRaises(RequestValidationError):
                self.service.claim(self.manager, state.ticket.id, revision)
        self.assertIsNone(self.audit.get_ticket(state.ticket.id)["assignment"]["assignee"])

    def test_concurrent_claim_has_one_owner_and_replay_does_not_add_history(self):
        state = self.create(); results = []; barrier = threading.Barrier(2)
        def run(actor):
            barrier.wait()
            try: results.append((actor.user_id, self.service.claim(actor, state.ticket.id, 0)))
            except (IdempotencyConflict, OperationPending): results.append((actor.user_id, None))
        threads = [threading.Thread(target=run, args=(actor,)) for actor in (self.manager, self.admin)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5); self.assertFalse(thread.is_alive())
        winners = [row for row in results if row[1] is not None]
        self.assertEqual(len(results), 2)
        self.assertEqual(len(winners), 1)
        actor = self.manager if winners[0][0] == "manager" else self.admin
        replay = self.service.claim(actor, state.ticket.id, 0)
        self.assertEqual(len(replay.ticket.assignment["history"]), 1)
        self.assertEqual(replay.ticket.assignment["assignee"], actor.user_id)

    def test_transfer_validates_identity_department_and_releases_previous_owner(self):
        state = self.claim(self.create())
        state = self.service.transfer(self.manager, state.ticket.id, self.admin, 1, "交管理员进一步核查")
        self.assertEqual(state.ticket.assignment["assignee"], "admin")
        self.assertEqual(len(state.ticket.assignment["history"]), 2)
        with self.assertRaises(IdempotencyConflict):
            self.service.request_info(self.manager, state.ticket.id, "symptom", 2)
        foreign = Principal(user_id="other", role="manager", department="Finance")
        with self.assertRaises(AuthorizationError):
            self.service.transfer(self.admin, state.ticket.id, foreign, 2, "跨部门转派")
        with self.assertRaises(AuthorizationError):
            self.service.transfer(self.admin, state.ticket.id, self.user, 2, "转给申请人")

    def test_admin_can_explicitly_take_over_with_reason(self):
        state = self.claim(self.create())
        state = self.service.transfer(self.admin, state.ticket.id, self.admin, 1, "原处理人不可用，管理员接管")
        self.assertEqual(state.ticket.assignment["assignee"], "admin")
        self.assertEqual(state.ticket.assignment["history"][-1]["reason"], "原处理人不可用，管理员接管")

    def test_employee_and_self_applicant_cannot_claim(self):
        state = self.create()
        with self.assertRaises(AuthorizationError): self.service.claim(self.user, state.ticket.id, 0)
        own = self.service.create(self.manager, title="ERP 故障", description="ERP 报错 503")
        with self.assertRaises(AuthorizationError): self.service.claim(self.manager, own.ticket.id, 0)

    def test_support_question_and_reply_preserve_receipt_and_current_owner(self):
        state = self.claim(self.create()); original = state.action_result.to_dict()
        state = self.service.request_info(self.manager, state.ticket.id, "occurred_at", 1)
        self.assertEqual(state.ticket.status, "awaiting_user_info")
        self.assertIsNotNone(state.ticket.assignment["paused_at"])
        result = self.service.follow_up(self.user, state.ticket.id, "今天上午10点开始", state.ticket.case_revision)
        self.assertEqual(result.ticket.status, "in_progress")
        self.assertEqual(result.ticket.assignment["assignee"], "manager")
        self.assertIsNone(result.ticket.assignment["paused_at"])
        self.assertEqual(result.action_result.to_dict(), original)
        self.assertEqual(len(self.actions.registrations), 1)
        replay = self.service.follow_up(self.user, result.ticket.id, "今天上午10点开始", state.ticket.case_revision)
        self.assertEqual(replay.to_dict(), result.to_dict())
        with self.assertRaises(IdempotencyConflict):
            self.service.follow_up(self.user, result.ticket.id, "换一个答案", state.ticket.case_revision)

    def test_investigator_asks_user_and_continues_same_registered_incident(self):
        model = self.investigative_agent([ask(), finish()])
        first = self.create("VPN 报错 503", "VPN 故障")
        self.assertEqual(first.ticket.status, "awaiting_investigation")
        waiting = self.agent.investigate(first.ticket.id)
        self.assertEqual(waiting.ticket.status, "awaiting_user_info")
        self.assertEqual(waiting.clarification["stage"], "investigation")
        resumed = self.service.follow_up(self.user, first.ticket.id, "今天上午10点开始", waiting.ticket.case_revision)
        self.assertEqual(resumed.ticket.status, "awaiting_investigation")
        done = self.agent.investigate(first.ticket.id)
        self.assertEqual(done.ticket.status, "awaiting_support")
        self.assertEqual(done.ticket.investigation_attempt, 2)
        self.assertEqual(len(done.investigation_history), 1)
        self.assertIn("今天上午10点开始", model.texts[-1])
        self.assertEqual(len(self.actions.registrations), 1)
        self.assertEqual(self.actions.attachments, [f"evidence:{first.ticket.id}:v2"])
        self.assertEqual(done.action_result.to_dict(), first.action_result.to_dict())

    def test_repeated_user_questions_are_bounded_and_handoff_remains_actionable(self):
        self.investigative_agent([ask(), ask("symptom"), ask("reproduction")])
        state = self.create("VPN 报错 503", "VPN 故障")
        for answer in ("今天10点", "VPN 报错 503"):
            state = self.agent.investigate(state.ticket.id)
            self.assertEqual(state.ticket.status, "awaiting_user_info")
            state = self.service.follow_up(self.user, state.ticket.id, answer, state.ticket.case_revision)
        state = self.agent.investigate(state.ticket.id)
        self.assertEqual(state.ticket.status, "awaiting_support")
        self.assertEqual(state.diagnosis["stop_reason"], "user_question_budget")
        state = self.claim(state)
        self.assertTrue(self.portal.get(self.manager, state.ticket.id, "support")["support"]["can_resolve"])
        self.assertEqual(len(self.actions.registrations), 1)

    def test_help_wording_and_uncertainty_in_incidents_do_not_trigger_manual_loops(self):
        for text in ("ERP 报错 503，怎么处理？", "ERP 报错 503，如何恢复？", "ERP 可能出问题，连接超时"):
            with self.subTest(text=text):
                state = self.create(text)
                self.assertEqual(state.ticket.status, "awaiting_support")
        for text in ("如何重置密码？", "如果 ERP 报错 503，要怎么处理？", "不要开通 ERP 使用权限", "替 bob 重置密码"):
            with self.subTest(text=text):
                before = len(self.actions.registrations)
                state = self.create(text, "服务咨询")
                self.assertIn(state.ticket.status, {"manual_review", "awaiting_clarification"})
                self.assertEqual(len(self.actions.registrations), before)

    def test_uncertain_sensitive_request_still_needs_bound_plan_confirmation(self):
        state = self.create("可能需要重置密码", "账号问题")
        self.assertEqual(state.ticket.status, "awaiting_clarification")
        self.assertEqual(len(self.actions.registrations), 0)
        state = self.service.clarify(self.user, state.ticket.id, "确认需要重置本人密码", state.clarification_round)
        self.assertIn(state.ticket.status, {"closed", "awaiting_clarification"})
        if state.ticket.status == "awaiting_clarification":
            self.assertEqual(state.confirmation["status"], "pending")
        else:
            self.assertEqual(state.ticket.recommended_action, "reset_password")

    def test_overdue_queue_escalates_once_and_excludes_waiting_user(self):
        state = self.create()
        state.ticket.assignment["due_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.audit.save_checkpoint(state.to_dict(), "fixture")
        self.agent.escalate_overdue(); self.agent.escalate_overdue()
        saved = self.audit.get_state(state.ticket.id)
        self.assertTrue(saved["ticket"]["assignment"]["escalated_at"])
        self.assertEqual(sum(row["node"] == "case_overdue" for row in self.audit.list_events(state.ticket.id)), 1)
        self.assertEqual(self.portal.list(self.manager, scope="support", queue="overdue")["total"], 1)
        state = self.claim(AgentState.from_dict(saved))
        state = self.service.request_info(self.manager, state.ticket.id, "symptom", state.ticket.assignment["revision"])
        self.assertFalse(CaseLifecycle.overdue(state.ticket.assignment))

    def test_migration_preserves_hypothesis_receipt_and_is_idempotent(self):
        state = self.create(); state.ticket.status = "closed"; state.ticket.assignment = None
        state.ticket.diagnostic_report = {"summary": "旧诊断假设", "observations": []}
        self.audit.save_checkpoint(state.to_dict(), "legacy_fixture")
        receipt = deepcopy(state.action_result.to_dict()); report = deepcopy(state.ticket.diagnostic_report)
        self.agent.migrate_cases(); before = self.audit.get_state(state.ticket.id); self.agent.migrate_cases()
        after = self.audit.get_state(state.ticket.id)
        self.assertEqual(before, after)
        self.assertEqual(after["ticket"]["status"], "awaiting_support")
        self.assertEqual(after["action_result"], receipt)
        self.assertEqual(after["ticket"]["diagnostic_report"], report)
        self.assertEqual(len(self.actions.registrations), 1)

    def test_http_scope_support_actions_and_queue_are_authorized(self):
        state = self.create(); url = "/portal/tickets/" + state.ticket.id
        with TestClient(create_app(self.agent, identities=self.identities)) as client:
            manager = auth_headers(self.keys, "manager"); employee = auth_headers(self.keys)
            self.assertEqual(client.post(url + "/claim", headers=employee, json={"revision": 0}).status_code, 403)
            self.assertEqual(client.post(url + "/claim", headers=manager, json={"revision": 0}).status_code, 200)
            view = client.get(url + "?scope=support", headers=manager).json()
            self.assertTrue(view["support"]["can_resolve"])
            self.assertTrue(view["support"]["assignees"])
            self.assertEqual(client.get("/portal/tickets?scope=support&queue=mine", headers=manager).json()["total"], 1)
            self.assertEqual(client.post(url + "/request-info", headers=manager, json={"revision": 1, "field": "occurred_at"}).status_code, 200)
            own = client.get(url, headers=employee).json()
            self.assertEqual(own["clarification"]["stage"], "investigation")
            self.assertNotIn("support", own)
            response = client.post(url + "/follow-up", headers=employee, json={"answer": "今天10点", "revision": own["case_revision"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"]["code"], "in_progress")

if __name__ == "__main__": unittest.main()
