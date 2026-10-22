"""Business case lifecycle, distinct from investigation execution status."""
from datetime import datetime, timedelta, timezone

from .models import now_iso

USER_QUESTIONS = {
    "occurred_at": "问题是什么时候开始的？请提供大致日期和时间。",
    "symptom": "请补充具体页面、报错内容和当前表现。请勿提供密码或验证码。",
    "reproduction": "请说明出现问题前的操作步骤，以及是否每次都能复现。",
    "previous_access": "之前是否可以正常使用，还是第一次使用这个系统？",
}


class CaseLifecycle:
    support_states = {"awaiting_support", "in_progress", "manual_review", "failed"}
    open_states = support_states | {"awaiting_user_info", "awaiting_investigation", "investigating"}
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
                "team": cls.teams.get((state.ticket.execution_plan or {}).get("resource"), "IT 服务支持"),
                "assignee": None, "assigned_at": None, "assigned_by": None, "revision": 0,
                "due_at": (datetime.fromisoformat(created) + timedelta(hours=cls.hours.get(state.ticket.severity, 24))).isoformat(),
                "escalated_at": None, "history": [], "paused_at": None,
            }
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
    def pause_for_user(cls, state):
        assignment = cls.ensure_assignment(state)
        assignment["paused_at"] = assignment.get("paused_at") or now_iso()
        state.ticket.status = "awaiting_user_info"

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
        return bool(assignment and not assignment.get("paused_at") and
                    datetime.fromisoformat(assignment["due_at"]) <= datetime.now(timezone.utc))
