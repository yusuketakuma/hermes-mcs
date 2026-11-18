"""Run a wholly synthetic, stubbed private previsit preparation and delivery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcs"))
import _mcs_path  # noqa: E402,F401

from ledger import Ledger  # noqa: E402
import mcs_requests  # noqa: E402
import private_previsit  # noqa: E402


JST = timezone(timedelta(hours=9))
CFG = {"private_previsit": {"enabled": True, "owner_gate_approved": True}}


def _command(cmd, **fields):
    return {"version": 1, "cmd": cmd, "command_id": str(uuid.uuid4()),
            "actor": "synthetic-demo-reviewer", "human_confirmed": True,
            "project_id": 1, **fields}


class SyntheticPrivateSender:
    def send(self, content, *, project_id, recipient_id, transport, account_scope,
             verify_authorization=None):
        if verify_authorization is None or not verify_authorization():
            return {"result": "not_sent", "error_code": "private_verification_failed"}
        return {"result": "delivered", "accepted_ref": "synthetic-lineworks:201"}


def main():
    with tempfile.TemporaryDirectory(prefix="mcs-private-previsit-demo-") as folder:
        ledger = Ledger(str(Path(folder) / "synthetic-ledger.db"))
        try:
            ledger.ensure_patient(1)
            body = "合成本文: 訪問前の確認事項"
            ledger.db.execute(
                "INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,body_html,"
                "body_state,content_hash) VALUES(?,?,?,?,?,?,?)",
                (1, 1, int(datetime(2026, 6, 1, 12, tzinfo=JST).timestamp()),
                 body, body, "full", hashlib.sha256(body.encode()).hexdigest()))
            ledger.db.commit()

            assignment = _command(
                "ops.previsit_assignment_set", expected_revision=0,
                staff_id="synthetic-staff-1", transport="lineworks",
                account_scope={"profile": "synthetic", "application_id": "synthetic-bot",
                               "team_id": "synthetic-team", "route_epoch": 1},
                recipient_id="synthetic-staff@example.invalid", reason="synthetic approval")
            visit = _command("ops.previsit_visit_date_set", expected_revision=0,
                             visit_date="2026-06-02", reason="synthetic date approval")
            assert mcs_requests.apply_command(ledger, assignment)["outcome"] == "applied"
            assert mcs_requests.apply_command(ledger, visit)["outcome"] == "applied"

            private_previsit.read_source_schedule = lambda db, project_id, **kwargs: {
                "project_id": project_id, "records": [],
                "coverage": {"state": "observed", "fetch_state": "complete"}}
            private_previsit.notify_views.patient_context_list = (
                lambda db, project_id, section, **kwargs: {
                    "title": "合成患者", "head": [], "page": 0, "pages": 1,
                    "items": [{"group": "合成の申し送り", "quotes": ["合成記録です。"],
                               "source_rest": "#1", "message_id": 1,
                               "project_id": project_id}]})
            prep_time = datetime(2026, 6, 1, 20, tzinfo=JST).timestamp()
            prepared = private_previsit.prepare_night(ledger, CFG, now=prep_time)
            print("準備:", prepared["items"][0]["state"])
            print("合成メモ:")
            print(prepared["items"][0]["note"])

            dispatch_time = datetime(2026, 6, 2, 8, tzinfo=JST).timestamp()
            outcomes = private_previsit.dispatch_due(
                ledger, CFG, sender=SyntheticPrivateSender(), now=dispatch_time)
            row = ledger.db.execute(
                "SELECT state,attempts,accepted_ref FROM notify_outbox WHERE kind=?",
                (private_previsit.KIND,)).fetchone()
            print("配送:", outcomes[0]["state"])
            print("outbox:", dict(row))
        finally:
            ledger.close()


if __name__ == "__main__":
    main()
