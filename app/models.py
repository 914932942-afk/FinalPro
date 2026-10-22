"""定义程序要传递的三种数据：工单、审批单、一次运行的状态。

可以把“类”理解成表格模板，把“对象”理解成按模板填写的一张表。
Ticket 是工单模板；Ticket(title=..., ...) 创建一张具体工单。

本文件只描述数据，不决定流程，也不负责存数据库。
workflow.py 填写这些对象，audit.py 保存它们的字典版本。
"""

# dataclass 自动生成初始化等方法，省去手写大量 self.title = title。
# field 用来指定字段的特殊默认值；asdict 把数据类对象转成字典。
from dataclasses import asdict, dataclass, field
# datetime 处理时间，timezone 提供 UTC 时区。
from datetime import datetime, timezone
# 这些是类型提示，帮助阅读和编辑器检查，不会自动验证用户输入。
# Dict[str, Any] = 键为字符串、值可为任意类型的字典。
# List[...] = 列表；Optional[str] = 字符串或者 None（暂时没有值）。
from typing import Any, Dict, List, Optional
# uuid4 生成完整随机标识；数据库唯一键保护重复操作。
from uuid import uuid4
from uuid import NAMESPACE_URL, uuid5
from .errors import ErrorInfo


def now_iso() -> str:
    """无输入，返回当前 UTC 时间文本，例如 2026-09-14T06:00:00+00:00。

    -> str 表示返回值预计是字符串。北京时间 = UTC 时间 + 8 小时。
    """
    # now() 取当前时间，isoformat() 把时间对象转换成方便存储的文本。
    return datetime.now(timezone.utc).isoformat()


# @ 开头的是装饰器，它会为下面的类补充行为。
# @dataclass 让我们可以写 Ticket(title="无法登录", description="...", ...)。
@dataclass
class Ticket:
    """一张服务工单：谁遇到了什么问题，系统准备怎么处理，当前到哪一步了。"""

    title: str  # 简短标题；没有默认值，创建对象时必须提供。
    description: str  # 详细问题描述。
    requester: str  # 申请人标识，示例里使用邮箱。
    department: str  # API/CLI 从认证身份获取；service 据此检查经理审批范围。
    severity: str = "medium"  # 严重程度，未传值时使用 medium。
    # default_factory 接收“生成默认值的函数”，每创建一张工单调用一次。
    # lambda 是没有名字的小函数；hex 转十六进制文本，保留完整 UUID。
    id: str = field(default_factory=lambda: f"T-{uuid4().hex.upper()}")
    category: str = "unclassified"  # plan 节点稍后填写，例如 access_request。
    status: str = "new"  # 工作流不断更新它，例如 awaiting_approval、closed。
    recommended_action: str = ""  # 计划节点填写的动作名称，例如 grant_access。
    # 这里传 now_iso 函数本身，不写 now_iso()，使每个对象取自己的创建时间。
    created_at: str = field(default_factory=now_iso)
    # 字段名叫更新时间，但当前实现仅在 finalize 节点重新赋值。
    updated_at: str = field(default_factory=now_iso)
    # 客户端可复用的幂等键；相同键在重复请求时返回同一张工单。
    idempotency_key: Optional[str] = None
    owner_id: Optional[str] = None  # 服务端认证身份；旧数据默认无可信归属。
    execution_plan: Optional[Dict[str, Any]] = None
    diagnostic_report: Optional[Dict[str, Any]] = None  # 服务端只读诊断附件，不是执行授权。
    investigation_status: str = "not_requested"
    feedback: List[Dict[str, Any]] = field(default_factory=list)
    assignment: Optional[Dict[str, Any]] = None
    case_revision: int = 0
    investigation_attempt: int = 0
    registration_context: Optional[Dict[str, Any]] = None  # 首次登记的用户证据快照；原始描述保留。
    investigation_job: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """返回普通字典，供 JSON 输出或数据库保存使用。"""
        # self 就是当前这张工单；asdict 会读取它的全部数据字段。
        return asdict(self)


@dataclass
class Approval:
    """审批单：记录哪张工单的哪个动作在等待人工决定。"""

    ticket_id: str  # 指向 Ticket.id，将审批单和工单关联起来。
    action: str  # 待审批动作，例如 grant_access。
    reason: str  # 为什么需要审批，展示给读者或审核人。
    required_role: str = "manager"  # service 校验实际审批角色；admin 可审批 manager 级别。
    # A- 前缀用来把审批编号和 T- 开头的工单编号区分开。
    id: str = field(default_factory=lambda: f"A-{uuid4().hex.upper()}")
    status: str = "pending"  # pending 等待；approved 同意；rejected 拒绝。
    decided_by: Optional[str] = None  # 尚未审批时没有审批人，使用 None。
    decided_at: Optional[str] = None  # 审批发生后才记录时间。
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

    ticket: Ticket  # 这里装的是 Ticket 对象，读取标题写 state.ticket.title。
    user_role: str = "employee"  # API/CLI 从服务端身份配置获取，不接受客户端自报。
    # 每个状态都要有自己的列表，所以用 default_factory=list。
    # 不能让多张工单共享同一个可修改的列表，否则内容会串在一起。
    retrieved_docs: List[Dict[str, Any]] = field(default_factory=list)
    # decision 保存 Decision.to_dict() 的结果，避免 models.py 反向依赖 model_adapter.py。
    decision: Optional[Dict[str, Any]] = None
    approval: Optional[Approval] = None  # 不需审批时为空。
    result: str = ""  # execute 节点填写工具返回的文本。
    trace: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None  # 模型严格模式/白名单失败；工具故障写入 degradation。
    llm_error: Optional[str] = None  # 非严格模式下记录异常类型，便于知道发生了回退。
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
    investigation_history: List[Dict[str, Any]] = field(default_factory=list)
    investigation_questions: int = 0
    enrichment_result: Optional[Dict[str, Any]] = None
    workflow_version: int = 1  # Missing on historical snapshots; new requests explicitly use v2.

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
        # append() 在列表末尾增加一项。这份 trace 暂时只在内存中。
        self.trace.append({"node": node, "at": now_iso(), "detail": detail})

    def to_dict(self) -> Dict[str, Any]:
        """把状态组织成能被 json.dumps() 序列化的嵌套字典。"""
        # Ticket 和 Approval 是自定义对象，需要分别调用 to_dict()。
        # 列表、字符串、None 已经是 JSON 编码器支持的类型。
        return {
            "ticket": self.ticket.to_dict(),
            "user_role": self.user_role,
            "retrieved_docs": self.retrieved_docs,
            "decision": self.decision,
            # 条件表达式：有审批对象就转字典，没有就放 None（JSON 中为 null）。
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
            "investigation_history": self.investigation_history,
            "investigation_questions": self.investigation_questions,
            "enrichment_result": self.enrichment_result,
            "workflow_version": self.workflow_version,
        }
