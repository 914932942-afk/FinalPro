"""API 和 CLI 共用的授权边界；Agent 内核只接收服务端确认后的身份。"""
import hashlib
import json

from .auth import Principal, IdentityStore
from .case_access import CaseAccess
from .errors import AuthenticationError, AuthorizationError, ResourceNotFoundError, RequestValidationError
from .models import Ticket


class TicketService:
    def __init__(self, agent, identities=None):
        self.agent = agent
        self.identities = identities or (agent.authorization.identities if agent.authorization else None)
        if self.identities is None and agent.settings.auth_file.is_file():
            self.identities = IdentityStore(agent.settings.auth_file)
        if self.identities:
            agent.bind_identities(self.identities)

    def current(self, principal):
        if not self.identities:
            return principal  # Trusted in-process callers without an identity store.
        current = self.identities.principal(principal.user_id)
        if (current.role, current.department) != (principal.role, principal.department):
            raise AuthorizationError("身份权限已有变化，请重新登录")
        return current

    def create(self, principal: Principal, *, title: str, description: str,
               severity: str = "medium", idempotency_key: str | None = None):
        principal = self.current(principal)
        title = self._text(title, 300)
        description = self._text(description, 10000)
        if severity not in {"low", "medium", "high", "critical"}:
            raise RequestValidationError()
        if idempotency_key is not None:
            idempotency_key = self._text(idempotency_key, 200)
        # 用户隔离，避免不同身份使用相同客户端幂等键读到彼此结果。
        key = None
        if idempotency_key is not None:
            identity = json.dumps([principal.user_id, idempotency_key], ensure_ascii=False)
            key = "request:" + hashlib.sha256(identity.encode()).hexdigest()
        ticket = Ticket(title, description, principal.user_id, principal.department,
                        severity=severity, owner_id=principal.user_id, idempotency_key=key)
        return self.agent.run(ticket, principal.role, deduplicate=True)

    def get_ticket(self, principal: Principal, ticket_id: str):
        principal = self.current(principal)
        ticket = self.agent.audit.get_ticket(ticket_id)
        if ticket is None:
            raise ResourceNotFoundError()
        allowed = CaseAccess.can_read(principal, ticket)
        if not allowed:
            # 不向无权读取者暴露工单是否存在。
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
        principal = self.current(principal)
        if principal.role not in {"manager", "admin"}:
            raise AuthorizationError("只有经理或管理员可以审批")
        approval = self.agent.audit.get_approval(approval_id)
        if approval is None:
            raise ResourceNotFoundError()
        ticket = self.get_ticket(principal, approval["ticket_id"])
        # 历史工单没有可信归属，不能根据旧 requester 字符串自动认领。
        if not ticket.get("owner_id"):
            raise AuthorizationError("旧工单没有已验证的申请人，请通过认证接口重新提交")
        if ticket["owner_id"] == principal.user_id:
            raise AuthorizationError("不能审批自己的申请")
        required_role = approval.get("required_role")
        if not CaseAccess.can_approve(principal, ticket, required_role):
            raise AuthorizationError("审批角色不满足要求")
        return self.agent.approve(approval_id, principal.user_id, approved)

    def list_tickets(self, principal: Principal, status=None, limit=50, offset=0):
        principal = self.current(principal)
        if not 1 <= limit <= 100 or offset < 0:
            raise RequestValidationError()
        tickets = [ticket for ticket in self.agent.audit.list_tickets()
                   if CaseAccess.can_read(principal, ticket)
                   and (status is None or ticket["status"] == status)]
        return {"items": tickets[offset:offset + limit], "total": len(tickets)}

    @staticmethod
    def _text(value, max_length=2000, required=True):
        if not isinstance(value, str) or len(value) > max_length or (required and not value.strip()):
            raise RequestValidationError()
        return value.strip()

    def _operator(self, principal, ticket_id):
        principal = self.current(principal)
        if principal.role not in {"support", "manager", "admin"}:
            raise AuthorizationError("只有授权的支持人员可以处理人工待办")
        ticket = self.get_ticket(principal, ticket_id)
        if not CaseAccess.can_handle(principal, ticket):
            raise AuthorizationError("当前支持组不能处理该工单")
        if not ticket.get("owner_id"):
            raise AuthorizationError("旧工单无可信归属，请重新提交")
        if ticket["owner_id"] == principal.user_id:
            raise AuthorizationError("不能处理自己的申请，请由其他授权支持人员跟进")
        return ticket

    def cancel(self, principal, ticket_id, reason, *, case_revision=None):
        ticket = self.get_ticket(principal, ticket_id)
        if ticket.get("owner_id") != principal.user_id:
            raise AuthorizationError("只有申请人可以取消工单")
        if ticket.get("recommended_action") == "open_incident":
            self._case_revision(case_revision)
        return self.agent.cancel(ticket_id, principal.user_id, self._text(reason), case_revision=case_revision)

    def resolve(self, principal, ticket_id, outcome, note, evidence="", *, case_revision=None):
        ticket = self._operator(principal, ticket_id)
        if ticket["owner_id"] == principal.user_id:
            raise AuthorizationError("不能人工结案自己的工单")
        if outcome not in {"resolved", "rejected", "archived"}:
            raise RequestValidationError()
        if case_revision is not None:
            self._case_revision(case_revision)
        return self.agent.resolve(ticket_id, principal.user_id, outcome, self._text(note),
                                  self._text(evidence, required=False), case_revision=case_revision)

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

    def feedback(self, principal, ticket_id, revision, verdict, note, evidence, issue_resolved=None, *, case_revision=None):
        ticket = self._operator(principal, ticket_id)
        if ticket["owner_id"] == principal.user_id:
            raise AuthorizationError("不能验证自己的诊断结果")
        if (type(revision) is not int or revision < 1 or verdict not in {"accepted", "rejected", "inconclusive"}
                or issue_resolved is not None and type(issue_resolved) is not bool):
            raise RequestValidationError()
        if case_revision is not None:
            self._case_revision(case_revision)
        return self.agent.feedback(ticket_id, principal.user_id, revision, verdict, self._text(note),
                                   self._text(evidence), issue_resolved, case_revision=case_revision)

    def verify_case(self, principal, ticket_id, revision, verdict, note=""):
        ticket = self.get_ticket(principal, ticket_id)
        if ticket["owner_id"] != principal.user_id:
            raise AuthorizationError("只有申请人可以验证处理结果或重新打开工单")
        self._case_revision(revision)
        if verdict not in {"confirmed", "still_failing", "recurred"}:
            raise RequestValidationError()
        note = self._text(note, required=verdict != "confirmed")
        return self.agent.case_interactions.verify(ticket_id, principal.user_id, revision, verdict, note)

    def recover(self, principal, ticket_id):
        self._operator(principal, ticket_id)
        return self.agent.recover(ticket_id, principal.user_id)

    def retry_investigation(self, principal, ticket_id, revision):
        self._case_revision(revision)
        ticket = self._operator(principal, ticket_id)
        if ticket["owner_id"] == principal.user_id:
            raise AuthorizationError("不能处理自己的申请")
        return self.agent.investigation_jobs.retry(ticket_id, principal.user_id, revision)

    def claim(self, principal, ticket_id, revision):
        self._case_revision(revision)
        ticket = self._operator(principal, ticket_id)
        if ticket["owner_id"] == principal.user_id:
            raise AuthorizationError("不能认领自己的申请")
        return self.agent.assign_case(ticket_id, principal.user_id, revision)

    def transfer(self, principal, ticket_id, target, revision, reason):
        principal = self.current(principal)
        try:
            target = self.current(target)
        except AuthenticationError:
            raise AuthorizationError("目标处理人不存在或已停用") from None
        self._case_revision(revision)
        ticket = self._operator(principal, ticket_id)
        if (principal.user_id == ticket["owner_id"] or target.user_id == ticket["owner_id"]
                or not CaseAccess.can_handle(target, ticket)):
            raise AuthorizationError("目标处理人不能访问或处理该工单")
        return self.agent.assign_case(ticket_id, principal.user_id, revision, target=target.user_id, reason=self._text(reason), allow_override=principal.role == "admin")

    def request_info(self, principal, ticket_id, field, revision):
        self._case_revision(revision)
        self._operator(principal, ticket_id)
        from .case_management import USER_QUESTIONS
        if field not in USER_QUESTIONS:
            raise RequestValidationError()
        return self.agent.request_case_info(ticket_id, principal.user_id, field, revision)

    def follow_up(self, principal, ticket_id, answer, revision):
        self._case_revision(revision)
        ticket = self.get_ticket(principal, ticket_id)
        if ticket["owner_id"] != principal.user_id:
            raise AuthorizationError("只有申请人可以补充故障信息")
        return self.agent.follow_up(ticket_id, principal.user_id, self._text(answer, 10000), revision)

    @staticmethod
    def public_ticket(ticket):
        fields = ("id", "title", "description", "requester", "owner_id", "department", "severity", "category",
                  "status", "recommended_action", "created_at", "updated_at", "case_revision")
        return {key: ticket[key] for key in fields if key in ticket}

    def response(self, principal, state):
        """Single projection boundary shared by every legacy HTTP and CLI response."""
        from .portal import present_ticket
        principal = self.current(principal)
        raw = state.to_dict() if hasattr(state, "to_dict") else state
        submission = getattr(state, "submission_result", None)
        ticket = raw["ticket"]
        if not CaseAccess.can_read(principal, ticket):
            raise ResourceNotFoundError()
        if CaseAccess.internal(principal, ticket):
            return {**raw, **({"submission": submission} if submission else {})}
        view = present_ticket(raw)
        approval = raw.get("approval")
        return {"ticket": self.public_ticket(ticket), "user_role": raw.get("user_role", "employee"),
                **({"submission": submission} if submission else {}),
                "approval": {key: approval.get(key) for key in ("id", "status", "required_role", "created_at", "expires_at")} if approval else None,
                "clarification": {"need_clarify": bool(view["clarification"]), "question": (view["clarification"] or {}).get("question", "请补充具体情况。"),
                                  "missing_fields": (raw.get("clarification") or {}).get("missing_fields", [])} if raw.get("clarification") else None,
                "clarification_round": raw.get("clarification_round", 0),
                "confirmation": {key: (raw.get("confirmation") or {}).get(key) for key in ("status", "round")} if raw.get("confirmation") else None,
                "result": view["status"]["message"], "view": view}

    @staticmethod
    def submission_view(state):
        from .portal import present_ticket
        view = present_ticket(state.to_dict())
        if state.submission_result:
            view["submission"] = state.submission_result
        return view

    def ticket_response(self, principal, ticket):
        principal = self.current(principal)
        if not CaseAccess.can_read(principal, ticket):
            raise ResourceNotFoundError()
        return ticket if CaseAccess.internal(principal, ticket) else self.public_ticket(ticket)

    def list_response(self, principal, result):
        return {**result, "items": [self.ticket_response(principal, ticket) for ticket in result["items"]]}

    @staticmethod
    def _case_revision(revision):
        if type(revision) is not int or not 0 <= revision <= 10000:
            raise RequestValidationError()
