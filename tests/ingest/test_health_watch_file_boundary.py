"""Health evidence is a bounded regular file, never a blocking pipe or link."""
import health_report
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import health_watch
import safe_io


def _read_in_child(reader, path, root):
    program = """
import json, sys
sys.path.insert(0, sys.argv[1])
import _mcs_path
import health_watch, health_report
reader, path, root = sys.argv[2:]
if reader == 'health':
    result = health_watch.classify_health(path, 1000, 900)
elif reader == 'standalone':
    result = health_watch._read_standalone_status(root)
else:
    health_report.HEALTH_FILE = path
    result = health_report.prev_health()
print(json.dumps(result))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", program,
         str(Path(health_watch.__file__).parents[1]), reader, str(path), str(root)],
        capture_output=True, text=True, timeout=5, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("reader", ["health", "standalone", "previous"])
@pytest.mark.parametrize("kind", ["symlink", "dangling_symlink", "oversize", "fifo", "directory"])
def test_health_evidence_refuses_non_regular_or_oversize_files(tmp_path, monkeypatch, reader, kind):
    data = tmp_path / "data"
    data.mkdir()
    path = data / ("health.json" if reader == "health" else "standalone-status.json")
    payload = json.dumps({"overall": "ok", "at": 900, "updated_at": 900})
    if kind in ("symlink", "dangling_symlink"):
        target = tmp_path / "elsewhere.json"
        if kind == "symlink":
            target.write_text(payload)
        path.symlink_to(target)
    elif kind == "oversize":
        path.write_text(payload + " " * (1024 * 1024))
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    if kind == "fifo":
        result = _read_in_child(reader, path, tmp_path)
        assert result == ({"status": "corrupt"} if reader == "health"
                          else None if reader == "standalone" else {})
        return
    if reader == "health":
        assert health_watch.classify_health(str(path), 1000, 900)["status"] == "corrupt"
    elif reader == "standalone":
        assert health_watch._read_standalone_status(str(tmp_path)) is None
    else:
        monkeypatch.setattr(health_report, 'HEALTH_FILE', str(path))
        assert health_report.prev_health() == {}


@pytest.mark.parametrize("failure", [PermissionError, OSError])
def test_unreadable_health_is_not_reported_as_missing(monkeypatch, failure):
    def unreadable(*args, **kwargs):
        raise failure("synthetic I/O failure")

    monkeypatch.setattr(safe_io, "read_json_nofollow", unreadable)
    assert health_watch.classify_health("synthetic.json", 1000, 900) == {"status": "corrupt"}


@pytest.mark.parametrize("reader", ["health", "standalone", "previous"])
def test_health_evidence_rechecks_bound_after_read(tmp_path, monkeypatch, reader):
    monkeypatch.setattr(safe_io, "read_regular_nofollow",
                        lambda *a, **kw: b"{}" + b" " * (1024 * 1024))
    if reader == "health":
        assert health_watch.classify_health("synthetic.json", 1000, 900)["status"] == "corrupt"
    elif reader == "standalone":
        assert health_watch._read_standalone_status(str(tmp_path)) is None
    else:
        monkeypatch.setattr(health_report, 'HEALTH_FILE', str(tmp_path / "synthetic.json"))
        assert health_report.prev_health() == {}
