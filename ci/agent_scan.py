#!/usr/bin/env python3
"""Turn a read-only code scan into a bounded, de-duplicated list of issues to file.

The scanning model only reads the checkout and returns its findings. This script plans what
it looks at (areas with recent commits first, then the rotation) and decides what reaches the
public issue tracker: findings must name a file that exists, fit the size limits, and not
repeat an issue filed before, even reworded (open or closed, so a finding the owner rejected
stays rejected).
A backlog of unanswered scan issues pauses the scan. Stdlib only, like the rest of ci/.
"""
import argparse
import difflib
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_LABEL = "自動スキャン"
TRIAGE_LABEL = "要トリアージ"
MAX_ISSUES = 3
# Open scan issues nobody has triaged yet; at this many the scan waits instead of piling on.
MAX_OPEN = 20
AREAS_PER_RUN = 3
# Of those, how many go to the areas changed most in the last RECENT_DAYS; the rest rotate.
RECENT_AREAS = 2
RECENT_DAYS = 7
MAX_RECENT_FILES = 40
MAX_TITLE = 80
MAX_TEXT = 1500
# Packages beside mcs/ that hold runtime code; mcs/ contributes one area per subpackage.
TOP_LEVEL_AREAS = ("adapters", "hermes_plugin", "lineworks_adapter", "mcs_standalone")
_MARKER = re.compile(r"<!-- agent-scan:([0-9a-f]{12}) -->")
_PLACE = re.compile(r"^## 場所\n`([^`\n]+)`(?: (\d+) 行目付近)?", re.M)
TITLE_PREFIX = "スキャン: "
# Two titles about one file this alike are the same finding reworded. Same or reworded, a finding
# is another one when both name a line and the lines are further apart than SAME_PLACE.
SAME_TITLE = 0.75
SAME_PLACE = 40


def areas(root=ROOT):
    """Scan areas in a stable order: each mcs/ subpackage, then the other runtime packages."""
    found = [f"mcs/{path.name}" for path in sorted((root / "mcs").iterdir())
             if path.is_dir() and any(path.rglob("*.py"))]
    return found + [name for name in TOP_LEVEL_AREAS if (root / name).is_dir()]


def _area_of(path, known):
    """The scan area holding `path`, or None."""
    return next((area for area in known if path.startswith(area + "/")), None)


def recent_files(root=ROOT, days=RECENT_DAYS):
    """Files committed in the last `days` days that still exist, most often changed first."""
    log = subprocess.run(
        ["git", "-C", str(root), "log", f"--since={days}.days", "--name-only", "--pretty=format:"],
        capture_output=True, text=True, check=True).stdout
    counts = {}
    for path in filter(None, log.splitlines()):
        if (root / path).is_file():
            counts[path] = counts.get(path, 0) + 1
    return sorted(counts, key=lambda path: (-counts[path], path))


def plan(run_number, changed, open_issues=0, requested="", root=ROOT):
    """What this run scans: {"areas": [...], "recent": [...]}; no areas while the backlog is full.

    A requested area is scanned alone. Otherwise the areas with the most recently changed
    files come first and the rotation fills the rest, so quiet areas are still reached.
    """
    known = areas(root)
    if requested:
        if requested not in known:
            raise ValueError(f"unknown area {requested!r}; choose one of: {', '.join(known)}")
        chosen = [requested]
    elif open_issues >= MAX_OPEN:
        chosen = []
    else:
        touched = [_area_of(path, known) for path in changed]
        chosen = sorted({area for area in touched if area}, key=lambda area: (-touched.count(area), area))[:RECENT_AREAS]
        offset = run_number * (AREAS_PER_RUN - RECENT_AREAS)
        rotation = [known[(offset + step) % len(known)] for step in range(len(known))]
        chosen += [area for area in rotation if area not in chosen][:AREAS_PER_RUN - len(chosen)]
    recent = [path for path in changed if _area_of(path, chosen)][:MAX_RECENT_FILES]
    return {"areas": chosen, "recent": recent}


def open_count(issues):
    """Scan issues still open (the REST API and gh spell the state in different cases)."""
    return sum(1 for issue in issues if str(issue.get("state", "")).lower() == "open")


def fingerprint(path, title):
    """Identity of a finding across runs: the file plus the title without spacing or case."""
    key = path + "\n" + _key(title)
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _key(title):
    return "".join(title.split()).lower()


def filed(issues):
    """(fingerprints, path, title key, line or None) of each scan issue already in the tracker."""
    found = []
    for issue in issues:
        body = issue.get("body") or ""
        place = _PLACE.search(body)
        path, line = (place.group(1), int(place.group(2)) if place.group(2) else None) if place else (None, None)
        found.append((set(_MARKER.findall(body)), path, _key((issue.get("title") or "").removeprefix(TITLE_PREFIX)), line))
    return found


def _filed_before(mark, path, title, line, earlier):
    """True when an `earlier` finding is this one: the same or a reworded title, at the same place."""
    return any(
        (line is None or other_line is None or abs(line - other_line) <= SAME_PLACE)
        and (mark in marks or (path == other_path
                               and difflib.SequenceMatcher(None, _key(title), other).ratio() >= SAME_TITLE))
        for marks, other_path, other, other_line in earlier)


def _text(value, limit):
    """One trimmed string within `limit`, with mentions defused so an issue pings nobody."""
    if isinstance(value, list):
        value = "\n".join(f"- {item}" for item in value if isinstance(item, str) and item.strip())
    if not isinstance(value, str):
        return ""
    return value.strip().replace("@", "＠")[:limit]


def _repo_file(path, scanned, root):
    """`path` as a repo-relative file inside one of the `scanned` areas, or None when it is not one."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        return None
    resolved = (root / path).resolve()
    if not resolved.is_file() or not resolved.is_relative_to(root.resolve()):
        return None
    relative = resolved.relative_to(root.resolve()).as_posix()
    return relative if _area_of(relative, scanned) else None


def issues_to_file(findings, existing, scanned, root=ROOT):
    """Issues ({title, body}) for the valid, new findings, within the per-run and backlog limits."""
    earlier = filed(existing)
    limit = min(MAX_ISSUES, MAX_OPEN - open_count(existing))
    issues = []
    for finding in findings if isinstance(findings, list) else []:
        if len(issues) >= limit:
            break
        if not isinstance(finding, dict):
            continue
        path = _repo_file(finding.get("path"), scanned, root)
        title = " ".join(_text(finding.get("title"), MAX_TITLE).split())
        problem = _text(finding.get("problem"), MAX_TEXT)
        evidence = _text(finding.get("evidence"), MAX_TEXT)
        if not (path and title and problem and evidence):
            continue
        line = finding.get("line")
        line = line if isinstance(line, int) and not isinstance(line, bool) and line > 0 else None
        mark = fingerprint(path, title)
        if _filed_before(mark, path, title, line, earlier):
            continue
        earlier.append(({mark}, path, _key(title), line))
        place = f"`{path}`" + (f" {line} 行目付近" if line else "")
        body = [
            f"自動スキャン（対象 `{_area_of(path, scanned)}`、読み取り専用）が見つけた点です。まだ誰も確かめていません。",
            "内容を確かめ、進めるなら会話で詰めてから担当を決めます。当たらなければ閉じてください（同じ指摘は再起票されません）。",
            "", "## 場所", place, "", "## 問題", problem, "", "## 根拠", evidence,
        ]
        suggestion = _text(finding.get("suggestion"), MAX_TEXT)
        if suggestion:
            body += ["", "## 直し方の案", suggestion]
        body += ["", f"<!-- agent-scan:{mark} -->"]
        issues.append({"title": TITLE_PREFIX + title, "body": "\n".join(body)})
    return issues


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    planner = commands.add_parser("plan", help="print what this run scans as JSON")
    planner.add_argument("--run-number", type=int, required=True)
    planner.add_argument("--existing", required=True)
    planner.add_argument("--requested", default="")
    issues = commands.add_parser("issues", help="print the issues to file as JSON")
    issues.add_argument("--plan", required=True)
    issues.add_argument("--result", required=True, help="the scan's structured output: {\"findings\": [...]}")
    issues.add_argument("--existing", required=True)
    args = parser.parse_args(argv)
    try:
        existing = _load(args.existing)
        if not isinstance(existing, list):
            raise ValueError("existing is not a list")
        if args.command == "plan":
            result = plan(args.run_number, recent_files(), open_count(existing), args.requested)
        else:
            scanned = _load(args.plan)["areas"]
            findings = _load(args.result)["findings"]
            if not scanned or not set(scanned) <= set(areas()) or not isinstance(findings, list):
                raise ValueError("unknown area, or findings is not a list")
            result = issues_to_file(findings, existing, scanned)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        # An unreadable plan or result is a failed scan; it did not find the code clean.
        print(f"scan unusable: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
