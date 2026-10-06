"""Synthetic invalid results and interrupted writes leave operation retries usable."""
import json
from types import SimpleNamespace

import pytest

import mcs_cli
import mcs_repair


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_repair_result_does_not_publish_on_interrupted_flush(tmp_path, monkeypatch, failure):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "result.json"
    original_fsync = mcs_repair.os.fsync

    def fail(fd):
        raise failure("synthetic flush failure")

    monkeypatch.setattr(mcs_repair.os, "fsync", fail)
    with pytest.raises(failure):
        mcs_repair.save_new(target, {"synthetic": 1})
    assert not target.exists()
    assert not list(private.iterdir())
    monkeypatch.setattr(mcs_repair.os, "fsync", original_fsync)
    mcs_repair.save_new(target, {"synthetic": 1})
    assert json.loads(target.read_text()) == {"synthetic": 1}
    with pytest.raises(FileExistsError):
        mcs_repair.save_new(target, {"synthetic": 2})
    assert json.loads(target.read_text()) == {"synthetic": 1}


def test_repair_invalid_serialization_creates_no_output(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "result.json"
    with pytest.raises(ValueError):
        mcs_repair.save_new(target, {"synthetic": float("nan")})
    assert not target.exists()


@pytest.mark.parametrize("report", [[], None, "synthetic", 1])
def test_update_plan_nonobject_is_a_failure_result_not_an_exception(monkeypatch, capsys, report):
    monkeypatch.setenv("MCS_LIFECYCLE_PINNED", "1")
    monkeypatch.setattr(mcs_cli.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr=""))
    assert mcs_cli.main(["update", "plan", "--no-fetch"]) == 1
    assert "Update plan did not return JSON." in capsys.readouterr().err


def test_update_plan_deep_json_is_a_failure_result_not_an_exception(monkeypatch, capsys):
    monkeypatch.setenv("MCS_LIFECYCLE_PINNED", "1")
    body = "[" * 10_000 + "0" + "]" * 10_000
    monkeypatch.setattr(mcs_cli.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=body, stderr=""))
    assert mcs_cli.main(["update", "plan", "--no-fetch"]) == 1
    assert "Update plan did not return JSON." in capsys.readouterr().err
