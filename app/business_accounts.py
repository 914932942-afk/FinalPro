"""Server-owned links between portal identities and service accounts; never infer a link."""
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

Resource = Literal["self_account", "vpn", "erp", "reports_db"]
DATA_GAPS = {
    "diagnosis_not_supported": ("该系统暂未接入自动排查，已交由支持人员处理。", "请对应支持组按该系统的规程人工核查，或接入经授权的只读日志适配器。"),
    "mapping_not_configured": ("尚未配置业务账号关联。", "请支持人员配置申请人与目标系统的账号关联。"),
    "mapping_invalid": ("业务账号关联配置需要核查。", "请支持人员核查重复关联、账号归属及配置格式。"),
    "account_unbound": ("尚未关联您在该系统中的业务账号。", "请支持人员核验身份并补齐该系统的账号关联。"),
    "account_binding_changed": ("业务账号关联已变化，已停止本轮自动排查。", "请支持人员核对原工单的目标账号与当前关联，再决定后续处理。"),
    "source_not_configured": ("该系统的日志源尚未接入。", "请支持人员接入对应系统的日志源。"),
    "source_unavailable": ("暂时无法读取该系统的日志。", "请支持人员检查日志服务的可用性。"),
    "source_forbidden": ("当前诊断服务没有读取日志的权限。", "请支持人员核查日志读取权限。"),
    "source_invalid": ("日志数据格式或归属信息需要核查。", "请支持人员校验日志结构、账号归属和记录标识。"),
    "account_not_found": ("日志源中尚未找到已关联的业务账号。", "请支持人员核对账号标识及日志覆盖范围。"),
    "no_records": ("已关联业务账号，但暂未取得相关日志记录。", "请支持人员核对采集状态和日志时间范围。"),
    "no_client_records": ("暂未取得可关联本次请求的客户端记录。", "请支持人员补齐该账号的客户端与会话关联记录。"),
    "session_records_missing": ("已取得客户端线索，但关联的会话记录尚未覆盖。", "请支持人员补齐该账号对应会话的服务端日志。"),
}


class DiagnosticDataGap(ValueError):
    def __init__(self, code):
        self.code = code
        self.detail = gap_detail(code)
        super().__init__(self.detail["message"])


def gap_detail(code):
    message, next_check = DATA_GAPS[code]
    return {"code": code, "message": message, "next_check": next_check}


class ServiceAccount(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, str_strip_whitespace=True)
    user_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    resource: Resource
    account_id: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.@\\:-]*$")
    enabled: StrictBool = True


class AccountBindings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    bindings: list[ServiceAccount] = Field(max_length=1000)


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    owner_id: str = Field(min_length=1, max_length=100)
    resource: Resource
    account_id: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: StrictBool | None = None
    code: Literal["ready", "business_account_disabled", "mapping_not_configured", "mapping_invalid", "account_unbound"]
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def consistent(self):
        if self.code in {"ready", "business_account_disabled"}:
            if self.account_id is None or self.enabled is not (self.code == "ready"):
                raise ValueError("inconsistent business account state")
        elif self.account_id is not None or self.enabled is not None:
            raise ValueError("unresolved mapping must not contain an account")
        return self


class AccountDirectory:
    def __init__(self, path):
        self.path = Path(path)

    def resolve(self, owner, resource):
        code, selected = "account_unbound", None
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(262145)
            if len(raw) > 262144:
                raise ValueError()
            rows = AccountBindings.model_validate_json(raw).bindings
            if (len({(row.user_id, row.resource) for row in rows}) != len(rows)
                    or len({(row.resource, row.account_id) for row in rows}) != len(rows)):
                raise ValueError("ambiguous account ownership")
            selected = next((row for row in rows if row.user_id == owner and row.resource == resource), None)
            if selected:
                code = "ready" if selected.enabled else "business_account_disabled"
        except FileNotFoundError:
            code = "mapping_not_configured"
        except (OSError, ValueError, ValidationError):
            code = "mapping_invalid"
        scope = {"owner_id": owner, "resource": resource, "account_id": selected.account_id if selected else None,
                 "enabled": selected.enabled if selected else None, "code": code}
        scope["revision"] = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        return scope

    def validate(self, scope, *, write=False):
        try:
            scope = AccountSnapshot.model_validate(scope).model_dump()
        except (ValueError, TypeError):
            raise DiagnosticDataGap("account_binding_changed") from None
        if not scope or self.resolve(scope["owner_id"], scope["resource"]) != scope:
            raise DiagnosticDataGap("account_binding_changed")
        if scope["code"] in DATA_GAPS:
            raise DiagnosticDataGap(scope["code"])
        if write and (scope["code"] != "ready" or scope["enabled"] is not True):
            raise DiagnosticDataGap("account_binding_changed")
        return scope


def demo_bindings():
    """Explicit demo identities only; arbitrary usernames never acquire inferred bindings."""
    rows = []
    for owner in ("alice", "bob", "manager", "admin"):
        for resource in ("self_account", "vpn", "erp", "reports_db"):
            account = ({"vpn": "vpn.bob", "erp": "erp.bob", "reports_db": "report.bob"}.get(resource, owner)
                       if owner == "bob" else owner)
            rows.append({"user_id": owner, "resource": resource, "account_id": account, "enabled": True})
    return {"version": 1, "bindings": rows}


def initialize_demo_bindings(path, identity_path):
    path = Path(path)
    if path.exists():
        return path
    identities = json.loads(Path(identity_path).read_text(encoding="utf-8"))
    owners = {row["user_id"] for row in identities}
    data = demo_bindings()
    data["bindings"] = [row for row in data["bindings"] if row["user_id"] in owners]
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
    except FileExistsError:
        pass
    return path


def main():
    import argparse
    from .config import get_settings
    parser = argparse.ArgumentParser(description="初始化本机演示业务账号关联；保留已有配置，不推断自定义账号。")
    parser.add_argument("command", choices=["init-demo"])
    parser.parse_args()
    settings = get_settings()
    target = initialize_demo_bindings(settings.account_bindings_file, settings.auth_file)
    print(f"业务账号关联配置：{target}\n仅初始化已知演示身份；自定义身份需由维护人员明确配置。")


if __name__ == "__main__":
    main()
