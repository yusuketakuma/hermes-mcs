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
    assert one["open_signals"] == {"count": 1, "state": "partial"}
    assert two["open_signals"] == {"count": 0, "state": "partial"}
    text = json.dumps(items, ensure_ascii=False)
    assert "is_unread" not in text and "karte_id" not in text and "Not a code" not in text


def test_patient_list_pages_by_activity_without_gaps(snapshot):
    seen, cursor = [], None
    view = mcs_view.View(snapshot)
    try:
        while True:
            page = view._page(*_patients_query(view), cursor, key_fields=("_activity", "_key"))
            seen += [row["project_id"] for row in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
    finally:
        view.close()
    assert seen == [2, 1, 4, 3]


def _patients_query(view):
    sql = ("SELECT * FROM (SELECT project_id,COALESCE((SELECT max(posted_at_ts) FROM messages m "
           "WHERE m.project_id=p.project_id),-9223372036854775808) AS _activity,project_id AS _key "
           "FROM patients p) WHERE 1=1")
    return sql, [], ["_activity", "_key"], ["synthetic-activity"], 1


def test_signal_counts_are_confirmed_only_after_a_complete_readable_run(snapshot, monkeypatch):
    import mcs_signals
    found = {"items": [{"project_id": 1}], "total": 1, "pipeline_last_run_at": 1.0, "errors": []}
    monkeypatch.setattr(mcs_signals, "current_open", lambda *a, **k: found)
    assert chat.response(snapshot, None, "counts", "project=1")["open_signals"] == {"count": 1, "state": "confirmed"}
    found["errors"] = ["signal_state_corrupt"]
    assert chat.response(snapshot, None, "counts", "project=1")["open_signals"]["state"] == "partial"
    monkeypatch.setattr(mcs_signals, "current_open", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    assert chat.response(snapshot, None, "counts", "project=1")["open_signals"] == {"count": None, "state": "failed"}
    assert chat.response(snapshot, None, "patients", "")["items"][0]["open_signals"] == {"count": None,
                                                                                         "state": "failed"}


def test_reply_counts_and_their_failed_state(snapshot, monkeypatch):
    data = chat.response(snapshot, {1}, "counts", "project=1")
    assert data["replies"]["state"] == "observed" and data["replies"]["requests"] == 0
    import patient_context_view
    monkeypatch.setattr(patient_context_view, "replies", lambda *a: {"items": [
        {"reply_state": None, "unverified": False, "reply_evaluation": None},
        {"reply_state": None, "unverified": False, "reply_evaluation": "unavailable"},
        {"reply_state": "ack", "unverified": False, "reply_evaluation": None}]})
    assert chat.response(snapshot, {1}, "counts", "project=1")["replies"] == {
        "requests": 3, "not_found": 1, "unknown": 1, "state": "observed"}
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
        row["reply_count"] = 5
        assert local_chat_view._last_reply(view, row)["complete"] is False
        row["reply_count"] = None
        assert local_chat_view._last_reply(view, row)["complete"] is False
    finally:
        view.close()


def test_mention_self_unknown_without_own_identity():
    metadata = {"mentions": [{"type": "user", "id": 8}], "mentions_status": "observed",
                "is_pinned": None, "pin_status": "invalid", "is_bookmarked": False, "bookmark_status": "observed"}
    assert local_chat_view._flags(metadata, None) == {
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
    ({"source": None, "kind": "clinical", "held": False, "reasons": []}, None),
    ({"source": "llm", "kind": "clinical", "held": False, "reasons": ["合成の急変"]},
     {"level": "high", "label": "🚨 緊急度高", "reasons": ["合成の急変"], "qc_note": "",
      "review": None, "review_label": None}),
    ({"source": "rule", "kind": "clinical", "held": True, "reasons": ["合成の搬送"]},
     {"level": "flag", "label": "🚨", "reasons": ["合成の搬送"], "qc_note": "", "review": "subject_unconfirmed",
      "review_label": "緊急度: 要確認（対象人物・時点の根拠を確認）"}),
    ({"source": None, "kind": "clinical", "held": True, "reasons": []},
     {"level": None, "label": None, "reasons": [], "qc_note": "", "review": "subject_unconfirmed",
      "review_label": "緊急度: 要確認（対象人物・時点の根拠を確認）"}),
    ({"source": None, "kind": "request", "held": False, "reasons": ["至急ご連絡ください"]},
     {"level": None, "label": None, "reasons": [], "qc_note": "", "review": "urgent_request",
      "review_label": "急ぎの確認依頼（本人の緊急状態とは別）"}),
])
def test_urgency_fields_follow_the_summary_reading(monkeypatch, details, expected):
    import clinical_urgency
    monkeypatch.setattr(clinical_urgency, "message_urgency_details", lambda db, mid: details)
    monkeypatch.setattr(local_chat_view, "urgency_qc_suffix", lambda db, mid: "")
    assert local_chat_view._urgency(None, 1) == expected


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
    monkeypatch.setattr(mcs_util, "load_config", lambda: {"clinical_metadata": True})
    before = hashlib.sha256(Path(snapshot).read_bytes()).digest()
    data = chat.response(snapshot, {1, 3}, "registered", "project=1")
    assert data["state"] == "listed" and data["collection_enabled"] is True
    by = {d["dataset"]: d for d in data["datasets"]}
    meds = by["medication_periods"]
    assert (meds["state"], meds["state_label"], meds["label"]) == ("complete", "取得済み", "薬剤登録")
    assert meds["rows"] == [{"begin_date": "2026-11-01", "end_date": "2026-11-14", "medicines": ["合成薬A錠"]}]
    assert by["observation_items"]["state"] == "failed" and by["observation_items"]["state_label"] == "取得失敗"
    assert "9001" not in json.dumps(data)
    group = chat.response(snapshot, {1, 3}, "registered", "project=3")
    assert group["state"] == "not_linked" and group["datasets"] == []
    monkeypatch.setattr(mcs_util, "load_config", lambda: {})
    assert chat.response(snapshot, {1}, "registered", "project=1")["collection_enabled"] is False
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
    assert empty["items"] == [] and empty["empty"] == "検索した取得済み原資料に一致するものはありません。"
    import patient_search
    monkeypatch.setattr(patient_search, "SCAN_LIMIT", 1)
    first = chat.response(snapshot, {2}, "sources", "project=2&q=合成記録")
    assert len(first["items"]) == 1 and first["next_cursor"] and not first["range_complete"]
    second = chat.response(snapshot, {2}, "sources", "project=2&q=合成記録&cursor=" + first["next_cursor"])
    assert {i["message_id"] for i in first["items"] + second["items"]} == {30, 31}
    for query, code in (("project=2&q=合成記録&cursor=bm90IGpzb24", "search_cursor_invalid"),
                        ("project=2&q=合成 資料:不明", "bad_search_filter"), ("project=2&q=", "bad_query")):
        with pytest.raises(ValueError, match=code):
            chat.response(snapshot, {2}, "sources", query)
    with pytest.raises(ValueError, match="project_not_allowed"):
        chat.response(snapshot, {1}, "sources", "project=2&q=合成")
    # A room without read permission is not searched, and says so instead of reading as no match.
    forbidden = chat.response(snapshot, {1}, "sources", "project=1&q=合成記録")
    assert forbidden["state"] == "unavailable" and forbidden["items"] == [] and "検索していません" in forbidden["empty"]


def test_policy_rejected_attachment_reaches_the_workspace(snapshot, monkeypatch):
    import attachment_ocr
    import mcs_util
    cfg = {"attachment_ocr": {"enabled": True, "reject_uncertain": True}}
    monkeypatch.setattr(mcs_util, "load_config", lambda: cfg)
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
