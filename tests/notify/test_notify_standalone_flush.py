"""runtime_mode=standalone: Slack/Discord text uses `mcs_standalone send`
with the sealed JSON contract; Hermes mode still uses `hermes send`."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import notify_flush
from ledger import Ledger
from test_notify_lineworks_flush import stored_attachment


def test_custom_root_never_reads_the_default_notification_destination(tmp_path):
    user_home = tmp_path / "home"
    default = user_home / ".mcs"
    root = tmp_path / "selected"
    default.mkdir(parents=True)
    root.mkdir()
    for directory, target in ((default, "slack:C0DEFAULT9"), (root, "slack:C0SELECTED")):
        (directory / "config.json").write_text(json.dumps(
            {"runtime_mode": "standalone", "notify_target": target}))
    repository = Path(__file__).resolve().parents[2]
    script = '''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "mcs"))
import _mcs_path
import mcs_util, notify_flush
cfg = notify_flush._config()
assert cfg["notify_target"] == "slack:C0SELECTED"
assert notify_flush.CONF_PATH == mcs_util.CONF_PATH
argv = notify_flush._send_argv(cfg, cfg["notify_target"])
assert argv[argv.index("--root") + 1] == sys.argv[2]
'''
    result = subprocess.run([sys.executable, "-c", script, str(repository), str(root)],
                            env={**os.environ, "HOME": str(user_home), "MCS_ROOT": str(root)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def _flush(tmp_path, monkeypatch, cfg, returncode):
    monkeypatch.setattr(notify_flush, "CONF_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda _: "/nonexistent/hermes")
    attachment = tmp_path / "data" / "attachments" / "fixture.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"synthetic")
    monkeypatch.setattr(notify_flush, "_format_event",
                        lambda *a: ("合成通知 MEDIA:/etc/passwd", [("fixture.txt", str(attachment))]))
    calls = []

    def child(argv, **kwargs):
        calls.append((argv, kwargs["input"]))
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr(notify_flush.subprocess, "run", child)
    led = Ledger(str(tmp_path / "data" / "ledger.db"))
    stored_attachment(led, attachment)
    eid = led.outbox_add("run_failed", None, {})
    result = notify_flush.flush(led)
    row = led.db.execute("SELECT state,next_try,progress FROM notify_outbox WHERE event_id=?",
                         (eid,)).fetchone()
    led.close()
    return calls, result, row, attachment


def test_standalone_sends_without_hermes_using_sealed_json(tmp_path, monkeypatch):
    cfg = {"runtime_mode": "standalone", "notify_target": "discord:1000000000000000001"}
    calls, result, row, attachment = _flush(tmp_path, monkeypatch, cfg, 0)
    assert result["sent"] == 1 and row["state"] == "accepted"
    (argv, body), = calls
    assert Path(argv[1]).parts[-2:] == ("mcs_standalone", "__main__.py")
    assert argv[2:] == ["send", "--root", str(tmp_path), "--to", cfg["notify_target"], "--quiet"]
    # untrusted text is data, never an attachment directive
    assert json.loads(body) == {"text": "合成通知 MEDIA:/etc/passwd", "files": [{
        "name": "fixture.txt", "path": str(attachment), "bytes": 9,
        "sha256": hashlib.sha256(b"synthetic").hexdigest()}]}


@pytest.mark.parametrize("returncode,retried", [(75, True), (1, False)])
def test_standalone_tempfail_retries_but_unknown_holds(tmp_path, monkeypatch, returncode, retried):
    cfg = {"runtime_mode": "standalone", "notify_target": "slack:C0SYNTHETIC"}
    _, result, row, _ = _flush(tmp_path, monkeypatch, cfg, returncode)
    assert result["failed"] == 1
    if retried:       # provably not accepted -> scheduled retry, no hold
        assert row["next_try"] is not None and result.get("uncertain", 0) == 0
    else:             # outcome unknown -> held, never re-sent automatically
        assert row["next_try"] is None and result["uncertain"] == 1


def test_standalone_permanent_failure_quarantines_after_five_attempts(
        tmp_path, monkeypatch):
    """A permanently-refused send (revoked token, deleted channel) keeps
    the backoff retries — a short outage self-heals — then quarantines
    like every deterministic fault instead of retrying hourly forever."""
    cfg = {"runtime_mode": "standalone", "notify_target": "slack:C0SYNTHETIC"}
    monkeypatch.setattr(notify_flush, "CONF_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    monkeypatch.setattr(notify_flush, "_format_event",
                        lambda *a: ("合成通知", []))
    monkeypatch.setattr(notify_flush.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            returncode=75, stdout="", stderr=""))
    (tmp_path / "data").mkdir(parents=True)
    led = Ledger(str(tmp_path / "data" / "ledger.db"))
    eid = led.outbox_add("run_failed", None, {})
    for _ in range(4):
        res = notify_flush.flush(led)
        row = led.db.execute(
            "SELECT state,next_try FROM notify_outbox WHERE event_id=?",
            (eid,)).fetchone()
        assert res["failed"] == 1 and row["next_try"] is not None
        led.db.execute("UPDATE notify_outbox SET next_try=0 WHERE event_id=?",
                       (eid,))
        led.db.commit()
    res = notify_flush.flush(led)
    row = led.db.execute(
        "SELECT state,next_try FROM notify_outbox WHERE event_id=?",
        (eid,)).fetchone()
    led.close()
    assert res["failed"] == 1 and row["state"] == "failed" \
        and row["next_try"] is None            # held — never re-armed


def test_hermes_mode_is_unchanged(tmp_path, monkeypatch):
    cfg = {"notify_target": "discord:1000000000000000001", "hermes_bin": "/opt/hermes"}
    monkeypatch.setattr(notify_flush, "_config", lambda: cfg)
    assert notify_flush._send_argv(cfg, cfg["notify_target"]) == [
        "/opt/hermes", "send", "--to", cfg["notify_target"], "--quiet"]
