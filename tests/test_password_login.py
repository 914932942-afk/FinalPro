"""Password login, cookie lifecycle, CSRF, and preservation of existing identities."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.api import create_app, LoginRequest
from app.audit import AuditStore
from app.auth import IdentityStore, PasswordHasher, initialize_local_passwords
from app.config import get_settings
from app.errors import AuthenticationError
from app.knowledge import KnowledgeBase
from app.sessions import SessionStore, LoginAttemptLimiter, LoginThrottled, SESSION_COOKIE
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from auth_helpers import demo_identities, auth_headers


class PasswordLoginTests(unittest.TestCase):
    password = "Test-Password-For-Local-Login!"

    @classmethod
    def setUpClass(cls):
        cls.encoded = PasswordHasher.hash(cls.password)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, self.keys = demo_identities(self.root)
        self.auth_path = self.root / "auth.json"
        records = json.loads(self.auth_path.read_text(encoding="utf-8"))
        for record in records:
            record["password_hash"] = self.encoded
        self.auth_path.write_text(json.dumps(records), encoding="utf-8")
        self.identities = IdentityStore(self.auth_path)
        cfg = get_settings(_env_file=None, data_dir=self.root, llm_mode="offline", rag_backend="lexical",
                           runbook_strategy="bm25", diagnosis_mode="off")
        self.audit = AuditStore(cfg.db_path)
        self.agent = EnterpriseAgent(EnterpriseTools(KnowledgeBase(self.root / "kb", backend="lexical"), self.audit),
                                     self.audit, settings=cfg)
        self.app = create_app(self.agent, identities=self.identities)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self, username="alice", password=None, **options):
        return self.client.post("/auth/login", json={"username": username, "password": password or self.password},
                                headers={"X-Portal-Request": "1", **options})

    def csrf(self, response):
        return {"X-Portal-Request": "1", "X-CSRF-Token": response.json()["csrf_token"]}

    def test_password_login_returns_identity_and_sets_protected_cookie(self):
        result = self.login("bob")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["user"], {"user_id": "bob", "role": "employee", "department": "IT"})
        cookie = result.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Max-Age=28800", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("password", result.text)
        self.assertNotIn(self.password, result.text)
        self.assertNotIn(self.encoded, result.text)
        for value in self.keys.values():
            self.assertNotIn(value, result.text)
        self.assertEqual(self.client.get("/me").json()["user_id"], "bob")
        self.assertEqual(self.client.get("/portal/tickets?scope=support").status_code, 403)

    def test_session_restores_on_refresh_and_role_is_server_assigned(self):
        response = self.login("manager")
        session = self.client.get("/auth/session")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json(), response.json())
        self.assertEqual(self.client.get("/portal/tickets?scope=support").status_code, 200)
        spoofed = self.client.post("/auth/login", headers={"X-Portal-Request": "1"},
            json={"username": "alice", "password": self.password, "role": "admin"})
        self.assertEqual(spoofed.status_code, 422)
        self.assertNotIn(self.password, spoofed.text)

    def test_wrong_password_unknown_user_and_disabled_user_share_error(self):
        unknown = self.login("unknown").json()
        wrong = self.login(password="incorrect-password").json()
        records = json.loads(self.auth_path.read_text())
        records[0]["enabled"] = False
        self.auth_path.write_text(json.dumps(records))
        self.app.state.identities = IdentityStore(self.auth_path)
        disabled = self.login()
        self.assertEqual(disabled.status_code, 401)
        self.assertEqual(unknown, wrong)
        self.assertEqual(wrong, disabled.json())
        self.assertNotIn("incorrect-password", str(wrong))

    def test_password_and_token_digests_only_are_stored_for_authentication(self):
        self.login()
        cookie = self.client.cookies.get(SESSION_COOKIE)
        data = self.auth_path.read_bytes() + self.app.state.sessions.path.read_bytes()
        self.assertNotIn(self.password.encode(), data)
        self.assertNotIn(cookie.encode(), data)
        with closing(sqlite3.connect(self.app.state.sessions.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM browser_sessions").fetchone()[0], 1)

    def test_cookie_writes_require_csrf_and_same_origin(self):
        login = self.login()
        body = {"title": "系统打不开", "description": "请帮我看看"}
        for headers in ({}, {"X-Portal-Request": "1"}, {"X-Portal-Request": "1", "X-CSRF-Token": "0" * 64},
                        {**self.csrf(login), "Origin": "https://other.example"},
                        {**self.csrf(login), "Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.post("/portal/tickets", json=body, headers=headers).status_code, 403)
        self.assertEqual(len(self.audit.list_tickets()), 0)
        non_ascii = self.client.post("/portal/tickets", json=body,
            headers=[(b"X-Portal-Request", b"1"), (b"X-CSRF-Token", b"\xe9" * 64)])
        self.assertEqual(non_ascii.status_code, 403)
        result = self.client.post("/portal/tickets", json=body, headers={**self.csrf(login), "Origin": "http://testserver"})
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["requester"], "alice")
        self.assertEqual(result.json()["status"]["code"], "needs_input")

    def test_cross_origin_login_and_missing_custom_header_are_rejected(self):
        body = {"username": "alice", "password": self.password}
        self.assertEqual(self.client.post("/auth/login", json=body).status_code, 403)
        self.assertEqual(self.login(Origin="https://foreign.example").status_code, 403)
        self.assertEqual(self.login(Origin="null").status_code, 403)
        self.assertNotIn(SESSION_COOKIE, self.client.cookies)

    def test_logout_invalidates_cookie_on_server_and_is_idempotent(self):
        login = self.login()
        old_token = self.client.cookies.get(SESSION_COOKIE)
        self.assertEqual(self.client.post("/auth/logout", headers={"X-Portal-Request": "1"}).status_code, 403)
        self.assertEqual(self.client.get("/auth/session").status_code, 200)
        result = self.client.post("/auth/logout", headers=self.csrf(login))
        self.assertEqual(result.status_code, 204)
        self.assertNotIn(SESSION_COOKIE, self.client.cookies)
        self.assertEqual(self.client.get("/me", headers={"Cookie": SESSION_COOKIE + "=" + old_token}).status_code, 401)
        self.assertEqual(self.client.post("/auth/logout", headers={"X-Portal-Request": "1"}).status_code, 204)

    def test_expiration_is_enforced_by_server_and_clears_cookie(self):
        login = self.login()
        now = self.app.state.sessions.clock()
        self.app.state.sessions.clock = lambda: now + SessionStore.lifetime + 1
        result = self.client.get("/auth/session")
        self.assertEqual(result.status_code, 401)
        self.assertIn("Max-Age=0", result.headers["set-cookie"])
        self.assertEqual(self.client.get("/portal/tickets").status_code, 401)

    def test_login_rotates_session_and_revokes_previous_session(self):
        self.login()
        old = self.client.cookies.get(SESSION_COOKIE)
        self.login("bob")
        new = self.client.cookies.get(SESSION_COOKIE)
        self.assertNotEqual(old, new)
        with self.assertRaises(AuthenticationError):
            self.app.state.sessions.authenticate(old)
        self.assertEqual(self.app.state.sessions.authenticate(new).user_id, "bob")

    def test_sessions_persist_across_restart_but_password_changes_revoke_them(self):
        self.login()
        token = self.client.cookies.get(SESSION_COOKIE)
        restarted = SessionStore(self.app.state.sessions.path, IdentityStore(self.auth_path))
        self.assertEqual(restarted.authenticate(token).user_id, "alice")
        records = json.loads(self.auth_path.read_text())
        records[0]["password_hash"] = PasswordHasher.hash("Another-Password-For-Tests!")
        self.auth_path.write_text(json.dumps(records))
        changed = SessionStore(restarted.path, IdentityStore(self.auth_path))
        with self.assertRaises(AuthenticationError):
            changed.authenticate(token)

    def test_repeated_login_attempts_are_limited(self):
        self.app.state.login_limiter = LoginAttemptLimiter(limit=2)
        self.assertEqual(self.login(password="wrong-password").status_code, 401)
        self.assertEqual(self.login(password="wrong-password").status_code, 401)
        result = self.login()
        self.assertEqual(result.status_code, 429)
        self.assertEqual(result.headers["Retry-After"], "60")

    def test_password_validation_never_echoes_submitted_secrets(self):
        for password in ("sensitive" * 20, "", {"sensitive": self.password}):
            result = self.client.post("/auth/login", json={"username": "alice", "password": password}, headers={"X-Portal-Request": "1"})
            self.assertEqual(result.status_code, 422)
            self.assertNotIn("sensitive", result.text)
            self.assertNotIn(self.password, result.text)

    def test_https_cookie_has_secure_flag(self):
        with TestClient(create_app(self.agent, identities=self.identities), base_url="https://testserver") as client:
            result = client.post("/auth/login", json={"username": "alice", "password": self.password}, headers={"X-Portal-Request": "1"})
            self.assertEqual(result.status_code, 200)
            self.assertIn("Secure", result.headers["set-cookie"])

    def test_legacy_cli_bearer_still_works_without_browser_csrf(self):
        result = self.client.post("/tickets", headers=auth_headers(self.keys),
                                  json={"title": "系统打不开", "description": "请帮我看看"})
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["ticket"]["owner_id"], "alice")

    def test_login_page_uses_standard_account_and_password_fields(self):
        page = self.client.get("/").text
        self.assertIn('autocomplete="username"', page)
        self.assertIn('autocomplete="current-password"', page)
        self.assertNotIn('id="api-key"', page)
        self.assertNotIn("访问凭证", page)
        script = self.client.get("/static/app.js").text
        self.assertNotIn("Bearer ", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)


class LocalAccountInitializationTests(unittest.TestCase):
    def test_initialization_preserves_keys_roles_and_existing_passwords(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, keys = demo_identities(root)
            path = root / "auth.json"
            before = json.loads(path.read_text())
            output = initialize_local_passwords(path)
            after = json.loads(path.read_text())
            self.assertEqual([{k: v for k, v in row.items() if k != "password_hash"} for row in after], before)
            self.assertEqual(len({row["password_hash"] for row in after}), 4)
            lines = output.read_text(encoding="utf-8").splitlines()
            passwords = [line.split("：", 1)[1] for line in lines if line.startswith("初始密码：")]
            usernames = [line.split("：", 1)[1] for line in lines if line.startswith("账号：")]
            store = IdentityStore(path)
            for user, password in zip(usernames, passwords):
                self.assertEqual(store.authenticate_password(user, password).user_id, user)
                self.assertEqual(store.authenticate(keys[user]).user_id, user)
                self.assertNotIn(password, path.read_text())
            checkpoint = path.read_bytes(), output.read_bytes()
            self.assertEqual(initialize_local_passwords(path), output)
            self.assertEqual((path.read_bytes(), output.read_bytes()), checkpoint)

    def test_salted_hashes_and_password_whitespace_are_significant(self):
        password = "  spaces-are-significant!  "
        first, second = PasswordHasher.hash(password), PasswordHasher.hash(password)
        self.assertNotEqual(first, second)
        self.assertTrue(PasswordHasher.verify(password, first))
        self.assertFalse(PasswordHasher.verify(password.strip(), first))
        self.assertEqual(LoginRequest(username=" alice ", password=password).password.get_secret_value(), password)

    def test_rate_limit_window_expires(self):
        now = [100]
        limiter = LoginAttemptLimiter(limit=1, window=60, clock=lambda: now[0])
        limiter.attempt("local")
        with self.assertRaises(LoginThrottled):
            limiter.attempt("local")
        now[0] += 61
        limiter.attempt("local")


if __name__ == "__main__":
    unittest.main()
