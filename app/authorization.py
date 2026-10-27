"""Revalidate live identity and time-bound approval before new side effects."""
from datetime import datetime, timedelta, timezone

from .case_access import CaseAccess
from .case_management import CaseLifecycle
from .errors import ActionRejectedError, AuthenticationError, ConfigurationError
from .models import AgentState, now_iso
from .business_accounts import DiagnosticDataGap


class AuthorizationRevoked(ActionRejectedError):
    code, category = "authorization_revoked", "authorization"
    public_message = "申请身份或审批条件已失效，已阻止执行，请联系支持人员核查后重新申请。"


class ExecutionAuthorization:
    def __init__(self, agent, identities):
        self.agent, self.identities = agent, identities

    def requester(self, state):
        try:
            owner = self.identities.principal(state.ticket.owner_id)
        except (AuthenticationError, ConfigurationError):
            raise AuthorizationRevoked() from None
        if owner.department != state.ticket.department:
            raise AuthorizationRevoked()
        return owner

    def stamp(self, state):
        owner = self.requester(state)
        self.check_account(state)
        created = datetime.now(timezone.utc)
        state.approval.created_at = created.isoformat()
        state.approval.expires_at = (created + timedelta(hours=self.agent.settings.approval_ttl_hours)).isoformat()
        state.approval.requester_revision = self.identities.identity_revision(owner)

    def check(self, state, *, approver=None, executing=False):
        # Trusted in-process legacy demos have no verified owner; public entry points require one.
        if not state.ticket.owner_id:
            return
        owner = self.requester(state)
        self.check_account(state)
        if state.ticket.recommended_action != "grant_access":
            return
        approval = state.approval
        try:
            if not approval or not approval.created_at or not approval.expires_at:
                raise ValueError()
            if datetime.fromisoformat(approval.expires_at) <= datetime.now(timezone.utc):
                raise ValueError()
            if approval.requester_revision != self.identities.identity_revision(owner):
                raise ValueError()
            actor = self.identities.principal(approver or approval.decided_by)
            if not CaseAccess.can_approve(actor, state.ticket.to_dict(), approval.required_role):
                raise ValueError()
            if executing and (approval.status != "approved" or
                              approval.approver_revision != self.identities.identity_revision(actor)):
                raise ValueError()
        except (ValueError, TypeError, AuthenticationError, ConfigurationError):
            raise AuthorizationRevoked() from None
        return actor

    def check_account(self, state):
        scope = state.ticket.account_binding
        if scope is None:
            if self.agent.account_directory and state.ticket.recommended_action in {"grant_access", "reset_password"}:
                raise AuthorizationRevoked()
            return
        if (scope.get("owner_id") != state.ticket.owner_id
                or scope.get("resource") != (state.ticket.execution_plan or {}).get("resource")):
            raise AuthorizationRevoked()
        if state.ticket.recommended_action in {"grant_access", "reset_password"}:
            try:
                self.agent.account_directory.validate(scope, write=True)
            except DiagnosticDataGap:
                raise AuthorizationRevoked() from None
            if state.approval and state.approval.account_binding != scope:
                raise AuthorizationRevoked()
            if (state.confirmation or {}).get("status") == "confirmed" and state.confirmation.get("account_binding") != scope:
                raise AuthorizationRevoked()
        elif scope.get("code") in {"ready", "business_account_disabled"}:
            try:
                self.agent.account_directory.validate(scope)
            except DiagnosticDataGap:
                raise AuthorizationRevoked() from None

    def invalidate(self, state):
        if state.approval:
            state.approval.status = "invalidated"
        self.agent._manual(state, AuthorizationRevoked.public_message)
        state.next_node = None
        state.ticket.case_revision += 1
        self.agent._persist(state, "authorization_invalidated", {"at": now_iso()})

    def maintain(self):
        for ticket in self.agent.audit.list_tickets():
            if ticket["status"] in CaseLifecycle.open_states and (ticket.get("assignment") or {}).get("assignee"):
                self._release_ineligible(ticket["id"])
            if ticket["status"] != "awaiting_approval":
                continue
            with self.agent.audit.ticket_lock(ticket["id"]):
                state = AgentState.from_dict(self.agent.audit.get_state(ticket["id"]))
                if state.ticket.status != "awaiting_approval" or not state.ticket.owner_id:
                    continue
                # Before a decision, there is no approver yet: validate owner and approval lifetime.
                try:
                    owner = self.requester(state)
                    self.check_account(state)
                    approval = state.approval
                    if (not approval or not approval.created_at or not approval.expires_at
                            or datetime.fromisoformat(approval.expires_at) <= datetime.now(timezone.utc)
                            or approval.requester_revision != self.identities.identity_revision(owner)):
                        raise AuthorizationRevoked()
                except (AuthorizationRevoked, ValueError, TypeError):
                    self.invalidate(state)

    def _release_ineligible(self, ticket_id):
        with self.agent.audit.ticket_lock(ticket_id):
            state = AgentState.from_dict(self.agent.audit.get_state(ticket_id))
            assignment = state.ticket.assignment or {}
            assignee = assignment.get("assignee")
            if not assignee or state.ticket.status not in CaseLifecycle.open_states:
                return
            try:
                actor = self.identities.principal(assignee)
                if CaseAccess.can_handle(actor, state.ticket.to_dict()) and actor.user_id != state.ticket.owner_id:
                    return
            except AuthenticationError:
                pass
            # An unreadable identity file must not trigger mass reassignment.
            assignment.update(assignee=None, assigned_at=None, assigned_by=None, revision=assignment["revision"] + 1)
            assignment["history"].append({"action": "release", "from": assignee, "to": None,
                                          "actor": "system", "at": now_iso(), "reason": "处理权限已变更"})
            if state.ticket.status == "in_progress":
                state.ticket.status = "awaiting_support"
            state.ticket.case_revision += 1
            self.agent._persist(state, "assignment_released")
