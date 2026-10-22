"""规划事实分流、引用约束、重复追问和最终计划确认。不调用网络。"""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from app.audit import AuditStore
from app.auth import Principal
from app.clarification import ClarificationDecision
from app.config import get_settings
from app.knowledge import KnowledgeBase
from app.planning import PlanningChoice, PlanningDecision
from app.service import TicketService
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent


class PlannerModel:
    supports_planning = True

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    def clarify(self, ticket, _history):
        # 测试刻意始终给 incident 候选，最终计划可根据新事实改成申请。
        return ClarificationDecision(False, interpretation={
            "speech_act": "report", "action": "open_incident", "resource": "erp", "permission": None,
            "subject": "self", "multiple_intents": False, "confidence": 0.95,
            "evidence": {"action": "ERP", "resource": "ERP"}}, source="stub")

    def plan(self, ticket, understanding, context):
        self.calls += 1
        return self.behavior(ticket, understanding, context)


def choice(step, *, action=None, permission=None, missing=None, refs=(), evidence=()):
    return PlanningDecision(PlanningChoice(next_step=step, action=action,
        resource="erp" if action else None, permission=permission, missing_fact=missing,
        user_evidence=list(evidence), sop_refs=list(refs), reason="test route", confidence=0.95), "stub")


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(Path(__file__).resolve().parents[1] / "data" / "knowledge", self.root / "kb")
        self.cfg = get_settings(_env_file=None, llm_mode="offline", data_dir=self.root, rag_backend="lexical")
        self.audit = AuditStore(self.root / "test.db")
        self.tools = EnterpriseTools(KnowledgeBase(self.root / "kb", settings=self.cfg), self.audit)
        self.owner = Principal(user_id="alice", role="employee", department="IT")
        self.manager = Principal(user_id="manager", role="manager", department="IT")

    def tearDown(self):
        self.tmp.cleanup()

    def setup_model(self, behavior, **cfg):
        self.model = PlannerModel(behavior)
        self.agent = EnterpriseAgent(self.tools, self.audit, self.model, settings=self.cfg.model_copy(update=cfg))
        self.service = TicketService(self.agent)

    def create(self, description="提示没有权限，帮我处理一下"):
        return self.service.create(self.owner, title="ERP 访问问题", description=description)

    def test_candidate_is_not_final_action_and_confirmation_comes_after_plan(self):
        def planner(ticket, understanding, docs):
            if "第一次" not in understanding.text:
                return choice("clarify", missing="previous_access")
            return choice("propose_action", action="grant_access", permission="use",
                          refs=[docs[0]["id"]], evidence=["第一次使用 ERP，需要普通使用权限"])
        self.setup_model(planner)
        first = self.create()
        self.assertEqual(first.planning["next_step"], "clarify")
        self.assertIsNone(first.confirmation)
        second = self.service.clarify(self.owner, first.ticket.id, "第一次使用 ERP，需要普通使用权限", 1)
        self.assertEqual(second.understanding["action"], "open_incident")
        self.assertEqual(second.confirmation["plan"]["action"], "grant_access")
        self.assertIsNone(second.approval)
        waiting = self.service.clarify(self.owner, first.ticket.id, "确认执行", 2)
        self.assertEqual(waiting.ticket.status, "awaiting_approval")
        self.assertTrue(waiting.clarified)
        self.assertEqual(self.model.calls, 2)
        done = self.service.approve(self.manager, waiting.approval.id, True)
        self.assertEqual(done.ticket.status, "closed")
        self.assertEqual(self.model.calls, 2)

    def test_existing_access_is_routed_to_incident_without_grant(self):
        def planner(ticket, understanding, docs):
            if "昨天还能用" not in understanding.text:
                return choice("clarify", missing="previous_access")
            return choice("propose_action", action="open_incident", refs=[docs[0]["id"]], evidence=["昨天还能用"])
        self.setup_model(planner)
        first = self.create()
        done = self.service.clarify(self.owner, first.ticket.id, "昨天还能用，今天报错 403", 1)
        self.assertEqual(done.ticket.recommended_action, "open_incident")
        self.assertEqual(done.ticket.status, "awaiting_support")
        self.assertIsNone(done.approval)

    def test_unretrieved_sop_reference_cannot_authorize_execution(self):
        self.setup_model(lambda *_: choice("propose_action", action="open_incident", refs=["invented"], evidence=["ERP"]))
        with patch.object(self.tools, "execute_action") as execute:
            result = self.create("昨天还能用，今天报错 503")
        self.assertEqual(result.ticket.status, "manual_review")
        execute.assert_not_called()

    def test_grant_candidate_can_become_incident_without_owner_confirmation(self):
        self.setup_model(lambda t,u,d: choice("propose_action", action="open_incident",
                          refs=[d[0]["id"]], evidence=["ERP"]))
        original = self.model.clarify
        def grant_candidate(ticket, history):
            result = original(ticket, history)
            result.interpretation["action"] = "grant_access"
            return result
        self.model.clarify = grant_candidate
        result = self.create("昨天还能用，今天报错 403")
        self.assertTrue(result.understanding["requires_confirmation"])
        self.assertEqual(result.ticket.recommended_action, "open_incident")
        self.assertEqual(result.ticket.status, "awaiting_support")
        self.assertIsNone(result.confirmation)

    def test_clarification_can_carry_known_resource_without_executing(self):
        decision = choice("clarify", missing="previous_access")
        decision.choice.resource = "erp"
        self.setup_model(lambda *_: decision)
        with patch.object(self.tools, "execute_action") as execute:
            result = self.create()
        self.assertEqual(result.ticket.status, "awaiting_clarification")
        self.assertIsNone(result.ticket.execution_plan)
        execute.assert_not_called()

    def test_last_answer_cannot_skip_final_plan_confirmation(self):
        def planner(t,u,d):
            if "第一次" not in u.text:
                return choice("clarify", missing="previous_access")
            return choice("propose_action", action="grant_access", permission="use",
                          refs=[d[0]["id"]], evidence=["ERP"])
        self.setup_model(planner, clarification_max_rounds=1)
        first = self.create()
        result = self.service.clarify(self.owner, first.ticket.id, "第一次用 ERP，需要普通使用权限", 1)
        self.assertEqual(result.ticket.status, "manual_review")
        self.assertIsNone(result.confirmation)
        self.assertIsNone(result.approval)
        self.assertIsNone(result.action_result.downstream_id)

    def test_sop_required_but_missing_blocks_action(self):
        self.setup_model(lambda *_: choice("propose_action", action="open_incident", evidence=["ERP"]))
        self.assertEqual(self.create("报错 503").ticket.status, "manual_review")

    def test_no_sop_variant_still_enforces_confirmation_and_approval(self):
        self.setup_model(lambda *_: choice("propose_action", action="grant_access", permission="use", evidence=["ERP"]), planning_use_sop=False)
        first = self.create("申请 ERP 普通使用权限")
        self.assertFalse(first.retrieved_docs)
        self.assertEqual(first.confirmation["status"], "pending")
        second = self.service.clarify(self.owner, first.ticket.id, "确认执行", 1)
        self.assertEqual(second.ticket.status, "awaiting_approval")

    def test_known_fact_is_not_repeatedly_asked(self):
        self.setup_model(lambda *_: choice("clarify", missing="previous_access"))
        self.assertEqual(self.create("昨天还能用，今天报错 403").ticket.status, "manual_review")

    def test_low_confidence_question_pauses_then_resumes_without_tool_call(self):
        def planner(t,u,d):
            if "昨天还能用" not in u.text:
                result = choice("clarify", missing="previous_access")
                result.choice.confidence = .1
                return result
            return choice("propose_action", action="open_incident", refs=[next(x["id"] for x in d if x["source"] == "incident_response.md")], evidence=["ERP"])
        self.setup_model(planner)
        with patch.object(self.tools, "execute_action") as execute:
            first = self.create()
        self.assertEqual(first.ticket.status, "awaiting_clarification")
        self.assertEqual(first.clarification["missing_fields"], ["previous_access"])
        self.assertIsNone(first.ticket.execution_plan)
        execute.assert_not_called()
        # 新建编排器模拟重启，恢复同一工单和轮次。
        self.setup_model(planner)
        done = self.service.clarify(self.owner, first.ticket.id, "昨天还能用，今天报错 403", 1)
        self.assertEqual(done.ticket.status, "awaiting_support")
        self.assertEqual(done.ticket.id, first.ticket.id)

    def test_low_confidence_does_not_bypass_question_guards(self):
        decision = choice("clarify", missing="previous_access")
        decision.choice.confidence = .1
        self.setup_model(lambda *_: decision)
        self.assertEqual(self.create("昨天还能用，今天报错 403").ticket.status, "manual_review")
        first = self.create()
        second = self.service.clarify(self.owner, first.ticket.id, "不知道", 1)
        done = self.service.clarify(self.owner, first.ticket.id, "仍然不知道", 2)
        self.assertEqual(second.ticket.status, "awaiting_clarification")
        self.assertEqual(done.ticket.status, "manual_review")
        self.assertIsNone(done.ticket.execution_plan)

    def test_low_confidence_action_never_enters_confirmation_or_execution(self):
        for action, permission in (("open_incident", None), ("grant_access", "use"), ("reset_password", None)):
            def planner(t,u,d):
                result = choice("propose_action", action=action, permission=permission, evidence=["ERP"])
                if action == "reset_password":
                    result.choice.resource = "self_account"
                result.choice.confidence = .79
                return result
            self.setup_model(planner, planning_use_sop=False)
            with self.subTest(action=action), patch.object(self.tools, "execute_action") as execute:
                result = self.create("报错 503，请处理")
                self.assertEqual(result.ticket.status, "manual_review")
                self.assertIsNone(result.confirmation)
                self.assertIsNone(result.approval)
                self.assertIsNone(result.ticket.execution_plan)
                execute.assert_not_called()

    def test_unproductive_questions_are_bounded(self):
        self.setup_model(lambda *_: choice("clarify", missing="previous_access"))
        first = self.create()
        second = self.service.clarify(self.owner, first.ticket.id, "不知道", 1)
        third = self.service.clarify(self.owner, first.ticket.id, "不知道", 2)
        self.assertEqual(third.ticket.status, "manual_review")
        self.assertIsNone(third.action_result.downstream_id)

    def test_recovery_reuses_confirmed_final_plan_without_replanning(self):
        self.setup_model(lambda t,u,d: choice("propose_action", action="grant_access", permission="use", refs=[d[0]["id"]], evidence=["ERP"]))
        first = self.create("申请 ERP 普通使用权限")
        with patch.object(self.agent, "_continue", side_effect=SystemExit()), self.assertRaises(SystemExit):
            self.service.clarify(self.owner, first.ticket.id, "确认执行", 1)
        recovered = self.service.recover(self.manager, first.ticket.id)
        self.assertEqual(recovered.ticket.status, "awaiting_approval")
        self.assertEqual(self.model.calls, 1)

    def test_user_correction_invalidates_final_plan_and_confirmation(self):
        def planner(t,u,d):
            action = "open_incident" if "503" in u.text else "grant_access"
            return choice("propose_action", action=action, permission="use" if action == "grant_access" else None,
                          refs=[d[0]["id"]], evidence=["ERP"])
        self.setup_model(planner)
        first = self.create("申请 ERP 普通使用权限")
        done = self.service.clarify(self.owner, first.ticket.id, "改成：ERP 报错 503", 1)
        self.assertEqual(done.ticket.recommended_action, "open_incident")
        self.assertIsNone(done.confirmation)

    def test_model_failure_in_planning_does_not_fall_back_to_account_action(self):
        def broken(*_):
            raise TimeoutError()
        self.setup_model(broken)
        result = self.create("申请 ERP 普通使用权限")
        self.assertEqual(result.ticket.status, "manual_review")
        self.assertEqual(result.planning["source"], "rule_fallback")
        self.assertIsNone(result.confirmation)

    def test_validated_self_account_does_not_trigger_unnecessary_target_question(self):
        def planner(t,u,d):
            result = choice("propose_action", action="reset_password", refs=[next(doc["id"] for doc in d if doc["source"] == "password_reset.md")], evidence=["密码"])
            result.choice.resource = "self_account"
            return result
        self.setup_model(planner)
        self.model.clarify = lambda *_: ClarificationDecision(True, ["target"], source="stub", interpretation={
            "speech_act": "request", "action": "reset_password", "resource": None, "permission": None,
            "subject": "self", "multiple_intents": False, "confidence": .95,
            "evidence": {"action": "请重置我的密码。"}})
        state = self.service.create(self.owner, title="密码重置", description="请重置我的密码。")
        self.assertEqual(state.clarification["missing_fields"], ["confirmation"])
        self.assertEqual(state.confirmation["plan"]["resource"], "self_account")
        done = self.service.clarify(self.owner, state.ticket.id, "确认执行", 1)
        self.assertEqual(done.ticket.status, "closed")


if __name__ == "__main__":
    unittest.main()
