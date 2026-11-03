from datetime import datetime, timezone
from decimal import Decimal

import pytest
import respx
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.agents.analyst import NEEDS_HUMAN, SYSTEM_PROMPT, analyze
from app.agents.investigator import investigate
from app.agents.llm import LLMError
from app.agents.runner import run_agents
from app.db import create_db_and_tables
from app.models import AgentStep, Decision, DecisionStatus, Dispute
from app.schemas import Recommendation
from app.scripts.seed import seed
from tests.conftest import BASE, token_response
from tests.fakes import ScriptedLLM

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
DISPUTES = f"{BASE}/v1/customer/disputes"
INR, SNAD = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
# What PayPal typically allows while it waits for the seller.
SELLER_TURN = ["provide_evidence", "accept_claim", "make_offer"]


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_db_and_tables(eng)
    return eng


@pytest.fixture
def demo(engine):
    with Session(engine) as s:
        return seed(s, now=NOW)


def paypal_dispute(pid, reason, amount, rels=SELLER_TURN):
    return {"dispute_id": pid, "reason": reason, "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_life_cycle_stage": "INQUIRY", "dispute_state": "REQUIRED_ACTION",
            "dispute_amount": {"currency_code": "USD", "value": amount},
            "update_time": "2026-10-31T10:00:00.000Z",
            "links": [{"rel": r, "href": f"{DISPUTES}/{pid}/{r}", "method": "POST"} for r in ("self", *rels)]}


async def investigated(engine, pp, order_id, pid, reason, amount, rels=SELLER_TURN):
    """Run the real investigator on a seeded demo order (PayPal mocked) and return (dispute_id, case)."""
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{DISPUTES}/{pid}").respond(200, json=paypal_dispute(pid, reason, amount, rels))
    with Session(engine) as s:
        d = Dispute(paypal_dispute_id=pid, order_id=order_id, reason=reason, status="OPEN",
                    amount=Decimal(amount), currency="USD")
        s.add(d); s.commit(); s.refresh(d)
        case = await investigate(s, d, pp)
        return d.id, case


def ids_of(case, *types):
    return [e.id for e in case.evidence if e.type in types]


def rec(action, confidence, cited, missing=(), reasoning="Because of the cited evidence.", note="Worth it."):
    return Recommendation(action=action, confidence=confidence, reasoning=reasoning, cited_evidence_ids=list(cited),
                          missing_items=list(missing), economic_note=note)


async def run_analyst(engine, did, case, llm, run_key="run:test"):
    with Session(engine) as s:
        result = await analyze(s, s.get(Dispute, did), case, run_key=run_key, llm=llm)
        steps = [a.action for a in s.exec(select(AgentStep).where(AgentStep.agent == "analyst")
                                          .order_by(AgentStep.id)).all()]
        decisions = s.exec(select(Decision).where(Decision.dispute_id == did).order_by(Decision.id)).all()
        status = s.get(Dispute, did).agent_status
    return result, steps, decisions, status


# ---- demo case A: delivered to the buyer, "got it, thanks" -> FIGHT, high ------------------

@respx.mock
async def test_case_a_fight_high(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["A"], "PP-D-A", INR, "89.00")
    cited = ids_of(case, "tracking", "delivery_confirmation", "buyer_message")
    llm = ScriptedLLM(rec("FIGHT", "high", cited, reasoning=(
        "USPS tracking shows delivery on 15 Oct to the buyer's own address, and the buyer later wrote "
        "\"got it, thanks\". That is strong proof the item arrived.")))

    result, steps, decisions, status = await run_analyst(engine, did, case, llm)

    assert result.checklist.rule_score == 0.93 and result.checklist.red_flags == []
    assert result.baseline.action == "FIGHT"
    assert (result.recommendation.action, result.recommendation.confidence) == ("FIGHT", "high")
    [d] = decisions
    assert (d.status, d.recommendation, d.confidence) == (DecisionStatus.PROPOSED, "FIGHT", "high")
    assert d.cited_evidence_ids == cited and d.run_key == "run:test" and status == "PROPOSED"
    assert steps[0] == "Checklist for MERCHANDISE_OR_SERVICE_NOT_RECEIVED: rule score 0.93, nothing required missing"
    assert steps[1] == "Economic rule suggests FIGHT: rule score 0.93 >= 0.7"
    assert steps[2].startswith("Recommend FIGHT, high confidence: USPS tracking shows delivery")
    # the LLM saw the case file, checklist, baseline and threshold
    p = llm.calls[0]["payload"]
    assert p["baseline"]["action"] == "FIGHT" and p["merchant_settle_threshold"] == "25.00 USD"
    assert {e["id"] for e in p["case_file"]["evidence"]} == {e.id for e in case.evidence}
    assert llm.calls[0]["system"] == SYSTEM_PROMPT


# ---- demo case B: $12 phone case, untracked, "not what was in the listing" -> SETTLE --------

@respx.mock
async def test_case_b_settle(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["B"], "PP-D-B", SNAD, "12.00")
    llm = ScriptedLLM(rec("SETTLE", "medium", ids_of(case, "buyer_message", "listing"), missing=["refund_policy"],
                          reasoning="Only $12, no tracking, no refund policy on file, and the buyer says the "
                                    "case differs from the listing. Refunding costs less than fighting.",
                          note="$12 is under the $25 threshold and the case is weak."))

    result, steps, decisions, status = await run_analyst(engine, did, case, llm)

    assert result.checklist.missing == ["refund_policy"] and result.checklist.rule_score == 0.59
    assert result.baseline.action == "SETTLE" and "under the 25.00 threshold" in result.baseline.why
    assert decisions[0].recommendation == "SETTLE" and decisions[0].status == DecisionStatus.PROPOSED
    assert "Economic rule suggests SETTLE" in steps[1]


# ---- demo case C: delivered to a different address -> GATHER_MORE (red flag) --------------

@respx.mock
async def test_case_c_gather_more_with_address_red_flag(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["C"], "PP-D-C", INR, "45.00")
    llm = ScriptedLLM(rec("GATHER_MORE", "low", ids_of(case, "delivery_confirmation", "tracking"),
                          missing=["proof of delivery to the buyer's address"],
                          reasoning="UPS says delivered, but to 1540 Industrial Pkwy, not the buyer's 88 Lake Road."))

    result, steps, decisions, _ = await run_analyst(engine, did, case, llm)

    assert result.checklist.red_flags == ["address_mismatch"] and result.checklist.rule_score == 0.53
    assert result.baseline.action == "GATHER_MORE"
    assert decisions[0].recommendation == "GATHER_MORE"
    assert steps[0].endswith("red flags: address_mismatch")
    assert llm.calls[0]["payload"]["checklist"]["red_flag_details"] == [
        "Carrier delivered to a different address than the order's shipping address"]


@respx.mock
async def test_case_c_weak_fight_needs_justifying_citation(pp, engine, demo):
    """FIGHT is one step up from the GATHER_MORE baseline; allowed only with a cited buyer message/listing."""
    did, case = await investigated(engine, pp, demo["C"], "PP-D-C", INR, "45.00")
    tracking_only = rec("FIGHT", "low", ids_of(case, "tracking"))
    with_message = rec("FIGHT", "low", ids_of(case, "tracking", "buyer_message"),
                       reasoning="Weak fight: tracking shows delivery, but to another address (red flag).")
    llm = ScriptedLLM(tracking_only, with_message)

    result, steps, decisions, _ = await run_analyst(engine, did, case, llm)

    assert steps[2] == "Recommendation rejected (attempt 1 of 2)"
    assert "must cite a buyer_message or listing" in llm.calls[1]["user"]  # retry tells the LLM why
    assert result.recommendation.action == "FIGHT" and result.recommendation.confidence == "low"
    assert steps[3].startswith("Recommend FIGHT (moved from GATHER_MORE), low confidence")


# ---- validation, retry once, NEEDS_HUMAN -------------------------------------------

@respx.mock
async def test_invented_evidence_twice_needs_human(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["A"], "PP-D-A", INR, "89.00")
    bad = rec("FIGHT", "high", [9999])
    llm = ScriptedLLM(bad, bad)

    result, steps, decisions, status = await run_analyst(engine, did, case, llm)

    assert result.needs_human and result.recommendation is None and decisions == []
    assert status == NEEDS_HUMAN and len(llm.calls) == 2
    assert steps[-1].startswith("Needs human review: Recommendation cites unknown evidence ids: [9999]")


@respx.mock
async def test_llm_failure_then_success(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["A"], "PP-D-A", INR, "89.00")
    llm = ScriptedLLM(LLMError("model refused (category=None)"), rec("FIGHT", "high", ids_of(case, "tracking")))
    result, steps, decisions, _ = await run_analyst(engine, did, case, llm)
    assert result.recommendation.action == "FIGHT" and len(decisions) == 1
    assert "LLM call failed: model refused" in llm.calls[1]["user"]


@respx.mock
async def test_llm_failing_twice_needs_human(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["A"], "PP-D-A", INR, "89.00")
    result, _, decisions, status = await run_analyst(engine, did, case,
                                                     ScriptedLLM(LLMError("timeout"), LLMError("timeout")))
    assert result.needs_human and decisions == [] and status == NEEDS_HUMAN


@respx.mock
async def test_two_step_jump_rejected(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["A"], "PP-D-A", INR, "89.00")  # baseline FIGHT
    jump = rec("SETTLE", "low", ids_of(case, "buyer_message"))
    result, _, _, _ = await run_analyst(engine, did, case, ScriptedLLM(jump, jump))
    assert result.needs_human
    assert "SETTLE is more than one step from the baseline FIGHT" in result.problems


@respx.mock
async def test_one_step_down_with_listing_citation_accepted(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["B"], "PP-D-B", SNAD, "12.00")  # baseline SETTLE
    up = rec("GATHER_MORE", "low", ids_of(case, "listing"), missing=["refund_policy", "photos of the item sent"])
    result, steps, _, _ = await run_analyst(engine, did, case, ScriptedLLM(up))
    assert result.recommendation.action == "GATHER_MORE"
    assert steps[-1].startswith("Recommend GATHER_MORE (moved from SETTLE)")


@respx.mock
async def test_gather_more_on_demo_c_red_flag_names_what_to_resolve(pp, engine, demo):
    """Demo C (address mismatch): nothing required is missing, so code names the red flag to resolve (10A evals)."""
    did, case = await investigated(engine, pp, demo["C"], "PP-D-C", INR, "45.00")
    result, _, _, _ = await run_analyst(engine, did, case, ScriptedLLM(rec("GATHER_MORE", "low", [])))
    assert not result.needs_human
    assert result.recommendation.missing_items == [
        "evidence that resolves: Carrier delivered to a different address than the order's shipping address"]


async def test_gather_more_without_missing_items_rejected_when_evidence_is_missing(engine):
    """Required evidence is missing: the LLM must name it (no code fill), else retry, then NEEDS_HUMAN."""
    from decimal import Decimal as D

    from sqlmodel import Session as S

    from app.agents.analyst import analyze
    from app.models import Dispute
    from app.schemas import CaseFile, EvidenceItem

    ev = lambda i, t: EvidenceItem(id=i, type=t, source="store", source_ref=f"store:{t}/{i}", summary=t)  # noqa: E731
    case = CaseFile(dispute_id="PP-MI", reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", amount=D("89"),
                    currency="USD", due_date=None, order_id=None, allowed_actions=["provide_evidence", "accept_claim"],
                    evidence=[ev(1, "listing"), ev(2, "order"), ev(3, "buyer_message")])  # no refund_policy
    with S(engine) as s:
        d = Dispute(paypal_dispute_id="PP-MI", reason=case.reason, status="OPEN", amount=D("89"), currency="USD", raw={})
        s.add(d); s.commit(); s.refresh(d)
        result = await analyze(s, d, case, llm=ScriptedLLM(rec("GATHER_MORE", "low", []), rec("GATHER_MORE", "low", [])))
    assert result.needs_human and "GATHER_MORE must name the missing items" in result.problems


@respx.mock
async def test_overlong_reasoning_rejected(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["A"], "PP-D-A", INR, "89.00")
    long = rec("FIGHT", "high", ids_of(case, "tracking"), reasoning="word " * 200)
    result, _, _, _ = await run_analyst(engine, did, case, ScriptedLLM(long, long))
    assert any("reasoning is 200 words" in p for p in result.problems)


# ---- PayPal's allowed actions ------------------------------------------------------

@respx.mock
async def test_fight_not_allowed_by_paypal(pp, engine, demo):
    """Strong case A, but PayPal only allows accept_claim: baseline drops to GATHER_MORE, FIGHT is refused."""
    did, case = await investigated(engine, pp, demo["A"], "PP-D-A", INR, "89.00", rels=["accept_claim"])
    assert case.can_fight is False
    fight = rec("FIGHT", "high", ids_of(case, "tracking", "buyer_message"))
    result, steps, _, _ = await run_analyst(engine, did, case, ScriptedLLM(fight, fight))
    assert result.baseline.action == "GATHER_MORE" and "does not accept evidence at this stage (settling is still possible)" in result.baseline.why
    assert result.needs_human
    assert any(p.startswith("FIGHT is not possible now; PayPal allows only ['accept_claim']") for p in result.problems)


# ---- superseding + unsupported reason ----------------------------------------------

@respx.mock
async def test_new_proposal_supersedes_old(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["A"], "PP-D-A", INR, "89.00")
    good = rec("FIGHT", "high", ids_of(case, "tracking"))
    await run_analyst(engine, did, case, ScriptedLLM(good), run_key="run:v1")
    _, steps, decisions, _ = await run_analyst(engine, did, case, ScriptedLLM(good), run_key="run:v2")
    assert [(d.status, d.run_key) for d in decisions] == [(DecisionStatus.SUPERSEDED, "run:v1"),
                                                          (DecisionStatus.PROPOSED, "run:v2")]


@respx.mock
async def test_unsupported_reason_goes_to_human_without_llm(pp, engine, demo):
    did, case = await investigated(engine, pp, demo["A"], "PP-D-X", "CREDIT_NOT_PROCESSED", "89.00")
    llm = ScriptedLLM()
    result, steps, decisions, status = await run_analyst(engine, did, case, llm)
    assert result.needs_human and llm.calls == [] and decisions == [] and status == NEEDS_HUMAN
    assert "No evidence rule for PayPal reason CREDIT_NOT_PROCESSED" in steps[-1]


# ---- through the graph -------------------------------------------------------------

@respx.mock
async def test_graph_ends_without_approval_when_needs_human(pp, engine, demo, tmp_path):
    respx.post(f"{BASE}/v1/oauth2/token").respond(200, json=token_response())
    respx.get(f"{DISPUTES}/PP-D-A").respond(200, json=paypal_dispute("PP-D-A", INR, "89.00"))
    with Session(engine) as s:
        d = Dispute(paypal_dispute_id="PP-D-A", order_id=demo["A"], reason=INR, status="OPEN",
                    amount=Decimal("89"), currency="USD")
        s.add(d); s.commit(); s.refresh(d)
        did = d.id
    bad = rec("FIGHT", "high", [9999])
    result = await run_agents(did, "run:nh", engine, client=pp, checkpoint_path=str(tmp_path / "c.db"),
                              llm=ScriptedLLM(bad, bad))
    assert result["needs_human"] is True and "__interrupt__" not in result
    with Session(engine) as s:
        actions = [a.action for a in s.exec(select(AgentStep).order_by(AgentStep.id)).all()]
    assert "Waiting for merchant approval" not in actions
    assert not any(a.startswith("Resolution") for a in actions)


def test_prompt_only_offers_actions_the_app_can_send():
    from app.agents.analyst import SYSTEM_PROMPT
    assert "escalating the case to a PayPal claim" in SYSTEM_PROMPT
    assert "cannot send partial offers or messages" in SYSTEM_PROMPT


async def test_gather_more_on_a_red_flag_gets_the_missing_items_from_the_rules(engine):
    """Evals (10A): for an address-mismatch case the real LLM chose GATHER_MORE with no missing items -> NEEDS_HUMAN."""
    from decimal import Decimal as D

    from sqlmodel import Session as S

    from app.agents.analyst import analyze
    from app.models import Dispute
    from app.schemas import CaseFile, EvidenceItem, Recommendation

    ev = lambda i, t, f=(): EvidenceItem(id=i, type=t, source="store", source_ref=f"store:{t}/{i}", summary=t,  # noqa: E731
                                         flags=list(f))
    case = CaseFile(dispute_id="PP-RF", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", amount=D("89"), currency="USD",
                    due_date=None, order_id=None, allowed_actions=["provide_evidence", "accept_claim"],
                    evidence=[ev(1, "tracking", ["delivered"]), ev(2, "delivery_confirmation", ["address_mismatch"]),
                              ev(3, "order"), ev(4, "invoice", ["protected:ITEM_NOT_RECEIVED"])])
    with S(engine) as s:
        d = Dispute(paypal_dispute_id="PP-RF", reason=case.reason, status="OPEN", amount=D("89"), currency="USD", raw={})
        s.add(d); s.commit(); s.refresh(d)

        async def llm(*, system, user, output_model):
            return Recommendation(action="GATHER_MORE", confidence="medium", reasoning="Delivered elsewhere; check.",
                                  cited_evidence_ids=[2], missing_items=[], economic_note="x")
        r = await analyze(s, d, case, llm=llm)
    assert not r.needs_human and r.recommendation.missing_items == [
        "evidence that resolves: Carrier delivered to a different address than the order's shipping address"]



def test_unjustified_move_says_which_action_to_use_when_nothing_could_justify_it():
    """10C/G74: at temperature 0 the model kept FIGHT on an address-mismatch case; the retry now names the baseline."""
    from decimal import Decimal as D

    from app.agents.analyst import validate
    from app.rules.evidence_rules import Baseline
    from app.schemas import CaseFile, EvidenceItem, Recommendation

    ev = lambda i, t: EvidenceItem(id=i, type=t, source="store", source_ref=f"store:{t}/{i}", summary=t)  # noqa: E731
    fight = Recommendation(action="FIGHT", confidence="high", reasoning="Delivered and protected.",
                           cited_evidence_ids=[1, 2], missing_items=[], economic_note="x")
    base = Baseline("GATHER_MORE", "rule score 0.45")
    no_msgs = CaseFile(dispute_id="P", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", amount=D("89"), currency="USD",
                       due_date=None, order_id=None, allowed_actions=["provide_evidence", "accept_claim"],
                       evidence=[ev(1, "tracking"), ev(2, "delivery_confirmation")])
    assert any("this case has none, so recommend the baseline GATHER_MORE" in p for p in validate(fight, no_msgs, base))
    with_msg = no_msgs.model_copy(update={"evidence": no_msgs.evidence + [ev(3, "buyer_message")]})
    probs = validate(fight, with_msg, base)
    assert any("must cite a buyer_message" in p for p in probs) and not any("has none" in p for p in probs)
