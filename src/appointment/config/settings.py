"""应用配置。

设计稿 §12.4 / §4.1：首版是模块化单体，FastAPI 业务入口嵌入 SDK；配置显式区分
local_dev / test / pilot / production。pilot 与 production 缺少身份配置时启动失败，
不提供 X-User-ID 回退。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local_dev", "test", "pilot", "production"]
ModelBackend = Literal["stub", "dashscope", "deepseek"]
AgentRuntime = Literal["deterministic", "agentscope"]
NotificationProvider = Literal["none", "sandbox"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APPOINTMENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------- 数据库 ----------------
    database_url: str = (
        "postgresql+asyncpg://appointment:appointment@127.0.0.1:5433/appointment"
    )
    db_pool_size: int = 8
    db_max_overflow: int = 4
    db_echo: bool = False

    # ---------------- 运行环境 ----------------
    env: Environment = "local_dev"

    # ---------------- 身份 ----------------
    allow_temp_identity: bool = True
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None

    # ---------------- 模型 ----------------
    model_backend: ModelBackend = "stub"
    model_name: str = "qwen-plus"

    # ---------------- Agent 运行时 ----------------
    agent_runtime: AgentRuntime = "deterministic"

    # ---------------- 业务参数 ----------------
    hold_ttl_seconds: int = 180
    turn_budget_seconds: float = 20.0
    max_tool_calls: int = 6
    max_consult_delegations: int = 2
    timezone: str = "Asia/Shanghai"

    #: 每客户最多一个有效普通占位（设计稿 §7 的明确配额政策，不是通用必然要求）。
    max_active_holds_per_customer: int = 1

    # ---------------- 通知 ----------------
    notification_provider: NotificationProvider = "sandbox"
    notification_sandbox_failure_rate: float = 0.0

    # ---------------- SSE ----------------
    #: task_event 保留窗口。窗口外恢复返回 RESET_REQUIRED。
    sse_retention_events: int = 500
    sse_retention_seconds: int = 3600

    # ---------------- Worker ----------------
    worker_lease_seconds: int = 30
    worker_batch_size: int = 32
    worker_poll_interval_seconds: float = 1.0
    outbox_max_attempts: int = 8
    delivery_reconcile_deadline_seconds: int = 900

    # ---------------- 安全 ----------------
    #: 报价数据签名密钥。真实部署必须来自秘密管理，不写入代码库。
    quote_token_secret: SecretStr = Field(
        default_factory=lambda: SecretStr("dev-only-quote-signing-key")
    )
    #: 关联用 HMAC 密钥，避免可枚举手机号的裸哈希。
    pseudonymization_key: SecretStr = Field(
        default_factory=lambda: SecretStr("dev-only-pseudonymization-key")
    )

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        # 生产环境不接受临时身份头，也不接受开发默认密钥。
        if self.env in ("pilot", "production"):
            if self.allow_temp_identity:
                raise ValueError(
                    f"{self.env} 环境不允许 X-User-ID 临时身份，必须配置 OIDC"
                )
            missing = [
                name
                for name, value in (
                    ("oidc_issuer", self.oidc_issuer),
                    ("oidc_audience", self.oidc_audience),
                    ("oidc_jwks_url", self.oidc_jwks_url),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"{self.env} 环境缺少身份配置：{', '.join(missing)}"
                )
        if not self.database_url.startswith("postgresql"):
            raise ValueError(
                "核心不变量依赖 PostgreSQL 的 btree_gist 区间排他约束，"
                f"当前 database_url 不是 PostgreSQL：{self.database_url}"
            )
        if self.hold_ttl_seconds <= 0:
            raise ValueError("hold_ttl_seconds 必须为正")
        return self

    @property
    def sync_database_url(self) -> str:
        """Alembic 等同步场景使用的 URL。"""

        return self.database_url.replace("+asyncpg", "+psycopg2")

    @property
    def is_local(self) -> bool:
        return self.env in ("local_dev", "test")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试夹具在改环境变量后调用。"""

    get_settings.cache_clear()
