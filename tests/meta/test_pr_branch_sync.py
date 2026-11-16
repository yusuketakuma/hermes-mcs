"""ci/pr_branch_sync.py: main is merged into behind PRs without force, CI is
restarted on the new head, and a refused merge leaves one edited comment."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci"))
_spec = importlib.util.spec_from_file_location("pr_branch_sync", ROOT / "ci/pr_branch_sync.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

REPO = "yusuketakuma/hermes-mcs"
MAIN = "m" * 40
OLD = "a" * 40
NEW = "n" * 40


def _pr(number=7, head_repo=REPO, auto_merge=None):
    return {"number": number, "node_id": f"PR_{number}", "auto_merge": auto_merge,
            "head": {"sha": OLD, "ref": f"feature-{number}", "repo": {"full_name": head_repo}},
            "base": {"repo": {"full_name": REPO}}}


class FakeGitHub:
    """Answers the calls Sync makes and records every write."""

    def __init__(self, prs, behind=3, refuse=None, comments=(), head_after=NEW,
                 head_author="someone", ci_runs=1):
        self.prs, self.behind, self.refuse = prs, behind, refuse
        self.head_author, self.ci_runs = head_author, ci_runs
        self.comments = [dict(c) for c in comments]
        self.head_after = head_after
        self.writes = []

    def __call__(self, *args):
        method = args[1] if args[0] == "-X" else ("POST" if args[0] == "graphql" else "GET")
        path = args[2] if args[0] == "-X" else args[0]
        if method != "GET":
            self.writes.append((method, path, args[3:] if args[0] == "-X" else args[1:]))
        if path == f"repos/{REPO}/branches/main":
            return {"commit": {"sha": MAIN}}
        if path.startswith(f"repos/{REPO}/pulls?state=open"):
            return [self.prs]
        if "/compare/" in path:
            return {"behind_by": self.behind}
        if path.endswith("/update-branch"):
            if self.refuse:
                raise sync.GitHubError(self.refuse)
            return {"message": "Updating pull request branch."}
        if path.startswith(f"repos/{REPO}/pulls/"):
            pr = dict(self.prs[0])
            pr["head"] = dict(pr["head"], sha=self.head_after)
            return pr
        if path.startswith(f"repos/{REPO}/commits/"):
            return {"author": {"login": self.head_author}}
        if "/runs?head_sha=" in path:
            return {"total_count": self.ci_runs}
        if "/comments?" in path:
            return [self.comments]
        return {}


def _run(fake):
    return sync.Sync(REPO, "main", api=fake, sleep=lambda s: None).run()


def test_behind_pr_is_merged_with_its_head_pinned_and_ci_dispatched():
    fake = FakeGitHub([_pr()])
    assert _run(fake) == 0
    methods = [(m, p) for m, p, _ in fake.writes]
    assert methods == [("PUT", f"repos/{REPO}/pulls/7/update-branch"),
                       ("POST", f"repos/{REPO}/actions/workflows/ci.yml/dispatches"),
                       ("POST", f"repos/{REPO}/actions/workflows/release-notes.yml/dispatches")]
    assert fake.writes[0][2] == ("-f", f"expected_head_sha={OLD}")
    assert fake.writes[1][2] == ("-f", "ref=feature-7")
    # only the read-only PR check of release-notes.yml, never its default sync
    assert fake.writes[2][2] == ("-f", "ref=feature-7", "-f", "inputs[action]=pr_check")


@pytest.mark.parametrize("pr, behind", [(_pr(head_repo="someone/fork"), 3), (_pr(), 0)])
def test_fork_and_up_to_date_prs_are_left_alone(pr, behind):  # head by a person, CI ran
    fake = FakeGitHub([pr], behind=behind)
    assert _run(fake) == 0
    assert fake.writes == []


def test_refused_merge_comments_once_then_edits_in_place():
    fake = FakeGitHub([_pr()], refuse="merge conflict between base and head")
    assert _run(fake) == 0
    (method, path, args), = fake.writes[1:]
    assert (method, path) == ("POST", f"repos/{REPO}/issues/7/comments")
    body = args[1]
    assert sync.MARKER in body and "merge conflict" in body and MAIN[:7] in body
    assert "force" not in body.replace("force-push は不要", "")
    # the next run finds its own comment and edits it rather than posting again
    again = FakeGitHub([_pr()], refuse="merge conflict", comments=[{"id": 99, "body": body}])
    assert _run(again) == 0
    assert [(m, p) for m, p, _ in again.writes[1:]] == [("PATCH", f"repos/{REPO}/issues/comments/99")]


def test_success_after_a_failure_edits_the_comment_but_never_posts_new():
    old = {"id": 5, "body": sync.failure_body("main", MAIN, "conflict")}
    fake = FakeGitHub([_pr()], comments=[old])
    assert _run(fake) == 0
    assert fake.writes[-1][:2] == ("PATCH", f"repos/{REPO}/issues/comments/5")
    quiet = FakeGitHub([_pr()])
    _run(quiet)
    assert not any(p.endswith("/comments") for _, p, _ in quiet.writes)


def test_auto_merge_reservation_is_withdrawn_before_the_head_moves():
    fake = FakeGitHub([_pr(auto_merge={"merge_method": "squash"})])
    assert _run(fake) == 0
    first, second = fake.writes[:2]
    assert first[1] == "graphql" and "disablePullRequestAutoMerge" in first[2][1]
    assert first[2][-1] == "id=PR_7"
    assert second[1].endswith("/update-branch")


def test_ci_is_not_dispatched_while_the_new_head_has_not_appeared():
    fake = FakeGitHub([_pr()], head_after=OLD)
    assert _run(fake) == 0
    assert not any("dispatches" in p for _, p, _ in fake.writes)


def test_one_failing_pr_does_not_stop_the_others():
    class Flaky(FakeGitHub):
        def __call__(self, *args):
            if args[0] == f"repos/{REPO}/compare/{MAIN}...{OLD}" and not getattr(self, "hit", False):
                self.hit = True
                raise sync.GitHubError("server error")
            return super().__call__(*args)
    fake = Flaky([_pr(1), _pr(2)])
    assert _run(fake) == 1
    assert any(p == f"repos/{REPO}/pulls/2/update-branch" for _, p, _ in fake.writes)


@pytest.mark.parametrize("author, runs, dispatched", [
    ("github-actions[bot]", 0, True),    # our merge whose dispatch was lost
    ("github-actions[bot]", 1, False),   # CI already ran on it
    ("someone", 0, False),               # a person's push starts CI itself
])
def test_up_to_date_sync_merge_without_ci_is_dispatched_again(author, runs, dispatched):
    fake = FakeGitHub([_pr()], behind=0, head_author=author, ci_runs=runs)
    assert _run(fake) == 0
    assert [p for _, p, _ in fake.writes] == ([
        f"repos/{REPO}/actions/workflows/ci.yml/dispatches",
        f"repos/{REPO}/actions/workflows/release-notes.yml/dispatches"] if dispatched else [])
