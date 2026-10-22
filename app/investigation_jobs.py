"""Durable investigation leases and bounded, process-local background workers."""
from contextlib import ExitStack
from queue import Queue, Empty, Full
from threading import Event, Lock, Thread
import logging
import time
from uuid import uuid4

from .case_management import CaseLifecycle
from .errors import IdempotencyConflict, OperationPending
from .models import AgentState, now_iso


class InvestigationSuperseded(RuntimeError):
    """The job timed out or another generation now owns its checkpoint."""


class InvestigationJobs:
    def __init__(self, agent):
        self.agent = agent
        self.audit = agent.audit

    @staticmethod
    def queue(state):
        previous = state.ticket.investigation_job or {}
        attempt = max(state.ticket.investigation_attempt, 1)
        state.ticket.investigation_job = {
            "token": uuid4().hex, "status": "queued", "queued_at": time.time(),
            "attempt": attempt, "runs": previous.get("runs", 0) if previous.get("attempt") == attempt else 0,
            "phase": state.next_node if state.next_node in {"diagnose", "enrich"} else "diagnose", "reason": None,
        }

    def _state(self, ticket_id):
        raw = self.audit.get_state(ticket_id)
        return AgentState.from_dict(raw) if raw else None

    def _fail(self, state, reason):
        job = state.ticket.investigation_job or {"attempt": max(state.ticket.investigation_attempt, 1), "phase": state.next_node}
        runs = job.get("runs", 0) + (1 if job.get("status") == "queued" else 0)
        job.update(status="failed", reason=reason, finished_at=now_iso(), runs=max(runs, 1),
                   max_runs=self.agent.settings.diagnosis_worker_max_runs)
        job["exhausted"] = job["runs"] >= self.agent.settings.diagnosis_worker_max_runs
        if job["exhausted"]:
            job["escalated_at"] = now_iso()
        state.ticket.investigation_job = job
        CaseLifecycle.handoff(state)
        state.ticket.assignment["attention_reason"] = "investigation_retry_exhausted" if job["exhausted"] else "investigation_failed"
        pending = any(row["kind"] == "tool" for row in self.audit.pending_operations(state.ticket.id))
        state.ticket.investigation_status = "attachment_unknown" if pending else "failed"
        state.degradation.append({"component": "investigation_worker", "reason": reason})
        self.agent._persist(state, "investigation_worker_failed", {"reason": reason, "runs": job["runs"], "escalated": job["exhausted"]})
        return state

    def fail(self, ticket_id, reason, *, token=None):
        with self.audit.ticket_lock(ticket_id):
            state = self._state(ticket_id)
            if not state or state.ticket.status not in {"awaiting_investigation", "investigating"}:
                return state
            job = state.ticket.investigation_job or {}
            if token is not None and job.get("token") != token:
                return state
            return self._fail(state, reason)

    def _check(self, ticket_id, token):
        state = self._state(ticket_id)
        job = (state.ticket.investigation_job or {}) if state else {}
        if job.get("token") != token or job.get("status") != "running":
            raise InvestigationSuperseded()
        if time.time() >= job["deadline_at"]:
            self._fail(state, "worker_timeout")
            raise InvestigationSuperseded()
        return state

    def _commit(self, state, token, node, detail=None):
        for attempt in range(6):
            try:
                with self.audit.ticket_lock(state.ticket.id):
                    self._check(state.ticket.id, token)
                    self.agent._persist(state, node, detail)
                return
            except OperationPending:
                if attempt == 5:
                    raise
                time.sleep(.01)

    def run(self, ticket_id, *, revision=None):
        with ExitStack() as locks:
            with self.audit.ticket_lock(ticket_id):
                state = self._state(ticket_id)
                if not state or state.next_node not in {"diagnose", "enrich"}:
                    return state
                if revision is not None and revision != state.ticket.case_revision:
                    return state
                if state.workflow_version != 2 or not state.action_result or state.action_result.status != "succeeded":
                    raise IdempotencyConflict("必须先核查事件登记结果，不能直接继续旧版调查")
                job = state.ticket.investigation_job or {}
                if job.get("status") == "failed":
                    return state  # Staff must explicitly queue a bounded retry.
                if job.get("status") == "running":
                    if time.time() >= job["deadline_at"]:
                        return self._fail(state, "worker_timeout")
                    try:
                        with self.audit.ticket_lock("investigation-worker:" + job["token"]):
                            pass  # OS released the crashed worker's lock.
                    except OperationPending:
                        return state
                runs = job.get("runs", 0)
                if runs >= self.agent.settings.diagnosis_worker_max_runs:
                    return self._fail(state, "retry_budget")
                token = uuid4().hex
                locks.enter_context(self.audit.ticket_lock("investigation-worker:" + token))
                state.ticket.investigation_attempt = max(state.ticket.investigation_attempt, 1)
                state.ticket.investigation_job = {**job, "token": token, "status": "running", "runs": runs + 1,
                    "attempt": state.ticket.investigation_attempt, "phase": state.next_node,
                    "started_at": now_iso(), "deadline_at": time.time() + self.agent.settings.diagnosis_worker_timeout}
                state.ticket.status, state.ticket.investigation_status = "investigating", "running"
                self.agent._persist(state, "investigation_started")
            def persist(current, node, detail=None):
                self._commit(current, token, node, detail)
            def guard():
                with self.audit.ticket_lock(ticket_id):
                    self._check(ticket_id, token)
            try:
                if state.next_node == "diagnose":
                    state = self.agent._diagnose(state, persist=persist)
                    paused = state.ticket.status in self.agent.WAITING | self.agent.TERMINAL | self.agent.CASE_WAITING
                    state.next_node = None if paused else "enrich"
                    state.ticket.investigation_job["phase"] = state.next_node or "diagnose"
                    if paused:
                        state.ticket.investigation_job.update(status="completed", finished_at=now_iso())
                    persist(state, "diagnose")
                if state.next_node == "enrich":
                    guard()
                    state = self.agent._enrich(state, guard=guard)
                    state.next_node = None
                    state.ticket.investigation_job.update(status="completed", finished_at=now_iso())
                    persist(state, "enrich")
                return state
            except InvestigationSuperseded:
                return self._state(ticket_id)
            except Exception as exc:
                return self.fail(ticket_id, type(exc).__name__, token=token)

    def expire(self):
        now = time.time()
        for ticket in self.audit.list_tickets():
            if ticket.get("investigation_status") not in {"queued", "running"}:
                continue
            try:
                with self.audit.ticket_lock(ticket["id"]):
                    state = self._state(ticket["id"])
                    if state.ticket.investigation_status not in {"queued", "running"}:
                        continue
                    job = state.ticket.investigation_job or {}
                    if state.ticket.investigation_status == "running":
                        deadline = job.get("deadline_at")
                        if deadline is None:
                            from datetime import datetime
                            deadline = datetime.fromisoformat(state.ticket.updated_at).timestamp() + self.agent.settings.diagnosis_worker_timeout
                        if now >= deadline:
                            self._fail(state, "worker_timeout")
                        elif job.get("token"):
                            try:
                                with self.audit.ticket_lock("investigation-worker:" + job["token"]):
                                    self._fail(state, "worker_interrupted")
                            except OperationPending:
                                pass
                    else:
                        from datetime import datetime
                        queued = job.get("queued_at", datetime.fromisoformat(state.ticket.updated_at).timestamp())
                        if now - queued >= self.agent.settings.diagnosis_queue_timeout:
                            self._fail(state, "queue_timeout")
            except OperationPending:
                continue

    def retry(self, ticket_id, actor, revision):
        with self.audit.ticket_lock(ticket_id):
            state = self._state(ticket_id)
            job = state.ticket.investigation_job or {}
            previous_revision, previous_status = state.ticket.case_revision, state.ticket.status
            def validate():
                self.agent._require_assignee(state, actor)
                if (previous_revision != revision or previous_status not in CaseLifecycle.support_states
                        or job.get("status") != "failed" or state.next_node != "diagnose"
                        or job.get("runs", 0) >= self.agent.settings.diagnosis_worker_max_runs
                        or any(row["kind"] == "tool" for row in self.audit.pending_operations(ticket_id))):
                    raise IdempotencyConflict("当前不能重新排查，请核查工单状态、剩余次数与未知操作")
            self.queue(state)
            if state.diagnosis and state.diagnosis.get("status") == "running":
                state.diagnosis["deadline_at"] = time.time() + self.agent.settings.diagnosis_deadline
            state.ticket.case_revision += 1
            state.ticket.status, state.ticket.investigation_status = "awaiting_investigation", "queued"
            state.error, state.error_info = None, None
            checkpoint = self.agent._checkpoint(state, "investigation_retry_queued", {"actor": actor, "revision": revision})
            return AgentState.from_dict(self.audit.once(f"investigation-retry:{ticket_id}:{revision}", {"actor": actor},
                state.to_dict, precondition=validate, ticket_id=ticket_id, checkpoint=checkpoint))


class InvestigationSupervisor:
    """Bounded daemon workers: a blocked dependency cannot keep shutdown or the portal waiting."""
    def __init__(self, agent):
        self.agent = agent
        self.stopping = Event()
        self.queue = Queue(maxsize=100)
        self.pending = set()
        self.lock = Lock()
        self.threads = []

    def start(self):
        if self.agent.settings.diagnosis_background:
            for number in range(self.agent.settings.diagnosis_workers):
                thread = Thread(target=self._worker, name=f"investigation-{number}", daemon=True)
                thread.start()
                self.threads.append(thread)
            for ticket in self.agent.audit.list_tickets():
                if ticket.get("investigation_status") in {"queued", "running"}:
                    self.submit(ticket["id"], ticket.get("case_revision", 0))

    def submit(self, ticket_id, revision):
        key = (ticket_id, revision)
        with self.lock:
            if self.stopping.is_set() or key in self.pending:
                return
            self.pending.add(key)
            try:
                self.queue.put_nowait(key)
            except Full:
                self.pending.discard(key)
                try:
                    self.agent.fail_investigation(ticket_id, "queue_full")
                except OperationPending:
                    pass

    def _worker(self):
        while not self.stopping.is_set():
            try:
                key = self.queue.get(timeout=.2)
            except Empty:
                continue
            if key is None:
                break
            try:
                self.agent.investigate(key[0], revision=key[1])
            except OperationPending:
                pass  # The monitor resubmits queued work; contention is not a job failure.
            except Exception as exc:
                logging.getLogger(__name__).warning("Investigation worker stopped: %s", type(exc).__name__)
                try:
                    self.agent.fail_investigation(key[0], type(exc).__name__)
                except OperationPending:
                    pass
            finally:
                with self.lock:
                    self.pending.discard(key)
                self.queue.task_done()

    def scan(self):
        self.agent.investigation_jobs.expire()
        if self.agent.settings.diagnosis_background:
            self.threads = [thread for thread in self.threads if thread.is_alive()]
            while len(self.threads) < self.agent.settings.diagnosis_workers and not self.stopping.is_set():
                thread = Thread(target=self._worker, name="investigation-replacement", daemon=True)
                thread.start()
                self.threads.append(thread)
            for ticket in self.agent.audit.list_tickets():
                if ticket.get("investigation_status") == "queued":
                    self.submit(ticket["id"], ticket.get("case_revision", 0))

    def close(self):
        self.stopping.set()
        with self.lock:
            active = list(self.pending)
        for ticket_id, revision in active:
            try:
                state = self.agent.audit.get_state(ticket_id)
                job = (state or {}).get("ticket", {}).get("investigation_job") or {}
                if job.get("status") == "running" and state["ticket"].get("case_revision", 0) == revision:
                    self.agent.investigation_jobs.fail(ticket_id, "service_stopping", token=job["token"])
            except OperationPending:
                pass
            except Exception as exc:
                logging.getLogger(__name__).warning("Investigation shutdown checkpoint failed: %s", type(exc).__name__)
        for _ in self.threads:
            try:
                self.queue.put_nowait(None)
            except Full:
                break
        for thread in self.threads:
            thread.join(timeout=.1)
