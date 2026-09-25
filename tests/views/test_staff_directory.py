"""Staff directory — senders observed in MCS posts resolve to the
facility they posted from (``organization``), and task assignees
materialize the link only when it is unambiguous. Synthetic rows +
temp DBs only."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import _mcs_path  # noqa: F401

import mcs_queries
import mcs_requests

from test_mcs_features import _create, _source, _snapshot


def _staff_msg(db, mid, pid, sender, org, prof="薬剤師"):
    db.db.execute(
        "INSERT INTO messages(message_id,project_id,sender_name,"
        "profession,organization,posted_at,posted_at_ts,body_text,"
        "content_hash,body_state) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (mid, pid, sender, prof, org, "2026-09-24T08:00",
         1700000000 + mid, "本文", f"{mid:064x}", "full"))
    db.db.commit()


def test_staff_directory_links_name_to_facility(tmp_path):
    db = _source(tmp_path)
    _staff_msg(db, 10, 1, "薬局 花子", "テスト薬局")
    _staff_msg(db, 11, 1, "薬局 花子", "テスト薬局")
    _staff_msg(db, 12, 1, "看護 太郎", "", prof="看護師")
    rows = mcs_queries.staff_directory(db.db, 1)
    by_name = {r["sender_name"]: r for r in rows}
    assert by_name["薬局 花子"]["organization"] == "テスト薬局"
    assert by_name["薬局 花子"]["messages"] == 2
    assert by_name["看護 太郎"]["profession"] == "看護師"
    # project scoping — a sender seen only in another room is absent
    _staff_msg(db, 20, 2, "他局 誰", "別薬局")
    assert "他局 誰" not in by_name
    all_rows = mcs_queries.staff_directory(db.db)
    assert any(r["sender_name"] == "他局 誰" for r in all_rows)
    db.close()


def test_resolve_staff_annotates_only_unambiguous(tmp_path):
    db = _source(tmp_path)
    _staff_msg(db, 10, 1, "薬局 花子", "テスト薬局")
    _staff_msg(db, 11, 1, "兼任 次郎", "施設A")
    _staff_msg(db, 12, 1, "兼任 次郎", "施設B")
    _staff_msg(db, 13, 1, "無所属 三子", "")
    def resolve(n):
        return mcs_queries.resolve_staff(db.db, n, 1)
    assert resolve("薬局 花子") == "薬局 花子（テスト薬局）"
    assert resolve("兼任 次郎") == "兼任 次郎"        # ambiguous stays raw
    assert resolve("無所属 三子") == "無所属 三子"    # no org stays raw
    assert resolve("不明 誰") == "不明 誰"            # unknown stays raw
    # already annotated -> idempotent
    assert resolve("薬局 花子（テスト薬局）") == "薬局 花子（テスト薬局）"
    assert resolve("  ") is None
    db.close()


def test_request_create_annotates_assignee(tmp_path):
    db = _source(tmp_path)
    _staff_msg(db, 10, 1, "薬局 花子", "テスト薬局")
    mcs_requests.apply_command(db, _create(db, assignee="薬局 花子"))
    mcs_requests.apply_command(db, _create(db, assignee="誰それ"))
    rows = db.db.execute(
        "SELECT assignee FROM requests ORDER BY request_id").fetchall()
    assert rows[0]["assignee"] == "薬局 花子（テスト薬局）"
    assert rows[1]["assignee"] == "誰それ"           # unresolvable -> raw
    # an update patching the assignee re-resolves through the ledger
    upd = {"version": 1, "cmd": "request.update", "command_id":
           "11111111-2222-3333-4444-555555555555",
           "actor": "synthetic reviewer", "human_confirmed": True,
           "project_id": 1, "request_id": 1, "expected_revision": 1,
           "reason": "担当差し替え", "patch": {"assignee": "薬局 花子"}}
    mcs_requests.apply_command(db, upd)
    assert db.db.execute(
        "SELECT assignee FROM requests WHERE request_id=1"
    ).fetchone()["assignee"] == "薬局 花子（テスト薬局）"
    db.close()


def test_staff_read_kind(tmp_path):
    db = _source(tmp_path)
    _staff_msg(db, 10, 1, "薬局 花子", "テスト薬局")
    view = _snapshot(db, tmp_path)
    out = view.read("staff", project=1)
    names = {i["sender_name"] for i in out["items"]}
    assert "薬局 花子" in names
    item = next(i for i in out["items"] if i["sender_name"] == "薬局 花子")
    assert item["organization"] == "テスト薬局"
    db.close()
