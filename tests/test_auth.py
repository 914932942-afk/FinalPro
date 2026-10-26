"""真实凭据验证、对象权限及审批隔离；所有数据使用临时目录。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import IdentityStore, initialize_demo_identities
from app.audit import AuditStore
from app.config import get_settings
from app.errors import AuthenticationError, ConfigurationError
from app.knowledge import KnowledgeBase
from app.models import Ticket
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities, auth_headers


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.identities, self.keys = demo_identities(self.root)
        self.audit = AuditStore(self.root / "test.db")
        settings = get_settings(data_dir=self.root, db_path=self.audit.db_path, llm_mode="offline", rag_backend="lexical")
        self.agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(self.root / "kb", backend="lexical"), self.audit),
                                     self.audit, settings=settings)
        self.client = TestClient(create_app(self.agent, identities=self.identities))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def headers(self, user="alice"):
        return auth_headers(self.keys, user)

    def create(self, user="alice", **overrides):
        response = self.client.post("/tickets", headers=self.headers(user),
            json={"title": "申请生产库权限", "description": "申请报表数据库只读权限", **overrides})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_all_business_routes_require_real_key(self):
        for method, path, body in (("get", "/me", None), ("get", "/tickets/unknown", None),
                ("get", "/tickets/unknown/state", None), ("post", "/tickets", {"title": "x", "description": "x"}),
                ("post", "/tickets/unknown/clarify", {"answer": "ERP", "round_number": 1}),
                ("post", "/approvals/unknown", {"approved": True})):
            for headers in ({}, {"Authorization": "Bearer admin"}, {"Authorization": "Basic admin"}):
                with self.subTest(path=path, headers=headers):
                    response = self.client.request(method, path, json=body, headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_identity_is_server_assigned_and_spoofed_fields_rejected(self):
        self.assertEqual(self.client.get("/me", headers=self.headers()).json(),
                         {"user_id": "alice", "role": "employee", "department": "IT", "support_groups": []})
        for field, value in (("user_role", "admin"), ("requester", "admin"), ("department", "Finance"), ("owner_id", "admin")):
            response = self.client.post("/tickets", headers=self.headers(),
                json={"title": "权限", "description": "生产权限", field: value})
            self.assertEqual(response.status_code, 422)
        result = self.create()
        self.assertEqual(result["ticket"]["owner_id"], "alice")
        self.assertEqual(result["ticket"]["requester"], "alice")
        self.assertEqual(result["user_role"], "employee")
        self.assertEqual(result["ticket"]["status"], "awaiting_approval")

    def test_employee_cannot_read_other_users_ticket_or_state(self):
        result = self.create()
        for suffix in ("", "/state"):
            url = f'/tickets/{result["ticket"]["id"]}{suffix}'
            self.assertEqual(self.client.get(url, headers=self.headers()).status_code, 200)
            self.assertEqual(self.client.get(url, headers=self.headers("bob")).status_code, 404)
            self.assertEqual(self.client.get(url, headers=self.headers("manager")).status_code, 200)

    def test_only_owner_can_clarify_and_checks_precede_replay(self):
        result = self.create(title="系统打不开", description="帮我看看")
        url = f'/tickets/{result["ticket"]["id"]}/clarify'
        body = {"answer": "ERP 报错 503", "round_number": 1}
        self.assertEqual(self.client.post(url, json=body, headers=self.headers("bob")).status_code, 404)
        self.assertEqual(self.client.post(url, json=body, headers=self.headers("manager")).status_code, 403)
        first = self.client.post(url, json=body, headers=self.headers())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["ticket"]["status"], "awaiting_support")
        self.assertEqual(self.client.post(url, json=body, headers=self.headers()).json(), first.json())
        self.assertEqual(self.client.post(url, json=body, headers=self.headers("admin")).status_code, 403)

    def test_employee_cannot_approve_or_spoof_approver(self):
        result = self.create()
        url = f'/approvals/{result["approval"]["id"]}'
        with patch.object(self.agent.tools, "execute_action") as execute:
            self.assertEqual(self.client.post(url, json={"approved": True}, headers=self.headers()).status_code, 403)
            self.assertEqual(self.client.post(url, json={"approved": True, "approver": "admin"},
                                             headers=self.headers()).status_code, 422)
        execute.assert_not_called()
        response = self.client.post(url, json={"approved": True}, headers=self.headers("manager"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["approval"]["decided_by"], "manager")
        self.assertEqual(response.json()["ticket"]["status"], "closed")
        self.assertEqual(self.client.post(url, json={"approved": True}, headers=self.headers()).status_code, 403)
        self.assertEqual(self.client.post(url, json={"approved": True}, headers=self.headers("manager")).json(), response.json())

    def test_managers_and_admins_cannot_skip_or_self_approve(self):
        for user in ("manager", "admin"):
            result = self.create(user)
            self.assertEqual(result["ticket"]["status"], "awaiting_approval")
            url = f'/approvals/{result["approval"]["id"]}'
            self.assertEqual(self.client.post(url, json={"approved": True}, headers=self.headers(user)).status_code, 403)
            other = "admin" if user == "manager" else "manager"
            self.assertEqual(self.client.post(url, json={"approved": True}, headers=self.headers(other)).status_code, 200)

    def test_department_scope_and_required_approval_role(self):
        records = json.loads((self.root / "auth.json").read_text())
        for record in records:
            if record["user_id"] == "alice":
                record["department"] = "Finance"
        (self.root / "auth.json").write_text(json.dumps(records))
        with TestClient(create_app(self.agent, identities=IdentityStore(self.root / "auth.json"))) as client:
            result = client.post("/tickets", json={"title": "生产权限", "description": "申请报表数据库只读权限"},
                                 headers=self.headers()).json()
            url = f'/approvals/{result["approval"]["id"]}'
            self.assertEqual(client.post(url, json={"approved": True}, headers=self.headers("manager")).status_code, 403)
            self.assertEqual(client.post(url, json={"approved": True}, headers=self.headers("admin")).status_code, 200)
        result = self.create()
        approval = self.audit.get_approval(result["approval"]["id"])
        approval["required_role"] = "admin"
        self.audit.save_approval(approval)
        url = f'/approvals/{approval["id"]}'
        self.assertEqual(self.client.post(url, json={"approved": False}, headers=self.headers("manager")).status_code, 403)
        self.assertEqual(self.client.post(url, json={"approved": False}, headers=self.headers("admin")).status_code, 200)

    def test_idempotency_keys_are_scoped_by_user(self):
        body = {"title": "密码重置", "description": "忘记密码"}
        results = []
        for user in ("alice", "bob"):
            headers = {**self.headers(user), "Idempotency-Key": "same-key"}
            first = self.client.post("/tickets", json=body, headers=headers)
            self.assertEqual(first.status_code, 201)
            self.assertEqual(first.json(), self.client.post("/tickets", json=body, headers=headers).json())
            results.append(first.json())
        self.assertNotEqual(results[0]["ticket"]["id"], results[1]["ticket"]["id"])
        states = [self.audit.get_state(result["ticket"]["id"]) for result in results]
        self.assertNotEqual(states[0]["action_result"]["idempotency_key"], states[1]["action_result"]["idempotency_key"])
        self.assertTrue(all("action_result" not in result for result in results))

    def test_keys_never_enter_responses_or_persisted_business_state(self):
        result = self.create()
        content = json.dumps(result) + self.audit.db_path.read_bytes().decode("utf-8", errors="ignore")
        for key in self.keys.values():
            self.assertNotIn(key, content)
        records = json.loads((self.root / "auth.json").read_text())
        self.assertEqual(len(records), 4)
        self.assertTrue(all("token_sha256" in record for record in records))

    def test_legacy_tickets_are_not_claimed_by_matching_name(self):
        state = self.agent.run(Ticket("生产权限", "申请报表数据库只读权限", "alice", "IT"))
        url = f"/tickets/{state.ticket.id}"
        self.assertEqual(self.client.get(url, headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.get(url, headers=self.headers("admin")).status_code, 200)
        self.assertEqual(self.client.post(f"/approvals/{state.approval.id}", json={"approved": True},
                                         headers=self.headers("admin")).status_code, 403)

    def test_disable_key_and_reload(self):
        records = json.loads((self.root / "auth.json").read_text())
        records[0]["enabled"] = False
        (self.root / "auth.json").write_text(json.dumps(records))
        with self.assertRaises(AuthenticationError):
            IdentityStore(self.root / "auth.json").authenticate(self.keys["alice"])

    def test_configuration_fails_closed_without_exposing_secret(self):
        for content in ("not-json", "[]", json.dumps([{"token": self.keys["alice"]}])):
            path = self.root / "invalid.json"
            path.write_text(content)
            with self.assertRaises(ConfigurationError) as error:
                IdentityStore(path)
            self.assertNotIn(self.keys["alice"], str(error.exception))
        with self.assertRaises(ConfigurationError):
            IdentityStore(self.root / "missing.json")
        with self.assertRaises(ConfigurationError):
            initialize_demo_identities(self.root / "auth.json")

    def test_cli_cannot_use_role_flags_or_approve_as_employee(self):
        state = self.create()
        env = {**os.environ, "AGENT_DATA_DIR": str(self.root), "AGENT_DB_PATH": str(self.audit.db_path),
               "AGENT_AUTH_FILE": str(self.root / "auth.json"), "AGENT_KB_DIR": str(self.root / "kb"),
               "AGENT_LLM_MODE": "offline", "AGENT_RAG_BACKEND": "lexical", "DEMO_API_KEY": self.keys["alice"]}
        args = [sys.executable, "-X", "utf8", "-m", "app.cli", "approve", state["approval"]["id"]]
        spoof = subprocess.run([*args, "--by", "admin"], cwd=Path(__file__).resolve().parents[1], env=env,
                               capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(spoof.returncode, 2)
        result = subprocess.run(args, cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("403", result.stderr)
        env["DEMO_API_KEY"] = self.keys["manager"]
        result = subprocess.run(args, cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["approval"]["decided_by"], "manager")

    def test_local_demo_uses_verified_identities(self):
        env = {**os.environ, "AGENT_DATA_DIR": str(self.root), "AGENT_DB_PATH": str(self.audit.db_path),
               "AGENT_AUTH_FILE": str(self.root / "auth.json"), "AGENT_KB_DIR": str(self.root / "kb"),
               "AGENT_LLM_MODE": "offline", "AGENT_RAG_BACKEND": "lexical"}
        result = subprocess.run([sys.executable, "-X", "utf8", "run_demo.py"],
            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
            text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"decided_by": "manager"', result.stdout)
        self.assertIn('"owner_id": "bob"', result.stdout)
        self.assertIn('"status": "closed"', result.stdout)
        for key in self.keys.values():
            self.assertNotIn(key, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
