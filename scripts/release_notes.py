#!/usr/bin/env python3
"""日本語の変更記録からCHANGELOGとRelease本文を決定的に生成する。"""

import argparse
import datetime
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    "added": "新機能",
    "changed": "改善",
    "fixed": "不具合修正",
    "breaking": "動作・設定の変更",
}
HEADINGS = [*GROUPS.values(), "更新時の注意", "技術詳細"]
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def version(value):
    if not VERSION.fullmatch(value):
        raise ValueError("version は X.Y.Z 形式で指定してください")
    return value


def validate(item):
    required = {"category", "title", "summary", "upgrade", "details", "refs"}
    if not isinstance(item, dict) or set(item) != required:
        raise ValueError("変更記録の項目が不正です")
    if not isinstance(item["category"], str) or item["category"] not in {*GROUPS, "security"}:
        raise ValueError("category が不正です")
    for name, limit in (("title", 70), ("summary", 280), ("upgrade", 500)):
        value = item[name]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} は1〜{limit}文字で必須です")
        if "\n" in value or "\r" in value or "<" in value or ">" in value:
            raise ValueError(f"{name} に改行・HTMLは使用できません")
    for name in ("details", "refs"):
        if not isinstance(item[name], list) or not all(
            isinstance(x, str) and x.strip() and "\n" not in x and "\r" not in x
            and "<" not in x and ">" not in x for x in item[name]
        ):
            raise ValueError(f"{name} は1行文字列の配列です")
    if not item["refs"] or not all(
        re.fullmatch(r"#[1-9][0-9]*|[0-9a-f]{7,40}|[A-Za-z0-9_./-]+", x)
        for x in item["refs"]
    ):
        raise ValueError("refs にPR番号・commit SHA・ソースパスを記録してください")
    return item


def load_fragments(root):
    return [(p, validate(json.loads(p.read_text(encoding="utf-8"))))
            for p in sorted((root / "changes").glob("*.json"))]


def section(text, release_version):
    version(release_version)
    headings = list(re.finditer(r"^## \[([^\]]+)\].*$", text, re.M))
    matches = [(i, h) for i, h in enumerate(headings)
               if h.group(1) == release_version]
    if len(matches) != 1:
        raise ValueError("CHANGELOGの対象versionが未記録または重複しています")
    i, heading = matches[0]
    end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
    body = text[heading.end():end].strip()
    if not body:
        raise ValueError("Release本文が空です")
    return body + "\n"


def release_title(body, release_version):
    version(release_version)
    first = body.splitlines()[0] if body else ""
    headline = first[2:-2] if first.startswith("**") and first.endswith("**") else ""
    if not headline or len(headline) > 100:
        raise ValueError("Release冒頭は100文字以内の太字の見出しにしてください")
    return f"v{release_version} — {headline}"


def check_body(body, release_version):
    release_title(body, release_version)
    if body.count("<details>") != 1 or body.count("</details>") != 1:
        raise ValueError("技術詳細の折りたたみは1個必要です")
    if body.index("<details>") > body.index("</details>"):
        raise ValueError("技術詳細の折りたたみ順が不正です")
    matches = list(re.finditer(r"^### (.+)$", body, re.M))
    labels = [m.group(1) for m in matches]
    if len(labels) != len(set(labels)) or any(x not in HEADINGS for x in labels):
        raise ValueError("段落の見出しが不正または重複しています")
    if labels != sorted(labels, key=HEADINGS.index):
        raise ValueError("段落の順番が不正です")
    if labels[-2:] != ["更新時の注意", "技術詳細"] or len(labels) < 3:
        raise ValueError("変更内容・更新時の注意・技術詳細が必要です")
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[m.end():end]
        if not content.startswith("\n\n") or not content.strip():
            raise ValueError("見出しの後は空行と内容が必要です")
        if m.group(1) != "技術詳細" and not re.search(r"^- \S", content, re.M):
            raise ValueError("空の分類段落は記載できません")
        if m.group(1) in GROUPS.values():
            bullets = re.findall(r"^- (.+)$", content, re.M)
            if any(not re.fullmatch(r"\*\*.+\*\*", x) for x in bullets):
                raise ValueError("変更項目は太字のタイトルと次行の説明で記載してください")
    if not body[matches[-1].end():].lstrip().startswith("<details>"):
        raise ValueError("技術詳細は折りたたみに記載してください")


def check_changelog(text):
    headers = list(re.finditer(r"^## \[([^\]]+)\](.*)$", text, re.M))
    names = [h.group(1) for h in headers]
    if not names or names[0] != "Unreleased" or len(names) != len(set(names)):
        raise ValueError("Unreleasedが先頭に1個必要で、versionは重複できません")
    versions = names[1:]
    for h in headers[1:]:
        version(h.group(1))
        match = re.fullmatch(r" — ([0-9]{4}-[0-9]{2}-[0-9]{2})", h.group(2))
        if not match:
            raise ValueError("各版の見出しにISO形式の日付が必要です")
        datetime.date.fromisoformat(match.group(1))
        check_body(section(text, h.group(1)), h.group(1))
    if versions != sorted(versions, key=lambda v: tuple(map(int, v.split("."))), reverse=True):
        raise ValueError("versionは新しい順に記載してください")
    return versions


def render(items, headline, summary):
    if not headline.strip() or not summary.strip() or len(headline) > 100:
        raise ValueError("headline・summaryは必須です。headlineは100文字以内です")
    if any(x in headline + summary for x in ("\n", "\r", "<", ">")):
        raise ValueError("headline・summaryはHTMLなしの1行で指定してください")
    out = [f"**{headline}**", "", summary, ""]
    upgrades = list(dict.fromkeys(item["upgrade"] for item in items))
    critical = [item for item in items if item["category"] in ("security", "breaking")]
    if critical:
        out += ["> 更新前の確認：" + critical[0]["upgrade"], ""]
    for category, label in GROUPS.items():
        entries = [item for item in items if item["category"] == category
                   or (category == "fixed" and item["category"] == "security")]
        if entries:
            out += [f"### {label}", ""]
            for item in entries:
                prefix = "セキュリティ：" if item["category"] == "security" else ""
                out += [f"- **{prefix}{item['title']}**", f"  {item['summary']}", ""]
    out += ["### 更新時の注意", ""]
    for x in upgrades:
        out += [f"- {x}", ""]
    out += ["### 技術詳細", "", "<details>", "<summary>技術詳細・根拠を表示</summary>", ""]
    for item in items:
        out += [f"#### {item['title']}", ""]
        out.extend(f"- {x}" for x in item["details"])
        out += ["- 根拠: " + "、".join(item["refs"]), ""]
    out += ["</details>", ""]
    return "\n".join(out)


def require_fragment(root, base):
    # base is passed as argv, never interpolated into a shell command.
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=root, check=True, capture_output=True, text=True,
    )
    paths = result.stdout.splitlines()
    runtime = any((p.startswith(("mcs/", "hermes_plugin/", "deployment/"))
                   and not p.endswith(".md")) or p == "install.sh" for p in paths)
    new_fragment = any(re.fullmatch(r"changes/[^/]+\.json", p)
                       and (root / p).is_file() for p in paths)
    if runtime and not new_fragment:
        raise ValueError("実行時の変更にはchanges/*.jsonの日本語変更記録が必要です")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--base")
    build = sub.add_parser("build")
    build.add_argument("--version", required=True)
    build.add_argument("--date", required=True)
    build.add_argument("--headline", required=True)
    build.add_argument("--summary", required=True)
    export = sub.add_parser("export")
    export.add_argument("--version", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--title-output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            load_fragments(args.root)
            check_changelog((args.root / "CHANGELOG.md").read_text(encoding="utf-8"))
            if args.base:
                require_fragment(args.root, args.base)
        elif args.command == "export":
            body = section((args.root / "CHANGELOG.md").read_text(encoding="utf-8"),
                           args.version)
            check_body(body, args.version)
            args.output.write_text(body, encoding="utf-8")
            if args.title_output:
                args.title_output.write_text(release_title(body, args.version) + "\n", encoding="utf-8")
        else:
            version(args.version)
            datetime.date.fromisoformat(args.date)
            fragments = load_fragments(args.root)
            if not fragments:
                raise ValueError("未リリースの変更記録がありません")
            path = args.root / "CHANGELOG.md"
            text = path.read_text(encoding="utf-8")
            if re.search(rf"^## \[{re.escape(args.version)}\]", text, re.M):
                raise ValueError("既存versionは上書きできません")
            if text.count("## [Unreleased]") != 1:
                raise ValueError("Unreleased見出しは1個必要です")
            after = text.split("## [Unreleased]", 1)[1]
            unreleased = re.split(r"^## \[", after, maxsplit=1, flags=re.M)[0]
            if unreleased.strip():
                raise ValueError("手書きのUnreleasedをchangesへ移してから生成してください")
            archive = args.root / "changes" / "archive" / args.version
            if archive.exists():
                raise ValueError("archiveが既に存在します")
            body = render([item for _, item in fragments], args.headline, args.summary)
            replacement = (f"## [Unreleased]\n\n## [{args.version}] — {args.date}\n\n"
                           + body.rstrip())
            new_text = text.replace("## [Unreleased]", replacement, 1)
            check_changelog(new_text)
            archive.mkdir(parents=True)
            path.write_text(new_text, encoding="utf-8")
            for source, _ in fragments:
                source.rename(archive / source.name)
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"release notes: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
