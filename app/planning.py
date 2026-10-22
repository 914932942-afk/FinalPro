"""基于事实的有限规划；模型决定下一步，服务端控制执行边界。"""
from dataclasses import dataclass, field
from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .policy import (Understanding, ExecutionPlan, resource_catalog, contains,
                     request_constraints, input_digest, analyze_request)


class PlanningChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    next_step: Literal["clarify", "propose_action", "manual_review"]
    action: Literal["reset_password", "grant_access", "open_incident"] | None
    resource: str | None
    permission: Literal["read_only", "use"] | None
    missing_fact: Literal["previous_access", "intent", "target", "permission", "symptom"] | None
    user_evidence: list[str] = Field(max_length=8)
    sop_refs: list[str] = Field(max_length=8)
    sop_evidence: dict[str, str] = Field(default_factory=dict, max_length=8)
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def consistent(self):
        if self.next_step == "propose_action":
            if not self.action or self.missing_fact is not None:
                raise ValueError("proposal requires action and no missing fact")
        elif self.action is not None or self.permission is not None:
            raise ValueError("non-executable decision cannot carry an action plan")
        if (self.next_step == "clarify") != (self.missing_fact is not None):
            raise ValueError("clarification requires one missing fact")
        return self


@dataclass
class PlanningDecision:
    choice: PlanningChoice
    source: str = "rule"
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return {**self.choice.model_dump(), "source": self.source, "metadata": self.metadata}


@dataclass(frozen=True)
class ProcedureConclusion:
    """只校验维护者声明的固定格式，不推断任意自然语言的蕴含关系。"""
    step: str
    action: str | None
    resource: str | None
    line: str

    @classmethod
    def parse(cls, snippet):
        lines = re.findall(r"(?m)^处理结论[:：]\s*(.+)$", snippet)
        if len(lines) != 1:
            raise ValueError("local procedure needs one explicit handling conclusion")
        # 结论句与其后解释分开；无需为了格式而强制引用同一行的所有补充说明。
        line = lines[0].split("。", 1)[0].strip()
        if re.match(r"manual_review\b", line):
            return cls("manual_review", None, None, line)
        action = re.search(r"动作\s+(reset_password|grant_access|open_incident)\b", line)
        resource = re.search(r"资源\s+(self_account|erp|vpn|reports_db)\b", line)
        if not re.match(r"propose_action\b", line) or not action or not resource:
            raise ValueError("local procedure handling conclusion is unsupported")
        return cls("propose_action", action[1], resource[1], line)


def validate_procedure_support(state, choice, codes, *, require_quotes):
    from .knowledge import KnowledgeBase
    applicable = [doc for doc in state.retrieved_docs if doc.get("status", "active") == "active"
                  and doc.get("resource") == choice.resource
                  and codes.intersection(KnowledgeBase.local_codes(doc.get("codes", "")))]
    conclusions = []
    cited_support = False
    for doc in applicable:
        conclusion = ProcedureConclusion.parse(doc["snippet"])
        conclusions.append(conclusion)
        # 校验召回到的全部适用规程，不能通过不引用冲突片段来绕过。
        if (conclusion.step != "propose_action" or conclusion.action != choice.action
                or conclusion.resource != choice.resource):
            raise ValueError("action contradicts an applicable explicit SOP conclusion")
        requirements = re.findall(r"(?m)^前置事实[:：]\s*(.+)$", doc["snippet"])
        for requirement in requirements:
            match = re.fullmatch(r"previous_access=(yes|no)", requirement.strip())
            if not match or previous_access((state.understanding or {}).get("text", ""),
                                             (state.understanding or {}).get("proposal")) != match[1]:
                raise ValueError("declared SOP prerequisite is unsupported or not established")
        if doc["id"] in choice.sop_refs:
            quote = choice.sop_evidence.get(doc["id"], "")
            if not require_quotes or conclusion.line in quote:
                cited_support = True
    if not conclusions or not cited_support:
        raise ValueError("local action needs a quoted applicable handling conclusion")


def previous_access(text, proposal=None):
    fact = (proposal or {}).get("facts", {}).get("previous_access")
    if fact and fact["value"] != "unknown" and fact["evidence"] in text:
        return fact["value"]
    if re.search(r"以前正常|以前能|之前能|昨天还能|原来能|一直能|used to work|worked yesterday", text, re.I):
        return "yes"
    if re.search(r"第一次|首次|从未用过|从没用过|first time|never used", text, re.I):
        return "no"
    return "unknown"


def access_ambiguity(text):
    return bool(re.search(r"没有权限|无权限|权限不足|access denied|forbidden|\b403\b", text, re.I))


def rule_plan(ticket, understanding, documents):
    """A 组基线：有限规则，不声称具备模型推理能力。"""
    text = understanding.text
    from .knowledge import KnowledgeBase
    if KnowledgeBase.local_codes(text):
        return PlanningDecision(PlanningChoice(next_step="manual_review", action=None, resource=None,
            permission=None, missing_fact=None, user_evidence=[text.strip()], sop_refs=[],
            reason="离线规则无法解释内部错误码，需要人工核查适用规程", confidence=1.0))
    prior = previous_access(text, understanding.proposal)
    raw = dict(next_step="propose_action", action=understanding.action,
               resource=understanding.resource, permission=understanding.permission,
               missing_fact=None, user_evidence=[text.strip()], sop_refs=[], reason="离线规则匹配", confidence=0.9)
    if access_ambiguity(text) and prior == "unknown" and understanding.action != "grant_access":
        raw.update(next_step="clarify", action=None, resource=None, permission=None,
                   missing_fact="previous_access", reason="区分已有访问故障与首次申请")
    elif access_ambiguity(text) and prior == "yes":
        raw.update(action="open_incident", permission=None, reason="已有访问突然失败，登记故障")
    elif not understanding.action:
        raw.update(next_step="clarify", action=None, resource=None, permission=None,
                   missing_fact="intent", reason="无法确定具体操作")
    if raw["next_step"] == "propose_action":
        source = {"reset_password": "password_reset.md", "grant_access": "access_request.md", "open_incident": "incident_response.md"}[raw["action"]]
        raw["sop_refs"] = [doc["id"] for doc in documents if doc.get("source") == source][:1]
    return PlanningDecision(PlanningChoice.model_validate(raw))


def validate_choice(state, choice, *, require_sop=True, require_quotes=False):
    constraints = request_constraints(state.ticket, state.conversation_history)
    if constraints.blocked_reason or constraints.missing:
        raise ValueError("request constraints prevent planning")
    text = constraints.text
    for quote in choice.user_evidence:
        if not quote.strip() or quote not in text:
            raise ValueError("planning evidence must come from user")
    docs = {doc["id"]: doc for doc in state.retrieved_docs}
    if any(ref not in docs for ref in choice.sop_refs):
        raise ValueError("SOP reference was not retrieved")
    for ref, quote in choice.sop_evidence.items():
        if ref not in choice.sop_refs or not quote.strip() or quote not in docs[ref]["snippet"]:
            raise ValueError("SOP quote must be an exact excerpt of a cited retrieved chunk")
    if choice.next_step != "propose_action":
        return None
    if not choice.user_evidence or (require_sop and not choice.sop_refs):
        raise ValueError("action lacks user or SOP evidence")
    if require_sop and require_quotes and set(choice.sop_evidence) != set(choice.sop_refs):
        raise ValueError("each SOP reference requires a verifiable quote")
    if require_sop:
        from .knowledge import KnowledgeBase
        codes = KnowledgeBase.local_codes(text)
        for ref in choice.sop_refs:
            doc = docs[ref]
            if doc.get("status", "active") != "active" or (doc.get("resource") and doc["resource"] != choice.resource):
                raise ValueError("SOP scope does not match the plan")
            doc_codes = KnowledgeBase.local_codes(doc.get("codes", ""))
            if doc_codes and not codes.intersection(doc_codes):
                raise ValueError("SOP code does not match user evidence")
        if codes and not any(codes.intersection(KnowledgeBase.local_codes(docs[ref].get("codes", "")))
                             for ref in choice.sop_refs):
            raise ValueError("local error code needs its applicable procedure, not a generic SOP")
        if codes:
            validate_procedure_support(state, choice, codes, require_quotes=require_quotes)
    if choice.action == "reset_password":
        if choice.resource != "self_account" or choice.permission is not None:
            raise ValueError("invalid password plan")
    else:
        catalog = resource_catalog()
        if choice.resource not in catalog or not any(contains(text, alias) for alias in catalog[choice.resource]["aliases"]):
            raise ValueError("resource is not supported by user evidence")
        if choice.action == "grant_access":
            if choice.permission not in catalog[choice.resource]["permissions"]:
                raise ValueError("permission is not allowed")
        elif choice.permission is not None:
            raise ValueError("incident cannot contain grant parameters")
    original = state.understanding or {}
    if original.get("input_digest") != input_digest(state.ticket, state.conversation_history):
        raise ValueError("interpretation is stale")
    if (original.get("proposal") or {}).get("subject") == "other":
        raise ValueError("cannot operate for another account")
    conservative = analyze_request(state.ticket, state.conversation_history)
    if conservative.blocked_reason or any(x in {"single_intent", "single_resource"} for x in conservative.missing):
        raise ValueError("ambiguous or unsupported request")
    # 解释出的计划不等于授权；账号来自认证身份，敏感动作还要本人确认/审批。
    return ExecutionPlan(action=choice.action, account=state.ticket.owner_id or state.ticket.requester,
                         resource=choice.resource, permission=choice.permission, severity=state.ticket.severity)


def validate_saved_plan(state):
    saved = state.planning or {}
    choice = PlanningChoice.model_validate({key: saved[key] for key in PlanningChoice.model_fields if key in saved})
    if saved.get("input_digest") != input_digest(state.ticket, state.conversation_history):
        raise ValueError("planning input changed")
    plan = validate_choice(state, choice, require_sop=saved.get("require_sop", True), require_quotes=saved.get("require_quotes", False))
    if plan is None or plan.model_dump() != state.ticket.execution_plan:
        raise ValueError("saved plan does not match validated decision")
    return plan
