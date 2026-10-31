"""Server-Sent Events: stream a dispute's agent_steps rows as the agents work.

GET /disputes/{id}/events (and /api/disputes/{id}/events for the Vite proxy)
- Each step is one SSE message: `id: <step id>`, `event: step`, `data: <StepOut JSON>`.
- On connect it first sends the steps the client hasn't seen, then `event: ready`: after `Last-Event-ID` (sent automatically by
  EventSource on reconnect) or `?after=<step id>`; with neither, the full backlog.
- Then polls the table every POLL_S and pushes new rows; a `: ping` comment every HEARTBEAT_S keeps proxies
  from closing an idle stream; `retry: 3000` tells the browser how fast to reconnect.
- Streams end after MAX_SECONDS (EventSource reconnects transparently with Last-Event-ID), and when the
  client disconnects. Single-process app: polling SQLite is simpler than a pub/sub and plenty fast here.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.db import get_engine
from app.models import AgentStep, Dispute

router = APIRouter(tags=["events"])

POLL_S = 1.0
HEARTBEAT_S = 15.0
MAX_SECONDS = 300
BATCH = 200


def _sse(step: AgentStep) -> str:
    data = {"id": step.id, "agent": step.agent, "action": step.action, "detail": step.detail,
            "created_at": step.created_at.isoformat()}
    return f"id: {step.id}\nevent: step\ndata: {json.dumps(data, default=str)}\n\n"


def _new_steps(engine: Engine, dispute_row_id: int, after: int) -> list[AgentStep]:
    with Session(engine) as s:
        return list(s.exec(select(AgentStep).where(AgentStep.dispute_id == dispute_row_id, AgentStep.id > after)
                           .order_by(AgentStep.id).limit(BATCH)).all())


@router.get("/disputes/{dispute_id}/events")
@router.get("/api/disputes/{dispute_id}/events", include_in_schema=False)
async def dispute_events(dispute_id: str, request: Request, engine: Engine = Depends(get_engine),
                         after: int | None = Query(default=None, ge=0),
                         max_seconds: float = Query(default=MAX_SECONDS, gt=0, le=MAX_SECONDS),
                         last_event_id: str | None = Header(default=None)) -> StreamingResponse:
    with Session(engine) as s:
        d = s.exec(select(Dispute).where(Dispute.paypal_dispute_id == dispute_id)).first()
    if d is None:
        raise HTTPException(404, "unknown dispute")
    cursor = after or 0
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))

    async def stream():
        nonlocal cursor
        started = last_beat = time.monotonic()
        yield "retry: 3000\n\n"
        first = True
        while True:
            if await request.is_disconnected():
                return
            for step in _new_steps(engine, d.id, cursor):
                cursor = step.id
                yield _sse(step)
            if first:  # backlog delivered: everything after this is live
                first = False
                yield f"event: ready\ndata: {json.dumps({'last_id': cursor})}\n\n"
            now = time.monotonic()
            if now - started >= max_seconds:
                yield "event: bye\ndata: {}\n\n"  # client reconnects with Last-Event-ID
                return
            if now - last_beat >= HEARTBEAT_S:
                last_beat = now
                yield ": ping\n\n"
            await asyncio.sleep(POLL_S)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
