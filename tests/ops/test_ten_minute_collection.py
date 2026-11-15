"""Existing services and the standalone host share ten-minute fetch slots."""
import service_management
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
    actual = {row["script"]: row for row in service_management.inspect_cron('/x/hermes')}
    assert actual["mcs_check.sh"]["schedule"] == "*/10 * * * *"
    assert actual["mcs_deep.sh"]["schedule"] == "10,40 * * * *"
    assert actual["mcs_check.sh"]["id"] == "000001"
    assert actual["mcs_deep.sh"]["id"] == "000002"
    mutations = [call for call in calls if call[1:3] in (
        ["cron", "create"], ["cron", "edit"], ["cron", "remove"])]
    assert mcs_setup.cmd_services(args) == 0
    assert [call for call in calls if call[1:3] in (
        ["cron", "create"], ["cron", "edit"], ["cron", "remove"])] == mutations
    rows = service_management.inspect_cron('/x/hermes')
    assert len(rows) == len({row['script'] for row in rows}) == 6
    assert {row['script'] for row in rows} == {
        'mcs_check.sh', 'mcs_deep.sh', 'mcs_ocr.sh',
        'mcs_llm_catchup.sh', 'llamacpp_restart_if_idle.sh', 'mcs_update.sh'}
    assert actual['mcs_ocr.sh']['schedule'] == '5,15,25,35,45,55 * * * *'


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
    # OCR sits between unread checks; the health watch is the separate agent only.
    assert [minute for job, minute in started if job == "mcs_ocr"] == [5, 15, 25, 35, 45, 55]
    assert not any(job == "mcs_health" for job, _ in started)


def test_services_retire_cron_health_watch_and_per_minute_ocr(tmp_path, monkeypatch):
    """An install from before the single-slot load fix owns a cron health
    watch and a per-minute OCR job: services removes the cron copy, moves
    OCR to its 10-minute grid, and keeps local.mcs-health as the watcher."""
    entries = [
        {"id": "000003", "name": "MCS health watch", "script": "mcs_health.sh",
         "schedule": "*/5 * * * *"},
        {"id": "000004", "name": "MCS attachment OCR", "script": "mcs_ocr.sh",
         "schedule": "* * * * *"},
    ]
    calls, args = _services_env(monkeypatch, tmp_path, cron_entries=entries)
    path, manifest = _seed_manifest(tmp_path)
    manifest["cron"] += [{"id": "000003", "script": "mcs_health.sh"},
                         {"id": "000004", "script": "mcs_ocr.sh"}]
    path.write_text(json.dumps(manifest))
    assert mcs_setup.cmd_services(args) == 0
    assert ["/x/hermes", "cron", "remove", "000003"] in calls
    actual = {row["script"]: row for row in service_management.inspect_cron('/x/hermes')}
    assert "mcs_health.sh" not in actual
    assert actual["mcs_ocr.sh"]["id"] == "000004"
    assert actual["mcs_ocr.sh"]["schedule"] == "5,15,25,35,45,55 * * * *"
    assert any(call[:2] == ["launchctl", "bootstrap"] and call[3].endswith("/local.mcs-health.plist")
               for call in calls)
    kept = json.loads(path.read_text())
    assert "mcs_health.sh" not in {row.get("script") for row in kept["cron"]}
    assert "local.mcs-health" in {row.get("label") for row in kept["agents"]}
    # still owned, so a runtime switch also retires an older install's cron copy
    assert "mcs_health.sh" in service_management._known_cron_scripts()
