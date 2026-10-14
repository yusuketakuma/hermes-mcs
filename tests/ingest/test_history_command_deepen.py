"""A deeper command cannot floor a range whose old cutoff page was never re-fetched."""
import json
import time
from datetime import datetime, timezone
import sqlite3
import sys
from contextlib import closing

import job_ops
import notify_cards
from ledger import Ledger
from mcs_adapter import MessageBatch
from extract_testkit import _message
import mcs_adapter


def test_deeper_single_page_command_restarts_and_imports_old_cutoff_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(job_ops.time, "time", lambda: 10_000_000)
    monkeypatch.setattr(notify_cards, "restore_awaiting_consent", lambda path: None)
    ledger = Ledger(str(tmp_path / "synthetic.db"))
    try:
        ledger.ensure_patient(1)
        ledger.job_add("history", 1, payload={"since": 9_000_000, "page": 3,
                                              "pages": 1, "stalls": 2})
        commands = tmp_path / "cmd"
        commands.mkdir()
        (commands / "deepen.json").write_text(json.dumps(
            {"cmd": "import", "project_id": 1, "days": 14, "pages": 1}))
        result = {"errors": []}
        job_ops.drain_commands(ledger, result, str(commands))
        calls = []

        class Adapter:
            def fetch_history(self, pid, since, max_pages, start_page):
                calls.append(start_page)
                # The old cutoff fell within page2; page3 is the terminal tail.
                messages = [] if start_page >= 3 else [_message(mid=2, body="SYNTH-gap",
                    posted_at=datetime.fromtimestamp(8_900_000, timezone.utc).isoformat())]
                return MessageBatch(messages, pages=1, reached=True)

        job_ops.run_history_jobs(Adapter(), ledger, result, time.monotonic() + 120)
        assert calls == [1]
        assert ledger.db.execute("SELECT COUNT(*) FROM messages WHERE message_id=2").fetchone()[0] == 1
        assert ledger.history_floor(1) == 10_000_000 - 14 * 86400
    finally:
        ledger.close()


# init_data: a deeper --since after a finished shallower walk must
# re-walk from page 1 so the gap is fetched before the floor deepens.
NOW = time.time()
MSGS = [{"id": 1000 - i,
         "created_at": datetime.fromtimestamp(
             NOW - i * 86400, timezone.utc).isoformat(),
         "comment": "synthetic", "user": {"id": 1, "type": "user"},
         "files": []} for i in range(1, 51)]


class _Adapter(mcs_adapter.MCSAdapter):
    def _get(self, path, params=None, extend_session=True):
        if path == "/projects":
            return {"projects": [{
                "id": 7, "type": "medical", "karte": {},
                "last_message": {"created_at": MSGS[0]["created_at"]}}],
                "paginate": {"has_next": False}}
        p, pp = params["page"], params["per_page"]
        return {"messages": MSGS[(p - 1) * pp:p * pp],
                "paginate": {"has_next": p * pp < len(MSGS)}}


def test_deeper_since_fetches_gap_before_floor(monkeypatch, tmp_path):
    import init_data
    ad = _Adapter()
    ad._token = "x" * 20
    monkeypatch.setattr(init_data, "MCSAdapter", lambda **kw: ad)
    db = str(tmp_path / "ledger.db")
    monkeypatch.setattr(init_data, "DB", db)
    monkeypatch.setattr(init_data, "LOCKFILE", str(tmp_path / "run.lock"))
    for days in ("14", "60"):
        monkeypatch.setattr(sys, "argv", [
            "init_data.py", "--days", days, "--delay", "0"])
        init_data.main()
    with closing(sqlite3.connect(db)) as c:
        assert c.execute("select count(*) from messages").fetchone()[0] == 50
        floor = c.execute("select history_floor from patients").fetchone()[0]
        assert floor <= NOW - 59 * 86400
