#!/usr/bin/env python3
"""CHANGELOGを正本として既存GitHub Releaseのタイトル・本文だけを同期する。"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from release_notes import check_changelog, release_title, section


ROOT = Path(__file__).resolve().parents[2]
IDENTITY = ("id", "tag_name", "draft", "prerelease", "target_commitish",
            "created_at", "published_at")


def api(repo, endpoint, payload=None):
    # Only the repo-relative releases endpoint is accepted; no arbitrary URL.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("repositoryはowner/nameで指定してください")
    if not re.fullmatch(r"releases(?:/[1-9][0-9]*|\?per_page=100&page=[1-9][0-9]*)", endpoint):
        raise ValueError("releases以外のAPIは使用できません")
    command = ["gh", "api", "--hostname", "github.com", f"repos/{repo}/{endpoint}",
               "--method", "PATCH" if payload is not None else "GET"]
    if payload is not None:
        if set(payload) != {"name", "body"}:
            raise ValueError("更新できるのはnameとbodyのみです")
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, check=True, timeout=60)
    return json.loads(result.stdout)


def synchronize(text, request, apply=False):
    versions = set(check_changelog(text))
    releases = []
    for page in range(1, 101):
        chunk = request(f"releases?per_page=100&page={page}")
        if not isinstance(chunk, list):
            raise ValueError("Release一覧の形式が不正です")
        releases.extend(chunk)
        if len(chunk) < 100:
            break
    else:
        raise ValueError("Release一覧がページ上限を超えました")
    plan, seen = [], set()
    for release in releases:
        tag = release["tag_name"]
        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
            continue  # prerelease tags are outside this stable-release policy.
        if tag in seen or tag[1:] not in versions:
            raise ValueError(f"{tag}: 重複またはCHANGELOGの対応版がありません")
        if type(release["id"]) is not int or release["id"] <= 0:
            raise ValueError("Release IDが不正です")
        seen.add(tag)
        body = section(text, tag[1:])
        desired = {"name": release_title(body, tag[1:]), "body": body}
        if any((release.get(k) or "") != v for k, v in desired.items()):
            plan.append((release, desired))
    # Preflight all releases before the first write.
    results = []
    for old, desired in plan:
        if apply:
            endpoint = f"releases/{old['id']}"
            current = request(endpoint)
            if any(current.get(k) != old.get(k)
                   for k in (*IDENTITY, "name", "body", "updated_at")):
                raise ValueError(f"{old['tag_name']}: 取得後に変更されたため停止します")
            updated = request(endpoint, desired)
            if any(updated.get(k) != old.get(k) for k in IDENTITY):
                raise ValueError("Releaseの識別・公開状態が変わったため停止します")
            verified = request(endpoint)
            if any(verified.get(k) != v for k, v in desired.items()):
                raise ValueError(f"{old['tag_name']}: 更新後の本文が一致しません")
        results.append(old["tag_name"])
    return {"mode": "apply" if apply else "dry-run", "releases_checked": len(seen),
            "updated" if apply else "would_update": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repoまたはGITHUB_REPOSITORYが必要です")
    try:
        result = synchronize((args.root / "CHANGELOG.md").read_text(encoding="utf-8"),
                             lambda endpoint, payload=None: api(args.repo, endpoint, payload),
                             apply=args.apply)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # Do not print request payloads or subprocess output (which may include secrets).
        parser.exit(1, f"release sync: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
