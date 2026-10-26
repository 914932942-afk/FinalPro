"""独立表达反例、模型字段提取、本人确认及旧状态恢复的安全回归。"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.api import create_app
from app.audit import AuditStore
from app.auth import Principal
from app.clarification import ClarificationDecision
from app.config import get_settings
from app.errors import AuthorizationError, ConfigurationError, IdempotencyConflict, ModelOutputError
from app.evaluation import evaluate
from app.knowledge import KnowledgeBase
from app.model_adapter import Decision, DeepSeekDecisionModel
from app.models import AgentState
from app.policy import analyze_request, validate_execution
from app.service import TicketService
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities, auth_headers


class SemanticModel:
    def __init__(self, proposal):
        self.proposal = proposal
        self.clarify_calls = 0

    def clarify(self, *_):
        self.clarify_calls += 1
        return ClarificationDecision(False, interpretation=copy.deepcopy(self.proposal), source="stub")

    def decide(self, *_):
        action = self.proposal["action"]
        category = {"reset_password": "access_support", "grant_access": "access_request", "open_incident": "incident"}[action]
        return Decision(category, action, 0.95, "structured evidence", source="stub")


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = get_settings(_env_file=None, data_dir=self.root, llm_mode="offline", rag_backend="lexical")
        self.audit = AuditStore(self.root / "test.db")
        self.tools = EnterpriseTools(KnowledgeBase(self.root / "kb", backend="lexical"), self.audit)
        self.agent = EnterpriseAgent(self.tools, self.audit, settings=self.cfg)
        self.service = TicketService(self.agent)
        self.alice = Principal(user_id="alice", role="employee", department="IT")
        self.manager = Principal(user_id="manager", role="manager", department="IT", support_groups=["network", "applications", "data", "service_desk"])

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, title, description):
        return self.service.create(self.alice, title=title, description=description)

    def use_model(self, action="grant_access", resource="reports_db", permission="read_only", text="报表数据库只读"):
        proposal = {"speech_act": "request", "action": action, "resource": resource, "permission": permission,
                    "subject": "self", "multiple_intents": False, "confidence": 0.95,
                    "evidence": {"action": text, "resource": "报表数据库", "permission": "只读"}}
        model = SemanticModel(proposal)
        self.agent = EnterpriseAgent(self.tools, self.audit, model, settings=self.cfg)
        self.service = TicketService(self.agent)
        return model

    def test_negation_across_fields_and_after_request_never_executes(self):
        for title, description in [("密码重置", "暂时不需要，请不要执行"), ("不要", "重置密码"),
                                   ("重置密码", "这次不用了"), ("password reset", "do not proceed")]:
            with self.subTest(title=title, description=description), patch.object(self.tools, "execute_action") as execute:
                state = self.create(title, description)
                self.assertEqual(state.ticket.status, "awaiting_clarification")
                execute.assert_not_called()

    def test_third_party_targets_are_not_silently_replaced_with_owner(self):
        for title, description in [("Bob 的密码错误", "请协助处理"), ("重置密码", "账号是 bob"),
                                   ("重置密码", "这是同事的账号"), ("Bob's password expired", "please assist")]:
            with self.subTest(title=title), patch.object(self.tools, "execute_action") as execute:
                state = self.create(title, description)
                self.assertEqual(state.ticket.status, "manual_review")
                execute.assert_not_called()

    def test_authenticated_self_account_reference_is_accepted(self):
        state = self.create("重置密码", "账号是 alice")
        self.assertEqual(state.ticket.status, "closed")
        self.assertEqual(state.ticket.execution_plan["account"], "alice")

    def test_hypothetical_and_consultation_do_not_trigger_operations(self):
        for text in ("如果密码错误，是否就会重置密码", "假如密码过期会自动处理吗", "how to reset password"):
            state = self.create("咨询", text)
            self.assertEqual(state.ticket.status, "manual_review")
            self.assertIsNone(state.ticket.execution_plan)

    def test_ordinary_correction_replaces_previous_intent(self):
        state = self.create("可能密码错误", "VPN 连接失败")
        done = self.service.clarify(self.alice, state.ticket.id, "不是密码问题，是 VPN 连接超时，请登记故障", 1)
        self.assertEqual(done.ticket.status, "awaiting_support")
        self.assertEqual(done.ticket.recommended_action, "open_incident")
        self.assertEqual(done.ticket.description, "VPN 连接失败")
        self.assertNotIn("密码", done.understanding["text"])

    def test_access_and_incident_are_not_reduced_to_one_action(self):
        for description in ("同时 ERP 报错 503", "ERP 报错 503"):
            state = self.create("申请 ERP 普通使用权限", description)
            self.assertEqual(state.clarification["missing_fields"], ["single_intent"])
            self.assertIsNone(state.approval)

    def test_natural_access_request_is_understood_offline(self):
        state = self.create("需要查看报表数据库里的报表", "请给我只读权限")
        self.assertEqual(state.ticket.status, "awaiting_approval")
        self.assertEqual(state.ticket.execution_plan["permission"], "read_only")

    def test_password_report_requires_round_and_plan_bound_owner_confirmation(self):
        state = self.create("密码过期", "本人账号密码过期")
        self.assertEqual(state.ticket.status, "awaiting_clarification")
        self.assertEqual(state.confirmation["plan"]["account"], "alice")
        self.assertIsNone(state.action_result)
        with self.assertRaises(AuthorizationError):
            self.service.clarify(self.manager, state.ticket.id, "确认执行", 1)
        with self.assertRaises(IdempotencyConflict):
            self.service.clarify(self.alice, state.ticket.id, "确认执行", 2)
        restarted = TicketService(EnterpriseAgent(self.tools, AuditStore(self.audit.db_path), settings=self.cfg))
        done = restarted.clarify(self.alice, state.ticket.id, "确认执行", 1)
        self.assertEqual(done.ticket.status, "closed")
        self.assertEqual(done.confirmation["status"], "confirmed")
        self.assertEqual(restarted.clarify(self.alice, state.ticket.id, "确认执行", 1).to_dict(), done.to_dict())

    def test_confirmation_cannot_approve_changed_request(self):
        state = self.create("密码过期", "本人账号密码过期")
        changed = self.service.clarify(self.alice, state.ticket.id, "改成：申请报表数据库只读权限", 1)
        self.assertIsNone(changed.confirmation)
        self.assertEqual(changed.ticket.status, "awaiting_approval")
        with self.assertRaises(IdempotencyConflict):
            self.service.clarify(self.alice, state.ticket.id, "确认执行", 1)

    def test_model_extracts_intent_before_rule_classification_then_confirmation_and_approval(self):
        model = self.use_model(text="报表数据库只读就够了")
        state = self.create("办公需求", "报表数据库只读就够了")
        self.assertEqual(model.clarify_calls, 1)
        self.assertEqual(analyze_request(state.ticket).missing, ["intent"])
        self.assertEqual(state.ticket.status, "awaiting_clarification")
        self.assertEqual(state.confirmation["plan"]["permission"], "read_only")
        waiting = self.service.clarify(self.alice, state.ticket.id, "确认执行", 1)
        self.assertEqual(model.clarify_calls, 1)
        self.assertEqual(waiting.ticket.status, "awaiting_approval")
        done = self.service.approve(self.manager, waiting.approval.id, True)
        self.assertEqual(done.ticket.status, "closed")
        self.assertIsNone(done.resolution["external_issue_resolved"])

    def test_model_cannot_supply_unquoted_or_unknown_resource(self):
        for resource, quote in (("reports_db", "凭空捏造"), ("production_admin", "报表数据库")):
            model = self.use_model(text="报表数据库只读就够了")
            model.proposal["resource"] = resource
            model.proposal["evidence"]["resource"] = quote
            state = self.create("办公需求", "报表数据库只读就够了")
            self.assertNotEqual(state.ticket.status, "closed")
            self.assertIsNone(state.action_result)
            self.assertIsNone(state.confirmation)

    def test_model_cannot_override_negation_or_request_another_account(self):
        model = self.use_model(text="报表数据库只读")
        state = self.create("不要", "报表数据库只读")
        self.assertEqual(model.clarify_calls, 0)
        self.assertIsNone(state.confirmation)
        model.proposal["subject"] = "other"
        state = self.create("办公需求", "报表数据库只读")
        self.assertEqual(state.ticket.status, "manual_review")

    def test_confirmation_digest_is_rechecked_before_execution(self):
        self.use_model(text="报表数据库只读就够了")
        state = self.create("办公需求", "报表数据库只读就够了")
        waiting = self.service.clarify(self.alice, state.ticket.id, "确认执行", 1)
        raw = waiting.to_dict()
        raw["confirmation"]["plan_digest"] = "stale"
        self.audit.save_state(raw)
        done = self.service.approve(self.manager, waiting.approval.id, True)
        self.assertEqual(done.ticket.status, "manual_review")
        self.assertIsNone(self.audit.get_operation(self.tools.action_key(done.ticket, "grant_access")))

    def test_deepseek_missing_interpretation_fails_closed(self):
        model = DeepSeekDecisionModel("fake", max_retries=0)
        response = {"choices": [{"message": {"content": '{"need_clarify":false,"missing_fields":[]}'}}]}
        with patch.object(model, "_post_json", return_value=response), self.assertRaises(ModelOutputError):
            model.clarify(self.create("系统打不开", "请帮忙").ticket)

    def test_completed_incident_is_not_claimed_as_resolved(self):
        state = self.create("ERP 故障", "ERP 报错 503")
        self.assertEqual(state.resolution["type"], "agent_task_completed")
        self.assertIsNone(state.resolution["external_issue_resolved"])
        self.assertTrue(state.resolution["simulated"])

    def test_confirmation_http_rejects_spoofed_plan_and_other_user(self):
        identities, keys = demo_identities(self.root)
        with TestClient(create_app(self.agent, identities=identities)) as client:
            state = client.post("/tickets", headers=auth_headers(keys), json={"title": "密码过期", "description": "本人账号密码过期"}).json()
            url = f'/tickets/{state["ticket"]["id"]}/clarify'
            answer = {"answer": "确认执行", "round_number": 1}
            self.assertEqual(client.post(url, headers=auth_headers(keys, "bob"), json=answer).status_code, 404)
            self.assertEqual(client.post(url, headers=auth_headers(keys), json={**answer, "plan_digest": "fake"}).status_code, 422)
            done = client.post(url, headers=auth_headers(keys), json=answer)
            self.assertEqual(done.status_code, 200)
            self.assertEqual(done.json()["ticket"]["status"], "closed")
            self.assertFalse(done.json()["clarification"]["need_clarify"])

    def test_confirmation_crash_recovers_without_asking_or_executing_twice(self):
        state = self.create("密码过期", "本人账号密码过期")
        with patch.object(self.agent, "_continue", side_effect=SystemExit("crash")), self.assertRaises(SystemExit):
            self.service.clarify(self.alice, state.ticket.id, "确认执行", 1)
        with patch.object(self.tools, "_execute_action", wraps=self.tools._execute_action) as execute:
            done = self.service.recover(self.manager, state.ticket.id)
            replay = self.service.clarify(self.alice, state.ticket.id, "确认执行", 1)
        self.assertEqual(done.ticket.status, "closed")
        self.assertEqual(replay.ticket.id, done.ticket.id)
        self.assertEqual(execute.call_count, 1)

    def test_configured_evaluation_cannot_silently_run_rules(self):
        with patch("app.evaluation.get_settings", return_value=self.cfg), self.assertRaises(ConfigurationError):
            evaluate(mode="configured")

    def test_regression_evaluation_uses_real_sop_documents(self):
        report = evaluate(Path(__file__).resolve().parents[1] / "evals" / "semantic_regression.json")
        self.assertEqual(report["total"], report["passed"])
        self.assertEqual(report["knowledge_documents"], len(list((Path(__file__).resolve().parents[1] / "data" / "knowledge").rglob("*.md"))))
        self.assertGreater(report["cases_with_retrieval"], 0)
        self.assertEqual(report["unverified_outcomes"], 0)
        case = next(x for x in report["cases"] if x["id"] == "normal_incident")
        self.assertIn("incident_response.md", case["retrieved_sources"])


if __name__ == "__main__":
    unittest.main()
