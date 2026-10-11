"""Run two representative tickets through the workflow without starting HTTP services."""

import json

from app.audit import AuditStore
from app.knowledge import KnowledgeBase
from app.model_adapter import build_model_from_env
from app.tools import EnterpriseTools
from app.workflow import EnterpriseAgent
from app.config import get_settings
from app.auth import IdentityStore
from app.service import TicketService

def main() -> None:
    """按顺序组装依赖、运行两张工单、模拟批准并打印结果。"""

    settings = get_settings()
    audit = AuditStore(settings.db_path, timeout=settings.sqlite_timeout)

    agent = EnterpriseAgent(
        EnterpriseTools(KnowledgeBase(settings.kb_dir, settings=settings), audit),
        audit,
        model=build_model_from_env(settings),
        settings=settings,
    )

    identities = IdentityStore(settings.auth_file)
    keys_path = settings.auth_file.with_name(settings.auth_file.stem + ".keys.json")
    keys = json.loads(keys_path.read_text(encoding="utf-8"))
    service = TicketService(agent)
    alice = identities.authenticate(keys["alice"])
    bob = identities.authenticate(keys["bob"])
    manager = identities.authenticate(keys["manager"])
    password = service.create(alice, title="无法登录", description="员工忘记密码，需要重置")
    access = service.create(bob, title="申请报表库权限", description="需要报表数据库只读权限")

    print("=== 低风险自动处理 ===")

    print(json.dumps(password.to_dict(), ensure_ascii=False, indent=2))
    print("=== 高风险进入审批 ===")
    print(json.dumps(access.to_dict(), ensure_ascii=False, indent=2))
    if access.approval:

        approved = service.approve(manager, access.approval.id, True)
        print("=== 审批后继续执行 ===")
        print(json.dumps(approved.to_dict(), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
