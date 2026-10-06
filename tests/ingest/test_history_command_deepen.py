"""A deeper command cannot floor a range whose old cutoff page was never re-fetched."""
import json
import time
from datetime import datetime, timezone

import job_ops
import notify_cards
from ledger import Ledger
from mcs_adapter import MessageBatch
from extract_testkit import _message


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
