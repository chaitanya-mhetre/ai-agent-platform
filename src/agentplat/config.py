from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment (prefix AGENTPLAT_)."""

    model_config = SettingsConfigDict(env_prefix="AGENTPLAT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./agentplat.db"
    redis_url: str | None = None  # None -> in-process queue + event bus (dev only)
    auto_create_schema: bool = True
    embedded_worker: bool = True  # run a worker inside the API process (dev only)
    worker_concurrency: int = 4
    lease_ttl_s: float = 30.0

    provider: str = "fake"  # fake | openai | anthropic | gemini
    model: str | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    anthropic_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None

    data_dir: str = "fixtures"  # sample DB, uploaded files, search index, offline web pages
    offline_web: bool = False  # serve http_get from fixtures/pages (*.test hosts) - eval/demo
    tool_secrets: dict[str, dict[str, str]] = {}  # {"tool_name": {"API_KEY": "..."}}
    bootstrap_admins: list[str] = []  # ["tenant:user", ...] granted admin at startup
    rate_limit_per_minute: int = 60
    pricing_file: str = "config/models.yaml"
    otel_endpoint: str | None = None  # e.g. http://otel-collector:4318/v1/traces
    worker_metrics_port: int = 9100

    def api_key_for(self, provider: str) -> str | None:
        secret = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "gemini": self.gemini_api_key,
        }.get(provider)
        return secret.get_secret_value() if secret else None
