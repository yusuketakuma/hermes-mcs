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


def release_record(value):
    """Reject missing or ill-typed release identity before planning or writing."""
    if not isinstance(value, dict) or not isinstance(value.get("tag_name"), str):
        raise ValueError("Release応答の形式が不正です")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", value["tag_name"]):
        return value  # Non-stable tags are outside the synchronization policy.
    if (type(value.get("id")) is not int or value["id"] <= 0
            or any(type(value.get(k)) is not bool for k in ("draft", "prerelease"))
            or any(not isinstance(value.get(k), str) or not value[k]
                   for k in ("target_commitish", "created_at", "updated_at"))
            or any(k not in value or value[k] is not None and not isinstance(value[k], str)
                   for k in ("name", "body", "published_at"))):
        raise ValueError("Releaseの識別・公開状態の形式が不正です")
    return value


def api(repo, endpoint, payload=None):
    # Only the repo-relative releases endpoint is accepted; no arbitrary URL.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("repositoryはowner/nameで指定してください")
    if not re.fullmatch(r"releases(?:/[1-9][0-9]*|\?per_page=100&page=[1-9][0-9]*)", endpoint):
        raise ValueError("releases以外のAPIは使用できません")
    command = ["gh", "api", "--hostname", "github.com", f"repos/{repo}/{endpoint}",
               "--method", "PATCH" if payload is not None else "GET"]
    if payload is not None:
        if (set(payload) != {"name", "body", "tag_name"}
                or not isinstance(payload["tag_name"], str)
                or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", payload["tag_name"])):
            raise ValueError("name・bodyと保持する安定版tag_nameのみ指定できます")
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
        release = release_record(release)
        tag = release["tag_name"]
        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
            continue  # prerelease tags are outside this stable-release policy.
        if tag in seen or tag[1:] not in versions:
            raise ValueError(f"{tag}: 重複またはCHANGELOGの対応版がありません")
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
            current = release_record(request(endpoint))
            if any(current.get(k) != old.get(k)
                   for k in (*IDENTITY, "name", "body", "updated_at")):
                raise ValueError(f"{old['tag_name']}: 取得後に変更されたため停止します")
            # Omitting tag_name can detach a draft from its existing tag.
            updated = release_record(request(endpoint, desired | {"tag_name": old["tag_name"]}))
            if any(updated.get(k) != old.get(k) for k in IDENTITY):
                raise ValueError("Releaseの識別・公開状態が変わったため停止します")
            verified = release_record(request(endpoint))
            if any(verified.get(k) != old.get(k) for k in IDENTITY):
                raise ValueError("再取得時にReleaseの識別・公開状態が変わったため停止します")
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
    except (ValueError, OSError, RecursionError, subprocess.SubprocessError) as exc:
        # Do not print request payloads or subprocess output (which may include secrets).
        parser.exit(1, f"release sync: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
