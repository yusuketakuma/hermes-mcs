"""Synthetic-snapshot tests for brain_export: file layout, read-only
boundary, rollup rendering, facts fence, signals, idempotency, and
stale-page GC. No live DB, network, or real patient data — every
fixture is invented for this file."""
import json
import sqlite3
import time
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


def test_layout_and_content(env):
    snap, out = env
    res = brain_export.run(out, snap)
    assert res["ok"] and res["patients"] == 1 and res["signals"] == 1
    for rel in ("meta.md", "health.md", "stats/latest.md",
                "signals/latest.md", "patients/p1.md"):
        assert (out / rel).exists(), rel
    today = time.strftime("%Y-%m-%d")
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


def test_updated_rollup_changes_file(env):
    snap, out = env
    brain_export.run(out, snap)
    _rollup(snap, summary="SYNTH summary v2 changed")
    brain_export.run(out, snap)
    patient = (out / "patients" / "p1.md").read_text()
    assert "SYNTH summary v2 changed" in patient
    assert "SYNTH summary v1" not in patient


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
    with pytest.raises(ValueError):
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
