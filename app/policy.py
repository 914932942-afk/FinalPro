"""小范围、保守的业务边界：用户证据生成参数，模型仅提出候选动作。"""
from dataclasses import asdict, dataclass, field
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


@lru_cache(maxsize=1)
def resource_catalog():
    return json.loads(Path(__file__).with_name("resources.json").read_text(encoding="utf-8"))


def contains(text, term):
    if term.isascii():
        return re.search(r"(?<![a-z0-9_])" + re.escape(term.lower()) + r"(?![a-z0-9_])", text.lower()) is not None
    return term in text


def password_issue(text):
    return any(term in text.lower() for term in (
        "密码重置", "重置密码", "忘记密码", "忘了密码", "密码忘", "密码错误", "密码不正确", "密码过期",
        "password reset", "reset password", "forgot password", "forgot my password", "incorrect password",
        "invalid password", "password expired"))


@dataclass
class Understanding:
    text: str
    action: str | None = None
    resource: str | None = None
    permission: str | None = None
    missing: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    requires_confirmation: bool = False
    source: str = "rule"
    proposal: dict | None = None
    input_digest: str = ""
    confidence_threshold: float = 0.8
    interpretation_only: bool = False

    def to_dict(self):
        return asdict(self)


def current_text(ticket, history=()):
    text = f"{ticket.title}\n{ticket.description}"
    # 明确的整单更正替换当前理解，原文和历史仍保留在快照中。
    for item in history:
        if item.get("role") != "user" or item.get("kind") in {"plan_confirmation", "diagnostic_followup", "case_verification"}:
            continue
        reply = item["content"].strip()
        correction = re.match(r"^(?:更正|改为|correction)\s*[:：]\s*(.+)$", reply, re.I | re.S)
        natural = re.match(r"^(?:不是|并非).+?[,，；;]\s*(?:而?是)(.+)$", reply, re.S)
        change = re.match(r"^(?:我说错了[,，：:]?\s*|请?改成\s*|请?改为\s*)(.+)$", reply, re.S)
        replacement = correction or natural or change
        text = replacement[1] if replacement else text + "\n" + reply
    return text


def input_digest(ticket, history=()):
    data = [ticket.title, ticket.description, ticket.owner_id or ticket.requester, ticket.severity,
             [item for item in history if item.get("role") == "user" and item.get("kind") not in {"plan_confirmation", "diagnostic_followup", "case_verification"}]]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def request_constraints(ticket, history=()):
    """高风险信号只允许收紧处理，模型不能把它们覆盖为可执行请求。"""
    text = current_text(ticket, history)
    result = Understanding(text=text)
    result.input_digest = input_digest(ticket, history)
    lower = re.sub(r"\s+", " ", text.lower())
    incident_report = bool(re.search(r"报错|超时|宕机|打不开|连不上|无法(?:登录|连接|打开|访问)|连接失败|一直转圈|\b(?:error|timeout|outage)\b|(?<!\d)[45]\d\d(?!\d)", lower))
    hypothetical = bool(re.search(r"如果|假如|假设|\bif\b|\bwould\b", lower))
    if hypothetical or (not incident_report and re.search(r"如何|怎么|教程|流程说明|\bsop\b|文档|how to|what is|是否|会不会", lower)):
        result.blocked_reason = "咨询或文档查询不等于操作授权，转人工答复"
        return result
    if re.search(r"删除.{0,15}(?:数据库|数据)|drop\s+table", lower):
        result.blocked_reason = "当前不支持删除数据操作"
        return result
    if re.search(r"忽略.{0,12}(?:规则|指令|审批|权限)|绕过.{0,8}(?:审批|验证)|ignore.{0,20}(?:instructions|rules)|bypass.{0,15}approval", lower):
        result.blocked_reason = "请求包含绕过规则的指令，需要人工核查"
        return result
    subject = ticket.owner_id or ticket.requester
    targets = re.findall(r"(?:帮|替|给|为)\s*([a-z0-9_.@-]+|我|本人|自己|[\u4e00-\u9fff]{1,6}?)\s*(?:的)?\s*(?:重置|申请|开通)", lower)
    targets += re.findall(r"(?:reset|重置)\s+([a-z0-9_.@-]+)(?:'s|\s*的)\s*(?:password|密码)", lower)
    targets += re.findall(r"([a-z0-9_.@-]+)\s*(?:的|['’]s)\s*(?:账号|帐户|账户|密码|password|account)", lower)
    targets += re.findall(r"(?:账号|账户|帐户|用户名|account|username)\s*(?:是|为|[:：=]|is)\s*([a-z0-9_.@-]+)", lower)
    if re.search(r"(?:同事|他人|别人|张三|李四)的?(?:账号|账户|密码)|(?:他的|她的)(?:账号|密码)", lower):
        result.blocked_reason = "当前仅支持本人账号操作，代他人申请需要人工处理"
        return result
    self_names = {"我", "本人", "自己", subject.lower(), *(name.lower() for name in ticket.account_aliases)}
    if any(target not in self_names for target in targets):
        result.blocked_reason = "当前仅支持本人账号操作，代他人申请需要人工处理"
        return result
    # Uncertainty in an incident report is normal; it is not authorization for
    # a password/grant action. An explicit operation needs current consent.
    sensitive = password_issue(lower) or bool(re.search(r"(?:申请|开通|授予|grant|request).{0,40}(?:权限|访问|access)", lower))
    consent_text = lower
    replies = [row.get("content", "") for row in history if row.get("role") == "user" and row.get("kind") == "constraint_answer"]
    if (replies and re.match(r"^(?:确认|确定|是的|是，|yes\b|confirm\b)", replies[-1].strip(), re.I)
            and not re.search(r"不确定|可能|也许|maybe|not sure", replies[-1], re.I)):
        # A controlled confirmation question can resolve uncertainty, while
        # hard denials in the original request continue to block execution.
        consent_text = re.sub(r"不确定|可能|也许|maybe|not sure", "", consent_text)
    deny = r"暂不|暂时不|取消|撤回|不要|不需要|不用|无需|不准|禁止|别.{0,18}(?:密码|重置|执行|开通|权限)|不希望|不想|不是|已解决|已完成|已经.{0,8}重置|\bdon['’]t\b|\bdo not\b|\bcancel\b"
    if re.search(deny, consent_text) or (sensitive and re.search(r"不确定|可能|也许|maybe|not sure|不能|\bnot\b", consent_text)):
        result.missing = ["confirmation"]
        return result
    return result


def analyze_request(ticket, history=()):
    result = request_constraints(ticket, history)
    if result.blocked_reason or result.missing:
        return result
    lower = re.sub(r"\s+", " ", result.text.lower())
    password = password_issue(lower)
    access = bool(re.search(r"(?:申请|开通|授予|需要|给我|赋予|允许).{0,60}(?:权限|访问)|(?:权限|访问)申请|(?:request|grant|need).{0,30}access", lower))
    incident = bool(re.search(r"故障|宕机|报错|错误|超时|打不开|连不上|无法|失败|登录|没有权限|无权限|权限不足|login|outage|error|timeout|denied|forbidden|refused|\b[45]\d\d\b", lower))
    resources = [key for key, value in resource_catalog().items() if any(contains(lower, alias) for alias in value["aliases"])]
    independent_incident = bool(re.search(r"超时|宕机|报错|故障|连接失败|timeout|outage|denied|\b[45]\d\d\b", lower))
    if password and access or ((password or access) and incident and resources and
            (independent_incident or re.search(r"同时|还要|还需要|并且|以及|另外|\band\b", lower))):
        result.missing = ["single_intent"]
        return result
    if len(resources) > 1:
        result.missing = ["single_resource"]
        return result
    result.resource = resources[0] if resources else None
    if password:
        result.action, result.resource = "reset_password", "self_account"
        result.requires_confirmation = not bool(re.search(r"重置|\breset\b", lower))
    elif access:
        result.action = "grant_access"
        if re.search(r"管理员|写入|读写|删除|超级|\badmin\b|\broot\b|\bwrite\b", lower):
            result.blocked_reason = "Demo 仅支持只读或普通使用权限，高权限申请转人工"
            return result
        if re.search(r"只读|read.only", lower):
            result.permission = "read_only"
        elif re.search(r"普通使用|使用权限|\buse\b", lower):
            result.permission = "use"
        if not result.permission:
            result.missing.append("permission")
    elif incident:
        result.action = "open_incident"
        if not re.search(r"超时|宕机|错误.{1,}|报错.{1,}|没有权限|无权限|权限不足|timeout|outage|denied|forbidden|refused|\b[45]\d\d\b|error.{1,}", lower):
            result.missing.append("symptom")
    else:
        result.missing.append("intent")
    if result.action in {"grant_access", "open_incident"} and not result.resource:
        result.missing.insert(0, "target")
    if (result.action == "grant_access" and result.resource and result.permission
            and result.permission not in resource_catalog()[result.resource]["permissions"]):
        result.blocked_reason = "所选资源不支持该权限，请人工核查"
    return result


class UserFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: Literal["yes", "no", "unknown"]
    evidence: str


class IntentProposal(BaseModel):
    """模型仅解释用户证据，不能提供身份、角色、审批或自由工具参数。"""
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    speech_act: Literal["request", "report", "question", "withdrawal", "uncertain"]
    action: Literal["reset_password", "grant_access", "open_incident"] | None
    resource: str | None
    permission: Literal["read_only", "use"] | None
    subject: Literal["self", "other", "unknown"]
    multiple_intents: bool
    confidence: float = Field(ge=0, le=1)
    evidence: dict[Literal["action", "resource", "permission", "subject"], str]
    candidate_actions: list[Literal["reset_password", "grant_access", "open_incident"]] = Field(default_factory=list, max_length=3)
    facts: dict[Literal["previous_access"], UserFact] = Field(default_factory=dict)


def from_proposal(ticket, history, raw, source="deepseek", min_confidence=0.8, *, for_planning=False):
    proposal = IntentProposal.model_validate(raw)
    result = request_constraints(ticket, history)
    result.source, result.proposal = source, proposal.model_dump()
    result.confidence_threshold = min_confidence
    result.interpretation_only = for_planning
    if result.blocked_reason or result.missing:
        return result
    # 引用必须来自当前用户证据，不能来自系统提示或检索文档。
    for quote in proposal.evidence.values():
        if not quote.strip() or quote not in result.text:
            raise ValueError("semantic evidence is not in user input")
    for fact in proposal.facts.values():
        if fact.value != "unknown" and (not fact.evidence.strip() or fact.evidence not in result.text):
            raise ValueError("fact evidence is not in user input")
    if proposal.subject == "other" or proposal.speech_act == "question":
        result.blocked_reason = "第三方操作或咨询需要人工处理"
        return result
    if proposal.multiple_intents:
        result.missing = ["single_intent"]
        return result
    if proposal.speech_act in {"withdrawal", "uncertain"} or (not for_planning and proposal.confidence < min_confidence):
        result.missing = ["confirmation"]
        return result
    if not proposal.action and not (for_planning and (proposal.candidate_actions or
            (proposal.speech_act == "report" and proposal.resource in resource_catalog()))):
        result.missing = ["intent"]
        return result
    if proposal.action and "action" not in proposal.evidence:
        raise ValueError("action evidence required")
    result.action = proposal.action
    result.resource, result.permission = proposal.resource, proposal.permission
    if result.action == "reset_password":
        if result.resource not in {None, "self_account"} or result.permission is not None:
            raise ValueError("invalid password reset target")
        result.resource = "self_account"
    else:
        if result.resource not in resource_catalog():
            result.resource = None
            result.missing.append("target")
        elif not any(contains(proposal.evidence.get("resource", ""), alias)
                     for alias in resource_catalog()[result.resource]["aliases"]):
            raise ValueError("resource evidence missing or mismatched")
    if result.action == "grant_access":
        if not result.permission and not for_planning:
            result.missing.append("permission")
        elif result.permission and not proposal.evidence.get("permission"):
            raise ValueError("permission evidence required")
        elif result.permission and result.resource and result.permission not in resource_catalog()[result.resource]["permissions"]:
            result.blocked_reason = "所选资源不支持该权限，需要人工处理"
    elif result.permission is not None:
        raise ValueError("permission only applies to grants")
    # 模型解释出的敏感操作必须由本人确认具体计划，不能以自报置信度代替授权。
    result.requires_confirmation = result.action in {"reset_password", "grant_access"}
    # 常见明确多意图、高权限、资源冲突仍可收紧模型判断。
    conservative = analyze_request(ticket, history)
    if conservative.blocked_reason:
        result.blocked_reason = conservative.blocked_reason
    for name in conservative.missing:
        if name in {"single_intent", "single_resource"} and name not in result.missing:
            result.missing.append(name)
    return result


def saved_understanding(state):
    stored = state.understanding or {}
    if stored.get("proposal"):
        result = from_proposal(state.ticket, state.conversation_history, stored["proposal"],
                               stored.get("source", "deepseek"), stored.get("confidence_threshold", 0.8),
                               for_planning=stored.get("interpretation_only", False))
        if stored.get("input_digest") != result.input_digest:
            raise ValueError("user evidence changed since interpretation")
        return result
    return analyze_request(state.ticket, state.conversation_history)


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: int = Field(default=1, ge=1, le=1)
    action: Literal["reset_password", "grant_access", "open_incident"]
    account: str = Field(min_length=1, max_length=200)
    resource: str = Field(min_length=1)
    permission: Literal["read_only", "use"] | None = None
    severity: Literal["low", "medium", "high", "critical"] = "medium"

    def digest(self):
        raw = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


def build_plan(ticket, understanding):
    if understanding.blocked_reason or understanding.missing or not understanding.action:
        raise ValueError("request is not ready")
    return ExecutionPlan(action=understanding.action, account=ticket.owner_id or ticket.requester,
                         resource=understanding.resource, permission=understanding.permission,
                         severity=ticket.severity)


def decision_problem(decision, understanding, *, min_confidence=0.8):
    categories = {"reset_password": "access_support", "grant_access": "access_request", "open_incident": "incident"}
    if decision.action == "manual_review":
        return "模型建议人工处理"
    if decision.action != understanding.action or decision.category != categories.get(decision.action):
        return "模型动作与用户证据不一致"
    if decision.confidence < min_confidence:
        return "模型明确表示不确定，停止自动执行"
    if decision.source == "rule_fallback" and decision.action in {"reset_password", "grant_access"}:
        return "模型故障降级期间不自动执行账号或权限变更"
    return None


def validate_execution(state):
    """每次执行包括审批恢复时重新检查，不能只依靠模型或旧审批状态。"""
    plan = ExecutionPlan.model_validate(state.ticket.execution_plan)
    understanding = saved_understanding(state)
    if state.planning:
        from .planning import validate_saved_plan
        current = validate_saved_plan(state)
    else:
        current = build_plan(state.ticket, understanding)
    if plan != current or plan.action != state.ticket.recommended_action:
        raise ValueError("execution plan no longer matches request")
    needs_confirmation = understanding.requires_confirmation
    if state.planning:
        needs_confirmation = plan.action in {"reset_password", "grant_access"} and (
            state.planning.get("source") != "rule" or state.planning.get("requires_confirmation")
            or understanding.requires_confirmation)
    if needs_confirmation:
        confirmation = state.confirmation or {}
        if (confirmation.get("status") != "confirmed" or confirmation.get("plan_digest") != plan.digest()
                or confirmation.get("input_digest") != input_digest(state.ticket, state.conversation_history)
                or confirmation.get("by") != (state.ticket.owner_id or state.ticket.requester)):
            raise ValueError("current plan requires owner confirmation")
    if plan.action == "grant_access":
        approval = state.approval
        if (not approval or approval.status != "approved" or approval.ticket_id != state.ticket.id
                or approval.action != plan.action or approval.plan_digest != plan.digest()
                or approval.plan != plan.model_dump() or not approval.decided_by
                or approval.decided_by == (state.ticket.owner_id or state.ticket.requester)):
            raise ValueError("matching independent approval required")
    return plan
