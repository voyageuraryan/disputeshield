"""Simulated buyers vs the REAL troubleshooting agent (Phase 10C).

DeepEval's ConversationSimulator plays the buyer (persona + scenario, simulator model = the Gemini judge, never the
model under test); its async model_callback drives our real agent on a private in-memory troubleshooting session:
  - text goes to troubleshooter.agent_reply (the model under test, Groq, through the shared rate limiter);
  - the page's buttons and upload box are simulated: the buyer writes tokens such as [button: Yes, it works now]
    or [upload photo] and the harness calls exactly what the page calls (confirm_fixed, step_button, handoff,
    record_proof). Code, not the LLM, owns every outcome, so these are part of what's being tested;
  - each assistant turn = the agent's new messages + a [screen] block describing the page (stripped before scoring).
A stopping controller ends the conversation as soon as the session reaches a final status (deterministic; no LLM
call per turn). Web research is served from evals/goldens/fixtures/research.json (real results recorded once with
`python -m evals.record_research`), except for the live-search smoke golden. Results are cached (golden, prompts,
fixture, model under test, simulator model) in evals/.cache/conversations/.
"""

from __future__ import annotations

import io
import json
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.agents import troubleshooter as T
from app.agents.web_research import ResearchNotes
from app.catalog import PRODUCTS, order_item
from app.db import create_db_and_tables
from app.models import Dispute, Order, TroubleshootingSession, TSMessage, TSProof, TSStatus, TSStep
from evals.cache import DiskCache, digest
from evals.goldens.conversations import ConvGolden

FIXTURE = Path(__file__).parent / "goldens" / "fixtures" / "research.json"
SKU = {"earbuds": "EARBUDS-01", "lamp": "LAMP-01"}
FINAL = {s.value for s in TSStatus} - {"INVITED", "ACTIVE", "AWAITING_PROOF"}
STEP_BUTTON = re.compile(r"\[button:\s*Step\s*(\d+)\s*:\s*(Done, it works now|Done, still not working|I can'?t do this)\s*\]",
                         re.IGNORECASE)
CONFIRM = re.compile(r"\[button:\s*(Yes, it works now|No, still not working)\s*\]", re.IGNORECASE)
HANDOFF = re.compile(r"\[button:\s*Talk to the seller\s*\]", re.IGNORECASE)
UPLOAD = re.compile(r"\[upload\s+(photo|video)\]", re.IGNORECASE)
SCREEN = re.compile(r"\n*\[screen\].*\Z", re.DOTALL)
STEP_STATUS = {"done, it works now": "DONE_FIXED", "done, still not working": "DONE_NOT_FIXED",
               "i can't do this": "CANT_DO", "i cant do this": "CANT_DO"}


class FixtureMissing(RuntimeError):
    pass


# ---- research fixture -------------------------------------------------------------------------------------------

def load_fixture(path: Path = FIXTURE) -> dict:
    if not path.is_file():
        raise FixtureMissing(f"{path} is missing: record real search results once with `python -m evals.record_research`")
    return json.loads(path.read_text())


def fixture_research(fixture: dict):
    """A stand-in for web_research.research: the recorded real results for the product's model."""
    async def research(product_name, model, symptom, *, db, today, http=None):  # noqa: ANN001
        rec = fixture.get(model or "")
        if rec is None:
            from app.agents.web_research import ResearchError

            raise ResearchError(f"no recorded research for {model!r}")
        return ResearchNotes(notes=rec["notes"], sources=rec["sources"], provider=f"fixture:{rec.get('provider')}",
                             queries=rec.get("queries") or [], cached=False)
    return research


# ---- small media for [upload ...] ----------------------------------------------------------------------------------

def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "gray").save(buf, "JPEG")
    return buf.getvalue()


def _mp4(seconds: int = 6) -> bytes:
    box = lambda t, p: struct.pack(">I", 8 + len(p)) + t + p  # noqa: E731
    mvhd = box(b"mvhd", struct.pack(">B3xIIII", 0, 0, 0, 1000, seconds * 1000) + b"\x00" * 80)
    return box(b"ftyp", b"isom\x00\x00\x02\x00isommp41") + box(b"moov", mvhd) + box(b"mdat", b"\x00" * 64)


# ---- one simulated session ------------------------------------------------------------------------------------------

@dataclass
class SimSession:
    golden: ConvGolden
    engine: Any
    session_id: int
    seen_message_id: int = 0
    events: list[dict] = field(default_factory=list)   # per buyer turn: status after it, buttons pressed, errors

    @classmethod
    def create(cls, g: ConvGolden) -> "SimSession":
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        create_db_and_tables(engine)
        product = PRODUCTS[SKU[g.product]]
        now = datetime.now(timezone.utc)
        with Session(engine) as s:
            o = Order(buyer_email="sim.buyer@example.com", amount=Decimal(str(product.price)), currency="USD",
                      items=[order_item(product, 1)])
            s.add(o)
            s.flush()
            d = Dispute(paypal_dispute_id=f"PP-SIM-{g.id.upper()}", order_id=o.id,
                        reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", status="WAITING_FOR_SELLER_RESPONSE",
                        stage="INQUIRY", amount=Decimal(str(product.price)), currency="USD", raw={})
            s.add(d)
            s.flush()
            ts = TroubleshootingSession(dispute_id=d.id, order_id=o.id, product_name=product.name,
                                        product_model=product.model, token_hash="0" * 64, invited_at=now,
                                        buyer_last_seen_at=now, expires_at=now + timedelta(days=3),
                                        status=TSStatus.ACTIVE, invited_via="email")
            s.add(ts)
            s.commit()
            s.refresh(ts)
            T.ensure_opening(s, ts)
            opener = s.exec(select(TSMessage).where(TSMessage.session_id == ts.id)).all()
            # the buyer has seen the greeting before typing (the scenario starts from there)
            return cls(g, engine, ts.id, seen_message_id=max(m.id for m in opener))

    def _ts(self, s: Session) -> TroubleshootingSession:
        return s.get(TroubleshootingSession, self.session_id)

    def new_agent_text(self, s: Session) -> str:
        msgs = s.exec(select(TSMessage).where(TSMessage.session_id == self.session_id,
                                              TSMessage.id > self.seen_message_id).order_by(TSMessage.id)).all()
        if msgs:
            self.seen_message_id = msgs[-1].id
        return "\n\n".join(m.body for m in msgs if m.role in ("agent", "merchant"))

    def screen(self, s: Session) -> str:
        ts = self._ts(s)
        steps = list(s.exec(select(TSStep).where(TSStep.session_id == ts.id).order_by(TSStep.ordinal)))
        lines = ["[screen]"]
        if ts.status.value in FINAL:
            lines.append(f"The page has ended ({ts.status.value.replace('_', ' ').lower()}). Nothing more can be done here.")
            return "\n".join(lines)
        if steps:
            done = sum(st.status != "PENDING" for st in steps)
            lines.append(f"Steps {done}/{len(steps)} done.")
            for st in steps:
                proofs = len(s.exec(select(TSProof).where(TSProof.step_id == st.id)).all())
                need = f" Needs a {st.proof_kind if st.proof_kind != 'none' else 'photo'} ({proofs} uploaded)." \
                    if st.requires_proof else ""
                lines.append(f"Step {st.ordinal} [{st.status}]: {st.title}. {st.instruction}{need}")
        if ts.awaiting_fixed_confirm:
            lines.append("A green box asks: 'Is it working now?' with buttons [button: Yes, it works now] and "
                         "[button: No, still not working].")
        lines.append("A [button: Talk to the seller] link is at the bottom.")
        return "\n".join(lines)

    def _proof_step(self, s: Session) -> TSStep | None:
        steps = list(s.exec(select(TSStep).where(TSStep.session_id == self.session_id).order_by(TSStep.ordinal)))
        lacking = [st for st in steps if st.requires_proof
                   and not s.exec(select(TSProof).where(TSProof.step_id == st.id)).first()]
        return next((st for st in lacking if st.status != "PENDING"), lacking[0] if lacking else None)

    async def buyer_turn(self, text: str, llm) -> str:  # noqa: ANN001
        """Apply the buyer's message like the page would; return the assistant turn (messages + [screen])."""
        event: dict = {"text": text[:300], "buttons": [], "errors": []}
        with Session(self.engine) as s:
            ts = self._ts(s)
            if ts.status.value not in FINAL:
                try:
                    for m in CONFIRM.finditer(text):
                        T.confirm_fixed(s, ts, m.group(1).lower().startswith("yes"))
                        event["buttons"].append(m.group(0))
                    steps = {st.ordinal: st for st in s.exec(select(TSStep).where(TSStep.session_id == ts.id))}
                    for m in STEP_BUTTON.finditer(text):
                        st = steps.get(int(m.group(1)))
                        if st and st.status == "PENDING":
                            T.step_button(s, ts, st.id, STEP_STATUS[m.group(2).lower().replace("’", "'")])
                            event["buttons"].append(m.group(0))
                    for m in UPLOAD.finditer(text):
                        st = self._proof_step(s)
                        if st:
                            data = _jpeg() if m.group(1).lower() == "photo" else _mp4()
                            T.record_proof(s, ts, st.id, data, "image/jpeg" if m.group(1).lower() == "photo" else "video/mp4")
                            event["buttons"].append(m.group(0))
                    if HANDOFF.search(text):
                        T.handoff(s, ts)
                        event["buttons"].append("[button: Talk to the seller]")
                    rest = UPLOAD.sub("", HANDOFF.sub("", STEP_BUTTON.sub("", CONFIRM.sub("", text)))).strip()
                    s.refresh(ts)
                    if rest and ts.status.value not in FINAL:
                        await T.agent_reply(s, ts, rest, llm=llm)
                except T.SessionEnded:
                    pass
                except (T.ProofRejected, T.StepUnknown) as exc:
                    event["errors"].append(str(exc)[:200])
            s.refresh(ts)
            event["status"] = ts.status.value
            event["language"] = ts.language
            event["clarifications"] = ts.clarifications or 0
            self.events.append(event)
            reply = self.new_agent_text(s) or ("(no reply)" if ts.status.value not in FINAL else "")
            return f"{reply}\n\n{self.screen(s)}".strip()

    def facts(self) -> dict:
        with Session(self.engine) as s:
            ts = self._ts(s)
            steps = list(s.exec(select(TSStep).where(TSStep.session_id == ts.id).order_by(TSStep.ordinal)))
            from app.models import TSSource

            sources = {x.id: x for x in s.exec(select(TSSource).where(TSSource.session_id == ts.id))}
            msgs = s.exec(select(TSMessage).where(TSMessage.session_id == ts.id).order_by(TSMessage.id)).all()
            return {
                "final_status": ts.status.value, "language": ts.language, "clarifications": ts.clarifications or 0,
                "plan_generic": ts.plan_generic, "ended_reason": ts.ended_reason,
                "steps": [{"ordinal": st.ordinal, "title": st.title, "instruction": st.instruction,
                           "status": st.status, "requires_proof": st.requires_proof,
                           "sources": [{"title": sources[i].title, "url": sources[i].url} for i in st.source_ids or []
                                       if i in sources]} for st in steps],
                "agent_messages": [m.body for m in msgs if m.role == "agent"],
                "events": self.events,
            }


def strip_screen(text: str) -> str:
    return SCREEN.sub("", text or "").strip()


# ---- the simulation --------------------------------------------------------------------------------------------------

def cache_key(g: ConvGolden, fixture: dict | None, model: str, simulator: str) -> str:
    prompts = digest(T.SYSTEM_CHAT, T.INTAKE_SYSTEM, T.PLAN_SYSTEM)
    fx = digest(json.dumps(fixture, sort_keys=True)) if fixture is not None else "live"
    return f"{g.id}-{digest(g.model_dump_json(), prompts, fx, model, simulator)[:16]}"


def simulate(g: ConvGolden, *, simulator_model, llm, fixture: dict | None,  # noqa: ANN001
             cache: DiskCache | None = None, model: str = "") -> dict:
    """Never caches a run where the model under test's provider failed (llm.provider_errors): that's an outage,
    not the agent's behaviour."""
    """Run (or reuse) one simulated conversation. Returns {turns, facts, ...}."""
    from deepeval.simulator import ConversationSimulator
    from deepeval.simulator.controller.controller import end, proceed
    from deepeval.test_case import Turn

    key = cache_key(g, fixture, model, simulator_model.get_model_name())
    if cache and (hit := cache.get(key)):
        return {**hit, "cached": True}
    sims: dict[str, SimSession] = {}

    async def callback(input: str, thread_id: str):  # noqa: A002 (DeepEval's argument name)
        sim = sims.get(thread_id) or sims.setdefault(thread_id, SimSession.create(g))
        return Turn(role="assistant", content=await sim.buyer_turn(input, llm))

    def controller(thread_id: str, simulated_user_turns: int, max_user_simulations: int):
        sim = sims.get(thread_id)
        if sim and sim.events and sim.events[-1]["status"] in FINAL:
            return end(f"session ended: {sim.events[-1]['status']}")
        return end("max turns") if simulated_user_turns >= max_user_simulations else proceed()

    original = T.research
    if fixture is not None:
        T.research = fixture_research(fixture)
    try:
        sim_runner = ConversationSimulator(model_callback=callback, simulator_model=simulator_model,
                                           stopping_controller=controller, async_mode=True, max_concurrent=1)
        [case] = sim_runner.simulate(conversational_goldens=[g.deepeval_golden()], max_user_simulations=g.max_turns)
    finally:
        T.research = original
    sim = next(iter(sims.values()))
    out = {"golden_id": g.id, "model": model, "simulator": simulator_model.get_model_name(), "cache_key": key,
           "turns": [{"role": t.role, "content": t.content} for t in case.turns], "facts": sim.facts(),
           "research": "live" if fixture is None else "fixture"}
    if getattr(llm, "provider_errors", None):
        return {"golden_id": g.id, "error": "provider_error", "detail": " | ".join(llm.provider_errors)[:500]}
    if cache:
        cache.set(key, out)
    return {**out, "cached": False}
