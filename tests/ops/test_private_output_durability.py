"""Private result publication persists directory entries and reports partial failures."""
import errno
import json
import os
import stat
from types import SimpleNamespace

import pytest

import import_drug_master as importer
import mcs_drug
import mcs_repair
from test_import_drug_master import _input
from test_mcs_drug_bundled import _bundle


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


@pytest.mark.parametrize("existing_depth", [0, 1, 2, 3])
def test_bundled_install_persists_each_new_directory_entry(tmp_path, monkeypatch, existing_depth):
    home = tmp_path / "home"
    chain = [home, home / "data", home / "data" / "drug-map"]
    for directory in chain[:existing_depth]:
        directory.mkdir(mode=0o700)
    synced = []
    from safe_io import fsync_dir

    def sync(directory):
        synced.append(directory)
        return fsync_dir(directory)

    monkeypatch.setattr(mcs_drug, "fsync_dir", sync)
    args = SimpleNamespace(terms_checked_on=importer.EDITION_DATE.isoformat(),
                           terms_record=None, confirmed_by="synthetic", approved_by=None)
    result = mcs_drug.install_bundled(args, directory=_bundle(tmp_path), home=str(home))
    assert result["state"] == "written"
    assert synced == [directory.parent for directory in chain[existing_depth:]]


@pytest.mark.parametrize("failed_level", [0, 1, 2])
def test_bundled_directory_creation_sync_failure_is_retryable(
        tmp_path, monkeypatch, capsys, failed_level):
    home = tmp_path / "home"
    chain = [home, home / "data", home / "data" / "drug-map"]
    from safe_io import fsync_dir

    def sync(directory):
        if directory == chain[failed_level].parent:
            raise OSError(errno.EIO, "synthetic new directory sync failure")
        return fsync_dir(directory)

    monkeypatch.setattr(mcs_drug, "fsync_dir", sync)
    monkeypatch.setattr(mcs_drug, "HOME", str(home))
    monkeypatch.setattr(mcs_drug, "BUNDLE", _bundle(tmp_path))
    argv = ["install-bundled", "--terms-checked-on", importer.EDITION_DATE.isoformat(),
            "--confirmed-by", "synthetic", "--json"]
    assert mcs_drug.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["written"] is False
    assert not chain[failed_level].exists()
    assert not list(home.rglob("*.json"))
    monkeypatch.setattr(mcs_drug, "fsync_dir", fsync_dir)
    assert mcs_drug.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "written"


def test_bundled_retry_after_unsynced_publication_syncs_before_present(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    folder = home / "data" / "drug-map"
    from safe_io import fsync_dir

    def failing(directory):
        if directory == folder:
            raise OSError(errno.EIO, "synthetic publication sync failure")
        return fsync_dir(directory)

    monkeypatch.setattr(mcs_drug, "HOME", str(home))
    monkeypatch.setattr(mcs_drug, "BUNDLE", _bundle(tmp_path))
    argv = ["install-bundled", "--terms-checked-on", importer.EDITION_DATE.isoformat(),
            "--confirmed-by", "synthetic", "--json"]
    monkeypatch.setattr(importer, "fsync_dir", failing)
    assert mcs_drug.main(argv) == 1
    first = json.loads(capsys.readouterr().out)
    assert first["reason"] == "output_published_sync_unconfirmed" and first["written"]
    # the same retry must not call the unsynced entry "present" without syncing it
    monkeypatch.setattr(mcs_drug, "fsync_dir", failing)
    assert mcs_drug.main(argv) == 1
    again = json.loads(capsys.readouterr().out)
    assert again["reason"] == "output_published_sync_unconfirmed" and again["written"]
    synced = []

    def recording(directory):
        synced.append(directory)
        return fsync_dir(directory)

    monkeypatch.setattr(mcs_drug, "fsync_dir", recording)
    assert mcs_drug.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "present"
    assert synced == [folder.resolve()]


def test_bundled_install_succeeds_under_an_owner_unreadable_umask(tmp_path):
    home = tmp_path / "home"
    args = SimpleNamespace(terms_checked_on=importer.EDITION_DATE.isoformat(),
                           terms_record=None, confirmed_by="synthetic", approved_by=None)
    bundle = _bundle(tmp_path)
    previous = os.umask(0o400)
    try:
        result = mcs_drug.install_bundled(args, directory=bundle, home=str(home))
    finally:
        os.umask(previous)
    assert result["state"] == "written"
    for directory in (home, home / "data", home / "data" / "drug-map"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
