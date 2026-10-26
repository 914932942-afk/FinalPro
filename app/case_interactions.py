"""Applicant verification, reopening and bounded waits without replaying actions."""
from datetime import datetime, timedelta, timezone

from .case_management import CaseLifecycle
from .errors import IdempotencyConflict, OperationPending
from .models import AgentState, now_iso


class CaseInteractions:
    def __init__(self, agent):
        self.agent, self.audit = agent, agent.audit

    def verify(self, ticket_id, actor, revision, verdict, note):
        with self.audit.ticket_lock(ticket_id):
            state = AgentState.from_dict(self.audit.get_state(ticket_id))
            previous_revision, previous_status = state.ticket.case_revision, state.ticket.status
            can_reopen = CaseLifecycle.can_reopen(state, self.agent.settings)
            def validate():
                if actor != state.ticket.owner_id or not CaseLifecycle.incident(state) or revision != previous_revision:
                    raise IdempotencyConflict("工单已有更新，请刷新后提交验证结果")
                if verdict == "confirmed" and previous_status != "awaiting_verification":
                    if (previous_status not in CaseLifecycle.open_states or not state.action_result or state.action_result.status != "succeeded"
                            or any(row["kind"] == "tool" for row in self.audit.pending_operations(ticket_id))):
                        raise IdempotencyConflict("当前不能确认恢复，仍有操作需要核查")
                if verdict == "still_failing" and previous_status != "awaiting_verification" and not can_reopen:
                    raise IdempotencyConflict("当前不能反馈未恢复；如已过重开期限，请新建并注明原工单编号")
                if verdict == "recurred" and not can_reopen:
                    raise IdempotencyConflict("当前不能重新打开；如已过重开期限，请新建并注明原工单编号")
            CaseLifecycle.retain_resolution(state)
            CaseLifecycle.resume_clock(state)
            CaseLifecycle.invalidate_worker(state)
            state.ticket.waiting, state.clarification, state.next_node = None, None, None
            state.conversation_history.append({"role": "user", "kind": "case_verification",
                "content": note or "我已验证，问题恢复。", "at": now_iso()})
            state.resolution = {"type": "user_confirmed" if verdict == "confirmed" else "user_reopened", "by": actor,
                "at": now_iso(), "note": note, "external_issue_resolved": verdict == "confirmed",
                "report_hash": CaseLifecycle.report_hash(state)}
            if verdict == "confirmed":
                state.ticket.status = "resolved"
                CaseLifecycle.set_reopen_window(state, self.agent.settings)
                CaseLifecycle.event(state, "user_confirmed", "你已验证并确认问题恢复，工单已结束。", actor)
            else:
                CaseLifecycle.handoff(state)
                if previous_status in {"resolved", "archived"}:
                    state.ticket.assignment.update(paused_at=None, escalated_at=None,
                        due_at=(datetime.now(timezone.utc) + timedelta(hours=CaseLifecycle.hours.get(state.ticket.severity, 24))).isoformat())
                CaseLifecycle.event(state, "user_reopened", "你反馈问题仍然存在，已在原工单继续跟进。", actor)
            state.ticket.case_revision += 1
            checkpoint = self.agent._checkpoint(state, "applicant_verification", {"actor": actor, "verdict": verdict})
            return AgentState.from_dict(self.audit.once(f"case-verification:{ticket_id}:{revision}",
                {"actor": actor, "verdict": verdict, "note": note}, state.to_dict,
                precondition=validate, ticket_id=ticket_id, checkpoint=checkpoint))

    def maintain(self):
        now = datetime.now(timezone.utc)
        for ticket in self.audit.list_tickets():
            if ticket["status"] not in {"awaiting_user_info", "awaiting_verification"}:
                continue
            try:
                with self.audit.ticket_lock(ticket["id"]):
                    state = AgentState.from_dict(self.audit.get_state(ticket["id"]))
                    if state.ticket.status not in {"awaiting_user_info", "awaiting_verification"}:
                        continue
                    if not state.ticket.waiting:
                        assignment = CaseLifecycle.ensure_assignment(state)
                        started = assignment.get("paused_at") or state.ticket.updated_at
                        assignment["paused_at"] = started
                        CaseLifecycle.start_wait(state, "user_info" if state.ticket.status == "awaiting_user_info" else "verification",
                                                 self.agent.settings, started_at=started)
                        self.agent._persist(state, "case_wait_initialized")
                    waiting = state.ticket.waiting
                    if now >= datetime.fromisoformat(waiting["expires_at"]):
                        kind = waiting["kind"]
                        CaseLifecycle.resume_clock(state)
                        state.ticket.waiting = None
                        state.ticket.case_revision += 1
                        if kind == "user_info":
                            state.next_node = (state.clarification or {}).get("resume_node")
                            state.clarification = None
                            CaseLifecycle.handoff(state)
                            state.ticket.assignment["attention_reason"] = "user_no_response"
                            CaseLifecycle.event(state, "reply_expired", "补充信息期限已过，已交由支持人员继续跟进。")
                        elif any(row["kind"] == "tool" for row in self.audit.pending_operations(state.ticket.id)):
                            CaseLifecycle.handoff(state)
                            state.ticket.assignment["attention_reason"] = "unknown_operation"
                            CaseLifecycle.event(state, "verification_expired", "验证期限已过，仍有操作需要支持人员核查。")
                        else:
                            CaseLifecycle.retain_resolution(state)
                            state.ticket.status, state.next_node = "archived", None
                            state.resolution = {"type": "verification_timeout", "at": now_iso(), "external_issue_resolved": None}
                            CaseLifecycle.set_reopen_window(state, self.agent.settings)
                            CaseLifecycle.event(state, "verification_expired", "验证期限已过，工单已归档，尚未确认问题解决。")
                        self.agent._persist(state, "case_wait_expired", {"kind": kind})
                    elif not waiting.get("reminded_at") and now >= datetime.fromisoformat(waiting["remind_at"]):
                        waiting["reminded_at"] = now_iso()
                        CaseLifecycle.event(state, "wait_reminder", "请在期限内补充故障信息。" if waiting["kind"] == "user_info" else "请在期限内验证处理结果是否恢复。")
                        self.agent._persist(state, "case_wait_reminder", {"kind": waiting["kind"]})
            except OperationPending:
                continue
