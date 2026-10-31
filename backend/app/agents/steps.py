"""Timeline logging (rule 6: every agent step goes to agent_steps)."""

from sqlmodel import Session

from app.models import AgentStep


def log_step(session: Session, dispute_id: int, agent: str, action: str, detail: dict | None = None) -> None:
    session.add(AgentStep(dispute_id=dispute_id, agent=agent, action=action, detail=detail))
    session.commit()
