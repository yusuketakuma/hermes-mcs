"""IA-085: a terminal can queue the bound DB-restore approval like /mcs does."""
import json
from types import SimpleNamespace

import mcs_requests
import mcs_util
import update_cli


def _args(**overrides):
    values = {"report_id": "a" * 64, "backup_sha256": "b" * 64, "backup_schema": 12,
              "confirm_human": True, "reason": "synthetic: loss report reviewed",
              "actor": "synthetic-human", "command_id": "00000000-0000-4000-8000-000000000085"}
    return SimpleNamespace(**{**values, **overrides})


def test_restore_approve_queues_the_bound_command(tmp_path, monkeypatch):
    (tmp_path / "data" / "cmd").mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    assert update_cli.cmd_restore_approve(_args()) == 0
    queued = [path for path in (tmp_path / "data" / "cmd").iterdir() if path.suffix == ".json"]
    assert len(queued) == 1
    req = json.loads(queued[0].read_text())
    assert req["cmd"] == "ops.restore_approve" and req["backup_schema"] == 12
    assert mcs_requests.validate(req) is None


def test_restore_approve_requires_a_human_and_valid_binding(tmp_path, monkeypatch):
    (tmp_path / "data" / "cmd").mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    assert update_cli.cmd_restore_approve(_args(confirm_human=False)) == 1
    assert update_cli.cmd_restore_approve(_args(report_id="not-a-hash")) == 1
    assert not list((tmp_path / "data" / "cmd").iterdir())
