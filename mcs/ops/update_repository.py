"""Verify source trees and repository state before an update changes the checkout."""
from __future__ import annotations
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path
import unicodedata
import urllib.request
from contextlib import contextmanager, suppress
import mcs_util  # noqa: E402
import update_policy  # noqa: E402
import owned_process  # noqa: E402
import update_discovery  # noqa: E402
import notify_flush  # noqa: E402,F401 - restore text quarantine
import notify_digest  # noqa: E402,F401 - restored digest card reissue
import notify_urgent  # noqa: E402,F401 - restored alert settlement
import alert_view  # noqa: E402,F401 - restored alert card reissue
import extraction_progress  # noqa: E402,F401 - restored card presentation
import present_discord  # noqa: E402,F401 - restored card presentation
import present_slack  # noqa: E402,F401 - restored card presentation
import present_lineworks  # noqa: E402,F401 - restored card presentation
import local_llm  # noqa: E402,F401 - postcheck model/admission diagnostics
import llm_admission  # noqa: E402,F401 - postcheck admission diagnostics
import mcs_restore  # noqa: E402,F401 - existing backup-adoption marker gate
import cloud_inference  # noqa: E402,F401 - rollback cloud-contract validation
from mcs_util import REPO
from safe_io import fdopen_owned  # noqa: E402

from update_lifecycle_context import (
    UpdateError,
    _PROVIDER_PRIOR_SOURCE_WITNESSES,
    _PROVIDER_ROLLBACK_SOURCE_WITNESSES,
    _RELEASE_GIT_SCOPE,
    _SPLIT_SOURCE_ROOTS,
    _bounded_timeout,
    _release_git_environment,
    _run_with_lifetime,
)

@contextmanager
def _release_git_scope():
    token = _RELEASE_GIT_SCOPE.set(True)
    try:
        yield
    finally:
        _RELEASE_GIT_SCOPE.reset(token)


def _release_git(args, timeout, *, deadline=None):
    budget = _bounded_timeout(timeout, deadline)
    # This per-command deadline keeps exact child retirement after quiesce without extending the pre-quiesce budget.
    stop_at = min(deadline, time.monotonic() + budget) if deadline is not None else time.monotonic() + budget
    try:
        result = owned_process.run_owned(['/usr/bin/git', '-C', REPO, '--work-tree', REPO, *args], budget,
            _release_git_environment(), deadline=stop_at)
        if result.returncode != 0:
            result.stderr = f'exit={result.returncode}'
        return result
    except subprocess.TimeoutExpired:
        raise UpdateError('git_timeout: ' + ' '.join(args[:1])) from None
    except OSError:
        raise UpdateError('git_spawn_failed') from None


def _git(args: list[str], timeout: int = update_discovery.LOCAL_TIMEOUT_S,
         *, deadline=None) -> subprocess.CompletedProcess:
    if deadline is not None or _RELEASE_GIT_SCOPE.get():
        return _release_git(args, timeout, deadline=deadline)
    try:
        result = update_discovery.git(args, _bounded_timeout(timeout, deadline), checkout=REPO,
            environment=os.environ if deadline is None else _release_git_environment())
        if deadline is not None:
            _bounded_timeout(None, deadline)
        return result
    except update_discovery.DiscoveryError as exc:
        raise UpdateError(str(exc)) from exc


def _checkout_git(checkout: str, *args: str, deadline=None) -> subprocess.CompletedProcess:
    """A bounded Git read of another checkout (OSError/TimeoutExpired propagate)."""
    return _run_with_lifetime(["git", "-C", checkout, *args], capture_output=True,
                              text=True, timeout=30, **({} if deadline is None else {
                                  'deadline': deadline, 'env': _release_git_environment()}))


def _index_has_flags(ls_files_v: str) -> bool:
    """True when `ls-files -v -z` shows any entry not tracked as plain 'H'."""
    return any(not row.startswith("H ") for row in ls_files_v.split("\0") if row)


def _current_version() -> tuple[str | None, str]:
    """(nearest release tag, HEAD sha) of the checkout; Git failures become UpdateError."""
    try:
        return update_discovery.current_version(checkout=REPO, environment=os.environ)
    except update_discovery.DiscoveryError as exc:
        raise UpdateError(str(exc)) from exc


def _git_out(args: list[str], timeout: int = update_discovery.LOCAL_TIMEOUT_S, *, deadline=None) -> str:
    r = _git(args, timeout, **({} if deadline is None else {"deadline": deadline}))
    if r.returncode != 0:
        raise UpdateError("git_failed: " + " ".join(args[:1])
                          + f" (exit={r.returncode})")
    return r.stdout


def _head_sha(*, deadline=None) -> str:
    return _git_out(["rev-parse", "HEAD"], **({} if deadline is None else {"deadline": deadline})).strip()


def _tree_clean(*, deadline=None) -> bool:
    return _git_out(["status", "--porcelain", "-uno"], **({} if deadline is None else {"deadline": deadline})).strip() == ""


def _local_changes_outside(prev, target) -> list[str]:
    """Unsafe states before resetting to target, allowing known update versions."""
    try:
        if not prev or not target:
            raise ValueError("update range unknown")
        snapshots = []
        for args, index in ((["ls-tree", "-rz", prev], False),
                            (["ls-tree", "-rz", target], False),
                            (["ls-files", "--stage", "-v", "-z"], True)):
            result = _git(args)
            if result.returncode != 0:
                raise ValueError("tree/index unreadable")
            versions = {}
            for record in filter(None, result.stdout.split("\0")):
                meta, sep, path = record.partition("\t")
                fields = meta.split()
                if index:
                    if not fields or fields.pop(0) != "H":
                        raise ValueError("unsupported index flags")
                if not sep or len(fields) != 3 or path in versions:
                    raise ValueError("tree/index malformed")
                mode, middle, last = fields
                blob = middle if index else last
                if (last != "0" if index else middle != "blob") or mode not in {
                        "100644", "100755", "120000"}:
                    raise ValueError("tree/index type unknown")
                if not _safe_relpath(path):
                    raise ValueError("tree/index path invalid")
                versions[path] = (mode, blob)
            snapshots.append(versions)
        ita_views = []
        for visibility in ("--ita-visible-in-index", "--ita-invisible-in-index"):
            result = _git(["diff", "--cached", "--raw", "--no-renames", "--no-ext-diff",
                           "--no-textconv", "--no-abbrev", "--no-relative", "-z", visibility, prev])
            if result.returncode != 0:
                raise ValueError("index intent unreadable")
            ita_views.append(result.stdout)
        if ita_views[0] != ita_views[1]:
            raise ValueError("intent-to-add index")

        old, new, index = snapshots
        directories = set()
        for path in old.keys() | new.keys():
            parts = path.split("/")
            directories.update("/".join(parts[:n]) for n in range(1, len(parts)))

        def worktree_version(path):
            full = Path(REPO)
            for part in path.split("/")[:-1]:
                full /= part
                try:
                    if not stat.S_ISDIR(full.lstat().st_mode):
                        ancestor = full.relative_to(REPO).as_posix()
                        if worktree_version(ancestor) not in {old.get(ancestor), new.get(ancestor)}:
                            raise ValueError("unknown non-directory ancestor")
                        return None
                except FileNotFoundError:
                    return None
            full /= path.split("/")[-1]
            try:
                before = full.lstat()
            except FileNotFoundError:
                return None
            if stat.S_ISDIR(before.st_mode) and path in directories:
                # A known directory may replace a leaf, but no private children may be lost.
                for child in full.iterdir():
                    name = path + "/" + child.name
                    if worktree_version(name) not in {old.get(name), new.get(name)}:
                        raise ValueError("unknown directory content")
                return None
            digest = hashlib.sha1(usedforsecurity=False)  # Git blob identity, not authentication.
            if stat.S_ISLNK(before.st_mode):
                content = os.fsencode(os.readlink(full))
                digest.update(f"blob {len(content)}\0".encode() + content)
                return "120000", digest.hexdigest()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("non-file at tracked path")
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with fdopen_owned(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("file changed during inspection")
                digest.update(f"blob {opened.st_size}\0".encode())
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                if (after.st_size, after.st_mtime_ns, after.st_mode) != (
                        opened.st_size, opened.st_mtime_ns, opened.st_mode):
                    raise ValueError("file changed during inspection")
            return ("100755" if opened.st_mode & stat.S_IXUSR else "100644"), digest.hexdigest()

        foreign = []
        for path in sorted(old.keys() | new.keys() | index.keys()):
            known = {old.get(path), new.get(path)}
            if index.get(path) not in known:
                foreign.append(path)
            elif path not in index and path not in new:
                continue  # Reset does not touch this untracked path.
            elif worktree_version(path) not in known:
                foreign.append(path)
        return foreign
    except (OSError, ValueError) as exc:
        raise UpdateError("diff: content provenance unknown") from exc


def fetch_notes(tag: str) -> str | None:
    """Release notes via the public GitHub API — no auth needed for a
    public repo; failure never blocks the notification itself."""
    remote = _git_out(["remote", "get-url", "origin"]).strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote)
    if not m:
        return None
    url = (f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}"
           f"/releases/tags/{tag}")
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": "mcs-update"})
        with mcs_util.no_proxy_opener(mcs_util.NoRedirect).open(req, timeout=15) as resp:
            raw = resp.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                return None
            data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    title = data.get("name") if isinstance(data.get("name"), str) else tag
    body = data.get("body") if isinstance(data.get("body"), str) else ""
    return f"{title}\n{body}".strip()


def impact_summary(cur_sha: str, tag: str) -> list[str]:
    """Read changed paths and explain their operational impact."""
    out = _git_out(["diff", "--name-status", f"{cur_sha}..{tag}"])
    names = [line.split("\t")[-1] for line in out.splitlines() if line]
    return update_policy.environment_impact(names, mcs_util.load_config())


def _tag_is_newer(tag, sha, cur_sha) -> bool:
    """Whether the latest tag is ahead of HEAD. HEAD already containing it
    (a checkout ahead of the tag) is not newer; only an exit-0 ancestry
    proof suppresses — missing objects (128) or a git failure keep the
    notice/auto path."""
    newer = bool(tag and sha and sha != cur_sha)
    if newer and cur_sha:
        with suppress(UpdateError):
            newer = _git(["merge-base", "--is-ancestor", sha, "HEAD"]
                         ).returncode != 0
    return newer


def _norm(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _safe_relpath(path: str) -> bool:
    """Reject anything that could escape or confuse the worktree:
    absolute, '..'/empty components, NUL, control chars."""
    if not path or path.startswith("/") or "\x00" in path:
        return False
    if any(ord(c) < 0x20 for c in path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _ls_tree_paths(tag: str, subdir: str | None = None,
                   *, deadline=None) -> list[tuple[str, str, str, str]]:
    """(mode, otype, sha, path) records via `ls-tree -rz` — NUL
    separation defeats core.quotepath quoting, which otherwise lets a
    crafted non-ASCII name slip past the protected-path check (S3/F2)."""
    args = ["ls-tree", "-rz", tag]
    if subdir:
        args += ["--", subdir]
    out = _git_out(args, **({} if deadline is None else {"deadline": deadline}))
    records = []
    for rec in out.split("\0"):
        if deadline is not None:
            _bounded_timeout(None, deadline)
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) < 3 or not path:
            continue
        records.append((parts[0], parts[1], parts[2], path))
    return records


def _split_source_tree(ref):
    return [entry for entry in _ls_tree_paths(ref)
            if any(entry[3] == root or entry[3].startswith(root + "/") for root in _SPLIT_SOURCE_ROOTS)]


def _provider_prior_source_verified(ref):
    if not isinstance(ref, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", ref) is None:
        return False
    tree = _split_source_tree(ref)
    if not tree:
        return False
    raw = json.dumps(tree, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest() in _PROVIDER_PRIOR_SOURCE_WITNESSES


def _split_target_source_matches(target: str, *, allow_reviewed_rollback=False) -> bool:
    """Verify runtime source identity, or an explicitly reviewed rollback tree.

    Migration requires identical source. Rollback alone may use a fixed tree
    witness; both paths verify every current file without importing target code.
    """
    if not isinstance(target, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", target) is None:
        return False
    head = _head_sha()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) is None or len(target) != len(head):
        return False
    current = _split_source_tree(head)
    candidate = _split_source_tree(target)
    if not current or not candidate:
        return False
    if current != candidate:
        raw = json.dumps(candidate, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if not allow_reviewed_rollback or hashlib.sha256(raw).hexdigest() not in _PROVIDER_ROLLBACK_SOURCE_WITNESSES:
            return False
        readers = {"mcs/core/provider_settings.py", "mcs/core/mcs_config.py"}
        if ([entry for entry in current if entry[3] in readers]
                != [entry for entry in candidate if entry[3] in readers]):
            return False
    required = {"mcs/core/mcs_util.py", "mcs/core/provider_settings.py", "mcs/core/mcs_config.py",
                "mcs/core/safe_io.py", "mcs/llm/cloud_policy.py", "mcs/semantic/semantic_jev.py"}
    if any(not required.issubset({path for _, _, _, path in tree}) for tree in (current, candidate)):
        return False
    # Ignore index hide flags and metadata caches: hash each actual file.
    # Untracked code in any executable tree can shadow a reader. Python does
    # not import source-less files from __pycache__; ordinary caches are safe.
    if _git_out(["status", "--porcelain=v1", "-z", "--untracked-files=no", "--", *_SPLIT_SOURCE_ROOTS]):
        return False
    extras = _git_out(["ls-files", "--others", "-z", "--", *_SPLIT_SOURCE_ROOTS]).split("\0")
    if any(path and not (path.endswith(".pyc") and "__pycache__" in path.split("/")) for path in extras):
        return False
    root = os.open(REPO, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for mode, kind, blob, path in current:
            if (kind != "blob" or mode not in ("100644", "100755") or not _safe_relpath(path)
                    or not any(path == root or path.startswith(root + "/") for root in _SPLIT_SOURCE_ROOTS)
                    or len(blob) != len(head)
                    or re.fullmatch(r"[0-9a-f]+", blob) is None):
                return False
            parent = os.dup(root)
            try:
                parts = path.split("/")
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent)
                    parent = child
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with fdopen_owned(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(before.st_mode) or before.st_size > 8 * 1024 * 1024
                            or before.st_nlink != 1 or before.st_uid != os.getuid()
                            or bool(before.st_mode & 0o111) != (mode == "100755")):
                        return False
                    raw = stream.read(8 * 1024 * 1024 + 1)
                    after = os.fstat(stream.fileno())
                    path_info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    def identity(st):
                        return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink,
                                st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                    if (len(raw) != before.st_size or identity(before) != identity(after)
                            or identity(after) != identity(path_info)):
                        return False
                    digest = hashlib.new("sha1" if len(blob) == 40 else "sha256")
                    digest.update(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw)
                    if digest.hexdigest() != blob:
                        return False
            finally:
                os.close(parent)
        return _head_sha() == head and _split_source_tree(target) == candidate
    finally:
        os.close(root)
