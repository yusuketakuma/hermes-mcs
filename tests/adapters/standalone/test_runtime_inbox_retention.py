"""Completed command inboxes must release filenames without losing backoff semantics."""
from pathlib import Path
from types import SimpleNamespace

from mcs_standalone import runtime


def test_successful_empty_inbox_releases_names_and_new_click_skips_backoff(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    host.last_minute = 0
    pending = {f"synthetic-{index}.json" for index in range(5000)}
    host.children["cmd_int"] = {
        "process": SimpleNamespace(pid=1001, poll=lambda: 0),
        "kind": "core", "stopping_at": None, "pending": pending,
    }
    names = []
    monkeypatch.setattr(Path, "glob", lambda *args: names)
    started = []
    monkeypatch.setattr(host, "start", lambda job, *args: started.append(job))
    host.tick(1)
    assert host.retry_at["cmd_int"] == 11
    retained = sum(len(value) for value in host.drained.values() if value is not None)
    print(f"synthetic_completed_inbox_names_retained={retained}")
    assert retained == 0
    assert host.drained["cmd_int"] == set()  # successful drain remains distinguishable
    names.append(SimpleNamespace(name="synthetic-new-click.json"))
    host.tick(2)
    assert "cmd_int" in started
    assert "cmd_int" not in host.retry_at


def test_empty_inbox_does_not_drop_failed_child_backoff(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    host.last_minute = 0
    host.children["cmd_int"] = {
        "process": SimpleNamespace(pid=1001, poll=lambda: 1),
        "kind": "core", "stopping_at": None, "pending": {"synthetic-failed.json"},
    }
    names = []
    monkeypatch.setattr(Path, "glob", lambda *args: names)
    monkeypatch.setattr(host, "start", lambda *args: None)
    host.tick(1)
    assert host.drained["cmd_int"] is None
    names.append(SimpleNamespace(name="synthetic-new-click.json"))
    host.tick(2)
    assert host.retry_at["cmd_int"] == 31
