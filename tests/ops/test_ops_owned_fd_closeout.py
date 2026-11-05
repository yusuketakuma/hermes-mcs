"""Synthetic pre-transfer failures reclaim only the seven owned ops descriptors."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_repository
import update_restore
import update_runtime_migration
import update_services
import errno
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

import brain_export
import import_drug_master
import mcs_repair
import mcs_restore
import mcs_update
import safe_io


@pytest.mark.parametrize("operation", ["export", "master", "repair_hash", "consent_read",
                                       "consent_publish", "worktree_hash", "restore_stage"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt, SystemExit])
def test_ops_owned_stream_failure_closes_descriptor(tmp_path, monkeypatch, operation, failure):
    owned = []
    directory = None
    staged = []
    settle = []
    source = tmp_path / "synthetic-input"
    error = failure("synthetic transfer refusal")
    try:
        source.write_bytes(b"synthetic")
        source.chmod(0o600)
        if operation == "export":
            def invoke():
                brain_export._write(tmp_path, "synthetic.md", "synthetic")
        elif operation == "master":
            tmp_path.chmod(0o700)
            def invoke():
                import_drug_master.write_private(
                    import_drug_master.Conversion({}, b"synthetic"), tmp_path / "dictionary.json")
        elif operation == "repair_hash":
            def invoke():
                mcs_repair.hash_file(source)
        elif operation in {"consent_read", "consent_publish"}:
            directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
            def invoke():
                if operation == "consent_read":
                    mcs_restore._read(directory, source.name)
                else:
                    mcs_restore._publish(directory, "receipt.json", {"synthetic": True})
        elif operation == "worktree_hash":
            digest = hashlib.sha1(b"blob 9\0synthetic", usedforsecurity=False).hexdigest()
            def git(args):
                if args[0] == "ls-tree":
                    output = f"100644 blob {digest}\tsynthetic-input\0"
                elif args[0] == "ls-files":
                    output = f"H 100644 {digest} 0\tsynthetic-input\0"
                else:
                    assert args[0] == "diff"
                    output = ""
                return subprocess.CompletedProcess(args, 0, output, "")
            monkeypatch.setattr(mcs_update, "REPO", str(tmp_path))
            monkeypatch.setattr(update_apply, 'REPO', mcs_update.REPO)
            monkeypatch.setattr(update_cli, 'REPO', mcs_update.REPO)
            monkeypatch.setattr(update_execution, 'REPO', mcs_update.REPO)
            monkeypatch.setattr(update_lifecycle_context, 'REPO', mcs_update.REPO)
            monkeypatch.setattr(update_preflight, 'REPO', mcs_update.REPO)
            monkeypatch.setattr(update_recovery, 'REPO', mcs_update.REPO)
            monkeypatch.setattr(update_repository, 'REPO', mcs_update.REPO)
            monkeypatch.setattr(update_runtime_migration, 'REPO', mcs_update.REPO)
            monkeypatch.setattr(update_services, 'REPO', mcs_update.REPO)
            monkeypatch.setattr(mcs_update, "_git", git)
            monkeypatch.setattr(update_apply, '_git', mcs_update._git)
            monkeypatch.setattr(update_cli, '_git', mcs_update._git)
            monkeypatch.setattr(update_planning, '_git', mcs_update._git)
            monkeypatch.setattr(update_preflight, '_git', mcs_update._git)
            monkeypatch.setattr(update_recovery, '_git', mcs_update._git)
            monkeypatch.setattr(update_repository, '_git', mcs_update._git)
            monkeypatch.setattr(update_restore, '_git', mcs_update._git)
            monkeypatch.setattr(update_runtime_migration, '_git', mcs_update._git)
            def invoke():
                mcs_update._local_changes_outside("synthetic-before", "synthetic-after")
        else:
            monkeypatch.setattr(mcs_update, "LEDGER", str(tmp_path / "ledger.db"))
            monkeypatch.setattr(update_apply, 'LEDGER', mcs_update.LEDGER)
            monkeypatch.setattr(update_execution, 'LEDGER', mcs_update.LEDGER)
            monkeypatch.setattr(update_lifecycle_context, 'LEDGER', mcs_update.LEDGER)
            monkeypatch.setattr(update_planning, 'LEDGER', mcs_update.LEDGER)
            monkeypatch.setattr(update_preflight, 'LEDGER', mcs_update.LEDGER)
            monkeypatch.setattr(update_restore, 'LEDGER', mcs_update.LEDGER)
            monkeypatch.setattr(mcs_update, "_settle_staging", lambda _directory: None)
            monkeypatch.setattr(update_restore, '_settle_staging', mcs_update._settle_staging)
            def release(root, record, journaled):
                settle.append((record, journaled))
                path = Path(root) / record["name"]
                staged.append(path)
                assert path.stat().st_ino == record["ino"]
                path.unlink()
            monkeypatch.setattr(mcs_update, "_release_staging", release)
            monkeypatch.setattr(update_restore, '_release_staging', mcs_update._release_staging)
            monkeypatch.setattr(mcs_update.journal_store, "save_state",
                                lambda *_a, **_kw: pytest.fail("no journal write before transfer"))
            monkeypatch.setattr(mcs_update.sqlite3, "connect",
                                lambda *_a, **_kw: pytest.fail("no DB access before transfer"))
            def invoke():
                mcs_update._replace_database(str(source), "a" * 64,
                                             lambda: pytest.fail("no database replacement"))

        def refuse(fd, *_args, **_kwargs):
            owned.append(fd)
            raise error

        with monkeypatch.context() as patch:
            patch.setattr(safe_io.os, "fdopen", refuse)
            if operation == "worktree_hash" and failure is OSError:
                with pytest.raises(mcs_update.UpdateError, match="diff: content provenance unknown") as caught:
                    invoke()
                assert caught.value.__cause__ is error
            else:
                with pytest.raises(failure) as caught:
                    invoke()
                assert caught.value is error
        assert len(owned) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(owned[0])
        assert closed.value.errno == errno.EBADF
        assert source.read_bytes() == b"synthetic"
        assert not (tmp_path / "synthetic.md").exists()
        assert not (tmp_path / "dictionary.json").exists()
        assert not (tmp_path / "receipt.json").exists()
        assert not (tmp_path / "ledger.db").exists()
        assert not list(tmp_path.glob(".backup-consent-*"))
        assert not list(tmp_path.glob(".drug-master-*"))
        if operation == "restore_stage":
            assert len(settle) == 1 and settle[0][1] is False
            assert len(staged) == 1 and not staged[0].exists()
        if directory is not None:
            assert os.fstat(directory).st_ino == tmp_path.stat().st_ino
    finally:
        try:
            for fd in owned:
                try:
                    os.close(fd)
                except OSError as cleanup_error:
                    if cleanup_error.errno != errno.EBADF:
                        raise
        finally:
            if directory is not None:
                os.close(directory)
