"""副作用适配器契约：真实实现须向下游发送同一个 Idempotency-Key。"""
from typing import Protocol

from .models import ActionResult, Ticket


class ActionAdapter(Protocol):
    def attach_evidence(self, ticket: Ticket, *, idempotency_key: str) -> ActionResult:
        """A separate idempotent downstream update; must not recreate the incident."""
        ...

    def execute(self, ticket: Ticket, action: str, *, idempotency_key: str) -> ActionResult:
        """HTTP 适配器使用 headers={"Idempotency-Key": idempotency_key}，不能生成新键。

        明确业务拒绝抛 ActionRejectedError；超时抛 ToolTimeoutError。
        未知结果禁止假报 succeeded；返回值必须回显传入的幂等键和 action。
        """
        ...


class SimulatedActionAdapter:
    def attach_evidence(self, ticket: Ticket, *, idempotency_key: str) -> ActionResult:
        return ActionResult("attach_evidence", "succeeded",
            "已模拟补充调查附件，结论待人工核查；未执行修复。", idempotency_key, simulated=True)

    def execute(self, ticket: Ticket, action: str, *, idempotency_key: str) -> ActionResult:
        plan = ticket.execution_plan or {}
        target = plan.get("resource", "未指定资源")
        account = (ticket.account_binding or {}).get("account_id") or plan.get("account", ticket.requester)
        messages = {
            "reset_password": f"已为 {account} 创建密码重置链接（演示模式，未触达真实 IAM）。",
            "open_incident": f"已模拟登记 {target} 事件，级别 {ticket.severity}，关联工单 {ticket.id}。",
            "grant_access": f"已模拟为 {account} 开通 {target} 的 {plan.get('permission', '未指定')} 权限，未触达真实 IAM。",
        }
        if action == "open_incident" and ticket.diagnostic_report:
            label = "待人工核查的诊断假设：" if ticket.diagnostic_report.get("format") == "investigation_v1" else "只读诊断附件："
            messages[action] += " " + label + ticket.diagnostic_report["summary"] + "；未执行修复。"
        return ActionResult(
            action=action, status="succeeded" if action in messages else "manual_review",
            message=messages.get(action, "等待人工处理。"), idempotency_key=idempotency_key,
            simulated=True,
        )
