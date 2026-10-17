"""Patient context v3 views: one evidence list, thin projections, clinical distinctions kept."""
import json
import sys
from types import SimpleNamespace

import pytest

import mcs_view
import patient_context_view as pcv
from patient_context import extract_context
from semantic_testkit import _message
from test_read_model import _artifact, _db, _hash_of

DAY = 86400
T0 = 1_790_000_000
BODIES = {1: "病名: 架空病A", 2: "対象薬: 架空薬\n残薬: 3日分", 3: "対象薬: 架空薬\n残薬: 10日分", 4: "病名: 架空病B",
          5: "次回確認事項: 架空薬の残数", 6: "経過: 架空病院から退院"}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    db = _db(tmp_path, ())
    db.save_messages([_message(mid, body=body) for mid, body in BODIES.items()], project_id=1)
    for mid in BODIES:
        db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (T0 + mid * DAY, mid))
        body = db.db.execute("SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
        _artifact(db, "extract_v1", mid, {"patient_context": extract_context(body)}, {"hash": _hash_of(db, mid)})
    nightly = {"value": None}
    import patient_nightly
    monkeypatch.setitem(sys.modules, "patient_nightly", SimpleNamespace(
        nightly_view_input=lambda conn, pid, cfg=None: nightly["value"],
        night_window=patient_nightly.night_window))
    yield SimpleNamespace(db=db, nightly=nightly)
    db.close()


def test_background_keeps_older_distinct_items_dated(ctx):
    view = pcv.background(pcv.evidence(ctx.db.db, 1))
    diagnoses = next(s for s in view["sections"] if s["category"] == "diagnoses")
    assert [i["text"] for i in diagnoses["items"]] == ["架空病B", "架空病A"]
    assert all(i["posted_at_ts"] and i["quote"] and i["sender"] == "sender" for i in diagnoses["items"])
    assert view["summary"] == [] and view["as_of"] is None


def test_edited_or_deleted_source_drops_its_items(ctx):
    ctx.db.db.execute("UPDATE messages SET content_hash='edited' WHERE message_id=1")
    ctx.db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=4")
    ctx.db.db.commit()
    texts = {i["text"] for i in pcv.evidence(ctx.db.db, 1)["items"]}
    assert "架空病A" not in texts and "架空病B" not in texts


def test_changes_separate_new_reports_from_older_records_found_at_night(ctx):
    ctx.nightly["value"] = {"as_of": T0 + 9 * DAY, "summary": [{"text": "s", "fact_ids": ["f1"]}],
                            "coverage": {"analyzed": 6}, "facts": [
        {"id": "f1", "category": "living", "text": "家族が服薬支援", "quote": "家族が服薬支援",
         "subject": "family", "message_id": 1, "content_hash": "h", "posted_at_ts": T0 + DAY,
         "discovery": "backfill_found", "found_night_id": "2026-10-18"},
        {"id": "f2", "category": "diagnoses", "text": "病名: 架空病B", "quote": "病名: 架空病B", "message_id": 4,
         "discovery": "new_report", "temporality": "current"}]}
    ev = pcv.evidence(ctx.db.db, 1)
    by_id = {f["id"]: f for f in ev["nightly_facts"]}
    assert [by_id[i]["text"] for s in ev["summary"] for i in s["fact_ids"]] == ["家族が服薬支援"]
    merged = next(i for i in ev["items"] if i["text"] == "架空病B")
    assert merged["temporality"] == "current" and merged["quote"] == "病名: 架空病B"
    got = pcv.changes(ctx.db.db, ev, T0 + 4 * DAY)["by_discovery"]
    assert got["backfill_found"] == []  # no found_at: the time of discovery is not known


def _observe(db, mid, first_seen):
    db.db.execute("UPDATE messages SET first_seen=? WHERE message_id=?", (first_seen, mid))


def test_changes_classify_by_observation_record(ctx):
    since = T0 + 4 * DAY + 3600
    for mid in BODIES:
        _observe(ctx.db, mid, T0 + mid * DAY + 60)
    _observe(ctx.db, 2, since + 10)  # posted day 2, first fetched after since: late fetch
    ctx.db.db.execute("INSERT INTO message_revisions VALUES(1,1,'h2','h1','full',?)", (since + 20,))
    ctx.db.db.execute("INSERT INTO message_revisions VALUES(3,1,'h3','h2','deleted',?)", (since + 30,))
    ctx.db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=3")
    ctx.db.db.commit()
    ctx.nightly["value"] = {"as_of": T0, "facts": [
        {"category": "living", "text": "架空発見", "quote": "架空発見", "message_id": 4, "posted_at_ts": T0,
         "discovery": "backfill_found", "found_at": since + 40},
        {"category": "living", "text": "架空旧発見", "quote": "架空旧発見", "message_id": 4, "posted_at_ts": T0,
         "discovery": "backfill_found", "found_at": since - 40}]}
    got = pcv.changes(ctx.db.db, pcv.evidence(ctx.db.db, 1), since)["by_discovery"]
    assert {i["message_id"] for i in got["new_report"]} == {5, 6}
    assert {i["message_id"] for i in got["retrospective"]} == {2}
    assert {i["message_id"] for i in got["edited"]} == {1}
    assert got["deleted"] == [{"message_id": 3, "observed_at": since + 30}]
    assert [i["text"] for i in got["backfill_found"]] == ["架空発見"]
    assert "残薬" not in json.dumps(got["deleted"], ensure_ascii=False)


def test_unpublished_nightly_adds_nothing(ctx):
    ctx.nightly["value"] = None
    ev = pcv.evidence(ctx.db.db, 1)
    assert all(i["discovery"] is None for i in ev["items"]) and ev["coverage"]["nightly"] is None
    assert ev["nightly_facts"] == [] and ev["summary"] == []


def test_replies_never_claim_untreated_or_formal_completion(ctx):
    roll = {"recent_requests": [{"mid": 2, "at": "10-01", "ctx": "架空依頼"},
                                {"mid": 3, "at": "10-02", "ctx": "架空依頼2", "reply_state": "done"}],
            "next_planned": "架空訪問"}
    ctx.db.db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES"
                      "('patient_rollup',1,?,'{}',0)", (json.dumps(roll, ensure_ascii=False),))
    got = pcv.replies(ctx.db.db, 1)
    assert [r["reply"] for r in got["items"]] == ["返答が見つからない", "返信あり（完了・記録上）"]
    assert "未対応" not in json.dumps(got, ensure_ascii=False)
    note = pcv.previsit(ctx.db.db, pcv.evidence(ctx.db.db, 1), T0 + 7 * DAY)
    assert [r["message_id"] for r in note["unanswered"]] == [2]
    assert note["next_planned"] == "架空訪問"
    assert [i["text"] for i in note["written_checks"]] == ["架空薬の残数"]


def test_previsit_lists_every_check_once_per_post_without_fixed_cap(ctx):
    roll = {"recent_requests": [{"mid": 5, "ctx": "架空依頼A"}, {"mid": 2, "ctx": "架空依頼B"},
                                {"mid": 3, "ctx": "架空依頼C"}, {"mid": 4, "ctx": "架空依頼D"}]}
    ctx.db.db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES"
                      "('patient_rollup',1,?,'{}',0)", (json.dumps(roll, ensure_ascii=False),))
    note = pcv.previsit(ctx.db.db, pcv.evidence(ctx.db.db, 1), T0 + 7 * DAY)
    assert [i["message_id"] for i in note["written_checks"]] == [5]  # written follow-up first
    assert [r["message_id"] for r in note["unanswered"]] == [2, 3, 4]  # post 5 not repeated; no cap


def test_residual_values_without_written_dates_are_not_assumed_simultaneous(ctx):
    got = pcv.discrepancies(pcv.evidence(ctx.db.db, 1))["items"]
    assert len(got) == 1 and got[0]["identity"] == ["架空薬"] and got[0]["kind"] == "time_unknown"
    assert sorted(r["value"] for r in got[0]["records"]) == ["10日分", "3日分"]


OBS = "検査・観測値\n検査項目: {}\n測定値: {}\n単位: {}\n測定日: {}"


@pytest.mark.parametrize("posts,kind", [
    ([("血糖", "120", "mg/dL", "10月1日"), ("血糖", "130", "mg/dL", "10月2日")], "change_over_time"),
    ([("血糖", "120", "mg/dL", "10月1日"), ("血糖", "130", "mg/dL", "10月1日")], "differing_records"),
    ([("血糖", "120", "mg/dL", "10月1日"), ("体重", "45", "kg", "10月1日")], None),
    ([("血糖", "120", "mg/dL", "10月1日"), ("血糖", "6.5", "%", "10月1日")], None)])
def test_observations_compare_only_the_same_named_item_and_unit_by_written_date(tmp_path, monkeypatch, posts, kind):
    import patient_nightly
    monkeypatch.setitem(sys.modules, "patient_nightly", SimpleNamespace(
        nightly_view_input=lambda *a, **k: None, night_window=patient_nightly.night_window))
    db = _db(tmp_path, ())
    db.save_messages([_message(n + 1, body=OBS.format(*p)) for n, p in enumerate(posts)], project_id=1)
    for mid in (1, 2):
        body = db.db.execute("SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
        _artifact(db, "extract_v1", mid, {"patient_context": extract_context(body)}, {"hash": _hash_of(db, mid)})
    got = pcv.discrepancies(pcv.evidence(db.db, 1))["items"]
    assert [g["kind"] for g in got] == ([kind] if kind else [])
    db.close()


def test_every_matching_item_receives_nightly_attributes(ctx):
    line = "対象薬: 架空薬"
    ctx.nightly["value"] = {"as_of": T0, "facts": [
        {"category": "medication_management", "text": line + "\n残薬: 3日分", "quote": line + "\n残薬: 3日分",
         "message_id": 2, "discovery": "backfill_found", "found_night_id": "2026-10-18"}]}
    items = [i for i in pcv.evidence(ctx.db.db, 1)["items"] if i["message_id"] == 2]
    assert len(items) == 2 and all(i["discovery"] == "backfill_found" for i in items)


def test_replies_drop_requests_whose_post_is_gone(ctx):
    roll = {"recent_requests": [{"mid": 2, "ctx": "架空依頼"}, {"mid": 3, "ctx": "架空依頼2"}]}
    ctx.db.db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES"
                      "('patient_rollup',1,?,'{}',0)", (json.dumps(roll, ensure_ascii=False),))
    ctx.db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=3")
    ctx.db.db.commit()
    assert [r["message_id"] for r in pcv.replies(ctx.db.db, 1)["items"]] == [2]


def test_oldest_long_term_item_survives_long_history(ctx):
    ctx.db.save_messages([_message(100 + n, body=f"架空連絡{n}") for n in range(80)], project_id=1)
    ev = pcv.evidence(ctx.db.db, 1)
    assert ev["items"][-1]["text"] == "架空病A"
    assert ev["coverage"]["read_model"]["collection"]["messages"] == 86


def test_timeline_includes_medication_and_symptom_mentions_as_written(ctx):
    _artifact(ctx.db, "extract_llm", 4, {"meds": [{"name": "架空錠", "dose": "1錠", "status": "planned",
                                                  "subject": "patient"}],
                                         "symptoms": [{"text": "架空のむくみ", "negated": True}]},
              {"hash": _hash_of(ctx.db, 4), "extract_version": 1})
    rows = [i for i in pcv.timeline(pcv.evidence(ctx.db.db, 1))["items"] if i["category"] in ("medications", "symptoms")]
    assert sorted((r["text"], json.dumps(r["mention"], sort_keys=True)) for r in rows) == sorted([
        ("架空錠", json.dumps({"dose": "1錠", "status": "planned"}, sort_keys=True)),
        ("架空のむくみ", json.dumps({"negated": True}))])


def test_timeline_is_oldest_first_and_transition_lists_missing_records(ctx):
    ev = pcv.evidence(ctx.db.db, 1)
    assert list(dict.fromkeys(i["message_id"] for i in pcv.timeline(ev)["items"])) == [1, 2, 3, 4, 5, 6]
    course = pcv.transition_course(ev)["items"]
    assert len(course) == 1 and course[0]["anchor"]["message_id"] == 6
    assert "adl" in course[0]["no_record"]


def test_role_order_names_existing_categories():
    from patient_context import CATEGORIES
    assert all(c in CATEGORIES for cats in pcv.ROLE_ORDER.values() for c in cats)


def test_role_order_only_reorders(ctx):
    view = pcv.background(pcv.evidence(ctx.db.db, 1))
    ordered = pcv.role_order(view, "pharmacy")
    assert ordered["sections"][0]["category"] == "medication_management"
    assert sorted(map(json.dumps, ordered["sections"])) == sorted(map(json.dumps, view["sections"]))


def test_cli_passes_current_settings_to_the_nightly_gate(ctx, monkeypatch):
    seen = []
    monkeypatch.setitem(sys.modules, "patient_nightly", SimpleNamespace(
        nightly_view_input=lambda conn, pid, cfg=None: seen.append(cfg)))
    monkeypatch.setattr("mcs_util.load_config", lambda *a, **k: {"extract": {"patient_context_nightly": {
        "enabled": True, "publication": False}}})
    mcs_view.View._patient_context(SimpleNamespace(db=ctx.db.db, meta={"generated_at": T0}), 1,
                                   "background", None, None, None)
    assert seen == [{"extract": {"patient_context_nightly": {"enabled": True, "publication": False}}}]


def test_cli_view_dispatch(ctx):
    view = SimpleNamespace(db=ctx.db.db, meta={"generated_at": T0 + 10 * DAY})
    got = mcs_view.View._patient_context(view, 1, "background", None, None, "nurse")
    assert got["view"] == "background"
    args = mcs_view._parser().parse_args(["patient_context", "--project", "1", "--view", "changes", "--since", "5"])
    assert (args.kind, args.view, args.since) == ("patient_context", "changes", 5)


def test_real_nightly_helper_without_artifact_falls_back_to_chat_items(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "patient_nightly", raising=False)
    db = _db(tmp_path, ())
    db.save_messages([_message(1, body=BODIES[1])], project_id=1)
    body = db.db.execute("SELECT body_text FROM messages WHERE message_id=1").fetchone()[0]
    _artifact(db, "extract_v1", 1, {"patient_context": extract_context(body)}, {"hash": _hash_of(db, 1)})
    ev = pcv.evidence(db.db, 1)
    assert [i["text"] for i in ev["items"]] == ["架空病A"] and ev["as_of"] is None
    db.close()


@pytest.mark.parametrize("section", ["changes", "previsit"])
def test_deleted_record_is_labelled_with_observation_time_in_private_views(ctx, section):
    from notify_views import patient_context_list
    observed = T0 + 5 * DAY
    ctx.db.db.execute("INSERT INTO message_revisions VALUES(3,1,'h3','h2','deleted',?)", (observed,))
    ctx.db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=3")
    ctx.db.db.commit()
    result = patient_context_list(ctx.db.db, 1, section, cfg={}, since=T0,
                                  now=observed + DAY)
    rows = [i for i in result["items"] if "投稿#3" in i["text"]]
    assert len(rows) == 1
    assert rows[0]["group"] == "削除された記録"
    assert "削除を確認" in rows[0]["text"]
    assert "投稿日時未確認" not in rows[0]["text"]
    assert "10日分" not in rows[0]["text"]


@pytest.mark.parametrize("section", ["background", "timeline", "previsit", "transition_course"])
def test_private_views_keep_negation_plan_and_family_in_mentions(ctx, section):
    from notify_views import patient_context_list
    body = "退院後の連絡。家族の架空錠は中止を検討中。本人の架空のむくみはありません。"
    ctx.db.save_messages([_message(4, body=body)], project_id=1)
    ctx.db.db.execute("UPDATE messages SET posted_at_ts=?,first_seen=? WHERE message_id=4", (T0, T0))
    ctx.db.db.commit()
    _artifact(ctx.db, "extract_llm", 4, {"meds": [{"name": "架空錠", "status": "planned", "subject": "family"}],
                                         "symptoms": [{"text": "架空のむくみ", "negated": True}]},
              {"hash": _hash_of(ctx.db, 4), "extract_version": 1})
    rendered = []
    first = patient_context_list(ctx.db.db, 1, section, cfg={}, since=T0 - 1, now=T0 + DAY)
    for page in range(first["pages"]):
        rendered.extend(patient_context_list(ctx.db.db, 1, section, cfg={}, since=T0 - 1,
                                            now=T0 + DAY, page=page)["items"])
    mentions = [r for r in rendered if "架空錠" in r["text"] or "架空のむくみ" in r["text"]]
    assert mentions
    assert all(body in row["text"] for row in mentions)
