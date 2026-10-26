"""Real HTTP transport to the demo incident service; IAM actions remain explicitly simulated."""
import httpx
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .action_adapter import SimulatedActionAdapter
from .errors import ActionRejectedError, IdempotencyConflict, ToolResultError, ToolTimeoutError, ToolUnavailableError
from .models import ActionResult
from .registration import incident_payload


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["open_incident", "attach_evidence"]
    status: Literal["succeeded"]
    idempotency_key: str
    ticket_id: str
    downstream_id: str = Field(min_length=1)
    simulated: Literal[True]


class IncidentHTTPAdapter:
    def __init__(self, base_url, api_key, *, timeout=5):
        self.base_url, self.api_key, self.timeout = base_url.rstrip("/"), api_key, timeout
        self.local = SimulatedActionAdapter()

    def _request(self, method, path, *, payload=None, key=None):
        headers = {"Authorization": "Bearer " + self.api_key}
        if key:
            headers["Idempotency-Key"] = key
        try:
            # No write retries or redirects: an unexpected response cannot establish non-execution.
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                response = client.request(method, self.base_url + path, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise ToolTimeoutError() from None
        except httpx.HTTPError:
            raise ToolUnavailableError() from None
        if response.status_code == 409:
            raise IdempotencyConflict("下游幂等键内容冲突，请核查原操作")
        if response.status_code in {400, 401, 403, 404, 422}:
            raise ActionRejectedError()
        if response.status_code not in {200, 201}:
            raise ToolUnavailableError()
        try:
            if len(response.content) > 65536:
                raise ValueError()
            return response.json()
        except ValueError:
            raise ToolResultError() from None

    def _write(self, ticket, action, path, payload, key):
        raw = self._request("POST", path, payload=payload, key=key)
        try:
            receipt = Receipt.model_validate(raw)
            if (receipt.ticket_id != ticket.id or receipt.action != action or receipt.idempotency_key != key):
                raise ValueError()
        except ValueError:
            raise ToolResultError() from None
        message = "已通过 HTTP 在演示服务持久化登记事件。" if action == "open_incident" else "已通过 HTTP 持久化调查附件，待人工核查。"
        return ActionResult(action, "succeeded", message, key, downstream_id=receipt.downstream_id, simulated=True)

    def execute(self, ticket, action, *, idempotency_key):
        if action != "open_incident":
            return self.local.execute(ticket, action, idempotency_key=idempotency_key)
        return self._write(ticket, action, "/incidents", incident_payload(ticket), idempotency_key)

    def attach_evidence(self, ticket, *, idempotency_key):
        return self._write(ticket, "attach_evidence", "/attachments", {
            "ticket_id": ticket.id, "report": ticket.diagnostic_report,
            "version": max(ticket.investigation_attempt, 1),
        }, idempotency_key)

    def lookup(self, key):
        from urllib.parse import quote
        return self._request("GET", "/operations/" + quote(key, safe=""))
