"""Domain models exchanged between workflow nodes and the persistence layer."""

from dataclasses import asdict, dataclass, field

from datetime import datetime, timezone

from typing import Any, Dict, List, Optional

from uuid import uuid4
from uuid import NAMESPACE_URL, uuid5
from .errors import ErrorInfo

def now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""

    return datetime.now(timezone.utc).isoformat()

@dataclass
class Ticket:
    """一张服务工单：谁遇到了什么问题，系统准备怎么处理，当前到哪一步了。"""

    title: str
    description: str
    requester: str
    department: str
    severity: str = "medium"

    id: str = field(default_factory=lambda: f"T-{uuid4().hex.upper()}")
    category: str = "unclassified"
    status: str = "new"
    recommended_action: str = ""

    created_at: str = field(default_factory=now_iso)

    updated_at: str = field(default_factory=now_iso)

    idempotency_key: Optional[str] = None
    owner_id: Optional[str] = None
    execution_plan: Optional[Dict[str, Any]] = None
    diagnostic_report: Optional[Dict[str, Any]] = None
    investigation_status: str = "not_requested"
    feedback: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """返回普通字典，供 JSON 输出或数据库保存使用。"""

        return asdict(self)

@dataclass
class Approval:
    """审批单：记录哪张工单的哪个动作在等待人工决定。"""

    ticket_id: str
    action: str
    reason: str
    required_role: str = "manager"

    id: str = field(default_factory=lambda: f"A-{uuid4().hex.upper()}")
    status: str = "pending"
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    plan: Optional[Dict[str, Any]] = None
    plan_digest: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """将审批对象转成字典，保存前由工具层调用。"""
        return asdict(self)

@dataclass(frozen=True)
class ActionResult:
    """可持久化的工具结果；旧 result 字符串继续由 message 提供。"""
    action: str
    status: str
    message: str
    idempotency_key: str
    execution_id: str = ""
    downstream_id: Optional[str] = None
    simulated: bool = False
    error: Optional[ErrorInfo] = None

    def __post_init__(self):
        if self.status not in {"succeeded", "rejected", "manual_review", "unknown"}:
            raise ValueError("invalid action result status")
        if not self.action or not self.idempotency_key or not isinstance(self.message, str):
            raise ValueError("invalid action result")
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("successful action cannot contain error")
        if not self.execution_id:
            object.__setattr__(self, "execution_id", "E-" + uuid5(NAMESPACE_URL, self.idempotency_key).hex)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, raw):
        data = dict(raw)
        if data.get("error"):
            data["error"] = ErrorInfo(**data["error"])
        return cls(**data)

@dataclass
class AgentState:
    """一次 Agent 运行的共享记录，各节点通过它传递处理结果。

    ticket 是业务数据；retrieved_docs 是检索结果；decision 是模型的结构化决定；
    result 是工具或流程的最终回复。trace 只记录节点名、时间和少量摘要，当前没有
    自动回放执行功能。llm_error 让接口调用方知道是否发生了模型回退。
    """

    ticket: Ticket
    user_role: str = "employee"

    retrieved_docs: List[Dict[str, Any]] = field(default_factory=list)

    decision: Optional[Dict[str, Any]] = None
    approval: Optional[Approval] = None
    result: str = ""
    trace: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    llm_error: Optional[str] = None
    degradation: List[Dict[str, Any]] = field(default_factory=list)
    action_result: Optional[ActionResult] = None
    error_info: Optional[ErrorInfo] = None
    conversation_history: List[Dict[str, Any]] = field(default_factory=list)
    clarification: Optional[Dict[str, Any]] = None
    clarification_round: int = 0
    clarification_max_rounds: int = 3
    clarified: bool = False
    next_node: Optional[str] = "intake"
    understanding: Optional[Dict[str, Any]] = None
    resolution: Optional[Dict[str, Any]] = None
    confirmation: Optional[Dict[str, Any]] = None
    planning: Optional[Dict[str, Any]] = None
    planning_questions: List[Dict[str, Any]] = field(default_factory=list)
    diagnosis: Optional[Dict[str, Any]] = None
    enrichment_result: Optional[Dict[str, Any]] = None
    workflow_version: int = 1

    @classmethod
    def from_dict(cls, raw):
        data = dict(raw)
        data["ticket"] = Ticket(**data["ticket"])
        if data.get("approval"):
            data["approval"] = Approval(**data["approval"])
        if data.get("action_result"):
            data["action_result"] = ActionResult.from_dict(data["action_result"])
        if data.get("error_info"):
            data["error_info"] = ErrorInfo(**data["error_info"])
        return cls(**data)

    def record(self, node: str, detail: Dict[str, Any]) -> None:
        """把一个节点的运行摘要追加到本次 trace 中；返回 None。"""

        self.trace.append({"node": node, "at": now_iso(), "detail": detail})

    def to_dict(self) -> Dict[str, Any]:
        """把状态组织成能被 json.dumps() 序列化的嵌套字典。"""

        return {
            "ticket": self.ticket.to_dict(),
            "user_role": self.user_role,
            "retrieved_docs": self.retrieved_docs,
            "decision": self.decision,

            "approval": self.approval.to_dict() if self.approval else None,
            "result": self.result,
            "trace": self.trace,
            "error": self.error,
            "llm_error": self.llm_error,
            "degradation": self.degradation,
            "action_result": self.action_result.to_dict() if self.action_result else None,
            "error_info": self.error_info.to_dict() if self.error_info else None,
            "conversation_history": self.conversation_history,
            "clarification": self.clarification,
            "clarification_round": self.clarification_round,
            "clarification_max_rounds": self.clarification_max_rounds,
            "clarified": self.clarified,
            "next_node": self.next_node,
            "understanding": self.understanding,
            "resolution": self.resolution,
            "confirmation": self.confirmation,
            "planning": self.planning,
            "planning_questions": self.planning_questions,
            "diagnosis": self.diagnosis,
            "enrichment_result": self.enrichment_result,
            "workflow_version": self.workflow_version,
        }
