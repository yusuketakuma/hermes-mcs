"""Synthetic published snapshot -> actual C1 CLI -> manual local outbox."""

import hashlib
import json
import sys
from contextlib import closing
from pathlib import Path

import pytest

from c1_contract import C1_FIELDS
import c1_envelopes
import ext_contract as ext
import ledger
import mcs_setup
import mcs_view
from test_c1_envelopes import _records
from test_c1_records import SNAP_TS, _snapshot
from test_ext_contract import _auth


def _cli(capsys, args):
    code = ext.main(args)
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def world(tmp_path, monkeypatch):
    data = tmp_path / "root" / "data"
    data.mkdir(parents=True)
    live, snapshot = _snapshot(data, monkeypatch)
    monkeypatch.setattr(ext.time, "time", lambda: SNAP_TS + 10)
    auth = _auth(tmp_path, fields=list(C1_FIELDS),
                 created_at=SNAP_TS, expires_at=SNAP_TS + 3600,
                 max_snapshot_age_s=3600)
    state, sink = tmp_path / "state", tmp_path / "sink"
    return live, Path(snapshot), auth, state, sink


def test_snapshot_dry_run_has_no_output_files_or_source_changes(world, capsys):
    live, snapshot, auth, state, sink = world
    before = [(hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
              for p in (live, snapshot)]
    code, result = _cli(capsys, [
        "handoff", "--c1", "--auth", str(auth), "--snapshot", str(snapshot),
        "--state", str(state), "--sink", str(sink), "--dry-run"])
    assert code == 0 and result["status"] == "dry_run"
    assert all(item["bytes"] <= c1_envelopes.MAX_WIRE_BYTES for item in result["envelopes"])
    assert "records" not in result and "body_text" not in json.dumps(result)
    assert not state.exists() and not sink.exists()
    assert [(hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in (live, snapshot)] == before


def test_actual_snapshot_handoff_stages_v2_without_self_ack(world, capsys):
    _, snapshot, auth, state, sink = world
    code, result = _cli(capsys, [
        "handoff", "--c1", "--auth", str(auth), "--snapshot", str(snapshot),
        "--state", str(state), "--sink", str(sink), "--only-with-facts",
        "--max-bytes", "20000"])
    assert code == 0 and result["messages_kept"] > 0, result
    assert all(item["status"] == "held" for item in result["envelopes"])
    assert not (sink / "acks").exists()
    bodies = []
    for item in result["envelopes"]:
        envelope = c1_envelopes.parse_envelope(
            (sink / "envelopes" / f"{item['envelope_id']}.json").read_bytes())
        assert envelope["contract"] == "mcs-ext-export/2"
        bodies += [r for r in envelope["records"] if r["type"] == "message_body"]
    assert bodies and all(body["sender_kind"] == "unknown" for body in bodies)


@pytest.mark.parametrize("dry", [False, True])
def test_argumentless_handoff_uses_explicit_profile(world, monkeypatch, capsys, dry):
    _, snapshot, auth, state, sink = world
    monkeypatch.setattr(mcs_setup, "HOME", str(snapshot.parent.parent.parent))
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {"ext_export": {
        "auth": str(auth), "state_dir": str(state), "outbox": str(sink), "since_days": 30}})
    code, result = _cli(capsys, ["handoff", *(["--dry-run"] if dry else [])])
    assert code == 0
    assert result["envelopes"]
    assert state.exists() is not dry and sink.exists() is not dry
    if dry:
        assert result["status"] == "dry_run"
    else:
        assert all(item["status"] == "held" for item in result["envelopes"])


@pytest.mark.parametrize("profile", [
    None, {}, {"auth": "/synthetic"},
    {"auth": "relative", "state_dir": "/synthetic/state", "outbox": "/synthetic/out",
     "since_days": 30},
    {"auth": "/synthetic/auth", "state_dir": "/synthetic/state", "outbox": "/synthetic/out",
     "since_days": True},
])
def test_argumentless_handoff_rejects_missing_or_invalid_profile(profile, monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {"ext_export": profile})
    code, result = _cli(capsys, ["handoff", "--dry-run"])
    assert code == 1 and result["status"] == "refused"
    assert result["reason"].startswith("handoff_profile_")


def test_c1_does_not_chmod_shared_existing_destination(world, capsys):
    _, snapshot, auth, state, sink = world
    sink.mkdir(mode=0o755)
    before = sink.stat()
    code, result = _cli(capsys, [
        "handoff", "--c1", "--auth", str(auth), "--snapshot", str(snapshot),
        "--state", str(state), "--sink", str(sink)])
    assert code == 1 and result["reason"] == "c1_private_directory_required"
    after = sink.stat()
    assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)
    assert not list(sink.iterdir()) and not state.exists()


def test_c1_heartbeat_warns_without_claiming_missing_clinical_action(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ext.time, "time", lambda: 1030)
    auth = _auth(tmp_path, fields=list(C1_FIELDS), expires_at=2000, created_at=1000,
                 max_snapshot_age_s=3600)
    source = tmp_path / "records.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in _records()))
    code, result = _cli(capsys, [
        "handoff", "--c1", "--auth", str(auth), "--records", str(source),
        "--state", str(tmp_path / "state"), "--sink", str(tmp_path / "sink")])
    assert code == 0
    assert result["messages_kept"] == 0 and result["warnings"] == ["messages_kept_zero"]
    assert result["envelopes"][0]["status"] == "held"


def test_link_hints_real_local_terminal_paging_does_not_write(world, monkeypatch, capsys):
    live, snapshot, _, state, sink = world
    with closing(ledger.Ledger(str(live))) as db:
        db.db.execute("UPDATE patients SET patient_name='SYNTHETIC_PERSON_'||project_id")
        db.db.commit()
    assert ledger.publish_snapshot(str(live), str(snapshot.parent)) is not None
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {"runtime_mode": "hermes"})
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (live, snapshot)]
    seen = []
    cursor = None
    for _ in range(20):
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        code, result = _cli(capsys, [
            "link-hints", "--snapshot", str(snapshot), "--limit", "1",
            *(["--cursor", cursor] if cursor else [])])
        assert code == 0 and result["binding"] == "local_hint_only"
        for row in result["items"]:
            assert row["patient_name"].startswith("SYNTHETIC_PERSON_")
            assert set(row) == {"project_id", "patient_name", "last_message_at"}
            seen.append(row["project_id"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and len(seen) == len(set(seen)) > 1
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in (live, snapshot)] == before
    assert not state.exists() and not sink.exists()


@pytest.mark.parametrize(("mode", "tty"), [("standalone", True), ("hermes", False)])
def test_link_hints_refuses_nonlocal_surface_before_read(monkeypatch, capsys, mode, tty):
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {"runtime_mode": mode})
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)

    def forbidden(_path):
        raise AssertionError("must not open a snapshot")

    monkeypatch.setattr(mcs_view, "View", forbidden)
    code, result = _cli(capsys, ["link-hints"])
    assert code == 1 and result["reason"] == "link_hints_local_terminal_required"


def test_link_hints_cursor_cannot_cross_generation(world, monkeypatch, capsys):
    live, snapshot, _, _, _ = world
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {"runtime_mode": "hermes"})
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    code, first = _cli(capsys, ["link-hints", "--snapshot", str(snapshot), "--limit", "1"])
    assert code == 0 and first["next_cursor"] is not None
    assert ledger.publish_snapshot(str(live), str(snapshot.parent)) is not None
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    code, second = _cli(capsys, [
        "link-hints", "--snapshot", str(snapshot), "--limit", "1",
        "--cursor", first["next_cursor"]])
    assert code == 1 and second["status"] == "refused"
