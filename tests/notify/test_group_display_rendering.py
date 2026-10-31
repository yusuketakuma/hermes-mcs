"""Group labels are display-only and follow existing notification privacy gates."""
import json
import sqlite3

import pytest

import notify_action_context
import notify_cards
import notify_digest
import notify_flush
import notify_history
import notify_render
import notify_views
from notify_testkit import (CFG, NOW, _begin, _card, _dispatch, _intent,
                            _latest_render, _msg, _patient, _receipt, _seed_thread, led)

__all__ = ["led"]


def _group(led, pid=1, name="合成連絡室"):
    led.db.execute("UPDATE patients SET project_type='group',group_id=?,group_name=? "
                   "WHERE project_id=?", (pid + 100, name, pid))


def test_group_name_is_only_a_display_name_and_refreshes_thread_fingerprint(led):
    _seed_thread(led)
    card = {"kind": "thread", "project_id": 1, "root_message_id": 100}
    before = notify_render.source_fingerprint(led.db, card)
    _group(led)
    assert notify_render.patient_name(led.db, 1) == "患者A"
    assert notify_render.patient_label(led.db, 1) == "合成連絡室"
    assert notify_render.patient_heading(led.db, 1) == "合成連絡室 #1"
    assert notify_cards._thread_name(led.db, card).startswith("💬 合成連絡室")
    assert notify_render.source_fingerprint(led.db, card) != before
    after = notify_render.source_fingerprint(led.db, card)
    led.db.execute("UPDATE patients SET group_name='合成別室' WHERE project_id=1")
    assert notify_render.source_fingerprint(led.db, card) != after
    assert notify_history._patient_identity(led.db, 1) == ["合成別室", 0]


def test_ideographic_space_in_group_name_displays_and_rename_changes_fingerprint(led):
    _seed_thread(led)
    card = {"kind": "thread", "project_id": 1, "root_message_id": 100}
    before = notify_render.source_fingerprint(led.db, card)
    _group(led, name="合成　連絡室")
    assert notify_render.project_display_name(led.db, 1) == "合成 連絡室"
    assert notify_render.patient_label(led.db, 1) == "合成 連絡室"
    assert notify_render.patient_heading(led.db, 1) == "合成 連絡室 #1"
    assert notify_cards._thread_name(led.db, card).startswith("💬 合成 連絡室")
    current = notify_render.source_fingerprint(led.db, card)
    assert current != before
    led.db.execute("UPDATE patients SET group_name=? WHERE project_id=1", ("合成　別室",))
    assert notify_render.project_display_name(led.db, 1) == "合成 別室"
    assert notify_render.patient_heading(led.db, 1) == "合成 別室 #1"
    assert notify_render.source_fingerprint(led.db, card) != current


def test_non_group_fingerprint_and_heading_are_unchanged(led):
    _seed_thread(led)
    card = {"kind": "thread", "project_id": 1, "root_message_id": 100}
    before = notify_render.source_fingerprint(led.db, card)
    heading = notify_render.patient_heading(led.db, 1)
    led.db.execute("UPDATE patients SET project_type='medical',group_id=101,group_name='合成室' WHERE project_id=1")
    assert notify_render.source_fingerprint(led.db, card) == before
    assert notify_render.patient_heading(led.db, 1) == heading
    assert notify_history._patient_identity(led.db, 1) == ["患者A", 0]


@pytest.mark.parametrize("group_id,name", [
    (None, "合成室"), (0, "合成室"), (-1, "合成室"), ("invalid", "合成室"),
    (5, None), (5, ""), (5, " \t "), (5, "合成\x00室"), (5, "合成\n室"),
    (5, "合" * 513),
])
def test_invalid_group_data_never_uses_patient_label(led, group_id, name):
    _patient(led)
    led.db.execute("UPDATE patients SET project_type='group',group_id=?,group_name=? WHERE project_id=1",
                   (group_id, name))
    assert notify_render.project_display_name(led.db, 1) == ""
    assert notify_render.patient_heading(led.db, 1) == "project 1"
    assert notify_history._patient_identity(led.db, 1) == ["", 0]


def test_group_name_cannot_create_mentions_or_ambiguous_action_targets(led):
    _patient(led)
    _group(led, name="合成 <@all>")
    led.db.execute("INSERT INTO messages(message_id,project_id,posted_at) VALUES(10,1,'2026-01-01T12:00')")
    context = {"project_id": 1, "source_message_id": 10}
    notify_action_context._confirm_target(led.db, context)
    assert "<@all>" not in context["target"]
    assert "project 1" in context["target"]
    assert "<@all>" not in notify_render.patient_heading(led.db, 1)


def test_old_snapshot_without_group_columns_preserves_label_and_identity():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE patients(project_id INTEGER PRIMARY KEY, patient_name TEXT, "
                   "station_name TEXT, is_archived INTEGER)")
        db.execute("INSERT INTO patients VALUES(1,'合成患者','合成施設',0)")
        assert notify_render.project_display_name(db, 1) == "合成患者"
        assert notify_render.patient_heading(db, 1) == "合成患者（合成施設）"
        assert notify_history._patient_identity(db, 1) == ["合成患者", 0]
        db.execute("ALTER TABLE patients ADD COLUMN project_type TEXT")
        db.execute("UPDATE patients SET project_type='group' WHERE project_id=1")
        assert notify_render.project_display_name(db, 1) == ""
        assert notify_render.patient_heading(db, 1) == "project 1"
        assert notify_history._patient_identity(db, 1) == ["", 0]
    finally:
        db.close()


def test_digest_name_gate_and_allowed_scope_apply_to_group_names(led):
    _patient(led, 1)
    _patient(led, 2)
    _group(led, 1, "合成室甲")
    _group(led, 2, "合成室乙")
    _msg(led, 100, pid=1, ts=int(NOW - 100))
    _msg(led, 200, pid=2, ts=int(NOW - 100))
    led.db.execute("UPDATE messages SET first_seen=?", (NOW - 50,))

    def render(names, allowed):
        parts = notify_digest.build(led.db, {}, NOW - 3600, NOW, names=names, allowed=allowed)
        return json.dumps(parts, ensure_ascii=False)

    assert "合成室甲" not in render(False, {1})
    visible = render(True, {1})
    assert "合成室甲" in visible
    assert "合成室乙" not in visible


def test_bounded_care_event_label_uses_safe_group_name(led):
    _patient(led)
    _group(led, name="合成室")
    assert notify_views._care_event_patient_label(led.db, 1) == "合成室"
    _group(led, name="合成\x00室")
    assert notify_views._care_event_patient_label(led.db, 1) == "project 1"


def test_display_identity_reads_type_and_name_in_one_statement(led):
    _patient(led)
    _group(led)
    statements = []
    led.db.set_trace_callback(statements.append)
    try:
        assert notify_render.project_display_name(led.db, 1) == "合成連絡室"
    finally:
        led.db.set_trace_callback(None)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1
    assert all(field in reads[0] for field in ("project_type", "group_id", "group_name", "patient_name"))


@pytest.mark.parametrize("state", ["sending", "unknown"])
def test_group_rename_waits_for_unsettled_delivery(led, state):
    _seed_thread(led)
    _group(led)
    led.db.commit()
    assert _dispatch(led, _intent(led))["cards"] == 1
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    if state == "unknown":
        assert _receipt(led, render, "0000000000000001", result="unknown", message_id=None)["applied"]
    before = dict(_latest_render(led))
    card_before = _card(led)
    led.db.execute("UPDATE patients SET group_name='合成改名' WHERE project_id=1")
    led.db.commit()
    notify_cards.sweep(led, CFG, now=NOW + 1)
    after = dict(_latest_render(led))
    assert after["delivery_id"] == before["delivery_id"]
    assert after["render_rev"] == before["render_rev"]
    assert after["state"] == before["state"]
    assert after["spec_json"] == before["spec_json"]
    assert _card(led)["source_fp"] == card_before["source_fp"]
    assert led.db.execute("SELECT COUNT(*) FROM notification_renders").fetchone()[0] == 1


@pytest.mark.parametrize("legacy,project_type,group_id,group_name,expected", [
    (False, "x" * 10000, 1, "合成室", "合成患者"),
    (False, b"x" * 10000, 1, "合成室", "合成患者"),
    (False, "group", "x" * 10000, "合成室", "project 1"),
    (False, "group", b"x" * 10000, "合成室", "project 1"),
    (False, "group", 1, "x" * 10000, "project 1"),
    (False, "group", 1, b"x" * 10000, "project 1"),
    (False, "group", 1, "合成室", "合成室"),
    (True, "x" * 10000, None, None, "合成患者"),
    (True, b"x" * 10000, None, None, "合成患者"),
    (True, "group", None, None, "project 1"),
])
def test_bounded_identity_never_materializes_large_metadata(legacy, project_type, group_id, group_name, expected):
    db = sqlite3.connect(":memory:")
    try:
        suffix = "" if legacy else ",group_id,group_name"
        db.execute("CREATE TABLE patients(project_id INTEGER PRIMARY KEY,patient_name,project_type" + suffix + ")")
        if legacy:
            db.execute("INSERT INTO patients VALUES(1,?,?)", ("合成患者", project_type))
        else:
            db.execute("INSERT INTO patients VALUES(1,?,?,?,?)", ("合成患者", project_type, group_id, group_name))

        def checked_text(value):
            assert len(value) <= 1024, "unbounded text reached Python"
            return value.decode("utf-8")

        def checked_row(cursor, values):
            assert all(not isinstance(value, (str, bytes)) or len(value) <= 1024 for value in values), \
                "unbounded value reached Python"
            return sqlite3.Row(cursor, values)

        db.text_factory = checked_text
        db.row_factory = checked_row
        assert notify_views._care_event_patient_label(db, 1) == expected
    finally:
        db.close()


@pytest.mark.parametrize("pid", [0, -1, -(2 ** 63)])
def test_signal_text_preserves_signed_sqlite_id_lookup(led, pid):
    led.db.execute("INSERT INTO patients(project_id,patient_name) VALUES(?,?)", (pid, "合成患者"))
    payload = {"text": "合成シグナル\n所在行", "project_id": pid}
    assert notify_flush._signal_text(led, payload, {}).split("\n")[1] == "合成患者（所在行）"
