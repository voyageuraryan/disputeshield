import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.decisions import router as decisions_router
from app.api.disputes import router as disputes_router
from app.api.store import router as store_router
from app.api.webhooks import router as webhooks_router
from app.config import get_settings
from app.db import create_db_and_tables, engine
from app.paypal.client import close_paypal_client

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    s = get_settings()
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
    await close_paypal_client()


app = FastAPI(title="DisputeShield", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(store_router)
app.include_router(disputes_router)
app.include_router(decisions_router)
app.include_router(decisions_router, prefix="/api", include_in_schema=False)  # for the Vite proxy
app.include_router(admin_router)
app.include_router(webhooks_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
