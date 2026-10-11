"""API 和 CLI 共用的授权边界；Agent 内核只接收服务端确认后的身份。"""
import hashlib
import json

from .auth import Principal
from .errors import AuthorizationError, ResourceNotFoundError, RequestValidationError
from .models import Ticket

class TicketService:
    def __init__(self, agent):
        self.agent = agent

    def create(self, principal: Principal, *, title: str, description: str,
               severity: str = "medium", idempotency_key: str | None = None):
        title = self._text(title, 300)
        description = self._text(description, 10000)
        if severity not in {"low", "medium", "high", "critical"}:
            raise RequestValidationError()
        if idempotency_key is not None:
            idempotency_key = self._text(idempotency_key, 200)

        key = None
        if idempotency_key is not None:
            identity = json.dumps([principal.user_id, idempotency_key], ensure_ascii=False)
            key = "request:" + hashlib.sha256(identity.encode()).hexdigest()
        ticket = Ticket(title, description, principal.user_id, principal.department,
                        severity=severity, owner_id=principal.user_id, idempotency_key=key)
        return self.agent.run(ticket, principal.role)

    def get_ticket(self, principal: Principal, ticket_id: str):
        ticket = self.agent.audit.get_ticket(ticket_id)
        if ticket is None:
            raise ResourceNotFoundError()
        owner = ticket.get("owner_id")
        allowed = (principal.role == "admin" or owner == principal.user_id
                   or (owner is not None and principal.role == "manager"
                       and ticket["department"] == principal.department))
        if not allowed:

            raise ResourceNotFoundError()
        return ticket

    def get_state(self, principal: Principal, ticket_id: str):
        self.get_ticket(principal, ticket_id)
        state = self.agent.audit.get_state(ticket_id)
        if state is None:
            raise ResourceNotFoundError()
        return state

    def clarify(self, principal: Principal, ticket_id: str, answer: str, round_number: int):
        ticket = self.get_ticket(principal, ticket_id)
        if ticket.get("owner_id") != principal.user_id:
            raise AuthorizationError("只有申请人可以补充工单")
        answer = self._text(answer, 10000)
        if type(round_number) is not int or not 1 <= round_number <= 10:
            raise RequestValidationError()
        return self.agent.clarify(ticket_id, answer, round_number)

    def approve(self, principal: Principal, approval_id: str, approved: bool):
        if principal.role not in {"manager", "admin"}:
            raise AuthorizationError("只有经理或管理员可以审批")
        approval = self.agent.audit.get_approval(approval_id)
        if approval is None:
            raise ResourceNotFoundError()
        ticket = self.get_ticket(principal, approval["ticket_id"])

        if not ticket.get("owner_id"):
            raise AuthorizationError("旧工单没有已验证的申请人，请通过认证接口重新提交")
        if ticket["owner_id"] == principal.user_id:
            raise AuthorizationError("不能审批自己的申请")
        required_role = approval.get("required_role")
        if required_role not in {"manager", "admin"} or (required_role == "admin" and principal.role != "admin"):
            raise AuthorizationError("审批角色不满足要求")
        return self.agent.approve(approval_id, principal.user_id, approved)

    def list_tickets(self, principal: Principal, status=None, limit=50, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise RequestValidationError()
        tickets = [ticket for ticket in self.agent.audit.list_tickets()
                   if (principal.role == "admin" or ticket.get("owner_id") == principal.user_id
                       or (ticket.get("owner_id") and principal.role == "manager"
                           and ticket["department"] == principal.department))
                   and (status is None or ticket["status"] == status)]
        return {"items": tickets[offset:offset + limit], "total": len(tickets)}

    @staticmethod
    def _text(value, max_length=2000, required=True):
        if not isinstance(value, str) or len(value) > max_length or (required and not value.strip()):
            raise RequestValidationError()
        return value.strip()

    def _operator(self, principal, ticket_id):
        if principal.role not in {"manager", "admin"}:
            raise AuthorizationError("只有经理或管理员可以处理人工待办")
        ticket = self.get_ticket(principal, ticket_id)
        if not ticket.get("owner_id"):
            raise AuthorizationError("旧工单无可信归属，请重新提交")
        return ticket

    def cancel(self, principal, ticket_id, reason):
        ticket = self.get_ticket(principal, ticket_id)
        if ticket.get("owner_id") != principal.user_id:
            raise AuthorizationError("只有申请人可以取消工单")
        return self.agent.cancel(ticket_id, principal.user_id, self._text(reason))

    def resolve(self, principal, ticket_id, outcome, note, evidence=""):
        ticket = self._operator(principal, ticket_id)
        if ticket["owner_id"] == principal.user_id:
            raise AuthorizationError("不能人工结案自己的工单")
        if outcome not in {"resolved", "rejected"}:
            raise RequestValidationError()
        return self.agent.resolve(ticket_id, principal.user_id, outcome, self._text(note),
                                  self._text(evidence, required=False))

    def inspect_operations(self, principal, ticket_id):
        self._operator(principal, ticket_id)
        state = self.get_state(principal, ticket_id)
        result = {"ticket_id": ticket_id, "status": state["ticket"]["status"],
                "next_node": state.get("next_node"),
                "pending_operations": self.agent.audit.pending_operations(ticket_id)}
        lookup = getattr(self.agent.tools.adapter, "lookup", None)
        if lookup:
            result["downstream_receipts"] = []
            for operation in result["pending_operations"]:
                if operation.get("kind") != "tool":
                    continue
                try:
                    receipt = lookup(operation["key"])
                    result["downstream_receipts"].append({"key": operation["key"], "receipt": receipt})
                except Exception as exc:
                    result["downstream_receipts"].append({"key": operation["key"], "unavailable": type(exc).__name__})
            result["receipt_note"] = "只读核查凭证，不自动改变未知结果或重放副作用。"
        return result

    def investigate(self, principal, ticket_id):
        self.get_ticket(principal, ticket_id)
        return self.agent.investigate(ticket_id)

    def feedback(self, principal, ticket_id, revision, verdict, note, evidence, issue_resolved=None):
        ticket = self._operator(principal, ticket_id)
        if ticket["owner_id"] == principal.user_id:
            raise AuthorizationError("不能验证自己的诊断结果")
        if (type(revision) is not int or revision < 1 or verdict not in {"accepted", "rejected", "inconclusive"}
                or issue_resolved is not None and type(issue_resolved) is not bool):
            raise RequestValidationError()
        return self.agent.feedback(ticket_id, principal.user_id, revision, verdict, self._text(note),
                                   self._text(evidence), issue_resolved)

    def recover(self, principal, ticket_id):
        self._operator(principal, ticket_id)
        return self.agent.recover(ticket_id, principal.user_id)
