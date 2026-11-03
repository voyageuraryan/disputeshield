"""App settings, loaded from environment / backend/.env. Never hardcode secrets."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"
SANDBOX_WEB_URL = "https://www.sandbox.paypal.com"  # browser-facing pages (Log in with PayPal)
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
    # Merchant login (Log in with PayPal). auth_mode "off" is for local development only and is refused
    # when running on Render (RENDER env var set).
    auth_mode: str = "paypal"
    auth_secret: SecretStr = SecretStr("")          # HMAC key for session tokens + OAuth state
    auth_redirect_uri: str = "http://localhost:8000/api/auth/paypal/callback"  # must match the PayPal app setting
    frontend_url: str = "http://localhost:5173"
    merchant_login_emails: str = ""                 # comma-separated PayPal emails allowed to log in
    session_hours: int = 12
    # Shared secret for /admin/sandbox/* demo helpers (X-Admin-Token header). Empty = helpers disabled.
    admin_token: SecretStr = SecretStr("")

    # LLMs: tried in LLM_PROVIDERS order; a provider without a key is skipped.
    llm_providers: str = "groq,gemini"
    # Replace buyer emails / addresses with placeholders before prompts leave the server.
    llm_redact_pii: bool = True
    # Sampling for the structured LLM calls (decisions, notes, plans). Providers default to temperature 1.0, which
    # made the same dispute get different recommendations between runs (seen in the 10C evals). Groq: temperature
    # 0-2 and a best-effort seed; Gemini: seed, but temperature is ignored on Gemini 3.6 Flash and newer.
    llm_temperature: float = 0.0
    llm_seed: int = 7
    groq_api_key: SecretStr = SecretStr("")
    groq_model: str = "openai/gpt-oss-120b"     # free plan, strict JSON-schema output
    gemini_api_key: SecretStr = SecretStr("")
    # Optional dedicated Gemini keys per role (each its own project + quota). Unset -> GEMINI_API_KEY. A role never
    # switches to another role's key when its own quota runs out: that job just stops (no failover between keys).
    research_gemini_api_key: SecretStr = SecretStr("")    # web research (troubleshooting plans, eval fixture)
    evals_judge_gemini_api_key: SecretStr = SecretStr("")  # eval judge (backend/evals/judge.py)
    evals_simulator_gemini_api_key: SecretStr = SecretStr("")  # eval simulated buyer (Phase 10C)

    def gemini_key_for(self, role: str) -> str:
        """research | judge | simulator | app -> that role's key, else GEMINI_API_KEY."""
        own = {"research": self.research_gemini_api_key, "judge": self.evals_judge_gemini_api_key,
               "simulator": self.evals_simulator_gemini_api_key}.get(role)
        return (own.get_secret_value() if own else "") or self.gemini_api_key.get_secret_value()
    # Comma-separated, tried in order. gemini-3.8-flash often 503s ("high demand") on the free tier.
    gemini_model: str = "gemini-3.8-flash,gemini-2.5-flash"
    cors_origins: str = "http://localhost:5173"  # comma-separated

    # Phase 9B: troubleshooting research. Gemini (GEMINI_API_KEY) with Google Search grounding first, then
    # Tavily search (optional; free tier 1,000 credits/month). RESEARCH_MODEL defaults to GEMINI_MODEL.
    tavily_api_key: SecretStr = SecretStr("")
    research_model: str = ""
    # Base URL for buyer troubleshooting links (defaults to FRONTEND_URL). dev.sh sets it to a public tunnel
    # so a buyer on another device can open the link while the merchant dashboard stays on localhost.
    buyer_portal_url: str = ""
    # Phase 9C: buyer proof uploads (stored under ARTIFACTS_DIR/ts/{session_id}/). Images are re-encoded to
    # JPEG/PNG <= 10 MB (PayPal's per-file evidence limit) without GPS; videos are limited by size and length.
    proof_image_max_bytes: int = 10 * 1024 * 1024
    proof_video_max_bytes: int = 50 * 1024 * 1024
    proof_video_max_seconds: int = 30
    # G44: delete buyer proof files this many days after the dispute is resolved (0 = keep them).
    proof_retention_days: int = 90
    # Advisory Gemini look at each proof photo (shown to the merchant, never decides anything). Off by default:
    # on Gemini's free tier, inputs may be read by human reviewers and Google asks not to send personal
    # information, and buyer photos can show people or homes. Turn on with a paid key or the buyer's consent.
    proof_vision_check: bool = False
    vision_model: str = ""  # defaults to the first GEMINI_MODEL
    # Shown in the buyer page header.
    store_name: str = "Demo store"
    # Phase 9D: a finished buyer troubleshooting session starts a new analysis of the dispute.
    troubleshooting_reanalyse: bool = True

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
