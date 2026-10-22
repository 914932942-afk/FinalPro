"""稳定错误分类。对外只返回固定文案，不返回底层异常、响应正文或密钥。"""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    category: str
    message: str
    retryable: bool = False
    outcome_unknown: bool = False

    def to_dict(self):
        return asdict(self)


class AgentError(RuntimeError):
    code = "internal_error"
    category = "internal"
    public_message = "内部错误，请核查审计记录。"
    http_status = 500
    retryable = False
    circuit_failure = True
    outcome_unknown = False

    def info(self):
        return ErrorInfo(self.code, self.category, self.public_message,
                         self.retryable, self.outcome_unknown)


class ConfigurationError(AgentError):
    code, category = "configuration_error", "configuration"
    public_message = "配置不合法，请检查配置字段。"
    circuit_failure = False


class AuthenticationError(AgentError):
    code, category = "unauthenticated", "authentication"
    public_message = "缺少或无效的身份凭据。"
    http_status = 401
    circuit_failure = False


class AuthorizationError(AgentError):
    code, category = "forbidden", "authorization"
    public_message = "没有此操作的权限。"
    http_status = 403
    circuit_failure = False


class ResourceNotFoundError(AgentError):
    code, category = "not_found", "business"
    public_message = "记录不存在或无权访问。"
    http_status = 404
    circuit_failure = False


class RequestValidationError(AgentError):
    code, category = "invalid_request", "validation"
    public_message = "请求字段不符合要求。"
    http_status = 422
    circuit_failure = False


class InvalidActionError(AgentError):
    code, category = "invalid_action", "business"
    public_message = "动作不在允许列表内。"
    http_status = 422
    circuit_failure = False


class ActionRejectedError(AgentError):
    code, category = "action_rejected", "business"
    public_message = "下游拒绝此动作，请人工核查。"
    http_status = 422
    circuit_failure = False


class IdempotencyConflict(AgentError):
    code, category = "idempotency_conflict", "conflict"
    public_message = "同一幂等键不能用于不同请求。"
    http_status = 409
    circuit_failure = False


class OperationPending(AgentError):
    code, category = "operation_pending", "conflict"
    public_message = "请求正在处理或结果不明，请查询状态并核查审计，勿更换键重做。"
    http_status = 409
    circuit_failure = False
    outcome_unknown = True


class CircuitOpenError(AgentError):
    code, category = "circuit_open", "dependency"
    public_message = "下游熔断器已打开，本次调用未发出。"
    http_status = 503
    retryable = True
    circuit_failure = False


class StorageError(AgentError):
    code, category = "storage_error", "storage"
    public_message = "存储访问失败，请核查状态后处理。"
    http_status = 503
    circuit_failure = False
    outcome_unknown = True


class ToolTimeoutError(AgentError):
    code, category = "tool_timeout", "dependency"
    public_message = "下游工具超时，执行结果不明，请人工对账。"
    http_status = 504
    outcome_unknown = True


class ToolUnavailableError(AgentError):
    code, category = "tool_unavailable", "dependency"
    public_message = "下游工具不可用，请人工核查。"
    http_status = 503
    outcome_unknown = True


class ToolResultError(AgentError):
    code, category = "tool_result_invalid", "contract"
    public_message = "下游返回不符合动作结果协议，请人工核查。"
    outcome_unknown = True


class ModelError(AgentError):
    code, category = "model_error", "dependency"
    public_message = "模型调用失败。"
    http_status = 502


class ModelConfigurationError(ModelError):
    code, category = "model_configuration_error", "configuration"
    public_message = "模型配置错误或鉴权失败。"
    circuit_failure = False


class ModelAPIError(ModelError):
    code = "model_api_error"
    public_message = "模型服务请求失败。"


class ModelUnavailableError(ModelAPIError):
    code = "model_unavailable"
    public_message = "模型服务暂时不可用。"
    retryable = True


class ModelTimeoutError(ModelAPIError):
    code = "model_timeout"
    public_message = "模型服务请求超时。"
    http_status = 504
    retryable = True


class ModelRateLimitError(ModelAPIError):
    code = "model_rate_limited"
    public_message = "模型服务限流。"
    http_status = 503
    retryable = True


class ModelOutputError(ModelError):
    code, category = "model_output_invalid", "contract"
    public_message = "模型输出不符合约定。"


def describe_error(exc: Exception, component: str = "") -> ErrorInfo:
    if isinstance(exc, AgentError):
        return exc.info()
    if component == "tool":
        if isinstance(exc, TimeoutError):
            return ToolTimeoutError().info()
        # 工具调用中的未知异常不能证明没有发生副作用。
        return ErrorInfo("tool_execution_unknown", "internal", "工具执行结果不明，请人工核查。",
                         outcome_unknown=True)
    if component == "rag":
        return ErrorInfo("retrieval_unavailable", "dependency", "向量检索不可用，已使用词法检索。",
                         retryable=True)
    if component == "llm" and isinstance(exc, TimeoutError):
        return ModelTimeoutError().info()
    return AgentError().info()
