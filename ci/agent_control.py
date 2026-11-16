#!/usr/bin/env python3
"""Decide the merge gate, the agent lock and the attempt budget for pull requests from GitHub API data.

`.github/workflows/agent-control.yml` fetches the data with `gh` and applies
the plan this script prints; the script itself makes no network call and
imports nothing from the application. Standard library only.

  gate     verdict for the `merge-gate` commit status of a PR head, whether
           the PR needs a person, auto-merge eligibility, and the attempt
           labels to set on the linked issue after a failed CI run
  attempt  the attempt labels after one more failure
  linked   the issues a PR closes (Fixes #N and the like)
  repair   whether Claude may repair a failed PR now, from the PR and the
           current labels of its linked issues
  lock     whether a newly added 担当:* label conflicts with another one,
           and why its sender may not assign this issue (if so)
  assigned whether an agent label is the issue's only assignment,
           and why its sender may not assign this issue (if so)

The verdict is evidence, not trust in "CI is green": every required job must
have run on the current head and succeeded (GitHub counts skipped and neutral
as passing a required check), the results must come from GitHub Actions, and
an automated branch is refused while the kill switch is off.
See docs/development/AUTOMATION.md.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys

# The CI jobs main-protect requires. They must have run on the head and
# succeeded: "skipped" or "neutral" also satisfy a required check on GitHub.
REQUIRED_JOBS = ("lint-test", "hygiene", "standalone-sdk", "incident-gates",
                 "macos", "hermes-integration")
# Jobs that carry the test evidence behind a gate job, when ci.yml has them.
# pytest-macos may be skipped only for a docs-only change (see docs_only()).
EVIDENCE_JOBS = ("pytest-linux", "pytest-macos")
ACTIONS_APP = "github-actions"
# Branches written by an AI agent. claude/ is left out: project threads use it
# for work the owner asked for. Claude Code Action is configured to agent/.
AGENT_PREFIXES = ("agent/", "copilot/", "codex/")
# Changing any of these never merges automatically: the checks and the agent
# instructions themselves, restart-only code paths, and the safety gates.
HUMAN_PATHS = (
    ".github/*", "ci/*", "scripts/*", "Makefile", "pyproject.toml", "conftest.py",
    "CLAUDE.md", "AGENTS.md", "SECURITY.md",
    "hermes_plugin/*", "adapters/*", "lineworks_adapter/*", "mcs_standalone/*",
    "deployment/*",
)
DOC_SUFFIXES = (".md", ".png", ".svg")
ATTEMPT_LIMIT = 3
ATTEMPT_PREFIX = "試行:"
OWNER_LABEL = "要オーナー対応"
AGENT_LABEL_PREFIX = "担当:"
# Only Claude can be re-run from a workflow; its branches start with this.
REPAIR_PREFIX = "agent/claude-"
REPAIR_AGENT_LABEL = "担当:claude"
# A failure classified as one of these is held for a person, never retried.
HOLD_LABELS = ("失敗:CI基盤", "失敗:利用枠", "失敗:不明")
# Issue headings that state how to tell the work is done. The last two are the issue templates' wording.
ACCEPTANCE_HEADINGS = ("受入条件", "完了と判断する条件", "期待した動き")
# A dependabot Actions bump rewrites only pinned `uses:` lines.
_USES_LINE = re.compile(r"^[+-]\s*(-\s+)?uses:\s+[\w.-]+/[\w./-]+@[0-9a-f]{40}(\s+#.*)?$")
_TEST_SKIP = re.compile(r"pytest\.mark\.(skip|skipif|xfail)|pytest\.(skip|xfail)\(|unittest\.skip|@skip\b")
_TEST_DEF = re.compile(r"^\s*(async\s+)?def\s+test_")
_LINKED_ISSUE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)


def is_agent_branch(branch):
    return branch.startswith(AGENT_PREFIXES)


def same_repository(pr):
    """True when the head branch lives in the base repository (a fork can name any branch)."""
    head = (pr.get("head") or {}).get("repo") or {}
    base = (pr.get("base") or {}).get("repo") or {}
    return bool(head.get("full_name")) and head.get("full_name") == base.get("full_name")


def assigned_to(labels, agent_label):
    """True when `agent_label` is the issue's only 担当:* label."""
    return [name for name in labels if name.startswith(AGENT_LABEL_PREFIX)] == [agent_label]


def _matches(path, patterns):
    return any(path == p or fnmatch.fnmatchcase(path, p) for p in patterns)


def docs_only(files):
    """True when every changed file is an edited or added document outside docs/specs/."""
    if not files:
        return False
    for f in files:
        path = f["filename"]
        if f.get("status") in ("removed", "renamed"):
            return False
        top_md = "/" not in path and path.endswith(".md") and path not in ("CLAUDE.md", "AGENTS.md", "SECURITY.md")
        in_docs = path.startswith("docs/") and not path.startswith("docs/specs/") and path.endswith(DOC_SUFFIXES)
        if not (top_md or in_docs):
            return False
    return True


def _patch_lines(f, sign):
    return [line[1:] for line in (f.get("patch") or "").splitlines()
            if line.startswith(sign) and not line.startswith(sign * 3)]


def actions_pin_bump(files, author):
    """True for a dependabot PR that only re-pins `uses:` lines in workflow files."""
    if author != "dependabot[bot]" or not files:
        return False
    for f in files:
        if not fnmatch.fnmatchcase(f["filename"], ".github/workflows/*.yml") or f.get("status") != "modified":
            return False
        changed = [line for line in (f.get("patch") or "").splitlines()
                   if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        if not changed or not all(_USES_LINE.match(line) for line in changed):
            return False
    return True


def human_reasons(files):
    """Reasons a person must review and merge the change, empty when none apply."""
    reasons = []
    protected = sorted({f["filename"] for f in files if _matches(f["filename"], HUMAN_PATHS)})
    if protected:
        reasons.append("保護対象の変更: " + ", ".join(protected[:3]) + (" ほか" if len(protected) > 3 else ""))
    tests_removed = [f["filename"] for f in files
                     if f["filename"].startswith("tests/") and f.get("status") in ("removed", "renamed")]
    if tests_removed:
        reasons.append("テストファイルの削除・改名: " + ", ".join(tests_removed[:3]))
    added_skips = [f["filename"] for f in files if f["filename"].startswith(("tests/", "integration/"))
                   and any(_TEST_SKIP.search(line) for line in _patch_lines(f, "+"))]
    if added_skips:
        reasons.append("skip・xfail の追加: " + ", ".join(added_skips[:3]))
    net = sum(sum(bool(_TEST_DEF.match(line)) for line in _patch_lines(f, "+"))
              - sum(bool(_TEST_DEF.match(line)) for line in _patch_lines(f, "-"))
              for f in files if f["filename"].startswith(("tests/", "integration/")))
    if net < 0:
        reasons.append(f"テスト関数が {-net} 件減少")
    if any(f.get("patch") is None and f.get("status") != "removed" for f in files):
        reasons.append("差分を取得できないファイルあり")
    return reasons


def _latest_runs(check_runs, head_sha):
    """name -> latest check run on head_sha (a re-run replaces an earlier attempt)."""
    latest = {}
    for run in check_runs:
        if run.get("head_sha") != head_sha:
            continue
        name = run.get("name")
        if name not in latest or (run.get("started_at") or "") >= (latest[name].get("started_at") or ""):
            latest[name] = run
    return latest


def evidence(check_runs, head_sha, files):
    """(state, problems): state is success, pending or failure for the required jobs on head_sha."""
    runs = _latest_runs(check_runs, head_sha)
    problems, pending = [], []
    for name in REQUIRED_JOBS + EVIDENCE_JOBS:
        run = runs.get(name)
        if run is None:
            if name in REQUIRED_JOBS:
                pending.append(name)
            continue
        if (run.get("app") or {}).get("slug") != ACTIONS_APP:
            problems.append(f"{name}: GitHub Actions 以外の結果")
        elif run.get("status") != "completed":
            pending.append(name)
        elif run.get("conclusion") == "success":
            continue
        elif name == "pytest-macos" and run.get("conclusion") == "skipped" and docs_only(files):
            continue
        else:
            problems.append(f"{name}: {run.get('conclusion')}")
    if problems:
        return "failure", problems
    if pending:
        return "pending", ["実行待ち: " + ", ".join(pending)]
    return "success", []


def linked_issues(body):
    return sorted({int(n) for n in _LINKED_ISSUE.findall(body or "")})


def next_attempt(labels):
    """(remove, add, exhausted) for one more failed attempt on an issue with `labels`."""
    used = 0
    current = [name for name in labels if name.startswith(ATTEMPT_PREFIX)]
    for name in current:
        tail = name[len(ATTEMPT_PREFIX):]
        if tail.isdigit():
            used = max(used, int(tail))
    count = min(used + 1, ATTEMPT_LIMIT)
    remove = [name for name in current if name != f"{ATTEMPT_PREFIX}{count}"]
    add = [f"{ATTEMPT_PREFIX}{count}"]
    exhausted = used + 1 >= ATTEMPT_LIMIT
    if exhausted:
        remove += [name for name in labels if name.startswith(AGENT_LABEL_PREFIX)]
        add.append(OWNER_LABEL)
    return remove, add, exhausted


def repair_refusal(pr, issue_labels, automation_enabled, owner_request=False):
    """Why Claude may not work on `pr` now, or None. `issue_labels` maps each linked issue to its current labels.

    Every linked issue must still be Claude's alone: one exhausted or reassigned
    issue stops the whole PR. An automatic repair also stops on a hold label;
    the owner's own @claude on the PR (`owner_request`) may proceed past a hold.
    """
    if not automation_enabled:
        return "自動化の停止中"
    if pr.get("state") != "open" or not same_repository(pr) or not pr["head"]["ref"].startswith(REPAIR_PREFIX):
        return "このリポジトリの開いた agent/claude- ブランチではない"
    issues = linked_issues(pr.get("body"))
    if not issues:
        return "結ばれた Issue がない"
    for number in issues:
        labels = issue_labels.get(str(number), issue_labels.get(number))
        if labels is None:
            return f"#{number} のラベルを取得できない"
        if not assigned_to(labels, REPAIR_AGENT_LABEL):
            return f"#{number} の担当が Claude だけではない"
        held = [name for name in labels if name == OWNER_LABEL or (name in HOLD_LABELS and not owner_request)]
        if held:
            return f"#{number} が保留中（{', '.join(held)}）"
    return None


def decide_gate(pr, check_runs, files, behind_by, automation_enabled, ci_conclusion=None):
    """The plan for one PR head: commit status, auto-merge eligibility and attempt bookkeeping."""
    head = pr["head"]["sha"]
    branch = pr["head"]["ref"]
    author = (pr.get("user") or {}).get("login", "")
    # Only branches pushed to this repository count as agent work; outsiders cannot push here.
    agent = is_agent_branch(branch) and same_repository(pr)
    state, notes = evidence(check_runs, head, files)
    if agent and not automation_enabled:
        state, notes = "failure", ["自動化の停止中（AUTOMATION_ENABLED が true ではない）"]
    if pr.get("draft") and state == "success":
        state, notes = "pending", ["下書きのため判定保留"]
    reasons = human_reasons(files)
    pin_bump = actions_pin_bump(files, author)
    if pin_bump:
        reasons = [r for r in reasons if not r.startswith("保護対象の変更")]
    eligible = (state == "success" and not reasons and behind_by == 0 and not pr.get("draft")
                and (pin_bump or (agent and docs_only(files))))
    if state == "success":
        description = "必須ジョブ実行済み・成功" + ("・要人間承認" if reasons else "")
    else:
        description = "; ".join(notes)
    plan = {
        "sha": head,
        "state": state,
        "description": description[:140],
        "human_reasons": reasons,
        "auto_merge_eligible": eligible,
        "attempts": [],
    }
    if agent and ci_conclusion == "failure":
        plan["attempts"] = [{"issue": n} for n in linked_issues(pr.get("body"))]
    return plan


def lock_conflict(labels, added):
    """True when `added` is a 担当:* label and the issue already has another one."""
    if not added.startswith(AGENT_LABEL_PREFIX):
        return False
    return any(name.startswith(AGENT_LABEL_PREFIX) and name != added for name in labels)


def acceptance_criteria(body):
    """The text under the issue's acceptance heading, or "" when the issue has none yet."""
    lines = (body or "").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#") and any(title in line for title in ACCEPTANCE_HEADINGS):
            section = []
            for following in lines[index + 1:]:
                if following.startswith("#"):
                    break
                section.append(following)
            text = "\n".join(section).strip()
            return "" if text == "_No response_" else text
    return ""


def assignment_refusal(issue, sender, owner):
    """Why a 担当:* label on `issue` must come off, or None when the assignment stands.

    Only the owner's account assigns (Dots works through it), and only an issue that already states
    how to tell it is done: an agent never starts from an instruction nobody wrote down.
    """
    if not sender or sender.lower() != owner.lower():
        return "担当ラベルは所有者だけが付けられます"
    if not acceptance_criteria(issue.get("body")):
        return "担当を決める前に、Issue 本文へ「受入条件」の見出しと中身を書いてください（詳細化してから付け直します）"
    return None


def _labels(issue):
    return [label["name"] for label in issue.get("labels", [])]


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _flatten_checks(data):
    """Accept a check-runs response, a list of them (gh --paginate --slurp) or a bare list."""
    if isinstance(data, dict):
        return data.get("check_runs", [])
    runs = []
    for item in data:
        runs.extend(item.get("check_runs", []) if isinstance(item, dict) and "check_runs" in item else [item])
    return runs


def _flatten(data):
    if data and isinstance(data[0], list):
        return [item for page in data for item in page]
    return data


def _add_sender_arguments(command):
    command.add_argument("--sender", required=True, help="login that added the label")
    command.add_argument("--owner", required=True, help="repository owner login")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("gate")
    gate.add_argument("--pr", required=True)
    gate.add_argument("--checks", required=True)
    gate.add_argument("--files", required=True)
    gate.add_argument("--behind-by", type=int, required=True)
    gate.add_argument("--automation", default="")
    gate.add_argument("--ci-conclusion", default="")
    attempt = sub.add_parser("attempt")
    attempt.add_argument("--labels", required=True, help="JSON list of label names")
    linked = sub.add_parser("linked")
    linked.add_argument("--pr", required=True)
    repair = sub.add_parser("repair")
    repair.add_argument("--pr", required=True)
    repair.add_argument("--issue-labels", required=True, help="JSON object: issue number -> label names")
    repair.add_argument("--automation", default="")
    repair.add_argument("--owner-request", action="store_true", help="the owner asked with @claude on the PR")
    lock = sub.add_parser("lock")
    lock.add_argument("--issue", required=True, help="the issue as the REST API returns it")
    lock.add_argument("--added", required=True)
    _add_sender_arguments(lock)
    assigned = sub.add_parser("assigned")
    assigned.add_argument("--issue", required=True, help="the issue as the REST API returns it")
    assigned.add_argument("--agent", required=True)
    _add_sender_arguments(assigned)
    args = parser.parse_args(argv)
    if args.command == "gate":
        plan = decide_gate(_load(args.pr), _flatten_checks(_load(args.checks)), _flatten(_load(args.files)),
                           args.behind_by, args.automation == "true", args.ci_conclusion or None)
    elif args.command == "attempt":
        remove, add, exhausted = next_attempt(json.loads(args.labels))
        plan = {"remove": remove, "add": add, "exhausted": exhausted}
    elif args.command == "linked":
        plan = {"issues": linked_issues(_load(args.pr).get("body"))}
    elif args.command == "repair":
        refusal = repair_refusal(_load(args.pr), _load(args.issue_labels), args.automation == "true",
                                 args.owner_request)
        plan = {"allowed": refusal is None, "reason": refusal or ""}
    elif args.command == "assigned":
        issue = _load(args.issue)
        plan = {"assigned": assigned_to(_labels(issue), args.agent),
                "refusal": assignment_refusal(issue, args.sender, args.owner) or ""}
    else:
        issue = _load(args.issue)
        plan = {"conflict": lock_conflict(_labels(issue), args.added),
                "refusal": assignment_refusal(issue, args.sender, args.owner) or ""}
    json.dump(plan, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
