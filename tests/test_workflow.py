"""检查工单处理、审批恢复、知识传递和模型故障的业务行为。

运行：在项目根目录执行 `py -m unittest discover -s tests -v`。
discover 查找测试，-s tests 指定目录，-v 显示每条用例的名字。
这些测试使用离线或假模型，没有覆盖全部输入错误、权限或并发场景。
"""

# tempfile 创建临时目录；unittest 是 Python 标准库测试框架。
import tempfile
import unittest
from pathlib import Path

from app.audit import AuditStore
from app.knowledge import KnowledgeBase
from app.models import Ticket
from app.model_adapter import Decision, ResilientDecisionModel
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent


class WorkflowTests(unittest.TestCase):
    """继承 TestCase 后，可以使用 assertEqual 等断言方法。"""

    def setUp(self):
        """每条 test_ 方法执行前自动调用，创建一套独立的临时测试数据。"""
        # 独立临时目录使测试不读写正常使用的 data/agent.db。
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        kb = root / "kb"
        kb.mkdir()
        # 准备最小文档即可，不依赖用户机器上的真实知识库内容。
        (kb / "access.md").write_text("生产 access 权限必须审批", encoding="utf-8")
        audit = AuditStore(root / "test.db")
        self.agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(kb, backend="lexical"), audit), audit)

    def tearDown(self):
        """每条测试结束后自动清理这条用例创建的临时目录。"""
        self.tmp.cleanup()

    def test_low_risk_ticket_is_closed(self):
        """给出登录问题，检查流程最终关闭，且选中密码重置动作。"""
        # Arrange/Act：准备输入，并调用待测试的真实主流程。
        state = self.agent.run(Ticket("密码重置", "无法 login", "a@example.com", "IT"))
        # Assert：实际值与期望值不同就失败，指出哪个行为发生变化。
        self.assertEqual(state.ticket.status, "closed")
        self.assertEqual(state.ticket.recommended_action, "reset_password")

    def test_access_request_waits_for_approval_then_closes(self):
        """给出权限申请，先检查暂停，再同意审批并检查恢复后的状态。"""
        state = self.agent.run(Ticket("申请报表库权限", "需要报表数据库只读权限", "b@example.com", "Data"))
        self.assertEqual(state.ticket.status, "awaiting_approval")
        # setUp 每次给出新数据库，所以审批编号属于当前用例，不与另一用例混用。
        done = self.agent.approve(state.approval.id, "manager", True)
        self.assertEqual(done.ticket.status, "closed")
        self.assertEqual(done.action_result.status, "succeeded")
        self.assertEqual(done.action_result.action, "grant_access")

    def test_injected_model_receives_retrieved_context(self):
        """工作流应把本地 SOP 传给注入模型，并保存模型来源。"""

        class RecordingModel:
            def __init__(self):
                self.context = None

            def decide(self, title, description, severity, context):
                self.context = list(context)
                return Decision(
                    "general",
                    "manual_review",
                    0.77,
                    f"模型处理：{title}",
                    source="fake_model",
                )

        model = RecordingModel()
        self.agent = EnterpriseAgent(
            self.agent.tools,
            self.agent.audit,
            model=model,
        )
        state = self.agent.run(Ticket("申请报表库权限", "需要报表数据库只读 access 权限", "c@example.com", "IT"))
        self.assertEqual(state.decision["source"], "fake_model")
        self.assertTrue(model.context)
        self.assertIn("access", model.context[0]["snippet"].lower())

    def test_fallback_reason_is_returned_and_audited(self):
        """包装器吞掉网络异常后，状态和审计仍应明确显示回退原因。"""

        class BrokenModel:
            def decide(self, *_args, **_kwargs):
                # 模拟故障即可，无需真的断网，也不会消耗 API 额度。
                raise RuntimeError("模拟模型服务不可用")

        self.agent.model = ResilientDecisionModel(BrokenModel())
        state = self.agent.run(Ticket("密码重置", "无法登录", "a@example.com", "IT"))
        self.assertEqual(state.decision["source"], "rule_fallback")
        self.assertEqual(state.llm_error, "RuntimeError")
        # 检查落盘记录，避免只有界面显示回退、数据库里却查不到原因。
        events = self.agent.audit.list_events(state.ticket.id)
        plan = next(event for event in events if event["node"] == "plan")
        self.assertEqual(plan["payload"]["llm_error"], "RuntimeError")

    def test_strict_failure_stops_before_execution(self):
        """严格模式失败应停在 plan，不能继续审批或执行工具。"""

        class BrokenModel:
            def decide(self, *_args, **_kwargs):
                raise RuntimeError("模拟模型服务不可用")

        self.agent.model = ResilientDecisionModel(BrokenModel(), strict=True)
        state = self.agent.run(Ticket("密码重置", "无法登录", "a@example.com", "IT"))
        self.assertEqual(state.ticket.status, "failed")
        self.assertEqual(state.result, "")
        self.assertIsNone(state.decision)
        self.assertEqual([item["node"] for item in state.trace], ["intake", "clarify", "retrieve", "plan"])


# 框架发现测试时也能执行；此入口同时支持直接以脚本方式调用测试主函数。
if __name__ == "__main__":
    unittest.main()
