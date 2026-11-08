"""Old restart writers cannot occupy the next host's automatic intent path."""
import json

import pytest

import safe_io
from mcs_standalone import runtime, service

NOW = 1_000_000.0
OLD = "a" * 32
CURRENT = "c" * 32


def _write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def _request(generation=CURRENT):
    return {"generation": generation, "request_id": "b" * 32, "requested_at": NOW}


@pytest.fixture
def host(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir(mode=0o700)
    monkeypatch.setattr(runtime.time, "time", lambda: NOW)
    monkeypatch.setattr(service, "pid_exists", lambda pid: True)
    owner = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    owner.generation = CURRENT
    owner._publish_status(NOW, False)
    return owner


def test_late_old_automatic_publication_cannot_block_new_generation(host, monkeypatch):
    old_status = service.status(host.root)
    old_status["generation"] = OLD
    read_status = service.status
    monkeypatch.setattr(service, "status", lambda *a, **kw: dict(old_status))
    publish = safe_io.publish_new_bytes

    def late_publication(path, raw):
        assert path == service.restart_request_path(host.root, OLD)
        # The sender already captured OLD before the replacement published its heartbeat.
        host._publish_status(NOW, False)
        publish(path, raw)

    monkeypatch.setattr(safe_io, "publish_new_bytes", late_publication)
    service.request_restart(host.root, expected_generation=OLD)
    previous = service.restart_request_path(host.root, OLD)
    before = previous.read_bytes()
    assert not host._restart_requested(NOW)
    assert not service.automatic_restart_blocked(host.root, CURRENT)
    monkeypatch.setattr(service, "status", read_status)
    monkeypatch.setattr(safe_io, "publish_new_bytes", publish)
    service.request_restart(host.root, expected_generation=CURRENT)
    assert host._restart_requested(NOW) and previous.read_bytes() == before


def test_late_old_manual_publication_is_preserved_without_blocking_auto(host, monkeypatch):
    old_status = service.status(host.root)
    old_status["generation"] = OLD
    read_status = service.status
    monkeypatch.setattr(service, "status", lambda *a, **kw: dict(old_status))
    write = service.atomic_write

    def late_publication(path, writer, **kw):
        host._publish_status(NOW, False)
        return write(path, writer, **kw)

    monkeypatch.setattr(service, "atomic_write", late_publication)
    service.request_restart(host.root)
    manual = host.data / service.RESTART_FILE
    before = manual.read_bytes()
    assert json.loads(before)["generation"] == OLD
    monkeypatch.setattr(service, "status", read_status)
    service.request_restart(host.root, expected_generation=CURRENT)
    assert host._restart_requested(NOW) and manual.read_bytes() == before


def test_manual_racing_after_auto_preflight_is_not_overwritten_or_deleted(host, monkeypatch):
    publish = safe_io.publish_new_bytes
    manual = service.restart_request_path(host.root)
    observed = []

    def racing(path, raw):
        service.request_restart(host.root)
        observed.append(manual.read_bytes())
        publish(path, raw)

    monkeypatch.setattr(safe_io, "publish_new_bytes", racing)
    service.request_restart(host.root, expected_generation=CURRENT)
    auto = service.restart_request_path(host.root, CURRENT)
    assert auto.exists() and manual.read_bytes() == observed[0]
    assert host._restart_requested(NOW) is True
    assert auto.exists() and manual.read_bytes() == observed[0]
    with pytest.raises(ValueError, match="standalone_restart_busy"):
        service.request_restart(host.root, expected_generation=CURRENT)
    assert manual.read_bytes() == observed[0]


@pytest.mark.parametrize("damage", ["current", "extra", "missing", "bool_time", "negative_time",
                                    "nonfinite", "bad_uuid", "duplicate", "symlink", "public"])
def test_current_or_unverified_manual_intent_blocks_auto_without_mutation(host, damage):
    path = service.restart_request_path(host.root)
    value = _request()
    if damage == "extra":
        value["extra"] = "synthetic_secret"
    elif damage == "missing":
        value.pop("request_id")
    elif damage == "bool_time":
        value["requested_at"] = True
    elif damage == "negative_time":
        value["requested_at"] = -1
    elif damage == "nonfinite":
        value["requested_at"] = float("inf")
    elif damage == "bad_uuid":
        value["request_id"] = "invalid"
    if damage == "symlink":
        target = host.data / "synthetic-request"
        _write(target, value)
        path.symlink_to(target)
    else:
        _write(path, value)
        if damage == "duplicate":
            path.write_text('{"generation":"' + OLD + '",' + json.dumps(value)[1:])
        if damage == "public":
            path.chmod(0o644)
    before = path.read_bytes()
    assert service.automatic_restart_blocked(host.root, CURRENT)
    with pytest.raises(ValueError, match="standalone_restart_busy"):
        service.request_restart(host.root, expected_generation=CURRENT)
    assert path.read_bytes() == before
    assert not service.restart_request_path(host.root, CURRENT).exists()


@pytest.mark.parametrize("manual", ["old", "broken"])
def test_valid_current_auto_is_read_even_when_manual_file_is_old_or_broken(host, manual):
    service.request_restart(host.root, expected_generation=CURRENT)
    path = service.restart_request_path(host.root)
    if manual == "old":
        _write(path, _request(OLD))
    else:
        path.write_text("{synthetic")
        path.chmod(0o600)
    before = path.read_bytes()
    assert host._restart_requested(NOW) is True
    assert path.read_bytes() == before


def test_current_auto_intent_blocks_passive_restart_planning(host):
    service.request_restart(host.root, expected_generation=CURRENT)
    with pytest.raises(RuntimeError, match="standalone_restart_planning_unavailable"):
        host._planning_controls_clear()
    assert not host.restarting


def test_manual_api_keeps_existing_replace_contract(host):
    first = service.request_restart(host.root)
    second = service.request_restart(host.root)
    assert first != second
    path = host.data / service.RESTART_FILE
    assert service.read_restart_request(path) == {
        "generation": CURRENT, "request_id": second, "requested_at": NOW}
    assert host._restart_requested(NOW)
    assert not service.restart_request_path(host.root, CURRENT).exists()


def test_automatic_intent_keeps_existing_freshness_gate(host):
    service.request_restart(host.root, expected_generation=CURRENT)
    assert host._restart_requested(NOW + service.STATUS_TTL)
    assert not host._restart_requested(NOW + service.STATUS_TTL + 0.01)
    assert service.automatic_restart_blocked(host.root, CURRENT)
