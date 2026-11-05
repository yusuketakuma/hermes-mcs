"""Released DDL/runtime classes reach the real tick and snapshot view offline."""
import extract_llm_runner
import collection_stages
from contextlib import closing
import json
import sys

import pytest

import extract_llm
import job_ops
import ledger
import maintenance
import mcs_adapter
import mcs_view
import notify_flush
import run_check
import health_report
from test_supported_schema_upgrade import (
    FIXTURES, RELEASES, _historical, _preserved, _records,
)


ORIGINS = {origin["release"]: origin for origin in RELEASES}
PATHS = json.loads((FIXTURES / "update-paths.json").read_text())["current_paths"]
GROUPS = {}
for path in PATHS:
    origin = ORIGINS[path["source"]]
    # Equal original DDL + schema use the same current migration; runtime stays separate.
    key = (origin["fixture_sha256"], origin["schema_version"], path["runtime"])
    GROUPS.setdefault(key, []).append(path["source"])
CASES = [
    pytest.param(ORIGINS[sources[0]], runtime,
                 id=f"{ORIGINS[sources[0]]['fixture']}-{runtime}-{'_'.join(sources)}")
    for (_, _, runtime), sources in GROUPS.items()
] + [pytest.param(None, runtime, id=f"clean-{runtime}")
     for runtime in ("hermes", "standalone")]
BODY = "病名: 架空導入確認病\nADL: 歩行は介助"


@pytest.mark.parametrize("origin,runtime", CASES)
def test_origin_migration_tick_extract_snapshot_view(tmp_path, monkeypatch, capsys,
                                                   origin, runtime):
    root = tmp_path / "home"
    data = root / "data"
    expected = None
    if origin:
        db_path = _historical(data, origin)
        expected = _records(db_path)
    else:
        data.mkdir(parents=True)
        db_path = data / "ledger.db"
    with closing(ledger.Ledger(str(db_path))) as db:
        assert db.db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
    if expected:
        _preserved(db_path, expected)
    with closing(ledger.Ledger(str(db_path))) as db:
        assert db.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, extend_session=True):
            if path == "/users/self":
                return {"user": {"id": 900, "last_name": "架空", "first_name": "導入担当",
                                 "stations": []}}
            if path == "/projects":
                return {"projects": [{"id": 303, "is_unread": True,
                                      "karte": {"id": 5}}],
                        "paginate": {"has_next": False, "timestamp": 123}}
            if path == "/kartes/5/memo_summary":
                return {"memo_summary": {"is_editable": True, "is_read": True,
                                         "read_style": "single_line"}}
            if path in ("/projects/101", "/projects/102", "/projects/303"):
                return {"project": {"id": int(path.rsplit("/", 1)[1]),
                                    "is_archived": False}}
            if path.endswith("/messages"):
                assert path.startswith("/projects/")
                messages = ([{"id": 999, "comment": BODY,
                              "created_at": "1970-01-01T00:02:50+00:00"}]
                            if path == "/projects/303/messages" else [])
                return {"messages": messages, "paginate": {"has_next": False}}
            pytest.fail(f"unexpected synthetic adapter path: {path}")

        def _request(self, *args, **kwargs):
            pytest.fail("external request forbidden")

    config = root / "config.json"
    config.write_text(json.dumps({"runtime_mode": runtime, "deep_history": False,
                                  "notify_target": "local", "semantic": {"mode": "off"}}))
    for name, value in {
        "HOME": root, "DB": db_path, "ATTACH_DIR": data / "attachments",
        "LOCKFILE": data / "run.lock", "CONF_PATH": config,
        "CACHE": root / "absent-token.json",
    }.items():
        monkeypatch.setattr(
            collection_stages if name in {"ATTACH_DIR", "with_relogin"} else (run_check),
            name, str(value))
    monkeypatch.setattr(health_report, "HEALTH_FILE", str(root / health_report.HEALTH_REL))
    for name, value in {
        "HOME": root, "DB_MAINT_STATE": data / "db_maintenance.json",
        "BACKUP_DIR": data / "backups", "SNAPSHOT_DIR": data / "snapshots",
        "LOGFILE": data / "run.log",
    }.items():
        monkeypatch.setattr(maintenance, name, str(value))
    drain = job_ops.drain_commands
    monkeypatch.setattr(job_ops, "drain_commands", lambda db, result:
                        drain(db, result, str(data / "cmd")))
    monkeypatch.setattr(run_check, "MCSAdapter", Adapter)
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: None)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **kwargs: False)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
    monkeypatch.setattr(notify_flush, "flush", lambda *args, **kwargs:
                        pytest.fail("external notification forbidden"))
    monkeypatch.setenv("MCS_DISK_GUARD_MB", "0")
    # Historical seed timestamps are 100..200; tick before their retention expires.
    monkeypatch.setattr(run_check.time, "time", lambda: 170)
    monkeypatch.setattr(sys, "argv", ["run_check", "--no-notify", "--no-backfill"])

    assert run_check.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["errors"] == [] and result["incomplete"] == []
    assert result["new_messages"] == 1 and result["extracted"] >= 1
    assert result["snapshot"]
    with closing(ledger.LedgerReader(str(db_path))) as db:
        artifact = db.artifacts("extract_v1", message_id=999)[0]
        context = json.loads(artifact["content"])["patient_context"]
        assert any(item["text"] == "架空導入確認病" for item in context)
        if expected:
            for table in ("messages", "artifacts", "requests", "command_receipts", "attachments"):
                columns, rows = expected[table]
                selected = {tuple(row) for row in db.db.execute(
                    f'SELECT {",".join(columns)} FROM "{table}"')}
                assert all(tuple(row) in selected for row in rows), table
    with closing(mcs_view.View(str(data / "snapshots" / "ledger-snapshot.db"))) as view:
        evidence = view.read("evidence", project=303, message_id=999)["message"]
        assert evidence["body_text"] == BODY and evidence["body_complete"]
        assert any(item["text"] == "架空導入確認病" for item in evidence["patient_context"])
        record = view.read("read_model", project=303, scope="detail")["records"][0]
        assert record["extraction"]["extract_v1"]["state"] == "current"
        assert record["extraction"]["extract_v1"]["artifact_id"] == artifact["artifact_id"]

    assert run_check.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["errors"] == [] and result["new_messages"] == 0
    with closing(ledger.LedgerReader(str(db_path))) as db:
        assert db.db.execute("SELECT count(*) FROM messages WHERE message_id=999").fetchone()[0] == 1
        assert len(db.artifacts("extract_v1", message_id=999)) == 1
