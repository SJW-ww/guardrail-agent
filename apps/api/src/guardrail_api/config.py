"""应用配置。所有可变项走环境变量,不硬编码。"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_local(self) -> bool:
        return self.app_env in {"local", "test"}

    @property
    def sqlalchemy_echo(self) -> bool:
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
