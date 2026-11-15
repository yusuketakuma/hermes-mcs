"""Standalone wrappers rendered into a shared root/checkout keep split source verifiable."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import mcs_util
import pytest
import service_management
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_preflight
import update_recovery
import update_repository
import update_runtime_migration
import update_services

from ops_testkit import _git
from mcs_update_testkit import updater  # noqa: F401
from test_update_rollback_guards import _head, _source_repo


def _shared_checkout(updater, tmp_path, monkeypatch):  # noqa: F811
    repo, _ = _source_repo(tmp_path)
    monkeypatch.setattr(updater, "REPO", str(repo))
    for module in (update_apply, update_cli, update_execution, update_lifecycle_context, update_preflight,
                   update_recovery, update_repository, update_runtime_migration, update_services):
        monkeypatch.setattr(module, "REPO", updater.REPO)
    template = repo / "deployment/scripts/mcs_check.sh"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("#!/bin/bash\n# synthetic wrapper\n")
    _git(repo, "add", "deployment/scripts/mcs_check.sh")
    _git(repo, "commit", "-qm", "synthetic wrapper template")
    return repo, _head(repo)


def test_standalone_services_in_shared_checkout_keeps_source_verified(updater, tmp_path, monkeypatch):  # noqa: F811
    repo, target = _shared_checkout(updater, tmp_path, monkeypatch)
    assert updater._split_target_source_matches(target)
    monkeypatch.setattr(mcs_util, "load_config", lambda *_a, **_k: {"runtime_mode": "standalone"})
    # Default layout: the MCS root and the checkout are the same directory.
    paths = service_management.default_paths(root=str(repo), checkout=str(repo),
                                             manifest_path=str(tmp_path / "manifest.json"))
    service_management.sync_scripts({}, {"scripts": []}, lambda _message: None, False, paths=paths)
    assert (repo / "scripts" / "mcs_check.sh").is_file()
    assert updater._split_target_source_matches(target)
    assert updater._split_target_source_matches(target, allow_reviewed_rollback=True)


@pytest.mark.parametrize("name", ["scripts/other.sh", "scripts/mcs_check.py", "scripts/sub/mcs_check.sh",
                                  "deployment/mcs_check.sh", "mcs/mcs_check.sh"])
def test_only_rendered_wrapper_names_are_tolerated(updater, tmp_path, monkeypatch, name):  # noqa: F811
    repo, target = _shared_checkout(updater, tmp_path, monkeypatch)
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# synthetic untracked file\n")
    assert not updater._split_target_source_matches(target)


def test_reviewed_rollback_accepts_wrappers_from_both_trees(updater, tmp_path, monkeypatch):  # noqa: F811
    repo, previous = _shared_checkout(updater, tmp_path, monkeypatch)
    _git(repo, "rm", "deployment/scripts/mcs_check.sh")
    (repo / "deployment/scripts").mkdir(parents=True, exist_ok=True)
    (repo / "deployment/scripts/mcs_new.sh").write_text("#!/bin/bash\n# synthetic new template\n")
    _git(repo, "add", "deployment/scripts/mcs_new.sh")
    _git(repo, "commit", "-qm", "synthetic template replacement")
    tree = update_repository._split_source_tree(previous)
    witness = hashlib.sha256(json.dumps(tree, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
    monkeypatch.setattr(update_repository, "_PROVIDER_ROLLBACK_SOURCE_WITNESSES", frozenset({witness}))
    for name in ("mcs_check.sh", "mcs_new.sh"):
        (repo / "scripts" / name).write_text("#!/bin/bash\n# synthetic rendered wrapper\n")
    assert update_repository._split_target_source_matches(previous, allow_reviewed_rollback=True)
    assert not update_repository._split_target_source_matches(_head(repo))
    (repo / "scripts/untracked.py").write_text("# synthetic shadow\n")
    assert not update_repository._split_target_source_matches(previous, allow_reviewed_rollback=True)


def test_every_rendered_wrapper_name_is_ignored_and_no_tracked_path_is():
    # The release-owner verify_checkout, its post-merge child and the SDC facts probe honour
    # .gitignore; a template without an entry would hold those routes on standalone again.
    root = Path(__file__).resolve().parents[2]
    names = sorted(p.name for p in (root / "deployment/scripts").iterdir() if p.name.endswith(".sh"))
    assert names
    ignored = subprocess.run(["git", "check-ignore", "--no-index", "--stdin", "-z"], cwd=root,
                             input="\0".join("scripts/" + n for n in names), capture_output=True, text=True,
                             timeout=10)
    assert sorted(p for p in ignored.stdout.split("\0") if p) == ["scripts/" + n for n in names]
    tracked = subprocess.run(["git", "ls-files", "-ci", "--exclude-standard", "-z"], cwd=root,
                             capture_output=True, text=True, timeout=10)
    assert tracked.returncode == 0 and tracked.stdout == ""


def test_ignored_wrapper_leaves_status_clean_but_other_untracked_scripts_are_reported(tmp_path):
    root = Path(__file__).resolve().parents[2]
    shutil.copyfile(root / ".gitignore", tmp_path / ".gitignore")
    (tmp_path / "deployment/scripts").mkdir(parents=True)
    (tmp_path / "deployment/scripts/mcs_check.sh").write_text("#!/bin/bash\n# synthetic template\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/mcs").write_text("#!/bin/bash\n# synthetic launcher\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "s")
    (tmp_path / "scripts/mcs_check.sh").write_text("#!/bin/bash\n# synthetic rendered wrapper\n")
    # Same arguments as release_apply_owner CHILD_HOLD and sdc_cli.facts.
    for mode in ("all", "normal"):
        assert _git(tmp_path, "status", "--porcelain=v1", f"--untracked-files={mode}").stdout == ""
    assert _git(tmp_path, "ls-files", "--others", "--exclude-standard", "-z").stdout == ""
    (tmp_path / "scripts/other.sh").write_text("# synthetic untracked script\n")
    assert _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all").stdout == "?? scripts/other.sh\n"
