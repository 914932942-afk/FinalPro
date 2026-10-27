"""统一配置：显式参数 > 进程环境变量 > 项目 .env > 默认值。启动时加载并校验。"""
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_", env_file=ROOT / ".env", env_file_encoding="utf-8",
        extra="forbid", frozen=True, hide_input_in_errors=True, populate_by_name=True,
        allow_inf_nan=False,
    )
    data_dir: Path = ROOT / "data"
    db_path: Path | None = None
    kb_dir: Path | None = None
    chroma_dir: Path | None = None
    auth_file: Path | None = None
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8080, ge=1, le=65535)
    llm_mode: Literal["auto", "deepseek", "offline"] = "auto"
    llm_strict: bool = False
    clarification_max_rounds: int = Field(default=3, ge=1, le=10)
    decision_min_confidence: float = Field(default=0.8, ge=0, le=1)
    planning_use_sop: bool = True
    require_action_confirmation: bool = False
    approval_ttl_hours: float = Field(default=24, gt=0, le=168)
    submission_completed_window_seconds: int = Field(default=600, ge=0, le=86400)
    diagnosis_mode: Literal["off", "react", "fixed", "rule"] = "react"
    diagnosis_backend: Literal["structured", "investigation"] = "investigation"
    investigation_file: Path | None = None
    runbooks_file: Path | None = None
    diagnosis_file: Path | None = None
    diagnosis_deadline: float = Field(default=60, gt=0, le=120)
    diagnosis_prefetch: bool = True
    diagnosis_background: bool = True
    diagnosis_max_attempts: int = Field(default=3, ge=1, le=10)
    diagnosis_max_questions: int = Field(default=2, ge=1, le=5)
    diagnosis_worker_timeout: float = Field(default=90, gt=0, le=600)
    diagnosis_queue_timeout: float = Field(default=120, gt=0, le=600)
    diagnosis_worker_max_runs: int = Field(default=3, ge=1, le=5)
    diagnosis_worker_poll: float = Field(default=5, gt=0, le=30)
    diagnosis_workers: int = Field(default=2, ge=1, le=8)
    case_user_reply_hours: float = Field(default=48, gt=0, le=720)
    case_user_reminder_hours: float = Field(default=24, gt=0, le=720)
    case_verification_hours: float = Field(default=72, gt=0, le=720)
    case_verification_reminder_hours: float = Field(default=24, gt=0, le=720)
    case_reopen_days: float = Field(default=7, gt=0, le=90)
    runbooks_dir: Path | None = None
    runbook_strategy: Literal["bm25", "vector", "hybrid"] = "hybrid"
    deepseek_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", validation_alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="deepseek-flash", min_length=1, validation_alias="DEEPSEEK_MODEL")
    deepseek_timeout: float = Field(default=30, gt=0, le=300, validation_alias="DEEPSEEK_TIMEOUT")
    deepseek_max_retries: int = Field(default=2, ge=0, le=5, validation_alias="DEEPSEEK_MAX_RETRIES")
    deepseek_temperature: float = Field(default=0.1, ge=0, le=2, validation_alias="DEEPSEEK_TEMPERATURE")
    rag_backend: Literal["chroma", "lexical"] = "chroma"
    rag_hybrid: bool = True
    # 检索后的安全增强：语义去重只在有可用 embedding 时启用；
    # rerank 默认 optional，模型未预置时保留 RRF 结果并记录降级原因。
    rag_semantic_dedup: bool = True
    rag_semantic_dedup_threshold: float = Field(default=0.94, gt=0, le=1)
    rag_rerank_mode: Literal["off", "optional", "required"] = "optional"
    rag_reranker_model: str = "BAAI/bge-reranker-base"
    rag_rerank_candidate_k: int = Field(default=12, ge=1, le=100)
    # Cross-Encoder 分数没有跨模型统一标尺；该阈值只在 reranker 实际运行时生效。
    rag_rerank_min_score: float = -100.0
    embedding_model: str | None = None
    rag_chunk_size: int = Field(default=500, ge=1, le=10000)
    rag_overlap: int = Field(default=80, ge=0)
    rag_top_k: int = Field(default=3, ge=1, le=50)
    rag_batch_size: int = Field(default=64, ge=1, le=1000)
    llm_failure_threshold: int = Field(default=3, ge=1, le=100)
    llm_recovery_timeout: float = Field(default=30, ge=0, le=3600)
    rag_failure_threshold: int = Field(default=3, ge=1, le=100)
    rag_recovery_timeout: float = Field(default=30, ge=0, le=3600)
    tool_failure_threshold: int = Field(default=3, ge=1, le=100)
    tool_recovery_timeout: float = Field(default=30, ge=0, le=3600)
    sqlite_timeout: float = Field(default=5, gt=0, le=120)
    operation_retry_after: int = Field(default=3, ge=1, le=300)
    action_backend: Literal["simulated", "http_incidents"] = "simulated"
    action_base_url: str = "http://127.0.0.1:8091"
    action_api_key: SecretStr = SecretStr("")
    action_timeout: float = Field(default=5, gt=0, le=60)

    @model_validator(mode="after")
    def validate_and_resolve(self):
        endpoint = urlsplit(self.action_base_url)
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError("invalid action service URL")
        if endpoint.scheme == "http" and endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("remote action service requires HTTPS")
        if self.action_backend == "http_incidents" and len(self.action_api_key.get_secret_value()) < 32:
            raise ValueError("HTTP action service requires an independent key of at least 32 characters")
        if self.rag_overlap >= self.rag_chunk_size:
            raise ValueError("AGENT_RAG_OVERLAP must be smaller than AGENT_RAG_CHUNK_SIZE")
        url = urlsplit(self.deepseek_base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("DEEPSEEK_BASE_URL must be an HTTP(S) URL without credentials")
        if self.llm_mode == "deepseek" and not self.deepseek_api_key.get_secret_value().strip():
            raise ValueError("DEEPSEEK_API_KEY is required in deepseek mode")
        def absolute(path):
            return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
        data = absolute(self.data_dir)
        object.__setattr__(self, "data_dir", data)
        for field, default in (("db_path", data / "agent.db"), ("kb_dir", data / "knowledge"),
                               ("chroma_dir", data / "chroma"), ("auth_file", data / "auth.json"),
                               ("diagnosis_file", data / "diagnostics/vpn.json"),
                               ("investigation_file", data / "diagnostics/it_events.json"),
                               ("runbooks_file", data / "diagnostics/runbooks.json"),
                               ("runbooks_dir", (self.kb_dir or data / "knowledge") / "runbooks")):
            object.__setattr__(self, field, absolute(getattr(self, field) or default))
        object.__setattr__(self, "deepseek_base_url", self.deepseek_base_url.rstrip("/"))
        if self.embedding_model:
            candidate = ROOT / self.embedding_model
            if candidate.is_dir():
                object.__setattr__(self, "embedding_model", str(candidate.resolve()))
        return self


def get_settings(**overrides) -> Settings:
    # 不做全局缓存：每次构造应用读取一次；已运行的应用持有不可变快照。
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        fields = ["/".join(map(str, item["loc"])) or "configuration" for item in exc.errors()]
        # 不插入验证器的输入值或异常原文，防止包含 API key。
        raise ConfigurationError("配置校验失败：" + ", ".join(fields)) from None
    except (OSError, ValueError):
        raise ConfigurationError("配置文件无法读取或格式不正确") from None


# 旧脚本导入常量仍可用，新入口统一注入 Settings。
def __getattr__(name):
    mapping = {"LLM_MODE": "llm_mode", "LLM_STRICT": "llm_strict"}
    field = mapping.get(name, name.lower())
    if field not in Settings.model_fields:
        raise AttributeError(name)
    value = getattr(get_settings(), field)
    return value.get_secret_value() if isinstance(value, SecretStr) else value
