"""Private result publication persists directory entries and reports partial failures."""
import errno
import json
import os
import stat

import pytest

import import_drug_master as importer
import mcs_drug
import mcs_repair
from test_import_drug_master import _input


@pytest.mark.parametrize("writer", ["master", "repair"])
def test_new_private_output_syncs_parent_after_complete_publication(tmp_path, monkeypatch, writer):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    destination = private / "result.json"
    observed = []
    original = os.fsync

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            observed.append(json.loads(destination.read_bytes()))
            assert sorted(path.name for path in private.iterdir()) == [destination.name]
        return original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    if writer == "master":
        source, pin = _input(tmp_path)
        importer.write_private(importer.convert(source, pin), destination)
    else:
        mcs_repair.save_new(destination, {"synthetic": True})
    assert len(observed) == 1


@pytest.mark.parametrize("writer", ["master", "repair"])
def test_directory_sync_failure_preserves_output_and_reports_publication(
        tmp_path, monkeypatch, capsys, writer):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    destination = private / "result.json"
    original = os.fsync

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "synthetic directory sync failure")
        return original(fd)

    if writer == "master":
        source, pin = _input(tmp_path)
        pin_path = tmp_path / "pin.json"
        pin_path.write_text(json.dumps(pin))
        main = importer.main
        argv = ["--source", str(source), "--pin", str(pin_path), "--output", str(destination)]
    else:
        monkeypatch.setattr(mcs_repair, "plan", lambda **kwargs: {"synthetic": True})
        main = mcs_repair.main
        argv = ["plan", "--snapshot", "synthetic-unused", "--out", str(destination)]
    monkeypatch.setattr(os, "fsync", sync)
    assert main(argv) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["written"] is True
    assert "sync_unconfirmed" in report.get("reason", report.get("error", ""))
    before = destination.read_bytes()
    assert json.loads(before)
    assert sorted(path.name for path in private.iterdir()) == [destination.name]
    monkeypatch.setattr(os, "fsync", original)
    assert main(argv) == 1  # exclusive publication never replaces the existing output
    assert destination.read_bytes() == before


def test_bundled_cli_retains_written_state_after_publication_failure(monkeypatch, capsys):
    def failed(*args, **kwargs):
        raise importer.MasterError("output_published_sync_unconfirmed", written=True)

    monkeypatch.setattr(mcs_drug, "install_bundled", failed)
    assert mcs_drug.main(["install-bundled", "--terms-checked-on", "2026-10-13",
                          "--confirmed-by", "synthetic", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "refused", "reason": "output_published_sync_unconfirmed", "written": True}
