"""v1.0.29 maintenance stability: no-follow live log
rotation, stranded atomic-write temporaries, in-place edit detection in
the snapshot signature, and retention of never-delivered renders and
unreferenced view manifests. Temp files and synthetic rows only."""
import time

import ledger
import maintenance


def test_rotate_log_never_follows_a_symlinked_live_log(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "LOG_MAX", 3)
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"keep this content")
    link = tmp_path / "run.log"
    link.symlink_to(target)
    maintenance.rotate_log([str(link)])
    assert target.read_bytes() == b"keep this content"
    assert not (tmp_path / "run.log.1").exists()


def test_rotate_log_still_copytruncates_a_plain_log(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "LOG_MAX", 3)
    log = tmp_path / "run.log"
    log.write_bytes(b"synthetic line\n")
    maintenance.rotate_log([str(log)])
    assert (tmp_path / "run.log.1").read_bytes() == b"synthetic line\n"
    assert log.read_bytes() == b""


def test_prune_leftovers_removes_stale_atomic_temporaries(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path))
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    data = tmp_path / "data"
    stale = [data / "cmd_results" / ".pub-abc", data / "slack_render" / ".pub-def",
             data / "discord_state" / ".atomic.x1.tmp",
             data / "lineworks_state" / ".alias-y2.tmp"]
    kept = [data / "slack_render" / "d1.json", data / "slack_render" / "d2.json.claimed",
            data / "discord_state" / "journal-w~0.jsonl", data / "cmd_results" / "r.json"]
    for f in stale + kept:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    assert maintenance.prune_leftovers() == 0               # fresh writes stay
    monkeypatch.setattr(maintenance, "LEFTOVER_KEEP_S", -60)
    assert maintenance.prune_leftovers() == len(stale)
    assert all(f.exists() for f in kept)
    assert not any(f.exists() for f in stale)


def test_prune_snapshot_refuses_symlinked_fence(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path))
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(tmp_path / "backups"))
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(snaps))
    monkeypatch.setattr(maintenance, "LEFTOVER_KEEP_S", -60)
    target = tmp_path / "outside"
    target.write_bytes(b"synthetic")
    (snaps / ".snapshot.lock").symlink_to(target)
    candidate = snaps / ".snapshot.abc.tmp"
    candidate.write_bytes(b"synthetic")
    assert maintenance.prune_leftovers() == 0
    assert candidate.exists()
    assert target.read_bytes() == b"synthetic"


def test_signature_moves_on_in_place_request_and_patient_edits(tmp_path):
    path = str(tmp_path / "ledger.db")
    db = ledger.Ledger(path)
    db.ensure_patient(1)
    db.db.execute(
        "INSERT INTO requests(request_id,project_id,source_message_id,source_hash,"
        "title,status,revision,created_at,updated_at)"
        " VALUES(1,1,1,'h','synthetic','open',1,1,1)")
    db.db.commit()
    before = maintenance._content_signature(path)
    db.db.execute("UPDATE requests SET status='done', updated_at=2")
    db.db.commit()
    after_request = maintenance._content_signature(path)
    assert after_request != before
    db.db.execute("UPDATE patients SET disease='synthetic-change' WHERE project_id=1")
    db.db.commit()
    assert maintenance._content_signature(path) != after_request
    before_seen = maintenance._content_signature(path)
    db.db.execute("UPDATE patients SET last_seen=12345 WHERE project_id=1")
    db.db.commit()
    assert maintenance._content_signature(path) == before_seen
    db.close()


OLD = time.time() - maintenance.RETENTION_S - 86400


def _card(con, card_id):
    con.execute(
        "INSERT INTO notification_cards(card_id,card_key,kind,anchor_key,"
        "created_at,updated_at) VALUES(?,?,'thread','a',?,?)",
        (card_id, f"k{card_id}", OLD, OLD))


def _manifest(con, mid, card_id, at=OLD):
    con.execute(
        "INSERT INTO notification_view_manifests(manifest_id,card_id,render_rev,"
        "source_generation,presentation_generation,shown,created_at)"
        " VALUES(?,?,1,1,1,'[]',?)", (mid, card_id, at))


def _render(con, did, card_id, rev, state, manifest=None, published=0, at=OLD):
    con.execute(
        "INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,"
        "manifest_id,route_epoch,payload_hash,correlation,state,spec_published,"
        "created_at,updated_at) VALUES(?,?,'update',?,?,1,'h',?,?,?,?,?)",
        (did, card_id, rev, manifest, "c" + did, state, published, at, at))


def test_retention_drops_only_unsent_superseded_renders_and_free_manifests(tmp_path):
    db = ledger.Ledger(str(tmp_path / "ledger.db"))
    con = db.db
    _card(con, 1)
    for mid in (1, 2, 3, 4):
        _manifest(con, mid, 1)
    _render(con, "gone", 1, 1, "not_sent", manifest=1)
    _render(con, "file", 1, 2, "cancelled", published=1)      # spec file not cleaned
    _render(con, "sent", 1, 3, "delivered", manifest=2)
    _render(con, "fresh", 1, 4, "cancelled", at=time.time())
    _render(con, "effect", 1, 5, "cancelled")                 # a part was delivered
    _render(con, "bound", 1, 6, "not_sent")                   # intent binding
    _render(con, "latest", 1, 7, "not_sent")                  # newest on its card
    con.execute("INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state)"
                " VALUES('gone','card','card',0,'not_sent')")
    con.execute("INSERT INTO notification_delivery_attempts(attempt_id,delivery_id,"
                "begin_command_id,state,created_at) VALUES('a1','gone','b1','not_sent',?)", (OLD,))
    con.execute("INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state)"
                " VALUES('effect','card','card',0,'delivered')")
    con.execute("INSERT INTO notify_outbox(event_id,kind,payload,state,created_at)"
                " VALUES(9,'new_messages','{}','accepted',?)", (OLD,))
    con.execute("INSERT INTO notification_intent_cards(event_id,card_id,coverage,"
                "delivery_id,state) VALUES(9,1,'c','bound','delivered')")
    con.execute("INSERT INTO notification_acknowledgements(card_id,manifest_id,actor,"
                "command_id,created_at) VALUES(1,3,'nurse-1','cmd-1',?)", (OLD,))
    con.commit()
    out = maintenance.prune_history(con)
    assert out["unsent_renders"] == 1
    renders = {r[0] for r in con.execute("SELECT delivery_id FROM notification_renders")}
    assert renders == {"file", "sent", "fresh", "effect", "bound", "latest"}
    assert con.execute("SELECT COUNT(*) FROM notification_render_parts "
                       "WHERE delivery_id='gone'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM notification_delivery_attempts").fetchone()[0] == 0
    # 1 lost its render; 2 is a delivered render's, 3 acknowledged, 4 the newest
    assert out["view_manifests"] == 1
    assert {r[0] for r in con.execute(
        "SELECT manifest_id FROM notification_view_manifests")} == {2, 3, 4}
    db.close()


def test_prune_leftovers_never_follows_a_symlinked_spool_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path))
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    outside = tmp_path / "outside"
    outside.mkdir()
    victims = [outside / ".pub-keep", outside / ".atomic.k.tmp"]
    for f in victims:
        f.write_bytes(b"x")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "archive_state").symlink_to(outside, target_is_directory=True)
    real = tmp_path / "data" / "slack_render"
    real.mkdir()
    (real / ".pub-link").symlink_to(outside / ".pub-keep")   # a symlinked entry too
    monkeypatch.setattr(maintenance, "LEFTOVER_KEEP_S", -60)  # everything is "old"
    assert maintenance.prune_leftovers() == 0
    assert all(f.exists() for f in victims)
    assert (real / ".pub-link").is_symlink()
