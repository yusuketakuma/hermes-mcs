"""An unreadable newest karte_summary artifact — syntax-invalid, or valid
to SQLite but too deep for Python 3.11's decoder (RecursionError) —
leaves the 🧾 summary readable: the line says 未取得, never 空 and never
the older summary; both rows are kept. Synthetic ledger."""
import json
import sqlite3

import pytest

import notify_views
from notify_testkit import _patient, led as led

__all__ = ["led"]

DEPTH = 999


@pytest.mark.parametrize("bad", ["deep", "syntax"])
def test_an_undecodable_newest_summary_reads_as_not_fetched(led, bad, monkeypatch):
    _patient(led, 1)
    led.karte_summary_store(1, 10, {
        "comment": "合成の旧サマリー", "updated_at": "2026-09-30T10:00:00+09:00",
        "user": {"profession": "薬剤師", "name": "SYNTH"}, "is_editable": True})
    content = "[" * DEPTH + "]" * DEPTH if bad == "deep" else '{"comment": '
    assert sqlite3.connect(":memory:").execute(
        "SELECT json_valid(?)", (content,)).fetchone()[0] == (bad == "deep")
    failures = []
    if bad == "deep":
        original_loads = json.loads

        def decode(raw, *args, **kwargs):
            if raw == content:
                failures.append(raw)
                raise RecursionError("synthetic decoder depth failure")
            return original_loads(raw, *args, **kwargs)

        monkeypatch.setattr(json, "loads", decode)
    led.artifact_add("karte_summary", content, project_id=1)
    body = notify_views.patient_summary_text(led.db, 1)[1]
    if bad == "deep":
        assert failures
    assert "連携サマリー（MCS）: 未取得" in body
    assert "合成の旧サマリー" not in body and "連携サマリー（MCS）: 空" not in body
    assert led.db.execute("SELECT COUNT(*) FROM artifacts "
                          "WHERE kind='karte_summary'").fetchone()[0] == 2
