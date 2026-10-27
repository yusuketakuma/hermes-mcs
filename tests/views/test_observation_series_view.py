"""Synthetic end-to-end observation views on the caller's DB snapshot."""
from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

import ledger
import mcs_view
import notify_views
import patient_context_refresh as refresh
import patient_context_view as views
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of


def seed(db, mid, body, evidence=None, *, name="Cr", value="1.0", unit="mg/dL", condition="空腹"):
    msg = replace(_message(mid, body=body), posted_at="2026-10-03T09:00:00+09:00")
    db.save_messages([msg], project_id=1)
    quote = evidence or body
    details = [{"key": key, "value": item_value, "evidence": quote} for key, item_value in (
        ("observation_name", name), ("value", value), ("unit", unit),
        ("condition", condition), ("measured_on", "2026-10-01")) if item_value is not None]
    item = {"category": "observations", "subject": "patient", "text": quote,
            "evidence": quote, "details": details}
    _artifact(db, "extract_llm", mid, {"patient_context": [item]}, {"hash": _hash_of(db, mid)})


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    db = _db(tmp_path, ())
    monkeypatch.setattr(refresh, "context_view_input", lambda *a, **k: None)
    monkeypatch.setattr(refresh, "inference_coverage", lambda *a, **k: {"complete": False,
        "status": "scan_pending", "sources": {"pending": 1}})
    monkeypatch.setattr("mcs_util.load_config", lambda: {})
    yield db
    db.close()


def test_cli_and_notification_use_the_same_verified_original(ctx):
    body = "本人は2026-10-01の空腹時の採血 Cr 1.0 mg/dL。"
    seed(ctx, 1, body)
    view = SimpleNamespace(db=ctx.db, meta={"generated_at": 1_790_000_000})
    got = mcs_view.View._patient_context(view, 1, "observations", None, None, "")
    assert len(got["series"]) == 1 and got["series"][0]["event_date_kind"] == "measured_on"
    assert got["source_checks"] == {"eligible": 1, "selected": 1, "verified": 1,
                                     "unavailable": 0, "limited": False}
    display = notify_views.patient_context_list(ctx.db, 1, "observations", cfg={}, since=0, now=1_790_000_000)
    assert any(body in row["text"] for row in display["items"])
    assert any("原文照合: 1 / 1投稿" in line for line in display["head"])
    assert mcs_view._parser().parse_args(["patient_context", "--project", "1", "--view", "observations"]).view == "observations"


def test_observation_view_borrows_published_snapshot_through_source_revision(ctx, tmp_path):
    original = "本人は2026-10-01の空腹時の採血 Cr 1.0 mg/dL。"
    seed(ctx, 1, original)
    snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snapshot"))
    view = mcs_view.View(snapshot)
    try:
        ctx.save_messages([replace(_message(1, body="訂正後の合成本文"),
                                   posted_at="2026-10-03T09:00:00+09:00")], project_id=1)
        result = view.read("patient_context", project=1, view="observations")
        assert view.db.in_transaction
        assert result["series"][0]["records"][0]["items"][0]["quote"] == original
        assert result["source_checks"]["verified"] == 1
    finally:
        view.close()


def test_full_original_context_is_required_and_partial_quote_is_not_shown(ctx):
    quote = "本人は2026-10-01の空腹時の採血 Cr 1.0 mg/dL。"
    seed(ctx, 1, "家族の話です。" + quote, evidence=quote)
    ev = views.evidence(ctx.db, 1, cfg={})
    result = views.observations(ctx.db, ev)
    assert result["series"] == [] and result["unconfirmed"]
    assert quote not in str(result)
    display = notify_views.patient_context_list(ctx.db, 1, "observations", cfg={}, since=0, now=1_790_000_000)
    assert quote not in str(display)
    assert any("対応未確認" in row["text"] for row in display["items"])


def test_changed_original_and_limited_source_checks_remain_unconfirmed(ctx):
    seed(ctx, 1, "本人は2026-10-01の空腹時の採血 Cr 1.0 mg/dL。")
    seed(ctx, 2, "本人は2026-10-01の空腹時の採血 Cr 1.0 mg/dL。")
    ev = views.evidence(ctx.db, 1, cfg={})
    limited = views.observations(ctx.db, ev, source_limit=1)
    assert limited["source_checks"]["limited"] is True
    assert len(limited["unconfirmed"]) >= 1
    ctx.db.execute("UPDATE messages SET body_html=? WHERE project_id=1 AND message_id=2", ("<p>変更済み</p>",))
    changed = views.observations(ctx.db, ev)
    assert changed["source_checks"]["unavailable"] == 1
    assert all(record["items"][0]["message_id"] != 2 for series in changed["series"] for record in series["records"])


def test_embedded_nul_never_returns_large_original_and_preserves_scope_and_limits(ctx):
    body = '本人は2026-10-01の空腹時の採血 Cr 1.0 mg/dL。'
    for mid in (1, 2, 3):
        seed(ctx, mid, body)
    ev = views.evidence(ctx.db, 1, cfg={})
    items = [item for item in ev['items'] if item.get('category') == 'observations'
             and item.get('message_id') in (1, 2, 3)]
    assert len(items) == 3
    items.sort(key=lambda item: item['message_id'])
    ctx.ensure_patient(2)
    ctx.save_messages([_message(4, pid=2, body=body)], project_id=2)
    foreign_hash = _hash_of(ctx, 4)
    ev['items'] = items + [{**items[0], 'message_id': 4, 'content_hash': foreign_hash}]
    huge = 'x\0' + 'A' * 100_000
    ctx.db.execute('UPDATE messages SET body_text=? WHERE project_id=1 AND message_id=2', (huge,))
    ctx.db.execute('UPDATE messages SET body_html=? WHERE project_id=1 AND message_id=3', (huge,))
    returned = []

    def inspect(cursor, values):
        names = tuple(col[0] for col in cursor.description or ())
        if names == ('message_id', 'content_hash', 'body_text', 'body_html'):
            returned.append(values[0])
        return sqlite3.Row(cursor, values)

    ctx.db.row_factory = inspect
    try:
        result = views.observations(ctx.db, ev)
        limited = views.observations(ctx.db, ev, source_limit=2)
    finally:
        ctx.db.row_factory = sqlite3.Row
    assert returned == [1, 1]
    assert result['source_checks'] == {'eligible': 4, 'selected': 4, 'verified': 1,
                                       'unavailable': 3, 'limited': False}
    assert limited['source_checks'] == {'eligible': 4, 'selected': 2, 'verified': 1,
                                        'unavailable': 1, 'limited': True}
    assert len(result['series']) == 1
    assert all(record['items'][0]['message_id'] == 1 for series in result['series'] for record in series['records'])


@pytest.mark.parametrize("body,name,value,unit", [
    ("本人は2026-10-01に採血 Cr 1.0 mg/dL。", "Cr", "1.0", "mg/dL"),
    ("本人は2026-10-01に体温36.5 ℃を測定しました。", "体温", "36.5", "℃"),
    ("本人は2026-10-01にSpO2 97 %を測定しました。", "SpO2", "97", "%"),
])
def test_condition_absent_lab_and_vitals_are_displayed_as_written(ctx, body, name, value, unit):
    seed(ctx, 1, body, name=name, value=value, unit=unit, condition=None)
    ev = views.evidence(ctx.db, 1, cfg={})
    result = views.observations(ctx.db, ev)
    assert len(result["series"]) == 1
    assert result["series"][0]["condition"] is None
    display = notify_views.patient_context_list(ctx.db, 1, "observations", cfg={}, since=0, now=1_790_000_000)
    assert any(body in row["text"] and "条件の記載なし" in row["group"] for row in display["items"])
    assert "None" not in str(display)
