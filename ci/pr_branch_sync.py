#!/usr/bin/env python3
"""Merge the base branch into every open pull request that is behind it, and re-run CI on the result.

`.github/workflows/main-sync.yml` runs this after each push to main. It talks
to GitHub only through the `gh` CLI with the workflow's GITHUB_TOKEN and
imports nothing from the application. Standard library only.

- A PR from a fork, or one already up to date, is left alone.
- The merge is GitHub's "Update branch" (a merge commit on the head branch,
  bound to the head we read): never a rebase or force-push.
- A merge GitHub refuses (a conflict, or a change to .github/workflows that
  GITHUB_TOKEN may not write) gets one comment on the PR, edited in place on
  later runs, saying how to merge by hand. A later success edits it again.
- A commit made with GITHUB_TOKEN starts no pull_request workflow, so the
  required checks would never report on the new head. CI is started with
  workflow_dispatch on the head branch instead (GITHUB_TOKEN may do that);
  agent-control.yml then publishes merge-gate for that head as usual.
- An auto-merge reservation is withdrawn first: it was decided for the old
  head, and agent-control.yml re-reserves after judging the new one.
See docs/development/AUTOMATION.md.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from agent_control import same_repository  # ci/ is this script's import root

MARKER = "<!-- main-sync:update-branch -->"
CI_WORKFLOW = "ci.yml"
HEAD_WAIT_SECONDS = 60
HEAD_POLL_SECONDS = 3


class GitHubError(Exception):
    """A `gh api` call that GitHub refused; the message is GitHub's."""


def gh_api(*args: str) -> object:
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if result.returncode:
        try:
            message = json.loads(result.stdout).get("message") or ""
        except (ValueError, AttributeError):
            message = ""
        raise GitHubError(message or result.stderr.strip() or "gh api failed")
    return json.loads(result.stdout) if result.stdout.strip() else None


def failure_body(base: str, base_sha: str, message: str) -> str:
    return (f"{MARKER}\n{base} の最新（`{base_sha[:7]}`）をこのブランチへ自動で取り込めませんでした。\n\n"
            f"GitHub の応答: {message}\n\n"
            "手元で取り込んで push してください（rebase や force-push は不要です）。\n\n"
            f"```sh\ngit fetch origin {base}\ngit merge origin/{base}\n"
            "# 衝突を解消して commit したら\ngit push\n```\n\n"
            "DEVELOPMENT.md の生成表で衝突したときは、生成部分を "
            f"origin/{base} 側に合わせてください（PR には含めません）。")


def success_body(base: str, base_sha: str) -> str:
    return f"{MARKER}\n{base}（`{base_sha[:7]}`）を自動で取り込みました。前回の取り込み失敗は解消しています。"


class Sync:
    def __init__(self, repo: str, base: str, api=gh_api, sleep=time.sleep):
        self.repo, self.base, self.api, self.sleep = repo, base, api, sleep

    def open_prs(self) -> list[dict]:
        pages = self.api(f"repos/{self.repo}/pulls?state=open&base={self.base}&per_page=100",
                         "--paginate", "--slurp")
        return [pr for page in pages or [] for pr in page]

    def marker_comment(self, number: int) -> dict | None:
        pages = self.api(f"repos/{self.repo}/issues/{number}/comments?per_page=100",
                         "--paginate", "--slurp")
        for comment in (c for page in pages or [] for c in page):
            if MARKER in (comment.get("body") or ""):
                return comment
        return None

    def comment(self, number: int, body: str, *, only_if_exists: bool = False) -> None:
        existing = self.marker_comment(number)
        if existing:
            if existing.get("body") != body:
                self.api("-X", "PATCH", f"repos/{self.repo}/issues/comments/{existing['id']}",
                         "-f", f"body={body}")
        elif not only_if_exists:
            self.api("-X", "POST", f"repos/{self.repo}/issues/{number}/comments", "-f", f"body={body}")

    def wait_for_new_head(self, number: int, old_sha: str) -> dict | None:
        waited = 0
        while waited < HEAD_WAIT_SECONDS:
            self.sleep(HEAD_POLL_SECONDS)
            waited += HEAD_POLL_SECONDS
            pr = self.api(f"repos/{self.repo}/pulls/{number}")
            if pr["head"]["sha"] != old_sha:
                return pr
        return None

    def sync_one(self, pr: dict, base_sha: str) -> str:
        number, head = pr["number"], pr["head"]
        if not same_repository(pr):
            return "skip: fork"
        behind = self.api(f"repos/{self.repo}/compare/{base_sha}...{head['sha']}")["behind_by"]
        if not behind:
            return "skip: up to date"
        if pr.get("auto_merge"):
            self.api("graphql", "-f",
                     "query=mutation($id:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$id}){clientMutationId}}",
                     "-f", f"id={pr['node_id']}")
        try:
            self.api("-X", "PUT", f"repos/{self.repo}/pulls/{number}/update-branch",
                     "-f", f"expected_head_sha={head['sha']}")
        except GitHubError as e:
            self.comment(number, failure_body(self.base, base_sha, str(e)))
            return f"failed: {e}"
        updated = self.wait_for_new_head(number, head["sha"])
        if updated is None:
            return "merged, but the new head did not appear in time; CI not started"
        self.api("-X", "POST", f"repos/{self.repo}/actions/workflows/{CI_WORKFLOW}/dispatches",
                 "-f", f"ref={updated['head']['ref']}")
        self.comment(number, success_body(self.base, base_sha), only_if_exists=True)
        return f"merged {base_sha[:7]} -> {updated['head']['sha'][:7]}, CI dispatched"

    def run(self) -> int:
        base_sha = self.api(f"repos/{self.repo}/branches/{self.base}")["commit"]["sha"]
        errors = 0
        for pr in self.open_prs():
            try:
                outcome = self.sync_one(pr, base_sha)
            except GitHubError as e:  # one PR's trouble must not stop the others
                outcome, errors = f"error: {e}", errors + 1
            print(f"#{pr['number']} {pr['head']['ref']}: {outcome}")
        return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", required=True)
    args = ap.parse_args(argv)
    return Sync(args.repo, args.base).run()


if __name__ == "__main__":
    sys.exit(main())
