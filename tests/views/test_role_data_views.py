"""Role surfaces share one source: order tables, chat labels and CLI choices never drift apart."""
import pytest

from patient_context import CATEGORIES
from patient_context_view import ROLE_LABELS, ROLE_ORDER, role_order


def test_every_role_order_uses_only_real_categories_and_has_a_label():
    for role, order in ROLE_ORDER.items():
        assert role in ROLE_LABELS and set(order) <= CATEGORIES and len(set(order)) == len(order)
    assert set(ROLE_LABELS) == set(ROLE_ORDER) | {""}
    for role in ("helper", "facility", "rehab", "nutrition", "dental", "msw"):
        assert role in ROLE_ORDER


def test_chat_labels_and_cli_choices_are_the_same_table():
    from notify_views import CONTEXT_ROLES
    import mcs_view

    assert CONTEXT_ROLES is ROLE_LABELS
    ns = mcs_view._parser().parse_args(["patient_context", "--project", "1", "--view", "background", "--role", "msw"])
    assert ns.role == "msw"
    with pytest.raises(SystemExit):
        mcs_view._parser().parse_args(["patient_context", "--project", "1", "--view", "background", "--role", "x"])


def test_role_order_only_reorders_sections():
    view = {"sections": [{"category": c} for c in ("followup", "excretion", "services_provided", "adl")]}
    ordered = role_order(view, "helper")
    assert [s["category"] for s in ordered["sections"]] == ["services_provided", "excretion", "adl", "followup"]
    assert sorted(s["category"] for s in ordered["sections"]) == sorted(s["category"] for s in view["sections"])
    # without a role order the sections follow the fixed category order (newest post no longer leads)
    assert [s["category"] for s in role_order(view, "unknown-role")["sections"]] == [
        "services_provided", "adl", "excretion", "followup"]


def _poster(mid, name, profession, organization, pid=1, parent=None, sender=None):
    import mcs_adapter
    return mcs_adapter.Message(
        message_id=mid, project_id=pid, parent_id=parent, sender_id=sender, sender_name=name,
        sender_type="user", profession=profession, organization=organization,
        posted_at=f"2026-10-{mid:02d}T09:00:00+09:00", body_html="<p>合成</p>", body_state="full",
        is_unread=False, reply_count=0)


def test_participants_come_from_the_posts_own_sender_metadata(tmp_path):
    from test_read_model import _db
    from patient_context_view import participants
    db = _db(tmp_path, ())
    db.save_messages([_poster(1, "合成看護師", "看護師", "合成訪問看護ステーション", sender=11),
                      _poster(2, "合成ヘルパー", "", "合成ヘルパーステーション", parent=1, sender=12),
                      _poster(3, "合成看護師", "主任看護師", "合成訪問看護ステーション", sender=11),
                      _poster(5, "合成看護師", "薬剤師", "合成薬局", sender=15),
                      _poster(6, "退職した人", "医師", "合成クリニック", sender=16)], project_id=1)
    db.save_messages([_poster(4, "別患者の人", "医師", "合成クリニック", pid=2, sender=14)], project_id=2)
    db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=6")
    try:
        rows = participants(db.db, 1)["items"]
    finally:
        db.close()
    # newest last post first; same name + different id stay apart; profession is the latest post's
    assert [(r["name"], r["profession"], r["organization"], r["messages"]) for r in rows] == [
        ("合成看護師", "薬剤師", "合成薬局", 1),
        ("合成看護師", "主任看護師", "合成訪問看護ステーション", 2),
        ("合成ヘルパー", None, "合成ヘルパーステーション", 1)]
    assert rows[1]["last_seen"].startswith("2026-10-03") and rows[1]["first_seen"].startswith("2026-10-01")
    assert rows[1]["last_message_id"] == 3


@pytest.mark.parametrize("role", ["", "care", "nurse"])
def test_chat_background_lists_participants_after_the_sections(tmp_path, role):
    # the role only reorders the sections; the poster list stays whatever the role
    from test_read_model import _db
    import notify_views
    db = _db(tmp_path, ())
    db.save_messages([_poster(1, "合成看護師", "看護師", "合成訪問看護ステーション", sender=11)], project_id=1)
    try:
        view = notify_views.patient_context_list(db.db, 1, "background", cfg={}, since=0, now=1_800_000_000, role=role)
    finally:
        db.close()
    rows = [r for r in view["items"] if r["group"] == "関係者・事業者（投稿者から取得）"]
    assert rows and rows[0]["text"] == "看護師 合成看護師／合成訪問看護ステーション\n投稿1件 · 最終投稿 2026/10/01 09:00"   # viewed in 2027: the year stays


def test_role_candidates_follow_the_posters_professions(tmp_path):
    from test_read_model import _db
    from patient_context_view import participants
    db = _db(tmp_path, ())
    db.save_messages([_poster(1, "合成看護師", "看護師", "合成訪問看護ステーション", sender=11),
                      _poster(2, "合成薬剤師", "薬剤師", "合成薬局", sender=12),
                      _poster(3, "合成歯科医", "歯科医師", "合成歯科", sender=13),
                      _poster(4, "合成事務", "", "合成薬局", sender=14)], project_id=1)
    try:
        got = participants(db.db, 1)
    finally:
        db.close()
    assert got["roles"] == [r for r in ROLE_ORDER if r in {"nurse", "pharmacy", "dental"}]
    assert got["items"][0]["sender_id"] == 14


def test_standard_background_offers_role_candidates_without_switching(tmp_path):
    from test_read_model import _db
    import notify_views
    db = _db(tmp_path, ())
    db.save_messages([_poster(1, "合成看護師", "看護師", "合成訪問看護ステーション", sender=11)], project_id=1)
    try:
        plain = notify_views.patient_context_list(db.db, 1, "background", cfg={}, since=0, now=1_800_000_000, role="")
        nurse = notify_views.patient_context_list(db.db, 1, "background", cfg={}, since=0, now=1_800_000_000, role="nurse")
    finally:
        db.close()
    assert "表示順: 標準（候補: 看護）" in plain["head"]
    assert "表示順: 看護" in nurse["head"]


def test_unanswered_requests_name_who_asked_and_their_last_post(tmp_path):
    from test_read_model import _db
    from patient_context_view import replies
    db = _db(tmp_path, ())
    db.save_messages([_poster(1, "合成看護師", "看護師", "合成訪問看護ステーション", sender=11),
                      _poster(3, "合成看護師", "看護師", "合成訪問看護ステーション", sender=11)], project_id=1)
    from test_read_model import _artifact, _hash_of
    _artifact(db, "extract_v1", 1, {"requests": [{"kind": "nurse", "ctx": "合成依頼"}]}, {"hash": _hash_of(db, 1)})
    try:
        rows = replies(db.db, 1)["items"]
    finally:
        db.close()
    assert rows[0]["reply"] == "返答が見つからない"
    assert rows[0]["sender"] == "看護師 合成看護師／合成訪問看護ステーション（最終投稿 2026-10-03 09:00）"


def test_written_provider_without_a_poster_is_a_candidate_only():
    from patient_context_view import discrepancies
    item = {"category": "care_team", "subject": "patient", "text": "担当：合成ケアプランセンター", "quote": "担当：合成ケアプランセンター",
            "details": [], "message_id": 1, "content_hash": "h", "posted_at_ts": 1, "source": "chat"}
    ev = {"items": [item, {**item, "text": "担当：田中", "quote": "担当：田中", "message_id": 2}]}
    posters = [{"organization": "合成薬局, 合成訪問看護ステーション"}]
    got = discrepancies(ev, posters=posters)["items"]
    assert [g["kind"] for g in got] == ["provider_not_posting"] and got[0]["records"][0]["message_id"] == 1
    assert discrepancies(ev)["items"] == []   # no posters known: nothing to compare against
    assert discrepancies(ev, posters=[{"organization": "合成ケアプランセンター"}])["items"] == []


def test_poster_gaps_rank_quiet_organizations_as_candidates_only():
    import sqlite3
    import mcs_stats
    from views_testkit import SCHEMA, SNAP_TS, _msg as _post
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    db.execute("INSERT INTO snapshot_meta VALUES (1,'g',?)", (SNAP_TS,))
    day = 86400
    _post(db, 1, pid=1, sender=1, name="看護A", org="合成訪問看護", ts=int(SNAP_TS) - 20 * day)
    _post(db, 2, pid=1, sender=2, name="薬局B", org="合成薬局", ts=int(SNAP_TS) - 2 * day)
    _post(db, 3, pid=1, sender=3, name="消えた人", org="合成クリニック", ts=int(SNAP_TS) - 40 * day, state="deleted")
    _post(db, 4, pid=2, sender=1, name="看護A", org="合成訪問看護", ts=int(SNAP_TS) - 1 * day)
    st = mcs_stats.run_stats(db, SNAP_TS, {"stat": "poster_gaps", "limit": 20})["stats"]["poster_gaps"]
    rows = [(r["project_id"], r["organization"], r["days_since_last"], r["posts"]) for r in st["by_organization"]["items"]]
    assert rows == [(1, "合成訪問看護", 20, 1), (1, "合成薬局", 2, 1), (2, "合成訪問看護", 1, 1)]
    assert st["definition_version"] == "2026-10-17" and "candidate" in st["notes"][0]
    scoped = mcs_stats.run_stats(db, SNAP_TS, {"stat": "poster_gaps", "limit": 20, "project": 2})["stats"]["poster_gaps"]
    assert [r["project_id"] for r in scoped["by_organization"]["items"]] == [2]


def test_same_day_rule_decides_differing_records():
    # review F5: repeated equal values on one date are not a conflict; differing values on one date are
    from patient_context_view import discrepancies

    def obs(mid, value, day):
        details = [{"key": "observation_name", "value": "合成値"}, {"key": "unit", "value": "mmHg"},
                   {"key": "value", "value": value}, {"key": "measured_on", "value": day}]
        return {"category": "observations", "subject": "patient", "text": value, "quote": value, "details": details,
                "message_id": mid, "content_hash": "h", "posted_at_ts": mid, "source": "chat"}
    def kinds(*rows):
        return [g["kind"] for g in discrepancies({"items": list(rows)})["items"]]
    assert kinds(obs(1, "120", "10/1"), obs(2, "120", "10/1"), obs(3, "130", "10/2")) == ["change_over_time"]
    assert kinds(obs(1, "120", "10/1"), obs(2, "130", "10/1")) == ["differing_records"]
