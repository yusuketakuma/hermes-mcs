#!/usr/bin/env python3
"""Turn a read-only code scan into a bounded, de-duplicated list of issues to file.

The scanning model only reads the checkout and writes a findings file. This script picks the
area it looks at, then decides what reaches the public issue tracker: findings must name a
file that exists, fit the size limits, and not repeat an issue filed before (open or closed,
so a finding the owner rejected stays rejected). Stdlib only, like the rest of ci/.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_LABEL = "自動スキャン"
TRIAGE_LABEL = "要トリアージ"
MAX_ISSUES = 3
MAX_TITLE = 80
MAX_TEXT = 1500
# Packages beside mcs/ that hold runtime code; mcs/ contributes one area per subpackage.
TOP_LEVEL_AREAS = ("adapters", "hermes_plugin", "lineworks_adapter", "mcs_standalone")
_MARKER = re.compile(r"<!-- agent-scan:([0-9a-f]{12}) -->")


def areas(root=ROOT):
    """Scan areas in a stable order: each mcs/ subpackage, then the other runtime packages."""
    found = [f"mcs/{path.name}" for path in sorted((root / "mcs").iterdir())
             if path.is_dir() and any(path.rglob("*.py"))]
    return found + [name for name in TOP_LEVEL_AREAS if (root / name).is_dir()]


def pick_area(run_number, requested="", root=ROOT):
    """The requested area when it is a known one, else the next area in rotation."""
    known = areas(root)
    if requested:
        if requested not in known:
            raise ValueError(f"unknown area {requested!r}; choose one of: {', '.join(known)}")
        return requested
    return known[run_number % len(known)]


def fingerprint(path, title):
    """Identity of a finding across runs: the file plus the title without spacing or case."""
    key = path + "\n" + "".join(title.split()).lower()
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def filed_fingerprints(issues):
    """Fingerprints of the scan issues already in the tracker."""
    return {mark for issue in issues for mark in _MARKER.findall(issue.get("body") or "")}


def _text(value, limit):
    """One trimmed string within `limit`, with mentions defused so an issue pings nobody."""
    if isinstance(value, list):
        value = "\n".join(f"- {item}" for item in value if isinstance(item, str) and item.strip())
    if not isinstance(value, str):
        return ""
    return value.strip().replace("@", "＠")[:limit]


def _repo_file(path, area, root):
    """`path` as a repo-relative file inside `area`, or None when it is not one."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        return None
    resolved = (root / path).resolve()
    if not resolved.is_file() or not resolved.is_relative_to((root / area).resolve()):
        return None
    return resolved.relative_to(root.resolve()).as_posix()


def issues_to_file(findings, existing, area, root=ROOT, limit=MAX_ISSUES):
    """Issues ({title, body}) for the valid, new findings, at most `limit` of them."""
    seen = filed_fingerprints(existing)
    issues = []
    for finding in findings if isinstance(findings, list) else []:
        if len(issues) == limit or not isinstance(finding, dict):
            break
        path = _repo_file(finding.get("path"), area, root)
        title = " ".join(_text(finding.get("title"), MAX_TITLE).split())
        problem = _text(finding.get("problem"), MAX_TEXT)
        evidence = _text(finding.get("evidence"), MAX_TEXT)
        if not (path and title and problem and evidence):
            continue
        mark = fingerprint(path, title)
        if mark in seen:
            continue
        seen.add(mark)
        line = finding.get("line")
        place = f"`{path}`" + (f" {line} 行目付近" if isinstance(line, int) and line > 0 else "")
        body = [
            f"自動スキャン（対象 `{area}`、読み取り専用）が見つけた点です。まだ誰も確かめていません。",
            "内容を確かめ、進めるなら会話で詰めてから担当を決めます。当たらなければ閉じてください（同じ指摘は再起票されません）。",
            "", "## 場所", place, "", "## 問題", problem, "", "## 根拠", evidence,
        ]
        suggestion = _text(finding.get("suggestion"), MAX_TEXT)
        if suggestion:
            body += ["", "## 直し方の案", suggestion]
        body += ["", f"<!-- agent-scan:{mark} -->"]
        issues.append({"title": f"スキャン: {title}", "body": "\n".join(body)})
    return issues


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    area = commands.add_parser("area", help="print the area this run scans")
    area.add_argument("--run-number", type=int, required=True)
    area.add_argument("--requested", default="")
    issues = commands.add_parser("issues", help="print the issues to file as JSON")
    issues.add_argument("--findings", required=True)
    issues.add_argument("--existing", required=True)
    issues.add_argument("--area", required=True)
    args = parser.parse_args(argv)
    if args.command == "area":
        try:
            print(pick_area(args.run_number, args.requested))
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        return 0
    # A missing or malformed findings file means the scan found nothing it could state.
    print(json.dumps(issues_to_file(_load(args.findings, []), _load(args.existing, []), args.area),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
