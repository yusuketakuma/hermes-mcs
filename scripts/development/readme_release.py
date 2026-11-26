#!/usr/bin/env python3
"""READMEの対象版を生成し、リリースごとの内容見直しを検査する。"""

import argparse
import json
from pathlib import Path
import re
import sys

import release_notes


ROOT = Path(__file__).resolve().parents[2]
BEGIN = "<!-- BEGIN GENERATED:release -->"
END = "<!-- END GENERATED:release -->"
REVIEW_SECTIONS = ("features", "demos", "quickstart", "safety", "docs")


def latest(changelog):
    versions = release_notes.check_changelog(changelog)
    if not versions:
        raise ValueError("READMEに表示するversionがありません")
    return versions[0]


def render_block(changelog):
    return (f"対象版 **v{latest(changelog)}**。"
            "[変更履歴と版ごとの更新時の注意](CHANGELOG.md) · "
            "[GitHub Releases](https://github.com/yusuketakuma/hermes-mcs/releases)")


def render(readme, changelog):
    if readme.count(BEGIN) != 1 or readme.count(END) != 1:
        raise ValueError("READMEのreleaseマーカーは開始・終了が各1個必要です")
    before, rest = readme.split(BEGIN, 1)
    if END not in rest:
        raise ValueError("READMEのreleaseマーカーの順番が不正です")
    _, after = rest.split(END, 1)
    return f"{before}{BEGIN}\n\n{render_block(changelog)}\n\n{END}{after}"


def local_file(root, value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("参照パスが不正です")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("参照はリポジトリ内の相対パスで指定してください")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError(f"参照ファイルがありません: {value}")
    return resolved


def check_review(root, version):
    review = json.loads((root / "docs/development/readme-review.json").read_text(encoding="utf-8"))
    if not isinstance(review, dict) or set(review) != {"version", "sections"}:
        raise ValueError("READMEの見直し記録の項目が不正です")
    if review["version"] != version:
        raise ValueError(f"READMEの各項目を見直し、readme-review.jsonをv{version}へ更新してください")
    sections = review["sections"]
    if not isinstance(sections, dict) or set(sections) != set(REVIEW_SECTIONS):
        raise ValueError("READMEの機能・画面例・導入・安全・導線の見直し記録が必要です")
    for name, entry in sections.items():
        if not isinstance(entry, dict) or set(entry) != {"notes", "sources"}:
            raise ValueError(f"README見直し項目が不正です: {name}")
        if not isinstance(entry["notes"], str) or not entry["notes"].strip():
            raise ValueError(f"見直した内容を記録してください: {name}")
        sources = entry["sources"]
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"確認したソースを記録してください: {name}")
        for source in sources:
            local_file(root, source)


def check_links(root, readme):
    for match in release_notes.IMAGES.finditer(readme):
        path = release_notes.repository_image_path(match["url"])
        if path is not None:
            local_file(root, path)
    anchors = set(re.findall(r'<a (?:name|id)="([^"]+)"\s*>', readme))
    targets = re.findall(r'\[[^\]\n]*\]\(([^\s)]+)\)', readme)
    for target in targets:
        if target.startswith(("https://", "http://", "mailto:")):
            continue
        path, _, fragment = target.partition("#")
        if not path:
            if fragment not in anchors:
                raise ValueError(f"READMEのリンク先見出しがありません: {target}")
        else:
            local_file(root, path)
    if readme.count("<details") != readme.count("</details>"):
        raise ValueError("READMEの折りたたみタグの数が一致しません")


def check(root, requested_version=None):
    readme = (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    version = latest(changelog)
    if requested_version is not None and requested_version != version:
        raise ValueError("README・CHANGELOG・リリースtagのversionが一致しません")
    if render(readme, changelog) != readme:
        raise ValueError("READMEの対象版が古くなっています: scripts/development/readme_release.pyを実行してください")
    check_review(root, version)
    check_links(root, readme)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--version")
    args = parser.parse_args(argv)
    try:
        if args.check:
            check(args.root, args.version)
        else:
            if args.version is not None:
                raise ValueError("--versionは--checkと組み合わせて指定してください")
            path = args.root / "README.md"
            text = render(path.read_text(encoding="utf-8"),
                          (args.root / "CHANGELOG.md").read_text(encoding="utf-8"))
            check_links(args.root, text)
            path.write_text(text, encoding="utf-8")
        return 0
    except (ValueError, OSError, RecursionError) as exc:
        print(f"readme: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
