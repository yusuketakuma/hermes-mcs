#!/usr/bin/env python3
"""Upgrade launcher: run the TARGET tag's updater against this checkout.

Taken out of the target tag and run from a temp file, so the installed
version's updater (missing before v1.0.3, unable to re-run install.sh
before v1.0.11) never decides anything:

  git -C ~/.mcs fetch --tags origin
  WORK=$(mktemp -d); git -C ~/.mcs show v1.0.11:scripts/mcs_upgrade.py > "$WORK/mcs_upgrade.py"
  "$PY" "$WORK/mcs_upgrade.py" --repo ~/.mcs plan --to v1.0.11
  "$PY" "$WORK/mcs_upgrade.py" --repo ~/.mcs apply --to v1.0.11 [--reinstall]

It copies the tag's exact ``mcs/`` blobs into a private temp dir and runs
that ``mcs/ops/mcs_update.py`` with ``MCS_UPDATE_REPO`` set to the live
checkout. Stdlib only, imports nothing from the repo. Runbook:
docs/guides/UPGRADE_AGENT.md.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

SEMVER_RE = re.compile(r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)$")


def _git(repo: str, *args: str, binary: bool = False):
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=not binary, timeout=120,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        raise SystemExit(f"git {args[0]} timed out") from None
    if r.returncode != 0:
        err = r.stderr if not binary else r.stderr.decode(errors="replace")
        raise SystemExit(f"git {args[0]} failed: {err.strip()[:200]}")
    return r.stdout


def latest_tag(repo: str) -> str:
    tags = [t for t in _git(repo, "tag", "--list").split()
            if SEMVER_RE.fullmatch(t)]
    if not tags:
        raise SystemExit("no release tag found — fetch tags first")
    return max(tags, key=lambda t: tuple(map(int, SEMVER_RE.fullmatch(t).groups())))


def extract(repo: str, tag: str, dest: str) -> None:
    """The tag's exact mcs/ blobs (no git-archive attribute rewriting)."""
    for rec in _git(repo, "ls-tree", "-rz", tag, "--", "mcs").split("\0"):
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        mode, otype, sha = meta.split()
        if otype != "blob":
            continue
        if mode not in ("100644", "100755"):     # no symlink/gitlink
            raise SystemExit(f"unsupported entry in {tag}: {mode} {path!r}")
        parts = path.split("/")
        if path.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise SystemExit(f"unsafe path in {tag}: {path!r}")
        out = os.path.join(dest, path)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(_git(repo, "cat-file", "blob", sha, binary=True))
        if mode == "100755":
            os.chmod(out, 0o755)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="the MCS checkout (default: cwd)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="use the tags already fetched")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--to", help="target tag (default: newest release tag)")
        if name == "apply":
            p.add_argument("--reinstall", action="store_true")
            p.add_argument("--install-arg", action="append", default=[])
    args = ap.parse_args(argv)
    if sys.version_info < (3, 10):
        raise SystemExit("Python >= 3.10 required — use the Hermes or "
                         "~/.mcs/venv interpreter (UPGRADE_AGENT.md §1)")
    repo = os.path.realpath(_git(args.repo, "rev-parse", "--show-toplevel").strip())
    if not args.no_fetch:
        _git(repo, "fetch", "--tags", "origin")
    tag = args.to or latest_tag(repo)
    if not SEMVER_RE.fullmatch(tag):
        raise SystemExit(f"bad target tag: {tag}")
    tmp = tempfile.mkdtemp(prefix="mcs_upgrade_")
    try:
        extract(repo, tag, tmp)
        updater = os.path.join(tmp, "mcs", "ops", "mcs_update.py")
        if not os.path.exists(updater):
            raise SystemExit(f"{tag} has no mcs/ops/mcs_update.py")
        cmd = [sys.executable, updater, args.cmd, "--tag", tag]
        if args.cmd == "apply":
            cmd += ["--reinstall"] if args.reinstall else []
            cmd += [f"--install-arg={a}" for a in args.install_arg]
        env = {**os.environ, "MCS_UPDATE_REPO": repo}
        return subprocess.run(cmd, env=env, cwd=repo).returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
