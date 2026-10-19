"""`mcs_standalone check` must reject a backup block the host scheduler
rejects, with the same fixed code `run` reports, instead of reporting OK."""
import json

import pytest

from mcs_standalone import __main__ as cli
from mcs_standalone import runtime

from standalone_testkit import write_root


def _root(tmp_path):
    cfg = write_root(tmp_path, interactive="off", target="local")
    cfg["backup"] = {"enabled": True, "schedule": "*/5 * * * *"}   # not one explicit minute
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return cfg


def test_runtime_rejects_the_backup_block(tmp_path):
    cfg = _root(tmp_path)
    with pytest.raises(ValueError, match="backup_config_invalid"):
        runtime.Runtime(tmp_path, cfg)


def test_check_fails_like_run_on_invalid_backup(tmp_path, monkeypatch, capsys):
    _root(tmp_path)
    monkeypatch.setattr(cli, "_sdk_problem", lambda: None)
    run_code = cli.main(["run", "--root", str(tmp_path)])
    run_err = capsys.readouterr().err
    assert run_code == 1
    # run fails; check must fail the same way (with a fixed code), not report OK
    assert cli.main(["check", "--root", str(tmp_path)]) == 1
    assert "backup_config_invalid" in run_err
