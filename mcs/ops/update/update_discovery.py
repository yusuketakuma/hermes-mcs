"""Observe checkout and origin tags through bounded, owned Git commands."""
import re
import subprocess

import owned_process
import update_policy

GIT_ENV = {"GIT_HTTP_LOW_SPEED_LIMIT": "1000",
           "GIT_HTTP_LOW_SPEED_TIME": "30",
           "GIT_TERMINAL_PROMPT": "0"}
REMOTE_TIMEOUT_S = 15
LOCAL_TIMEOUT_S = 30
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class DiscoveryError(Exception):
    """Git discovery failure with the existing sanitized diagnostic token."""


def git(args: list[str], timeout: int = LOCAL_TIMEOUT_S, *,
        checkout, environment) -> subprocess.CompletedProcess:
    env = dict(environment)
    env.update(GIT_ENV)
    try:
        result = owned_process.run_owned(["git", "-C", checkout, *args], timeout, env)
        if result.returncode != 0:
            result.stderr = f"exit={result.returncode}"
        return result
    except subprocess.TimeoutExpired as e:
        raise DiscoveryError("git_timeout: " + " ".join(args[:1])) from e
    except OSError as e:
        raise DiscoveryError("git_spawn_failed") from e


def git_output(args: list[str], timeout: int = LOCAL_TIMEOUT_S, *,
               checkout, environment) -> str:
    r = git(args, timeout, checkout=checkout, environment=environment)
    if r.returncode != 0:
        raise DiscoveryError("git_failed: " + " ".join(args[:1])
                          + f" (exit={r.returncode})")
    return r.stdout


def remote_tag_sha(tag: str, *, checkout, environment) -> str | None:
    """Peeled commit sha for <tag> at origin, or None when origin has no
    such tag; a failed query raises DiscoveryError. Queries both the
    ref and its ^{} peel — a lightweight tag returns only the direct
    line, whose sha IS the commit (S15)."""
    r = git(["ls-remote", "origin",
              f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
             timeout=REMOTE_TIMEOUT_S, checkout=checkout, environment=environment)
    if r.returncode != 0:
        # a failed query is not "the tag moved": None stays "no such ref at origin"
        raise DiscoveryError(f"git_failed: ls-remote (exit={r.returncode})")
    direct = peeled = None
    for line in r.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/tags/{tag}^{{}}":
            peeled = sha.strip()
        elif ref == f"refs/tags/{tag}":
            direct = sha.strip()
    sha = peeled or direct
    return sha if sha and COMMIT_SHA_RE.fullmatch(sha) else None


def detect_latest(include_prerelease: bool = False, *, checkout,
                  environment) -> tuple[str | None, str | None]:
    """(tag, peeled commit sha) of the semver-max tag at origin.
    Peeled ^{} lines win over the direct tag-object line (B2); a
    lightweight tag only has the direct line, which IS the commit."""
    r = git(["ls-remote", "--tags", "origin"], timeout=REMOTE_TIMEOUT_S, checkout=checkout, environment=environment)
    if r.returncode != 0:
        raise DiscoveryError("ls-remote failed: " + r.stderr.strip()[:200])
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in r.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        sha = sha.strip()
        if not ref.startswith("refs/tags/") or not COMMIT_SHA_RE.fullmatch(sha):
            continue
        name = ref[len("refs/tags/"):]
        if name.endswith("^{}"):
            peeled[name[:-3]] = sha
        else:
            direct[name] = sha
    best: tuple[tuple, str] | None = None
    for name in direct:
        key = update_policy.version_key(name)
        if key is None:
            continue
        if not include_prerelease and key[3] == 0:
            continue
        # deterministic tie-break on the name so v1.2.3 vs 1.2.3
        # duplicates never pick randomly
        if best is None or key > best[0] \
                or (key == best[0] and name > best[1]):
            best = (key, name)
    if best is None:
        return None, None
    return best[1], peeled.get(best[1]) or direct[best[1]]


def current_version(*, checkout, environment) -> tuple[str | None, str | None]:
    """(nearest ancestor release tag under update_policy.release_tag, HEAD sha).
    Tags that only look like versions (v1garbage) are excluded by name, so an
    older valid release behind them is still found; None when there is none."""
    patterns = ["--match", "v[0-9]*", "--match", "[0-9]*"]
    try:
        names = git_output(["tag", "--list", "v[0-9]*", "[0-9]*"],
                           checkout=checkout, environment=environment).split()
        # refnames cannot hold *, ? or [, so a tag name is its own exact pattern
        excludes = [arg for name in names if not update_policy.release_tag(name)
                    for arg in ("--exclude", name)]
        tag = git_output(["describe", "--tags", "--abbrev=0", *patterns, *excludes],
                         checkout=checkout, environment=environment).strip()
    except DiscoveryError:
        tag = None
    if not update_policy.release_tag(tag):
        tag = None
    return tag, git_output(["rev-parse", "HEAD"], checkout=checkout, environment=environment).strip()
