"""Existing services and the standalone host share ten-minute fetch slots."""
import json
from types import SimpleNamespace

import mcs_setup
from mcs_standalone import runtime
from test_mcs_setup import _seed_manifest, _services_env


def test_services_migrate_existing_fetch_jobs_without_duplicates(tmp_path, monkeypatch):
    entries = [
        {"id": "000001", "name": "old unread", "script": "mcs_check.sh",
         "schedule": "*/5 * * * *"},
        {"id": "000002", "name": "old drain", "script": "mcs_deep.sh",
         "schedule": "7,37 * * * *"},
    ]
    calls, args = _services_env(monkeypatch, tmp_path, cron_entries=entries)
    path, manifest = _seed_manifest(tmp_path)
    manifest["cron"].append({"id": "000002", "script": "mcs_deep.sh"})
    path.write_text(json.dumps(manifest))
    assert mcs_setup.cmd_services(args) == 0
    actual = {row["script"]: row for row in mcs_setup._cron_list("/x/hermes")}
    assert actual["mcs_check.sh"]["schedule"] == "*/10 * * * *"
    assert actual["mcs_deep.sh"]["schedule"] == "10,40 * * * *"
    assert actual["mcs_check.sh"]["id"] == "000001"
    assert actual["mcs_deep.sh"]["id"] == "000002"
    mutations = [call for call in calls if call[1:3] in (
        ["cron", "create"], ["cron", "edit"], ["cron", "remove"])]
    assert mcs_setup.cmd_services(args) == 0
    assert [call for call in calls if call[1:3] in (
        ["cron", "create"], ["cron", "edit"], ["cron", "remove"])] == mutations
    assert len(mcs_setup._cron_list("/x/hermes")) == 6


def test_standalone_fetches_only_on_ten_minute_grid(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    started = []
    monkeypatch.setattr(host, "start", lambda job, kind, now, *args:
                        started.append((job, int(now // 60))))
    monkeypatch.setattr(runtime.time, "localtime", lambda now:
                        SimpleNamespace(tm_hour=12, tm_min=int(now // 60)))
    for minute in range(60):
        host.tick(minute * 60)
    assert [minute for job, minute in started if job == "mcs_check"] == [
        0, 10, 20, 30, 40, 50]
    assert [minute for job, minute in started if job == "mcs_deep"] == [10, 40]
