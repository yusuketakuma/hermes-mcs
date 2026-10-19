"""Posters recorded on a patient's room put that room's own staff first among task assignees."""
import mcs_signals
import notify_cards
import notify_action_context
from notify_testkit import _msg, _patient, led, pinned_clock  # noqa: F401 - shared fixtures

__all__ = ["led", "pinned_clock"]


def test_task_assignees_who_posted_in_this_room_come_first(led):
    _patient(led, 1)
    _patient(led, 2)
    _msg(led, 100, pid=2, sender="佐藤 一郎", org="みどり薬局")
    _msg(led, 101, pid=1, sender="鈴木 二郎", org="みどり薬局")
    _msg(led, 102, pid=1, sender="訪問 看護", org="あおば訪問看護")   # not our station: never an assignee
    with led.db:
        mcs_signals.record_self_profile(led.db, {
            "sender_id": 9, "name": "薬局 太郎", "professions": [], "organizations": ["みどり薬局"]})
    base = notify_action_context.assignee_choices(led.db)
    scoped = notify_action_context.assignee_choices(led.db, project_id=1)
    assert base[0] == "薬局 太郎（みどり薬局）" and sorted(base) == sorted(scoped) and len(base) == 3
    assert scoped[0] == "鈴木 二郎（みどり薬局）"
    assert [x for x in scoped if x != scoped[0]] == [x for x in base if x != scoped[0]]   # order kept
    assert notify_action_context.assignee_choices(led.db, project_id=3) == base   # nobody posted there


def test_a_namesake_elsewhere_never_jumps_ahead(led):
    # review F6: the roster id must match the poster's sender_id; a name alone proves nothing
    _patient(led, 1)
    with led.db:
        mcs_signals.record_station_staff(led.db, [
            {"staff_id": 201, "name": "合成A", "station": "合成薬局"},
            {"staff_id": 202, "name": "同名職員", "station": "合成薬局"},
            {"staff_id": 203, "name": "合成C", "station": "合成薬局"}])
    led.db.execute("INSERT INTO messages(message_id,project_id,sender_id,sender_name,profession,organization,"
                   "posted_at,posted_at_ts,body_text,content_hash,body_state) VALUES"
                   "(300,1,902,'同名職員','看護師','別の訪問看護','2026-09-24T08:00',1,'本文',?,'full'),"
                   "(301,1,203,'合成C','薬剤師','合成薬局','2026-09-24T08:01',2,'本文',?,'full')",
                   (f"{300:064x}", f"{301:064x}"))
    assert notify_action_context.assignee_choices(led.db, project_id=1) == [
        "合成C（合成薬局）", "合成A（合成薬局）", "同名職員（合成薬局）"]


def test_a_room_poster_beyond_the_cap_still_comes_first(led):
    # NA1: the room-first order applies before the 25-choice cap, never after it
    _patient(led, 1)
    roster = [{"staff_id": 300 + i, "name": f"合成職員{i:02d}", "station": "合成薬局"} for i in range(30)]
    with led.db:
        mcs_signals.record_station_staff(led.db, roster)
    led.db.execute("INSERT INTO messages(message_id,project_id,sender_id,sender_name,profession,organization,"
                   "posted_at,posted_at_ts,body_text,content_hash,body_state) VALUES"
                   "(400,1,329,'合成職員29','薬剤師','合成薬局','2026-09-24T08:00',1,'本文',?,'full')", (f"{400:064x}",))
    got = notify_action_context.assignee_choices(led.db, project_id=1)
    assert got[0] == "合成職員29（合成薬局）" and len(got) == notify_action_context.STAFF_CHOICES
    assert notify_action_context.assignee_choices(led.db)[-1] == f"合成職員{notify_action_context.STAFF_CHOICES - 1:02d}（合成薬局）"


def test_thread_names_never_show_an_unknown_date(led):
    # display audit BUG 12: signal cards (no root post) used to end in 「— ??-??」
    _patient(led, 1)
    card = {"kind": "signal", "project_id": 1, "root_message_id": None, "created_at": 1_790_000_000.0}
    assert notify_cards._thread_name(led.db, card).endswith("— 09-21")   # 2026-09-21 23:13 JST
    assert "?" not in notify_cards._thread_name(led.db, {**card, "root_message_id": 999})
    assert notify_cards._thread_name(led.db, {**card, "created_at": None}) == "💬 患者A"
