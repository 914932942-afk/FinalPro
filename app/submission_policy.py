"""Conservative business deduplication; lifecycle decisions remain explicit."""
from datetime import datetime, timezone
import hashlib
import json


class SubmissionPolicy:
    active = {"new", "triaged", "planned", "authorized", "executed", "recovering", "failed", "manual_review",
              "awaiting_clarification", "awaiting_approval", "awaiting_support", "in_progress", "awaiting_user_info",
              "awaiting_investigation", "investigating", "awaiting_verification"}

    @staticmethod
    def fingerprint(ticket):
        # Collapse formatting whitespace only: identifiers, URLs and error-code case remain meaningful.
        payload = {key: " ".join(ticket[key].split()) for key in ("title", "description")}
        payload.update(owner_id=ticket["owner_id"], department=ticket["department"], severity=ticket["severity"])
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def reuse_reason(cls, state, completed_window, *, now=None, account_context=None):
        ticket = state["ticket"]
        if (state.get("approval") or {}).get("status") == "invalidated":
            return None  # A blocked authorization must allow a fresh, independently approved application.
        scope = ticket.get("account_binding") or {}
        current = (account_context or {}).get(scope.get("resource"), {})
        if (ticket["status"] == "manual_review" and ticket.get("recommended_action") in {"grant_access", "reset_password"}
                and scope.get("code") not in {None, "ready"} and current.get("code") == "ready"
                and current.get("revision") != scope.get("revision")):
            return None  # A repaired mapping requires a new plan/approval, not a silent target replacement.
        if ticket["status"] in cls.active:
            return "active_case"
        result = state.get("action_result") or {}
        resolution = state.get("resolution") or {}
        if ticket["status"] == "closed" and ticket.get("recommended_action") == "open_incident" and resolution.get("external_issue_resolved") is not True:
            return "active_case"  # Legacy registration was not a verified fault resolution.
        if (ticket["status"] != "closed" or result.get("status") != "succeeded"
                or ticket.get("recommended_action") not in {"reset_password", "grant_access"} or completed_window <= 0):
            return None
        try:
            completed = datetime.fromisoformat(resolution.get("at") or ticket["updated_at"])
            age = ((now or datetime.now(timezone.utc)) - completed).total_seconds()
            return "recent_completion" if 0 <= age <= completed_window else None
        except (ValueError, TypeError):
            return None
