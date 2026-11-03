"""Conversational goldens for the buyer troubleshooting agent (Phase 10C): evals/goldens/troubleshooting.json.

Each maps onto DeepEval's ConversationalGolden (scenario, expected_outcome, persona=Persona(name,
characteristics), context, additional_metadata) and adds our deterministic expectations:
  expected_status      final session status(es) that count as correct (code decides outcomes, so this is exact)
  hazard               the buyer mentions a hazard: SAFETY_STOP must happen on that same turn
  max_clarifications   clarifying questions the agent may ask before giving steps (<= 2)
  reply_language       BCP-47 code the agent must answer in (e.g. "hi")
  must_not             phrases the agent must never assert (refund promises, a phone number, ...)
  live_search          use real web search instead of the recorded fixture (the separate smoke golden)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

FILE = Path(__file__).parent / "troubleshooting.json"
Status = Literal["INVITED", "ACTIVE", "AWAITING_PROOF", "RESOLVED_FIXED", "DEFECT_CONFIRMED", "SAFETY_STOP",
                 "EXPIRED", "HANDED_TO_MERCHANT"]
OPEN = ["ACTIVE", "AWAITING_PROOF"]


class ConvPersona(BaseModel):
    name: str
    characteristics: str


class ConvGolden(BaseModel):
    version: int = 1
    id: str
    product: Literal["earbuds", "lamp"]
    scenario: str
    expected_outcome: str
    persona: ConvPersona
    expected_status: list[Status]
    hazard: bool = False
    max_clarifications: int = 2
    reply_language: str | None = None
    must_not: list[str] = Field(default_factory=list)
    live_search: bool = False
    max_turns: int = 8
    notes: str = ""

    def deepeval_golden(self):
        from deepeval.dataset import ConversationalGolden
        from deepeval.dataset.golden import Persona

        return ConversationalGolden(
            scenario=self.scenario + "\n\n" + BUTTON_HELP, expected_outcome=self.expected_outcome, name=self.id,
            persona=Persona(name=self.persona.name, characteristics=self.persona.characteristics),
            additional_metadata={"golden_id": self.id})


# What the simulated buyer can do besides typing (the real page has buttons and an upload box). The harness turns
# these tokens into the same calls the page makes; the assistant's turn shows the page in a [screen] block.
BUTTON_HELP = (
    "You are on a web page with a chat and step cards. Besides typing, you can press buttons by writing one of these "
    "tokens exactly in your message: [button: Step N: Done, it works now], [button: Step N: Done, still not "
    "working], [button: Step N: I can't do this], [button: Yes, it works now], [button: No, still not working], "
    "[button: Talk to the seller], and [upload photo] or [upload video] to attach a picture or clip to the step that "
    "asks for one. The [screen] part of each assistant message shows what the page currently displays.")


def load_conversations(path: Path = FILE) -> list[ConvGolden]:
    goldens = [ConvGolden.model_validate(x) for x in json.loads(path.read_text())]
    ids = [g.id for g in goldens]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate conversational golden id")
    return goldens
