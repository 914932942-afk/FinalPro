"""受限 Agent 工作流：证据检查、计划绑定、原子快照及人工恢复。"""
from dataclasses import replace
from threading import RLock
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

from .audit import AuditStore
from .clarification import ClarificationDecision
from .planning import PlanningDecision, validate_choice, previous_access
from .config import get_settings
from .errors import describe_error, IdempotencyConflict, ModelOutputError, OperationPending
from .model_adapter import DecisionModel, RuleBasedModel, ResilientDecisionModel, DeepSeekDecisionModel
from .models import ActionResult, AgentState, Approval, Ticket, now_iso
from .policy import (analyze_request, build_plan, decision_problem, validate_execution, ExecutionPlan,
                     request_constraints, from_proposal, saved_understanding, input_digest, resource_catalog)
from .reliability import CircuitBreaker
from .tools import EnterpriseTools

class GraphState(TypedDict):
    state: dict

class EnterpriseAgent:
    HIGH_RISK = {"grant_access"}
    NODE_ORDER = ("intake", "clarify", "retrieve", "plan", "authorize", "execute", "finalize", "diagnose", "enrich")
    WAITING = {"awaiting_clarification", "awaiting_approval"}
    TERMINAL = {"closed", "manual_review", "rejected", "cancelled", "resolved_manually"}

    def __init__(self, tools: EnterpriseTools, audit: AuditStore,
                 model: DecisionModel | None = None, strict_model: bool = False, *, settings=None, diagnostic_runner=None):
        self.settings = settings or get_settings()
        self.tools, self.audit = tools, audit
        self.diagnostic_runner = diagnostic_runner
        self.investigation_runners = {}
        self.investigation_knowledge = None
        self._investigation_lock = RLock()
        self.model = model or RuleBasedModel()
        if not isinstance(self.model, (RuleBasedModel, ResilientDecisionModel)):
            self.model = ResilientDecisionModel(self.model, strict=strict_model,
                breaker=CircuitBreaker(self.settings.llm_failure_threshold, self.settings.llm_recovery_timeout))
        primary = getattr(self.model, "primary", self.model)
        if hasattr(primary, "planning_use_sop") or isinstance(primary, DeepSeekDecisionModel):
            primary.planning_use_sop = self.settings.planning_use_sop
        self.fallback_model = RuleBasedModel()
        self.strict_model = strict_model
        if strict_model and isinstance(self.model, ResilientDecisionModel):
            self.model.strict = True
        self.tool_breaker = CircuitBreaker(self.settings.tool_failure_threshold, self.settings.tool_recovery_timeout)
        self.nodes = {name: getattr(self, "_" + name) for name in self.NODE_ORDER}

    @property
    def planning_enabled(self):
        primary = getattr(self.model, "primary", self.model)
        if (isinstance(primary, RuleBasedModel) and type(primary).decide is not RuleBasedModel.decide
                and type(primary).plan is RuleBasedModel.plan):
            return False
        return bool(getattr(self.model, "supports_planning", False))

    def _build_graph(self, names):
        builder = StateGraph(GraphState)
        for name in names:
            builder.add_node(name, self._node(name))
        builder.add_edge(START, names[0])
        for index, name in enumerate(names):
            next_node = names[index + 1] if index + 1 < len(names) else END
            def route(raw, target=next_node):
                state = raw["state"]
                if state["error"] or state["ticket"]["status"] in self.WAITING | self.TERMINAL | {"awaiting_investigation"}:
                    return END
                return target
            builder.add_conditional_edges(name, route)
        return builder.compile()

    def _persist(self, state, node, detail=None, *, reconcile=False, resolve_tools=False):
        checkpoint = self._checkpoint(state, node, detail, reconcile=reconcile, resolve_tools=resolve_tools)
        self.audit.save_checkpoint(**checkpoint)

    def _checkpoint(self, state, node, detail=None, **options):
        state.ticket.updated_at = now_iso()
        state.record(node, detail or {"status": state.ticket.status})
        return {"state": state.to_dict(), "node": node, "detail": detail, **options}

    def _node(self, name):
        def invoke(raw):
            state = self.nodes[name](AgentState.from_dict(raw["state"]))
            index = self.NODE_ORDER.index(name)
            if state.error:
                state.next_node = name
            elif state.ticket.status in self.WAITING | self.TERMINAL:
                state.next_node = None
            else:
                state.next_node = self.NODE_ORDER[index + 1] if index + 1 < len(self.NODE_ORDER) else None
            self._persist(state, name)
            return {"state": state.to_dict()}
        return invoke

    def _continue(self, state):
        if state.next_node is None:
            return state
        if state.next_node not in self.NODE_ORDER:
            raise IdempotencyConflict("未知恢复位置，需人工核查")
        graph = self._build_graph(self.NODE_ORDER[self.NODE_ORDER.index(state.next_node):])
        return AgentState.from_dict(graph.invoke({"state": state.to_dict()})["state"])

    def run(self, ticket: Ticket, user_role: str = "employee") -> AgentState:
        payload = {k: getattr(ticket, k) for k in ("title", "description", "requester", "department", "severity")}
        payload["user_role"] = user_role
        if ticket.owner_id is not None:
            payload["owner_id"] = ticket.owner_id
        key = "ticket:" + (ticket.idempotency_key or ticket.id)
        initial = AgentState(ticket=ticket, user_role=user_role,
                             workflow_version=2,
                             clarification_max_rounds=self.settings.clarification_max_rounds)
        def process():
            with self.audit.ticket_lock(ticket.id):
                current = AgentState.from_dict(self.audit.get_state(ticket.id))
                return self._continue(current).to_dict()
        return AgentState.from_dict(self.audit.once(key, payload, process, ticket_id=ticket.id,
                                                     initial_state=initial.to_dict()))

    def _intake(self, state):
        state.ticket.category, state.ticket.status = "unclassified", "triaged"
        return state

    def _manual(self, state, reason):
        state.ticket.status = "manual_review"
        state.result = reason + "；等待授权人员人工处理。"
        if state.action_result is None:
            state.action_result = ActionResult("manual_review", "manual_review", state.result,
                                               self.tools.action_key(state.ticket, "manual_review"))
        return state

    def _clarify(self, state):
        if (state.confirmation or {}).get("status") == "confirmed":
            if state.confirmation["input_digest"] == input_digest(state.ticket, state.conversation_history):
                state.clarified = True
                if state.clarification:
                    state.clarification.update(need_clarify=False, missing_fields=[], question="")
                state.ticket.status, state.result = "triaged", ""
                return state
            state.confirmation = None
        constraints = request_constraints(state.ticket, state.conversation_history)
        understanding = analyze_request(state.ticket, state.conversation_history)
        state.understanding = understanding.to_dict()
        if understanding.blocked_reason:
            state.clarified = False
            return self._manual(state, understanding.blocked_reason)
        try:
            if constraints.missing:
                result = ClarificationDecision(True, constraints.missing)
            else:

                current = replace(state.ticket, title="", description=understanding.text)
                result = self.model.clarify(current, ())
                if not isinstance(result, ClarificationDecision):
                    raise ModelOutputError("澄清结果类型无效")
                result.__post_init__()
                if result.interpretation is not None:
                    understanding = from_proposal(state.ticket, state.conversation_history,
                        result.interpretation, result.source, self.settings.decision_min_confidence, for_planning=self.planning_enabled)
                state.understanding = understanding.to_dict()
                if understanding.blocked_reason:
                    return self._manual(state, understanding.blocked_reason)
                model_missing = result.missing_fields
                if self.planning_enabled and understanding.proposal and understanding.resource:
                    model_missing = [name for name in model_missing if name not in {"target", "permission", "symptom", "previous_access"}]
                    if understanding.proposal["speech_act"] == "report":
                        model_missing = [name for name in model_missing if name != "intent"]
                missing = list(dict.fromkeys([*understanding.missing, *model_missing]))
                result = ClarificationDecision(bool(missing), missing, result.source, result.metadata, result.interpretation)
        except Exception as exc:
            state.llm_error = type(exc).__name__
            info = describe_error(exc, "llm")
            if self.strict_model or getattr(self.model, "strict", False):
                state.error, state.error_info = "澄清模型调用失败", info
                state.ticket.status = "failed"
                return state
            understanding = analyze_request(state.ticket, state.conversation_history)
            state.understanding = understanding.to_dict()
            result = ClarificationDecision(bool(understanding.missing), understanding.missing, source="rule_fallback",
                                           metadata={"fallback_reason": type(exc).__name__, "error_info": info.to_dict()})
        state.clarification = result.to_dict()
        if result.source == "rule_fallback":
            state.llm_error = result.metadata.get("fallback_reason")
            state.degradation.append({"component": "llm", "stage": "clarify", **result.metadata})
        if not self.planning_enabled and not result.need_clarify and understanding.requires_confirmation:
            if result.source == "rule_fallback":
                return self._manual(state, "模型故障期间不继续敏感操作")
            plan = build_plan(state.ticket, understanding)
            state.confirmation = {"status": "pending", "plan": plan.model_dump(), "plan_digest": plan.digest(),
                                  "input_digest": input_digest(state.ticket, state.conversation_history),
                                  "round": state.clarification_round + 1}
            result = ClarificationDecision(True, ["confirmation"], result.source, result.metadata, result.interpretation)
            state.clarification = result.to_dict()
            action_name = "创建密码重置任务" if plan.action == "reset_password" else "申请访问权限"
            resource_name = resource_catalog().get(plan.resource, {"name": "本人登录账号"})["name"]
            permission_name = {None: "不适用", "read_only": "只读", "use": "普通使用"}[plan.permission]
            state.clarification["question"] = (f"请确认仅为本人账号 {plan.account} {action_name}，"
                f"资源：{resource_name}，权限：{permission_name}。同意请回复“确认执行”；"
                "如不一致，请直接说明正确诉求或取消工单。")
            state.clarification["confirmation_plan"] = plan.model_dump()
        state.clarified = not result.need_clarify
        if state.clarified:
            state.ticket.status, state.result = "triaged", ""
        elif state.clarification_round >= state.clarification_max_rounds:
            self._manual(state, "已达到澄清轮次上限，信息仍不足")
        else:
            state.clarification_round += 1
            state.ticket.status, state.result = "awaiting_clarification", state.clarification["question"]
            state.conversation_history.append({"role": "assistant", "content": state.result,
                                               "round": state.clarification_round, "at": now_iso()})
        return state

    def clarify(self, ticket_id: str, answer: str, round_number: int):
        if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 10000:
            raise ValueError("answer 长度必须为 1 到 10000")
        if type(round_number) is not int or not 1 <= round_number <= 10:
            raise ValueError("round_number 必须为 1 到 10 的整数")
        answer = answer.strip()
        with self.audit.ticket_lock(ticket_id):
            raw = self.audit.get_state(ticket_id)
            if raw is None:
                return None
            def validate():
                if raw["ticket"]["status"] != "awaiting_clarification" or raw.get("clarification_round") != round_number:
                    raise IdempotencyConflict("工单不在指定澄清轮次，请读取最新状态")
            state = AgentState.from_dict(raw)
            entry = {"role": "user", "content": answer, "round": round_number, "at": now_iso()}
            pending = state.confirmation or {}
            if (answer == "确认执行" and pending.get("status") == "pending"
                    and pending.get("round") == round_number
                    and pending.get("input_digest") == input_digest(state.ticket, state.conversation_history)):
                pending["status"], pending["by"], pending["at"] = "confirmed", state.ticket.owner_id or state.ticket.requester, now_iso()
                entry["kind"] = "plan_confirmation"
            else:
                state.confirmation = None
                state.diagnosis = None
                state.ticket.diagnostic_report = None
                state.planning = None
                state.ticket.execution_plan = None
                state.ticket.recommended_action = ""
                state.approval = None
            state.conversation_history.append(entry)
            state.ticket.status, state.result, state.next_node = "triaged", "", "clarify"
            if entry.get("kind") == "plan_confirmation" and state.planning:
                state.next_node = "authorize"
                state.clarified = True
                if state.clarification:
                    state.clarification.update(need_clarify=False, missing_fields=[], question="")
            checkpoint = self._checkpoint(state, "clarification_answer", {"round": round_number})
            def resume():
                return self._continue(state).to_dict()
            response = self.audit.once(f"clarification:{ticket_id}:{round_number}", {"answer": answer}, resume,
                                       precondition=validate, ticket_id=ticket_id, checkpoint=checkpoint)
            return AgentState.from_dict(response)

    def _retrieve(self, state):
        if not self.settings.planning_use_sop:
            state.retrieved_docs = []
            return state
        query = saved_understanding(state).text
        state.retrieved_docs, status = self.tools.kb.planning_context(query)
        if status.get("degraded"):
            state.degradation.append({"component": "rag", **status})
        return state

    def _plan(self, state):
        if self.planning_enabled:
            return self._plan_next_step(state)
        try:
            understanding = saved_understanding(state)
            plan = build_plan(state.ticket, understanding)
            decision = (self.model.decision_for(understanding) if type(self.model) is RuleBasedModel else
                        self.model.decide("", understanding.text, state.ticket.severity, state.retrieved_docs))

            DeepSeekDecisionModel._validate_decision(decision.to_dict())
        except Exception as exc:
            if self.strict_model or getattr(self.model, "strict", False):
                state.error, state.error_info = "模型规划或计划校验失败", describe_error(exc, "llm")
                state.ticket.status = "failed"
                return state
            return self._manual(state, "模型规划或计划校验失败")
        state.decision = decision.to_dict()
        if decision.source == "rule_fallback":
            state.llm_error = decision.metadata.get("fallback_reason")
            state.degradation.append({"component": "llm", "stage": "plan", **decision.metadata})
        issue = decision_problem(decision, understanding, min_confidence=self.settings.decision_min_confidence)
        if not issue and plan.action in {"reset_password", "grant_access"} and any(
                item.get("stage") == "clarify" and item.get("fallback_reason") for item in state.degradation):
            issue = "澄清模型故障，账号变更需要人工确认"
        if issue:
            return self._manual(state, issue)
        state.ticket.execution_plan = plan.model_dump()
        state.ticket.category, state.ticket.recommended_action = decision.category, plan.action
        state.ticket.status = "planned"
        return state

    def _ask(self, state, field, *, question=None):
        if state.clarification_round >= state.clarification_max_rounds:
            return self._manual(state, "达到澄清轮次上限，不能省略必要信息或确认")
        state.clarification_round += 1
        state.clarified = False
        state.clarification = ClarificationDecision(True, [field], source="planner").to_dict()
        if question:
            state.clarification["question"] = question
        state.ticket.status, state.result = "awaiting_clarification", state.clarification["question"]
        state.conversation_history.append({"role": "assistant", "content": state.result,
                                           "round": state.clarification_round, "at": now_iso()})
        return state

    def _plan_next_step(self, state):
        understanding = saved_understanding(state)
        try:
            decision = self.model.plan(state.ticket, understanding, state.retrieved_docs)
            if not isinstance(decision, PlanningDecision):
                raise ModelOutputError("规划必须返回 PlanningDecision")
            choice = decision.choice
            require_sop = self.settings.planning_use_sop and decision.source != "rule"
            require_quotes = decision.source == "deepseek"
            plan = validate_choice(state, choice, require_sop=require_sop, require_quotes=require_quotes)
        except Exception as exc:
            if self.strict_model or getattr(self.model, "strict", False):
                state.error, state.error_info = "规划模型或证据校验失败", describe_error(exc, "llm")
                state.ticket.status = "failed"
                return state
            return self._manual(state, "规划失败或缺少可核查证据")
        state.planning = {**decision.to_dict(), "input_digest": input_digest(state.ticket, state.conversation_history),
                          "require_sop": require_sop, "require_quotes": require_quotes}
        state.decision = decision.to_dict()
        if decision.source == "rule_fallback":
            state.llm_error = decision.metadata.get("fallback_reason")
            state.degradation.append({"component": "llm", "stage": "plan", **decision.metadata})
        if choice.next_step == "manual_review" or (choice.next_step == "propose_action"
                and choice.confidence < self.settings.decision_min_confidence):
            return self._manual(state, choice.reason if choice.next_step == "manual_review" else "模型对下一步判断不确定")
        if choice.next_step == "clarify":
            field = choice.missing_fact
            digest = input_digest(state.ticket, state.conversation_history)
            repeats = sum(item["field"] == field for item in state.planning_questions)
            already_known = ((field == "previous_access" and previous_access(understanding.text, understanding.proposal) != "unknown")
                             or (field == "target" and understanding.resource is not None)
                             or (field == "permission" and understanding.permission is not None))
            if already_known or repeats >= 2 or any(item["input_digest"] == digest and item["field"] == field for item in state.planning_questions):
                return self._manual(state, "规划重复追问已有信息或没有取得进展")
            state.planning_questions.append({"field": field, "input_digest": digest})
            return self._ask(state, field)
        if plan.action in self.HIGH_RISK | {"reset_password"} and any(item.get("component") == "llm" and item.get("fallback_reason") for item in state.degradation):
            return self._manual(state, "模型故障后不继续敏感操作")
        state.ticket.execution_plan = plan.model_dump()
        state.ticket.recommended_action = plan.action
        state.ticket.category = {"reset_password": "access_support", "grant_access": "access_request", "open_incident": "incident"}[plan.action]
        state.ticket.status = "planned"
        needs_confirmation = plan.action in {"reset_password", "grant_access"} and (
            decision.source != "rule" or understanding.requires_confirmation or self.settings.require_action_confirmation)
        state.planning["requires_confirmation"] = needs_confirmation
        if needs_confirmation:
            if state.clarification_round >= state.clarification_max_rounds:
                return self._manual(state, "达到澄清轮次上限，不能省略必要信息或确认")
            state.confirmation = {"status": "pending", "plan": plan.model_dump(), "plan_digest": plan.digest(),
                "input_digest": input_digest(state.ticket, state.conversation_history), "round": state.clarification_round + 1}
            action_name = "创建密码重置任务" if plan.action == "reset_password" else "申请访问权限"
            resource_name = resource_catalog().get(plan.resource, {"name": "本人登录账号"})["name"]
            permission_name = {None: "不适用", "read_only": "只读", "use": "普通使用"}[plan.permission]
            state = self._ask(state, "confirmation", question=f"请确认仅为本人账号 {plan.account} {action_name}，资源：{resource_name}，权限：{permission_name}。同意请回复“确认执行”；如不一致请说明正确诉求或取消。")
            state.clarification["confirmation_plan"] = plan.model_dump()
        return state

    def _diagnose(self, state):
        """为已校验的本人故障计划补充证据，不创建或修改执行计划。"""
        from .diagnostics import (DiagnosticRunner, LocalVPNDiagnostics, DeepSeekDiagnosticModel,
                                  RuleDiagnosticModel, incident_attachment)
        import hashlib
        plan = state.ticket.execution_plan or {}
        primary = getattr(self.model, "primary", self.model)
        investigate = (self.settings.diagnosis_mode == "react" and self.settings.diagnosis_backend == "investigation"
                       and isinstance(primary, DeepSeekDecisionModel) and self.diagnostic_runner is None)
        if (self.settings.diagnosis_mode == "off" or plan.get("action") != "open_incident"
                or plan.get("resource") not in ({"vpn", "erp"} if investigate else {"vpn"}) or not state.ticket.owner_id):
            return state
        try:
            validate_execution(state)
        except (ValueError, TypeError):
            return self._manual(state, "诊断前执行计划校验失败")
        runner = self.diagnostic_runner
        if investigate:
            from .investigation import InvestigationTools, InvestigationModel, InvestigationRunner
            frozen = bool((state.diagnosis or {}).get("evidence_snapshot"))
            if not self.settings.investigation_file.is_file() and not frozen:
                state.diagnosis = {"status": "skipped", "stop_reason": "investigation_not_configured"}
                return state
            resource = plan["resource"]
            with self._investigation_lock:
                if self.investigation_knowledge is None:
                    from .runbook_knowledge import RunbookKnowledge
                    self.investigation_knowledge = RunbookKnowledge(self.settings.runbooks_dir, settings=self.settings)
                knowledge = self.investigation_knowledge
                if not frozen and not knowledge.capture(resource):
                    state.diagnosis = {"status": "skipped", "stop_reason": "runbooks_not_configured"}
                    return state
                if resource not in self.investigation_runners:
                    self.investigation_runners[resource] = InvestigationRunner(
                        InvestigationTools(self.settings.investigation_file, self.settings.runbooks_file, resource, knowledge=knowledge),
                        InvestigationModel(self.settings), deadline_seconds=self.settings.diagnosis_deadline,
                        prefetch=self.settings.diagnosis_prefetch)
                runner = self.investigation_runners[resource]
        elif runner is None:
            if not self.settings.diagnosis_file.is_file():
                state.diagnosis = {"status": "skipped", "stop_reason": "telemetry_not_configured"}
                return state
            diagnostic_model = (DeepSeekDiagnosticModel(self.settings)
                if isinstance(primary, DeepSeekDecisionModel) and self.settings.diagnosis_mode != "rule"
                else RuleDiagnosticModel())
            runner = DiagnosticRunner(LocalVPNDiagnostics(self.settings.diagnosis_file), diagnostic_model,
              mode=("fixed" if self.settings.diagnosis_mode == "fixed" else
                    "rule" if diagnostic_model.source == "rule" else "react"),
                deadline_seconds=self.settings.diagnosis_deadline)
            self.diagnostic_runner = runner
        binding = hashlib.sha256((input_digest(state.ticket, state.conversation_history)
                                  + ExecutionPlan.model_validate(plan).digest()).encode()).hexdigest()
        def checkpoint(session):
            state.diagnosis = session
            state.next_node = "diagnose"
            self._persist(state, "diagnostic_checkpoint", {"tool_calls": session["tool_calls"],
                "model_calls": session["model_calls"], "status": session["status"],
                "pending_tool": session["pending_tool"]})
        state.diagnosis = runner.run(text=saved_understanding(state).text, owner=state.ticket.owner_id,
            binding=binding, saved=state.diagnosis, checkpoint=checkpoint)
        state.ticket.diagnostic_report = incident_attachment(state.diagnosis)
        for observation in state.diagnosis.get("observations", []):
            retrieval = observation.get("retrieval") or {}
            if retrieval.get("degraded"):
                state.degradation.append({"component": "rag", **retrieval})
        if state.diagnosis["stop_reason"] != "model_finish":
            state.degradation.append({"component": "diagnosis", "reason": state.diagnosis["stop_reason"]})
        return state

    def _authorize(self, state):
        if state.ticket.recommended_action in self.HIGH_RISK:
            plan = ExecutionPlan.model_validate(state.ticket.execution_plan)
            state.approval = Approval(ticket_id=state.ticket.id, action=plan.action,
                                      reason="请核对具体账号、资源及权限后审批", plan=plan.model_dump(), plan_digest=plan.digest())
            state.ticket.status = "awaiting_approval"
        else:
            state.ticket.status = "authorized"
        return state

    def _execute(self, state):
        try:
            validate_execution(state)
        except (ValueError, TypeError):
            return self._manual(state, "执行计划与用户证据或审批不一致，已阻止执行")
        action = state.ticket.recommended_action
        key = self.tools.action_key(state.ticket, action)
        try:
            cached = self.audit.get_operation(key)
            if cached and cached["status"] == "completed":
                state.action_result = self.tools.execute_action(state.ticket, action)
            else:
                state.action_result = self.tool_breaker.call(lambda: self.tools.execute_action(state.ticket, action))
            state.result = state.action_result.message
            state.ticket.status = "executed" if state.action_result.status == "succeeded" else "manual_review"
            if state.action_result.error:
                state.degradation.append({"component": "tool", "error_info": state.action_result.error.to_dict()})
        except Exception as exc:
            info = describe_error(exc, "tool")
            state.degradation.append({"component": "tool", "reason": type(exc).__name__, "error_info": info.to_dict()})
            state.ticket.status, state.result = "manual_review", "工具不可用或结果不明，等待人工核查，禁止盲目重试。"
            state.action_result = ActionResult(action, "unknown" if info.outcome_unknown else "manual_review",
                                               state.result, key, error=info)
        return state

    def _finalize(self, state):
        if not state.action_result or state.action_result.status != "succeeded":
            return self._manual(state, "缺少成功执行结果，不能自动关闭")
        state.ticket.status = "closed"
        state.resolution = {"type": "agent_task_completed", "external_issue_resolved": None,
                            "action": state.ticket.recommended_action, "simulated": state.action_result.simulated,
                            "at": now_iso()}
        if state.workflow_version >= 2 and self._can_investigate(state):
            state.ticket.status = "awaiting_investigation"
            state.ticket.investigation_status = "queued"
            state.resolution["type"] = "incident_registered"
        return state

    def _can_investigate(self, state):
        plan = state.ticket.execution_plan or {}
        primary = getattr(self.model, "primary", self.model)
        modern = (self.settings.diagnosis_backend == "investigation" and self.settings.diagnosis_mode == "react"
                  and isinstance(primary, DeepSeekDecisionModel) and self.diagnostic_runner is None)
        return (self.settings.diagnosis_mode != "off" and plan.get("action") == "open_incident"
                and plan.get("resource") in ({"vpn", "erp"} if modern else {"vpn"})
                and (self.diagnostic_runner is not None or
                     (self.settings.investigation_file if modern else self.settings.diagnosis_file).is_file()))

    def investigate(self, ticket_id):
        """Worker entry. Service/API checks caller access; persisted ticket binds ownership."""
        with self.audit.ticket_lock(ticket_id):
            raw = self.audit.get_state(ticket_id)
            if not raw:
                return None
            state = AgentState.from_dict(raw)
            if state.next_node not in {"diagnose", "enrich"}:
                return state
            if state.workflow_version != 2 or not state.action_result or state.action_result.status != "succeeded":
                raise IdempotencyConflict("必须先核查事件登记结果，不能直接继续旧版调查")
            state.ticket.status = "investigating"
            state.ticket.investigation_status = "running"
            self._persist(state, "investigation_started")
            return self._continue(state)

    def _enrich(self, state):
        if state.ticket.diagnostic_report:
            try:
                result = self.tools.attach_evidence(state.ticket)
                state.enrichment_result = result.to_dict()
                if result.status != "succeeded":
                    raise ValueError("attachment not confirmed")
                state.result = state.action_result.message + " " + result.message
            except Exception as exc:
                state.ticket.investigation_status = "attachment_unknown"
                state.degradation.append({"component": "enrichment", "reason": type(exc).__name__})
                return self._manual(state, "事件已登记，但附件更新结果不明，禁止盲目重试")
        state.ticket.status = "closed"
        state.ticket.investigation_status = ("completed" if state.ticket.diagnostic_report else "unavailable")
        state.resolution = {**(state.resolution or {}), "type": "agent_task_completed", "external_issue_resolved": None}
        return state

    def feedback(self, ticket_id, actor, revision, verdict, note, evidence, issue_resolved):
        with self.audit.ticket_lock(ticket_id):
            state = AgentState.from_dict(self.audit.get_state(ticket_id))
            previous_length = len(state.ticket.feedback)
            def validate():
                if state.ticket.status not in {"closed", "resolved_manually"} or not state.ticket.diagnostic_report:
                    raise IdempotencyConflict("调查附件完成并交接后才能核实")
                if revision != previous_length + 1:
                    raise IdempotencyConflict("反馈版本已变化，请读取最新工单")
            import hashlib
            import json
            payload = dict(actor=actor, revision=revision, verdict=verdict, note=note,
                           evidence=evidence, issue_resolved=issue_resolved)
            report_hash = hashlib.sha256(json.dumps(state.ticket.diagnostic_report, sort_keys=True).encode()).hexdigest()
            state.ticket.feedback.append({**payload, "report_hash": report_hash, "at": now_iso()})
            state.resolution = {**(state.resolution or {}), "external_issue_resolved": issue_resolved,
                                "verified_by": actor, "feedback_revision": revision}
            checkpoint = self._checkpoint(state, "diagnostic_feedback", payload)
            return AgentState.from_dict(self.audit.once(f"feedback:{ticket_id}:{revision}", payload, state.to_dict,
                ticket_id=ticket_id, checkpoint=checkpoint, precondition=validate))

    def approve(self, approval_id: str, approver: str, approved: bool):
        if type(approved) is not bool:
            raise ValueError("approved must be bool")
        approval_raw = self.audit.get_approval(approval_id)
        if not approval_raw:
            return None
        ticket_id = approval_raw["ticket_id"]
        with self.audit.ticket_lock(ticket_id):
            raw = self.audit.get_state(ticket_id)
            if not raw:
                return None
            def validate():
                if raw["ticket"]["status"] != "awaiting_approval" or not raw.get("approval") or raw["approval"]["id"] != approval_id:
                    raise IdempotencyConflict("工单已不在等待该审批的状态")
                try:
                    plan = ExecutionPlan.model_validate(raw["ticket"].get("execution_plan"))
                except (ValueError, TypeError):
                    raise IdempotencyConflict("缺少有效执行计划，不能沿用旧审批，请重新提交") from None
                if (raw["approval"].get("plan_digest") != plan.digest()
                        or raw["approval"].get("plan") != plan.model_dump()):
                    raise IdempotencyConflict("审批计划已发生变化，禁止沿用旧审批")
            state = AgentState.from_dict(raw)
            state.approval.status = "approved" if approved else "rejected"
            state.approval.decided_by, state.approval.decided_at = approver, now_iso()
            state.ticket.status = "authorized" if approved else "rejected"
            state.next_node = "execute" if approved else None
            state.result = "" if approved else "审批被拒绝，未执行权限变更。"
            checkpoint = self._checkpoint(state, "approval", {"approved": approved, "approver": approver})
            def resume():
                return self._continue(state).to_dict()
            return AgentState.from_dict(self.audit.once("approval:" + approval_id,
                {"approved": approved, "approver": approver}, resume, precondition=validate, ticket_id=ticket_id,
                checkpoint=checkpoint))

    def cancel(self, ticket_id, actor, reason):
        with self.audit.ticket_lock(ticket_id):
            state = AgentState.from_dict(self.audit.get_state(ticket_id))
            previous_status = state.ticket.status
            def validate():
                if previous_status not in self.WAITING:
                    raise IdempotencyConflict("只能取消等待澄清或审批的工单")
            state.ticket.status, state.next_node = "cancelled", None
            state.result = "申请人已取消，未执行自动动作。"
            if state.approval:
                state.approval.status = "cancelled"
            state.resolution = {"type": "cancelled", "by": actor, "reason": reason, "at": now_iso()}
            checkpoint = self._checkpoint(state, "cancel", state.resolution)
            return AgentState.from_dict(self.audit.once("cancel:" + ticket_id, {"actor": actor, "reason": reason},
                state.to_dict, precondition=validate, ticket_id=ticket_id, checkpoint=checkpoint))

    def resolve(self, ticket_id, actor, outcome, note, evidence):
        with self.audit.ticket_lock(ticket_id):
            state = AgentState.from_dict(self.audit.get_state(ticket_id))
            previous_status = state.ticket.status
            def validate():
                if previous_status not in {"manual_review", "failed"}:
                    raise IdempotencyConflict("仅人工待办或失败工单可以人工结案")
                unknown = state.action_result and state.action_result.status == "unknown"
                if (unknown or any(x["kind"] == "tool" for x in self.audit.pending_operations(ticket_id))) and not evidence.strip():
                    raise IdempotencyConflict("执行结果未知，必须填写核查凭证后才能结案")
            state.ticket.status, state.next_node = "resolved_manually", None
            state.resolution = {"type": "manual", "by": actor, "outcome": outcome, "note": note,
                                "evidence": evidence, "at": now_iso()}
            if state.error:
                state.resolution["previous_error"] = state.error
                state.resolution["previous_error_info"] = state.error_info.to_dict() if state.error_info else None
                state.error, state.error_info = None, None
            state.result = "人工处理完成：" + note

            checkpoint = self._checkpoint(state, "manual_resolution", state.resolution,
                                          reconcile=True, resolve_tools=True)
            return AgentState.from_dict(self.audit.once("resolve:" + ticket_id,
                {"actor": actor, "outcome": outcome, "note": note, "evidence": evidence}, state.to_dict,
                precondition=validate, ticket_id=ticket_id, checkpoint=checkpoint))

    def recover(self, ticket_id, actor):
        with self.audit.ticket_lock(ticket_id):
            raw = self.audit.get_state(ticket_id)
            if "next_node" not in raw:
                raise IdempotencyConflict("旧快照没有明确恢复位置，禁止自动回放")
            state = AgentState.from_dict(raw)
            if state.workflow_version < 2 and state.next_node == "diagnose":
                raise IdempotencyConflict("旧版调查位于登记之前，请人工核查后结案，不自动迁移执行顺序")
            if state.next_node is None:

                self._persist(state, "recovery_reconcile", {"actor": actor}, reconcile=True)
                return state
            if state.next_node == "execute":
                key = self.tools.action_key(state.ticket, state.ticket.recommended_action)
                operation = self.audit.get_operation(key)
                if operation and operation["status"] == "pending":
                    state.action_result = ActionResult(state.ticket.recommended_action, "unknown",
                        "下游执行结果未知，禁止自动重试", key)
                    self._manual(state, "发现未确认的工具操作，必须人工核查")
                    state.next_node = None
                    self._persist(state, "recovery_blocked", {"actor": actor}, reconcile=True)
                    return state
            state.error, state.error_info = None, None
            state.ticket.status = "recovering"
            self._persist(state, "recovery_started", {"actor": actor, "next_node": state.next_node})
            state = self._continue(state)
            self._persist(state, "recovery_completed", {"actor": actor}, reconcile=True)
            return state
