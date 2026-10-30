"""Aggregate the local case lifecycle without exporting applicant or log content."""
from collections import Counter
from datetime import datetime, timezone
from statistics import median

from .case_management import CaseLifecycle
from .portal import support_work_items


def _timestamp(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def summarize_cases(states, *, now=None):
    """Return counts and safety signals for persisted case snapshots, never raw records."""
    now = now or datetime.now(timezone.utc)
    statuses = Counter()
    counts = Counter()
    assignment_minutes = []
    confirmed_hours = []
    for state in states:
        if not state:
            continue
        ticket = state["ticket"]
        status = ticket["status"]
        statuses[status] += 1
        if ticket.get("recommended_action") != "open_incident":
            continue
        counts["incidents"] += 1
        report = ticket.get("diagnostic_report") or {}
        assignment = ticket.get("assignment") or {}
        resolution = state.get("resolution") or {}
        waiting = ticket.get("waiting") or {}
        counts["diagnosed"] += bool(report)
        counts["supported_hypotheses"] += report.get("assessment") == "supported_hypothesis"
        counts["model_interruptions"] += str(report.get("stop_reason", "")).startswith("model_error:")
        counts["manual_work_records"] += len(ticket.get("work_log", []))
        counts["support_feedback_records"] += len(ticket.get("feedback", []))
        counts["reopen_events"] += sum(row.get("kind") == "user_reopened" for row in ticket.get("case_events", []))
        if status in CaseLifecycle.open_states:
            counts["active_incidents"] += 1
        if status in CaseLifecycle.support_states:
            counts["awaiting_or_in_support"] += 1
            counts["unassigned_support"] += not assignment.get("assignee")
            counts["missing_support_group"] += not assignment.get("group_id")
            counts["missing_support_deadline"] += not _timestamp(assignment.get("due_at"))
            counts["overdue_support"] += CaseLifecycle.overdue(assignment)
            counts["attention_support"] += support_work_items(state)[1]
        if status in {"awaiting_user_info", "awaiting_verification"}:
            expires = _timestamp(waiting.get("expires_at"))
            counts["expired_waits"] += bool(expires and expires <= now)
        claimed_resolved = status == "resolved" or (status in {"resolved_manually", "closed"}
                                                    and resolution.get("external_issue_resolved") is True)
        if claimed_resolved:
            verified = resolution.get("type") == "user_confirmed" and resolution.get("external_issue_resolved") is True
            counts["user_verified_resolutions"] += verified
            counts["resolved_without_user_verification"] += not verified
            created, ended = _timestamp(ticket.get("created_at")), _timestamp(resolution.get("at"))
            if verified and created and ended and ended >= created:
                confirmed_hours.append((ended - created).total_seconds() / 3600)
        if status == "archived":
            counts["archives_claiming_resolution"] += resolution.get("external_issue_resolved") is True
        created, assigned = _timestamp(ticket.get("created_at")), _timestamp(assignment.get("assigned_at"))
        if created and assigned and assigned >= created:
            assignment_minutes.append((assigned - created).total_seconds() / 60)
    metrics = {key: counts[key] for key in (
        "incidents", "active_incidents", "diagnosed", "supported_hypotheses", "model_interruptions",
        "manual_work_records", "support_feedback_records", "reopen_events", "user_verified_resolutions",
        "awaiting_or_in_support", "unassigned_support", "missing_support_group", "missing_support_deadline", "overdue_support",
        "attention_support", "expired_waits", "resolved_without_user_verification", "archives_claiming_resolution")}
    return {"cases_total": sum(statuses.values()), "statuses": dict(sorted(statuses.items())),
            "metrics": metrics,
            "median_minutes_to_assignment": round(median(assignment_minutes), 2) if assignment_minutes else None,
            "median_hours_to_user_confirmation": round(median(confirmed_hours), 2) if confirmed_hours else None}
