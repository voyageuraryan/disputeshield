"""Merchant evidence uploads (Phase 12). Merchant-only (main.py adds require_merchant).

  GET    /api/disputes/{id}/uploads                  the merchant's uploads (not removed ones)
  POST   /api/disputes/{id}/uploads                  multipart: evidence_type + file (PDF/JPG/PNG < 10 MB) and/or fact
                                                     -> stored, read by Gemini (suggested fields), status PENDING
  POST   /api/disputes/{id}/uploads/{uid}/confirm    JSON fields the merchant confirmed/edited -> CONFIRMED, fresh run
  DELETE /api/disputes/{id}/uploads/{uid}            remove (file deleted); a fresh run if it was evidence
  GET    /api/disputes/{id}/uploads/{uid}/file       the stored file (the dashboard fetches it with the session header)
Nothing becomes evidence before the merchant confirms; the new proposal supersedes the old PROPOSED one.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app import merchant_uploads as mu
from app.api.disputes import _find, check_can_change, start_fresh_run
from app.db import get_engine, get_session
from app.models import MerchantUpload, UploadStatus

router = APIRouter(tags=["uploads"])


class UploadOut(BaseModel):
    id: int
    evidence_type: str
    status: str
    kind: str                      # "file" | "fact"
    original_name: str | None
    mime: str | None
    size: int | None
    fact: str | None
    extraction: dict | None        # suggestions read by Gemini (advisory)
    fields: dict | None            # what the merchant confirmed
    flags: list[str] | None
    summary: str | None
    created_at: datetime
    confirmed_at: datetime | None


class RunOut(BaseModel):
    started: bool
    run_key: str | None = None
    message: str


class ConfirmOut(BaseModel):
    upload: UploadOut
    run: RunOut


def upload_out(u: MerchantUpload) -> UploadOut:
    return UploadOut(id=u.id, evidence_type=u.evidence_type, status=u.status.value,
                     kind="file" if u.file_path or u.mime else "fact", original_name=u.original_name, mime=u.mime,
                     size=u.size, fact=u.fact, extraction=u.extraction, fields=u.fields, flags=u.flags,
                     summary=u.summary, created_at=u.created_at, confirmed_at=u.confirmed_at)


def _get(session: Session, dispute_id: str, upload_id: int):  # noqa: ANN202
    d = _find(session, dispute_id)
    up = session.get(MerchantUpload, upload_id)
    if up is None or up.dispute_id != d.id:
        raise HTTPException(404, "unknown upload")
    return d, up


def _rerun(session: Session, d, background: BackgroundTasks, engine: Engine, why: str) -> RunOut:  # noqa: ANN001
    from datetime import datetime, timezone

    from app.agents import runner

    try:
        key = start_fresh_run(session, d, background, engine, why)
    except HTTPException as exc:  # a run is going (or the case just changed): the evidence is saved anyway
        follow = f"run:{d.paypal_dispute_id}:manual:{datetime.now(timezone.utc).isoformat()}"
        if exc.status_code == 409 and runner.defer(d.id, follow):  # G110: picked up when that run ends
            return RunOut(started=False, run_key=follow,
                          message="The agents are busy with this case; they'll re-analyse with the new evidence "
                                  "as soon as the current run finishes.")
        return RunOut(started=False, message=f"{exc.detail} Re-analyse when it's done to use the new evidence.")
    return RunOut(started=True, run_key=key, message="The agents are re-analysing the case with the new evidence.")


@router.get("/api/disputes/{dispute_id}/uploads", response_model=list[UploadOut])
def list_uploads(dispute_id: str, session: Session = Depends(get_session)) -> list[UploadOut]:
    d = _find(session, dispute_id)
    rows = session.exec(select(MerchantUpload).where(MerchantUpload.dispute_id == d.id,
                                                     MerchantUpload.status != UploadStatus.DISCARDED)
                        .order_by(MerchantUpload.id)).all()
    return [upload_out(u) for u in rows]


@router.post("/api/disputes/{dispute_id}/uploads", response_model=UploadOut, status_code=201)
async def create_upload(dispute_id: str, request: Request, session: Session = Depends(get_session)) -> UploadOut:
    d = _find(session, dispute_id)
    check_can_change(session, d)
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):  # urlencoded: fact only
        raise HTTPException(415, "Send multipart/form-data with evidence_type and a file and/or a fact.")
    if int(request.headers.get("content-length") or 0) > mu.MAX_BYTES + 64 * 1024:
        raise HTTPException(413, "PayPal accepts evidence files under 10 MB. Save a smaller copy.")
    form = await request.form(max_files=1, max_fields=3)
    try:
        etype, fact, f = form.get("evidence_type"), form.get("fact"), form.get("file")
        data = name = None
        if f is not None and not isinstance(f, str):
            data, name = await f.read(), f.filename
    finally:
        await form.close()
    try:
        up, created = await run_in_threadpool(mu.store, session, d, etype if isinstance(etype, str) else "",
                                              data=data, name=name, fact=fact if isinstance(fact, str) else None)
    except mu.UploadRejected as exc:
        raise HTTPException(413 if "10 MB" in str(exc) else 422, str(exc)) from exc
    if created:
        await mu.extract(session, up)
        session.refresh(up)
    return upload_out(up)


@router.post("/api/disputes/{dispute_id}/uploads/{upload_id}/confirm", response_model=ConfirmOut)
def confirm_upload(dispute_id: str, upload_id: int, body: mu.ConfirmFields, background: BackgroundTasks,
                   session: Session = Depends(get_session), engine: Engine = Depends(get_engine)) -> ConfirmOut:
    d, up = _get(session, dispute_id, upload_id)
    check_can_change(session, d)
    if up.status == UploadStatus.CONFIRMED:
        raise HTTPException(409, "Already confirmed. Remove it and upload again to change it.")
    try:
        up = mu.confirm(session, up, body)
    except mu.UploadRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    run = _rerun(session, d, background, engine,
                 f"Added evidence ({up.evidence_type.replace('_', ' ')}); asked the agents to re-analyse")
    return ConfirmOut(upload=upload_out(up), run=run)


@router.delete("/api/disputes/{dispute_id}/uploads/{upload_id}", response_model=RunOut)
def delete_upload(dispute_id: str, upload_id: int, background: BackgroundTasks,
                  session: Session = Depends(get_session), engine: Engine = Depends(get_engine)) -> RunOut:
    d, up = _get(session, dispute_id, upload_id)
    if up.status == UploadStatus.DISCARDED:
        return RunOut(started=False, message="Already removed.")
    if up.status == UploadStatus.CONFIRMED:
        check_can_change(session, d)
    if mu.discard(session, up):
        return _rerun(session, d, background, engine, "Removed evidence; asked the agents to re-analyse")
    return RunOut(started=False, message="Removed.")


@router.get("/api/disputes/{dispute_id}/uploads/{upload_id}/file")
def upload_file(dispute_id: str, upload_id: int, session: Session = Depends(get_session)) -> FileResponse:
    _, up = _get(session, dispute_id, upload_id)
    if not up.file_path:
        raise HTTPException(404, "no file for this upload")
    return FileResponse(up.file_path, media_type=up.mime, filename=up.original_name or None,
                        content_disposition_type="inline", headers={"Cache-Control": "private, no-store"})
