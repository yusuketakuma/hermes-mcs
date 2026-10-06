"""mcs_view search folds width/case variants on a read-only snapshot."""
import ledger
import mcs_view


def test_snapshot_search_matches_width_variants(tmp_path):
    db_path = tmp_path / "ledger.db"
    store = ledger.Ledger(str(db_path))
    try:
        with store.db:
            for mid, body in ((1, "ﾛｷｿﾆﾝ60mg 継続"), (2, "ＢＳ １２０")):
                store.db.execute(
                    "INSERT INTO messages(message_id,project_id,sender_name,body_state,"
                    "body_text,content_hash) VALUES(?,1,'職員','full',?,?)",
                    (mid, body, f"{mid:064x}"))
        snapshot = ledger.publish_snapshot(str(db_path), str(tmp_path / "snapshots"))
    finally:
        store.close()
    view = mcs_view.View(snapshot)
    try:
        def ids(q):
            return [r["message_id"] for r in view.read("search", project=1, query=q)["items"]]
        assert ids("ロキソニン") == [1]
        assert ids("bs120") == [2]
        assert ids("ﾛｷｿﾆﾝ ＢＳ") == []
    finally:
        view.close()
