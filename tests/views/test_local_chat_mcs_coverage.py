"""Synthetic-only contracts for the MCS data the local patient workspace shows beyond the chat text."""
import hashlib
import json
from pathlib import Path
import time

import pytest

import ledger
import local_chat_view
import mcs_local_chat as chat
import mcs_view
import self_identity

NOW = 1_700_000_000
FRESH = time.time()   # who-pressed lists expire; this one is read right after it was "fetched"
SUMMARY = "合成の連携サマリー本文。" * 12   # longer than the 80 characters of the summary-card line


@pytest.fixture(autouse=True)
def _no_cached_signals():
    local_chat_view._CACHE.clear()
    yield
    local_chat_view._CACHE.clear()


def _attempt(dataset, *, complete, rows=(), reason=None, status=None):
    return json.dumps({"contract": "project-metadata/1", "dataset": dataset, "scope": "karte", "entity_id": 501,
                       "item_id": None, "complete": complete, "attempted_at": time.time(), "reason": reason,
                       "http_status": status, "rows": list(rows)}, ensure_ascii=False)


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    capture = {"version": 1, "cohort_id": "0" * 32, "boundary_ts": 1, "boundary_source": "server",
               "started_at": 1, "capture_phase": "capturing", "saved_count": 0, "updated_at": 1}
    try:
        with store.db:
            db = store.db
            db.executemany(
                "INSERT INTO patients(project_id,patient_name,project_type,station_name,disease,karte_id,fetch_state,"
                "fetch_reason,last_complete_fetch,karte_summary_failed_at,group_id,group_name,initial_import) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                    (1, "合成 花子", "medical", "合成ステーション", "合成疾患", 501, "incomplete", "forbidden",
                     NOW, NOW + 50, None, None, None),
                    (2, "合成 花子", "medical", None, None, 502, "complete", None, NOW, None, None, None, None),
                    (3, "合成グループ室", "group", None, None, None, "pending", None, None, None, 77, "合成グループ",
                     json.dumps(capture)),
                    (4, "合成 次郎", "medical", None, None, None, "pending", "Not a code!", None, None, None, None,
                     "{")])
            rows = [(10, 1, None, 7, "合成 看護師", "看護師", "合成事業所", 1000, "合成記録 十", "full", 2),
                    (11, 1, 10, 8, "合成 薬剤師", "薬剤師", "合成薬局", 1100, "合成の返信", "full", 0),
                    (12, 1, 10, 9, "合成 医師", "医師", "合成医院", 1200, "削除された合成返信", "deleted", 0),
                    (20, 1, None, 7, "合成 看護師", "看護師", "合成事業所", 2000, "合成記録 二十", "full", 0),
                    (30, 2, None, 7, "合成 看護師", "看護師", "合成事業所", 5000, "合成記録 三十", "full", 0),
                    (31, 2, None, 7, "合成 看護師", "看護師", "合成事業所", 4000, "合成記録 三十一", "full", 0)]
            db.executemany("INSERT INTO messages(message_id,project_id,parent_id,sender_id,sender_name,profession,"
                           "organization,posted_at_ts,body_text,body_state,reply_count,content_hash) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,printf('%064x',?))", [(*row, row[0]) for row in rows])
            self_identity.record_station_staff(db, [{"staff_id": 8, "is_self": True}, {"staff_id": 7}])
            observed = {"observed_at": NOW}
            db.execute("INSERT INTO message_metadata(message_id,source,content,checked_at) VALUES(10,'capture',?,?)",
                       (json.dumps({"reactions": {"value": [{"type": "viewed", "count": 2, "self_reacted": True}],
                                                  **observed},
                                    "mentions": {"value": [{"type": "user", "id": 8}], **observed},
                                    "is_pinned": {"value": True, **observed}}), NOW))
            db.execute("INSERT INTO message_reaction_actor_fetch(message_id,complete_at,checked_at) VALUES(10,?,?)",
                       (FRESH, FRESH))
            db.executemany("INSERT INTO message_reaction_actors(message_id,actor_id,reaction_type,profession,"
                           "observed_at,actor_name) VALUES(10,?,'viewed',?,?,?)",
                           [(8, "薬剤師", NOW, "合成 薬剤師"), (9, "医師", NOW, "合成 医師")])
            db.executemany("INSERT INTO message_revisions(message_id,seq,content_hash,prev_content_hash,body_state,"
                           "observed_at) VALUES(?,?,?,?,?,?)", [
                               (10, 1, "a" * 64, None, "full", NOW), (10, 2, "b" * 64, "a" * 64, "full", NOW + 10),
                               (12, 1, "c" * 64, None, "full", NOW), (12, 2, "c" * 64, "c" * 64, "deleted", NOW + 20)])
            db.execute("INSERT INTO attachments(message_id,name,state) VALUES(10,'合成資料.pdf','downloaded')")
            store.artifact_add_tx("extract_llm", "{}", project_id=1, message_id=20,
                                  meta={"error": 1, "attempts": 5, "hash": f"{20:064x}"})
            store.artifact_add_tx("signal_v1", json.dumps({
                "project_id": 1, "state": "open", "type": "request_overdue", "evidence": {"message_ids": [10]},
                "detected_at": NOW, "note": "合成の期限超過"}), project_id=1, meta={"key": "synthetic-1"})
            store.artifact_add_tx("karte_summary", json.dumps({
                "comment": SUMMARY, "updated_at": "2026-11-01T10:00:00+09:00",
                "updater": {"profession": "看護師"}, "empty": False}, ensure_ascii=False),
                project_id=2, meta={"karte_id": 502})
            store.artifact_add_tx("project_metadata_v1", _attempt("medication_periods", complete=True, rows=[
                {"begin_date": "2026-11-01", "end_date": "2026-11-14",
                 "medicine_informations": [{"id": 9001, "name": "合成薬A錠"}]}]), project_id=1)
            store.artifact_add_tx("project_metadata_v1", _attempt(
                "observation_items", complete=False, reason="http_error", status=500), project_id=1)
        yield ledger.publish_snapshot(str(path), str(tmp_path / "snapshots"))
    finally:
        store.close()


def _message(snapshot, mid, project=1):
    return chat.response(snapshot, None, "evidence", f"project={project}&message={mid}")["message"]


def test_patient_list_orders_by_activity_and_carries_identity_and_freshness(snapshot):
    items = chat.response(snapshot, None, "patients", "")["items"]
    assert [item["project_id"] for item in items] == [2, 1, 4, 3]   # newest post first, undated last by ID
    one, two, four, three = (next(i for i in items if i["project_id"] == pid) for pid in (1, 2, 4, 3))
    assert one["heading"] == "合成 花子（合成ステーション） #1" and two["heading"] == "合成 花子 #2"
    assert (one["station_name"], one["disease"], one["project_type"]) == ("合成ステーション", "合成疾患", "medical")
    assert one["mcs_url"].endswith("/projects/medical/1") and three["mcs_url"] is None
    assert three["is_group"] and three["group_name"] == "合成グループ" and three["heading"] == "合成グループ #3"
    assert (one["fetch_reason"], one["fetch_reason_label"]) == ("forbidden", "閲覧権限なし")
    assert (four["fetch_reason"], four["fetch_reason_label"]) == ("other", "理由を判別できません")
    assert two["fetch_reason"] is None and two["fetch_reason_label"] is None
    assert one["last_complete_fetch"] == NOW and three["last_complete_fetch"] is None
    assert (one["initial_import"], three["initial_import"], four["initial_import"]) == (None, "capturing", "unknown")
    # No finished detector run: the count is what was judged, never a confirmed total.
    assert one["open_signals"] == {"count": 1, "state": "partial", "reason": "pending"}
    assert two["open_signals"] == {"count": 0, "state": "partial", "reason": "pending"}
    text = json.dumps(items, ensure_ascii=False)
    assert "is_unread" not in text and "karte_id" not in text and "Not a code" not in text


def test_patient_list_pages_by_activity_without_gaps(tmp_path):
    # 150 rooms over three pages of 60: runs of equal latest times, rooms with no post and rooms whose only
    # post has no time, interleaved by ID so every page boundary falls inside a tie.
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    latest = {pid: (None if pid % 7 == 0 else 3000 - (pid // 10) * 10) for pid in range(1, 151)}
    try:
        with store.db:
            store.db.executemany("INSERT INTO patients(project_id,patient_name) VALUES(?,?)",
                                 [(pid, f"合成 患者{pid}") for pid in latest])
            rows = [(pid * 10, pid, ts) for pid, ts in latest.items() if ts is not None or pid % 14 == 0]
            rows += [(pid * 10 + 1, pid, ts - 500) for pid, ts in latest.items() if ts is not None]
            store.db.executemany("INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,body_state,"
                                 "content_hash) VALUES(?,?,?,'合成','full',printf('%064x',?))",
                                 [(*row, row[0]) for row in rows])
        snapshot = ledger.publish_snapshot(str(path), str(tmp_path / "snapshots"))
    finally:
        store.close()
    seen, pages, cursor = [], 0, None
    view = mcs_view.View(snapshot)
    try:
        while True:
            page = local_chat_view.patients(view, None, cursor=cursor)
            assert len(page["items"]) <= 60
            seen += [item["project_id"] for item in page["items"]]
            pages, cursor = pages + 1, page["next_cursor"]
            if not cursor:
                break
    finally:
        view.close()
    dated = sorted((pid for pid, ts in latest.items() if ts is not None), key=lambda pid: (-latest[pid], -pid))
    undated = sorted((pid for pid, ts in latest.items() if ts is None), reverse=True)
    assert pages == 3 and seen == dated + undated      # no gap, no repeat, undated last by ID


def test_signal_counts_are_confirmed_only_after_a_complete_readable_run(snapshot, monkeypatch):
    import mcs_signals
    found = {"items": [{"project_id": 1}], "total": 1, "pipeline_last_run_at": 1.0, "errors": []}
    calls = []

    def current_open(*a, **k):
        calls.append(1)
        if isinstance(found, Exception):
            raise found
        return found
    monkeypatch.setattr(mcs_signals, "current_open", current_open)

    def both(reason_state):
        local_chat_view._CACHE.clear()
        counts = chat.response(snapshot, None, "counts", "project=1")["open_signals"]
        listed = next(i for i in chat.response(snapshot, None, "patients", "")["items"] if i["project_id"] == 1)
        assert counts == listed["open_signals"] == reason_state
    both({"count": 1, "state": "confirmed", "reason": None})
    found["errors"] = ["signal_state_corrupt"]
    both({"count": 1, "state": "partial", "reason": "unreadable"})
    found.update(errors=[], pipeline_last_run_at=None)
    both({"count": 1, "state": "partial", "reason": "pending"})
    found = ValueError("x")
    both({"count": None, "state": "failed", "reason": None})


def test_signal_map_is_read_once_per_snapshot_generation(snapshot, monkeypatch):
    import mcs_signals
    calls = []
    monkeypatch.setattr(mcs_signals, "current_open", lambda *a, **k: calls.append(1) or {
        "items": [], "total": 0, "pipeline_last_run_at": 1.0, "errors": []})
    chat.response(snapshot, None, "patients", "")
    chat.response(snapshot, None, "counts", "project=1")
    chat.response(snapshot, None, "counts", "project=2")
    assert len(calls) == 1
    view = mcs_view.View(snapshot)
    try:
        view.meta["generation_id"] = "another-synthetic-generation"
        local_chat_view.counts(view, None, 1)                # a new generation reads again
    finally:
        view.close()
    assert len(calls) == 2


def test_counts_are_built_once_per_patient_and_generation(snapshot, monkeypatch):
    import patient_context_view
    builds = []
    real = patient_context_view.replies
    monkeypatch.setattr(patient_context_view, "replies", lambda db, pid: builds.append(pid) or real(db, pid))
    first = chat.response(snapshot, {1, 2}, "counts", "project=1")
    assert chat.response(snapshot, {1, 2}, "counts", "project=1") == first and builds == [1]
    chat.response(snapshot, {1, 2}, "counts", "project=2")
    assert builds == [1, 2]
    # A failed read is not kept: the next selection tries again.
    monkeypatch.setattr(patient_context_view, "replies", lambda *a: builds.append("x") or (_ for _ in ()).throw(
        KeyError("x")))
    local_chat_view._CACHE.clear()
    for _ in range(2):
        assert chat.response(snapshot, {1}, "counts", "project=1")["replies"]["state"] == "failed"
    assert builds[-2:] == ["x", "x"]


def test_patient_list_carries_reply_checks_within_a_build_budget(snapshot, monkeypatch):
    import patient_context_view
    builds = []
    real = patient_context_view.replies
    monkeypatch.setattr(patient_context_view, "replies", lambda db, pid: builds.append(pid) or real(db, pid))
    monkeypatch.setattr(local_chat_view, "LIST_REPLY_BUILDS", 2)
    items = chat.response(snapshot, None, "patients", "")["items"]
    by = {item["project_id"]: item["replies"] for item in items}
    assert [item["project_id"] for item in items] == [2, 1, 4, 3] and builds == [2, 1]
    assert by[1] == chat.response(snapshot, None, "counts", "project=1")["replies"]     # the same reading
    assert by[1]["state"] == "partial" and by[1]["requests"] == 0
    unbuilt = {"requests": None, "not_found": None, "unknown": None, "state": "not_computed"}
    assert by[4] == by[3] == unbuilt
    chat.response(snapshot, None, "counts", "project=3")       # a selected patient's heading fills its row
    again = {item["project_id"]: item["replies"] for item in chat.response(snapshot, None, "patients", "")["items"]}
    assert builds == [2, 1, 3, 4] and again[3]["state"] != "not_computed" and again[4]["state"] != "not_computed"


def test_reply_counts_and_their_failed_state(snapshot, monkeypatch):
    import notify_views
    import patient_context_view
    data = chat.response(snapshot, {1}, "counts", "project=1")
    # The fixture's analysis is unfinished (message 20 failed): the counts are a lower bound.
    assert data["replies"]["state"] == "partial" and data["replies"]["requests"] == 0
    seen = []
    local_chat_view._CACHE.clear()
    monkeypatch.setattr(notify_views, "analysis_incomplete", lambda ev: seen.append(ev["coverage"]) or False)
    monkeypatch.setattr(patient_context_view, "replies", lambda *a: {"items": [
        {"reply_state": None, "unverified": False, "reply_evaluation": None},
        {"reply_state": None, "unverified": False, "reply_evaluation": "unavailable"},
        {"reply_state": "ack", "unverified": False, "reply_evaluation": None}]})
    assert chat.response(snapshot, {1}, "counts", "project=1")["replies"] == {
        "requests": 3, "not_found": 1, "unknown": 1, "state": "observed"}
    assert seen and all("inference" in coverage for coverage in seen)   # the context card's own reading
    local_chat_view._CACHE.clear()
    monkeypatch.setattr(patient_context_view, "evidence", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    assert chat.response(snapshot, {1}, "counts", "project=1")["replies"]["state"] == "partial"
    monkeypatch.setattr(patient_context_view, "replies", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    assert chat.response(snapshot, {1}, "counts", "project=1")["replies"]["state"] == "failed"
    for query in ("project=2", "project=1&q=x", "project=1&message=10"):
        with pytest.raises(ValueError):
            chat.response(snapshot, {1}, "counts", query)


def test_post_flags_stamps_actors_and_reply_marker(snapshot):
    root = _message(snapshot, 10)
    assert root["flags"] == {"mention_self": {"value": True, "state": "observed"},
                             "pinned": {"value": True, "state": "observed"},
                             "bookmarked": {"value": None, "state": "not_fetched"}}
    stamps = root["stamps"]
    assert stamps["state"] == "observed" and stamps["self"] == "👀" and not stamps["refresh_failed"]
    assert stamps["counts"] == [{"emoji": "👀", "count": 2}]
    assert stamps["actors"]["state"] == "complete" and stamps["actors"]["observed_at"] == FRESH
    assert {(a["name"], a["self"]) for a in stamps["actors"]["items"]} == {("合成 薬剤師", True), ("合成 医師", False)}
    assert "合成 医師" in root["stamp_line"]        # names on someone else's post too, as on the chat thread
    assert root["sender_self"] is False and root["sender_own_station"] is True
    assert root["last_reply"] == {"sender_name": "合成 薬剤師", "profession": "薬剤師", "organization": "合成薬局",
                                  "posted_at": None, "posted_at_ts": 1100, "self": True, "complete": True}
    reply = _message(snapshot, 11)
    assert reply["sender_self"] is True and reply["last_reply"] is None
    other = _message(snapshot, 20)
    assert other["flags"]["mention_self"] == {"value": None, "state": "not_fetched"}
    assert other["stamps"]["state"] == "not_fetched" and other["stamps"]["counts"] is None
    assert other["stamps"]["self"] is None and other["stamps"]["actors"]["items"] == []
    assert other["last_reply"] is None


def test_last_reply_is_incomplete_while_replies_are_missing(snapshot):
    page = chat.response(snapshot, {2}, "timeline", "project=2")
    assert page["items"][0]["last_reply"] is None
    view = mcs_view.View(snapshot)
    try:
        row = view._message(1, 10)
        reads = local_chat_view._PageReads(view, 1, [row])
        row["reply_count"] = 5
        assert local_chat_view._last_reply(view, row, reads)["complete"] is False
        row["reply_count"] = None
        assert local_chat_view._last_reply(view, row, reads)["complete"] is False
    finally:
        view.close()


def test_mention_self_unknown_without_own_identity():
    import message_metadata
    assert local_chat_view.flags is message_metadata.flags      # the chat cards' own reading
    metadata = {"mentions": [{"type": "user", "id": 8}], "mentions_status": "observed",
                "is_pinned": None, "pin_status": "invalid", "is_bookmarked": False, "bookmark_status": "observed"}
    assert local_chat_view.flags(metadata, None) == {
        "mention_self": {"value": None, "state": "self_unknown"},
        "pinned": {"value": None, "state": "invalid"}, "bookmarked": {"value": False, "state": "observed"}}


def test_summary_state_and_observed_edits_and_deletions(snapshot):
    failed = _message(snapshot, 20)
    assert failed["summary_lines"] == [] and failed["summary_state"] == "failed"
    assert failed["summary_state_label"] == "要約 作成失敗"
    pending = _message(snapshot, 30, project=2)
    assert pending["summary_state"] == "pending" and pending["summary_state_label"] == "要約 処理待ち"
    edited = _message(snapshot, 10)
    assert edited["change_observed_at"] == NOW + 10 and edited["deleted_observed_at"] is None
    deleted = _message(snapshot, 12)
    assert deleted["deleted_observed_at"] == NOW + 20 and deleted["change_observed_at"] is None
    assert deleted["summary_state"] is deleted["urgency"] is deleted["stamps"] is deleted["flags"] is None
    assert pending["change_observed_at"] is None


def test_summary_empty_state_is_the_chat_card_reading():
    import notify_render
    assert notify_render.SUMMARY_EMPTY_LABELS == {"failed": "作成失敗", "attention": "要確認", "pending": "処理待ち"}


@pytest.mark.parametrize("details,expected", [
    (None, None),
    ({"source": None, "kind": "clinical", "held": False, "reasons": [], "qc_note": ""}, None),
    ({"source": "llm", "kind": "clinical", "held": False, "reasons": ["合成の急変"], "qc_note": "（監査では通常判定）"},
     {"level": "high", "label": "🚨 緊急度高", "reasons": ["合成の急変"], "qc_note": "（監査では通常判定）",
      "review": None, "review_label": None}),
    ({"source": "rule", "kind": "clinical", "held": True, "reasons": ["合成の搬送"], "qc_note": ""},
     {"level": "flag", "label": "🚨", "reasons": ["合成の搬送"], "qc_note": "", "review": "subject_unconfirmed",
      "review_label": "緊急度: 要確認（対象人物・時点の根拠を確認）"}),
    ({"source": None, "kind": "clinical", "held": True, "reasons": [], "qc_note": ""},
     {"level": None, "label": None, "reasons": [], "qc_note": "", "review": "subject_unconfirmed",
      "review_label": "緊急度: 要確認（対象人物・時点の根拠を確認）"}),
    ({"source": None, "kind": "request", "held": False, "reasons": ["至急ご連絡ください"], "qc_note": ""},
     {"level": None, "label": None, "reasons": [], "qc_note": "", "review": "urgent_request",
      "review_label": "急ぎの確認依頼（本人の緊急状態とは別）"}),
])
def test_urgency_fields_follow_the_summary_reading(details, expected):
    assert local_chat_view._urgency(details) == expected


def test_summary_lines_and_urgency_come_from_one_reading(snapshot, monkeypatch):
    import structured_view
    calls = []
    details = {"source": "rule", "kind": "clinical", "held": False, "reasons": ["合成の搬送"], "qc_note": ""}
    monkeypatch.setattr(local_chat_view, "structured_summary",
                        lambda db, mid, **k: calls.append((mid, k)) or (["合成要約"], details))
    message = _message(snapshot, 30, project=2)
    assert calls == [(30, {"drug_candidates": False, "plain": True, "request_first": True})]
    assert message["summary_lines"] == ["合成要約"] and message["urgency"] == local_chat_view._urgency(details)
    assert not hasattr(local_chat_view, "message_urgency_details")
    # the chat card's wording, one definition
    assert local_chat_view.URGENT_REQUEST_LINE is structured_view.URGENT_REQUEST_LINE
    assert local_chat_view.URGENCY_REVIEW_LINE is structured_view.URGENCY_REVIEW_LINE


def test_context_carries_status_karte_summary_and_allergies_first(snapshot, monkeypatch):
    import mcs_util
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    data = chat.response(snapshot, {1, 2}, "context", "project=1")
    assert data["summary"] == [] and data["as_of"] is None
    assert any(line.startswith("履歴取得: ") for line in data["status"])
    assert data["karte_summary"] == {"state": "failed", "comment": None, "updated_at": None,
                                     "updater_profession": None, "failed_at": NOW + 50,
                                     "line": "連携サマリー（MCS）: 取得失敗"}
    present = chat.response(snapshot, {1, 2}, "context", "project=2")["karte_summary"]
    assert present["state"] == "present" and present["comment"] == SUMMARY      # whole, not the 80-char line
    assert present["updater_profession"] == "看護師" and present["updated_at"].startswith("2026-11-01")
    import notify_views
    view = mcs_view.View(snapshot)
    try:
        assert notify_views.karte_summary_status(view.db, 4)["state"] == "not_fetched"
    finally:
        view.close()
    sections = [{"category": c, "items": []} for c in ("demographics", "diagnoses", "history", "course", "allergies")]
    monkeypatch.setattr(mcs_view.View, "read", lambda self, *a, **k: {"sections": sections, "coverage": {}})
    keys = [s["key"] for s in chat.response(snapshot, {1}, "context", "project=1")["sections"]]
    assert keys == ["allergies", "demographics", "diagnoses", "history", "course"]


def test_registered_data_states_rows_without_mcs_ids(snapshot, monkeypatch):
    import mcs_util
    monkeypatch.setattr(mcs_util, "config_read_state", lambda: ({"clinical_metadata": True}, "read"))
    before = hashlib.sha256(Path(snapshot).read_bytes()).digest()
    data = chat.response(snapshot, {1, 3}, "registered", "project=1")
    assert data["state"] == "listed" and data["collection_enabled"] is True and data["config_state"] == "read"
    by = {d["dataset"]: d for d in data["datasets"]}
    meds = by["medication_periods"]
    assert (meds["state"], meds["state_label"], meds["label"]) == ("complete", "取得済み", "薬剤登録")
    assert meds["rows"] == [{"begin_date": "2026-11-01", "end_date": "2026-11-14", "medicines": ["合成薬A錠"]}]
    assert by["observation_items"]["state"] == "failed" and by["observation_items"]["state_label"] == "取得失敗"
    assert "9001" not in json.dumps(data)
    group = chat.response(snapshot, {1, 3}, "registered", "project=3")
    assert group["state"] == "not_linked" and group["datasets"] == []
    monkeypatch.setattr(mcs_util, "config_read_state", lambda: ({}, "read"))
    assert chat.response(snapshot, {1}, "registered", "project=1")["collection_enabled"] is False
    # Without a config.json the collector runs on the defaults, collection off; an unreadable one says nothing.
    for state, enabled, reason in (("missing", False, "config_missing"), ("unreadable", None, None),
                                   ("read", False, None)):
        monkeypatch.setattr(mcs_util, "config_read_state", lambda state=state: ({}, state))
        data = chat.response(snapshot, {1}, "registered", "project=1")
        assert (data["collection_enabled"], data["collection_reason"], data["config_state"]) == (
            enabled, reason, state)
    assert hashlib.sha256(Path(snapshot).read_bytes()).digest() == before
    with pytest.raises(ValueError, match="project_not_allowed"):
        chat.response(snapshot, {1}, "registered", "project=2")


def test_source_search_says_what_was_not_searched_and_pages(snapshot, monkeypatch):
    import mcs_util
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    data = chat.response(snapshot, {1, 2}, "sources", "project=2&q=合成記録")
    assert data["state"] == "searched" and {item["message_id"] for item in data["items"]} == {30, 31}
    assert all(item["source"] == "chat" and item["page"] is None for item in data["items"])
    assert any("添付OCRは設定で無効" in note for note in data["notes"])
    assert "source_ref" not in json.dumps(data) and data["range_complete"] is True and data["next_cursor"] is None
    empty = chat.response(snapshot, {2}, "sources", "project=2&q=存在しない語")
    assert empty["items"] == [] and empty["empty"] == "調べた範囲の取得済み原資料には、一致する記載は見つかりませんでした。"
    import patient_search
    monkeypatch.setattr(patient_search, "SCAN_LIMIT", 1)
    first = chat.response(snapshot, {2}, "sources", "project=2&q=合成記録")
    assert len(first["items"]) == 1 and first["next_cursor"] and not first["range_complete"]
    second = chat.response(snapshot, {2}, "sources", "project=2&q=合成記録&cursor=" + first["next_cursor"])
    assert {i["message_id"] for i in first["items"] + second["items"]} == {30, 31}
    for query, code in (("project=2&q=合成記録&cursor=bm90IGpzb24", "search_cursor_invalid"),
                        ("project=2&q=合成 資料:不明", "bad_search_filter"),
                        ("project=2&q=合成 after:2026-11-02 before:2026-11-01", "bad_search_filter"),
                        ("project=2&q=", "search_query_empty"), ("project=2&q=after:2026-11-01", "search_query_empty")):
        with pytest.raises(ValueError, match=f"^{code}$"):
            chat.response(snapshot, {2}, "sources", query)
    with pytest.raises(ValueError, match="project_not_allowed"):
        chat.response(snapshot, {1}, "sources", "project=2&q=合成")
    # A room without read permission is not searched, and says so instead of reading as no match.
    forbidden = chat.response(snapshot, {1}, "sources", "project=1&q=合成記録")
    assert forbidden["state"] == "unavailable" and forbidden["items"] == [] and "検索していません" in forbidden["empty"]
    # A group room has no medical-record page: its heading carries no MCS link.
    assert any("MCS: " in line for line in data["head"])
    group = chat.response(snapshot, {3}, "sources", "project=3&q=合成")
    assert group["state"] == "searched" and group["head"] and "MCS" not in "".join(group["head"]) and "/projects/" not in json.dumps(group)


def test_policy_rejected_attachment_reaches_the_workspace(snapshot, monkeypatch):
    import attachment_ocr
    import mcs_util
    cfg = {"attachment_ocr": {"enabled": True, "reject_uncertain": True}}
    monkeypatch.setattr(mcs_util, "config_read_state", lambda: (cfg, "read"))
    seen = []

    def view(db, attachment_id, project_id=None, include_text=False, *, cfg=None):
        seen.append(cfg)
        return {"state": "rejected_by_policy", "page_count": 0, "artifact_id": 5, "attachment_id": attachment_id,
                "project_id": project_id, "reason": "output_schema_table_overlap", "policy_binding": "f" * 64,
                "proof": "e" * 64, "original_project_url": "https://mcs.invalid/projects/medical/1"}
    monkeypatch.setattr(attachment_ocr, "view", view)
    files = chat.response(snapshot, {1}, "attachments", "project=1&message=10")["items"]
    assert seen == [cfg]      # the exclusion policy is read from the configuration, not left unset
    assert files[0]["ocr"] == {"state": "rejected_by_policy", "page_count": 0, "artifact_id": 5,
                               "reason": "output_schema_table_overlap",
                               "original_project_url": "https://mcs.invalid/projects/medical/1"}


def test_policy_needs_the_configuration_to_reject():
    import attachment_ocr
    assert attachment_ocr.rejection_policy(None) is None


def test_configuration_state_tells_missing_and_unreadable_from_read(tmp_path):
    from mcs_util import config_read_state
    path = tmp_path / "config.json"
    assert config_read_state(str(path)) == ({}, "missing")
    path.write_text("{", encoding="utf-8")
    assert config_read_state(str(path)) == ({}, "unreadable")
    path.write_text('{"clinical_metadata": true, "clinical_metadata": false}', encoding="utf-8")
    assert config_read_state(str(path)) == ({}, "unreadable")
    path.write_text('{"clinical_metadata": true}', encoding="utf-8")
    assert config_read_state(str(path)) == ({"clinical_metadata": True}, "read")


def _page_statements(snapshot, route, query, monkeypatch=None):
    """The SQL one workspace read runs, in order."""
    statements = []
    view = mcs_view.View(snapshot)
    view.db.set_trace_callback(statements.append)
    try:
        project = int(dict(part.split("=") for part in query.split("&"))["project"])
        result = local_chat_view.read(view, None, route, project)
    finally:
        view.close()
    return result, statements


def test_summary_state_follows_the_chat_card_rule(snapshot, monkeypatch):
    monkeypatch.setattr(local_chat_view, "progress_state",
                        lambda db, mid, cfg: {"state": "attention", "completed": 1, "total": 2})
    message = _message(snapshot, 30, project=2)
    assert (message["summary_state"], message["summary_state_label"]) == ("attention", "要約 要確認")
    # an exhausted extraction is still a failure, whatever the progress reads
    assert _message(snapshot, 20)["summary_state"] == "failed"
    monkeypatch.setattr(local_chat_view, "progress_state", lambda db, mid, cfg: None)
    assert _message(snapshot, 30, project=2)["summary_state"] == "pending"



@pytest.mark.parametrize("content", ["{", '{"extract": 1, "extract": 2}'])
def test_unreadable_settings_leave_summary_state_and_ocr_unknown(snapshot, monkeypatch, tmp_path, content):
    # A config.json that exists but cannot be parsed is not the defaults (load_config would read it as {}).
    import attachment_ocr
    import mcs_util
    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")
    real = mcs_util.config_read_state
    monkeypatch.setattr(mcs_util, "config_read_state", lambda: real(str(path)))
    real_view = attachment_ocr.view
    monkeypatch.setattr(attachment_ocr, "view", lambda *a, **k: pytest.fail("policy cannot be decided"))
    for message in (_message(snapshot, 10),
                    next(i for i in chat.response(snapshot, {1}, "timeline", "project=1")["items"]
                         if i["message_id"] == 10)):
        # the rule cannot be read without the settings: unknown, never a quiet "pending" or "failed" OCR
        assert (message["summary_state"], message["summary_state_label"]) == ("unknown", "要約 状況不明")
        assert message["attachments_ocr"] is None
    # A missing file is the defaults, as for the collector.
    monkeypatch.setattr(attachment_ocr, "view", real_view)
    path.unlink()
    assert _message(snapshot, 10)["summary_state"] in ("pending", "attention")


def test_timeline_counts_attachment_ocr_states_per_post(snapshot, monkeypatch):
    import attachment_ocr
    import mcs_util
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    states = iter(["failed"])
    real = attachment_ocr.view
    monkeypatch.setattr(attachment_ocr, "view", lambda db, aid, project_id=None, include_text=False, *, cfg=None:
                        {**real(db, aid, project_id=project_id, cfg=cfg), "state": next(states)})
    items = {item["message_id"]: item for item in chat.response(snapshot, {1}, "timeline", "project=1")["items"]}
    assert items[10]["attachments_ocr"] == [{"state": "failed", "count": 1}]
    assert items[20]["attachments_ocr"] == []
    monkeypatch.setattr(attachment_ocr, "view", real)
    state = _message(snapshot, 10)["attachments_ocr"][0]["state"]
    assert state in ("complete", "empty", "unprocessed", "failed", "unsupported", "unavailable", "rejected_by_policy")
    assert _message(snapshot, 12)["attachments_ocr"] == []             # deleted


def test_timeline_reads_each_post_detail_once_per_page(snapshot, monkeypatch):
    import mcs_util
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    result, statements = _page_statements(snapshot, "timeline", "project=1")
    assert len(result["items"]) == 2
    once = ("FROM message_revisions r", "FROM message_reaction_actor_fetch WHERE message_id IN",
            "ROW_NUMBER() OVER (PARTITION BY parent_id", "SELECT message_id,profession,organization FROM messages",
            "SELECT parent_id,count(*) FROM messages", "SELECT message_id,state,count(*) AS count FROM attachments")
    for text in once:
        assert sum(text in statement for statement in statements) == 1, text
    assert not any("WHERE message_id=?" in s and "profession,organization" in s for s in statements)


def test_page_actor_readings_are_the_stamp_reader_s_own(snapshot):
    from stamp_read import reaction_actor_summary
    view = mcs_view.View(snapshot)
    try:
        rows = [view._message(1, 10), view._message(1, 20)]
        reads = local_chat_view._PageReads(view, 1, rows)
        now = view.meta["generated_at"]
        assert reads.actors == {mid: reaction_actor_summary(view.db, mid, now=now) for mid in (10, 20)}
        assert reads.actors[10]["state"] == "complete" and reads.actors[20]["state"] == "not_fetched"
    finally:
        view.close()


def test_only_a_query_without_search_words_is_an_empty_search(snapshot, monkeypatch):
    import mcs_util
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    view = mcs_view.View(snapshot)
    try:
        for query, code in (("", "search_query_empty"), ("　 ", "search_query_empty"),
                            ("source:chat", "search_query_empty"), ("語" * 501, "bad_query"), (None, "bad_query")):
            with pytest.raises(ValueError, match=f"^{code}$"):
                local_chat_view.source_search(view, {2}, 2, query)
    finally:
        view.close()


def test_source_search_returns_each_range_s_unsearched_counts(snapshot, monkeypatch):
    import mcs_util
    import notify_views
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    assert chat.response(snapshot, {2}, "sources", "project=2&q=合成")["skipped"] == {
        "ocr_pending": 0, "body_unavailable": 0, "unclassified": 0}
    real = notify_views.patient_search_view
    monkeypatch.setattr(notify_views, "patient_search_view", lambda *a, **k: {
        **real(*a, **k), "skipped": {"ocr_pending": 2, "body_unavailable": 1, "unclassified": 0, "other": 9}})
    data = chat.response(snapshot, {2}, "sources", "project=2&q=合成")
    assert data["skipped"] == {"ocr_pending": 2, "body_unavailable": 1, "unclassified": 0}


def _metadata(dataset, *, item_id=None, complete=True, rows=(), definition=None):
    content = {"contract": "project-metadata/1", "dataset": dataset, "scope": "karte", "entity_id": 501,
               "item_id": item_id, "complete": complete, "attempted_at": time.time(), "reason": None,
               "http_status": None, "rows": list(rows)}
    if definition is not None:
        content["definition"] = definition
    return json.dumps(content, ensure_ascii=False)


def test_registered_lists_every_defined_item_with_unfetched_values(tmp_path, monkeypatch):
    import mcs_util
    monkeypatch.setattr(mcs_util, "config_read_state", lambda: ({"clinical_metadata": True}, "read"))
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    a = {"id": 11, "name": "合成検査A", "unit": "mg/dL"}
    b = {"id": 12, "name": "合成検査B", "unit": "%"}
    try:
        with store.db:
            store.db.execute("INSERT INTO patients(project_id,patient_name,project_type,karte_id) "
                             "VALUES(1,'合成 花子','medical',501)")
            store.artifact_add_tx("project_metadata_v1", _metadata("observation_items", rows=[
                {"lab_test_item": a, "upper_reference_limit_scalar": 1.2}, {"lab_test_item": b}]), project_id=1)
            store.artifact_add_tx("project_metadata_v1", _metadata(
                "observation_values", item_id=11, rows=[{"observation_issued_at": "2026-11-01", "scalar": 0.9}],
                definition={"lab_test_item": a}), project_id=1)
        snapshot = ledger.publish_snapshot(str(path), str(tmp_path / "snapshots"))
    finally:
        store.close()
    values = [d for d in chat.response(snapshot, {1}, "registered", "project=1")["datasets"]
              if d["dataset"] == "observation_values"]
    assert [d["item"]["name"] for d in values] == ["合成検査A", "合成検査B"]
    fetched, unfetched = values
    assert fetched["state"] == "complete" and fetched["rows"] == [
        {"issued_at": "2026-11-01", "scalar": 0.9, "max": None, "min": None, "left": None, "right": None}]
    assert (unfetched["state"], unfetched["state_label"], unfetched["rows"]) == ("unknown", "未取得・未確認", [])
    assert unfetched["item"] == {"name": "合成検査B", "unit": "%", "reference": {}}
    assert "12" not in json.dumps([d.get("item") for d in values])


def test_sender_search_reaches_posts_past_the_body_matches(tmp_path):
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    try:
        with store.db:
            store.db.executemany("INSERT INTO patients(project_id,patient_name) VALUES(?,?)",
                                 [(1, "合成 花子"), (2, "合成 次郎")])
            rows = [(100 + i, 1, "合成 看護師", 10_000 - i, "ヤマダ さんへ連絡") for i in range(60)]
            rows += [(1, 1, "ﾔﾏﾀﾞ 薬剤師", 10, "合成の古い記録"), (2, 2, "ヤマダ 医師", 20, "別の患者")]
            store.db.executemany("INSERT INTO messages(message_id,project_id,sender_name,posted_at_ts,body_text,"
                                 "body_state,content_hash) VALUES(?,?,?,?,?,'full',printf('%064x',?))",
                                 [(*row, row[0]) for row in rows])
        snapshot = ledger.publish_snapshot(str(path), str(tmp_path / "snapshots"))
    finally:
        store.close()
    body = chat.response(snapshot, {1}, "search", "project=1&q=ヤマダ")
    assert len(body["items"]) == 50 and 1 not in {item["message_id"] for item in body["items"]}
    senders = chat.response(snapshot, {1}, "senders", "project=1&q=ヤマダ")     # ﾔﾏﾀﾞ folds like search
    assert [item["message_id"] for item in senders["items"]] == [1] and senders["next_cursor"] is None
    timeline = chat.response(snapshot, {1}, "timeline", "project=1")["items"]
    assert set(senders["items"][0]) == set(timeline[0])           # the timeline's message shape
    with pytest.raises(ValueError, match="cursor_scope_or_generation_changed"):
        chat.response(snapshot, {1}, "senders", "project=1&q=ヤマダ&cursor=" + body["next_cursor"])
    with pytest.raises(ValueError):
        chat.response(snapshot, {1}, "senders", "project=1&q=ヤマダ&cursor=not-a-cursor")
    with pytest.raises(ValueError, match="project_not_allowed"):
        chat.response(snapshot, {1}, "senders", "project=2&q=ヤマダ")
    with pytest.raises(ValueError, match="^bad_query$"):
        chat.response(snapshot, {1}, "senders", "project=1&q=%20%E3%80%80")


def test_reply_checks_follow_the_settings_the_tab_reads(snapshot, monkeypatch):
    # Within one snapshot a settings change re-judges completeness: list, heading and the 連絡・返答の確認 tab agree.
    import mcs_util
    import patient_context_view
    settings = {"extract": {"patient_context": {"enabled": False}}}
    monkeypatch.setattr(mcs_util, "config_read_state", lambda: (settings, "read"))
    monkeypatch.setattr(mcs_util, "load_config", lambda: settings)
    real = patient_context_view.evidence

    def evidence(db, pid, *, cfg=None):
        ev = real(db, pid, cfg=cfg)
        done = cfg["extract"]["patient_context"]["enabled"]        # a synthetic stand-in for the real judgment
        return {**ev, "coverage": {**ev["coverage"], "inference": {
            "complete": done, "status": "complete" if done else "pending", "sources": {}}}}
    monkeypatch.setattr(patient_context_view, "evidence", evidence)

    def readings():
        listed = next(i for i in chat.response(snapshot, None, "patients", "")["items"] if i["project_id"] == 1)
        heading = chat.response(snapshot, None, "counts", "project=1")["replies"]
        tab = chat.response(snapshot, None, "panel", "project=1&section=replies")["panel"]
        return listed["replies"]["state"], heading["state"], any(n.startswith("解析未完了") for n in tab["notes"])
    assert readings() == ("partial", "partial", True)
    settings = {"extract": {"patient_context": {"enabled": True}}}
    assert readings() == ("observed", "observed", False)
    # Unreadable settings cannot confirm anything.
    monkeypatch.setattr(mcs_util, "config_read_state", lambda: ({}, "unreadable"))
    assert chat.response(snapshot, None, "counts", "project=1")["replies"]["state"] == "partial"


def test_a_single_post_reads_only_its_own_revisions(snapshot):
    view = mcs_view.View(snapshot)
    statements = []
    view.db.set_trace_callback(statements.append)
    try:
        assert local_chat_view._PageReads(view, 1, []).revisions == {}
        assert not any("message_revisions r" in s for s in statements)
        row = view._message(1, 10)
        reads = local_chat_view._PageReads(view, 1, [row])
    finally:
        view.close()
    assert set(reads.revisions) == {10}
    scan = [s for s in statements if "FROM message_revisions r" in s]
    assert len(scan) == 1 and "r.message_id IN (" in scan[0]


def test_unreadable_settings_leave_attachment_ocr_unknown(snapshot, monkeypatch, tmp_path):
    import attachment_ocr
    import mcs_util
    path = tmp_path / "config.json"
    path.write_text("{", encoding="utf-8")
    real = mcs_util.config_read_state
    monkeypatch.setattr(mcs_util, "config_read_state", lambda: real(str(path)))
    monkeypatch.setattr(attachment_ocr, "view", lambda *a, **k: pytest.fail("policy cannot be decided"))
    files = chat.response(snapshot, {1}, "attachments", "project=1&message=10")["items"]
    # not "failed": whether the policy excludes the file is unknown, and no text it might exclude is shown
    assert [item["ocr"] for item in files] == [{"state": "unknown", "reason": "config_unreadable"}]


def test_a_multi_post_page_reads_only_its_own_revisions(snapshot):
    view = mcs_view.View(snapshot)
    statements = []
    try:
        rows = [view._message(1, 10), view._message(1, 11)]
        view.db.set_trace_callback(statements.append)
        reads = local_chat_view._PageReads(view, 1, rows)
    finally:
        view.close()
    assert reads.revisions == {10: ("full", NOW + 10)}      # 12's deletion is not on this page
    scan = [s for s in statements if "FROM message_revisions r" in s]
    assert len(scan) == 1 and "r.message_id IN (" in scan[0]


def test_attachment_fetch_failure_keeps_its_http_status(tmp_path):
    errors = ["http_404", "http_4041", "network_error", "/private/合成/path", "http_abc"]
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    try:
        with store.db:
            store.db.execute("INSERT INTO patients(project_id,patient_name) VALUES(1,'合成 花子')")
            store.db.executemany("INSERT INTO messages(message_id,project_id,posted_at_ts,body_text,body_state,"
                                 "content_hash) VALUES(?,1,?,'合成','full',printf('%064x',?))",
                                 [(10 + i, 100 + i, 10 + i) for i in range(len(errors))])
            store.db.executemany("INSERT INTO attachments(message_id,name,state,error) VALUES(?,'合成.pdf','failed',?)",
                                 [(10 + i, error) for i, error in enumerate(errors)])
        snapshot = ledger.publish_snapshot(str(path), str(tmp_path / "snapshots"))
    finally:
        store.close()
    shown = [chat.response(snapshot, {1}, "attachments", f"project=1&message={10 + i}")["items"][0]["error"]
             for i in range(len(errors))]
    # the collector's http_NNN keeps its number; anything else stays normalised as before
    assert shown == ["http_404", "other_error", "network_error", "other_error", "other_error"]


def test_rollup_reads_registered_data_without_the_unfetched_items(tmp_path, monkeypatch):
    # The rollup freshness signature hashes registered_data's default output: listing unfetched items for a
    # reader must not change it, or every such patient's rollup would read as stale.
    import registered_data as shared
    import rollup
    path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(path))
    a, b = {"id": 11, "name": "合成検査A"}, {"id": 12, "name": "合成検査B"}
    try:
        with store.db:
            store.db.execute("INSERT INTO patients(project_id,patient_name,project_type,karte_id) "
                             "VALUES(1,'合成 花子','medical',501)")
            store.artifact_add_tx("project_metadata_v1", _metadata("observation_items", rows=[
                {"lab_test_item": a}, {"lab_test_item": b}]), project_id=1)
            store.artifact_add_tx("project_metadata_v1", _metadata(
                "observation_values", item_id=11, rows=[], definition={"lab_test_item": a}), project_id=1)
        base = shared.registered_data(store.db, "aggregate", 1, None)
        listed = shared.registered_data(store.db, "aggregate", 1, None, unfetched_items=True)
        assert [(r["dataset"], r["state"]) for r in base["records"]] == [
            ("medication_periods", "unknown"), ("observation_items", "complete"), ("observation_values", "empty")]
        assert base["total"] == 3 and listed["total"] == 4 and listed["records"][:3] == base["records"]
        assert shared.registered_data(store.db, "detail", 1, 2)["total"] == 3     # the limit counts stored only
        calls = []
        real = shared.registered_data
        monkeypatch.setattr(shared, "registered_data", lambda *args, **kwargs: calls.append(kwargs) or real(
            *args, **kwargs))
        first = rollup._message_source_signature_snapshot(store.db, 1, NOW)
        assert calls == [{}]                         # the signature reads the stored targets only
        monkeypatch.setattr(shared, "registered_data", lambda *args, **kwargs: base)
        assert rollup._message_source_signature_snapshot(store.db, 1, NOW) == first
    finally:
        store.close()


def test_source_search_with_unreadable_settings_says_ocr_is_unknown(snapshot, monkeypatch, tmp_path):
    import mcs_util
    import notify_views
    path = tmp_path / "config.json"
    path.write_text("{", encoding="utf-8")
    real = mcs_util.config_read_state
    monkeypatch.setattr(mcs_util, "config_read_state", lambda: real(str(path)))
    data = chat.response(snapshot, {2}, "sources", "project=2&q=合成記録")
    assert data["state"] == "searched" and {item["message_id"] for item in data["items"]} == {30, 31}
    assert notify_views.OCR_SEARCH_SETTINGS_UNREADABLE in data["notes"]
    assert not any("設定で無効" in note for note in data["notes"])
