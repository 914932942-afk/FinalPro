"""命令行入口：让你用自选标题/描述创建工单，或按编号审批。

在项目根目录运行 `py -m app.cli --help` 查看用法。
run_demo.py 写死了两条案例；本文件用 argparse 从终端读取用户参数。
两者都会调用 workflow.py，业务逻辑相同。
"""

import argparse

import json
import os

from .audit import AuditStore
from .knowledge import KnowledgeBase
from .model_adapter import build_model_from_env
from .tools import EnterpriseTools
from .workflow import EnterpriseAgent
from .config import get_settings
from .auth import IdentityStore
from .service import TicketService
from .errors import AgentError, AuthorizationError

def build_agent() -> EnterpriseAgent:
    """组装数据库、知识库、工具和 Agent，返回可调用的 Agent 对象。"""
    settings = get_settings()
    audit = AuditStore(settings.db_path, timeout=settings.sqlite_timeout)

    return EnterpriseAgent(
        EnterpriseTools(KnowledgeBase(settings.kb_dir, settings=settings), audit,
                        adapter=build_action_adapter(settings)),
        audit,
        model=build_model_from_env(settings),
        settings=settings,
    )

def build_action_adapter(settings):
    from .action_adapter import SimulatedActionAdapter
    if settings.action_backend == "http_incidents":
        from .http_actions import IncidentHTTPAdapter
        return IncidentHTTPAdapter(settings.action_base_url, settings.action_api_key.get_secret_value(),
                                   timeout=settings.action_timeout)
    return SimulatedActionAdapter()

def main() -> None:
    """读取命令 -> 构造对象 -> 执行业务 -> 打印 JSON。"""

    parser = argparse.ArgumentParser(description="Enterprise Ops Agent")

    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="处理一个工单")

    run.add_argument("--title", required=True)
    run.add_argument("--description", required=True)
    run.add_argument("--severity", default="medium", choices=["low", "medium", "high", "critical"])
    run.add_argument("--idempotency-key", default=None)
    sub.add_parser("index", help="将 Markdown 分块并更新 Chroma 向量索引")
    approve = sub.add_parser("approve", help="审批高风险动作")

    approve.add_argument("approval_id")

    approve.add_argument("--reject", action="store_true")
    clarify = sub.add_parser("clarify", help="补充等待澄清的工单")
    clarify.add_argument("ticket_id")
    clarify.add_argument("--answer", required=True)
    clarify.add_argument("--round", dest="round_number", required=True, type=int)
    listing = sub.add_parser("list", help="列出有权访问的工单")
    listing.add_argument("--status")
    cancel = sub.add_parser("cancel", help="取消自己等待中的工单")
    cancel.add_argument("ticket_id")
    cancel.add_argument("--reason", required=True)
    resolve = sub.add_parser("resolve", help="人工结案，不触发工具调用")
    resolve.add_argument("ticket_id")
    resolve.add_argument("--outcome", choices=["resolved", "rejected"], required=True)
    resolve.add_argument("--note", required=True)
    resolve.add_argument("--evidence", default="")
    feedback = sub.add_parser("feedback", help="独立处理人记录诊断核实结果")
    feedback.add_argument("ticket_id")
    feedback.add_argument("--revision", type=int, required=True)
    feedback.add_argument("--verdict", choices=["accepted", "rejected", "inconclusive"], required=True)
    feedback.add_argument("--note", required=True)
    feedback.add_argument("--evidence", required=True)
    feedback.add_argument("--issue-resolved", choices=["yes", "no", "unknown"], default="unknown")
    for name in ("recover", "operations", "investigate"):
        sub.add_parser(name, help="恢复或核查异常工单").add_argument("ticket_id")

    args = parser.parse_args()
    agent = build_agent()
    try:
        principal = IdentityStore(agent.settings.auth_file).authenticate(os.environ.get("DEMO_API_KEY"))
        service = TicketService(agent)
        state = execute_command(agent, service, principal, args)
    except AgentError as exc:
        raise SystemExit(f"{exc.http_status} {exc.code}: {exc}") from None
    if state is not None:
        print(json.dumps(state.to_dict() if hasattr(state, "to_dict") else state, ensure_ascii=False, indent=2))

def execute_command(agent, service, principal, args):
    if args.command == "investigate":
        return service.investigate(principal, args.ticket_id)
    if args.command == "feedback":
        return service.feedback(principal, args.ticket_id, args.revision, args.verdict, args.note,
                                args.evidence, {"yes": True, "no": False, "unknown": None}[args.issue_resolved])
    if args.command == "list":
        return service.list_tickets(principal, args.status)
    if args.command == "cancel":
        return service.cancel(principal, args.ticket_id, args.reason)
    if args.command == "resolve":
        return service.resolve(principal, args.ticket_id, args.outcome, args.note, args.evidence)
    if args.command == "recover":
        return service.recover(principal, args.ticket_id)
    if args.command == "operations":
        return service.inspect_operations(principal, args.ticket_id)
    if args.command == "index":
        if principal.role != "admin":
            raise AuthorizationError("只有管理员可以更新知识索引")
        print(json.dumps(agent.tools.kb.reindex(), ensure_ascii=False, indent=2))
        return
    if args.command == "run":
        return service.create(principal, title=args.title, description=args.description,
                              severity=args.severity, idempotency_key=args.idempotency_key)
    elif args.command == "clarify":
        return service.clarify(principal, args.ticket_id, args.answer, args.round_number)
    else:
        return service.approve(principal, args.approval_id, not args.reject)

if __name__ == "__main__":
    main()
