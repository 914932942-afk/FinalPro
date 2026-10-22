"""澄清结果契约与保守的离线完整性检查，不执行任何业务动作。"""
from dataclasses import asdict, dataclass, field
import re
from .policy import analyze_request, password_issue

from .errors import ModelOutputError


FIELD_QUESTIONS = {
    "previous_access": "您之前能正常使用这个系统，还是第一次申请使用？请说明具体情况。",
    "intent": "请说明希望完成什么操作，或遇到了什么具体问题。",
    "target": "请选择具体资源：ERP、VPN 或报表数据库。其他资源请说明，以便人工处理。",
    "symptom": "请描述具体表现或报错文字（不要提供密码、验证码或密钥）。",
    "requester": "请提供申请人标识（不要提供密码或验证码）。",
    "department": "请提供所属部门。",
    "permission": "请说明申请的权限：ERP/VPN 的普通使用，或报表数据库的只读权限。",
    "confirmation": "请明确现在希望执行的操作；如需修改，可以直接说明“不是……，是……”或“改成……”。不再需要可取消工单。",
    "single_intent": "本单包含多个诉求，请选择本次要处理的一个，其余另建工单；可以回答“改成：完整诉求”。",
    "single_resource": "一张工单只处理一个资源，请明确本次具体资源和操作。",
}


def is_password_reset_issue(text: str) -> bool:
    """仅把明确重置诉求或明确凭据故障交给离线密码流程。"""
    return password_issue(text) and not re.search(r"不要|不需要|不用|不是|don't|do not", text.lower())


@dataclass
class ClarificationDecision:
    need_clarify: bool
    missing_fields: list[str] = field(default_factory=list)
    source: str = "rule"
    metadata: dict = field(default_factory=dict)
    interpretation: dict | None = None

    def __post_init__(self):
        if self.interpretation is not None:
            from .policy import IntentProposal
            try:
                IntentProposal.model_validate(self.interpretation)
            except ValueError:
                raise ModelOutputError("意图提取结果无效") from None
        if type(self.need_clarify) is not bool:
            raise ModelOutputError("need_clarify 必须为布尔值")
        if (not isinstance(self.missing_fields, list)
                or len(self.missing_fields) > len(FIELD_QUESTIONS)
                or any(not isinstance(x, str) or x not in FIELD_QUESTIONS for x in self.missing_fields)
                or len(set(self.missing_fields)) != len(self.missing_fields)
                or self.need_clarify != bool(self.missing_fields)):
            raise ModelOutputError("澄清字段无效或与 need_clarify 不一致")

    @property
    def question(self):
        # 面向用户的问题来自受控模板，模型只能选择业务字段。
        return " ".join(FIELD_QUESTIONS[name] for name in self.missing_fields)

    def to_dict(self):
        return {**asdict(self), "question": self.question}

    @classmethod
    def from_model(cls, raw):
        if not isinstance(raw, dict) or set(raw) not in ({"need_clarify", "missing_fields"}, {"need_clarify", "missing_fields", "interpretation"}):
            raise ModelOutputError("澄清输出字段无效")
        return cls(**raw)


def check_offline(ticket, history=()):
    understanding = analyze_request(ticket, history)
    missing = list(understanding.missing)
    if understanding.blocked_reason:
        missing = ["confirmation"]
    if not ticket.requester.strip():
        missing.append("requester")
    if not ticket.department.strip():
        missing.append("department")
    return ClarificationDecision(bool(missing), missing)
