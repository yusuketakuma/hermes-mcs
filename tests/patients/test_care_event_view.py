"""Synthetic care-event view coverage, authorization boundaries, and snapshots."""
from types import SimpleNamespace

import pytest

import ledger
import mcs_view
import notify_views
import patient_context_refresh
import patient_context_view
from test_care_event_sources import _event, _save
from test_care_event_sources import store as store


@pytest.fixture(autouse=True)
def context_stubs(monkeypatch):
    monkeypatch.setattr("mcs_util.load_config", lambda: {})
    monkeypatch.setattr(patient_context_refresh, "context_view_read", lambda *a, **k: (None, {
        "complete": False, "status": "scan_pending", "sources": {"pending": 1}}))


def test_care_events_cli_and_chat_display_source_and_distinct_dates(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    view = SimpleNamespace(db=store.db, meta={"generated_at": 1_790_000_000})
    result = mcs_view.View._patient_context(view, 1, "care_events", None, None, "")
    event, = result["items"]
    assert event["quote"] == body and event["event_day"] == "2026-09-01"
    assert event["posted_at"].startswith("2026-10-03")
    assert not store.db.in_transaction
    display = notify_views.patient_context_list(store.db, 1, "care_events", cfg={}, since=0, now=1_790_000_000)
    assert any(body in row["text"] for row in display["items"])
    assert any("2026-09-01" in row["source"] for row in display["items"])
    assert any("関係は未照合" in line for line in display["head"])
    assert mcs_view._parser().parse_args(
        ["patient_context", "--project", "1", "--view", "care_events"]).view == "care_events"


def test_same_borrowed_snapshot_and_no_commit_after_concurrent_source_change(store, tmp_path):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snapshot"))
    view = mcs_view.View(snapshot)
    try:
        from extract_testkit import _message
        store.save_messages([_message(mid=1, body="訂正後の合成本文")])
        event, = view.read("patient_context", project=1, view="care_events")["items"]
        assert event["quote"] == body
        assert view.db.in_transaction
    finally:
        view.close()


def test_forbidden_and_other_patient_scope_do_not_expose_source(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    store.ensure_patient(2)
    result = patient_context_view.care_events(store.db, 2, cfg={})
    assert result["items"] == [] and body not in str(result)
    with store.db:
        store.db.execute("UPDATE patients SET fetch_state='forbidden',patient_name='合成患者名' WHERE project_id=1")
    result = patient_context_view.care_events(store.db, 1, cfg={})
    assert result["coverage"]["state"] == "scope_unavailable" and body not in str(result)
    display = notify_views.patient_context_list(store.db, 1, "care_events", cfg={}, since=0, now=1_790_000_000)
    assert not display["items"] and "確認できません" in display["empty"]
    assert "合成患者名" not in str(display) and body not in str(display)


def test_existing_medication_view_still_handles_present_and_missing_post(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    present = notify_views.meds_view(store.db, 1, 1, cfg={})
    missing = notify_views.meds_view(store.db, 1, 999, cfg={})
    assert "選択した投稿の薬剤" in present["title"]
    assert "この投稿の薬剤情報を確認できません" in missing["empty"]


def test_scope_not_proven_before_resource_limit_never_reads_patient_label(store, monkeypatch):
    import care_event_sources
    with store.db:
        store.db.execute("UPDATE patients SET fetch_state='forbidden',patient_name='合成患者名' WHERE project_id=1")
    monkeypatch.setattr(care_event_sources, "_resource_preflight", lambda *a, **k: "row_limit")
    def forbidden(*args, **kwargs):
        raise AssertionError("patient label read before scope was proven")
    monkeypatch.setattr(notify_views, "patient_label", forbidden)
    display = notify_views.patient_context_list(store.db, 1, "care_events", cfg={}, since=0, now=1_790_000_000)
    assert display["title"].startswith("患者 — ")
    assert "合成患者名" not in str(display) and "確認できません" in display["empty"]


@pytest.mark.parametrize("state", ["schema_unknown", "resource_unconfirmed", "scope_unknown", "scope_unavailable"])
def test_any_unproven_scope_uses_anonymous_title(store, monkeypatch, state):
    import care_event_sources
    coverage = {"state": state, "source_complete": False}
    monkeypatch.setattr(care_event_sources, "collect",
                        lambda *a, **k: {"project_id": 1, "events": [], "coverage": coverage})
    def forbidden(*args, **kwargs):
        raise AssertionError("patient label read before scope was proven")
    monkeypatch.setattr(notify_views, "patient_label", forbidden)
    display = notify_views.patient_context_list(store.db, 1, "care_events", cfg={}, since=0, now=1_790_000_000)
    assert display["title"].startswith("患者 — ") and "確認できません" in display["empty"]


@pytest.mark.parametrize("name", ["x" * 1025, "合成\0患者", "合成患者".encode(), None])
def test_observed_scope_with_oversized_or_invalid_name_is_anonymous(store, monkeypatch, name):
    with store.db:
        store.db.execute("UPDATE patients SET patient_name=? WHERE project_id=1", (name,))
    def forbidden(*args, **kwargs):
        raise AssertionError("unbounded patient label read")
    monkeypatch.setattr(notify_views, "patient_label", forbidden)
    display = notify_views.patient_context_list(store.db, 1, "care_events", cfg={}, since=0, now=1_790_000_000)
    assert display["title"].startswith("患者 — ")
    assert "x" * 50 not in str(display) and "合成" not in str(display)


def test_old_schema_without_patient_name_is_anonymous(store, monkeypatch):
    class OlderSchema:
        def __init__(self, db):
            self.db = db

        @property
        def in_transaction(self):
            return self.db.in_transaction

        def execute(self, sql, params=()):
            result = self.db.execute(sql, params)
            if sql == "PRAGMA table_info(patients)":
                return [row for row in result if row[1] != "patient_name"]
            return result

    def forbidden(*args, **kwargs):
        raise AssertionError("unbounded patient label read")
    monkeypatch.setattr(notify_views, "patient_label", forbidden)
    display = notify_views.patient_context_list(OlderSchema(store.db), 1, "care_events",
                                                 cfg={}, since=0, now=1_790_000_000)
    assert display["title"].startswith("患者 — ")


def test_bounded_normal_patient_name_retains_existing_label(store):
    with store.db:
        store.db.execute("UPDATE patients SET patient_name='合成患者名' WHERE project_id=1")
    display = notify_views.patient_context_list(store.db, 1, "care_events", cfg={}, since=0, now=1_790_000_000)
    assert display["title"].startswith("合成患者名 — ")


def test_invalid_or_unacquired_source_does_not_look_complete(store):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    with store.db:
        store.db.execute("UPDATE messages SET body_html='<p>別の本文</p>' WHERE message_id=1")
    result = patient_context_view.care_events(store.db, 1, cfg={})
    assert result["items"] == [] and not result["coverage"]["source_complete"]
    assert result["coverage"]["invalid_source"] == 1
    display = notify_views.patient_context_list(store.db, 1, "care_events", cfg={}, since=0, now=1_790_000_000)
    assert not display["items"] and "確認できません" in display["empty"]
    assert any("未完了" in line for line in display["head"])


def test_bounded_scope_and_incomplete_fetch_are_visible(store, monkeypatch):
    import care_event_sources
    for mid in (1, 2):
        body = "本人は2026年9月1日に退院しました。"
        _save(store, mid, body, records=[_event(body)])
    original = care_event_sources.collect
    monkeypatch.setattr(care_event_sources, "collect",
                        lambda db, project_id, *, cfg: original(db, project_id, cfg=cfg, limit=1))
    with store.db:
        store.db.execute("UPDATE patients SET fetch_state='incomplete' WHERE project_id=1")
    result = patient_context_view.care_events(store.db, 1, cfg={})
    assert result["coverage"]["truncated"] and not result["coverage"]["source_complete"]
    display = notify_views.patient_context_list(store.db, 1, "care_events", cfg={}, since=0, now=1_790_000_000)
    assert any("一部に限定" in line for line in display["head"])
    assert any("未完了" in line for line in display["head"])


def test_event_screen_never_loads_unbounded_context_or_generic_coverage(store, monkeypatch):
    body = "本人は2026年9月1日に退院しました。"
    _save(store, 1, body, records=[_event(body)])
    def forbidden(*args, **kwargs):
        raise AssertionError("unbounded reader invoked")
    monkeypatch.setattr(patient_context_view, "evidence", forbidden)
    monkeypatch.setattr(notify_views, "_coverage_line", forbidden)
    display = notify_views.patient_context_list(store.db, 1, "care_events", cfg={}, since=0, now=1_790_000_000)
    assert any(body in row["text"] for row in display["items"])


def test_uncertain_future_completion_is_not_ordered_or_displayed_as_confirmed_day(store):
    future = "本人は2026年12月1日に退院しました。"
    known = "本人は2026年9月1日に退院しました。"
    _save(store, 1, future, records=[_event(future)])
    _save(store, 2, known, records=[_event(known)])
    result = patient_context_view.care_events(store.db, 1, cfg={})
    assert [item["message_id"] for item in result["items"]] == [2, 1]
    assert result["items"][1]["date_uncertain"] is True
    assert result["items"][1]["quote"] == future
    display = notify_views.patient_context_list(store.db, 1, "care_events", cfg={}, since=0, now=1_790_000_000)
    future_row = next(row for row in display["items"] if future in row["text"])
    assert "日付未確認" in future_row["source"] and "2026-12-01" not in future_row["source"]
    assert any("日付未確認" in line for line in display["head"])
