"""The LINE WORKS text CLI exits 75 only when provably nothing was accepted."""
import fcntl
import io
import json
import os
import time
from types import SimpleNamespace

import pytest
from adapters.lineworks import __main__ as cli
from adapters.lineworks.client import ClientError
from test_lineworks_adapter import CONFIG, SCOPE, SECRET, raw, world


def _setup(monkeypatch, tmp_path, error=None):
    w = world(tmp_path)
    w.client.error = error
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    monkeypatch.setattr(cli, "load_credentials", lambda *args: (w.client, SECRET))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw({"text": "合成通知"}))))
    monkeypatch.setattr(cli, "SENDER_BUSY_WAIT", 0)
    return w


def _send(tmp_path):
    return cli.main(["send", "--root", str(tmp_path), "--to", "lineworks:" + SCOPE["channel_id"]])


def _hold_lock(w):
    fd = os.open(os.path.join(w.dirs["state"], "api.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_held_api_lock_exits_tempfail_without_sending(monkeypatch, tmp_path, capsys):
    w = _setup(monkeypatch, tmp_path)
    fd = _hold_lock(w)
    try:
        assert _send(tmp_path) == 75
    finally:
        os.close(fd)
    assert w.client.calls == [] and capsys.readouterr().err.strip() == "sender_busy"


def test_brief_api_lock_hold_is_waited_out(monkeypatch, tmp_path):
    w = _setup(monkeypatch, tmp_path)
    fd = _hold_lock(w)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: os.close(fd))
    assert _send(tmp_path) == 0 and len(w.client.calls) == 1


def test_cooldown_exits_tempfail_without_sending(monkeypatch, tmp_path):
    w = _setup(monkeypatch, tmp_path)
    with open(os.path.join(w.dirs["state"], "rate-limit.json"), "w") as stream:
        json.dump({"until": time.time() + 60}, stream)
    assert _send(tmp_path) == 75 and w.client.calls == []


@pytest.mark.parametrize("error, code", [(ClientError("http_error", 403), 75),
                                         (ClientError("rate_limited", 429), 75),
                                         (ClientError("http_error", 500), 1),
                                         (ClientError("transport_unknown"), 1)])
def test_first_post_reject_is_tempfail_but_unknown_stays_one(monkeypatch, tmp_path, error, code):
    w = _setup(monkeypatch, tmp_path, error)
    assert _send(tmp_path) == code and len(w.client.calls) == 1


def test_failure_after_text_accepted_stays_one(monkeypatch, tmp_path):
    w = _setup(monkeypatch, tmp_path)
    attachments = w.data / "attachments"
    attachments.mkdir()
    path = attachments / "fixture.txt"
    path.write_bytes(b"synthetic-sealed")
    import hashlib
    payload = {"text": "合成通知", "files": [{"path": str(path), "name": "fixture.txt", "bytes": 16,
                                          "sha256": hashlib.sha256(b"synthetic-sealed").hexdigest()}]}
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw(payload))))

    def reject(blob, name):
        raise ClientError("http_error", 403)

    w.client.upload_file = reject
    assert _send(tmp_path) == 1 and len(w.client.calls) == 1
