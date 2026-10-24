"""Synthetic oracles for the pure, unsent R4a diagnostic projection."""
import json
from pathlib import Path
import re

import pytest

import sdc_support_diagnostics as projection

CANARY = "SYNTHETIC_PHI_SECRET_DO_NOT_COPY"
COMMIT = "a" * 40


def source(**software):
    return {"schema_version": "mcs.support-diagnostics.v1",
            "software": software, "collection": {"observed_at": 111}}


@pytest.mark.parametrize("mode", ["hermes", "standalone"])
def test_runtime_projects_only_the_two_supported_facts(mode):
    shared = source(mode=mode, disk_commit=COMMIT, loaded_commit="b" * 40,
                    process="running", installed_version="1.2.3", loaded_version="1.2.3")
    result = projection.project_diagnostic("runtime.status.v1", shared, generated_at_ms=222)
    assert result == {
        "schema_version": "sdc.support.diagnostic-result.v1", "diagnostic_id": "runtime.status.v1",
        "generated_at": 222, "observed_at": None, "status": "partial", "duration_ms": None,
        "error_code": "SOURCE_UNKNOWN", "data": {
            "mode": mode, "process": "unknown", "installed_commit": COMMIT,
            "loaded_commit": None, "installed_version": None, "loaded_version": None}}
    assert shared["collection"]["observed_at"] == 111


@pytest.mark.parametrize("software", [{}, {"mode": "invalid"}, {"mode": True},
    {"mode": [CANARY], "disk_commit": {"value": CANARY}},
    {"disk_commit": "a" * 40 + "\n"}, {"disk_commit": "A" * 40}])
def test_unusable_runtime_facts_remain_unknown(software):
    result = projection.project_diagnostic("runtime.status.v1", source(**software), generated_at_ms=0)
    assert result["status"] == "unknown" and result["error_code"] == "SOURCE_UNKNOWN"
    assert result["data"]["mode"] == "unknown"
    assert result["data"]["installed_commit"] is None
    assert CANARY not in json.dumps(result)


@pytest.mark.parametrize("software", [{"mode": "hermes"}, {"disk_commit": COMMIT}])
def test_one_supported_fact_is_partial_never_observed(software):
    result = projection.project_diagnostic("runtime.status.v1", source(**software), generated_at_ms=0)
    assert result["status"] == "partial" and result["error_code"] == "SOURCE_UNKNOWN"


@pytest.mark.parametrize("diagnostic_id,expected", [
    ("notification.health.v1", {"service": "unknown", "credential_configured": "unknown",
                                "authentication": "unknown", "retry": "unknown"}),
    ("connectivity.status.v1", {"target_id": "sdc-central", "connection": "unknown", "duration_ms": None})])
def test_absent_witnesses_are_unknown_even_with_forged_extra_fields(diagnostic_id, expected):
    shared = source(mode="standalone", disk_commit=COMMIT)
    shared.update(notification={"authentication": "valid", "credential_configured": "configured"},
                  connectivity={"connection": "reachable", "duration_ms": 1},
                  arbitrary={CANARY: CANARY})
    result = projection.project_diagnostic(diagnostic_id, shared, generated_at_ms=222)
    assert result["data"] == expected
    assert result["status"] == "unknown" and result["error_code"] == "SOURCE_UNKNOWN"
    assert CANARY not in json.dumps(result)


@pytest.mark.parametrize("diagnostic_id", projection.DIAGNOSTICS)
def test_canonical_closed_json_contains_no_arbitrary_keys_or_values(diagnostic_id):
    shared = source(mode=CANARY, disk_commit=CANARY, loaded_commit=COMMIT)
    shared[CANARY] = {CANARY: CANARY}
    raw = projection.diagnostic_json(diagnostic_id, shared, generated_at_ms=projection.MAX_SAFE_INTEGER)
    result = json.loads(raw)
    assert CANARY.encode() not in raw and len(raw) <= 16384 and raw.isascii()
    assert raw == json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    assert result["generated_at"] == 9007199254740991
    assert result["observed_at"] is None
    assert result["duration_ms"] is None


@pytest.mark.parametrize("bad", [True, False, -1, 9007199254740992, 1.0, None, CANARY])
def test_assembly_timestamp_is_required_and_strict(bad):
    with pytest.raises(ValueError, match="^SDC_DIAGNOSTIC_TIME_INVALID$"):
        projection.project_diagnostic("runtime.status.v1", source(), generated_at_ms=bad)


def test_missing_timestamp_is_not_filled_from_collection():
    with pytest.raises(TypeError):
        projection.project_diagnostic("runtime.status.v1", source())


@pytest.mark.parametrize("bad", [CANARY, "runtime.status.v2", None, True, [], {}])
def test_unknown_diagnostic_has_fixed_error(bad):
    with pytest.raises(ValueError, match="^SDC_DIAGNOSTIC_INVALID$"):
        projection.project_diagnostic(bad, source(), generated_at_ms=0)


@pytest.mark.parametrize("bad", [[], True, CANARY, {},
    {"schema_version": CANARY, "software": {}},
    {"schema_version": "mcs.support-diagnostics.v1", "software": []},
    {"schema_version": "mcs.support-diagnostics.v1", "software": {}, "extra": float("nan")},
    {"schema_version": "mcs.support-diagnostics.v1", "software": {}, "extra": "x" * 65537},
    {"schema_version": "mcs.support-diagnostics.v1", "software": {}, "extra": [0] * 4097},
    {"schema_version": "mcs.support-diagnostics.v1", "software": {}, "extra": [[[[[[[[[0]]]]]]]]]}])
@pytest.mark.parametrize("diagnostic_id", projection.DIAGNOSTICS)
def test_malformed_or_oversized_source_is_closed_failure(bad, diagnostic_id):
    result = projection.project_diagnostic(diagnostic_id, bad, generated_at_ms=0)
    assert result["status"] == "failed" and result["error_code"] == "DIAGNOSTIC_FAILED"
    assert CANARY not in json.dumps(result)
    if diagnostic_id == "runtime.status.v1":
        assert result["data"]["installed_commit"] is None


def test_unavailable_source_has_distinct_fixed_failure():
    result = projection.project_diagnostic("runtime.status.v1", None, generated_at_ms=0)
    assert result["status"] == "failed" and result["error_code"] == "SOURCE_UNAVAILABLE"


def test_all_outputs_match_fixed_schema_fields_and_value_constraints():
    contract = Path(__file__).resolve().parents[2] / "adapters/sdc/contracts/support/diagnostic-result.schema.json"
    schema = json.loads(contract.read_text())
    assert schema["additionalProperties"] is False
    assert set(projection.DIAGNOSTICS) == set(schema["properties"]["diagnostic_id"]["enum"])
    for diagnostic_id in projection.DIAGNOSTICS:
        branch = next(branch["properties"]["data"] for branch in schema["oneOf"]
                      if branch["properties"]["diagnostic_id"]["const"] == diagnostic_id)
        assert branch["additionalProperties"] is False
        for shared in (None, {}, source(), source(mode="hermes", disk_commit=COMMIT)):
            result = projection.project_diagnostic(diagnostic_id, shared, generated_at_ms=0)
            assert set(result) == set(schema["required"]) == set(schema["properties"])
            assert result["schema_version"] == schema["properties"]["schema_version"]["const"]
            assert result["status"] in schema["properties"]["status"]["enum"]
            assert result["error_code"] in schema["properties"]["error_code"]["enum"]
            assert result["status"] != "observed" and result["error_code"] != "NONE"
            assert set(result["data"]) == set(branch["required"]) == set(branch["properties"])
            for name, value in result["data"].items():
                definition = branch["properties"][name]
                if "enum" in definition:
                    assert value in definition["enum"]
                elif "const" in definition:
                    assert value == definition["const"]
                elif value is None:
                    assert {"type": "null"} in definition["anyOf"]
                else:
                    pattern = next(item["pattern"] for item in definition["anyOf"] if "pattern" in item)
                    assert re.fullmatch(pattern, value)


@pytest.fixture
def private_reader():
    import safe_io

    return safe_io.read_private_bytes


@pytest.fixture
def provider_source(tmp_path, monkeypatch, private_reader):
    import os
    import socket
    import sqlite3
    import subprocess
    import mcs_util
    import safe_io
    from mcs_standalone import service
    from adapters.sdc.keychain import KeychainCredentialStore

    def forbidden(*_args, **_kwargs):
        pytest.fail("provider accessed a forbidden source or active probe")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(mcs_util, "load_config", forbidden)
    monkeypatch.setattr(mcs_util, "env_value", forbidden)
    monkeypatch.setattr(KeychainCredentialStore, "read", forbidden)
    signals = []

    def pid_probe(pid, signal):
        assert (pid, signal) == (471100, 0)
        signals.append((pid, signal))

    monkeypatch.setattr(os, "kill", pid_probe)
    root = tmp_path / "product"
    (root / "data").mkdir(parents=True)
    witness = {"pid": 471100, "generation": "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa",
               "started_at": 90, "updated_at": 99, "children": {},
               "connector": {"state": "running", "last_error": CANARY},
               "unknown": {CANARY: CANARY}, "loaded_commit": COMMIT}
    path = root / "data" / service.STATUS_FILE
    original_read = private_reader

    def registered_read(source_path, limit=16384, *, dir_fd=None):
        assert Path(source_path) == Path(service.STATUS_FILE) and limit == 1024 * 1024
        assert type(dir_fd) is int
        borrowed, expected = os.fstat(dir_fd), path.parent.stat()
        assert (borrowed.st_dev, borrowed.st_ino) == (expected.st_dev, expected.st_ino)
        return original_read(source_path, limit, dir_fd=dir_fd)

    monkeypatch.setattr(safe_io, "read_private_bytes", registered_read)

    def publish(**changes):
        value = {**witness, **changes}
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return value

    publish()
    return root, path, publish, signals


@pytest.fixture
def provider_fds(provider_source, monkeypatch):
    import os

    original_open, original_close = os.open, os.close
    opened, closed = [], []

    def capture_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    def capture_close(fd):
        closed.append(fd)
        return original_close(fd)

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(os, "close", capture_close)
    return opened, closed


def assert_provider_fds_closed(opened, closed):
    import errno
    import os

    assert len(closed) == len(set(closed))
    for fd in opened:
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF


def test_provider_fresh_owner_projects_only_safe_partial_facts(provider_source):
    root, _, _, signals = provider_source
    runtime = projection.collect_diagnostic("runtime.status.v1", root=root, mode="standalone", generated_at_ms=100000)
    notify = projection.collect_diagnostic("notification.health.v1", root=root, mode="standalone", generated_at_ms=100000)
    assert runtime["data"] == {"mode": "standalone", "process": "running", "installed_commit": None,
                               "loaded_commit": None, "installed_version": None, "loaded_version": None}
    assert notify["data"] == {"service": "running", "credential_configured": "unknown",
                              "authentication": "unknown", "retry": "unknown"}
    for result in (runtime, notify):
        assert result["status"] == "partial" and result["error_code"] == "SOURCE_UNKNOWN"
        assert result["observed_at"] == 99000 and result["generated_at"] == 100000
        assert result["duration_ms"] is None
        assert CANARY not in json.dumps(result) and "471100" not in json.dumps(result)
    assert signals == [(471100, 0), (471100, 0)]


@pytest.mark.parametrize("changes", [
    {"updated_at": 84.999}, {"updated_at": 100.001}, {"updated_at": True},
    {"updated_at": float("nan")}, {"updated_at": float("inf")}, {"updated_at": 2**10000},
    {"started_at": 99.001}, {"started_at": -1}, {"started_at": True},
    {"generation": "a" * 32}, {"generation": "AAAAAAAAAAAA4AAA8AAAAAAAAAAAAAAA"},
    {"generation": "a" * 31}, {"children": []}])
def test_provider_invalid_stale_or_future_owner_never_proves_running(provider_source, changes):
    root, _, publish, _ = provider_source
    publish(**changes)
    for diagnostic_id in ("runtime.status.v1", "notification.health.v1"):
        result = projection.collect_diagnostic(diagnostic_id, root=root, mode="standalone", generated_at_ms=100000)
        assert result["observed_at"] is None and result["error_code"] == "SOURCE_UNKNOWN"
        assert "running" not in result["data"].values()
        assert "stopped" not in result["data"].values()


def test_provider_accepts_exact_freshness_boundary_and_dead_pid_is_unknown(provider_source, monkeypatch):
    from mcs_standalone import service
    root, _, publish, _ = provider_source
    publish(updated_at=85, started_at=85)
    assert projection.collect_diagnostic("runtime.status.v1", root=root, mode="standalone", generated_at_ms=100000)["observed_at"] == 85000
    monkeypatch.setattr(service, "pid_exists", lambda _pid: False)
    result = projection.collect_diagnostic("runtime.status.v1", root=root, mode="standalone", generated_at_ms=100000)
    assert result["data"]["process"] == "unknown" and result["observed_at"] is None


@pytest.mark.parametrize("connector,expected", [
    ({"state": "waiting", "retry_at": 101}, "backoff"),
    ({"state": "waiting", "retry_at": 100}, "unknown"),
    ({"state": "waiting", "retry_at": True}, "unknown"),
    ({"state": "waiting", "retry_at": float("inf")}, "unknown"),
    ({"state": "waiting", "retry_at": 2**10000}, "unknown"),
    ({"state": "off", "retry_at": 101}, "unknown"),
    ({"state": CANARY, "retry_at": 101}, "unknown"), ([], "unknown")])
def test_provider_connector_retry_is_not_authentication_or_delivery(provider_source, connector, expected):
    root, _, publish, _ = provider_source
    publish(connector=connector)
    result = projection.collect_diagnostic("notification.health.v1", root=root, mode="standalone", generated_at_ms=100000)
    assert result["data"] == {"service": "unknown", "credential_configured": "unknown",
                              "authentication": "unknown", "retry": expected}
    assert result["status"] == ("partial" if expected == "backoff" else "unknown")
    assert CANARY not in json.dumps(result)


@pytest.mark.parametrize("kind", ["missing", "malformed", "symlink", "fifo", "oversize"])
def test_provider_reuses_private_reader_boundaries(provider_source, provider_fds, kind):
    import os
    root, path, _, signals = provider_source
    witness = path.read_bytes()
    path.unlink()
    if kind == "malformed":
        path.write_bytes(b"SYNTHETIC_PRIVATE_INVALID_JSON")
        path.chmod(0o600)
    elif kind == "symlink":
        target = root / "unread-witness"
        target.write_bytes(witness)
        target.chmod(0o600)
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "oversize":
        with path.open("wb") as stream:
            stream.truncate(1024 * 1024 + 1)
        path.chmod(0o600)
    result = projection.collect_diagnostic("runtime.status.v1", root=root, mode="standalone", generated_at_ms=100000)
    assert result["data"]["process"] == "unknown" and result["observed_at"] is None
    assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
    assert signals == []
    opened, closed = provider_fds
    assert len(opened) == (2 if kind in ("missing", "symlink") else 3)
    assert len(closed) == 2 and set(closed) == set(opened[:2])
    assert_provider_fds_closed(opened, closed)


@pytest.mark.parametrize("level", ["root", "data"])
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
@pytest.mark.parametrize("diagnostic_id", ["runtime.status.v1", "notification.health.v1"])
def test_provider_never_traverses_unsafe_owner_directories(provider_source, provider_fds,
                                                          private_reader, monkeypatch, level, kind, diagnostic_id):
    import os
    import safe_io

    root, path, _, signals = provider_source
    # Exercise the actual path traversal, independently of registered_read's
    # new dirfd contract: a caller that follows the link reaches a valid witness.
    monkeypatch.setattr(safe_io, "read_private_bytes", private_reader)
    before = path.read_bytes()
    candidate = root.parent / "unsafe-product"
    if level == "data":
        candidate.mkdir()
    unsafe = candidate if level == "root" else candidate / "data"
    if kind == "symlink":
        unsafe.symlink_to(root if level == "root" else path.parent, target_is_directory=True)
    else:
        os.mkfifo(unsafe, 0o600)
    result = projection.collect_diagnostic(diagnostic_id, root=candidate, mode="standalone", generated_at_ms=100000)
    assert result["observed_at"] is None and result["error_code"] == "SOURCE_UNKNOWN"
    assert "running" not in result["data"].values() and "stopped" not in result["data"].values()
    assert CANARY not in json.dumps(result) and signals == []
    opened, closed = provider_fds
    assert len(opened) == (0 if level == "root" else 1)
    assert closed == opened
    assert_provider_fds_closed(opened, closed)
    assert path.read_bytes() == before


@pytest.mark.parametrize("entry", ["service", "projection"])
def test_provider_reader_timeout_propagates_original_instance_and_closes_directories(
        provider_source, provider_fds, monkeypatch, entry):
    import safe_io
    from mcs_standalone import service

    root, path, _, signals = provider_source
    before = path.read_bytes()
    original = TimeoutError("synthetic-original-deadline")
    calls = []

    def timeout(source_path, limit=16384, *, dir_fd=None):
        # Both absolute-path and directory-relative callers must reach the
        # same original exception; argument assertions cannot stand in for it.
        calls.append((source_path, limit, dir_fd))
        raise original

    monkeypatch.setattr(safe_io, "read_private_bytes", timeout)
    with pytest.raises(TimeoutError) as caught:
        if entry == "service":
            service.status(root, now=100)
        else:
            projection.collect_diagnostic("runtime.status.v1", root=root, mode="standalone", generated_at_ms=100000)
    assert caught.value is original and len(calls) == 1 and signals == []
    opened, closed = provider_fds
    assert len(opened) == 2
    assert len(closed) == 2 and set(closed) == set(opened)
    assert_provider_fds_closed(opened, closed)
    assert path.read_bytes() == before


def test_provider_reader_closes_its_resource_without_changing_witness(provider_source, provider_fds, monkeypatch):
    import safe_io
    root, path, _, _ = provider_source
    before = path.read_bytes()
    original, streams = safe_io.fdopen_owned, []

    def capture(*args, **kwargs):
        stream = original(*args, **kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr(safe_io, "fdopen_owned", capture)
    projection.collect_diagnostic("runtime.status.v1", root=root, mode="standalone", generated_at_ms=100000)
    assert streams and all(stream.closed for stream in streams)
    opened, closed = provider_fds
    assert len(opened) == 3 and len(closed) == 2 and set(closed) == set(opened[:2])
    assert_provider_fds_closed(opened, closed)
    assert path.read_bytes() == before


def test_provider_never_discovers_sources_for_other_modes_or_connectivity(monkeypatch):
    from mcs_standalone import service
    monkeypatch.setattr(service, "status", lambda *_args, **_kwargs: pytest.fail("unexpected source read"))
    for mode in (None, True, "invalid", "hermes"):
        for diagnostic_id in projection.DIAGNOSTICS:
            result = projection.collect_diagnostic(diagnostic_id, generated_at_ms=100000, root="/synthetic/product", mode=mode)
            assert result["observed_at"] is None
    for root in (None, "", "relative", 123):
        result = projection.collect_diagnostic("runtime.status.v1", generated_at_ms=100000, root=root, mode="standalone")
        assert result["data"]["process"] == "unknown"
    result = projection.collect_diagnostic("connectivity.status.v1", generated_at_ms=100000, root="/synthetic/product", mode="standalone")
    assert result["data"] == {"target_id": "sdc-central", "connection": "unknown", "duration_ms": None}


@pytest.mark.parametrize("generated", [True, -1, 1.0, 2**53, None])
def test_provider_rejects_unsafe_generation_time_before_read(generated, monkeypatch):
    from mcs_standalone import service
    monkeypatch.setattr(service, "status", lambda *_args, **_kwargs: pytest.fail("invalid time read"))
    with pytest.raises(ValueError, match="^SDC_DIAGNOSTIC_TIME_INVALID$"):
        projection.collect_diagnostic("runtime.status.v1", generated_at_ms=generated, root="/synthetic/product", mode="standalone")
