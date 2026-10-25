"""update_discovery keeps "no such tag at origin" apart from a failed query, and
current_version only reports a tag the release policy accepts (synthetic
stubs and a throwaway local Git repository; no network)."""
import os
import subprocess

import pytest

import update_discovery
import update_policy


def _stub(monkeypatch, rc, out, err=""):
    monkeypatch.setattr("owned_process.run_owned",
                        lambda argv, timeout, environment, stdin=None:
                        subprocess.CompletedProcess(argv, rc, out, err))


def _remote(tag="v1.2.3"):
    return update_discovery.remote_tag_sha(tag, checkout="/synthetic/checkout", environment={})


def test_failed_ls_remote_is_a_discovery_error_not_a_moved_tag(monkeypatch):
    _stub(monkeypatch, 128, "", "fatal: synthetic-secret-host unreachable")
    with pytest.raises(update_discovery.DiscoveryError) as caught:
        _remote()
    assert str(caught.value) == "git_failed: ls-remote (exit=128)"     # fixed code, no stderr text


@pytest.mark.parametrize("out", ["", "a" * 40 + "\trefs/tags/v9.9.9\n", "not-a-sha\trefs/tags/v1.2.3\n"])
def test_a_successful_query_without_a_usable_ref_is_still_none(monkeypatch, out):
    _stub(monkeypatch, 0, out)
    assert _remote() is None


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=synthetic",
                           "-c", "user.email=synthetic@invalid.test", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def tagged_repo(tmp_path):
    """Linear history whose nearest tags walk from release forms to non-release ones."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    commits = {}
    for tag in ("1.2.3", "v1.2.4-rc1", "archive/x", "v1garbage"):
        _git(repo, "commit", "-q", "--allow-empty", "-m", tag)
        _git(repo, "tag", tag)
        commits[tag] = _git(repo, "rev-parse", "HEAD")
    return repo, commits


@pytest.mark.parametrize("head,expected", [
    ("1.2.3", "1.2.3"),               # bare semver is a release for update_policy
    ("v1.2.4-rc1", "v1.2.4-rc1"),     # prerelease tags are releases too
    ("archive/x", "v1.2.4-rc1"),      # a non-release tag is skipped by the match
    ("v1garbage", "v1.2.4-rc1"),      # pattern-alike but not a release: the valid ancestor still wins
])
def test_current_version_reports_only_policy_release_tags(tagged_repo, head, expected):
    repo, commits = tagged_repo
    _git(repo, "checkout", "-q", "--detach", commits[head])
    tag, sha = update_discovery.current_version(checkout=str(repo), environment=os.environ)
    assert tag == expected and sha == commits[head]
    assert tag is None or update_policy.release_tag(tag)


def test_current_version_is_none_without_any_policy_release_ancestor(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for tag in ("archive/x", "v1garbage", "1.2"):
        _git(repo, "commit", "-q", "--allow-empty", "-m", tag)
        _git(repo, "tag", tag)
    tag, _ = update_discovery.current_version(checkout=str(repo), environment=os.environ)
    assert tag is None
