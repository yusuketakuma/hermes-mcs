"""Publication-boundary races preserve complete outputs and clean owned staging."""
import json

import pytest

import mcs_repair


def test_concurrent_result_creation_does_not_replace_other_writer(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    destination = private / "result.json"
    link = mcs_repair.os.link

    def concurrent_link(source, target, **kwargs):
        destination.write_text('{"synthetic":"other writer"}')
        return link(source, target, **kwargs)

    monkeypatch.setattr(mcs_repair.os, "link", concurrent_link)
    with pytest.raises(FileExistsError):
        mcs_repair.save_new(destination, {"synthetic": "this writer"})
    assert json.loads(destination.read_text()) == {"synthetic": "other writer"}
    assert sorted(path.name for path in private.iterdir()) == ["result.json"]


def test_interruption_after_link_keeps_complete_result_and_removes_staging(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    destination = private / "result.json"
    link = mcs_repair.os.link

    def interrupted_link(source, target, **kwargs):
        link(source, target, **kwargs)
        raise KeyboardInterrupt("synthetic after-publication interruption")

    monkeypatch.setattr(mcs_repair.os, "link", interrupted_link)
    with pytest.raises(KeyboardInterrupt):
        mcs_repair.save_new(destination, {"synthetic": "complete"})
    assert json.loads(destination.read_text()) == {"synthetic": "complete"}
    assert sorted(path.name for path in private.iterdir()) == ["result.json"]
