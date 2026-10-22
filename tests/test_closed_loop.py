"""Agent 语义边界、计划审批、人工出口、原子性与崩溃恢复。"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import create_app
from app.audit import AuditStore
from app.auth import Principal
from app.clarification import ClarificationDecision
from app.config import get_settings
from app.errors import IdempotencyConflict, OperationPending, StorageError
from app.evaluation import evaluate
from app.knowledge import KnowledgeBase
from app.model_adapter import Decision, RuleBasedModel
from app.models import AgentState
from app.service import TicketService
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities, auth_headers


class ClosedLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audit = AuditStore(self.root / "test.db")
        self.settings = get_settings(_env_file=None, data_dir=self.root, llm_mode="offline", rag_backend="lexical")
        self.tools = EnterpriseTools(KnowledgeBase(self.root / "kb", backend="lexical"), self.audit)
        self.agent = EnterpriseAgent(self.tools, self.audit, settings=self.settings)
        self.service = TicketService(self.agent)
        self.alice = Principal(user_id="alice", role="employee", department="IT")
        self.manager = Principal(user_id="manager", role="manager", department="IT")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, title="申请报表库权限", description="需要报表数据库只读权限", **kwargs):
        return self.service.create(self.alice, title=title, description=description, **kwargs)

    def test_fixed_behavior_evaluation(self):
        report = evaluate()
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["incorrect_auto_executions"], 0)
        self.assertEqual(report["passed"], report["total"],
                         [case for case in report["cases"] if not case["passed"]])

    def test_crash_after_claim_preserves_answer_and_approval(self):
        state = self.create("系统打不开", "请协助")
        with patch.object(self.agent, "_continue", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.service.clarify(self.alice, state.ticket.id, "更正：申请报表数据库只读权限", 1)
        saved = self.audit.get_state(state.ticket.id)
        self.assertEqual(saved["next_node"], "clarify")
        self.assertEqual(saved["conversation_history"][-1]["role"], "user")
        waiting = self.service.recover(self.manager, state.ticket.id)
        self.assertEqual(waiting.ticket.status, "awaiting_approval")
        with patch.object(self.agent, "_continue", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.service.approve(self.manager, waiting.approval.id, True)
        saved = self.audit.get_state(state.ticket.id)
        self.assertEqual(saved["next_node"], "execute")
        self.assertEqual(saved["approval"]["status"], "approved")
        done = self.service.recover(self.manager, state.ticket.id)
        self.assertEqual(done.ticket.status, "closed")
        self.assertFalse(self.audit.pending_operations(state.ticket.id))

    def test_legacy_approval_without_plan_is_rejected_before_claim(self):
        state = self.create()
        raw = state.to_dict()
        raw["ticket"].pop("execution_plan")
        self.audit.save_state(raw)
        with self.assertRaises(IdempotencyConflict):
            self.service.approve(self.manager, state.approval.id, True)
        self.assertIsNone(self.audit.get_operation("approval:" + state.approval.id))

    def test_cli_list_cancel_and_manual_resolution(self):
        _, keys = demo_identities(self.root)
        env = {**os.environ, "AGENT_DATA_DIR": str(self.root), "AGENT_DB_PATH": str(self.audit.db_path),
               "AGENT_AUTH_FILE": str(self.root / "auth.json"), "AGENT_KB_DIR": str(self.root / "kb"),
               "AGENT_LLM_MODE": "offline", "AGENT_RAG_BACKEND": "lexical", "DEMO_API_KEY": keys["alice"]}
        def cli(*args):
            result = subprocess.run([sys.executable, "-X", "utf8", "-m", "app.cli", *args],
                cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        waiting = self.create()
        self.assertEqual(cli("list", "--status", "awaiting_approval")["total"], 1)
        self.assertEqual(cli("cancel", waiting.ticket.id, "--reason", "不再需要")["ticket"]["status"], "cancelled")
        manual = self.create("如何重置密码", "咨询流程")
        env["DEMO_API_KEY"] = keys["manager"]
        cli("claim", manual.ticket.id, "--revision", "0")
        done = cli("resolve", manual.ticket.id, "--outcome", "resolved", "--note", "已提供操作说明")
        self.assertEqual(done["resolution"]["by"], "manager")
        self.assertEqual(cli("operations", manual.ticket.id)["pending_operations"], [])

    def test_correction_replaces_current_understanding_without_destroying_original(self):
        state = self.create("不要重置密码", "VPN 连接错误")
        self.assertEqual(state.ticket.status, "awaiting_clarification")
        done = self.service.clarify(self.alice, state.ticket.id, "更正：ERP 报错 503", 1)
        self.assertEqual(done.ticket.execution_plan["resource"], "erp")
        self.assertEqual(done.ticket.description, "VPN 连接错误")
        self.assertNotIn("密码", done.understanding["text"])
        self.assertEqual(done.ticket.recommended_action, "open_incident")

    def test_low_confidence_and_model_action_disagreement_do_not_execute(self):
        for confidence, category, action in ((0.01, "incident", "open_incident"),
                                             (0.99, "access_support", "reset_password")):
            class Model:
                def clarify(self, *_):
                    return ClarificationDecision(False)
                def decide(self, *_):
                    return Decision(category, action, confidence, "test", source="stub")
            self.agent = EnterpriseAgent(self.tools, self.audit, Model(), settings=self.settings)
            self.service = TicketService(self.agent)
            with patch.object(self.tools, "_execute_action") as execute:
                state = self.create("ERP 故障", "ERP 报错 503")
            self.assertEqual(state.ticket.status, "manual_review")
            execute.assert_not_called()

    def test_model_failure_does_not_auto_reset_account(self):
        class Broken:
            def clarify(self, *_):
                raise TimeoutError()
            def decide(self, *_):
                raise TimeoutError()
        agent = EnterpriseAgent(self.tools, self.audit, Broken(), settings=self.settings)
        with patch.object(self.tools, "_execute_action") as execute:
            state = TicketService(agent).create(self.alice, title="密码重置", description="忘记密码")
        self.assertEqual(state.ticket.status, "manual_review")
        execute.assert_not_called()

    def test_approval_binds_complete_plan_and_modified_plan_cannot_execute(self):
        state = self.create()
        self.assertEqual(state.approval.plan, state.ticket.execution_plan)
        self.assertEqual(state.approval.plan["account"], "alice")
        self.assertEqual(state.approval.plan["resource"], "reports_db")
        self.assertEqual(state.approval.plan["permission"], "read_only")
        raw = state.to_dict()
        raw["ticket"]["execution_plan"]["account"] = "bob"
        self.audit.save_checkpoint(raw, "test_change")
        with patch.object(self.tools, "_execute_action") as execute, self.assertRaises(IdempotencyConflict):
            self.service.approve(self.manager, state.approval.id, True)
        execute.assert_not_called()
        self.assertIsNone(self.audit.get_operation("approval:" + state.approval.id))

    def test_changed_user_evidence_is_checked_again_after_approval(self):
        state = self.create()
        raw = state.to_dict()
        raw["ticket"]["description"] = "申请报表数据库管理员权限"
        self.audit.save_checkpoint(raw, "test_change")
        with patch.object(self.tools, "_execute_action") as execute:
            result = self.service.approve(self.manager, state.approval.id, True)
        self.assertEqual(result.ticket.status, "manual_review")
        execute.assert_not_called()

    def test_cancel_then_approve_cannot_execute_and_cancel_is_idempotent(self):
        state = self.create()
        cancelled = self.service.cancel(self.alice, state.ticket.id, "暂时不需要")
        self.assertEqual(cancelled.ticket.status, "cancelled")
        self.assertEqual(cancelled.approval.status, "cancelled")
        self.assertEqual(self.service.cancel(self.alice, state.ticket.id, "暂时不需要").to_dict(), cancelled.to_dict())
        with patch.object(self.tools, "_execute_action") as execute, self.assertRaises(IdempotencyConflict):
            self.service.approve(self.manager, state.approval.id, True)
        execute.assert_not_called()

    def test_manual_resolution_records_actor_without_fake_tool_success(self):
        state = self.create("给 bob 重置密码", "他忘记密码")
        self.assertEqual(state.ticket.status, "manual_review")
        self.service.claim(self.manager, state.ticket.id, 0)
        with patch.object(self.tools, "_execute_action") as execute:
            resolved = self.service.resolve(self.manager, state.ticket.id, "rejected", "请由本人申请")
        self.assertEqual(resolved.ticket.status, "rejected")
        self.assertEqual(resolved.resolution["by"], "manager")
        self.assertEqual(resolved.resolution["outcome"], "rejected")
        self.assertNotEqual(resolved.action_result.status, "succeeded")
        self.assertEqual(self.service.resolve(self.manager, state.ticket.id, "rejected", "请由本人申请").to_dict(), resolved.to_dict())
        execute.assert_not_called()

    def test_unknown_tool_result_requires_evidence_and_never_retries(self):
        with patch.object(self.tools, "_execute_action", side_effect=TimeoutError()) as execute:
            state = self.create("密码重置", "忘记密码")
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(state.action_result.status, "unknown")
        self.service.claim(self.manager, state.ticket.id, 0)
        with self.assertRaises(IdempotencyConflict):
            self.service.feedback(self.manager, state.ticket.id, 1, "accepted", "不能以反馈跳过核查", "test-evidence", True)
        with self.assertRaises(IdempotencyConflict):
            self.service.resolve(self.manager, state.ticket.id, "resolved", "已处理")
        with patch.object(self.tools, "_execute_action") as execute:
            recovered = self.service.recover(self.manager, state.ticket.id)
            self.assertEqual(recovered.ticket.status, "in_progress")
            result = self.service.resolve(self.manager, state.ticket.id, "resolved", "已核实下游成功", "IAM-核查单-001")
        execute.assert_not_called()
        self.assertEqual(result.action_result.status, "unknown")
        self.assertEqual(result.resolution["evidence"], "IAM-核查单-001")
        self.assertEqual(self.audit.get_operation(state.action_result.idempotency_key)["status"], "reconciled")
        self.assertEqual(self.audit.pending_operations(state.ticket.id), [])

    def test_atomic_checkpoint_rolls_back_ticket_state_and_events(self):
        state = self.create("系统打不开", "请看看")
        original = copy.deepcopy(state.to_dict())
        events = self.audit.list_events(state.ticket.id)
        with self.audit._db() as conn:
            conn.execute("CREATE TRIGGER fail_snapshot BEFORE INSERT ON states BEGIN SELECT RAISE(FAIL, 'injected'); END")
        changed = copy.deepcopy(original)
        changed["ticket"]["status"] = "cancelled"
        with self.assertRaises(StorageError):
            self.audit.save_checkpoint(changed, "test_failure")
        self.assertEqual(self.audit.get_ticket(state.ticket.id), original["ticket"])
        self.assertEqual(self.audit.get_state(state.ticket.id), original)
        self.assertEqual(self.audit.list_events(state.ticket.id), events)

    def test_recover_before_any_node_and_reconcile_creation_idempotency(self):
        with patch.object(self.agent, "_continue", side_effect=RuntimeError("crash")), self.assertRaises(RuntimeError):
            self.create("密码重置", "忘记密码", idempotency_key="start-crash")
        ticket_id = self.audit.list_tickets()[0]["id"]
        self.assertEqual(self.audit.get_state(ticket_id)["next_node"], "intake")
        restarted = EnterpriseAgent(self.tools, AuditStore(self.audit.db_path), settings=self.settings)
        result = TicketService(restarted).recover(self.manager, ticket_id)
        self.assertEqual(result.ticket.status, "closed")
        replay = self.create("密码重置", "忘记密码", idempotency_key="start-crash")
        self.assertEqual(replay.ticket.id, ticket_id)
        self.assertEqual(replay.ticket.status, "closed")

    def test_recover_cached_tool_result_without_second_effect(self):
        save = self.audit.save_checkpoint
        def fail_after_tool(state, node, *args, **kwargs):
            if node == "execute":
                raise RuntimeError("snapshot unavailable after tool success")
            return save(state, node, *args, **kwargs)
        with patch.object(self.audit, "save_checkpoint", side_effect=fail_after_tool), self.assertRaises(RuntimeError):
            self.create("密码重置", "忘记密码")
        ticket_id = self.audit.list_tickets()[0]["id"]
        self.assertEqual(self.audit.get_state(ticket_id)["next_node"], "execute")
        with patch.object(self.tools, "_execute_action") as execute:
            recovered = self.service.recover(self.manager, ticket_id)
        self.assertEqual(recovered.ticket.status, "closed")
        execute.assert_not_called()

    def test_crash_during_tool_stays_unknown_and_does_not_reexecute(self):
        with patch.object(self.tools, "_execute_action", side_effect=SystemExit("process death")), self.assertRaises(SystemExit):
            self.create("密码重置", "忘记密码")
        ticket_id = self.audit.list_tickets()[0]["id"]
        with patch.object(self.tools, "_execute_action") as execute:
            result = self.service.recover(self.manager, ticket_id)
        self.assertEqual(result.ticket.status, "manual_review")
        self.assertEqual(result.action_result.status, "unknown")
        execute.assert_not_called()

    def test_strict_model_failure_can_be_recovered(self):
        class Broken:
            def clarify(self, *_):
                raise TimeoutError()
        agent = EnterpriseAgent(self.tools, self.audit, Broken(), strict_model=True, settings=self.settings)
        state = TicketService(agent).create(self.alice, title="密码重置", description="忘记密码")
        self.assertEqual(state.ticket.status, "failed")
        self.assertEqual(state.next_node, "clarify")
        agent.model = RuleBasedModel()
        state = TicketService(agent).recover(self.manager, state.ticket.id)
        self.assertEqual(state.ticket.status, "closed")

    def test_concurrent_cancel_and_recovery_cannot_interrupt_live_approval(self):
        state = self.create()
        started, release = threading.Event(), threading.Event()
        original = self.tools._execute_action
        errors = []
        def block(*args, **kwargs):
            started.set()
            release.wait(5)
            return original(*args, **kwargs)
        def worker():
            try:
                self.service.approve(self.manager, state.approval.id, True)
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.tools, "_execute_action", side_effect=block):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                with self.assertRaises(OperationPending):
                    self.service.cancel(self.alice, state.ticket.id, "取消")
                with self.assertRaises(OperationPending):
                    self.service.recover(self.manager, state.ticket.id)
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(self.audit.get_ticket(state.ticket.id)["status"], "closed")

    def test_process_lock_released_by_operating_system_after_worker_exit(self):
        code = "from app.audit import AuditStore; from pathlib import Path; import sys,time; a=AuditStore(Path(sys.argv[1]));\nwith a.ticket_lock('test-lock'):\n print('locked',flush=True); time.sleep(30)"
        process = subprocess.Popen([sys.executable, "-c", code, str(self.audit.db_path)],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises(OperationPending):
                with self.audit.ticket_lock("test-lock"):
                    self.fail("concurrent lock")
        finally:
            process.terminate()
            process.communicate(timeout=5)
        with self.audit.ticket_lock("test-lock"):
            pass

    def test_new_http_routes_keep_identity_and_object_authorization(self):
        identities, keys = demo_identities(self.root)
        with TestClient(create_app(self.agent, identities=identities)) as client:
            state = self.create("帮 bob 重置密码", "他忘记密码")
            path = f"/tickets/{state.ticket.id}"
            for suffix in ("/resolve", "/cancel", "/recover"):
                self.assertEqual(client.post(path + suffix, json={}).status_code, 401)
            self.assertEqual(client.get("/tickets").status_code, 401)
            self.assertEqual(client.get("/tickets", headers=auth_headers(keys, "bob")).json()["total"], 0)
            self.assertEqual(client.get("/tickets?status=manual_review", headers=auth_headers(keys, "manager")).json()["total"], 1)
            self.assertEqual(client.get(path + "/operations", headers=auth_headers(keys)).status_code, 403)
            self.assertEqual(client.post(path + "/recover", headers=auth_headers(keys)).status_code, 403)
            body = {"outcome": "rejected", "note": "请本人提交"}
            self.assertEqual(client.post(path + "/resolve", json=body, headers=auth_headers(keys)).status_code, 403)
            self.assertEqual(client.post("/portal/tickets/" + state.ticket.id + "/claim", json={"revision": 0}, headers=auth_headers(keys, "manager")).status_code, 200)
            response = client.post(path + "/resolve", json=body, headers=auth_headers(keys, "manager"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["resolution"]["by"], "manager")
            self.assertEqual(client.post(path + "/resolve", json={**body, "by": "admin"},
                                         headers=auth_headers(keys, "manager")).status_code, 422)


if __name__ == "__main__":
    unittest.main()
