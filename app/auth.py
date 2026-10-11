"""Demo 身份认证：独立随机 API Key，服务端仅存摘要和预置身份。"""
import argparse
import hashlib
import hmac
import json
from pathlib import Path
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from .errors import AuthenticationError, ConfigurationError

class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    user_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    role: Literal["employee", "manager", "admin"]
    department: str = Field(min_length=1, max_length=200)

class Credential(Principal):
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    enabled: StrictBool = True

class IdentityStore:
    """启动时读取服务端配置；轮换或禁用 Key 后重启服务生效。"""

    def __init__(self, path: Path):
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(raw, list) or not raw:
                raise ValueError()
            records = tuple(Credential.model_validate(item) for item in raw)
            if (len({x.user_id for x in records}) != len(records)
                    or len({x.token_sha256 for x in records}) != len(records)):
                raise ValueError()
        except (OSError, ValueError, TypeError, ValidationError):
            raise ConfigurationError("身份文件缺失或无效，请先运行 python -m app.auth init") from None
        self._records = records

    def authenticate(self, token: str | None) -> Principal:
        if not token or not 32 <= len(token) <= 256:
            raise AuthenticationError()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for record in self._records:
            if hmac.compare_digest(digest, record.token_sha256) and record.enabled:
                return Principal(user_id=record.user_id, role=record.role, department=record.department)
        raise AuthenticationError()

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
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "enabled": True})
        keys[user_id] = token
    path.parent.mkdir(parents=True, exist_ok=True)
    with keys_path.open("x", encoding="utf-8") as output:
        json.dump(keys, output, indent=2)
    with path.open("x", encoding="utf-8") as output:
        json.dump(records, output, ensure_ascii=False, indent=2)
    return keys_path

def main():
    parser = argparse.ArgumentParser(description="初始化本机 Demo 身份，无默认通用密码")
    parser.add_argument("command", choices=["init"])
    parser.parse_args()
    from .config import get_settings
    try:
        settings = get_settings()
        keys_path = initialize_demo_identities(settings.auth_file)
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    print(f"身份配置：{settings.auth_file}\n本机演示凭据：{keys_path}\n请勿分享凭据文件或提交版本库。")

if __name__ == "__main__":
    main()
