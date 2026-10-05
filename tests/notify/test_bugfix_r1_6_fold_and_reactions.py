"""fit_parts の折りたたみ件数表示と、body_state NULL 投稿の反応表示（完全合成）。"""
import notify_render
from notify_testkit import led

__all__ = ["led"]

NOTE = "※ 記録が見つからない≠対応がなかった。"


def test_fit_parts_merges_marker_before_trailing_note():
    rows = [f"・合成行{i:02d} " + "x" * 10 for i in range(10)]
    parts = {"containers": [{"type": "text", "fold": True,
                             "text": "\n".join(rows + ["・…他5件", NOTE])}]}
    lines = notify_render.fit_parts(parts, 150)["containers"][0]["text"].split("\n")
    marks = [ln for ln in lines if notify_render._FOLD_RE.match(ln)]
    kept = [ln for ln in lines if ln.startswith("・合成行")]
    assert len(marks) == 1
    assert marks[0] == f"・…他{5 + 10 - len(kept)}件"
    assert lines[-1] == NOTE
    assert lines.index(marks[0]) == len(kept)


def test_card_reactions_keeps_posts_with_null_body_state(led):
    db = led.db
    db.execute("INSERT INTO messages(message_id,project_id,parent_id,posted_at_ts,body_state) "
               "VALUES(1,'P',NULL,1,NULL)")
    db.execute("INSERT INTO messages(message_id,project_id,parent_id,posted_at_ts,body_state) "
               "VALUES(2,'P',1,2,'full')")
    db.execute("INSERT INTO messages(message_id,project_id,parent_id,posted_at_ts,body_state) "
               "VALUES(3,'P',1,3,'deleted')")
    card = {"kind": "thread", "project_id": "P", "root_message_id": 1}
    assert [m for m, _ in notify_render.card_reactions(db, card)] == [1, 2]
