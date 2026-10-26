"""Patient context v3 views: one evidence list, thin projections, clinical distinctions kept."""
import mcs_contract
import json
import sys
from types import SimpleNamespace

import pytest

import mcs_view
import notify_views
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
    context_refresh = {"value": None}
    import patient_context_refresh
    monkeypatch.setitem(sys.modules, "patient_context_refresh", SimpleNamespace(
        context_view_input=lambda conn, pid, cfg=None: context_refresh["value"],
        inference_coverage=lambda *args: {'complete':False,'status':'scan_pending','sources':{'pending':6}},
        workday_window=patient_context_refresh.workday_window))
    yield SimpleNamespace(db=db, context_refresh=context_refresh)
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
    ctx.context_refresh["value"] = {"as_of": T0 + 9 * DAY, "summary": [{"text": "s", "fact_ids": ["f1"]}],
                            "coverage": {"analyzed": 6}, "facts": [
        {"id": "f1", "category": "living", "text": "家族が服薬支援", "quote": "家族が服薬支援",
         "subject": "family", "message_id": 1, "content_hash": "h", "posted_at_ts": T0 + DAY,
         "discovery": "backfill_found", "found_night_id": "2026-10-18"},
        {"id": "f2", "category": "diagnoses", "text": "架空病B", "quote": "病名: 架空病B", "message_id": 4,
         "subject": "unspecified", "content_hash": _hash_of(ctx.db,4),
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
    # Both supported writers seed the old terminal state before recording a change.
    ctx.db.db.executemany("INSERT INTO message_revisions VALUES(?,1,?,NULL,'full',?)",
                         [(1, 'h1', T0), (3, 'h2', T0)])
    ctx.db.db.execute("INSERT INTO message_revisions VALUES(1,2,'h2','h1','full',?)", (since + 20,))
    ctx.db.db.execute("INSERT INTO message_revisions VALUES(3,2,'h3','h2','deleted',?)", (since + 30,))
    ctx.db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=3")
    ctx.db.db.commit()
    ctx.context_refresh["value"] = {"as_of": T0, "facts": [
        {"category": "living", "text": "架空発見", "quote": "架空発見", "message_id": 4, "posted_at_ts": T0,
         "discovery": "backfill_found", "found_at": since + 40},
        {"category": "living", "text": "架空旧発見", "quote": "架空旧発見", "message_id": 4, "posted_at_ts": T0,
         "discovery": "backfill_found", "found_at": since - 40},
        # EV3: a post the reader has not seen before stays new/retrospective even if the scan tagged it
        {"category": "living", "text": "架空遅着", "quote": "架空遅着", "message_id": 2, "posted_at_ts": T0,
         "discovery": "backfill_found", "found_at": since + 50}]}
    got = pcv.changes(ctx.db.db, pcv.evidence(ctx.db.db, 1), since)["by_discovery"]
    assert {i["message_id"] for i in got["new_report"]} == {5, 6}
    assert {i["message_id"] for i in got["retrospective"]} == {2}
    assert {i["message_id"] for i in got["edited"]} == {1}
    assert got["deleted"] == [{"message_id": 3, "observed_at": since + 30}]
    assert [i["text"] for i in got["backfill_found"]] == ["架空発見"]
    assert "架空遅着" in [i["text"] for i in got["retrospective"]]
    assert "残薬" not in json.dumps(got["deleted"], ensure_ascii=False)


def test_unpublished_context_refresh_adds_nothing(ctx):
    ctx.context_refresh["value"] = None
    ev = pcv.evidence(ctx.db.db, 1)
    assert all(i["discovery"] is None for i in ev["items"]) and ev["coverage"]["nightly"] is None
    assert ev["nightly_facts"] == [] and ev["summary"] == []


def _requests(ctx, rows, planned=None):
    """Hash-bound extractions carrying each post's request (LLM, so a thread reply can answer it)
    and the plan (rule extraction) as written."""
    for mid, text in rows:
        _artifact(ctx.db, "extract_llm", mid, {"requests": [{"to": "pharmacy", "action": text}]},
                  {"hash": _hash_of(ctx.db, mid)})
    if planned:
        body = ctx.db.db.execute("SELECT body_text FROM messages WHERE message_id=?", (planned[0],)).fetchone()[0]
        _artifact(ctx.db, "extract_v1", planned[0], {"patient_context": extract_context(body),
                                                      "next_planned": planned[1]}, {"hash": _hash_of(ctx.db, planned[0])})


def _reply(ctx, mid, parent, kind):
    ctx.db.save_messages([mcs_contract.Message(
        message_id=mid, project_id=1, parent_id=parent, sender_id=2, sender_name="返信者", sender_type="user",
        profession="", organization="", posted_at="2026-09-19T00:00:00+09:00", body_html="<p>架空の返信</p>",
        body_state="full", is_unread=False, reply_count=0)], project_id=1)
    ctx.db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?", (T0 + parent * DAY + 3600, mid))
    _artifact(ctx.db, "extract_llm", mid, {"reply": {"kind": kind}}, {"hash": _hash_of(ctx.db, mid)})
    from test_shortcut_debt_request_replies import seed_loop_replies
    seed_loop_replies(ctx.db)


def test_replies_never_claim_untreated_or_formal_completion(ctx):
    _requests(ctx, [(2, "架空依頼"), (3, "架空依頼2")], planned=(5, "架空訪問"))
    _reply(ctx, 30, 3, "done")
    got = pcv.replies(ctx.db.db, 1)
    assert [r["reply"] for r in got["items"]] == ["返信あり（完了・記録上）", "返答が見つからない"]  # newest first
    assert "未対応" not in json.dumps(got, ensure_ascii=False)
    note = pcv.previsit(ctx.db.db, pcv.evidence(ctx.db.db, 1), T0 + 7 * DAY)
    assert [r["message_id"] for r in note["unanswered"]] == [2]
    assert note["next_planned"] == "架空訪問"
    assert [i["text"] for i in note["written_checks"]] == ["架空薬の残数"]


def test_previsit_lists_every_distinct_check_of_one_post_without_fixed_cap(ctx):
    _requests(ctx, [(5, "架空依頼A"), (2, "架空依頼B"), (3, "架空依頼C"), (4, "架空依頼D")])
    note = pcv.previsit(ctx.db.db, pcv.evidence(ctx.db.db, 1), T0 + 7 * DAY)
    assert [i["message_id"] for i in note["written_checks"]] == [5]  # written follow-up first
    assert [r["message_id"] for r in note["unanswered"]] == [5, 4, 3, 2]  # Post 5's different request also remains.
    assert note["unanswered"][0]["text"] == "架空依頼A"


def test_previsit_carries_coverage_like_handoff(ctx):
    """A missing reply is only 返答が見つからない within what was fetched; previsit says how much that is."""
    ev = pcv.evidence(ctx.db.db, 1)
    assert pcv.previsit(ctx.db.db, ev, T0 + 7 * DAY)["coverage"] == ev["coverage"] == pcv.handoff(ctx.db.db, ev)["coverage"]


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
    import patient_context_refresh
    monkeypatch.setitem(sys.modules, "patient_context_refresh", SimpleNamespace(
        context_view_input=lambda *a, **k: None,
        inference_coverage=lambda *a, **k: {"complete": False, "status": "scan_pending", "sources": {"pending": len(posts)}},
        workday_window=patient_context_refresh.workday_window))
    db = _db(tmp_path, ())
    db.save_messages([_message(n + 1, body=OBS.format(*p)) for n, p in enumerate(posts)], project_id=1)
    for mid in (1, 2):
        body = db.db.execute("SELECT body_text FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
        _artifact(db, "extract_v1", mid, {"patient_context": extract_context(body)}, {"hash": _hash_of(db, mid)})
    got = pcv.discrepancies(pcv.evidence(db.db, 1))["items"]
    assert [g["kind"] for g in got] == ([kind] if kind else [])
    db.close()


def test_enclosing_quote_does_not_absorb_distinct_assertions(ctx):
    line = "対象薬: 架空薬"
    ctx.context_refresh["value"] = {"as_of": T0, "facts": [
        {"category": "medication_management", "text": line + "\n残薬: 3日分", "quote": line + "\n残薬: 3日分",
         "message_id": 2, "discovery": "backfill_found", "found_night_id": "2026-10-18"}]}
    items = [i for i in pcv.evidence(ctx.db.db, 1)["items"] if i["message_id"] == 2]
    assert len(items) == 3
    assert sum(i["discovery"] == "backfill_found" for i in items) == 1
    assert {i["text"] for i in items if i["discovery"] is None} == {"架空薬", "3日分"}
    assert next(i for i in items if i["discovery"] == "backfill_found")["quote"] == line + "\n残薬: 3日分"


def test_replies_drop_requests_whose_post_is_gone(ctx):
    _requests(ctx, [(2, "架空依頼"), (3, "架空依頼2")])
    ctx.db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=3")
    ctx.db.db.commit()
    assert [r["message_id"] for r in pcv.replies(ctx.db.db, 1)["items"]] == [2]


def test_oldest_long_term_item_survives_long_history(ctx):
    ctx.db.save_messages([_message(100 + n, body=f"架空連絡{n}") for n in range(80)], project_id=1)
    ev = pcv.evidence(ctx.db.db, 1)
    assert ev["items"][-1]["text"] == "架空病A"
    assert ev["coverage"]["read_model"]["collection"]["messages"] == 86


def test_timeline_includes_medication_and_symptom_mentions_as_written(ctx):
    plan = "本人は明日、架空錠を1錠服用予定です。"
    absence = "本人の架空のむくみはありません。"
    ctx.db.save_messages([_message(4, body=plan + absence)], project_id=1)
    _artifact(ctx.db, "extract_llm", 4, {"meds": [{"name": "架空錠", "dose": "1錠", "status": "planned",
                                                  "subject": "patient", "negated": False, "evidence": plan}],
                                         "symptoms": [{"text": "架空のむくみ", "subject": "patient",
                                                       "negated": True, "evidence": absence}]},
              {"hash": _hash_of(ctx.db, 4), "extract_version": 1})
    rows = [i for i in pcv.timeline(pcv.evidence(ctx.db.db, 1))["items"] if i["category"] in ("medications", "symptoms")]
    assert sorted((r["text"], json.dumps(r["mention"], sort_keys=True)) for r in rows) == sorted([
        ("架空錠", json.dumps({"dose": "1錠", "status": "planned", "negated": False}, sort_keys=True)),
        ("架空のむくみ", json.dumps({"negated": True}))])
    assert {r["evidence"] for r in rows} == {plan, absence}


def test_timeline_is_oldest_first_and_transition_lists_missing_records(ctx):
    ev = pcv.evidence(ctx.db.db, 1)
    assert list(dict.fromkeys(i["message_id"] for i in pcv.timeline(ev)["items"])) == [1, 2, 3, 4, 5, 6]
    course = pcv.transition_course(ev)["items"]
    assert len(course) == 1 and course[0]["anchor"]["message_id"] == 6
    assert course[0]["anchor_kind"] == "reported"  # Nominal wording does not establish actual patient discharge.
    assert course[0]["nearby"] == [] and course[0]["no_record"] == []


def test_transition_chat_labels_distinguish_patient_execution_from_family_report(ctx):
    bodies = {6:'経過: 本人は本日退院しました。',4:'経過: 家族は本日入院しました。'}
    for mid,body in bodies.items():
        ctx.db.save_messages([_message(mid,body=body)],project_id=1)
        ctx.db.db.execute('UPDATE messages SET posted_at_ts=? WHERE message_id=?',(T0+mid*DAY,mid))
        _artifact(ctx.db,'extract_v1',mid,{'patient_context':extract_context(body)}, {'hash':_hash_of(ctx.db,mid)})
    result = notify_views.patient_context_list(ctx.db.db,1,'transition_course',cfg={},since=T0,now=T0+7*DAY)
    rows = list(result['items'])
    for page in range(1,result['pages']):
        rows.extend(notify_views.patient_context_list(ctx.db.db,1,'transition_course',cfg={},since=T0,now=T0+7*DAY,page=page)['items'])
    assert any(r['group'] == '本人の移行実施の記録' and '本人は本日退院しました。' in r['text'] for r in rows)
    assert any(r['group'] == '移行に関する記載' and '家族は本日入院しました。' in r['text'] for r in rows)
    assert not any(r['group'] == '本人の移行実施の記録' and '家族は本日入院しました。' in r['text'] for r in rows)


def test_role_order_names_existing_categories():
    from patient_context import CATEGORIES
    assert all(c in CATEGORIES for cats in pcv.ROLE_ORDER.values() for c in cats)


def test_role_order_only_reorders(ctx):
    view = pcv.background(pcv.evidence(ctx.db.db, 1))
    ordered = pcv.role_order(view, "pharmacy")
    assert ordered["sections"][0]["category"] == "medication_management"
    assert sorted(map(json.dumps, ordered["sections"])) == sorted(map(json.dumps, view["sections"]))


def test_cli_passes_current_settings_to_the_context_refresh_gate(ctx, monkeypatch):
    seen = []
    monkeypatch.setitem(sys.modules, "patient_context_refresh", SimpleNamespace(
        context_view_input=lambda conn, pid, cfg=None: seen.append(cfg),
        inference_coverage=lambda *a, **k: {"complete": False, "status": "scan_pending", "sources": {"pending": 6}}))
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


@pytest.mark.parametrize("search", [
    lambda db, query: mcs_view.View._patient_context(
        SimpleNamespace(db=db), 1, "search", None, query, None),
    lambda db, query: pcv.search(db, 1, query),
    lambda db, query: notify_views.patient_search_view(db, 1, query),
], ids=["cli", "context", "shared"])
@pytest.mark.parametrize("query", [None, "", " \t\u3000", "語" * 501, 42, [], {}])
def test_search_rejects_invalid_input_before_reading_posts(search, query):
    with pytest.raises(ValueError, match="^bad_query$"):
        search(None, query)


@pytest.mark.parametrize("query, count", [("架空病B", 1), ("語" * 500, 0)])
def test_patient_context_search_accepts_valid_query_and_length_boundary(ctx, query, count):
    got = mcs_view.View._patient_context(SimpleNamespace(db=ctx.db.db), 1,
                                       "search", None, query, None)
    assert got["view"] == "search" and len(got["items"]) == count
    assert got["notes"]


def test_real_context_refresh_helper_without_artifact_falls_back_to_chat_items(tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "patient_context_refresh", raising=False)
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
    ctx.db.db.execute("INSERT INTO message_revisions VALUES(3,1,'h2',NULL,'full',?)", (T0,))
    ctx.db.db.execute("INSERT INTO message_revisions VALUES(3,2,'h3','h2','deleted',?)", (observed,))
    ctx.db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=3")
    ctx.db.db.commit()
    result = patient_context_list(ctx.db.db, 1, section, cfg={}, since=T0,
                                  now=observed + DAY)
    rows = [i for i in result["items"] if "#3）" in i["text"]]
    assert len(rows) == 1
    assert rows[0]["group"] == "削除された記録"
    assert "削除を確認" in rows[0]["text"]
    assert "投稿日時未確認" not in rows[0]["text"]
    assert "10日分" not in rows[0]["text"]


@pytest.mark.parametrize("section", ["background", "timeline", "previsit", "transition_course"])
def test_private_views_keep_negation_plan_and_family_in_mentions(ctx, section):
    from notify_views import patient_context_list
    transition = "退院後の連絡。"
    plan = "家族の架空錠は中止を検討中。"
    absence = "本人の架空のむくみはありません。"
    body = transition + plan + absence
    ctx.db.save_messages([_message(4, body=body)], project_id=1)
    ctx.db.db.execute("UPDATE messages SET posted_at_ts=?,first_seen=? WHERE message_id=4", (T0, T0))
    ctx.db.db.commit()
    _artifact(ctx.db, "extract_llm", 4, {
        "patient_context": [{"category": "care_services", "text": transition,
                             "evidence": transition, "subject": "unspecified"}],
        "meds": [{"name": "架空錠", "status": "planned", "subject": "family",
                  "negated": False, "evidence": plan}],
        "symptoms": [{"text": "架空のむくみ", "subject": "patient", "negated": True, "evidence": absence}]},
        {"hash": _hash_of(ctx.db, 4), "extract_version": 1})
    ev = pcv.evidence(ctx.db.db, 1)
    source_mentions = [i for i in ev["items"] if i.get("mention") and i["message_id"] == 4]
    assert {i["evidence"] for i in source_mentions} == {plan, absence}
    assert next(i for i in source_mentions if i["text"] == "架空錠")["subject"] == "family"
    assert next(i for i in source_mentions if i["text"] == "架空のむくみ")["mention"]["negated"] is True
    rendered = []
    first = patient_context_list(ctx.db.db, 1, section, cfg={}, since=T0 - 1, now=T0 + DAY)
    for page in range(first["pages"]):
        rendered.extend(patient_context_list(ctx.db.db, 1, section, cfg={}, since=T0 - 1,
                                            now=T0 + DAY, page=page)["items"])
    mentions = [r for r in rendered if "架空錠" in r["text"] or "架空のむくみ" in r["text"]]
    if section == "transition_course":
        # A nominal historical transition cannot open a patient execution window.
        group = next(g for g in pcv.transition_course(ev)["items"] if g["anchor"]["message_id"] == 4)
        assert group["anchor_kind"] == "reported" and group["nearby"] == group["no_record"] == []
        assert any(transition in r["text"] and r["group"] == "移行に関する記載" for r in rendered)
        assert not mentions
    else:
        assert mentions
        assert any(plan in row["text"] for row in mentions)
        assert any(absence in row["text"] for row in mentions)


def test_replies_follow_the_current_posts_not_a_cached_rollup(ctx):
    # review F4: an old cached rollup row never reads as the current request, reply or plan
    _requests(ctx, [(2, "架空依頼"), (3, "架空依頼2")], planned=(5, "架空訪問"))
    _reply(ctx, 30, 3, "done")
    stale = {"recent_requests": [{"mid": 2, "ctx": "古い依頼", "reply_state": "done"}], "next_planned": "古い予定"}
    ctx.db.db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES"
                      "('patient_rollup',1,?,'{}',0)", (json.dumps(stale, ensure_ascii=False),))
    got = pcv.replies(ctx.db.db, 1)
    assert [(r["message_id"], r["text"], r["reply_state"]) for r in got["items"]] == [
        (3, "架空依頼2", "done"), (2, "架空依頼", None)] and got["next_planned"] == "架空訪問"
    # request post edited: its old extraction no longer proves a request
    ctx.db.save_messages([_message(2, body="依頼は取り消しました")], project_id=1)
    assert [r["message_id"] for r in pcv.replies(ctx.db.db, 1)["items"]] == [3]
    # reply post edited away: the done state is not kept from the old reply
    ctx.db.save_messages([_message(30, parent=3, body="別の話題です")], project_id=1)
    assert [r["reply_state"] for r in pcv.replies(ctx.db.db, 1)["items"]] == [None]
    # plan post deleted: the plan is gone too
    ctx.db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=5")
    assert pcv.replies(ctx.db.db, 1)["next_planned"] is None


def test_unreadable_reply_sources_are_an_error_not_zero_requests(ctx, monkeypatch):
    # review F4 follow-up: a read failure must not render as an empty, confirmed list
    import sqlite3
    import rollup

    def unreadable(*_a, **_k):
        raise sqlite3.OperationalError("synthetic unreadable input")
    monkeypatch.setattr(rollup, "build_rollup", unreadable)
    with pytest.raises(sqlite3.OperationalError):
        pcv.replies(ctx.db.db, 1)
