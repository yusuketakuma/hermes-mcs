"""Config rewrites read config.json the way load_config does: a file that
load_config would read as {} (symlink, non-regular, oversized) is never
treated as an empty config and overwritten. Synthetic temp files only."""
import json
import os
from types import SimpleNamespace

import pytest

import mcs_setup
import mcs_util

TARGET = {"mcs_login_id": "synthetic-user", "notify": {"target": "slack:CSYNTHETIC"}}


def _unreadable_config(tmp_path, kind):
    conf = tmp_path / "c.json"
    if kind == "symlink":
        (tmp_path / "real.json").write_text(json.dumps(TARGET))
        conf.symlink_to(tmp_path / "real.json")
    elif kind == "oversized":
        conf.write_text(json.dumps({"pad": "x" * mcs_util.CONFIG_MAX_BYTES}))
    else:
        os.mkfifo(conf)
    return conf


def _snapshot(conf):
    if conf.is_symlink():
        return ("link", os.readlink(conf), (conf.parent / "real.json").read_bytes())
    if conf.is_fifo():
        return ("fifo",)
    return ("file", conf.read_bytes())


@pytest.mark.parametrize("kind", ["symlink", "oversized", "fifo"])
@pytest.mark.parametrize("command", ["init", "fact-source"])
def test_rewrite_refuses_config_that_load_config_reads_as_empty(
        monkeypatch, tmp_path, capsys, kind, command):
    conf = _unreadable_config(tmp_path, kind)
    before = _snapshot(conf)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    if command == "init":
        assert mcs_setup.cmd_init(SimpleNamespace(yes=False)) == 1
    else:
        assert mcs_setup.cmd_fact_source(
            SimpleNamespace(fact_source="shadow", gate_evidence=None)) == 1
    assert _snapshot(conf) == before
    assert "保存していません" in capsys.readouterr().out
    assert not list(tmp_path.glob("c.json.corrupt-*"))


def test_yes_moves_a_symlink_aside_without_touching_its_target(monkeypatch, tmp_path):
    conf = _unreadable_config(tmp_path, "symlink")
    real = tmp_path / "real.json"
    real.chmod(0o644)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    assert mcs_setup._guard_config(True) is True
    (moved,) = tmp_path.glob("c.json.corrupt-*")
    assert moved.is_symlink() and not conf.exists()
    assert json.loads(real.read_text()) == TARGET
    assert real.stat().st_mode & 0o777 == 0o644


def test_regular_config_within_the_limit_is_still_accepted(monkeypatch, tmp_path):
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps(TARGET))
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    assert mcs_setup._config_problem() is None
