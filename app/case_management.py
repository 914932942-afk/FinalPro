"""Business case lifecycle, distinct from investigation execution status."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import hashlib
import json

from .models import now_iso
from .case_access import CaseAccess, GROUPS

USER_QUESTIONS = {
    "occurred_at": "问题发生在哪一天、什么时间？请提供大致日期、时间和时区，例如“2026-10-27 10:30，北京时间”，或“今天上午 10 点”。",
    "symptom": "请补充具体页面、报错内容和当前表现。请勿提供密码或验证码。",
    "reproduction": "请说明出现问题前的操作步骤，以及是否每次都能复现。",
    "previous_access": "之前是否可以正常使用，还是第一次使用这个系统？",
}


class CaseLifecycle:
    support_states = {"awaiting_support", "in_progress", "manual_review", "failed"}
    open_states = support_states | {"awaiting_user_info", "awaiting_investigation", "investigating", "awaiting_verification"}
    teams = {"vpn": "网络支持", "erp": "应用支持", "reports_db": "数据服务支持"}
    hours = {"critical": 1, "high": 4, "medium": 24, "low": 72}

    @staticmethod
    def incident(state):
        return state.ticket.recommended_action == "open_incident"

    @classmethod
    def ensure_assignment(cls, state):
        if state.ticket.assignment is None:
            created = now_iso()
            state.ticket.assignment = {
                "team": GROUPS[CaseAccess.group(state.ticket.to_dict())],
                "group_id": CaseAccess.group(state.ticket.to_dict()),
                "assignee": None, "assigned_at": None, "assigned_by": None, "revision": 0,
                "due_at": (datetime.fromisoformat(created) + timedelta(hours=cls.hours.get(state.ticket.severity, 24))).isoformat(),
                "escalated_at": None, "history": [], "paused_at": None,
            }
        state.ticket.assignment.update(group_id=CaseAccess.group(state.ticket.to_dict()),
                                       team=GROUPS[CaseAccess.group(state.ticket.to_dict())])
        return state.ticket.assignment

    @classmethod
    def handoff(cls, state):
        assignment = cls.ensure_assignment(state)
        state.ticket.status = "in_progress" if assignment["assignee"] else "awaiting_support"
        return state

    @classmethod
    def normalize(cls, state):
        if cls.incident(state) and state.ticket.status in {"closed", "resolved_manually"}:
            resolution = state.resolution or {}
            if resolution.get("outcome") == "rejected":
                return state
            if resolution.get("external_issue_resolved") is True or (state.ticket.status == "resolved_manually" and resolution.get("outcome") == "resolved" and "external_issue_resolved" not in resolution):
                state.ticket.status = "resolved"
            else:
                cls.handoff(state)
        if state.ticket.status in cls.support_states | {"awaiting_user_info"}:
            cls.ensure_assignment(state)
        return state

    @classmethod
    def pause_for_user(cls, state, settings=None):
        assignment = cls.ensure_assignment(state)
        assignment["paused_at"] = assignment.get("paused_at") or now_iso()
        state.ticket.status = "awaiting_user_info"
        cls.start_wait(state, "user_info", settings)

    @staticmethod
    def report_hash(state):
        return hashlib.sha256(json.dumps(state.ticket.diagnostic_report, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def event(state, kind, message, actor=None):
        state.ticket.case_events.append({"kind": kind, "message": message, "actor": actor, "at": now_iso()})

    @staticmethod
    def retain_resolution(state):
        if state.resolution:
            state.ticket.resolution_history.append({"resolution": deepcopy(state.resolution),
                "case_revision": state.ticket.case_revision, "report_hash": CaseLifecycle.report_hash(state),
                "assignment": deepcopy(state.ticket.assignment), "investigation_attempt": state.ticket.investigation_attempt, "at": now_iso()})

    @staticmethod
    def start_wait(state, kind, settings=None, *, started_at=None):
        started = datetime.fromisoformat(started_at or now_iso())
        ttl = getattr(settings, "case_user_reply_hours", 48) if kind == "user_info" else getattr(settings, "case_verification_hours", 72)
        reminder = getattr(settings, "case_user_reminder_hours", 24) if kind == "user_info" else getattr(settings, "case_verification_reminder_hours", 24)
        state.ticket.waiting = {"kind": kind, "started_at": started.isoformat(),
            "remind_at": (started + timedelta(hours=min(reminder, ttl / 2))).isoformat(),
            "expires_at": (started + timedelta(hours=ttl)).isoformat(), "reminded_at": None}

    @classmethod
    def propose_resolution(cls, state, actor, settings):
        cls.ensure_assignment(state)["paused_at"] = now_iso()
        state.ticket.status, state.next_node = "awaiting_verification", None
        state.resolution.update(type="awaiting_user_verification", external_issue_resolved=None, support_issue_resolved=True,
                                proposed_by=actor, proposed_at=now_iso())
        cls.start_wait(state, "verification", settings)
        cls.event(state, "verification_requested", "支持人员已提交处理结果，等待你验证是否恢复。", actor)

    @staticmethod
    def set_reopen_window(state, settings):
        state.resolution["reopen_until"] = (datetime.now(timezone.utc) + timedelta(days=settings.case_reopen_days)).isoformat()

    @staticmethod
    def can_reopen(state, settings=None):
        return CaseLifecycle.can_reopen_ticket(state.ticket.to_dict(), state.resolution or {}, settings)

    @staticmethod
    def can_reopen_ticket(ticket, resolution, settings=None):
        if ticket.get("recommended_action") != "open_incident" or ticket["status"] not in {"resolved", "archived"}:
            return False
        deadline = resolution.get("reopen_until")
        if not deadline:
            ended = resolution.get("at") or ticket["updated_at"]
            deadline = (datetime.fromisoformat(ended) + timedelta(days=getattr(settings, "case_reopen_days", 7))).isoformat()
        return datetime.now(timezone.utc) <= datetime.fromisoformat(deadline)

    @staticmethod
    def invalidate_worker(state):
        job = state.ticket.investigation_job
        if job and job.get("status") in {"queued", "running"}:
            job.update(status="cancelled", finished_at=now_iso())
            state.ticket.investigation_status = "cancelled"

    @classmethod
    def resume_clock(cls, state):
        assignment = cls.ensure_assignment(state)
        if assignment.get("paused_at"):
            now = datetime.now(timezone.utc)
            waited = now - datetime.fromisoformat(assignment["paused_at"])
            assignment["due_at"] = (datetime.fromisoformat(assignment["due_at"]) + waited).isoformat()
            assignment["paused_at"] = None

    @staticmethod
    def overdue(assignment):
        if not assignment or assignment.get("paused_at") or not assignment.get("due_at"):
            return False
        try:
            due = datetime.fromisoformat(assignment["due_at"])
            if due.tzinfo is None:
                return False  # 未知时区不能可靠判定超时，交由数据完整性检查。
            return due <= datetime.now(timezone.utc)
        except (TypeError, ValueError):
            return False
