"""Support routing, live revocation, and requester projections across all entry points."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import create_app
from app.audit import AuditStore
from app.auth import IdentityStore, initialize_local_passwords, initialize_support_groups
from app.case_access import CaseAccess
from app.config import get_settings
from app.errors import AuthenticationError, AuthorizationError, IdempotencyConflict, ResourceNotFoundError
from app.knowledge import KnowledgeBase
from app.models import AgentState
from app.portal import PortalService, present_ticket
from app.service import TicketService
from app.sessions import SessionStore
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities, auth_headers
from test_case_flow import CountingActions


class AccessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.identities, self.keys = demo_identities(self.root)
        rows = self.rows()
        rows[0]["department"] = "Finance"
        for user, role, groups in (("finance", "manager", []), ("network", "support", ["network"]),
                                   ("application", "support", ["applications"])):
            token = hashlib.sha256((user + str(self.root)).encode()).hexdigest()
            self.keys[user] = token
            rows.append({"user_id": user, "role": role, "department": "Finance" if user == "finance" else "IT",
                         "support_groups": groups, "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "enabled": True})
        self.write(rows)
        self.cfg = get_settings(_env_file=None, data_dir=self.root, llm_mode="offline", rag_backend="lexical",
                                diagnosis_mode="off", diagnosis_background=False, runbook_strategy="bm25")
        self.audit = AuditStore(self.cfg.db_path)
        self.actions = CountingActions()
        self.agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(self.root / "kb", settings=self.cfg), self.audit,
                                                    adapter=self.actions), self.audit, settings=self.cfg)
        self.service = TicketService(self.agent, self.identities)
        self.portal = PortalService(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def rows(self):
        return json.loads((self.root / "auth.json").read_text(encoding="utf-8"))

    def write(self, rows):
        path = self.root / "auth.json"
        temp = path.with_suffix(".next")
        temp.write_text(json.dumps(rows), encoding="utf-8")
        temp.replace(path)

    def change(self, user, **values):
        rows = self.rows()
        next(row for row in rows if row["user_id"] == user).update(values)
        self.write(rows)

    def who(self, user):
        return self.identities.principal(user)

    def create(self, grant=False, owner="alice"):
        return self.service.create(self.who(owner), title="VPN 访问申请" if grant else "VPN 故障",
                                   description="申请 VPN 普通使用权限" if grant else "VPN 报错 691 无法连接")

    def test_network_support_handles_finance_case_and_department_manager_cannot(self):
        state = self.create()
        self.assertEqual(state.ticket.assignment["group_id"], "network")
        with self.assertRaises(ResourceNotFoundError):
            self.service.claim(self.who("finance"), state.ticket.id, 0)
        with self.assertRaises(ResourceNotFoundError):
            self.service.get_ticket(self.who("application"), state.ticket.id)
        view = self.portal.get(self.who("network"), state.ticket.id, "support")
        self.assertTrue(view["support"]["can_claim"])
        claimed = self.service.claim(self.who("network"), state.ticket.id, 0)
        self.assertEqual(claimed.ticket.assignment["assignee"], "network")
        candidates = self.identities.support_people(claimed.ticket.to_dict())
        self.assertIn("manager", [row["user_id"] for row in candidates])
        self.assertNotIn("application", [row["user_id"] for row in candidates])
        with self.assertRaises(AuthorizationError):
            self.service.transfer(self.who("network"), state.ticket.id, self.who("application"), 1, "移交")
        own = self.create(owner="network")
        for action in (self.service.inspect_operations, self.service.recover):
            with self.assertRaises(AuthorizationError):
                action(self.who("network"), own.ticket.id)
        self.assertNotIn("trace", self.service.response(self.who("network"), own))

    def test_department_approval_does_not_require_support_membership(self):
        state = self.create(grant=True)
        manager = self.who("finance")
        self.assertTrue(self.portal.get(manager, state.ticket.id, "support")["support"]["can_approve"])
        self.assertNotIn("trace", self.service.response(manager, state))
        with self.assertRaises(AuthorizationError):
            self.service.approve(self.who("network"), state.approval.id, True)
        with self.assertRaises(AuthorizationError):
            self.service.approve(self.who("manager"), state.approval.id, True)
        done = self.service.approve(manager, state.approval.id, True)
        self.assertEqual(done.ticket.status, "closed")
        self.assertEqual(len(self.actions.registrations), 1)

    def test_live_membership_revocation_releases_assignee_and_fences_stale_form(self):
        state = self.create()
        actor = self.who("network")
        state = self.service.claim(actor, state.ticket.id, 0)
        old_revision = state.ticket.case_revision
        self.change("network", support_groups=[])
        with self.assertRaises(ResourceNotFoundError):
            self.service.request_info(actor, state.ticket.id, "occurred_at", 1)
        self.agent.authorization.maintain()
        raw = self.audit.get_state(state.ticket.id)
        self.assertEqual(raw["ticket"]["status"], "awaiting_support")
        self.assertIsNone(raw["ticket"]["assignment"]["assignee"])
        self.assertGreater(raw["ticket"]["case_revision"], old_revision)
        present_ticket(raw)  # Released assignment must render without a nullable-name crash.
        with self.assertRaises(IdempotencyConflict):
            self.service.claim(self.who("admin"), state.ticket.id, 1)
        self.service.claim(self.who("admin"), state.ticket.id, 2)

    def test_disabled_owner_invalidates_pending_approval_without_execution(self):
        state = self.create(grant=True)
        self.change("alice", enabled=False)
        with self.assertRaises(AuthenticationError):
            self.identities.authenticate(self.keys["alice"])
        with self.assertRaises(IdempotencyConflict):
            self.service.approve(self.who("finance"), state.approval.id, True)
        raw = self.audit.get_state(state.ticket.id)
        self.assertEqual(raw["approval"]["status"], "invalidated")
        self.assertEqual(raw["ticket"]["status"], "manual_review")
        self.assertEqual(present_ticket(raw)["status"]["title"], "本次审批已失效")
        self.assertFalse(self.actions.registrations)
        self.change("alice", enabled=True)
        with self.assertRaises(IdempotencyConflict):
            self.service.approve(self.who("finance"), state.approval.id, True)
        self.assertFalse(self.actions.registrations)

    def test_expired_and_unstamped_legacy_approvals_are_not_executable(self):
        for legacy in (False, True):
            state = self.create(grant=True)
            state.approval.expires_at = None if legacy else (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            self.agent._persist(state, "test_expiry")
            with self.assertRaises(IdempotencyConflict):
                self.service.approve(self.who("finance"), state.approval.id, True)
        self.assertFalse(self.actions.registrations)

    def test_owner_department_change_and_background_expiry_invalidate_pending_requests(self):
        state = self.create(grant=True)
        self.change("alice", department="HR")
        self.agent.authorization.maintain()
        self.assertEqual(self.audit.get_state(state.ticket.id)["approval"]["status"], "invalidated")
        state = self.create(grant=True)
        state.approval.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.agent._persist(state, "test_expiry")
        self.agent.authorization.maintain()
        self.assertEqual(self.audit.get_state(state.ticket.id)["approval"]["status"], "invalidated")
        self.assertFalse(self.actions.registrations)

    def test_revocation_between_approval_and_adapter_is_known_rejection_and_never_replayed(self):
        state = self.create(grant=True)
        original = self.agent.tools.execute_action
        def race(ticket, action, **kwargs):
            self.change("alice", enabled=False)
            return original(ticket, action, **kwargs)
        with patch.object(self.agent.tools, "execute_action", side_effect=race):
            done = self.service.approve(self.who("finance"), state.approval.id, True)
        self.assertEqual(done.action_result.status, "rejected")
        self.assertEqual(done.action_result.error.code, "authorization_revoked")
        self.assertEqual(done.approval.status, "invalidated")
        self.assertFalse(self.actions.registrations)
        self.assertFalse(self.audit.pending_operations(state.ticket.id))
        self.change("alice", enabled=True)
        claimed = self.service.claim(self.who("manager"), state.ticket.id, 0)
        self.service.recover(self.who("manager"), state.ticket.id)
        self.assertFalse(self.actions.registrations)

    def test_approver_demotion_before_execution_blocks_grant(self):
        state = self.create(grant=True)
        original = self.agent.tools.execute_action
        def race(ticket, action, **kwargs):
            self.change("finance", role="employee")
            return original(ticket, action, **kwargs)
        with patch.object(self.agent.tools, "execute_action", side_effect=race):
            done = self.service.approve(self.who("finance"), state.approval.id, True)
        self.assertEqual(done.action_result.status, "rejected")
        self.assertFalse(self.actions.registrations)

    def test_finished_receipt_replay_preserves_history_after_disable(self):
        state = self.create(grant=True)
        done = self.service.approve(self.who("finance"), state.approval.id, True)
        self.change("alice", enabled=False)
        replay = self.service.approve(self.who("finance"), state.approval.id, True)
        self.assertEqual(done.to_dict(), replay.to_dict())
        self.assertEqual(len(self.actions.registrations), 1)

    def test_recovery_after_approval_crash_rechecks_disabled_owner_before_first_write(self):
        state = self.create(grant=True)
        with patch.dict(self.agent.nodes, execute=lambda state: (_ for _ in ()).throw(SystemExit("crash before adapter"))):
            with self.assertRaises(SystemExit):
                self.service.approve(self.who("finance"), state.approval.id, True)
        self.assertEqual(self.audit.get_state(state.ticket.id)["next_node"], "execute")
        self.change("alice", enabled=False)
        done = self.service.recover(self.who("manager"), state.ticket.id)
        self.assertEqual(done.action_result.status, "rejected")
        self.assertFalse(self.actions.registrations)

    def test_recovery_after_completed_write_preserves_receipt_after_owner_disable(self):
        save = self.audit.save_checkpoint
        def crash(state, node, *args, **kwargs):
            if node == "execute":
                raise RuntimeError("crash after adapter commit")
            return save(state, node, *args, **kwargs)
        with patch.object(self.audit, "save_checkpoint", side_effect=crash), self.assertRaises(RuntimeError):
            self.service.create(self.who("alice"), title="密码重置", description="忘记密码")
        tid = self.audit.list_tickets()[0]["id"]
        self.change("alice", enabled=False)
        done = self.service.recover(self.who("manager"), tid)
        self.assertEqual(done.ticket.status, "closed")
        self.assertEqual(done.action_result.status, "succeeded")
        self.assertEqual(len(self.actions.registrations), 1)

    def test_cli_requester_output_uses_same_projection_as_http(self):
        state = self.create()
        env = {**os.environ, "AGENT_DATA_DIR": str(self.root), "AGENT_DB_PATH": str(self.cfg.db_path),
               "AGENT_AUTH_FILE": str(self.identities.path), "AGENT_KB_DIR": str(self.root / "kb"),
               "AGENT_LLM_MODE": "offline", "AGENT_RAG_BACKEND": "lexical", "DEMO_API_KEY": self.keys["alice"]}
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "app.cli", "list"], env=env,
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        ticket = json.loads(result.stdout)["items"][0]
        self.assertEqual(ticket["id"], state.ticket.id)
        self.assertNotIn("diagnostic_report", ticket)
        self.assertNotIn("registration_context", ticket)

    def test_cookie_role_change_and_api_key_disable_take_effect_without_restart(self):
        initialize_local_passwords(self.identities.path)
        sessions = SessionStore(self.root / "sessions.db", self.identities)
        actor = self.who("network")
        token = sessions.create(actor)
        self.change("network", support_groups=[])
        with self.assertRaises(AuthenticationError):
            sessions.authenticate(token)
        self.change("network", enabled=False)
        with self.assertRaises(AuthenticationError):
            self.identities.authenticate(self.keys["network"])

    def test_requester_legacy_routes_and_ticket_lists_never_include_internal_payload(self):
        marker = "PRIVATE_DIAGNOSTIC_PAYLOAD"
        with TestClient(create_app(self.agent, identities=self.identities)) as client:
            h = auth_headers(self.keys)
            created = client.post("/tickets", headers=h, json={"title": "VPN 故障", "description": "VPN 报错 691 无法连接"})
            self.assertEqual(created.status_code, 201)
            self.assertNotIn("trace", created.json())
            tid = created.json()["ticket"]["id"]
            raw = self.audit.get_state(tid)
            for field in ("diagnosis", "planning"):
                raw[field] = {"private": marker}
            raw["trace"].append({"private": marker})
            raw["retrieved_docs"] = [{"text": marker}]
            raw["ticket"]["diagnostic_report"] = {"summary": marker, "observations": [{"private": marker}]}
            self.agent._persist(AgentState.from_dict(raw), "test_private_payload")
            for method, path in (("get", f"/tickets/{tid}"), ("get", f"/tickets/{tid}/state"),
                                 ("get", "/tickets"), ("get", f"/portal/tickets/{tid}"),
                                 ("post", f"/tickets/{tid}/investigate")):
                response = client.request(method, path, headers=h)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(marker, response.text)
                self.assertNotIn('"trace"', response.text)
            internal = client.get(f"/tickets/{tid}/state", headers=auth_headers(self.keys, "network"))
            self.assertEqual(internal.status_code, 200)
            self.assertIn(marker, internal.text)
            cancelled = client.post(f"/tickets/{tid}/cancel", headers=h, json={"reason": "不再需要", "case_revision": 0})
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            self.assertNotIn(marker, cancelled.text)

    def test_create_clarify_and_business_approval_response_are_allowlisted(self):
        with TestClient(create_app(self.agent, identities=self.identities)) as client:
            h = auth_headers(self.keys)
            state = client.post("/tickets", headers=h, json={"title": "系统打不开", "description": "帮我看看"}).json()
            self.assertNotIn("trace", state)
            response = client.post(f'/tickets/{state["ticket"]["id"]}/clarify', headers=h,
                                   json={"answer": "ERP 报错 503 无法打开", "round_number": 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("retrieved_docs", response.json())
            state = self.create(grant=True)
            response = client.post(f"/approvals/{state.approval.id}", headers=auth_headers(self.keys, "finance"), json={"approved": True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("planning", response.json())
            self.assertNotIn("action_result", response.json())

    def test_acl_metadata_migration_preserves_existing_secrets_and_explicit_membership(self):
        rows = self.rows()
        for row in rows:
            if row["user_id"] == "manager":
                row.pop("support_groups")
        self.write(rows)
        initialize_support_groups(self.identities.path)
        migrated = self.rows()
        self.assertEqual({row["user_id"]: row["token_sha256"] for row in rows},
                         {row["user_id"]: row["token_sha256"] for row in migrated})
        self.assertIn("network", self.who("manager").support_groups)
        self.assertEqual(self.who("finance").support_groups, [])
        self.change("manager", support_groups=[])
        initialize_support_groups(self.identities.path)
        self.assertEqual(self.who("manager").support_groups, [])


if __name__ == "__main__":
    unittest.main()
