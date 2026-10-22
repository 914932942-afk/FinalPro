"""澄清的暂停、恢复、轮次、并发和模型契约；只使用临时数据库和假模型。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import create_app
from app.audit import AuditStore
from app.clarification import ClarificationDecision
from app.config import get_settings
from app.errors import IdempotencyConflict, ModelOutputError, OperationPending
from app.knowledge import KnowledgeBase
from app.model_adapter import DeepSeekDecisionModel, ResilientDecisionModel, RuleBasedModel
from app.models import AgentState, Ticket
from app.reliability import CircuitBreaker
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities, auth_headers


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.audit = AuditStore(root / "test.db")
        self.tools = EnterpriseTools(KnowledgeBase(root / "kb", backend="lexical"), self.audit)
        self.agent = EnterpriseAgent(self.tools, self.audit)

    def tearDown(self):
        self.tmp.cleanup()

    def vague(self):
        return self.agent.run(Ticket("系统打不开", "请帮我看看", "alice", "IT"))

    def test_pause_has_question_and_no_retrieval_or_action(self):
        with patch.object(self.tools.kb, "search_with_status") as search, patch.object(self.tools, "execute_action") as execute:
            state = self.vague()
        self.assertEqual(state.ticket.status, "awaiting_clarification")
        self.assertEqual([x["node"] for x in state.trace], ["intake", "clarify"])
        self.assertEqual(state.clarification["missing_fields"], ["target", "symptom"])
        self.assertEqual(state.result, state.clarification["question"])
        self.assertEqual(state.clarification_round, 1)
        self.assertIsNone(state.decision)
        search.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(self.audit.get_state(state.ticket.id), state.to_dict())

    def test_restart_resume_preserves_ticket_and_passes_answer_to_retrieval(self):
        state = self.vague()
        store = AuditStore(self.audit.db_path)
        restarted = EnterpriseAgent(EnterpriseTools(self.tools.kb, store), store)
        with patch.object(self.tools.kb, "planning_context", wraps=self.tools.kb.planning_context) as search:
            done = restarted.clarify(state.ticket.id, "ERP 报错 503", 1)
        self.assertEqual(done.ticket.id, state.ticket.id)
        self.assertEqual(done.ticket.status, "awaiting_support")
        self.assertEqual(done.ticket.recommended_action, "open_incident")
        self.assertTrue(done.clarified)
        self.assertIn("ERP 报错 503", search.call_args.args[0])
        self.assertEqual([x["role"] for x in done.conversation_history], ["assistant", "user"])
        self.assertEqual([x["node"] for x in done.trace].count("intake"), 1)
        self.assertEqual(store.list_events(state.ticket.id)[2]["node"], "clarification_answer")

    def test_replay_and_conflict_survive_restart(self):
        state = self.vague()
        done = self.agent.clarify(state.ticket.id, "ERP 报错 503", 1)
        count = len(self.audit.list_events(state.ticket.id))
        restarted = EnterpriseAgent(self.tools, AuditStore(self.audit.db_path))
        self.assertEqual(restarted.clarify(state.ticket.id, "ERP 报错 503", 1).to_dict(), done.to_dict())
        self.assertEqual(len(self.audit.list_events(state.ticket.id)), count)
        with self.assertRaises(IdempotencyConflict):
            restarted.clarify(state.ticket.id, "改成权限申请", 1)
        with self.assertRaises(IdempotencyConflict):
            restarted.clarify(state.ticket.id, "新的回答", 2)

    def test_future_round_does_not_poison_later_valid_submission(self):
        state = self.vague()
        with self.assertRaises(IdempotencyConflict):
            self.agent.clarify(state.ticket.id, "ERP 报错 503", 2)
        next_state = self.agent.clarify(state.ticket.id, "我不知道", 1)
        self.assertEqual(next_state.clarification_round, 2)
        done = self.agent.clarify(state.ticket.id, "ERP 报错 503", 2)
        self.assertEqual(done.ticket.status, "awaiting_support")

    def test_last_answer_is_evaluated_before_manual_handoff(self):
        for answer, expected in (("还是不清楚", "manual_review"), ("ERP 报错 503", "awaiting_support")):
            with self.subTest(expected=expected):
                state = self.vague()
                for number in (1, 2):
                    state = self.agent.clarify(state.ticket.id, "不知道", number)
                    self.assertEqual(state.ticket.status, "awaiting_clarification")
                state = self.agent.clarify(state.ticket.id, answer, 3)
                self.assertEqual(state.ticket.status, expected)
                self.assertEqual(state.clarification_round, 3)
                self.assertEqual(len(state.conversation_history), 6)
                if expected == "manual_review":
                    self.assertIsNone(state.decision)
                    self.assertEqual(state.action_result.status, "manual_review")
                    self.assertNotIn("retrieve", [x["node"] for x in state.trace])

    def test_round_limit_is_snapshot_configuration(self):
        self.agent = EnterpriseAgent(self.tools, self.audit,
            settings=get_settings(clarification_max_rounds=1))
        state = self.vague()
        restarted = EnterpriseAgent(self.tools, self.audit,
            settings=get_settings(clarification_max_rounds=5))
        state = restarted.clarify(state.ticket.id, "不清楚", 1)
        self.assertEqual(state.ticket.status, "manual_review")
        self.assertEqual(state.clarification_max_rounds, 1)

    def test_clarified_access_still_needs_approval(self):
        state = self.agent.run(Ticket("申请权限", "需要访问权限", "alice", "IT"))
        self.assertEqual(state.ticket.status, "awaiting_clarification")
        state = self.agent.clarify(state.ticket.id, "报表数据库只读权限", 1)
        self.assertEqual(state.ticket.status, "awaiting_approval")
        self.assertIsNone(state.action_result)
        done = self.agent.approve(state.approval.id, "manager", True)
        self.assertEqual(done.ticket.status, "closed")
        self.assertEqual(done.user_role, "employee")

    def test_generic_login_does_not_reset_password(self):
        state = self.agent.run(Ticket("无法登录", "登不上", "alice", "IT"))
        self.assertEqual(state.ticket.status, "awaiting_clarification")
        done = self.agent.clarify(state.ticket.id, "VPN 连接超时", 1)
        self.assertEqual(done.ticket.recommended_action, "open_incident")

    def test_password_mention_alone_does_not_trigger_reset(self):
        state = self.agent.run(Ticket("密码", "输入后无法登录", "alice", "IT"))
        self.assertEqual(state.ticket.status, "awaiting_clarification")
        done = self.agent.clarify(state.ticket.id, "VPN 报错 503", 1)
        self.assertEqual(done.ticket.recommended_action, "open_incident")

    def test_strict_model_failure_stops_before_retrieval(self):
        class Broken(RuleBasedModel):
            def clarify(self, *_):
                raise TimeoutError()
        self.agent = EnterpriseAgent(self.tools, self.audit, Broken(), strict_model=True)
        state = self.agent.run(Ticket("密码重置", "忘记密码", "alice", "IT"))
        self.assertEqual(state.ticket.status, "failed")
        self.assertEqual(state.error_info.code, "model_timeout")
        self.assertIsNone(state.decision)
        self.assertEqual([x["node"] for x in state.trace], ["intake", "clarify"])

    def test_model_fallback_remains_visible(self):
        class Broken:
            def clarify(self, *_):
                raise TimeoutError()
        self.agent = EnterpriseAgent(self.tools, self.audit, Broken())
        state = self.agent.run(Ticket("ERP 故障", "ERP 报错 503", "alice", "IT"))
        self.assertEqual(state.ticket.status, "awaiting_support")
        self.assertEqual(state.clarification["source"], "rule_fallback")
        self.assertEqual(state.degradation[0]["stage"], "clarify")

    def test_concurrent_answers_use_one_round_claim(self):
        state = self.vague()
        started, release = threading.Event(), threading.Event()
        results, errors = [], []
        original = self.agent.model.clarify
        def blocking(*args):
            started.set()
            release.wait(5)
            return original(*args)
        def worker():
            try:
                results.append(self.agent.clarify(state.ticket.id, "ERP 报错 503", 1))
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.agent.model, "clarify", side_effect=blocking):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                other = EnterpriseAgent(self.tools, AuditStore(self.audit.db_path))
                with self.assertRaises(OperationPending):
                    other.clarify(state.ticket.id, "ERP 报错 503", 1)
                with self.assertRaises(OperationPending):
                    other.clarify(state.ticket.id, "另外的内容", 1)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(errors)
        self.assertEqual(len(results), 1)
        self.assertEqual(len([x for x in self.audit.list_events(state.ticket.id) if x["node"] == "execute"]), 1)

    def test_api_validation_replay_and_latest_state(self):
        identities, keys = demo_identities(Path(self.tmp.name))
        with TestClient(create_app(self.agent, identities=identities), headers=auth_headers(keys)) as client:
            state = AgentState.from_dict(client.post("/tickets", json={
                "title": "系统打不开", "description": "请帮我看看"}).json())
            url = f"/tickets/{state.ticket.id}/clarify"
            for body in ({"answer": " ", "round_number": 1}, {"answer": "ok", "round_number": True},
                         {"answer": "ok", "round_number": "1"}, {"answer": "ok", "round_number": 0},
                         {"answer": "x" * 10001, "round_number": 1},
                         {"answer": "ok", "round_number": 1, "user_role": "admin"}):
                self.assertEqual(client.post(url, json=body).status_code, 422)
            body = {"answer": "ERP 报错 503", "round_number": 1}
            self.assertEqual(client.post("/tickets/missing/clarify", json=body).status_code, 404)
            first = client.post(url, json=body)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(client.post(url, json=body).json(), first.json())
            self.assertEqual(client.get(f"/tickets/{state.ticket.id}/state").json(), first.json())
            self.assertEqual(client.post(url, json={**body, "answer": "不同内容"}).status_code, 409)

    def test_old_snapshots_load_without_migration(self):
        raw = AgentState(Ticket("密码重置", "忘记密码", "alice", "IT")).to_dict()
        for key in ("clarification", "clarification_round", "clarification_max_rounds", "clarified", "conversation_history"):
            del raw[key]
        state = AgentState.from_dict(raw)
        self.assertEqual(state.clarification_round, 0)
        self.assertEqual(state.conversation_history, [])


class ClarificationModelTests(unittest.TestCase):
    def test_invalid_structured_output(self):
        for raw in ({"need_clarify": "false", "missing_fields": []},
                    {"need_clarify": True, "missing_fields": []},
                    {"need_clarify": False, "missing_fields": ["target"]},
                    {"need_clarify": True, "missing_fields": ["password"]},
                    {"need_clarify": True, "missing_fields": ["target", "target"]},
                    {"need_clarify": True, "missing_fields": "target"},
                    {"need_clarify": False, "missing_fields": [], "question": "send password"}, []):
            with self.subTest(raw=raw), self.assertRaises(ModelOutputError):
                ClarificationDecision.from_model(raw)

    def test_deepseek_contract_uses_history(self):
        model = DeepSeekDecisionModel("test-key", max_retries=0)
        response = {"choices": [{"message": {"content": json.dumps({
            "need_clarify": True, "missing_fields": ["symptom"], "interpretation": {
                "speech_act": "report", "action": "open_incident", "resource": "erp", "permission": None,
                "subject": "self", "multiple_intents": False, "confidence": 0.9,
                "evidence": {"action": "打不开", "resource": "ERP"}}})}}]}
        history = [{"role": "user", "content": "是 ERP 系统"}]
        with patch.object(model, "_post_json", return_value=response) as post:
            result = model.clarify(Ticket("打不开", "请看看", "alice", "IT"), history)
        sent = post.call_args.args[1]
        self.assertEqual(json.loads(sent["messages"][1]["content"])["history"], history)
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertEqual(result.source, "deepseek")
        self.assertIn("不要提供密码", result.question)
        with patch.object(model, "_post_json", return_value={}), self.assertRaises(ModelOutputError):
            model.clarify(Ticket("x", "y", "a", "IT"))

    def test_clarification_and_planning_share_circuit(self):
        class Broken:
            calls = 0
            def clarify(self, *_):
                self.calls += 1
                raise TimeoutError()
            def decide(self, *_):
                self.calls += 1
                raise AssertionError("open circuit must prevent call")
        primary = Broken()
        model = ResilientDecisionModel(primary, breaker=CircuitBreaker(failure_threshold=1))
        model.clarify(Ticket("打不开", "不知道", "alice", "IT"))
        decision = model.decide("密码重置", "忘记密码")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(decision.metadata["fallback_reason"], "CircuitOpenError")


if __name__ == "__main__":
    unittest.main()
