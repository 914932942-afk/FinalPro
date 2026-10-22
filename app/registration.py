"""Versioned user evidence frozen before the first incident registration."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .policy import ExecutionPlan, saved_understanding


class ClarificationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    round_number: int = Field(ge=1, le=10)
    answer: str = Field(min_length=1, max_length=10000)


class RegistrationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    effective_description: str = Field(min_length=1, max_length=120000)
    clarifications: list[ClarificationAnswer] = Field(max_length=10)
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @classmethod
    def from_state(cls, state):
        understanding = saved_understanding(state)
        return cls(
            effective_description=understanding.text,
            clarifications=[ClarificationAnswer(round_number=row["round"], answer=row["content"])
                for row in state.conversation_history
                if row.get("role") == "user" and row.get("kind") not in {"plan_confirmation", "diagnostic_followup"}],
            input_digest=understanding.input_digest,
            plan_digest=ExecutionPlan.model_validate(state.ticket.execution_plan).digest(),
        )


def incident_payload(ticket):
    """Shared wire payload and ledger fingerprint; legacy calls retain their exact shape."""
    payload = {
        "ticket_id": ticket.id, "owner": ticket.owner_id or ticket.requester,
        "resource": (ticket.execution_plan or {}).get("resource", "unknown"),
        "title": ticket.title, "description": ticket.description, "severity": ticket.severity,
    }
    if ticket.registration_context is not None:
        payload["registration_context"] = RegistrationContext.model_validate(ticket.registration_context).model_dump()
    return payload
