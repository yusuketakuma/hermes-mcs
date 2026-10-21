"""Native queue authorization stays current through atomic publication."""
from contextlib import contextmanager
import json

import pytest

import mcs_requests


@pytest.fixture
def request_input(tmp_path):
    inbox = tmp_path / "cmd"
    inbox.mkdir()
    request = {"version": 1, "cmd": "request.create",
               "command_id": "00000000-0000-4000-8000-000000000001",
               "actor": "synthetic operator", "human_confirmed": True,
               "project_id": 1, "source_message_id": 1, "source_hash": "a" * 64,
               "title": "synthetic task", "reason": "synthetic confirmation"}
    return request, inbox


@pytest.mark.parametrize("guarded", [False, True])
def test_authorized_and_legacy_enqueue_keep_queue_contract(request_input, monkeypatch, guarded):
    request, inbox = request_input
    real_lock = mcs_requests.queue_lock
    waits = []

    @contextmanager
    def observed_lock(path, *, wait=True):
        waits.append(wait)
        with real_lock(path, wait=wait) as acquired:
            yield acquired

    monkeypatch.setattr(mcs_requests, "queue_lock", observed_lock)
    result = mcs_requests.enqueue(request, str(inbox),
                                  **({"authorized": lambda: True} if guarded else {}))
    assert result == {"command_id": request["command_id"],
                      "payload_hash": mcs_requests.payload_hash(request), "outcome": "queued"}
    assert waits == [not guarded]
    queued, = inbox.glob("*.json")
    assert json.loads(queued.read_text()) == request
    assert not list(inbox.glob("*.tmp"))


@pytest.mark.parametrize("stage", ["lock_acquired", "file_synced"])
def test_revocation_before_publication_leaves_no_command(request_input, monkeypatch, stage):
    request, inbox = request_input
    current = True
    real_lock, real_fsync = mcs_requests.queue_lock, mcs_requests.os.fsync

    @contextmanager
    def revoke_after_lock(path, *, wait=True):
        nonlocal current
        with real_lock(path, wait=wait) as acquired:
            if acquired and stage == "lock_acquired":
                current = False
            yield acquired

    def revoke_after_fsync(fd):
        nonlocal current
        real_fsync(fd)
        if stage == "file_synced":
            current = False

    monkeypatch.setattr(mcs_requests, "queue_lock", revoke_after_lock)
    monkeypatch.setattr(mcs_requests.os, "fsync", revoke_after_fsync)
    with pytest.raises(ValueError, match="^command_scope_changed$"):
        mcs_requests.enqueue(request, str(inbox), authorized=lambda: current)
    assert not list(inbox.iterdir())
    with real_lock(str(inbox), wait=False) as acquired:
        assert acquired


@pytest.mark.parametrize("granted", [False, None, 1])
def test_authority_must_be_explicit_true(request_input, granted):
    request, inbox = request_input
    with pytest.raises(ValueError, match="^command_scope_changed$"):
        mcs_requests.enqueue(request, str(inbox), authorized=lambda: granted)
    assert not list(inbox.iterdir())


def test_published_uncertainty_is_not_reclassified_after_revocation(request_input, monkeypatch):
    request, inbox = request_input
    current = True
    real_link = mcs_requests.os.link

    def publish_then_revoke(*args, **kwargs):
        nonlocal current
        result = real_link(*args, **kwargs)
        current = False
        return result

    def fail_directory_sync(_path):
        raise OSError("synthetic directory sync failure")

    monkeypatch.setattr(mcs_requests.os, "link", publish_then_revoke)
    monkeypatch.setattr(mcs_requests, "fsync_dir", fail_directory_sync)
    result = mcs_requests.enqueue(request, str(inbox), authorized=lambda: current)
    assert result == {"command_id": request["command_id"],
                      "payload_hash": mcs_requests.payload_hash(request),
                      "outcome": "unknown", "error": "queue_durability_unknown"}
    queued, = inbox.glob("*.json")
    assert json.loads(queued.read_text()) == request
    assert not list(inbox.glob("*.tmp"))
