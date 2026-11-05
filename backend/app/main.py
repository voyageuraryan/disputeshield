import asyncio
import contextlib
import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.auth import require_merchant
from app.api.decisions import router as decisions_router
from app.api.disputes import router as disputes_router
from app.api.events import router as events_router
from app.api.settings import router as settings_router
from app.api.store import router as store_router
from app.api.evals import router as evals_router
from app.api.uploads import router as uploads_router
from app.api.analytics import router as analytics_router
from app.api.demo import router as demo_router
from app.api.troubleshooting import buyer_router as help_router, merchant_router as troubleshooting_router
from app.api.webhooks import router as webhooks_router
from app.config import get_settings
from app import events_bus  # noqa: F401  (registers commit hooks that wake SSE streams)
from app.db import create_db_and_tables, engine
from app.paypal.client import close_paypal_client

log = logging.getLogger(__name__)

_TOKEN_IN_URL = re.compile(r"((?:^|[?&])(?:token|t)=)[^&\s]+")


class _RedactTokenFilter(logging.Filter):
    """Strip session/link tokens from uvicorn access-log lines (the buyer help link carries ?t=; old merchant links may still send ?token=)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "=" in args[2]:
            record.args = (*args[:2], _TOKEN_IN_URL.sub(r"\1[redacted]", args[2]), *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTokenFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    s = get_settings()
    from app.signals import check_key

    check_key()  # on Render, refuse to start with the public dev key for the hashed checkout signals
    if 0 < len(s.admin_token.get_secret_value()) < 32:
        # A warning, not a refusal, so an existing deployment keeps booting; rotate it to a long random string.
        log.warning("ADMIN_TOKEN is shorter than 32 characters; use a long random value")
    from sqlmodel import Session as _S

    from app.analytics.outcomes import backfill

    if s.auto_seed:  # Render free plan: the disk is ephemeral, so rebuild the demo data on an empty database
        from app.analytics.demo import auto_seed

        with _S(engine) as _s:
            log.info("auto-seed: %s", auto_seed(_s))
    with _S(engine) as _s:  # Phase 14 fix: disputes resolved before outcome capture existed get their analytics row
        if (n := backfill(_s)):
            log.info("analytics: backfilled %d resolved dispute outcome(s)", n)
    from app.troubleshooting import outcome

    outcome.install(asyncio.get_running_loop())  # 9D: a finished buyer session starts a new analysis
    from app.troubleshooting.retention import retention_loop

    retention = asyncio.create_task(retention_loop(engine), name="proof-retention")  # G44
    from app.alerts import alerts_loop

    alerts = asyncio.create_task(alerts_loop(engine), name="alerts")  # Phase 17
    poller = None
    if s.poll_disputes and s.paypal_client_id and s.paypal_client_secret.get_secret_value():
        from app.scripts.poll_disputes import poll_forever

        poller = asyncio.create_task(poll_forever(engine, s.poll_interval_s), name="dispute-poller")
    elif s.poll_disputes:
        log.warning("dispute poller disabled: PayPal credentials not configured")
    from app.mcp_server import mount as mcp_mount

    async with mcp_mount.start():  # Phase 17: the MCP server's session manager (fresh per lifespan)
        yield
    if poller:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
    for task in (retention, alerts):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    outcome.set_scheduler(None)
    await close_paypal_client()


def docs_enabled() -> bool:
    """Interactive API docs are for local development; on Render they'd publish the whole route map (unless
    DEMO_MODE is on for a demo)."""
    return not os.environ.get("RENDER") or get_settings().demo_mode


_docs = docs_enabled()
app = FastAPI(title="DisputeShield", lifespan=lifespan, docs_url="/docs" if _docs else None,
              redoc_url="/redoc" if _docs else None, openapi_url="/openapi.json" if _docs else None)
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),        # an uploaded file is served only as its sniffed-at-upload type
    (b"x-frame-options", b"DENY"),                  # nothing from the API is meant to be framed (clickjacking)
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"referrer-policy", b"no-referrer"),
]


class SecurityHeaders:
    """Pure ASGI (not BaseHTTPMiddleware) so streamed responses (SSE, files) keep streaming; adds the headers to every
    HTTP response that doesn't set them itself (security review 2026-11-05)."""

    def __init__(self, inner):  # noqa: ANN001
        self.inner = inner

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)

        async def send_with_headers(message):  # noqa: ANN001
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [h for h in SECURITY_HEADERS
                                                                         if h[0] not in have]
            await send(message)

        await self.inner(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Public: storefront (buyers), buyer troubleshooting links, PayPal webhooks (signature-verified), login, admin (own token), /health.
app.include_router(store_router)
app.include_router(webhooks_router)
app.include_router(auth_router)
app.include_router(admin_router)
# Buyer troubleshooting page: public, but each request needs that session's private link token.
app.include_router(help_router)
# Merchant-only: everything that shows cases or acts on them.
merchant = [Depends(require_merchant)]
app.include_router(disputes_router, dependencies=merchant)
app.include_router(decisions_router, dependencies=merchant)
app.include_router(decisions_router, prefix="/api", include_in_schema=False, dependencies=merchant)  # Vite proxy
app.include_router(events_router, dependencies=merchant)
app.include_router(settings_router, dependencies=merchant)
app.include_router(troubleshooting_router, dependencies=merchant)
app.include_router(uploads_router, dependencies=merchant)
app.include_router(analytics_router, dependencies=merchant)  # Phase 14
app.include_router(demo_router, dependencies=merchant)  # Phase 16: trust + demo mode (gated)  # Phase 12: merchant evidence uploads
app.include_router(evals_router, dependencies=merchant)  # Phase 10B: latest eval report (Trust page)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}

# Phase 17: the MCP server (streamable HTTP at /mcp + its OAuth protected-resource metadata). Mounted LAST at "/" so
# every API route above matches first; unknown paths fall through to it and get its 404.
from app.mcp_server import mount as _mcp_mount  # noqa: E402

app.mount("/", _mcp_mount)

