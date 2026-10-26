"""白名单、持久化幂等与下游动作适配；默认适配器只做本地模拟。"""
from .action_adapter import ActionAdapter, SimulatedActionAdapter
from .audit import AuditStore
from .errors import ActionRejectedError, InvalidActionError, ToolResultError
from .knowledge import KnowledgeBase
from .models import ActionResult, Ticket
from .model_adapter import ALLOWED_ACTIONS
from .registration import incident_payload


class EnterpriseTools:
    def __init__(self, kb: KnowledgeBase, audit: AuditStore, adapter: ActionAdapter | None = None):
        self.kb, self.audit = kb, audit
        self.adapter = adapter or SimulatedActionAdapter()

    @staticmethod
    def action_key(ticket: Ticket, action: str) -> str:
        # 客户端键原样传给下游；无客户端键时才生成本地稳定键。
        return ticket.idempotency_key or f"tool:{ticket.id}:{action}"

    def execute_action(self, ticket: Ticket, action: str, *, guard=None) -> ActionResult:
        if action not in ALLOWED_ACTIONS:
            raise InvalidActionError()
        key = self.action_key(ticket, action)
        def perform():
            try:
                if guard:
                    guard()
                result = self._execute_action(ticket, action, idempotency_key=key)
            except ActionRejectedError as exc:
                # 明确拒绝是终态，可以缓存；未知异常保留 operations.pending。
                result = ActionResult(action, "rejected", exc.public_message, key, error=exc.info())
            if not isinstance(result, ActionResult) or result.action != action or result.idempotency_key != key:
                raise ToolResultError()
            return result.to_dict()
        payload = {"action": action, "requester": ticket.requester}
        if ticket.execution_plan:
            payload["plan"] = ticket.execution_plan
        if ticket.diagnostic_report:
            payload["diagnostic_report"] = ticket.diagnostic_report
        if action == "open_incident" and ticket.registration_context is not None:
            payload["registration"] = incident_payload(ticket)
        raw = self.audit.once(key, payload, perform, ticket_id=ticket.id, kind="tool")
        if isinstance(raw, str):
            # 旧版工具 ledger 保存字符串，读取兼容且绝不再调用下游。
            return ActionResult(action, "succeeded", raw, key, simulated=True)
        try:
            result = ActionResult.from_dict(raw)
        except (TypeError, ValueError, KeyError):
            raise ToolResultError() from None
        if result.action != action or result.idempotency_key != key:
            raise ToolResultError()
        return result

    def _execute_action(self, ticket: Ticket, action: str, *, idempotency_key: str) -> ActionResult:
        return self.adapter.execute(ticket, action, idempotency_key=idempotency_key)

    def attach_evidence(self, ticket, *, guard=None):
        key = f"evidence:{ticket.id}:v{max(ticket.investigation_attempt, 1)}"
        def perform():
            if guard:
                guard()
            result = self.adapter.attach_evidence(ticket, idempotency_key=key)
            if (not isinstance(result, ActionResult) or result.action != "attach_evidence"
                    or result.idempotency_key != key):
                raise ToolResultError()
            return result.to_dict()
        raw = self.audit.once(key, {"ticket_id": ticket.id, "report": ticket.diagnostic_report}, perform,
                              ticket_id=ticket.id, kind="tool")
        return ActionResult.from_dict(raw)
