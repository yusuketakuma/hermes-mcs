"""Readers keep target values and display only still-bound synthetic antecedents."""
import json

import pytest

import extract_llm as llm
import notify_views
import patient_state_context as state
import read_model
import rollup
import structured_view
from extract_testkit import _hash, _ledger, _message
from test_pharmacy_state import context


def seed(led):
    prior = "本人の合成薬Aの保管場所は冷蔵庫。"
    body = "本人の合成薬Aの保管場所は前と同じ場所です。"
    led.save_messages([_message(mid=1, body=prior, posted_at="2026-10-01T10:00:00+09:00"),
                       _message(mid=2, body=body, posted_at="2026-10-02T10:00:00+09:00")])
    aid = led.artifact_add(llm.KIND, json.dumps({"patient_context": [context(prior, key="storage_place",
             values=("冷蔵庫",))]}), project_id=1, message_id=1, model=llm.MODEL,
             meta={"hash": _hash(led, 1), "extract_version": llm.EXTRACT_VERSION, "actual_model": llm.MODEL,
                   "integrity": {"validation": {"items_dropped": 0, "evidence_dropped": 0}}})
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=2").fetchone())
    frame = state.prepare(led, row, model=llm.MODEL, contract="synthetic/1", attempt={},
                          enabled=True, base_context=None, version=llm.EXTRACT_VERSION)
    item = context(body, key="storage_place", values=("前と同じ場所",))
    item["details"][-1]["refers_to"] = "S1"
    doc = llm._validate({"patient_context": [item]}, body, references=frame["refs"])
    r = {**row, "_frame": frame, "_context": frame["text"], "_target": ("", llm.MODEL)}
    assert llm._replace_current(led, r, json.dumps(doc), ctx=True,
                              integrity={"validation": {"items_dropped": 0, "evidence_dropped": 0}})
    return aid, row, rollup.build_rollup(led, 1)


def test_shared_and_private_lines_annotate_the_exact_frozen_reference(tmp_path):
    led = _ledger(tmp_path)
    try:
        _, row, cached = seed(led)
        items = read_model.message_patient_context(led.db, row)
        detail = items[0]["details"][-1]
        assert detail["value"] == "前と同じ場所" and detail["refers_to"]["state"] == "current"
        assert detail["refers_to"]["value"] == "冷蔵庫"
        assert "（参照: 10-01「冷蔵庫」）" in structured_view._pharmacy_detail_lines(led.db, 2)[0]
        assert any("（参照: 10-01「冷蔵庫」）" in line
                   for line in notify_views._pharmacy_state_lines(led.db, 1, cached))
        aggregate = read_model.message_patient_context(led.db, row, "aggregate")
        assert "冷蔵庫" not in json.dumps(aggregate, ensure_ascii=False)
    finally:
        led.close()


@pytest.mark.parametrize("change", ["body", "extraction", "snapshot", "foreign"])
def test_updated_references_do_not_show_the_frozen_value_or_substitute_a_new_one(tmp_path, change):
    led = _ledger(tmp_path)
    try:
        aid, row, cached = seed(led)
        if change == "body":
            led.save_messages([_message(mid=1, body="本人の合成薬Aの保管場所は新しい薬品庫。")])
        elif change == "extraction":
            with led.db:
                led.db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?", (aid,))
        elif change == "snapshot":
            with led.db:
                led.db.execute("UPDATE artifacts SET content='not json' WHERE kind=?", (state.KIND,))
        else:
            # A different project's target must not borrow this frame.
            row = {**row, "project_id": 2}
            assert read_model.message_patient_context(led.db, row) == []
            return
        items = read_model.message_patient_context(led.db, row)
        ref = items[0]["details"][-1]["refers_to"]
        assert ref == {"state": "updated"}
        line = structured_view._pharmacy_detail_lines(led.db, 2)[0]
        assert "前と同じ場所" in line and "参照先は更新済み" in line
        assert "冷蔵庫" not in line and "新しい薬品庫" not in line
        private = next(line for line in notify_views._pharmacy_state_lines(led.db, 1, cached)
                       if "投稿#2" in line)
        assert "参照先は更新済み" in private and "冷蔵庫" not in private
    finally:
        led.close()


def test_reference_metadata_does_not_change_the_target_body_fingerprint(tmp_path):
    led = _ledger(tmp_path)
    try:
        _, row, _ = seed(led)
        raw = json.loads(led.artifacts(llm.KIND, message_id=2)[0]["content"])
        before = rollup.pharmacy_state_items(raw, row)[0]["fingerprint"]
        raw["patient_context"][0]["details"][-1].pop("refers_to")
        assert rollup.pharmacy_state_items(raw, row)[0]["fingerprint"] == before
    finally:
        led.close()
