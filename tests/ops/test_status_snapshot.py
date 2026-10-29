"""The status snapshot exports only listed numbers and machine codes. Synthetic files only."""
import json
import os

import pytest

import status_snapshot

HEALTH = {
    "overall": "ok", "state_reasons": ["notification_pending"], "at": 1.5e9,
    "run_id": 7, "run": {"elapsed_s": 12.5},
    "notify": {"state": "ok", "pending": 2, "held": 0},
    "stall_marks": {"llm_down_since": None},
    "collection": "合成患者さんの本文",           # free text must never leave
}
SPEC = {"schema": "mcs-status-fields/1",
        "sources": {"health": {"kind": "json", "path": "health.json"},
                    "workers": {"kind": "json_dir", "path": "worker_status"}},
        "fields": [
            {"name": "overall", "source": "health", "path": "overall", "type": "code"},
            {"name": "state_reasons", "source": "health", "path": "state_reasons", "type": "codes"},
            {"name": "run_id", "source": "health", "path": "run_id", "type": "int"},
            {"name": "run_elapsed_s", "source": "health", "path": "run.elapsed_s", "type": "number"},
            {"name": "notify_pending", "source": "health", "path": "notify.pending", "type": "int"},
            {"name": "llm_down_since", "source": "health", "path": "stall_marks.llm_down_since",
             "type": "number", "nullable": True},
            {"name": "semantic_due_since", "source": "health", "path": "stall_marks.semantic_due_since",
             "type": "number", "nullable": True},
            {"name": "collection", "source": "health", "path": "collection", "type": "code"},
            {"name": "missing", "source": "health", "path": "nope.deeper", "type": "int"},
            {"name": "card_workers", "source": "workers", "count_by": "reason", "type": "counts"}]}


def _root(tmp_path, health=HEALTH):
    (tmp_path / "health.json").write_text(json.dumps(health))
    workers = tmp_path / "worker_status"
    workers.mkdir()
    for i, reason in enumerate(["worker_started", "profile_mismatch", "profile_mismatch"]):
        (workers / f"slack-{i}.json").write_text(json.dumps({"reason": reason}))
    return tmp_path


def test_snapshot_keeps_typed_values_and_reports_unknown_by_name(tmp_path):
    out = status_snapshot.snapshot(str(_root(tmp_path)), SPEC, now=10.0)
    assert out["schema"] == "mcs-status/1" and out["generated_at"] == 10.0
    assert out["fields"] == {
        "overall": "ok", "state_reasons": ["notification_pending"], "run_id": 7,
        "run_elapsed_s": 12.5, "notify_pending": 2, "llm_down_since": None,
        "card_workers": {"worker_started": 1, "profile_mismatch": 2}}
    # absent key, free text and a wrong type are unknown, never 0 or a value
    assert out["unavailable"] == ["semantic_due_since", "collection", "missing"]
    assert "合成" not in json.dumps(out, ensure_ascii=False)


def test_missing_or_symlinked_source_is_unknown_not_zero(tmp_path):
    (tmp_path / "real.json").write_text(json.dumps(HEALTH))
    os.symlink(tmp_path / "real.json", tmp_path / "health.json")
    out = status_snapshot.snapshot(str(tmp_path), SPEC)
    assert out["fields"] == {}
    assert len(out["unavailable"]) == len(SPEC["fields"])


@pytest.mark.parametrize("change", [
    {"sources": {"health": {"kind": "json", "path": "/etc/passwd"}}},
    {"sources": {"health": {"kind": "json", "path": "../config.json"}}},
    {"fields": [{"name": "x", "source": "health", "path": "a", "type": "text"}]},
    {"fields": [{"name": "x", "source": "other", "path": "a", "type": "int"}]},
    {"fields": [{"name": "dup", "source": "health", "type": "int"},
                {"name": "dup", "source": "health", "type": "int"}]},
])
def test_field_list_rejects_paths_outside_data_and_unknown_types(tmp_path, change):
    path = tmp_path / "fields.json"
    path.write_text(json.dumps({**SPEC, **change}))
    with pytest.raises(ValueError, match="status_fields_invalid"):
        status_snapshot.load_fields(str(path))


def test_shipped_field_list_is_valid_and_reads_only_data_files():
    spec = status_snapshot.load_fields()
    assert {s.get("path") for s in spec["sources"].values()} == {"health.json", "worker_status", None}
    assert len({f["name"] for f in spec["fields"]}) == len(spec["fields"])


def test_cli_writes_an_owner_only_file(tmp_path, monkeypatch):
    monkeypatch.setattr(status_snapshot.mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(status_snapshot, "host_status", lambda root: {})   # no real OS/LLM probe
    _root(tmp_path / "data" if (tmp_path / "data").mkdir() is None else None)
    out = tmp_path / "status.json"
    assert status_snapshot.main(["--out", str(out)]) == 0
    assert out.stat().st_mode & 0o777 == 0o600
    body = json.loads(out.read_text())
    assert body["fields"]["overall"] == "ok"


def test_host_status_reads_only_numbers_and_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(status_snapshot.mcs_util, "load_config", lambda: {})
    replies = {
        ("/usr/sbin/sysctl", "-n", "kern.boottime"): "{ sec = 100, usec = 0 } Thu",
        ("/usr/sbin/sysctl", "-n", "vm.swapusage"): "total = 2048.00M  used = 512.50M  free = 1.0M",
        ("/usr/sbin/sysctl", "-n", "hw.memsize"): "17179869184\n",
        ("/usr/sbin/sysctl", "-n", "kern.memorystatus_level"): "40\n",
        ("/sbin/route", "-n", "get", "default"): "   gateway: 192.0.2.1\n  interface: en9\n",
        ("/usr/sbin/netstat", "-ibn"): (
            "Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll\n"
            "en9 1500 <Link#9> 00:00:00:00:00:00 10 1 2000 20 0 4000 0\n"),
        ("/bin/launchctl", "list"): ("PID\tStatus\tLabel\n12\t0\tai.mcs.extract-drainer\n"
                                    "-\t3\tlocal.mcs-int\n-\t0\tlocal.mcs-cmd\n-\t1\tcom.other.job\n"),
    }
    monkeypatch.setattr(status_snapshot, "_run", lambda argv: replies.get(tuple(argv)))
    monkeypatch.setattr(status_snapshot, "_llm_get", lambda path, **kwargs: (
        {"status": "ok"} if path == "/health"
        else [{"is_processing": True}, {"is_processing": False}]))
    monkeypatch.setattr(status_snapshot.time, "time", lambda: 1100.0)
    host = status_snapshot.host_status(str(tmp_path))
    assert host["memory"] == {"total_bytes": 17179869184, "free_pct": 40,
                              "swap_total_mb": 2048.0, "swap_used_mb": 512.5}
    assert host["uptime_s"] == 1000
    assert host["llm"] == {"health": "ok", "slots_total": 2, "slots_busy": 1}
    assert host["network"] == {"default_route": True, "interface": "en9", "rx_packets": 10,
                               "rx_errors": 1, "rx_bytes": 2000, "tx_packets": 20,
                               "tx_errors": 0, "tx_bytes": 4000}
    assert host["jobs"] == {"running": 1, "nonzero_exit": ["local.mcs-int"]}
    # the gateway address never leaves
    assert "192.0.2.1" not in json.dumps(host)


def test_host_status_marks_unreachable_llm_and_unknown_values(tmp_path, monkeypatch):
    monkeypatch.setattr(status_snapshot.mcs_util, "load_config", lambda: {})
    monkeypatch.setattr(status_snapshot, "_run", lambda argv: None)
    monkeypatch.setattr(status_snapshot, "_llm_get", lambda path, **kwargs: None)
    host = status_snapshot.host_status(str(tmp_path))
    assert host["llm"] == {"health": None, "slots_total": None, "slots_busy": None}
    assert host["memory"]["free_pct"] is None and host["uptime_s"] is None
    assert host["network"] == {"default_route": None, "interface": None}
    assert host["jobs"] is None


def test_empty_worker_directory_is_observed_zero(tmp_path):
    (tmp_path / "worker_status").mkdir()
    result = status_snapshot.snapshot(str(tmp_path), SPEC)
    assert result["fields"]["card_workers"] == {}
    assert "card_workers" not in result["unavailable"]


@pytest.mark.parametrize("bad", ["{", "[]", "{}", '{"reason":"合成の自由文"}',
                                  '{"reason":null}'])
def test_incomplete_worker_record_is_unknown_without_leaking_content(tmp_path, bad):
    workers = tmp_path / "worker_status"
    workers.mkdir()
    (workers / "ok.json").write_text('{"reason":"worker_started"}')
    (workers / "bad.json").write_text(bad)
    result = status_snapshot.snapshot(str(tmp_path), SPEC)
    assert "card_workers" not in result["fields"]
    assert "card_workers" in result["unavailable"]
    assert "合成の自由文" not in json.dumps(result, ensure_ascii=False)


def test_worker_directory_over_limit_is_unknown(tmp_path):
    workers = tmp_path / "worker_status"
    workers.mkdir()
    for index in range(status_snapshot.MAX_DIR_FILES + 1):
        (workers / f"worker-{index}.json").write_text('{"reason":"worker_started"}')
    result = status_snapshot.snapshot(str(tmp_path), SPEC)
    assert "card_workers" not in result["fields"]
    assert "card_workers" in result["unavailable"]
    (workers / f"worker-{status_snapshot.MAX_DIR_FILES}.json").unlink()
    result = status_snapshot.snapshot(str(tmp_path), SPEC)
    assert result["fields"]["card_workers"] == {"worker_started": status_snapshot.MAX_DIR_FILES}


def test_unreadable_worker_directory_or_symlinked_record_is_unknown(tmp_path, monkeypatch):
    workers = tmp_path / "worker_status"
    workers.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"reason":"worker_started"}')
    (workers / "link.json").symlink_to(outside)
    result = status_snapshot.snapshot(str(tmp_path), SPEC)
    assert "card_workers" in result["unavailable"]
    monkeypatch.setattr(status_snapshot.os, "scandir", lambda path: (_ for _ in ()).throw(PermissionError()))
    result = status_snapshot.snapshot(str(tmp_path), SPEC)
    assert "card_workers" in result["unavailable"]


def test_host_failures_are_unavailable_while_valid_empty_observations_are_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(status_snapshot.mcs_util, "load_config", lambda: {})
    spec = status_snapshot.load_fields()
    names = {"jobs_running", "jobs_nonzero_exit", "net_default_route", "llm_health",
             "llm_slots_total", "llm_slots_busy"}
    spec["fields"] = [field for field in spec["fields"] if field["name"] in names]
    monkeypatch.setattr(status_snapshot, "_run", lambda argv: None)
    monkeypatch.setattr(status_snapshot, "_llm_get", lambda *args, **kwargs: None)
    missing = status_snapshot.snapshot(str(tmp_path), spec)
    assert set(missing["unavailable"]) == names
    assert not missing["fields"]

    def run(argv):
        if argv == ["/bin/launchctl", "list"]:
            return "PID\tStatus\tLabel\n"
        if argv == ["/sbin/route", "-n", "get", "default"]:
            return "interface: en9\n"
        return None

    monkeypatch.setattr(status_snapshot, "_run", run)
    monkeypatch.setattr(status_snapshot, "_llm_get", lambda path, **kwargs:
                        {"status": "ok"} if path == "/health" else [])
    observed = status_snapshot.snapshot(str(tmp_path), spec)
    assert observed["fields"] == {"jobs_running": 0, "jobs_nonzero_exit": [],
                                  "net_default_route": True, "llm_health": "ok",
                                  "llm_slots_total": 0, "llm_slots_busy": 0}
    assert observed["unavailable"] == []


@pytest.mark.parametrize("slots,total", [([None], None), ([{}], 1),
                                          ([{"is_processing": "false"}], 1)])
def test_invalid_slots_never_understate_busy_count(tmp_path, monkeypatch, slots, total):
    monkeypatch.setattr(status_snapshot.mcs_util, "load_config", lambda: {})
    monkeypatch.setattr(status_snapshot, "_run", lambda argv: None)
    monkeypatch.setattr(status_snapshot, "_llm_get", lambda path, **kwargs:
                        {"status": "ok"} if path == "/health" else slots)
    llm = status_snapshot.host_status(str(tmp_path))["llm"]
    assert llm["slots_total"] == total and llm["slots_busy"] is None


@pytest.mark.parametrize("output", ["", "broken", "PID\tStatus\tLabel\nbroken\n",
                                    "PID\tStatus\tLabel\n?\t0\tai.mcs.extract\n"])
def test_invalid_job_list_is_unknown(monkeypatch, output):
    monkeypatch.setattr(status_snapshot, "_run", lambda argv: output)
    assert status_snapshot._jobs() is None


def test_host_probes_the_configured_llm_port(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace

    monkeypatch.setattr(status_snapshot, "_run", lambda argv: None)
    monkeypatch.setattr(status_snapshot.mcs_util, "load_config", lambda: {
        "local_llm": {"url": "http://127.0.0.1:18080/v1/chat/completions"}})
    seen = []

    def open_url(url, *, timeout):
        seen.append(url)
        raw = b'[{"is_processing":true}]' if url.endswith('/slots') else b'{"status":"ok"}'
        response = io.BytesIO(raw)
        response.status = 200
        return response

    monkeypatch.setattr(status_snapshot.urllib.request, "build_opener",
                        lambda *args: SimpleNamespace(open=open_url))
    host = status_snapshot.host_status(str(tmp_path))
    assert seen == ["http://127.0.0.1:18080/slots", "http://127.0.0.1:18080/health"]
    assert host["llm"] == {"health": "ok", "slots_total": 1, "slots_busy": 1}


@pytest.mark.parametrize("endpoint", [
    "https://example.org/v1/chat/completions", "http://127.0.0.1:bad/v1/chat/completions",
    "http://user:secret@127.0.0.1:18080/v1/chat/completions",
])
def test_llm_probe_refuses_unsafe_configuration_before_io(monkeypatch, endpoint):
    monkeypatch.setattr(status_snapshot.urllib.request, "build_opener",
                        lambda *args: pytest.fail("unsafe endpoint reached HTTP"))
    assert status_snapshot._llm_get("/health", endpoint=endpoint) is None


def test_oversized_llm_response_is_not_a_valid_truncated_observation(monkeypatch):
    import io
    from types import SimpleNamespace

    def open_url(url, *, timeout):
        response = io.BytesIO(b'{"status":"ok"}' + b" " * 65536)
        response.status = 200
        return response

    monkeypatch.setattr(status_snapshot.urllib.request, "build_opener",
                        lambda *args: SimpleNamespace(open=open_url))
    assert status_snapshot._llm_get("/health", endpoint="http://127.0.0.1:18080/") is None


@pytest.mark.parametrize("overshoot", [0, 1.125])
def test_shipped_snapshot_contract_preserves_run_overshoot(tmp_path, overshoot):
    spec = status_snapshot.load_fields()
    spec["fields"] = [field for field in spec["fields"] if field["name"] == "run_overshoot_s"]
    (tmp_path / "health.json").write_text(json.dumps({"run": {"overshoot_s": overshoot}}))
    out = status_snapshot.snapshot(str(tmp_path), spec)
    assert out["fields"] == {"run_overshoot_s": overshoot}
    assert out["unavailable"] == []
