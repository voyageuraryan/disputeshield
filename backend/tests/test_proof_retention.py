"""G44: buyer proof files are deleted after the retention period; buyers can delete their own uploads."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from app.models import AgentStep, Dispute, TroubleshootingSession, TSProof, TSStatus
from app.troubleshooting.retention import purge_expired_proofs, resolved_at
from tests import media
from tests.test_proof_uploads import clean_state, open_session, upload, view  # noqa: F401  (fixture + helpers)
from tests.test_store_api import client, engine  # noqa: F401  (fixtures)

NOW = datetime.now(timezone.utc)


def resolve(engine, days_ago):
    with Session(engine) as s:
        d = s.exec(select(Dispute)).first()
        d.status = "RESOLVED"
        d.raw = {**d.raw, "update_time": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")}
        s.add(d); s.commit()


def test_files_are_purged_only_after_the_retention_period(client, engine):
    sid, h = open_session(client, engine)
    pid = upload(client, sid, h, view(client, sid, h)["steps"][4]["id"], media.jpeg()).json()["proof"]["id"]
    with Session(engine) as s:
        path = Path(s.get(TSProof, pid).file_path)
    assert path.is_file()
    resolve(engine, days_ago=30)
    with Session(engine) as s:
        assert purge_expired_proofs(s, now=NOW, days=90) == []  # not yet
        assert resolved_at(s.exec(select(Dispute)).first()) is not None
    resolve(engine, days_ago=91)
    with Session(engine) as s:
        assert purge_expired_proofs(s, now=NOW, days=90) == [pid]
        p = s.get(TSProof, pid)
        assert p.file_path == "" and p.evidence_path is None and p.checks["purged_at"]
        assert p.checks["detected_type"] == "JPEG"  # the record and its checks stay for the merchant
        assert purge_expired_proofs(s, now=NOW, days=90) == []  # idempotent
        assert any("retention policy" in a.action for a in s.exec(select(AgentStep)))
    assert not path.exists()
    r = client.get(f"/api/disputes/PP-TS-1/troubleshooting/proofs/{pid}")
    assert r.status_code == 410 and "retention policy" in r.json()["detail"]


def test_open_disputes_and_zero_days_keep_everything(client, engine):
    sid, h = open_session(client, engine)
    pid = upload(client, sid, h, view(client, sid, h)["steps"][4]["id"], media.png()).json()["proof"]["id"]
    with Session(engine) as s:
        assert purge_expired_proofs(s, now=NOW + timedelta(days=999), days=1) == []  # dispute not resolved
    resolve(engine, days_ago=500)
    with Session(engine) as s:
        assert purge_expired_proofs(s, now=NOW, days=0) == []  # 0 = keep
        assert Path(s.get(TSProof, pid).file_path).is_file()


def test_buyer_deletes_own_upload_and_the_step_needs_proof_again(client, engine):
    sid, h = open_session(client, engine)
    steps = view(client, sid, h)["steps"]
    client.post(f"/api/help/{sid}/steps/{steps[4]['id']}", json={"status": "DONE_NOT_FIXED"}, headers=h)
    r = upload(client, sid, h, steps[3]["id"], media.png())  # step 4 (no proof needed) keeps the session open
    pid = r.json()["proof"]["id"]
    with Session(engine) as s:
        path = Path(s.get(TSProof, pid).file_path)
    r = client.delete(f"/api/help/{sid}/proofs/{pid}", headers=h)
    assert r.status_code == 200 and r.json()["steps"][3]["proofs"] == 0 and not path.exists()
    assert client.delete(f"/api/help/{sid}/proofs/{pid}", headers=h).status_code == 404
    # another session can't delete it
    sid2, h2 = open_session(client, engine, pid="PP-TS-2", buyer_email="other@example.com")
    pid2 = upload(client, sid2, h2, view(client, sid2, h2)["steps"][3]["id"], media.png()).json()["proof"]["id"]
    assert client.delete(f"/api/help/{sid}/proofs/{pid2}", headers=h).status_code == 404


def test_deleting_the_only_proof_of_a_finished_step_awaits_proof_again(client, engine):
    sid, h = open_session(client, engine)
    steps = view(client, sid, h)["steps"]
    pid = upload(client, sid, h, steps[4]["id"], media.png()).json()["proof"]["id"]
    client.post(f"/api/help/{sid}/steps/{steps[4]['id']}", json={"status": "DONE_NOT_FIXED"}, headers=h)
    assert view(client, sid, h)["status"] == "ACTIVE"  # proof present, other steps still open
    client.delete(f"/api/help/{sid}/proofs/{pid}", headers=h)
    assert view(client, sid, h)["status"] == "AWAITING_PROOF"


def test_no_deleting_after_the_page_ended(client, engine):
    sid, h = open_session(client, engine)
    pid = upload(client, sid, h, view(client, sid, h)["steps"][3]["id"], media.png()).json()["proof"]["id"]
    with Session(engine) as s:
        ts = s.get(TroubleshootingSession, sid)
        ts.status = TSStatus.DEFECT_CONFIRMED
        s.add(ts); s.commit()
    assert client.delete(f"/api/help/{sid}/proofs/{pid}", headers=h).status_code in (409, 410)
