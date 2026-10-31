"""App settings, loaded from environment / backend/.env. Never hardcode secrets."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    paypal_client_id: str = ""
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_base_url: str = SANDBOX_BASE_URL
    # Webhook ID from developer.paypal.com > your app > Webhooks. Required to verify events.
    paypal_webhook_id: str = ""

    # Background dispute poller (safety net for slow/missed webhooks).
    poll_disputes: bool = True
    poll_interval_s: int = 60
    # Dev only: save raw webhook body + headers here, to record real test fixtures.
    webhook_record_dir: str = ""

    database_url: str = "sqlite:///./disputeshield.db"
    # LangGraph checkpoints (paused runs waiting for approval live here).
    checkpoint_db: str = "./checkpoints.db"
    settle_threshold: Decimal = Decimal("25")
    # Generated documents (evidence summary PDFs). On Render: /var/data/artifacts.
    artifacts_dir: str = "./artifacts"
    # Shared secret for /admin/sandbox/* demo helpers (X-Admin-Token header). Empty = helpers disabled.
    admin_token: SecretStr = SecretStr("")

    # LLMs: tried in LLM_PROVIDERS order; a provider without a key is skipped.
    llm_providers: str = "groq,gemini"
    # Replace buyer emails / addresses with placeholders before prompts leave the server.
    llm_redact_pii: bool = True
    groq_api_key: SecretStr = SecretStr("")
    groq_model: str = "openai/gpt-oss-120b"     # free plan, strict JSON-schema output
    gemini_api_key: SecretStr = SecretStr("")
    # Comma-separated, tried in order. gemini-3.8-flash often 503s ("high demand") on the free tier.
    gemini_model: str = "gemini-3.8-flash,gemini-2.5-flash"
    cors_origins: str = "http://localhost:5173"  # comma-separated

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @field_validator("paypal_base_url")
    @classmethod
    def sandbox_only(cls, v: str) -> str:
        # Hard rule 1: sandbox only.
        v = v.rstrip("/")
        if v != SANDBOX_BASE_URL:
            raise ValueError(f"PAYPAL_BASE_URL must be {SANDBOX_BASE_URL} (sandbox only)")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
