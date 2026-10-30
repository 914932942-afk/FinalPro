"""Requester projections, truthful progress labels, and support-scope authorization."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.api import create_app
from app.audit import AuditStore
from app.config import get_settings
from app.knowledge import KnowledgeBase
from app.models import AgentState, Approval, Ticket
from app.portal import present_ticket
from app.portal import support_work_items
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities, auth_headers


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        identities, self.keys = demo_identities(self.root)
        cfg = get_settings(_env_file=None, data_dir=self.root, llm_mode="offline",
                           rag_backend="lexical", runbook_strategy="bm25", diagnosis_mode="off")
        self.audit = AuditStore(cfg.db_path)
        self.agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(self.root / "kb", backend="lexical"), self.audit),
                                     self.audit, settings=cfg)
        self.client = TestClient(create_app(self.agent, identities=identities))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def headers(self, user="alice"):
        return auth_headers(self.keys, user)

    def seed(self, *, owner="alice", department="IT", status="closed", action="open_incident", **extra):
        ticket = Ticket("ERP 一直打不开", "ERP 卡在登录页面", owner, department,
                        owner_id=owner, status=status, recommended_action=action)
        state = AgentState(ticket)
        for name, value in extra.items():
            setattr(state, name, value)
        self.audit.save_checkpoint(state.to_dict(), "test")
        return state.to_dict()

    def get(self, ticket_id, user="alice", scope="mine"):
        return self.client.get(f"/portal/tickets/{ticket_id}", params={"scope": scope}, headers=self.headers(user))

    def assert_public(self, value):
        forbidden = {"trace", "degradation", "diagnosis", "retrieved_docs", "decisions", "action_result",
                     "enrichment_result", "error_info", "llm_error", "tool_calls", "model_calls", "usage", "support"}
        if isinstance(value, dict):
            self.assertFalse(forbidden.intersection(value))
            for child in value.values():
                self.assert_public(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_public(child)

    def test_incomplete_investigation_is_processing_and_does_not_mutate_state(self):
        state = self.seed(diagnosis={"tool_calls": 1, "decisions": [{"reason": "private"}]},
                          degradation=[{"reason": "repeated_tool"}])
        state["ticket"]["diagnostic_report"] = {"assessment": "insufficient_evidence", "evidence_verified": False,
            "summary": "INTERNAL_REPORT", "stop_reason": "repeated_tool"}
        self.audit.save_checkpoint(state, "test")
        before = deepcopy(self.audit.get_state(state["ticket"]["id"]))
        result = self.get(state["ticket"]["id"]).json()
        self.assertEqual(result["status"]["code"], "pending_review")
        self.assertEqual(result["status"]["group"], "processing")
        self.assertIn("尚未确认问题已解决", result["status"]["message"])
        self.assert_public(result)
        self.assertNotIn("INTERNAL_REPORT", str(result))
        self.assertEqual(self.audit.get_state(state["ticket"]["id"]), before)

    def test_supported_hypothesis_is_not_a_resolved_issue(self):
        state = self.seed()
        state["ticket"]["diagnostic_report"] = {"assessment": "supported_hypothesis", "evidence_verified": True}
        result = present_ticket(state)
        self.assertEqual(result["status"]["code"], "pending_review")
        self.assertIn("仍需要支持人员核实", result["status"]["message"])

    def test_model_interruption_has_distinct_requester_message_without_internal_trace(self):
        for case_status in ("awaiting_support", "in_progress"):
            with self.subTest(status=case_status):
                state = self.seed(status=case_status)
                state["ticket"]["diagnostic_report"] = {
                    "assessment": "not_completed", "stop_reason": "model_error:ModelUnavailableError",
                    "model_failure": {"failure_kind": "http_5xx", "http_status": 503, "attempts": 3},
                    "summary": "private detail"}
                result = present_ticket(state)
                self.assertIn("自动排查暂时中断", result["status"]["message"])
                self.assertNotIn("private detail", str(result))
                self.assertNotIn("http_5xx", str(result))

    def test_model_interruption_creates_support_task_and_attention_queue(self):
        state = self.seed(status="awaiting_support")
        state["ticket"]["diagnostic_report"] = {"assessment": "not_completed",
            "stop_reason": "model_error:ModelUnavailableError", "next_check": "人工核对日志"}
        self.audit.save_checkpoint(state, "test")
        tasks, attention = support_work_items(state)
        self.assertTrue(attention)
        self.assertIn("model_interrupted", [row["code"] for row in tasks])
        listing = self.client.get("/portal/tickets", params={"scope": "support", "queue": "attention"},
                                  headers=self.headers("manager")).json()
        self.assertIn(state["ticket"]["id"], [row["id"] for row in listing["items"]])
        support = self.get(state["ticket"]["id"], "manager", "support").json()["support"]
        self.assertTrue(support["needs_attention"])
        self.assertIn("人工继续排查", [row["title"] for row in support["work_items"]])

    def test_only_confirmed_resolution_marks_incident_resolved(self):
        for resolved, group in ((None, "processing"), (False, "processing"), (True, "completed")):
            with self.subTest(resolved=resolved):
                state = self.seed(resolution={"external_issue_resolved": resolved,
                    "type": "user_confirmed" if resolved is True else "agent_task_completed"})
                self.assertEqual(present_ticket(state)["status"]["group"], group)

    def test_legacy_resolution_claim_without_requester_verification_is_not_shown_as_complete(self):
        for status, resolved in (("resolved", True), ("resolved", None),
                                 ("resolved_manually", True), ("closed", True)):
            with self.subTest(status=status, resolved=resolved):
                state = self.seed(status=status, resolution={"type": "agent_task_completed", "external_issue_resolved": resolved})
                result = present_ticket(state)
                self.assertEqual(result["status"]["code"], "pending_review")
                self.assertIn("没有申请人的验证记录", result["status"]["message"])

    def test_simulated_access_request_explains_demo_result(self):
        state = self.seed(action="grant_access")
        state["action_result"] = {"simulated": True}
        result = present_ticket(state)
        self.assertEqual(result["status"]["code"], "fulfilled")
        self.assertIn("不会修改真实账号或权限", result["status"]["message"])

    def test_later_feedback_can_mark_a_manually_closed_issue_unresolved(self):
        state = self.seed(status="resolved_manually", resolution={"outcome": "resolved", "external_issue_resolved": False})
        self.assertEqual(present_ticket(state)["status"]["code"], "pending_review")
        self.assertIn("尚未解决", present_ticket(state)["status"]["message"])

    def test_requester_list_is_own_tickets_even_for_manager(self):
        for owner in ("alice", "bob", "manager"):
            self.seed(owner=owner)
        for user in ("alice", "manager"):
            response = self.client.get("/portal/tickets", headers=self.headers(user))
            self.assertEqual(response.status_code, 200)
            self.assertEqual([item["requester"] for item in response.json()["items"]], [user])
            self.assert_public(response.json())
        alice = self.client.get("/portal/tickets", headers=self.headers()).json()["items"][0]
        self.assertEqual(self.get(alice["id"], "manager").status_code, 404)

    def test_support_requires_role_and_obeys_support_group_across_departments(self):
        same = self.seed()
        foreign = self.seed(owner="bob", department="Finance")
        self.assertEqual(self.get(same["ticket"]["id"], scope="support").status_code, 403)
        self.assertEqual(self.client.get("/portal/tickets?scope=support", headers=self.headers()).status_code, 403)
        manager = self.client.get("/portal/tickets?scope=support", headers=self.headers("manager")).json()
        self.assertEqual(manager["total"], 2)
        self.assertEqual(self.get(foreign["ticket"]["id"], "manager", "support").status_code, 200)
        admin = self.client.get("/portal/tickets?scope=support", headers=self.headers("admin")).json()
        self.assertEqual(admin["total"], 2)

    def test_support_receives_meaningful_evidence_without_model_trace(self):
        state = self.seed(diagnosis={"decisions": ["internal reasoning"], "model_calls": 2})
        state["ticket"]["diagnostic_report"] = {"summary": "需要核查日志", "observations": [{"tool": "client_events", "records": []}]}
        self.audit.save_checkpoint(state, "test")
        result = self.get(state["ticket"]["id"], "manager", "support").json()
        self.assertEqual(result["support"]["summary"], "需要核查日志")
        self.assertIn("未取得", result["support"]["diagnostic_note"])
        self.assertNotIn("internal reasoning", str(result))
        self.assertNotIn("model_calls", str(result))
        self.assertNotIn("support", self.get(state["ticket"]["id"]).json())

    def test_approval_controls_respect_required_role_and_self_approval(self):
        state = self.seed(status="awaiting_approval", action="grant_access")
        state["approval"] = Approval(state["ticket"]["id"], "grant_access", "审批", required_role="admin").to_dict()
        scope = self.agent.account_directory.resolve("alice", "erp")
        state["ticket"]["account_binding"] = scope
        state["ticket"]["execution_plan"] = {"version": 1, "action": "grant_access", "account": "alice", "resource": "erp", "permission": "use", "severity": "medium"}
        state["approval"]["account_binding"] = scope
        bound = AgentState.from_dict(state)
        self.agent.authorization.stamp(bound)
        state = bound.to_dict()
        self.audit.save_checkpoint(state, "test")
        ticket_id = state["ticket"]["id"]
        self.assertFalse(self.get(ticket_id, "manager", "support").json()["support"]["can_approve"])
        self.assertTrue(self.get(ticket_id, "admin", "support").json()["support"]["can_approve"])
        state["ticket"]["owner_id"] = "admin"
        self.audit.save_checkpoint(state, "test")
        self.assertFalse(self.get(ticket_id, "admin", "support").json()["support"]["can_approve"])

    def test_filters_search_pagination_and_counts_use_display_status(self):
        self.seed()
        self.seed(status="awaiting_clarification")
        self.seed(status="cancelled")
        self.seed(owner="bob")
        response = self.client.get("/portal/tickets?group=processing&query=erp&limit=1", headers=self.headers()).json()
        self.assertEqual(response["total"], 1)
        self.assertEqual(response["items"][0]["status"]["code"], "pending_review")
        self.assertEqual(response["counts"], {"total": 3, "processing": 1, "action_required": 1, "completed": 1})
        page = self.client.get("/portal/tickets?offset=1&limit=1", headers=self.headers()).json()
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["total"], 3)
        self.assertEqual(self.client.get("/portal/tickets?group=closed", headers=self.headers()).status_code, 422)

    def test_create_clarify_and_replay_use_public_projection(self):
        headers = {**self.headers(), "Idempotency-Key": "portal-create"}
        body = {"title": "系统打不开", "description": "请帮我看看"}
        first = self.client.post("/portal/tickets", headers=headers, json=body)
        self.assertEqual(first.status_code, 201, first.text)
        initial = first.json()
        self.assertEqual(initial["status"]["code"], "needs_input")
        self.assertEqual(self.client.post("/portal/tickets", headers=headers, json=body).json(), initial)
        answer = {"answer": "ERP 报错 503", "round_number": initial["clarification"]["round_number"]}
        response = self.client.post(f'/portal/tickets/{initial["id"]}/clarify', headers=self.headers(), json=answer)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["id"], initial["id"])
        self.assertEqual(result["status"]["code"], "pending_review")
        self.assert_public(result)
        replay = self.client.post(f'/portal/tickets/{initial["id"]}/clarify', headers=self.headers(), json=answer)
        self.assertEqual(replay.json(), result)
        self.assertEqual(self.client.post(f'/portal/tickets/{initial["id"]}/clarify', headers=self.headers("manager"), json=answer).status_code, 403)

    def test_cancel_and_confirmation_are_customer_facing(self):
        state = self.seed(status="awaiting_clarification", clarification_round=1,
            clarification={"missing_fields": ["confirmation"], "question": "raw question"},
            confirmation={"status": "pending", "plan": {"action": "grant_access", "account": "alice", "resource": "erp", "permission": "use"}})
        response = self.get(state["ticket"]["id"]).json()
        self.assertEqual(response["status"]["code"], "confirmation")
        self.assertEqual(response["confirmation"][0], {"label": "申请事项", "value": "访问权限申请"})
        result = self.client.post(f'/portal/tickets/{response["id"]}/cancel', headers=self.headers(), json={"reason": "不再需要", "case_revision": response["case_revision"]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"]["code"], "cancelled")
        self.assert_public(result.json())
        self.assertEqual(self.client.post(f'/portal/tickets/{response["id"]}/cancel', headers=self.headers("manager"), json={"reason": "不再需要"}).status_code, 403)

    def test_portal_routes_require_auth_and_static_support_has_csp(self):
        for path in ("/portal/tickets", "/portal/tickets/unknown"):
            self.assertEqual(self.client.get(path).status_code, 401)
        response = self.client.get("/support")
        self.assertEqual(response.status_code, 200)
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertIn("我的工单", response.text)
        self.assertNotIn("工具选择与预算", response.text)
        script = self.client.get("/static/app.js").text
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("/runtime", script)


if __name__ == "__main__":
    unittest.main()
