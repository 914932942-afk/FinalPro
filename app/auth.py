"""Local identities: salted password hashes for users, separate keys for CLI clients."""
import argparse
import hashlib
import hmac
import json
from pathlib import Path
import secrets
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from .errors import AuthenticationError, ConfigurationError


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    user_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    role: Literal["employee", "support", "manager", "admin"]
    department: str = Field(min_length=1, max_length=200)
    support_groups: list[Literal["network", "applications", "data", "service_desk"]] = Field(default_factory=list, max_length=4)


class Credential(Principal):
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    enabled: StrictBool = True
    password_hash: str | None = Field(default=None, pattern=r"^pbkdf2_sha256\$600000\$[a-f0-9]{32}\$[a-f0-9]{64}$", repr=False)


class PasswordHasher:
    iterations = 600_000
    dummy_hash = "pbkdf2_sha256$600000$" + "0" * 32 + "$" + "0" * 64

    @classmethod
    def hash(cls, password: str) -> str:
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise ValueError("Password must contain 12 to 128 characters")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, cls.iterations)
        return f"pbkdf2_sha256${cls.iterations}${salt.hex()}${digest.hex()}"

    @classmethod
    def verify(cls, password: str, encoded: str) -> bool:
        try:
            algorithm, rounds, salt, expected = encoded.split("$")
            if algorithm != "pbkdf2_sha256" or int(rounds) != cls.iterations or len(salt) != 32 or len(expected) != 64:
                return False
            if not isinstance(password, str) or not 1 <= len(password) <= 128:
                return False
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), cls.iterations)
            return hmac.compare_digest(actual.hex(), expected)
        except (ValueError, TypeError):
            return False


class IdentityStore:
    """Reload server-owned identities at each security boundary; fail closed."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = RLock()
        self._refresh()

    def _refresh(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list) or not raw:
                raise ValueError()
            records = tuple(Credential.model_validate(item) for item in raw)
            if (len({x.user_id for x in records}) != len(records)
                    or len({x.token_sha256 for x in records}) != len(records)):
                raise ValueError()
        except (OSError, ValueError, TypeError, ValidationError):
            raise ConfigurationError("身份文件缺失或无效，请先运行 python -m app.auth init") from None
        with self._lock:
            self._records = records
            self._by_user = {record.user_id: record for record in records}

    @staticmethod
    def _principal(record):
        return Principal.model_validate(record.model_dump(include=set(Principal.model_fields)))

    def authenticate(self, token: str | None) -> Principal:
        self._refresh()
        if not token or not 32 <= len(token) <= 256:
            raise AuthenticationError()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for record in self._records:
            if hmac.compare_digest(digest, record.token_sha256) and record.enabled:
                return self._principal(record)
        raise AuthenticationError()

    def authenticate_password(self, username: str, password: str) -> Principal:
        self._refresh()
        record = self._by_user.get(username)
        encoded = record.password_hash if record and record.password_hash else PasswordHasher.dummy_hash
        valid = PasswordHasher.verify(password, encoded)
        if not valid or not record or not record.enabled or not record.password_hash:
            raise AuthenticationError()
        return self._principal(record)

    def session_identity(self, user_id: str) -> tuple[Principal, str]:
        self._refresh()
        record = self._by_user.get(user_id)
        if not record or not record.enabled or not record.password_hash:
            raise AuthenticationError()
        principal = self._principal(record)
        revision = hashlib.sha256((record.password_hash + self.identity_revision(principal)).encode()).hexdigest()
        return principal, revision

    def principal(self, user_id: str) -> Principal:
        self._refresh()
        record = self._by_user.get(user_id)
        if not record or not record.enabled:
            raise AuthenticationError()
        return self._principal(record)

    @staticmethod
    def identity_revision(principal):
        data = principal.model_dump()
        data["support_groups"] = sorted(set(data["support_groups"]))
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def support_people(self, ticket):
        from .case_access import CaseAccess
        self._refresh()
        return [{"user_id": row.user_id, "role": row.role} for row in self._records
                if row.enabled and row.user_id != ticket.get("owner_id") and CaseAccess.can_handle(row, ticket)]


def initialize_support_groups(path: Path):
    """One-time metadata migration; preserve existing secrets and account ownership."""
    path = Path(path)
    IdentityStore(path)  # Validate before replacing any configuration.
    rows = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    for row in rows:
        if "support_groups" not in row:
            row["support_groups"] = (["network", "applications", "data", "service_desk"]
                                     if row["user_id"] == "manager" and row["role"] == "manager" and row["department"] == "IT" else [])
            changed = True
    if changed:
        temporary = path.with_name(path.name + ".groups-init.tmp")
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(rows, output, ensure_ascii=False, indent=2)
        temporary.replace(path)


def initialize_local_passwords(path: Path) -> Path:
    """Add missing password hashes without changing identity ownership, roles or keys.

    The local handoff file holds generated initial passwords for the operator;
    authentication reads only salted hashes from auth.json. Existing passwords
    are never silently reset, and repeated startup does not overwrite the file.
    """
    path = Path(path)
    initialize_support_groups(path)
    store = IdentityStore(path)
    accounts_path = path.with_name(path.stem + ".accounts.txt")
    missing = [record for record in store._records if record.password_hash is None]
    if not missing:
        return accounts_path
    if accounts_path.exists():
        raise ConfigurationError("存在初始账号文件但账号初始化未完成，请核查本机身份配置；不会覆盖已有密码")
    raw = json.loads(path.read_text(encoding="utf-8"))
    passwords = {record.user_id: secrets.token_urlsafe(12) for record in missing}
    labels = {"employee": "员工", "support": "支持人员", "manager": "经理", "admin": "管理员"}
    lines = ["IT 服务中心 · 本机初始账号", "", "登录地址：http://127.0.0.1:8080/",
             "此文件仅用于交付本机初始密码，请勿分享或提交到版本库。",
             "服务端使用 auth.json 中的密码哈希校验身份，不读取此文件。", ""]
    for record in raw:
        if record["user_id"] in passwords:
            password = passwords[record["user_id"]]
            record["password_hash"] = PasswordHasher.hash(password)
            lines.extend([f"账号：{record['user_id']}", f"身份：{labels[record['role']]} · {record['department']}", f"初始密码：{password}", ""])
    # Atomic replacement leaves existing keys and ownership intact on failure.
    temporary = path.with_name(path.name + ".password-init.tmp")
    try:
        with accounts_path.open("x", encoding="utf-8") as output:
            output.write("\n".join(lines))
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(raw, output, ensure_ascii=False, indent=2)
        temporary.replace(path)
    except OSError:
        raise ConfigurationError("本机账号初始化未完成，请核查身份文件与初始账号文件") from None
    return accounts_path


def initialize_demo_identities(path: Path) -> Path:
    """显式初始化，不覆盖已有凭据；明文 Key 只写入本机演示凭据文件。"""
    path = Path(path)
    keys_path = path.with_name(path.stem + ".keys.json")
    if path.exists() or keys_path.exists():
        raise ConfigurationError("身份或凭据文件已存在，初始化不会覆盖现有 Key")
    records, keys = [], {}
    for user_id, role in (("alice", "employee"), ("bob", "employee"),
                          ("manager", "manager"), ("admin", "admin")):
        token = secrets.token_urlsafe(32)
        records.append({"user_id": user_id, "role": role, "department": "IT",
                        "support_groups": ["network", "applications", "data", "service_desk"] if role == "manager" else [],
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "enabled": True})
        keys[user_id] = token
    path.parent.mkdir(parents=True, exist_ok=True)
    with keys_path.open("x", encoding="utf-8") as output:
        json.dump(keys, output, indent=2)
    with path.open("x", encoding="utf-8") as output:
        json.dump(records, output, ensure_ascii=False, indent=2)
    from .business_accounts import initialize_demo_bindings
    initialize_demo_bindings(path.with_name("account_bindings.json"), path)
    return keys_path


def main():
    parser = argparse.ArgumentParser(description="初始化本机 Demo 身份，无默认通用密码")
    parser.add_argument("command", choices=["init", "init-passwords"])
    args = parser.parse_args()
    from .config import get_settings
    try:
        settings = get_settings()
        keys_path = (initialize_demo_identities(settings.auth_file) if args.command == "init"
                     else initialize_local_passwords(settings.auth_file))
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    print(f"身份配置：{settings.auth_file}\n本机凭据文件：{keys_path}\n请勿分享凭据文件或提交版本库。")


if __name__ == "__main__":
    main()
