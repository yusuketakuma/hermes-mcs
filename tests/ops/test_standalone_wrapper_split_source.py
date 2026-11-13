"""Standalone wrappers rendered into a shared root/checkout keep split source verifiable."""
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
