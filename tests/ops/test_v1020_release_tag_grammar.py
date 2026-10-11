"""One release-tag grammar for detection, notice, approval and the CLI (F22).

Fully synthetic: temp ledger, stubbed updater pins/config, temp git repos.
"""
import importlib.util
import json
from pathlib import Path
import uuid

import pytest

from ledger import Ledger
import mcs_cli
import mcs_requests
import mcs_update
from ops_testkit import _git, _make_repo
from test_mcs_upgrade import _release

ROOT = Path(__file__).resolve().parents[2]
PRERELEASE = "v1.3.0-rc.1"


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "mcs_upgrade_grammar", ROOT / "scripts" / "mcs_upgrade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _apply_req(**fields):
    req = {"version": 1, "cmd": "ops.update_apply",
           "command_id": str(uuid.uuid4()), "actor": "synthetic-cco",
           "human_confirmed": True, "project_id": None, "reason": "go"}
    req.update(fields)
    return req


def _approve(tmp_path, monkeypatch, update_cfg, tag):
    import mcs_util
    wrapper = tmp_path / "mcs_update.sh"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(mcs_update, "WRAPPER", str(wrapper))
    monkeypatch.setattr(mcs_util, "load_config", lambda: {"update": update_cfg})
    monkeypatch.setattr(mcs_update, "remote_tag_sha", lambda t: "b" * 40)
    monkeypatch.setattr(mcs_update, "current_version",
                        lambda: ("v1.2.0", "c" * 40))
    db = Ledger(str(tmp_path / "ledger.db"))
    try:
        return mcs_requests.apply_command(db, _apply_req(tag=tag))
    finally:
        db.close()


def test_notified_prerelease_preview_validates():
    # The notify-mode notice tells the operator to send exactly this tag.
    assert mcs_requests.validate(_apply_req(tag=PRERELEASE)) is None
    rollback = _apply_req(cmd="ops.update_rollback", tag=PRERELEASE)
    assert mcs_requests.validate(rollback) is None


def test_opted_in_prerelease_approval_is_scheduled(tmp_path, monkeypatch):
    receipt = _approve(tmp_path, monkeypatch,
                       {"mode": "notify", "include_prerelease": True}, PRERELEASE)
    assert receipt["outcome"] == "applied"
    assert receipt["tag"] == PRERELEASE and receipt["target_sha"] == "b" * 40
    # the scheduled updater picks the same receipt up
    assert mcs_update._ver_key(receipt["tag"]) is not None


def test_prerelease_approval_requires_the_opt_in(tmp_path, monkeypatch):
    receipt = _approve(tmp_path, monkeypatch, {"mode": "notify"}, PRERELEASE)
    assert receipt["outcome"] == "rejected"
    assert receipt["error"] == "prerelease_not_enabled"


@pytest.mark.parametrize("tag", [
    "v1.2.3", "1.2.3", PRERELEASE, "v1.3.0-rc1", "v1.3.0-0.alpha-x",
    "v1.0.0-rc..1", "v1.0.0-", "v1.0.0-.rc", "v1.0.0-rc.", "v1.2", "v1.2.3.4",
    "v١.٠.٢", "v1.2.3\n", "V1.2.3", "v1.0.0-rc+1",
    "v1.0.0-" + "a" * 250, None, 123,
])
def test_approval_grammar_equals_the_updater_ordering(tag):
    # anything approvable must be orderable, or the scan drops the receipt
    approvable = mcs_requests.validate(_apply_req(tag=tag)) is None
    assert approvable == mcs_requests.release_tag(tag) \
        == (mcs_update._ver_key(tag) is not None)


def test_apply_requires_a_single_well_formed_tag():
    req = _apply_req()
    assert mcs_requests.validate(req) == "bad_tag"
    assert mcs_requests.validate(_apply_req(tag="v1.0.0-rc..1")) == "bad_tag"


def test_launcher_copy_matches_the_shared_grammar():
    assert _launcher().SEMVER_RE.pattern == mcs_requests.RELEASE_TAG_RE.pattern


def test_launcher_default_target_is_the_newest_release(monkeypatch):
    launcher = _launcher()
    monkeypatch.setattr(launcher, "_git",
                        lambda *a, **k: "v1.2.0\nv1.10.0\nv1.11.0-rc.1\nnotes\n")
    assert launcher.latest_tag("unused") == "v1.10.0"


def test_launcher_accepts_an_explicit_prerelease(tmp_path, capfd):
    repo, bare = _make_repo(tmp_path)
    stub = "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    _release(tmp_path / "remote-work", bare, PRERELEASE,
             {"mcs/ops/mcs_update.py": stub, "mcs/_mcs_path.py": ""})
    _git(repo, "fetch", "-q", "--tags")
    assert _launcher().main(["--repo", str(repo), "--no-fetch", "plan",
                             "--to", PRERELEASE]) == 0
    assert json.loads(capfd.readouterr().out) == ["plan", "--tag", PRERELEASE]


def test_cli_accepts_a_prerelease_and_rejects_non_ascii_digits(monkeypatch):
    monkeypatch.setenv("MCS_LIFECYCLE_PINNED", "1")
    calls = []
    monkeypatch.setattr(mcs_cli.os, "execv",
                        lambda exe, argv: calls.append(argv))
    mcs_cli.main(["update", "apply", "--to", PRERELEASE])
    assert calls[0][1:] == [str(ROOT / "scripts/mcs_upgrade.py"), "--repo",
                            str(ROOT), "apply", "--to", PRERELEASE]
    with pytest.raises(SystemExit) as result:
        mcs_cli.main(["update", "plan", "--to", "v١.٠.٢"])
    assert result.value.code == 2
