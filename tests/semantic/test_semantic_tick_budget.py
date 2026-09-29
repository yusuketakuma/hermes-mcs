"""AT054: the real tick preserves ordinary work when semantic budget waits."""
import json
import sys

import pytest

import extract_llm
import job_ops
import ledger
import mcs_adapter
import maintenance
import notify_flush
import run_check
import semantic


class _BudgetJev:
    instances = []
    events = []

    def __init__(self, *args, **kwargs):
        self.requests_made = 0
        self.request_cap = None
        self.last_error = None
        self.calls = 0
        type(self).instances.append(self)

    def evaluate(self, state, questions, deadline):
        type(self).events.append("semantic")
        self.calls += 1
        self.last_error = semantic.jev.JevError(
            "budget_exceeded", "synthetic", retryable=True)
        raise self.last_error


def test_real_tick_keeps_extraction_and_semantic_pending_on_budget_wait(
        tmp_path, monkeypatch, capsys):
    _BudgetJev.instances.clear()
    _BudgetJev.events.clear()

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if path == "/projects":
                return {"projects": [{"id": 1, "is_unread": True,
                                      "karte": {}}],
                        "paginate": {"has_next": False, "timestamp": 123}}
            assert path == "/projects/1/messages"
            return {"messages": [{
                "id": 1, "comment": "synthetic body",
                "created_at": "2026-09-19T00:00:00+09:00",
            }], "paginate": {"has_next": False}}

        def _request(self, *args, **kwargs):
            pytest.fail("external request forbidden")

    data = tmp_path / "data"
    data.mkdir()
    (data / "cmd").mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "deep_history": False,
        "semantic": {
            "mode": "shadow", "summary_mode": "shadow",
            "loop_mode": "shadow", "threshold_mode": "calibrated",
            "calibration_version": "synthetic-test-v1",
            "daily_request_budget": 1, "project_ids": [1],
        },
    }), encoding="utf-8")
    for name, value in {
        "HOME": tmp_path, "DB": data / "ledger.db",
        "ATTACH_DIR": data / "attachments", "LOCKFILE": data / "run.lock",
        "CONF_PATH": config, "CACHE": tmp_path / "absent-token.json",
    }.items():
        monkeypatch.setattr(run_check, name, str(value))
    for name, value in {
        "BACKUP_DIR": data / "backups", "SNAPSHOT_DIR": data / "snapshots",
        "LOGFILE": data / "run.log",
    }.items():
        monkeypatch.setattr(maintenance, name, str(value))
    drain = job_ops.drain_commands
    monkeypatch.setattr(
        job_ops, "drain_commands",
        lambda db, result: drain(db, result, str(data / "cmd")),
    )
    monkeypatch.setattr(run_check, "MCSAdapter", Adapter)
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: None)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kw: False)
    monkeypatch.setattr(semantic.jev, "JevClient", _BudgetJev)
    def flush(ledger, *args, **kwargs):
        _BudgetJev.events.append("notify")
        assert ledger.db.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert ledger.db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind='extract_v1'"
        ).fetchone()[0] == 1     # instant rule lane before notify
        return {"sent": 0}

    monkeypatch.setattr(notify_flush, "flush", flush)
    monkeypatch.setattr(sys, "argv", [
        "run_check", "--no-backfill",
    ])

    assert run_check.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    # the derive stage still selected the message for extract_llm —
    # endpoint-down just leaves it pending instead of an artifact
    assert result["extract_llm"]["selected"] == 1
    assert result["extract_llm"]["done"] == 0
    assert result["semantic"]["deferred"] == 1
    assert result["semantic"]["failed"] == 0
    assert _BudgetJev.instances and _BudgetJev.instances[0].calls > 0
    assert _BudgetJev.instances[0].requests_made == 0
    assert _BudgetJev.events.index("notify") < _BudgetJev.events.index("semantic")

    snapshot = data / "snapshots" / "ledger-snapshot.db"
    assert snapshot.exists()
    view = ledger.LedgerReader(str(snapshot))
    try:
        assert view.db.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert view.db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind='extract_v1'"
        ).fetchone()[0] == 1    # instant rule lane stays live
        assert view.db.execute(
            "SELECT COUNT(*) FROM notify_outbox WHERE kind='new_messages'"
        ).fetchone()[0] == 1
        job = view.db.execute(
            "SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'"
        ).fetchone()
        assert job["state"] == "pending" and job["attempts"] == 0
        assert view.db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind='semantic_bundle'"
        ).fetchone()[0] == 1
    finally:
        view.close()
