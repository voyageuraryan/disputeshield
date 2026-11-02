import asyncio
import contextlib
import logging
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
from app.api.troubleshooting import buyer_router as help_router, merchant_router as troubleshooting_router
from app.api.webhooks import router as webhooks_router
from app.config import get_settings
from app import events_bus  # noqa: F401  (registers commit hooks that wake SSE streams)
from app.db import create_db_and_tables, engine
from app.paypal.client import close_paypal_client

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    s = get_settings()
    from app.troubleshooting import outcome

    outcome.install(asyncio.get_running_loop())  # 9D: a finished buyer session starts a new analysis
    poller = None
    if s.poll_disputes and s.paypal_client_id and s.paypal_client_secret.get_secret_value():
        from app.scripts.poll_disputes import poll_forever

        poller = asyncio.create_task(poll_forever(engine, s.poll_interval_s), name="dispute-poller")
    elif s.poll_disputes:
        log.warning("dispute poller disabled: PayPal credentials not configured")
    yield
    if poller:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
    outcome.set_scheduler(None)
    await close_paypal_client()


app = FastAPI(title="DisputeShield", lifespan=lifespan)
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
app.include_router(evals_router, dependencies=merchant)  # Phase 10B: latest eval report (Trust page)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
