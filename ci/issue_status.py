#!/usr/bin/env python3
"""Render one auto-updated progress comment per issue for the AI implementation, from GitHub API data.

`.github/workflows/issue-status.yml` saves API responses with `gh` into one
work directory and writes the comment this script plans; the script itself
makes no network call. Standard library only. Every run recomputes the whole
comment from the current state, so a missed event is fixed by the next run.

  plan          issues to refresh (number and chosen PR), optionally narrowed
                by hints: "#N" (issue or PR) and 40-hex head SHAs
  failed-ci-run id of the failed ci run on the chosen PR head, for its job names
  render        the comment body and whether it must be written

Work directory: open.json, closed.json, pulls.json, claude_runs.json and
repair_runs.json for the repository; <issue>/events.json, comments.json,
pr.json, ci_runs.json, ci_jobs.json, status.json and reviews.json per issue.
See docs/development/AUTOMATION.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ci/ is this script's import root
from agent_control import AGENT_LABEL_PREFIX, ATTEMPT_PREFIX, OWNER_LABEL, linked_issues

MARKER = "<!-- issue-status -->"
BOT = "github-actions[bot]"  # author of comments written with GITHUB_TOKEN
CLOSED_WINDOW = timedelta(days=2)
CI_PATH = ".github/workflows/ci.yml"
HOLD_LABEL = "保留"
FAILURE_PREFIX = "失敗:"
VERDICTS = ("APPROVED", "CHANGES_REQUESTED")
JST = timezone(timedelta(hours=9))
_SEAT = re.compile(r"^\s*独立レビュー（([^、）\s]+)")
_HINT_NUMBER = re.compile(r"#(\d+)")
_HINT_SHA = re.compile(r"\b[0-9a-f]{40}\b")
_RUN_NUMBER = re.compile(r"#(\d+)")


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _jst(value):
    moment = _time(value)
    return moment.astimezone(JST).strftime("%m/%d %H:%M") if moment else ""


def _short(sha):
    return (sha or "")[:7]


def _cell(text):
    # Table cells: one line, no column breaks, no mention that would ping someone.
    return str(text).replace("\n", " ").replace("|", "\\|").replace("@", "@​")


def _labels(issue):
    return [label["name"] for label in issue.get("labels", [])]


def agent_label(issue):
    return next((name for name in _labels(issue) if name.startswith(AGENT_LABEL_PREFIX)), None)


def _links(pulls):
    """issue number -> PRs whose body closes it (Fixes #N and the like)."""
    links = {}
    for pr in pulls:
        for number in linked_issues(pr.get("body")):
            links.setdefault(number, []).append(pr)
    return links


def choose_pr(prs):
    """The PR that tells the issue's progress: the latest open one, else the latest merged, else the latest."""
    for pick in (lambda p: p.get("state") == "open", lambda p: p.get("merged_at"), lambda p: True):
        found = [p for p in prs if pick(p)]
        if found:
            return max(found, key=lambda p: p.get("merged_at") or p.get("updated_at") or "")
    return None


def plan_targets(issues, pulls, hints="", now=None):
    """[{issue, pr}] for open issues with a 担当:* label or a linked PR, plus ones closed within CLOSED_WINDOW."""
    now = now or datetime.now(timezone.utc)
    links = _links(pulls)
    numbers = {int(n) for n in _HINT_NUMBER.findall(hints or "")}
    shas = set(_HINT_SHA.findall(hints or ""))
    targets = []
    for issue in sorted({i["number"]: i for i in issues if "pull_request" not in i}.values(),
                        key=lambda i: i["number"]):
        prs = links.get(issue["number"], [])
        # A direct "#N" also refreshes an issue that just lost its 担当:* label; render writes only over an old comment.
        if not (agent_label(issue) or prs or issue["number"] in numbers):
            continue
        if issue.get("state") != "open" and now - _time(issue.get("closed_at") or issue["updated_at"]) > CLOSED_WINDOW:
            continue
        if (numbers or shas) and not (issue["number"] in numbers or any(
                p["number"] in numbers or p["head"]["sha"] in shas for p in prs)):
            continue
        pr = choose_pr(prs)
        targets.append({"issue": issue["number"], "pr": pr["number"] if pr else None})
    return targets


def latest_ci_run(runs, head_sha):
    found = [r for r in runs if r.get("path") == CI_PATH and r.get("head_sha") == head_sha]
    return max(found, key=lambda r: (r.get("created_at") or "", r.get("run_attempt") or 0)) if found else None


def _agent_run(runs, numbers):
    """The latest non-skipped run whose name carries one of `numbers` as "#N" (see run-name in the workflows)."""
    found = []
    for run in runs:
        match = _RUN_NUMBER.search(run.get("display_title") or "")
        if match and int(match.group(1)) in numbers and run.get("conclusion") != "skipped":
            found.append(run)
    return max(found, key=lambda r: r.get("created_at") or "") if found else None


def _active(run):
    return bool(run) and run.get("status") != "completed"


def owner_verdict(reviews, owner):
    """The owner's latest APPROVED or CHANGES_REQUESTED review; COMMENTED and other authors never decide."""
    found = [r for r in reviews if r.get("state") in VERDICTS and (r.get("user") or {}).get("login") == owner]
    return max(found, key=lambda r: r.get("submitted_at") or "") if found else None


def review_seat(review):
    """The seat name from 「独立レビュー（<席名>、作者でない席）: …」, or None."""
    match = _SEAT.match(review.get("body") or "")
    return match.group(1) if match else None


def _link(text, url):
    return f"[{text}]({url})" if url else text


def decide(bundle, owner, default_branch="main"):
    """(rows, notes) for one issue. A row is (state, name, time, evidence); state is done, active or todo."""
    issue = bundle["issue"]
    pr = bundle.get("pr")
    labels = _labels(issue)
    numbers = {issue["number"]} | ({pr["number"]} if pr else set())
    claude_run = _agent_run(bundle.get("claude_runs", []), numbers)
    rows = [("done", "起票", issue.get("created_at"), _link(f"#{issue['number']}", issue.get("html_url")))]

    # The current label decides; events only date it (a removed label is no assignment).
    agent = agent_label(issue)
    dates = [e.get("created_at") or "" for e in bundle.get("events", [])
             if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == agent]
    if agent:
        rows.append(("done", "担当決定", max(dates, default=None), agent))
    else:
        rows.append(("todo", "担当決定", None, ""))

    run_link = _link(f"run {claude_run['id']}", claude_run.get("html_url")) if claude_run else ""
    if pr:
        rows.append(("done", "実装中", claude_run.get("created_at") if claude_run else None, run_link))
    elif agent:
        rows.append(("active", "実装中", claude_run.get("created_at") if claude_run else None,
                     run_link or "実行状況はこの担当から取得しない"))
    else:
        rows.append(("todo", "実装中", None, ""))
    if not pr:
        rows += [("todo", name, None, "") for name in ("PR 作成", "CI", "マージ判定", "レビュー", "マージ")]
        return _finish(rows, issue, labels, pr, bundle.get("others", []))

    head = pr["head"]["sha"]
    rows.append(("done", "PR 作成", pr.get("created_at"),
                 _link(f"#{pr['number']}", pr.get("html_url")) + f" head {_short(head)}"))

    ci = latest_ci_run(bundle.get("ci_runs", []), head)
    attempt = next((name for name in labels if name.startswith(ATTEMPT_PREFIX)), "")
    if ci is None:
        rows.append(("active", "CI 実行待ち", None, f"head {_short(head)} の ci はまだない"))
    elif _active(ci):
        rows.append(("active", "CI 実行中", ci.get("created_at"), _link(f"ci {ci['id']}", ci.get("html_url"))))
    elif ci.get("conclusion") == "success":
        rows.append(("done", "CI 成功", ci.get("updated_at"), _link(f"ci {ci['id']}", ci.get("html_url"))))
    else:
        failed = [j["name"] for j in bundle.get("ci_jobs", [])
                  if j.get("conclusion") not in ("success", "skipped", "neutral", None)]
        detail = ", ".join(failed[:5]) + (" ほか" if len(failed) > 5 else "")
        rows.append(("active", "CI 失敗" + (f"（{attempt}）" if attempt else ""), ci.get("updated_at"),
                     _link(f"ci {ci['id']}", ci.get("html_url")) + (f" {detail}" if detail else "")))
        repair = _agent_run(bundle.get("repair_runs", []), {pr["number"]})
        if _active(repair):
            rows.append(("active", "自動修正中", repair.get("created_at"),
                         _link(f"run {repair['id']}", repair.get("html_url"))))

    gate = next((s for s in (bundle.get("status") or {}).get("statuses", []) if s.get("context") == "merge-gate"), None)
    if gate is None:
        rows.append(("todo", "マージ判定", None, ""))
    else:
        rows.append(("done" if gate.get("state") == "success" else "active", "マージ判定", gate.get("updated_at"),
                     f"{gate.get('state')}: {gate.get('description') or ''}"))

    verdict = owner_verdict(bundle.get("reviews", []), owner)
    ci_ok = bool(ci) and ci.get("conclusion") == "success"
    if verdict is None:
        rows.append(("active" if ci_ok else "todo", "レビュー待ち", None, ""))
    else:
        seat = review_seat(verdict)
        at = f"（{seat}）" if seat else ""
        evidence = _link("review", verdict.get("html_url")) + f" commit {_short(verdict.get('commit_id'))}"
        changes = verdict["state"] == "CHANGES_REQUESTED"
        stale = verdict.get("commit_id") != head
        if changes:
            rows.append(("done" if stale or _active(claude_run) else "active", "修正依頼" + at,
                         verdict.get("submitted_at"), evidence))
        if changes and _active(claude_run):
            rows.append(("active", "修正中", claude_run.get("created_at"), run_link))
        elif stale:
            if changes:
                rows.append(("done", "修正中", None, f"head {_short(head)}"))
            rows.append(("active" if ci_ok else "todo", "再レビュー待ち", None,
                         f"前回{'の修正依頼' if changes else 'の承認'}{at}は {_short(verdict.get('commit_id'))}、"
                         f"head は {_short(head)}"))
        elif not changes:
            rows.append(("done", "承認" + at, verdict.get("submitted_at"), evidence))

    if pr.get("merged_at"):
        base = pr["base"]["ref"]
        if base == default_branch:
            name = f"マージ済み（{base}）"
        elif base.startswith("group/"):
            name = f"グループへ統合済み（{base}）"
        else:
            name = f"マージ済み（{base}）"
        rows.append(("done", name, pr.get("merged_at"), _link(f"#{pr['number']}", pr.get("html_url"))))
    else:
        rows.append(("todo", "マージ", None, ""))
    return _finish(rows, issue, labels, pr, bundle.get("others", []))


def _finish(rows, issue, labels, pr, others):
    if issue.get("state") == "closed":
        reason = "見送り" if issue.get("state_reason") == "not_planned" else ""
        rows.append(("done", "完了" + (f"（{reason}）" if reason else ""), issue.get("closed_at"), ""))
    elif pr and pr.get("merged_at") and pr["base"]["ref"].startswith("group/"):
        rows.append(("todo", "完了", None, "グループが main へ入ったあと所有者が閉じる"))
    else:
        rows.append(("todo", "完了", None, ""))
    if issue.get("state") == "closed" or (pr and pr.get("merged_at")):
        # Nothing still runs after a merge or close: unfinished stages are history, not the present.
        rows = [("skip" if state == "active" else state, *rest) for state, *rest in rows]
    notes = [name for name in labels if name in (OWNER_LABEL, HOLD_LABEL) or name.startswith(FAILURE_PREFIX)]
    if pr and pr.get("state") == "open":
        if pr.get("draft"):
            notes.append("下書きPR")
        if pr.get("mergeable_state") == "dirty":
            notes.append("衝突あり")
    if others:
        notes.append("ほかの PR: " + ", ".join(f"#{n}" for n in others))
    return rows, notes


def current_index(rows):
    active = [i for i, row in enumerate(rows) if row[0] == "active"]
    if active:
        return active[0]
    return max(i for i, row in enumerate(rows) if row[0] == "done")


def render_body(rows, notes):
    now = current_index(rows)
    lines = [MARKER, "### AI 実装の進み具合", "", f"**現在: {_cell(rows[now][1])}**", ""]
    if notes:
        lines += ["注意: " + _cell("・".join(notes)), ""]
    lines += ["| | 段階 | 時刻（JST） | 根拠 |", "| --- | --- | --- | --- |"]
    marks = {"done": "済", "active": "", "todo": "", "skip": "—"}
    for i, (state, name, at, evidence) in enumerate(rows):
        mark = "**現在**" if i == now else marks[state]
        label = f"**{_cell(name)}**" if i == now else _cell(name)
        lines.append(f"| {mark} | {label} | {_jst(at)} | {_cell(evidence)} |")
    lines += ["", "このコメントは issue-status.yml が自動で書き換えます。"]
    return "\n".join(lines) + "\n"


def own_comment(comments):
    """The status comment this workflow wrote; a pasted marker from anyone else is not it."""
    return next((c for c in comments if (c.get("user") or {}).get("login") == BOT
                 and (c.get("body") or "").startswith(MARKER)), None)


def comment_plan(body, comments, tracked=True):
    """Edit our comment when the body changed; start one only for an issue with an assignee or a PR."""
    existing = own_comment(comments)
    old = (existing or {}).get("body", "").replace("\r\n", "\n").strip()
    write = old != body.strip() and (tracked or existing is not None)
    return {"comment_id": existing["id"] if existing else None, "write": write, "body": body}


def _flatten(data):
    if data and isinstance(data, list) and isinstance(data[0], list):
        return [item for page in data for item in page]
    return data


def _read(path, default):
    return _flatten(json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else default


def _issues(work):
    return _read(work / "open.json", []) + _read(work / "closed.json", [])


def load_bundle(work, number):
    issue = next(i for i in _issues(work) if i["number"] == number and "pull_request" not in i)
    prs = _links(_read(work / "pulls.json", [])).get(number, [])
    chosen = choose_pr(prs)
    d = work / str(number)
    return {
        "issue": issue,
        "events": _read(d / "events.json", []),
        "comments": _read(d / "comments.json", []),
        "pr": _read(d / "pr.json", None),
        "others": sorted(p["number"] for p in prs if chosen and p["number"] != chosen["number"]),
        "ci_runs": _read(d / "ci_runs.json", []),
        "ci_jobs": _read(d / "ci_jobs.json", []),
        "status": _read(d / "status.json", None),
        "reviews": _read(d / "reviews.json", []),
        "claude_runs": _read(work / "claude_runs.json", []),
        "repair_runs": _read(work / "repair_runs.json", []),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--work", type=Path, required=True)
    plan.add_argument("--hints", default="")
    failed = sub.add_parser("failed-ci-run")
    failed.add_argument("--work", type=Path, required=True)
    failed.add_argument("--issue", type=int, required=True)
    render = sub.add_parser("render")
    render.add_argument("--work", type=Path, required=True)
    render.add_argument("--issue", type=int, required=True)
    render.add_argument("--owner", required=True)
    render.add_argument("--default-branch", default="main")
    args = parser.parse_args(argv)
    if args.command == "plan":
        out = plan_targets(_issues(args.work), _read(args.work / "pulls.json", []), args.hints)
    elif args.command == "failed-ci-run":
        bundle = load_bundle(args.work, args.issue)
        run = latest_ci_run(bundle["ci_runs"], bundle["pr"]["head"]["sha"]) if bundle["pr"] else None
        failed_run = run and run.get("status") == "completed" and run.get("conclusion") != "success"
        sys.stdout.write(f"{run['id'] if failed_run else ''}\n")
        return 0
    else:
        bundle = load_bundle(args.work, args.issue)
        out = comment_plan(render_body(*decide(bundle, args.owner, args.default_branch)), bundle["comments"],
                           bool(agent_label(bundle["issue"]) or bundle["pr"]))
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
