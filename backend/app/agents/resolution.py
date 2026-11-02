"""Resolution: turn a PROPOSED decision into the exact PayPal request, then (after approval) execute it.

resolve()  (graph node "resolve", before the approval interrupt)
  FIGHT   LLM drafts a factual seller note (< 1,000 chars, cites only case evidence ids; retry once,
          then NEEDS_HUMAN - rule 7). A PDF evidence summary is generated (ReportLab): page 1 the case,
          page 2 (Phase 9D, when the buyer finished troubleshooting) the troubleshooting record and up to 3
          of the buyer's proof photos. PayPal: jpg/png/pdf, < 10 MB per file, <= 50 MB per call
          (paypal/disputes.py); the PDF is kept under 10 MB (photos are shrunk, else left out).
          Request: provide-evidence (when PayPal allows it) with PROOF_OF_FULFILLMENT + tracking_info,
          else provide-supporting-info (case under PayPal review), both with the PDF attached;
          else, in an inquiry that takes no evidence, escalate (note only) so PayPal reviews it as a claim.
  SETTLE  accept-claim with a short templated note.
  GATHER_MORE  nothing to send; the merchant may approve with a different action or reject.

execute_decision()  (graph node "execute", after approval)
  Refuses unless decision.status == APPROVED (rule 5), calls PayPal, stores paypal_response,
  sets EXECUTED or FAILED, logs every step.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.tracing import observe
from app.agents import llm as llm_module
from app.agents.llm import LLMError, StructuredLLM
from app.agents.redact import contains_placeholder, pii_terms, redact
from app.agents.steps import log_step
from app.config import get_settings
from app.models import Decision, DecisionStatus, Dispute, Order, Shipment
from app.paypal.client import PayPalClient, PayPalError
from app.paypal.disputes import (
    Attachment,
    NotApprovedError,
    accept_claim,
    escalate,
    provide_evidence,
    provide_supporting_info,
)
from app.schemas import CaseFile, SellerNote

log = logging.getLogger(__name__)
AGENT = "resolution"
NOTE_MAX_CHARS = 1000
MAX_ATTEMPTS = 2
NEEDS_HUMAN = "NEEDS_HUMAN"

# PayPal tracking_info.carrier_name enum values we map store carriers onto; anything else -> OTHER.
PAYPAL_CARRIERS = {"USPS", "UPS", "FEDEX", "DHL"}
# Evidence type when PayPal hasn't requested a specific one (Disputes v1 evidence_type enum).
EVIDENCE_TYPE_BY_REASON = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "PROOF_OF_FULFILLMENT",
    "UNAUTHORISED": "PROOF_OF_FULFILLMENT",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "ITEM_DESCRIPTION",
}

NOTE_SYSTEM_PROMPT = f"""You write the seller's note that a small merchant sends to PayPal to contest a dispute.
Write in the first person as the merchant, plainly and factually, for a PayPal dispute reviewer.
- State only facts that appear in the case file evidence; never invent tracking numbers, dates,
  addresses, messages or anything else. Cite the integer ids of the evidence items you rely on.
- Lead with the strongest proof (for example delivery tracking), then supporting facts.
- Never claim more than the evidence shows. "Shipped" is not "delivered": say the item was delivered
  or received ONLY if the case file has delivery_confirmation evidence.
- At most {NOTE_MAX_CHARS} characters. No greetings, no threats, no speculation about the buyer.
- Personal data is replaced by placeholders such as [email] and [shipping address]. Do not write any
  placeholder in the note; say "the buyer's shipping address" instead."""


# ---- seller note (LLM) ---------------------------------------------------------------

DELIVERY_CLAIM = re.compile(r"\b(delivered|deliver(y|ies) (was|were) (made|completed)|received|receipt of|arrived)\b",
                            re.IGNORECASE)


def validate_note(n: SellerNote, case: CaseFile) -> list[str]:
    problems = []
    has_delivery_proof = any(e.type == "delivery_confirmation" for e in case.evidence)
    if not has_delivery_proof and (m := DELIVERY_CLAIM.search(n.note)):
        problems.append(f"note claims delivery/receipt ({m.group(0)!r}) but the case has no delivery_confirmation "
                        f"evidence; say only that it was shipped")
    if not n.note.strip():
        problems.append("note is empty")
    if len(n.note) > NOTE_MAX_CHARS:
        problems.append(f"note is {len(n.note)} characters; the limit is {NOTE_MAX_CHARS}")
    unknown = sorted(set(n.cited_evidence_ids) - case.evidence_ids)
    if unknown:
        problems.append(f"note cites unknown evidence ids: {unknown}")
    if not n.cited_evidence_ids:
        problems.append("note must cite the evidence it relies on")
    if (ph := contains_placeholder(n.note)):
        problems.append(f"note contains the placeholder {ph}; describe it in words instead")
    return problems


@observe(type="llm")
async def draft_seller_note(case: CaseFile, reasoning: str, llm: StructuredLLM,
                            pii: dict[str, str] | None = None) -> tuple[SellerNote | None, list[str]]:
    import json

    prompt = redact("Draft the seller note for this dispute.\n\n" + json.dumps(
        {"case_file": case.model_dump(mode="json"), "analyst_reasoning": reasoning}, indent=2, sort_keys=True,
        default=str), pii or {})
    problems: list[str] = []
    for _ in range(MAX_ATTEMPTS):
        user = prompt if not problems else prompt + "\n\nYour previous note was rejected; fix:\n- " + "\n- ".join(problems)
        try:
            note = await llm(system=NOTE_SYSTEM_PROMPT, user=user, output_model=SellerNote)
        except LLMError as exc:
            problems = [f"LLM call failed: {exc}"]
            continue
        problems = validate_note(note, case)
        if not problems:
            return note, []
    return None, problems


def settle_note(case: CaseFile) -> str:
    return (f"We accept this claim and agree to refund {case.amount} {case.currency} for dispute "
            f"{case.dispute_id}. Thank you for your patience.")


# ---- PDF evidence summary (ReportLab) -------------------------------------------------------

PDF_IMAGE_SIDE = 1000      # px: proof photos are shrunk for the PDF (stored copies are up to 2560 px)
PDF_MAX_BYTES = 10 * 1024 * 1024  # PayPal: each file < 10 MB


def _pdf_image(path: str, max_w: float, max_h: float):
    """A shrunk JPEG copy of a proof photo as a ReportLab flowable (None if it can't be read)."""
    import io

    from PIL import Image as PILImage
    from reportlab.platypus import Image

    try:
        with PILImage.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((PDF_IMAGE_SIDE, PDF_IMAGE_SIDE))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            w, h = im.size
    except Exception:  # noqa: BLE001 - a missing/broken file just isn't included
        return None
    buf.seek(0)
    scale = min(max_w / w, max_h / h, 1.0)
    return Image(buf, width=w * scale, height=h * scale)


def build_evidence_pdf(path: Path, case: CaseFile, note: str, *, order: Order | None = None,
                       troubleshooting=None) -> Path:  # noqa: ANN001  (outcome.PdfSection | None)
    pdf = _build_pdf(path, case, note, order, troubleshooting, images=True)
    if troubleshooting is not None and pdf.stat().st_size >= PDF_MAX_BYTES:
        pdf = _build_pdf(path, case, note, order, troubleshooting, images=False)  # PayPal: < 10 MB per file
    return pdf


def _build_pdf(path: Path, case: CaseFile, note: str, order: Order | None, ts, *, images: bool) -> Path:  # noqa: ANN001
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import KeepInFrame, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8, leading=10)
    p = lambda t, st=small: Paragraph(escape(str(t)), st)  # noqa: E731

    rows = [[p("#"), p("Type"), p("Source"), p("What it shows")]]
    for e in case.evidence[:14]:
        rows.append([p(e.id), p(e.type), p(e.source), p(e.summary[:330])])
    table = Table(rows, colWidths=[0.35 * inch, 1.35 * inch, 0.6 * inch, 4.9 * inch], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
                               ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    facts = [f"Dispute: {case.dispute_id}", f"Reason: {case.reason}",
             f"Amount: {case.amount} {case.currency}", f"Stage/status: {case.stage or '-'} / {case.status or '-'}"]
    if order is not None:
        facts.append(f"Store order #{order.id}, invoice {order.invoice_id or '-'}, placed {order.created_at:%d %b %Y}")
    story = [Paragraph("Seller evidence summary", styles["Title"]),
             p(" · ".join(facts)), Spacer(1, 8),
             Paragraph("Seller statement", styles["Heading3"]), p(note), Spacer(1, 8),
             Paragraph("Evidence", styles["Heading3"]), table, Spacer(1, 6),
             p(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by DisputeShield from the merchant's "
               f"PayPal and store records.")]
    doc = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch, title=f"Evidence {case.dispute_id}")
    # KeepInFrame(mode="shrink") guarantees one page per section: content shrinks rather than overflowing.
    pages = [KeepInFrame(doc.width, doc.height, story, mode="shrink")]
    if ts is not None:
        rows = [[p("Step"), p("What the buyer was asked to do"), p("Buyer's answer")]]
        rows += [[p(o), p(t), p(a)] for o, t, a in ts.steps]
        steps = Table(rows, colWidths=[0.5 * inch, 4.4 * inch, 2.3 * inch], repeatRows=1)
        steps.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
                                   ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        second = [Paragraph(escape(ts.title), styles["Title"])] + [p(line) for line in ts.lines] + [Spacer(1, 8), steps]
        pics = [(img, cap) for path_, cap in (ts.images if images else [])
                if (img := _pdf_image(path_, doc.width / 2, 3.2 * inch)) is not None]
        if pics:
            second += [Spacer(1, 8), Paragraph("Buyer's photos", styles["Heading3"])]
            for img, cap in pics:
                second += [img, p(cap), Spacer(1, 6)]
        elif ts.images and not images:
            second.append(p("The buyer's photos are left out to keep this file under PayPal's 10 MB limit."))
        pages += [PageBreak(), KeepInFrame(doc.width, doc.height, second, mode="shrink")]
    doc.build(pages)
    return path


# ---- request builders (deterministic; reused when the merchant edits the note or action) ----

def _tracking_info(session: Session, order_id: int | None) -> list[dict]:
    if not order_id:
        return []
    info = []
    for sh in session.exec(select(Shipment).where(Shipment.order_id == order_id)).all():
        if not sh.tracking_number:
            continue
        carrier = (sh.carrier or "").upper()
        entry = {"tracking_number": sh.tracking_number}
        if carrier in PAYPAL_CARRIERS:
            entry["carrier_name"] = carrier
        else:
            entry["carrier_name"] = "OTHER"
            entry["carrier_name_other"] = sh.carrier or "unknown"
        info.append(entry)
    return info


def _requested_evidence_type(raw: dict | None) -> str | None:
    for e in (raw or {}).get("evidences") or []:
        if e.get("source") == "REQUESTED_FROM_SELLER" and e.get("evidence_type"):
            return e["evidence_type"]
    return None


def build_action_request(session: Session, dispute: Dispute, case: CaseFile, action: str, note: str,
                         pdf_path: str | None) -> dict[str, Any]:
    """The exact PayPal call execute will make. Raises ValueError if PayPal doesn't allow it now."""
    attachments = [{"path": pdf_path, "content_type": "application/pdf"}] if pdf_path else []
    if action == "FIGHT":
        if "provide_evidence" in case.allowed_actions:
            ev_type = _requested_evidence_type(dispute.raw) or EVIDENCE_TYPE_BY_REASON.get(case.reason, "OTHER")
            evidence: dict[str, Any] = {"evidence_type": ev_type, "notes": note}
            tracking = _tracking_info(session, case.order_id)
            if tracking and ev_type in ("PROOF_OF_FULFILLMENT", "ADDITIONAL_PROOF_OF_SHIPMENT",
                                        "ONLINE_TRACKING_INFORMATION"):
                evidence["evidence_info"] = {"tracking_info": tracking}
            return {"endpoint": "provide-evidence", "evidences": [evidence], "attachments": attachments}
        if "provide_supporting_info" in case.allowed_actions:
            return {"endpoint": "provide-supporting-info", "notes": note, "attachments": attachments}
        if "escalate" in case.allowed_actions:
            # Inquiry where PayPal takes no evidence yet: ask PayPal to review it as a claim (note only; the
            # escalate request has no file part). PayPal then asks for evidence and we analyse again.
            return {"endpoint": "escalate", "note": note}
        raise ValueError(f"PayPal allows no way to contest this dispute now (allowed: {case.allowed_actions})")
    if action == "SETTLE":
        if "accept_claim" not in case.allowed_actions:
            raise ValueError(f"PayPal does not allow accepting the claim now (allowed: {case.allowed_actions})")
        return {"endpoint": "accept-claim", "note": note}
    raise ValueError(f"{action} has nothing to send to PayPal")


def pdf_path_for(decision_id: int) -> Path:
    return Path(get_settings().artifacts_dir) / f"evidence-decision-{decision_id}.pdf"


# ---- graph node: resolve ---------------------------------------------------------------

@observe(type="agent")
async def resolve(session: Session, dispute: Dispute, case: CaseFile, decision_id: int, *,
                  llm: StructuredLLM | None = None) -> dict[str, Any]:
    llm = llm or llm_module.structured_call
    decision = session.get(Decision, decision_id)
    action = decision.recommendation
    order = session.get(Order, case.order_id) if case.order_id else None

    if action == "GATHER_MORE":
        log_step(session, dispute.id, AGENT, "Nothing to send to PayPal yet: approve with FIGHT or SETTLE, or reject",
                 {"decision_id": decision.id})
        return {"needs_human": False, "action_request": None}

    if action == "FIGHT":
        note, problems = await draft_seller_note(case, decision.reasoning, llm, pii_terms(session, case.order_id))
        if note is None:
            dispute.agent_status = NEEDS_HUMAN
            session.add(dispute)
            session.commit()
            log_step(session, dispute.id, AGENT, "Needs human review: could not draft a valid seller note: "
                     + "; ".join(problems), {"decision_id": decision.id, "problems": problems})
            return {"needs_human": True, "action_request": None}
        log_step(session, dispute.id, AGENT, f"Drafted seller note ({len(note.note)} chars)",
                 {"decision_id": decision.id, "note": note.note, "cited_evidence_ids": note.cited_evidence_ids,
                  "llm": llm_module.last_provider.get()})
        from app.troubleshooting.outcome import pdf_section

        ts = pdf_section(session, dispute.id)
        pdf = build_evidence_pdf(pdf_path_for(decision.id), case, note.note, order=order, troubleshooting=ts)
        log_step(session, dispute.id, AGENT,
                 f"Generated the evidence summary PDF ({pdf.stat().st_size // 1024} KB"
                 + (f"; with the buyer's troubleshooting record and {len(ts.images)} photo(s)" if ts else "") + ")",
                 {"decision_id": decision.id, "path": str(pdf), "troubleshooting": bool(ts)})
        text, pdf_path = note.note, str(pdf)
    else:  # SETTLE
        text, pdf_path = settle_note(case), None

    try:
        request = build_action_request(session, dispute, case, action, text, pdf_path)
    except ValueError as exc:
        dispute.agent_status = NEEDS_HUMAN
        session.add(dispute)
        session.commit()
        log_step(session, dispute.id, AGENT, f"Needs human review: {exc}", {"decision_id": decision.id})
        return {"needs_human": True, "action_request": None}
    decision.draft_note = text
    decision.action_request = request
    session.add(decision)
    session.commit()
    log_step(session, dispute.id, AGENT, f"Prepared PayPal {request['endpoint']} request; waiting for approval",
             {"decision_id": decision.id, "request": request})
    return {"needs_human": False, "action_request": request}


# ---- graph node: execute ------------------------------------------------------------------

def paypal_issues(body: Any) -> list[str]:
    """PayPal error issue codes, e.g. ["EVIDENCE_TYPE_IS_NOT_ALLOWED"]."""
    if not isinstance(body, dict):
        return []
    return [d.get("issue") for d in body.get("details") or [] if isinstance(d, dict) and d.get("issue")]


def definitely_not_processed(response: dict | None) -> bool:
    """True when PayPal rejected the call outright (4xx): nothing happened at PayPal, so a corrected
    retry cannot double-submit. 5xx / timeouts are ambiguous and must be checked in PayPal first."""
    r = response or {}
    code = r.get("status_code")
    return bool(r.get("error")) and isinstance(code, int) and 400 <= code < 500


def without_tracking(request: dict) -> dict | None:
    """Documented alternative for PROOF_OF_FULFILLMENT: notes/documents instead of tracking_info
    (developer.paypal.com/platforms/disputes/reference/dispute-reasons). None if nothing to drop."""
    if request.get("endpoint") != "provide-evidence":
        return None
    evs = request.get("evidences") or []
    if not any("evidence_info" in e for e in evs):
        return None
    return {**request, "evidences": [{k: v for k, v in e.items() if k != "evidence_info"} for e in evs]}


async def _send(decision: Decision, pid: str, req: dict, client: PayPalClient | None) -> dict:
    attachments = [Attachment(a["path"], a.get("content_type", "application/pdf")) for a in req.get("attachments", [])]
    if req.get("endpoint") == "provide-evidence":
        return await provide_evidence(decision, pid, req["evidences"], attachments, client=client)
    if req.get("endpoint") == "provide-supporting-info":
        return await provide_supporting_info(decision, pid, req["notes"], attachments, client=client)
    if req.get("endpoint") == "accept-claim":
        return await accept_claim(decision, pid, req["note"], client=client)
    if req.get("endpoint") == "escalate":
        return await escalate(decision, pid, req["note"], client=client)
    raise ValueError(f"decision {decision.id} has no PayPal request to execute")


async def execute_decision(session: Session, decision_id: int, *, client: PayPalClient | None = None) -> Decision:
    decision = session.get(Decision, decision_id)
    if decision is None:
        raise NotApprovedError(f"decision {decision_id} does not exist")
    dispute = session.get(Dispute, decision.dispute_id)
    if decision.status != DecisionStatus.APPROVED:  # rule 5, enforced here and again in paypal/disputes.py
        log_step(session, dispute.id, "executor", f"Refused to execute: decision is {decision.status.value}, not APPROVED",
                 {"decision_id": decision.id})
        raise NotApprovedError(f"decision {decision.id} is {decision.status.value}, not APPROVED")
    req = decision.action_request or {}
    pid = dispute.paypal_dispute_id
    log_step(session, dispute.id, "executor", f"Sending {req.get('endpoint')} to PayPal for {pid}",
             {"decision_id": decision.id})
    try:
        try:
            resp = await _send(decision, pid, req, client)
        except PayPalError as exc:
            alt = without_tracking(req) if "EVIDENCE_TYPE_IS_NOT_ALLOWED" in paypal_issues(exc.body) else None
            if alt is None:
                raise
            # 400 = PayPal processed nothing, so the documented alternative (same evidence type, note + PDF,
            # no tracking block) can be sent once without any risk of a double submission.
            log_step(session, dispute.id, "executor",
                     f"PayPal refused the evidence with tracking (EVIDENCE_TYPE_IS_NOT_ALLOWED, debug id "
                     f"{exc.debug_id}); resending the same evidence type with the note and PDF only",
                     {"decision_id": decision.id})
            req = alt
            decision.action_request = alt
            resp = await _send(decision, pid, req, client)
    except PayPalError as exc:
        decision.status = DecisionStatus.FAILED
        decision.paypal_response = {"error": True, "status_code": exc.status_code, "debug_id": exc.debug_id,
                                    "issues": paypal_issues(exc.body),
                                    "body": exc.body if isinstance(exc.body, dict) else str(exc.body)}
        dispute.agent_status = "FAILED"
        session.add_all([decision, dispute])
        session.commit()
        unsure = exc.status_code >= 500
        issues = paypal_issues(exc.body)
        log_step(session, dispute.id, "executor",
                 f"PayPal rejected {req.get('endpoint')} (HTTP {exc.status_code}"
                 + (f", {', '.join(issues)}" if issues else "") + f", debug id {exc.debug_id})"
                 + ("; outcome unknown, check the dispute in PayPal before retrying" if unsure
                    else "; nothing was processed, so it can be retried"),
                 {"decision_id": decision.id, "paypal_response": decision.paypal_response})
        return decision
    except (ValueError, OSError, Exception) as exc:  # transport errors, missing PDF, ...
        decision.status = DecisionStatus.FAILED
        decision.paypal_response = {"error": True, "message": str(exc)}
        dispute.agent_status = "FAILED"
        session.add_all([decision, dispute])
        session.commit()
        log_step(session, dispute.id, "executor", f"Could not send {req.get('endpoint')}: {exc}; "
                 "outcome unknown if the request was in flight, check the dispute in PayPal",
                 {"decision_id": decision.id})
        return decision
    decision.status = DecisionStatus.EXECUTED
    decision.executed_at = datetime.now(timezone.utc)
    decision.paypal_response = resp or {}
    dispute.agent_status = "EXECUTED"
    session.add_all([decision, dispute])
    session.commit()
    log_step(session, dispute.id, "executor", f"PayPal accepted {req.get('endpoint')} for {pid}",
             {"decision_id": decision.id, "paypal_response": decision.paypal_response})
    return decision
