"""Opaque, revocable browser sessions. SQLite stores token digests, never cookies."""
from collections import OrderedDict, deque
from contextlib import contextmanager
import hashlib
import hmac
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from .errors import AgentError, AuthenticationError, ConfigurationError

SESSION_COOKIE = "it_portal_session"
PORTAL_HEADER = "X-Portal-Request"
CSRF_HEADER = "X-CSRF-Token"


class LoginThrottled(AgentError):
    code, category = "login_throttled", "authentication"
    public_message = "登录尝试过于频繁，请稍后再试。"
    http_status = 429
    circuit_failure = False


class LoginAttemptLimiter:
    def __init__(self, limit=10, window=60, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._attempts = OrderedDict()
        self._lock = threading.Lock()

    def attempt(self, address):
        with self._lock:
            now = self.clock()
            for key in list(self._attempts):
                values = self._attempts[key]
                while values and values[0] <= now - self.window:
                    values.popleft()
                if not values:
                    del self._attempts[key]
            values = self._attempts.setdefault(address, deque())
            if len(values) >= self.limit:
                raise LoginThrottled()
            values.append(now)
            self._attempts.move_to_end(address)
            while len(self._attempts) > 1024:
                self._attempts.popitem(last=False)


class SessionStore:
    lifetime = 8 * 60 * 60

    def __init__(self, path: Path, identities, *, clock=time.time):
        self.path, self.identities, self.clock = Path(path), identities, clock
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._db() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS browser_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    credential_revision TEXT NOT NULL, expires_at REAL NOT NULL)""")
        except (OSError, sqlite3.Error):
            raise ConfigurationError("本机会话存储无法初始化") from None

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def csrf_token(token):
        return hmac.new(token.encode(), b"it-portal-csrf-v1", hashlib.sha256).hexdigest()

    def create(self, principal):
        _, revision = self.identities.session_identity(principal.user_id)
        token = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("DELETE FROM browser_sessions WHERE expires_at <= ?", (self.clock(),))
            db.execute("INSERT INTO browser_sessions VALUES (?, ?, ?, ?)",
                       (self._digest(token), principal.user_id, revision, self.clock() + self.lifetime))
        return token

    def authenticate(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise AuthenticationError()
        with self._db() as db:
            row = db.execute("SELECT user_id,credential_revision,expires_at FROM browser_sessions WHERE token_hash = ?",
                             (self._digest(token),)).fetchone()
        if not row or row[2] <= self.clock():
            raise AuthenticationError()
        principal, revision = self.identities.session_identity(row[0])
        if not hmac.compare_digest(row[1], revision):
            raise AuthenticationError()
        return principal

    def revoke(self, token):
        if isinstance(token, str) and 32 <= len(token) <= 128:
            with self._db() as db:
                db.execute("DELETE FROM browser_sessions WHERE token_hash = ?", (self._digest(token),))
