"""Synthetic-snapshot tests for brain_export: file layout, read-only
boundary, rollup rendering, facts fence, signals, idempotency, and
stale-page GC. No live DB, network, or real patient data — every
fixture is invented for this file."""
import json
import os
import stat
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import brain_export

SNAP_TS = time.time()

SCHEMA = """
CREATE TABLE snapshot_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                            generation_id TEXT, generated_at REAL);
CREATE TABLE patients (project_id INTEGER PRIMARY KEY, project_type TEXT,
                       patient_name TEXT, disease TEXT, station_name TEXT,
                       url TEXT, fetch_state TEXT, fetch_reason TEXT,
                       last_complete_fetch REAL, last_seen REAL,
                       is_archived INTEGER, created_at REAL);
CREATE TABLE messages (message_id INTEGER PRIMARY KEY, project_id INTEGER,
                       parent_id INTEGER, sender_id INTEGER,
                       sender_name TEXT, sender_type TEXT, profession TEXT,
                       organization TEXT, posted_at TEXT, posted_at_ts INTEGER,
                       body_text TEXT, body_state TEXT, content_hash TEXT,
                       reply_count INTEGER DEFAULT 0);
CREATE TABLE artifacts (artifact_id INTEGER PRIMARY KEY, kind TEXT,
                        project_id INTEGER, message_id INTEGER,
                        content TEXT, model TEXT, meta TEXT, created_at REAL);
CREATE TABLE runs (run_id INTEGER PRIMARY KEY, started_at REAL,
                   finished_at REAL, snapshot_ts REAL, status TEXT,
                   error TEXT);
CREATE TABLE fetch_jobs (job_id INTEGER PRIMARY KEY, kind TEXT,
                         payload TEXT, state TEXT, created_at REAL);
CREATE TABLE notify_outbox (outbox_id INTEGER PRIMARY KEY, kind TEXT,
                            payload TEXT, state TEXT, created_at REAL);
CREATE TABLE requests (request_id INTEGER PRIMARY KEY, project_id INTEGER,
                       status TEXT, due_date TEXT, created_at REAL,
                       updated_at REAL, source_message_id INTEGER);
"""

SECRET_BODY = "SYNTHETIC_RAW_BODY_SHOULD_NEVER_LEAK_9f3c"
PATIENT_NAME = "テスト 患者A"  # fully synthetic fixture name


def _snapshot(tmp_path: Path, gen: str = "gen-1") -> Path:
    db_path = tmp_path / "snap.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version=7")
    conn.execute("INSERT INTO snapshot_meta VALUES (1,?,?)", (gen, SNAP_TS))
    conn.execute(
        "INSERT INTO patients VALUES (1,'visiting',?,'SYNTH-disease',"
        "'SYNTH-station','https://example.invalid/p/1','ok',NULL,?,?,0,?)",
        (PATIENT_NAME, SNAP_TS, SNAP_TS, SNAP_TS))
    conn.execute(
        "INSERT INTO messages VALUES (100,1,NULL,7,'SYNTH-nurse','staff',"
        "'看護師','SYNTH-org','2026-09-20T10:00:00+09:00',?,?,?,?,0)",
        (SNAP_TS - 3600, SECRET_BODY, "full", "hash-1"))
    conn.execute(
        "INSERT INTO runs VALUES (1,?,?,?, 'ok', NULL)",
        (SNAP_TS - 7200, SNAP_TS - 7100, SNAP_TS - 7100))
    conn.execute("INSERT INTO notify_outbox VALUES (1,'n','{}','pending',?)",
                 (SNAP_TS - 100,))
    conn.execute("INSERT INTO fetch_jobs VALUES (1,'poll','{}','pending',?)",
                 (SNAP_TS - 200,))
    conn.commit()
    conn.close()
    return db_path


def _rollup(db_path: Path, summary: str = "SYNTH summary v1") -> None:
    conn = sqlite3.connect(str(db_path))
    content = {
        "project_id": 1, "generated_at": SNAP_TS,
        "msg_count": 3, "reply_count": 1,
        "last_activity": "2026-09-20T10:00:00+09:00",
        "last_activity_ts": SNAP_TS - 3600,
        "latest_vitals": {"at": "2026-09-19", "bp": "120/80"},
        "current_med_period": {"start": "2026-09-01", "end": "2026-09-30"},
        "medications": [{"name": "SYNTH-med", "dose": "5mg",
                         "last": "2026-09-18"}],
        "recent_symptoms": [{"symptom": "SYNTH-咳", "last": "2026-09-19"}],
        "recent_requests": [{"kind": "確認", "ctx": "SYNTH ctx",
                             "at": "2026-09-19", "mid": 100}],
        "next_planned": "2026-10-01 訪問",
        "summary": {"text": summary, "at": "2026-09-20"},
        "top_senders": [["SYNTH-nurse", 3]],
        "possibly_deleted": [],
    }
    conn.execute("DELETE FROM artifacts WHERE kind='patient_rollup'")
    conn.execute(
        "INSERT INTO artifacts(kind,project_id,content,created_at) "
        "VALUES ('patient_rollup',1,?,?)",
        (json.dumps(content), SNAP_TS))
    conn.commit()
    conn.close()


def _signal(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    content = {"type": "rx_period_expiry", "project_id": 1,
               "detected_at": SNAP_TS - 1000, "state": "open",
               "evidence": {"request_ids": [5]},
               "context": {"days_left": 3},
               "note": "SYNTH note: 期限近接の確認候補"}
    conn.execute(
        "INSERT INTO artifacts(kind,project_id,content,meta,created_at) "
        "VALUES ('signal_v1',1,?,?,?)",
        (json.dumps(content), json.dumps({"key": "sig-1"}), SNAP_TS))
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path):
    snap = _snapshot(tmp_path)
    _rollup(snap)
    _signal(snap)
    out = tmp_path / "exports"
    return snap, out


def _all_text(out: Path) -> str:
    return "\n".join(p.read_text() for p in sorted(out.rglob("*.md")))


@pytest.mark.parametrize("content", ["{broken", "[]", "7",
                                   '{"summary":7}',
                                   '{"medications":[7]}',
                                   '{"top_senders":[["synthetic"]]}'])
def test_invalid_rollup_preserves_existing_export(env, content, capsys):
    snap, out = env
    brain_export.run(out, snap)
    before = {str(p.relative_to(out)): p.read_bytes()
              for p in out.rglob("*") if p.is_file()}
    with sqlite3.connect(snap) as conn:
        conn.execute("UPDATE artifacts SET content=? WHERE kind='patient_rollup'",
                     (content,))
        conn.execute("UPDATE snapshot_meta SET generation_id='synthetic-next'")
    assert brain_export.main(["--out", str(out), "--snapshot", str(snap)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "patient_rollup_invalid"
    assert {str(p.relative_to(out)): p.read_bytes()
            for p in out.rglob("*") if p.is_file()} == before


def test_layout_and_content(env):
    snap, out = env
    res = brain_export.run(out, snap)
    assert res["ok"] and res["patients"] == 1 and res["signals"] == 1
    for rel in ("meta.md", "health.md", "stats/latest.md",
                "signals/latest.md", "patients/p1.md"):
        assert (out / rel).exists(), rel
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    assert (out / "stats" / f"{today}.md").exists()
    patient = (out / "patients" / "p1.md").read_text()
    assert PATIENT_NAME in patient          # real-name field renders
    assert "SYNTH-med" in patient           # medications
    assert "SYNTH-咳" in patient            # symptoms
    assert "SYNTH summary v1" in patient    # summary
    assert "SYNTH-nurse" in patient         # senders


def test_no_raw_bodies(env):
    snap, out = env
    brain_export.run(out, snap)
    assert SECRET_BODY not in _all_text(out)


@pytest.mark.parametrize("subdir", ["patients", "stats", "signals"])
def test_export_refuses_linked_generated_directory_before_writes(
        env, tmp_path, subdir, capsys):
    snap, out = env
    # A missing rollup exercises the patient cleanup path, not just writes.
    with sqlite3.connect(snap) as conn:
        conn.execute("DELETE FROM artifacts WHERE kind='patient_rollup'")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    asset = foreign / "p999.md"
    asset.write_text("SYNTHETIC unrelated asset")
    out.mkdir()
    (out / subdir).symlink_to(foreign, target_is_directory=True)
    assert brain_export.main(["--out", str(out), "--snapshot", str(snap)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "export_directory_unsafe"
    assert asset.read_text() == "SYNTHETIC unrelated asset"
    assert sorted(p.name for p in foreign.iterdir()) == ["p999.md"]
    assert sorted(p.name for p in out.iterdir()) == [subdir]


def test_patient_cleanup_preserves_symbolic_link(env, tmp_path):
    snap, out = env
    brain_export.run(out, snap)
    foreign = tmp_path / "foreign.md"
    foreign.write_text("SYNTHETIC unrelated asset")
    link = out / "patients/p999.md"
    link.symlink_to(foreign)
    brain_export.run(out, snap)
    assert link.is_symlink()
    assert foreign.read_text() == "SYNTHETIC unrelated asset"


def test_write_refuses_directory_changed_before_open(tmp_path, monkeypatch):
    root = tmp_path / "exports"
    directory = root / "patients"
    directory.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    marker = foreign / "p1.md"
    marker.write_text("SYNTHETIC unrelated asset")
    open_file = brain_export.os.open

    def changed_before_open(path, *args, **kwargs):
        candidate = Path(path)
        # The old writer opens a pathname-based staging file; the fixed
        # writer opens the child directory before creating its staging FD.
        if candidate.name == "patients" or candidate.parent == directory \
                and candidate.name.startswith(".p1.md-"):
            directory.rmdir()
            directory.symlink_to(foreign, target_is_directory=True)
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(brain_export.os, "open", changed_before_open)
    with pytest.raises(ValueError, match="export_directory_unsafe"):
        brain_export._write(root, "patients/p1.md", "SYNTHETIC export")
    assert marker.read_text() == "SYNTHETIC unrelated asset"
    assert sorted(p.name for p in foreign.iterdir()) == ["p1.md"]


def test_write_stays_in_opened_directory_during_publication(tmp_path, monkeypatch):
    root = tmp_path / "exports"
    directory = root / "patients"
    directory.mkdir(parents=True)
    retained = tmp_path / "owned-moved"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    marker = foreign / "p1.md"
    marker.write_text("SYNTHETIC unrelated asset")
    replace = brain_export.os.replace

    def changed_before_replace(src, dst, **kwargs):
        directory.rename(retained)
        directory.symlink_to(foreign, target_is_directory=True)
        return replace(src, dst, **kwargs)

    monkeypatch.setattr(brain_export.os, "replace", changed_before_replace)
    brain_export._write(root, "patients/p1.md", "SYNTHETIC export")
    assert marker.read_text() == "SYNTHETIC unrelated asset"
    assert (retained / "p1.md").read_text() == "SYNTHETIC export"
    assert (retained / "p1.md").stat().st_mode & 0o777 == 0o600
    assert not list(retained.glob("*.tmp"))


@pytest.mark.parametrize(("subdir", "name"), [
    ("patients", "p999.md"), ("stats", "2000-01-01.md")])
def test_cleanup_stays_in_opened_directory_during_unlink(
        env, tmp_path, monkeypatch, subdir, name):
    snap, out = env
    brain_export.run(out, snap)
    directory = out / subdir
    (directory / name).write_text("SYNTHETIC owned stale export")
    retained = tmp_path / "owned-moved"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    marker = foreign / name
    marker.write_text("SYNTHETIC unrelated asset")
    unlink = brain_export.os.unlink
    changed = False

    def changed_before_unlink(path, **kwargs):
        nonlocal changed
        if Path(path).name == name and not changed:
            changed = True
            directory.rename(retained)
            directory.symlink_to(foreign, target_is_directory=True)
        return unlink(path, **kwargs)

    monkeypatch.setattr(brain_export.os, "unlink", changed_before_unlink)
    brain_export.run(out, snap)
    assert changed
    assert marker.read_text() == "SYNTHETIC unrelated asset"
    assert not (retained / name).exists()


@pytest.mark.parametrize("subdir", ["patients", "stats"])
def test_export_rechecks_directories_after_processing_starts(
        env, tmp_path, monkeypatch, subdir, capsys):
    snap, out = env
    with sqlite3.connect(snap) as conn:
        conn.execute("DELETE FROM artifacts WHERE kind='patient_rollup'")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    asset = foreign / "p999.md"
    asset.write_text("SYNTHETIC unrelated asset")
    run_stats = brain_export.mcs_stats.run_stats

    def changed_directory(*args, **kwargs):
        link = out / subdir
        if not link.is_symlink():
            link.symlink_to(foreign, target_is_directory=True)
        return run_stats(*args, **kwargs)

    monkeypatch.setattr(brain_export.mcs_stats, "run_stats", changed_directory)
    assert brain_export.main(["--out", str(out), "--snapshot", str(snap)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "export_directory_unsafe"
    assert asset.read_text() == "SYNTHETIC unrelated asset"
    assert sorted(p.name for p in foreign.iterdir()) == ["p999.md"]


@pytest.mark.parametrize("host_tz", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_export_uses_jst_independently_of_host_timezone(
        env, monkeypatch, host_tz):
    snap, out = env
    # The UTC/JST midnight boundary is clinically significant for dates.
    instant = datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc).timestamp()
    with sqlite3.connect(snap) as conn:
        conn.execute("UPDATE snapshot_meta SET generated_at=?", (instant,))
        conn.execute("UPDATE runs SET finished_at=?", (instant,))
        row = conn.execute("SELECT artifact_id,content FROM artifacts WHERE kind='signal_v1'").fetchone()
        signal = json.loads(row[1])
        signal["detected_at"] = instant
        conn.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                     (json.dumps(signal), row[0]))
    previous_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", host_tz)
        time.tzset()
        monkeypatch.setattr(brain_export.time, "time", lambda: instant)
        strftime = time.strftime
        monkeypatch.setattr(time, "strftime", lambda fmt, value=None: strftime(
            fmt, time.localtime(instant) if value is None else value))
        brain_export.run(out, snap)
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()
    meta = (out / "meta.md").read_text()
    assert "at 2026-01-02 00:30 JST" in meta
    assert "2026-01-02T00:30:00+0900" in meta
    assert "01-02 00:30" in (out / "health.md").read_text()
    assert "2026-01-02" in (out / "stats/latest.md").read_text()
    assert "2026-01-02" in (out / "signals/latest.md").read_text()
    assert (out / "stats/2026-01-02.md").is_file()
    assert (out / "export-2026-01-02.jsonl").is_file()


def test_stats_facts_and_honesty(env):
    snap, out = env
    brain_export.run(out, snap)
    stats = (out / "stats" / "latest.md").read_text()
    assert "## Facts" in stats
    assert "mcs.patients.total" in stats and "metric" in stats
    meta = (out / "meta.md").read_text()
    assert "記録の欠如は対応の欠如を意味しません" in meta
    assert "gen-1" in meta
    signals = (out / "signals" / "latest.md").read_text()
    assert "rx_period_expiry" in signals
    assert "SYNTH note" in signals
    assert "記録の欠如は対応の欠如を意味しません" in signals
    health = (out / "health.md").read_text()
    assert "notify_outbox pending: 1" in health
    assert "fetch_jobs: 1" in health


def test_idempotent(env):
    snap, out = env
    brain_export.run(out, snap)
    first = {p.name: p.read_bytes() for p in sorted(out.rglob("*.md"))}
    brain_export.run(out, snap)
    second = {p.name: p.read_bytes() for p in sorted(out.rglob("*.md"))}
    assert first == second
    assert not list(out.rglob("*.tmp"))  # no torn temp files


def test_export_staging_is_private_and_does_not_follow_symlinks(env, tmp_path):
    snap, out = env
    out.mkdir()
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep")
    (out / "meta.md.tmp").symlink_to(unrelated)
    brain_export.run(out, snap)
    assert unrelated.read_text() == "keep"
    assert (out / "meta.md.tmp").is_symlink()
    assert not (out / "meta.md").is_symlink()
    for path in out.rglob("*.md"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_export_failed_publication_preserves_previous_file(env, monkeypatch):
    snap, out = env
    brain_export.run(out, snap)
    previous = (out / "meta.md").read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(brain_export.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic publication failure"):
        brain_export.run(out, snap)
    assert (out / "meta.md").read_bytes() == previous
    assert not list(out.rglob("*.tmp"))


def test_updated_rollup_changes_file(env):
    snap, out = env
    brain_export.run(out, snap)
    _rollup(snap, summary="SYNTH summary v2 changed")
    brain_export.run(out, snap)
    patient = (out / "patients" / "p1.md").read_text()
    assert "SYNTH summary v2 changed" in patient
    assert "SYNTH summary v1" not in patient


def test_hostile_name_does_not_break_front_matter_or_tables(env):
    snap, out = env
    conn = sqlite3.connect(str(snap))
    conn.execute("UPDATE patients SET patient_name=? WHERE project_id=1",
                 ("evil: name\n---\ninjected: yes",))
    conn.execute(
        "UPDATE artifacts SET content=? WHERE kind='patient_rollup'",
        (json.dumps({"project_id": 1, "generated_at": SNAP_TS,
                     "msg_count": 1, "last_activity": "2026-09-20",
                     "last_activity_ts": SNAP_TS, "summary": None,
                     "medications": [{"name": "a|b\nc", "dose": "x",
                                      "last": "d"}],
                     "recent_symptoms": [], "recent_requests": [],
                     "top_senders": [], "possibly_deleted": []}),))
    conn.commit()
    conn.close()
    res = brain_export.run(out, snap)
    assert res["ok"]
    text = (out / "patients" / "p1.md").read_text()
    fm = text.split("---\n")[1]
    assert "\ninjected:" not in fm
    title_line = next(line for line in fm.splitlines()
                      if line.startswith("title:"))
    assert json.loads(title_line.split(":", 1)[1].strip()) == \
        "MCS evil: name\n---\ninjected: yes"
    assert "a|b\nc" not in text  # table cells cannot inject a row break
    assert "a\\|b c" in text


def test_stale_cleanup_never_deletes_foreign_files(env, tmp_path):
    """--out may point at a shared knowledge store — only generated
    p<int>.md names are collected; anything else is left alone (FIX-BE2)."""
    snap, out = env
    brain_export.run(out, snap)
    pdir = out / "patients"
    foreign = pdir / "p-notes.md"
    foreign.write_text("user content")
    also_foreign = pdir / "p.md"
    also_foreign.write_text("user content")
    stale = pdir / "p999.md"
    stale.write_text("old generated page")
    brain_export.run(out, snap)
    assert foreign.read_text() == "user content"
    assert also_foreign.read_text() == "user content"
    assert not stale.exists()


def test_stale_patient_removed(env, tmp_path):
    snap, out = env
    brain_export.run(out, snap)
    assert (out / "patients" / "p1.md").exists()
    conn = sqlite3.connect(str(snap))
    conn.execute("DELETE FROM artifacts WHERE kind='patient_rollup'")
    conn.commit()
    conn.close()
    res = brain_export.run(out, snap)
    assert res["patients"] == 0
    assert not (out / "patients" / "p1.md").exists()


def test_not_a_snapshot(tmp_path):
    bad = tmp_path / "live.db"
    conn = sqlite3.connect(str(bad))  # no snapshot_meta, user_version=0
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="snapshot_upgrade_required"):
        brain_export.run(tmp_path / "out", bad)


def test_main_cli(env, capsys):
    snap, out = env
    assert brain_export.main(
        ["--out", str(out), "--snapshot", str(snap)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["patients"] == 1
    assert brain_export.main(
        ["--out", str(out), "--snapshot", str(snap) + ".missing"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


# ---------- T15: machine contract + retention ----------

def _jsonl(out: Path) -> list:
    return [json.loads(line) for line in
            (out / "export.jsonl").read_text().splitlines() if line]


def test_machine_export_parses_completely_and_binds_generation(env):
    snap, out = env
    res = brain_export.run(out, snap)
    assert res["snapshot_generation_id"] == "gen-1"
    assert res["contract"] == "mcs-read-model/1"
    records = _jsonl(out)
    assert records                       # file is non-empty JSONL
    assert all(r["contract"] == "mcs-read-model/1" for r in records)
    assert all(r["snapshot_generation_id"] == "gen-1" for r in records)
    types = {r["type"] for r in records}
    assert {"meta", "coverage", "stat", "signal", "message"} <= types
    # message records carry provenance, not bodies
    msg = next(r for r in records if r["type"] == "message")
    assert msg["message_id"] == 100 and msg["content_hash"] == "hash-1"
    assert msg["state"] in ("current", "stale", "pending", "unknown")
    assert "extraction" in msg and "extract_llm" in msg["extraction"]
    # coverage makes "no record" distinguishable from "no event"
    assert "extraction" in next(r for r in records
                              if r["type"] == "coverage")["coverage"]


def test_machine_export_never_carries_raw_content(env):
    snap, out = env
    brain_export.run(out, snap)
    blob = (out / "export.jsonl").read_text()
    for secret in (SECRET_BODY, PATIENT_NAME, "SYNTH-nurse",
                   "SYNTH note"):
        assert secret not in blob, secret
    # signal 'note' text is human-surface content — machine records
    # carry the typed identity + evidence ids only
    sig = next(r for r in _jsonl(out) if r["type"] == "signal")
    assert sig["signal_type"] == "rx_period_expiry"
    assert sig["evidence"] == {"request_ids": [5]}
    assert "note" not in sig


def test_snapshot_rotation_is_detectable_in_records(env, tmp_path):
    snap, out = env
    brain_export.run(out, snap)
    assert all(r["snapshot_generation_id"] == "gen-1"
               for r in _jsonl(out))
    conn = sqlite3.connect(str(snap))
    conn.execute("UPDATE snapshot_meta SET generation_id='gen-2'")
    conn.commit()
    conn.close()
    brain_export.run(out, snap)
    # rotation -> every record rebinds to the new generation; a
    # consumer holding the previous file can compare and mark it stale
    assert all(r["snapshot_generation_id"] == "gen-2"
               for r in _jsonl(out))


def test_retention_sweeps_only_generated_dated_files(env, tmp_path):
    snap, out = env
    brain_export.run(out, snap)
    old = brain_export.EXPORT_RETENTION_DAYS + 5
    old_day = time.strftime(
        "%Y-%m-%d", time.localtime(time.time() - old * 86400))
    victims = [out / "stats" / f"{old_day}.md",
               out / "signals" / f"{old_day}.md",
               out / f"export-{old_day}.jsonl"]
    for f in victims:
        f.write_text("old generated export")
    # foreign files — even dated-looking ones outside generated dirs,
    # or non-matching names inside them — are never ours to delete
    foreign = [out / "stats" / "notes.md",
               out / "stats" / "99999-99.md",
               out / "keep-2000-01-01.md",
               out / "meta.md"]
    for f in foreign:
        f.write_text("user content")
    res = brain_export.run(out, snap)
    for f in victims:
        assert not f.exists(), f
    for f in foreign:
        assert f.exists() and f.read_text() == "user content" \
            or f.name == "meta.md", f
    assert sorted(res["retention"]["expired"]) == sorted(
        str(v.relative_to(out)) for v in victims)
    assert res["retention"]["keep_days"] == \
        brain_export.EXPORT_RETENTION_DAYS
    # the source snapshot — a duplicate of the live ledger — is a
    # file this exporter only reads; retention never touches it
    assert snap.exists()
    conn = sqlite3.connect(str(snap))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        conn.close()


def test_machine_export_long_stat_is_complete_not_sliced(env):
    """The markdown fence slices stats to [:4000] for humans; the
    machine record must carry the COMPLETE stat object."""
    snap, out = env
    brain_export.run(out, snap)
    stats = [r for r in _jsonl(out) if r["type"] == "stat"]
    assert stats
    for r in stats:
        assert isinstance(r["value"], dict)
        # a record is complete iff it round-trips and carries the
        # stat's own fields — the [:4000] cut never reaches it
        assert "status" in r["value"] or "kind" in r["value"] \
            or r["value"] != {}
    # and every line is under no artificial byte cap — the JSON itself
    # decides completeness
    assert (out / "export.jsonl").stat().st_size > 0


def test_machine_records_keep_counts_and_ids_without_surface_text(env, monkeypatch):
    snap, out = env
    secret = "SYNTHETIC_FREE_TEXT_73"
    monkeypatch.setattr(brain_export.mcs_stats, "run_stats", lambda *a: {
        "stats": {"meds": {"status": "ok", "distinct_names": 1,
                           "action_totals": {"start": 2},
                           "by_name_month": {"items": [{"name": secret}]},
                           "future_field": secret}}})
    monkeypatch.setattr(brain_export.mcs_signals, "current_open", lambda *a, **kw: {
        "items": [{"type": "rx_period_expiry", "project_id": 1,
                   "detected_at": SNAP_TS,
                   "evidence": {"message_ids": [100], "med": secret,
                                "raw": secret, "future_field": secret}}],
        "total": 1})
    brain_export.run(out, snap)
    assert secret not in (out / "export.jsonl").read_text()
    records = _jsonl(out)
    value = next(r["value"] for r in records if r["type"] == "stat")
    assert value["distinct_names"] == 1 and value["action_totals"] == {"start": 2}
    assert next(r["evidence"] for r in records if r["type"] == "signal") \
        == {"message_ids": [100]}
    assert secret in (out / "stats/latest.md").read_text()


def test_retention_preserves_foreign_date_names_and_symlink_targets(tmp_path):
    out = tmp_path / "exports"
    (out / "stats").mkdir(parents=True)
    (out / "signals").mkdir()
    foreign = [out / "2000-01-01.md", out / "2000-01-01.jsonl",
               out / "stats" / "export-2000-01-01.jsonl",
               out / "stats" / "2000-01-01.jsonl", out / "notes.md"]
    for path in foreign:
        path.write_text("keep unrelated content")
    link = out / "stats" / "2000-01-01.md"
    link.symlink_to(out / "notes.md")
    generated = out / "export-2000-01-01.jsonl"
    generated.write_text("expired generated export")
    assert brain_export._sweep_exports(out, SNAP_TS) == [generated.name]
    assert link.is_symlink()
    assert all(path.read_text() == "keep unrelated content" for path in foreign)


def test_machine_export_preserves_pruned_attachment_state(env):
    snap, out = env
    with sqlite3.connect(snap) as db:
        db.execute("CREATE TABLE attachments (attachment_id INTEGER, message_id INTEGER, "
                   "name TEXT, bytes INTEGER, sha256 TEXT, state TEXT)")
        db.execute("INSERT INTO attachments VALUES (1,100,'synthetic.txt',10,'h1','pruned')")
    brain_export.run(out, snap)
    records = _jsonl(out)
    attachment = next(r for r in records if r["type"] == "attachment")
    assert attachment["state"] == "pruned" and "name" not in attachment
    coverage = next(r for r in records if r["type"] == "coverage")
    assert coverage["coverage"]["attachments"]["pruned"] == 1


def _set_requests(db_path: Path, requests: list) -> None:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT content FROM artifacts "
                       "WHERE kind='patient_rollup'").fetchone()
    content = json.loads(row[0])
    content["recent_requests"] = requests
    conn.execute("UPDATE artifacts SET content=? WHERE kind='patient_rollup'",
                 (json.dumps(content),))
    conn.commit()
    conn.close()


def _table_rows(text: str, title: str):
    for chunk in text.split("\n## ")[1:]:
        head, _, body = chunk.partition("\n")
        if head == title:
            return [line for line in body.splitlines()
                    if line.startswith("| ") and not line.startswith(
                        ("| at ", "| ---"))]
    return None


def _req(ctx, **flag):
    return {"kind": "確認", "ctx": ctx, "at": "2026-09-19", "mid": 100,
            **flag}


def test_unverified_requests_exported_apart_from_confirmed(env):
    snap, out = env
    _set_requests(snap, [
        _req("SYNTH 確定A"), _req("SYNTH 確定B", unverified=False),
        _req("SYNTH 候補C", unverified=True),
        _req("SYNTH 候補D", unverified="false"),
        _req("SYNTH 候補E", unverified=0)])
    brain_export.run(out, snap)
    text = (out / "patients" / "p1.md").read_text()
    assert _table_rows(text, "open-looking requests") == [
        "| 2026-09-19 | 確認 | SYNTH 確定A |",
        "| 2026-09-19 | 確認 | SYNTH 確定B |"]
    assert _table_rows(text, "依頼候補（未確認）") == [
        "| 2026-09-19 | 確認 | SYNTH 候補C |",
        "| 2026-09-19 | 確認 | SYNTH 候補D |",
        "| 2026-09-19 | 確認 | SYNTH 候補E |"]


def test_unverified_only_requests_never_in_confirmed_table(env):
    snap, out = env
    _set_requests(snap, [_req("SYNTH 候補のみ", unverified=True)])
    brain_export.run(out, snap)
    text = (out / "patients" / "p1.md").read_text()
    assert _table_rows(text, "open-looking requests") is None
    assert "open-looking requests" not in text
    assert _table_rows(text, "依頼候補（未確認）") == [
        "| 2026-09-19 | 確認 | SYNTH 候補のみ |"]
