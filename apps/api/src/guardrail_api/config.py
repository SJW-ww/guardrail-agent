"""应用配置。所有可变项走环境变量,不硬编码。"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from guardrail_api.domain.trust import TrustLevel


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "GuardRail"
    app_env: Literal["local", "test", "staging", "prod"] = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://guardrail:guardrail@localhost:5432/guardrail"
    redis_url: str = "redis://localhost:6379/0"

    cors_origins: str = "http://localhost:3000"

    # 治理层参数(W2 起启用)
    lease_seconds: int = 30
    heartbeat_interval_seconds: int = 10
    max_step_retries: int = 3

    # --- 规划器(W3)---
    # 默认 deterministic:没有模型也能把整条链路跑通,CI 不依赖外部服务。
    # 切成 llm 才会真的发请求 —— 这也让"模型出问题"和"工程出问题"能分开定位。
    planner_backend: Literal["deterministic", "llm"] = "deterministic"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    llm_temperature: float = 0.0
    # 模型输出不合规时,把校验错误回喂给它重试几次(不是重试网络,是重试*表达*)
    llm_max_repair_attempts: int = 2

    # --- 策略引擎(W3)---
    # 信任等级授予给**执行体**,不是给这次操作打的危险分。危险分是工具的 risk_level。
    # 默认 L2:低风险可逆操作自动执行,高风险仍强制人工审批 —— 演示环境够用且不危险;
    # 银行这类场景应该把默认档压到 L1 甚至 L0。
    policy_default_trust_level: TrustLevel = TrustLevel.L2
    # 按执行体单独授权,格式 `agent:refund-bot=L4,operator-01=L2`,未列出的一律走默认档
    policy_actor_trust_levels: str = ""
    # 只有 L4 用得上:高风险工具在「幂等 + 可补偿 + 未超此额度」时可以自动执行
    policy_l4_high_risk_limit_cents: int = 20000
    # 是否允许机器执行体(agent: 前缀)批准人工审批步骤。
    # 默认不允许:让机器人给自己的同类签字,等于把审批这道闸门拆了。
    policy_allow_agent_approval: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_local(self) -> bool:
        return self.app_env in {"local", "test"}

    @property
    def sqlalchemy_echo(self) -> bool:
        return False

    @property
    def llm_configured(self) -> bool:
        """规划器选了 llm 且凭据齐全,才算真的可用。"""
        return self.planner_backend == "llm" and bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
