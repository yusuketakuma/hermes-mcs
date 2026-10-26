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
    for i, reason in enumerate(["worker_started", "profile_mismatch", "profile_mismatch",
                                "合成の自由文"]):
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
    monkeypatch.setattr(status_snapshot, "_run", lambda argv: None)
    monkeypatch.setattr(status_snapshot, "_llm_get", lambda path, **kwargs: None)
    host = status_snapshot.host_status(str(tmp_path))
    assert host["llm"] == {"health": "down", "slots_total": None, "slots_busy": None}
    assert host["memory"]["free_pct"] is None and host["uptime_s"] is None
    assert host["network"] == {"default_route": False, "interface": None}


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


@pytest.mark.parametrize("overshoot", [0, 1.125])
def test_shipped_snapshot_contract_preserves_run_overshoot(tmp_path, overshoot):
    spec = status_snapshot.load_fields()
    spec["fields"] = [field for field in spec["fields"] if field["name"] == "run_overshoot_s"]
    (tmp_path / "health.json").write_text(json.dumps({"run": {"overshoot_s": overshoot}}))
    out = status_snapshot.snapshot(str(tmp_path), spec)
    assert out["fields"] == {"run_overshoot_s": overshoot}
    assert out["unavailable"] == []
