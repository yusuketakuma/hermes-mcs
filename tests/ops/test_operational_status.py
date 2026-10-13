"""Operational diagnostics only read bounded synthetic evidence."""
import json
import os
import sqlite3

import pytest

import extract_llm
import operational_status as observation

NOW = 20000.0


@pytest.fixture
def health(tmp_path):
    path = tmp_path / "data" / "health.json"
    path.parent.mkdir()
    return path


def record(health, **updates):
    data = {"at": NOW - 10, "unread_at": NOW - 10, "overall": "ok",
            "collection": "ok", "notify": {"pending": 2, "oldest_age_s": 100},
            "extract_v2_coverage": {"counted_at": NOW - 100,
                                    "extract_version": extract_llm.EXTRACT_VERSION,
                                    "eligible": 20, "current": 4}}
    data.update(updates)
    health.write_text(json.dumps(data))
    return data


def read(health):
    return observation.stored_status(health.parent.parent, {}, now=NOW)


def test_recorded_coverage_is_not_a_queue_or_current_observation(health):
    record(health, extract_v2_coverage={"counted_at": NOW - 4000,
                                      "extract_version": extract_llm.EXTRACT_VERSION,
                                      "eligible": 20, "current": 4})
    before = health.read_bytes(), health.stat().st_mtime_ns
    out = read(health)
    assert out["status"] == "ok"
    assert out["extraction_coverage"]["status"] == "stale"
    assert out["extraction_coverage"]["ratio"] == .2
    assert "left" not in out["extraction_coverage"]
    assert out["queues"]["semantic_jobs"]["pending"] is None
    assert out["queues"]["notify"]["pending"] == 2
    assert "集計が古い" in "\n".join(observation.status_lines(out))
    assert before == (health.read_bytes(), health.stat().st_mtime_ns)


def test_deep_run_does_not_refresh_unread_collection(health):
    record(health, unread_at=NOW - 10000)
    out = read(health)
    assert out["status"] == "stale"
    assert out["recorded_overall"] == "ok"
    assert out["evidence_at"] == NOW - 10000


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), "100"])
def test_invalid_unread_time_does_not_prove_current_health(health, value):
    record(health, unread_at=value)
    assert read(health)["status"] == "unknown"


@pytest.mark.parametrize("overall", ["failed", "degraded"])
@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), "100"])
def test_missing_unread_evidence_never_hides_recorded_failure(health, overall, value):
    record(health, overall=overall, unread_at=value)
    out = read(health)
    assert out["status"] == overall
    assert out["unread_observed_at"] is None
    lines = observation.status_lines(out)
    assert lines[0] == "運用記録: " + {"failed": "失敗", "degraded": "要確認"}[overall]
    assert "未読収集の観測時刻: 未確認" in "\n".join(lines)


@pytest.mark.parametrize("value", [None, True, -1, 2**80, "2", {}, []])
def test_invalid_counts_are_unknown_not_zero(health, value):
    record(health, notify={"pending": value})
    assert read(health)["queues"]["notify"]["pending"] is None


@pytest.mark.parametrize("patch", [{"current": 21}, {"eligible": True},
                                   {"counted_at": NOW + 1}, {"extract_version": "private"}])
def test_inconsistent_coverage_is_not_shown_as_measured(health, patch):
    data = record(health)
    data["extract_v2_coverage"].update(patch)
    health.write_text(json.dumps(data))
    assert read(health)["extraction_coverage"]["status"] == "unknown"
    assert read(health)["extraction_coverage"]["current"] is None


def test_zero_denominator_is_not_one_hundred_percent(health):
    record(health, extract_v2_coverage={"counted_at": NOW,
                                      "extract_version": extract_llm.EXTRACT_VERSION,
                                      "eligible": 0, "current": 0})
    out = read(health)["extraction_coverage"]
    assert out["status"] == "recorded"
    assert out["ratio"] is None


@pytest.mark.parametrize("offset", [-1, 1])
def test_other_generation_is_not_displayed_as_current_coverage(health, offset):
    data = record(health)
    data["extract_v2_coverage"]["extract_version"] += offset
    health.write_text(json.dumps(data))
    out = read(health)
    coverage = out["extraction_coverage"]
    assert coverage["status"] == "unknown"
    assert all(coverage[key] is None for key in (
        "generation", "counted_at", "eligible", "current", "ratio"))
    text = "\n".join(observation.status_lines(out))
    assert "現行抽出世代の充足: 未確認" in text
    assert "4/20投稿" not in text


def test_current_generation_coverage_keeps_recorded_counts_and_text(health):
    record(health)
    out = read(health)
    assert out["extraction_coverage"]["status"] == "recorded"
    assert out["extraction_coverage"]["generation"] == extract_llm.EXTRACT_VERSION
    assert "現行抽出世代の充足: 4/20投稿（100秒前）" in "\n".join(
        observation.status_lines(out))


def test_arbitrary_producer_fields_never_leave_diagnostic(health):
    secret = "synthetic-private-patient-and-path"
    record(health, errors=[secret], state_reasons=[secret],
           backup={"state": secret, "local": {"state": secret, "path": secret}},
           run={"error": secret}, collection=secret,
           extract_v2_coverage={"source": secret})
    out = read(health)
    assert secret not in json.dumps(out)
    assert secret not in "\n".join(observation.status_lines(out))


@pytest.mark.parametrize("raw", [b'[]', b'{', b'null', b'"private"', b'\xff',
                                b'[' * 2000, b'x' * (observation.HEALTH_BYTES + 1)])
def test_invalid_or_oversized_record_is_bounded_failure(health, raw):
    health.write_bytes(raw)
    assert read(health)["status"] == "invalid"


def test_symlink_and_fifo_are_not_followed_or_blocked(health):
    target = health.with_name("private.json")
    target.write_text('{"private":true}')
    health.symlink_to(target)
    assert read(health)["status"] == "unavailable"
    health.unlink()
    os.mkfifo(health)
    assert read(health)["status"] == "invalid"


def test_missing_health_does_not_create_files(health):
    assert read(health)["status"] == "missing"
    assert list(health.parent.iterdir()) == []


def test_old_record_preserves_absence_of_new_backup_evidence(health):
    record(health, backup={"state": "disabled"})
    out = read(health)
    assert out["backup"]["offsite_state"] == "disabled"
    assert out["backup"]["local"]["state"] == "unknown"
    assert "日次ローカルバックアップ: 未確認" in "\n".join(observation.status_lines(out))


def test_interruption_is_readonly_per_run_and_does_not_export_errors():
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE runs (run_id INTEGER,status TEXT,finished_at REAL,error TEXT)")
        db.executemany("INSERT INTO runs VALUES (?,?,?,?)", [
            (1, "crashed", NOW - 90000, "old-private-error"),
            (2, "crashed", NOW - 100, "watchdog_exit"),
            (3, "crashed", NOW - 10, "synthetic-private-error"),
            (4, "running", None, ""),
            (5, "ok", NOW, ""),
        ])
        db.commit()
        db.execute("PRAGMA query_only=ON")
        before = db.execute("SELECT * FROM runs").fetchall()
        out = observation.interrupted_runs(db, now=NOW)
        assert out["count_24h"] == 2
        assert out["by_reason_24h"] == {"unknown": 1, "watchdog_exit": 1}
        assert out["reason"] == "unknown"
        assert out["detected_at"] == NOW - 10
        assert out["occurred_at"] is None
        assert "private" not in json.dumps(out)
        assert db.execute("SELECT * FROM runs").fetchall() == before
    finally:
        db.close()


def test_unreadable_interruption_history_is_not_zero():
    db = sqlite3.connect(":memory:")
    try:
        out = observation.interrupted_runs(db, now=NOW)
        assert out["status"] == "unavailable"
        assert "count_24h" not in out
    finally:
        db.close()


@pytest.mark.parametrize("error,age,count,reason", [
    (None, None, 0, None),
    ("synthetic-private-error", 10, 1, "unknown"),
    ("synthetic-private-error", 90000, 0, "unknown"),
    ("watchdog_exit", 10, 1, "watchdog_exit"),
])
def test_interruption_writer_reader_preserves_none_vs_unknown(health, error, age,
                                                               count, reason):
    now = NOW + 100000
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE runs (run_id INTEGER,status TEXT,finished_at REAL,error TEXT)")
        if error is not None:
            db.execute("INSERT INTO runs VALUES (1,'crashed',?,?)", (now - age, error))
        record(health, at=now - 10, unread_at=now - 10,
               run={"last_interrupt": observation.interrupted_runs(db, now=now)})
    finally:
        db.close()
    out = observation.stored_status(health.parent.parent, {}, now=now)
    # These values must survive the doctor --json serialization boundary.
    interrupt = json.loads(json.dumps(out))["last_interrupt"]
    assert interrupt["reason"] == reason
    assert interrupt["count_24h"] == count
    assert interrupt["detected_at"] == (None if age is None else now - age)
    assert f"前回未完了の検出（記録時点の24時間）: {count}件" in "\n".join(
        observation.status_lines(out))


@pytest.mark.parametrize("interrupt", [{}, {"reason": None},
                                     {"status": "unavailable", "count_24h": None},
                                     {"status": "recorded", "reason": None,
                                      "count_24h": 0}])
def test_missing_interruption_evidence_does_not_mean_no_interruption(health, interrupt):
    record(health, run={"last_interrupt": interrupt})
    assert read(health)["last_interrupt"]["reason"] == "unknown"


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("sqlite_safe", [False, True])
def test_doctor_includes_stored_operations_without_probes_or_exit_change(
        health, monkeypatch, capsys, as_json, sqlite_safe):
    from types import SimpleNamespace
    import mcs_setup

    record(health, run={"last_interrupt": {
        "status": "recorded", "count_24h": 0, "reason": None, "detected_at": None}})
    before = health.read_bytes(), health.stat().st_mtime_ns
    monkeypatch.setattr(mcs_setup, "HOME", str(health.parent.parent))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(health.parent.parent / "agents"))
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {
        "mcs_login_id": "synthetic-private-id", "notify_target": "local"})
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(mcs_setup, "_runtime_probe", lambda exe: {
        "python": "3.14.8", "sqlite": "3.51.3", "packages": {}})
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info",
                        (3, 51, 3) if sqlite_safe else (3, 50, 6))
    monkeypatch.setattr(observation.time, "time", lambda: NOW)

    def forbidden(*args, **kwargs):
        pytest.fail("doctor operation display must not probe services, secrets or DB")

    monkeypatch.setattr(mcs_setup, "_run", forbidden)
    monkeypatch.setattr(mcs_setup, "_check_llm", forbidden)
    monkeypatch.setattr(mcs_setup, "check_runtime", forbidden)
    monkeypatch.setattr(mcs_setup, "env_value", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)

    def source(cfg, *, probe):
        assert probe is False
        return {"disk": {"revision": None, "dirty": False},
                "gateway": {"status": "not_checked", "pid": None,
                            "started_at": None, "source_revision": None},
                "restart_request": {"status": "missing"}}

    monkeypatch.setattr(mcs_setup, "_doctor_source", source)
    assert mcs_setup.cmd_doctor(SimpleNamespace(json=as_json)) == (0 if sqlite_safe else 1)
    output = capsys.readouterr().out
    assert "synthetic-private-id" not in output
    expected = read(health)
    if as_json:
        report = json.loads(output)
        assert report["operations"] == expected
        for scope in ("llm", "services", "credentials", "data"):
            assert report["checks"][scope]["status"] == "not_checked"
    else:
        assert output.startswith("doctor: mode=hermes")
        assert all(line in output for line in observation.status_lines(expected))
        assert "configuration: mcs setup check" in output
    assert before == (health.read_bytes(), health.stat().st_mtime_ns)
