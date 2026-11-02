import os
import tempfile

# DeepEval (dev dependency; the eval-metric unit tests import it) autoloads .env files into os.environ on import and
# sends telemetry: switch both off before anything can import it. Its pytest plugin is disabled in pytest.ini.
os.environ.update(DEEPEVAL_DISABLE_DOTENV="1", DEEPEVAL_TELEMETRY_OPT_OUT="1", CONFIDENT_TRACE_VERBOSE="0")

# Isolate tests from any real backend/.env and on-disk DB.
os.environ.update(
    PAYPAL_CLIENT_ID="test-client",
    PAYPAL_CLIENT_SECRET="test-secret",
    PAYPAL_BASE_URL="https://api-m.sandbox.paypal.com",
    DATABASE_URL="sqlite://",
    PAYPAL_WEBHOOK_ID="0NH55953DH663215D",
    POLL_DISPUTES="false",
    GROQ_API_KEY="test-groq-key",
    GEMINI_API_KEY="test-gemini-key",
    LLM_PROVIDERS="groq,gemini",
    AUTH_MODE="paypal",
    AUTH_SECRET="test-secret-test-secret-test-secret-123",
    MERCHANT_LOGIN_EMAILS="merchant@business.example.com",
    FRONTEND_URL="http://localhost:5173",
    WEBHOOK_RECORD_DIR="",
    CHECKPOINT_DB=os.path.join(tempfile.mkdtemp(prefix="ds-ckpt-"), "checkpoints.db"),
)

import pytest  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402

# Tests must never read the developer's real backend/.env (keys, models, DB path).
Settings.model_config["env_file"] = None
get_settings.cache_clear()
from app.paypal.client import PayPalClient  # noqa: E402

BASE = "https://api-m.sandbox.paypal.com"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
async def pp(clock, sleeps):
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    settings = Settings(_env_file=None)
    client = PayPalClient(settings, sleep=fake_sleep, clock=clock)
    yield client
    await client.aclose()


def token_response(expires_in: int = 32400, token: str = "tok-1") -> dict:
    return {"scope": "x", "access_token": token, "token_type": "Bearer", "app_id": "APP-1", "expires_in": expires_in, "nonce": "n"}


def mock_investigation_lookups(router) -> None:
    """Default PayPal responses for the investigator's capture/order lookups (404 = not found),
    for tests about ingestion rather than investigation. Register specific routes first."""
    router.get(url__regex=rf"{BASE}/v2/payments/captures/.+").respond(404, json={"name": "RESOURCE_NOT_FOUND"})
    router.get(url__regex=rf"{BASE}/v2/checkout/orders/.+").respond(404, json={"name": "RESOURCE_NOT_FOUND"})


@pytest.fixture(autouse=True)
def reruns(monkeypatch):
    """Phase 9D: a finished troubleshooting session schedules an agent run. Tests record it instead of running
    the real agents (which would call PayPal), including when the app's lifespan installs the scheduler."""
    from app.troubleshooting import outcome

    calls: list[tuple[int, str]] = []
    record = lambda dispute_id, key, engine: calls.append((dispute_id, key))  # noqa: E731
    monkeypatch.setattr(outcome, "install", lambda loop: outcome.set_scheduler(record))
    outcome.set_scheduler(record)
    yield calls
    outcome.set_scheduler(None)


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """No test ever calls the real Anthropic API. Tests that care inject their own LLM."""
    from tests.fakes import ObedientLLM

    llm = ObedientLLM()
    monkeypatch.setattr("app.agents.llm.structured_call", llm)
    return llm


@pytest.fixture(autouse=True)
def logged_in_merchant(request):
    """Most tests are about disputes, not login: act as a logged-in merchant unless marked real_auth."""
    from app.auth import require_merchant
    from app.main import app

    if request.node.get_closest_marker("real_auth"):
        app.dependency_overrides.pop(require_merchant, None)
        yield
        return
    app.dependency_overrides[require_merchant] = lambda: {"sub": "test", "email": "merchant@business.example.com"}
    yield
    app.dependency_overrides.pop(require_merchant, None)
