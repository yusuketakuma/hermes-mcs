"""MCS self-update — detection, apply, rollback, interrupted recovery.

Drives the update lifecycle documented in docs/auto-update-plan.md:

  status    print update_state.json + current/latest tag summary
  check     daily cron driver: detect latest tag, notify once, scan the
            command_receipts approval queue, auto-apply when mode=auto
  apply     run the staged apply pipeline for a pending approval (or
            --tag explicit). Takes run.lock -> update.lock, journals
            every stage, quiesces resident drainers, merges, then runs
            post-merge steps in a NEW-code subprocess (locks held).
  rollback  restore the last applied entry's prev_sha (+ DB restore when
            the apply carried a schema_bump)
  recover   journal-driven interrupted-apply recovery (also invoked by
            the independent launchd watchdog via mcs_recover.py)

Safety invariants: never touches protected paths (data/, config.json,
.env), never runs `git clean`, treats unverifiable as failure, and the
approval boundary is the command_receipts commit — argv is re-verified
against receipts, never trusted.
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401,E402  registers every subdir as import root

from mcs_util import acquire_run_lock, load_config  # noqa: E402

HOME = os.path.expanduser("~/.mcs")
DATA = os.path.join(HOME, "data")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
LEDGER = os.path.join(DATA, "ledger.db")
STATE_PATH = os.path.join(DATA, "update_state.json")
UPDATE_LOCK = os.path.join(DATA, "update.lock")
RUN_LOCK = os.path.join(DATA, "run.lock")
MARKER_PATH = os.path.join(DATA, "update_in_progress.marker")
MANIFEST_PATH = os.path.join(DATA, "service_manifest.json")
REPORT_PATH = os.path.join(DATA, "recovery_report.json")
BACKUP_DIR = os.path.join(DATA, "backups")
SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
WRAPPER = os.path.join(SCRIPTS_DIR, "mcs_update.sh")
RECOVERY_TOOL = os.path.expanduser("~/.mcs-recovery/mcs_recover.py")

RESIDENT_LABELS = ("ai.mcs.extract-drainer", "ai.mcs.extract-drainer-rt")
WATCHER_LABELS = ("local.mcs-cmd", "local.mcs-int")
# install.sh-owned labels the updater must never touch (S5).
EXCLUDED_LABELS = frozenset({"ai.mcs.llamaserver", "org.mcs.recovery"})
PROTECTED = ("data/", "config.json", ".env", "chrome-profile/")
SEMVER_RE = re.compile(
    r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z.-]+))?$")
HEX_RE = re.compile(r"^[0-9a-f]{40}$")
_MENTION_RE = re.compile(r"<@[!&]?\d+>|<#\d+>|@everyone|@here")
# drainer stray sweep: interpreter argv0 + script path — never matches
# `vim extract_llm.py` or `pytest ...` (H7/F13)
_STRAY_RE = r"(^|/)python[0-9.]* \S*(extract_llm|semantic_drain)\.py"

GIT_ENV = {"GIT_HTTP_LOW_SPEED_LIMIT": "1000",
           "GIT_HTTP_LOW_SPEED_TIME": "30",
           "GIT_TERMINAL_PROMPT": "0"}
T_LS_REMOTE = 15
T_FETCH = 120
T_MERGE = 60
T_GIT = 30
T_POST_MERGE = 600        # services + restart + postcheck can be slow
STALE_S = 1800            # no stage progress for this long => stale apply
GIT_LOCK_MIN_AGE_S = 600  # younger .git/*.lock may belong to a live op
RUN_LOCK_TRIES = 40       # 30s x 40 = 20min > RUN_DEADLINE_S (S21)
RUN_LOCK_INTERVAL = 30
UPDATE_OPS = ("ops.update_apply", "ops.update_rollback")
_UPDATE_ENV_STRIP = (
    "_HERMES_GATEWAY", "_HERMES_GATEWAY_BREAKAWAY",
    "HERMES_SUPERVISED_CHILD", "HERMES_S6_SUPERVISED_CHILD",
    "HERMES_GATEWAY", "HERMES_GATEWAY_MODE", "HERMES_GATEWAY_DETACHED",
    "HERMES_CRON_JOB", "HERMES_JOB_ID", "HERMES_QUIET", "TERMINAL_CWD")
_UPDATE_PM_ENV = "_MCS_UPDATE_PM"   # post-merge child handshake token
# bail reasons that mean "we never attempted" — do NOT consume the
# receipt or poison attempts[]; the next check may legitimately retry
_TRANSIENT_BAIL = frozenset({"run_lock_timeout", "update_lock_busy"})


class UpdateError(Exception):
    """Gate/stage failure with a stable reason token for the journal."""


# ---------------------------------------------------------------- state

def _default_state() -> dict:
    return {"v": 1, "stages": [], "applied": [], "attempts": {},
            "executed": {}, "applying": None}


def load_state() -> dict:
    """Lock-free read — writers publish atomically so a torn read never
    happens. Unparsable state is 'corrupt': callers must escalate and
    touch nothing (S17/S24)."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return _default_state()
    except (OSError, json.JSONDecodeError, ValueError):
        return {"_corrupt": True}
    if not isinstance(state, dict) or state.get("v") != 1:
        return {"_corrupt": True}
    return state


def save_state(state: dict) -> None:
    """tmp -> fsync -> os.replace -> dir fsync (R13). Caller holds
    update.lock (or is the --locks-held post-merge child)."""
    os.makedirs(DATA, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".update_state.",
                             suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
        dfd = os.open(DATA, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def journal(state: dict, stage: str) -> None:
    state["stages"].append({"stage": stage, "at": time.time()})
    save_state(state)


def acquire_update_lock() -> int | None:
    """Non-blocking flock on data/update.lock; fd or None."""
    os.makedirs(DATA, exist_ok=True)
    fd = os.open(UPDATE_LOCK, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _acquire_run_lock_wait(tries: int = RUN_LOCK_TRIES) -> int | None:
    for i in range(tries):
        fd = acquire_run_lock(RUN_LOCK)
        if fd is not None:
            return fd
        if i + 1 < tries:
            time.sleep(RUN_LOCK_INTERVAL)
    return None


# ------------------------------------------------------------------ git

def _git(args: list[str], timeout: int = T_GIT) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(GIT_ENV)
    try:
        return subprocess.run(["git", "-C", REPO, *args],
                              capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise UpdateError("git_timeout: " + " ".join(args[:1]))
    except OSError as e:
        raise UpdateError(f"git_spawn_failed: {e}")


def _git_out(args: list[str], timeout: int = T_GIT) -> str:
    r = _git(args, timeout)
    if r.returncode != 0:
        raise UpdateError("git_failed: " + " ".join(args[:1])
                          + " — " + r.stderr.strip()[:200])
    return r.stdout


def _head_sha() -> str:
    return _git_out(["rev-parse", "HEAD"]).strip()


def _tree_clean() -> bool:
    return _git_out(["status", "--porcelain", "-uno"]).strip() == ""


def remote_tag_sha(tag: str) -> str | None:
    """Peeled commit sha for <tag> at origin, or None. Queries both the
    ref and its ^{} peel — a lightweight tag returns only the direct
    line, whose sha IS the commit (S15)."""
    r = _git(["ls-remote", "origin",
              f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
             timeout=T_LS_REMOTE)
    if r.returncode != 0:
        return None
    direct = peeled = None
    for line in r.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/tags/{tag}^{{}}":
            peeled = sha.strip()
        elif ref == f"refs/tags/{tag}":
            direct = sha.strip()
    sha = peeled or direct
    return sha if sha and HEX_RE.fullmatch(sha) else None


def _ver_key(name: str) -> tuple | None:
    """Semver ordering key: (major, minor, patch, release-flag, pre).
    A prerelease sorts BELOW the same release; None when not semver."""
    m = SEMVER_RE.fullmatch(name or "")
    if not m:
        return None
    pre = m.group(4)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
            0 if pre else 1, pre or "")


def detect_latest(include_prerelease: bool = False
                  ) -> tuple[str | None, str | None]:
    """(tag, peeled commit sha) of the semver-max tag at origin.
    Peeled ^{} lines win over the direct tag-object line (B2); a
    lightweight tag only has the direct line, which IS the commit."""
    r = _git(["ls-remote", "--tags", "origin"], timeout=T_LS_REMOTE)
    if r.returncode != 0:
        raise UpdateError("ls-remote failed: " + r.stderr.strip()[:200])
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in r.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        sha = sha.strip()
        if not ref.startswith("refs/tags/") or not HEX_RE.fullmatch(sha):
            continue
        name = ref[len("refs/tags/"):]
        if name.endswith("^{}"):
            peeled[name[:-3]] = sha
        else:
            direct[name] = sha
    best: tuple[tuple, str] | None = None
    for name in direct:
        key = _ver_key(name)
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


def current_version() -> tuple[str | None, str | None]:
    """(nearest ancestor tag, HEAD sha)."""
    try:
        tag = _git_out(["describe", "--tags", "--abbrev=0"]).strip()
    except UpdateError:
        tag = None
    return tag, _head_sha()


# ------------------------------------------------------------- detection

def defuse_mentions(text: str) -> str:
    """Neutralise Discord mention syntax in release notes (F4) —
    fullwidth the @ AND the <> wrappers: <#id> carries no @."""
    return _MENTION_RE.sub(
        lambda m: m.group(0).replace("@", "＠")
                  .replace("<", "＜").replace(">", "＞"), text)


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
        import mcs_util
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": "mcs-update"})
        with mcs_util.no_proxy_opener().open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    title = data.get("name") or tag
    body = (data.get("body") or "").strip()
    return f"{title}\n{body}".strip()


def impact_summary(cur_sha: str, tag: str) -> list[str]:
    """Environment-impact lines derived from the diff fileset."""
    out = _git_out(["diff", "--name-status", f"{cur_sha}..{tag}"])
    names = [line.split("\t")[-1] for line in out.splitlines() if line]
    notes = []
    if any(n in ("mcs/ops/mcs_setup.py", "config.json") or
           n.endswith("/config.json") for n in names):
        notes.append("新しい必須設定の可能性があります")
    if any(n.startswith("deployment/") for n in names):
        notes.append("services 再実行が必要な変更を含みます")
    if "install.sh" in names:
        notes.append("install.sh に差分 — 依存追加の可能性、"
                     "auto モードでは適用を中止します")
    if any(n.startswith("hermes_plugin/") for n in names):
        notes.append("plugin 変更 — 適用後に gateway restart が必要です")
    return notes


# -------------------------------------------------------------- gates

def precheck_local(cfg: dict) -> list[str]:
    """Local-only gates (no tag objects needed yet)."""
    errors = []
    if not _tree_clean():
        errors.append("tree_dirty")
    try:
        import mcs_setup
        errs, _ = mcs_setup.validate_config(cfg)
        errors.extend("config: " + e for e in errs)
    except Exception as e:
        errors.append(f"validate_config_failed: {type(e).__name__}")
    try:
        need = os.path.getsize(LEDGER) * 2 + 64 * 1024 * 1024
        if shutil.disk_usage(DATA).free < need:
            errors.append("insufficient_disk")
    except OSError:
        pass
    import mcs_setup
    hermes = mcs_setup._hermes_exe(cfg)
    if not mcs_setup._hermes_ok(hermes):
        errors.append("hermes_not_resolvable")
    return errors


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


def _ls_tree_paths(tag: str, subdir: str | None = None
                   ) -> list[tuple[str, str, str, str]]:
    """(mode, otype, sha, path) records via `ls-tree -rz` — NUL
    separation defeats core.quotepath quoting, which otherwise lets a
    crafted non-ASCII name slip past the protected-path check (S3/F2)."""
    args = ["ls-tree", "-rz", tag]
    if subdir:
        args += ["--", subdir]
    out = _git_out(args)
    records = []
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, _, path = rec.partition("\t")
        parts = meta.split()
        if len(parts) < 3 or not path:
            continue
        records.append((parts[0], parts[1], parts[2], path))
    return records


def precheck_tag(tag: str) -> list[str]:
    """Candidate-tree content gates — all work on fetched objects."""
    errors: list[str] = []
    paths = []
    for mode, otype, _sha, path in _ls_tree_paths(tag):
        if mode not in ("100644", "100755", "040000") or \
                otype not in ("blob", "tree"):
            errors.append(f"bad_entry_type: {mode} {path}")
        if not _safe_relpath(path):
            errors.append(f"unsafe_path: {path!r}")
        paths.append(path)
    norm_protected = tuple(_norm(p) for p in PROTECTED)
    for path in paths:
        np_ = _norm(path)
        if any(np_ == p or np_.startswith(p.rstrip("/") + "/")
               for p in norm_protected):
            errors.append(f"protected_path: {path}")
    # Rule-based ignored-path check (S16): the tag must not track
    # anything the repo's own ignore rules would cover.
    if paths:
        r = subprocess.run(
            ["git", "-C", REPO, "check-ignore", "-z", "--stdin",
             "--no-index"],
            input="\0".join(paths), capture_output=True, text=True,
            timeout=T_GIT)
        if r.returncode == 0:
            for name in r.stdout.split("\0"):
                if name:
                    errors.append(f"ignored_path_tracked: {name}")
        elif r.returncode > 1:
            errors.append("check_ignore_failed")
    # untracked collision: merge would refuse, but name the files first
    untracked = set(_git_out(
        ["ls-files", "--others", "--exclude-standard", "-z"]).split("\0"))
    diff = set(_git_out(
        ["diff", "--name-only", "-z", "HEAD", tag]).split("\0"))
    clash = (untracked & diff) - {""}
    if clash:
        errors.append("untracked_collision: " + ",".join(sorted(clash)[:10]))
    # fast-forward feasibility + ancestry must be provable BEFORE the
    # quiesce window, not discovered inside it (L3)
    if _git(["merge-base", "--is-ancestor", "HEAD", tag]
            ).returncode != 0:
        errors.append("not_fast_forward: HEAD is not an ancestor of tag")
    # schema compat: candidate SCHEMA_VERSION vs live user_version
    try:
        src = _git_out(["show", f"{tag}:mcs/core/ledger.py"])
        m = re.search(r"SCHEMA_VERSION\s*=\s*([0-9]+)", src)
        if not m:
            errors.append("schema_version_unparseable")
        else:
            new_ver = int(m.group(1))
            try:
                con = sqlite3.connect(
                    "file:" + LEDGER + "?mode=ro", uri=True)
                try:
                    cur_ver = con.execute(
                        "PRAGMA user_version").fetchone()[0]
                finally:
                    con.close()
            except sqlite3.Error:
                cur_ver = 0
            if new_ver < cur_ver:
                errors.append(f"schema_downgrade:{cur_ver}->{new_ver}")
            elif new_ver > cur_ver:
                errors.append(f"schema_bump:{cur_ver}->{new_ver}")
    except UpdateError:
        errors.append("schema_version_unreadable")
    # candidate preflight: exact blobs, not git archive (S3)
    tmp = tempfile.mkdtemp(prefix="mcs_preflight_")
    try:
        for mode, otype, bsha, path in _ls_tree_paths(tag, "mcs"):
            if otype != "blob":
                continue
            if not _safe_relpath(path):
                raise UpdateError("unsafe_path: " + path)
            dst = os.path.join(tmp, path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            blob = subprocess.run(
                ["git", "-C", REPO, "cat-file", "blob", bsha],
                capture_output=True, timeout=T_GIT)
            if blob.returncode != 0:
                raise UpdateError("cat-file failed: " + path)
            with open(dst, "wb") as f:
                f.write(blob.stdout)
            if mode == "100755":
                os.chmod(dst, 0o755)
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys, json, os\n"
             "sys.path.insert(0, sys.argv[1])\n"
             "import _mcs_path  # noqa\n"
             "import mcs_setup\n"
             "errs, _ = mcs_setup.validate_config(\n"
             "    json.load(open(os.environ['MCS_CONF'])))\n"
             "print(json.dumps(errs))",
             os.path.join(tmp, "mcs")],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "MCS_CONF": os.path.join(HOME,
                                                       "config.json")})
        if probe.returncode != 0:
            errors.append("preflight_failed: "
                          + (probe.stderr or "").strip()[:200])
        else:
            try:
                for e in json.loads(probe.stdout.strip() or "[]"):
                    errors.append("new_config: " + e)
            except json.JSONDecodeError:
                errors.append("preflight_unparseable")
    except (UpdateError, subprocess.TimeoutExpired, OSError) as e:
        errors.append(f"preflight_failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if _git(["diff", "--quiet", "HEAD", tag, "--", "install.sh"]
            ).returncode != 0:
        errors.append("install_sh_changed")
    return errors


# ------------------------------------------------------------- services

def _uid() -> int:
    return os.getuid()


def _agent_pid(label: str) -> int | None:
    r = subprocess.run(["launchctl", "print", f"gui/{_uid()}/{label}"],
                       capture_output=True, text=True, timeout=T_GIT)
    if r.returncode != 0:
        return None
    m = re.search(r"^\s*pid\s*=\s*(\d+)", r.stdout, re.M)
    return int(m.group(1)) if m else None


def _write_marker() -> None:
    with open(MARKER_PATH, "w") as f:
        f.write(str(time.time()))
        f.flush()
        os.fsync(f.fileno())
    dfd = os.open(DATA, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _remove_marker() -> None:
    try:
        os.unlink(MARKER_PATH)
    except OSError:
        pass


def _stray_drainer_pids() -> list[int]:
    """Same-uid python processes running the drainer scripts — the
    pattern requires an interpreter argv0 so editors, test runners and
    unrelated commands containing the filename never match (H7)."""
    try:
        r = subprocess.run(
            ["pgrep", "-u", str(_uid()), "-f", _STRAY_RE],
            capture_output=True, text=True, timeout=T_GIT)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(p) for p in r.stdout.split()
            if p.isdigit() and int(p) != os.getpid()]


def quiesce() -> list[str]:
    """Stop resident drainers + stray helpers; returns old pids' labels
    for restart verification. Marker first — helper launchers (cron
    catchup) must see it before any process is stopped (S8)."""
    _write_marker()
    stopped = []
    for label in RESIDENT_LABELS:
        subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{label}"],
                       capture_output=True, timeout=T_GIT)
        deadline = time.time() + 15
        while time.time() < deadline:
            if _agent_pid(label) is None:
                break
            time.sleep(0.5)
        if _agent_pid(label) is not None:
            raise UpdateError(f"drainer_stop_failed: {label}")
        stopped.append(label)
    # stray sweep: helpers may have spawned drainers outside launchd
    for i in range(4):
        pids = _stray_drainer_pids()
        if not pids:
            break
        sig = 15 if i < 3 else 9       # TERM, then KILL on the last pass
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        time.sleep(1)
    if _stray_drainer_pids():
        raise UpdateError("stray_drainer_survived")
    return stopped


def restart_agents() -> list[str]:
    """Re-bootstrap resident drainers and verify a NEW pid; watchers are
    verified loaded only (R20). Returns list of verify failures."""
    problems = []
    for label in RESIDENT_LABELS:
        plist = os.path.join(AGENTS_DIR, label + ".plist")
        subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{label}"],
                       capture_output=True, timeout=T_GIT)
        r = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{_uid()}", plist],
            capture_output=True, text=True, timeout=T_GIT)
        if r.returncode != 0:
            problems.append(f"bootstrap_failed:{label}")
            continue
        deadline = time.time() + 15
        pid = None
        while time.time() < deadline:
            pid = _agent_pid(label)
            if pid:
                break
            time.sleep(0.5)
        if not pid:
            problems.append(f"drainer_not_running:{label}")
    for label in WATCHER_LABELS:
        r = subprocess.run(
            ["launchctl", "print", f"gui/{_uid()}/{label}"],
            capture_output=True, timeout=T_GIT)
        if r.returncode != 0:
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{_uid()}",
                 os.path.join(AGENTS_DIR, label + ".plist")],
                capture_output=True, timeout=T_GIT)
            r = subprocess.run(
                ["launchctl", "print", f"gui/{_uid()}/{label}"],
                capture_output=True, timeout=T_GIT)
            if r.returncode != 0:
                problems.append(f"watcher_not_loaded:{label}")
    _remove_marker()
    return problems


def restart_gateway(cfg: dict) -> None:
    """Fire-and-forget — a cron-spawned updater is a gateway descendant;
    a synchronous `gateway restart` would wait on ourselves (S12)."""
    subprocess.Popen(
        ["launchctl", "kickstart", "-k",
         f"gui/{_uid()}/ai.hermes.gateway"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True)


def _surgical_delete(sha_a: str, sha_b: str) -> None:
    """Delete only untracked files that differ between the two update
    endpoints — `git clean` is banned (it would take unrelated user
    files). Uses the pinned SHAs, not tag names, so a re-pointed tag
    can never widen the delete set; -z parsing handles quoted and
    non-ASCII paths (F2/F3)."""
    fileset = set(_git_out(
        ["diff", "--name-only", "-z", sha_a, sha_b]).split("\0"))
    fileset.discard("")
    untracked = _git_out(["ls-files", "--others", "--full-name", "-z"])
    norm_protected = tuple(_norm(p) for p in PROTECTED)
    for name in untracked.split("\0"):
        if not name or name not in fileset or not _safe_relpath(name):
            continue
        np_ = _norm(name)
        if any(np_ == p or np_.startswith(p.rstrip("/") + "/")
               for p in norm_protected):
            continue                        # never touch protected
        try:
            os.unlink(os.path.join(REPO, name))
        except OSError:
            pass


def _clean_stale_git_locks() -> list[str]:
    """Stage-0 of every recovery: .git/*.lock leftovers block even
    `reset --hard`. Only locks OLDER than GIT_LOCK_MIN_AGE_S are
    removed — a fresh lock may belong to an unrelated live `git`
    process the user started themselves (H3). update.lock being free
    already proves no updater is alive (S10)."""
    removed = []
    cutoff = time.time() - GIT_LOCK_MIN_AGE_S
    for path in glob.glob(os.path.join(REPO, ".git", "**", "*.lock"),
                          recursive=True):
        try:
            if os.path.getmtime(path) >= cutoff:
                continue
            os.unlink(path)
            removed.append(path)
        except OSError:
            pass
    return removed


# ------------------------------------------------------------- approval

def scan_pending_approvals(state: dict
                           ) -> tuple[list[dict], list[str]]:
    """Read command_receipts read-only (no Ledger init — R16).

    Returns (candidates, consumed):
      candidates — unexecuted ops in processed_at order: among pending
        applies only the newest tag is eligible (older ones are
        superseded); each ops.update_rollback is its own op AND vetoes
        every apply receipt that arrived before it.
      consumed — (command_id, result) pairs to record as executed
        without running (superseded / vetoed applies)."""
    try:
        con = sqlite3.connect("file:" + LEDGER + "?mode=ro", uri=True)
    except sqlite3.Error:
        return [], []
    try:
        rows = con.execute(
            "SELECT command_id, receipt_json, processed_at, rowid"
            " FROM command_receipts WHERE outcome='applied'"
            " ORDER BY processed_at, rowid").fetchall()
    except sqlite3.Error:
        return [], []
    finally:
        con.close()
    executed = state.get("executed", {})
    applies: list[dict] = []
    rollbacks: list[dict] = []
    consumed: list[tuple[str, str]] = []
    for cid, rj, at, rowid in rows:
        if cid in executed:
            continue
        try:
            rec = json.loads(rj)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        cmd = rec.get("cmd")
        if cmd == "ops.update_apply" and rec.get("scheduled") is True:
            applies.append({"command_id": cid, "tag": rec.get("tag"),
                            "target_sha": rec.get("target_sha"),
                            "base_sha": rec.get("base_sha"),
                            "at": at, "rowid": rowid})
        elif cmd == "ops.update_rollback":
            consumed.extend((p["command_id"], "vetoed")
                            for p in applies)
            applies.clear()
            rollbacks.append({"command_id": cid, "rollback": True,
                              "at": at, "rowid": rowid})
    # same-tag duplicates: the NEWEST receipt wins (it carries the
    # freshest sha pin / review context) — older ones are superseded
    by_tag: dict[str, dict] = {}
    for p in applies:
        cur = by_tag.get(p["tag"])
        if cur is None or (p["at"] or 0, p["rowid"]) \
                > (cur["at"] or 0, cur["rowid"]):
            by_tag[p["tag"]] = p
    keep = {id(p) for p in by_tag.values()}
    consumed.extend((p["command_id"], "superseded")
                    for p in applies if id(p) not in keep)
    # among surviving tags the highest version wins; the rest are
    # superseded and must not fire later
    ordered = sorted(
        by_tag.values(),
        key=lambda p: _ver_key(p.get("tag")) or (-1, -1, -1, -1, ""),
        reverse=True)
    consumed.extend((p["command_id"], "superseded")
                    for p in ordered[1:])
    candidates = ordered[:1] + rollbacks
    candidates.sort(key=lambda p: (p.get("at") or 0, p.get("rowid") or 0))
    return candidates, consumed


def spawn_detached() -> None:
    """Launch the updater detached from the caller's fds/session (S9).
    Used by drain_commands AFTER the receipt commit — the spawned
    process re-verifies via receipt scan, never trusting argv."""
    env = {k: v for k, v in os.environ.items()
           if k not in _UPDATE_ENV_STRIP}
    os.makedirs(DATA, exist_ok=True)
    log = open(os.path.join(DATA, "update.log"), "ab")
    try:
        subprocess.Popen([WRAPPER], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, env=env,
                         close_fds=True, start_new_session=True)
    finally:
        log.close()


# ------------------------------------------------------------- pipeline

def _enqueue_notice(text: str, use_run_lock: bool = False) -> bool:
    """Freeze sanitized text into an update_notice outbox event.
    use_run_lock=True for the daily check (NB acquire — a busy tick
    defers the notify to the next check); the apply child skips it
    because its parent already holds the lock."""
    fd = None
    if use_run_lock:
        fd = acquire_run_lock(RUN_LOCK)
        if fd is None:
            return False
    try:
        import ledger
        db = ledger.Ledger(LEDGER)
        try:
            db.outbox_add("update_notice", None,
                          {"text": defuse_mentions(text)})
        finally:
            db.db.close()
        return True
    except Exception:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def _record_attempt(state: dict, tag: str | None, result: str,
                    detail: str = "") -> None:
    if tag:
        state.setdefault("attempts", {})[tag] = {
            "result": result, "at": time.time(), "detail": detail}


def _services_reconcile() -> None:
    r = subprocess.run([sys.executable,
                        os.path.join(REPO, "mcs", "ops", "mcs_setup.py"),
                        "services"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise UpdateError("services_failed: "
                          + (r.stderr or r.stdout).strip()[:200])


def _postcheck(state: dict, expect_sha: str) -> list[str]:
    """Positive verification — 'no new error' alone is never success."""
    errors = []
    if _head_sha() != expect_sha:
        errors.append("head_mismatch")
    if not _tree_clean():
        errors.append("tree_dirty_after_merge")
    try:
        import ledger
        db = ledger.Ledger(LEDGER)
        db.db.close()
    except Exception:
        errors.append("ledger_open_failed")
    try:
        import mcs_setup
        cfg = load_config()
        errs, _ = mcs_setup.check_environment(cfg)
        baseline = set(state.get("baseline_check") or [])
        new = [e for e in errs if e not in baseline]
        errors.extend("new_env_error: " + e for e in new)
    except Exception:
        errors.append("postcheck_unverifiable")
    for label in RESIDENT_LABELS:
        if _agent_pid(label) is None:
            errors.append(f"drainer_not_running:{label}")
    return errors


def _post_merge(state: dict) -> int:
    """Post-merge stages, run as a NEW-code subprocess while the parent
    (old code) holds both locks (--locks-held)."""
    applying = state["applying"]
    try:
        _services_reconcile()
        journal(state, "services")
        problems = restart_agents()
        if problems:
            raise UpdateError("restart_failed: " + ",".join(problems))
        journal(state, "restart")
        errors = _postcheck(state, applying["sha"])
        if errors:
            raise UpdateError("postcheck_failed: " + ",".join(errors))
        journal(state, "postcheck")
    except UpdateError:
        raise
    state.setdefault("applied", []).append({
        "tag": applying["tag"], "sha": applying["sha"],
        "prev_sha": applying["prev_sha"],
        "backup_path": applying.get("backup_path"),
        "schema_bump": applying.get("schema_bump", False),
        "plugin_changed": applying.get("plugin_changed", False),
        "command_id": applying.get("command_id"), "at": time.time()})
    state["stages"].append({"stage": "done", "at": time.time()})
    state["applying"] = None
    save_state(state)
    _enqueue_notice(
        f"[MCS] 更新を適用しました: {applying['tag']}\n"
        f"prev: {applying['prev_sha'][:12]} → {applying['sha'][:12]}")
    return 0


def _update_mode(cfg: dict) -> str:
    upd = cfg.get("update") if isinstance(cfg.get("update"), dict) else {}
    return upd.get("mode", "off")


def apply(tag: str | None, sha: str | None, command_id: str | None,
          base_sha: str | None = None) -> int:
    """The staged apply pipeline (④).

    Lock discipline: update.lock is acquired BEFORE the first journal
    write and held for the whole pipeline, so every mutation of
    update_state.json is serialized and 'journal exists + lock free'
    reliably means the writer died. run.lock is taken second (after
    the long pre-checks) — run.lock holders (drainer ticks) never
    block on update.lock, so no lock-order inversion exists."""
    if not tag or not SEMVER_RE.fullmatch(tag):
        print("apply: bad tag", tag)
        return 2
    if sha is not None and not HEX_RE.fullmatch(sha):
        print("apply: bad sha", sha)
        return 2
    state = load_state()
    if state.get("_corrupt"):
        print("update_state corrupt — refusing to act")
        return 2
    if state.get("applying") or state.get("stages"):
        return recover_interrupted()

    cfg = load_config()
    run_fd = upd_fd = None
    quiesced = False
    rollback_failed = False

    def bail(reason: str) -> int:
        nonlocal rollback_failed
        transient = reason.split(":")[0] in _TRANSIENT_BAIL
        stages = [s.get("stage") for s in state.get("stages", [])]
        merged = "merge" in stages or "post_merge" in stages
        if merged and state.get("applying"):
            # the tree is already on the new code — a plain abort would
            # leave the system running NEW code while claiming nothing
            # happened; fall back to the surgical rollback path
            try:
                _rollback_tree(state["applying"])
                reason += " (rolled back)"
            except Exception as e:
                rollback_failed = True
                reason += f" (rollback failed: {e} — escalate)"
        if not rollback_failed:
            # keep remnants when the rollback itself failed — recovery
            # must still be able to see and escalate the wedged state
            state["applying"] = None
            state["stages"] = []
        if transient:
            # never attempted: clean journal, leave the receipt
            # pending, no attempt record, no notification — the next
            # check retries
            state["stages"] = []
            save_state(state)
            return 2
        if command_id:
            state.setdefault("executed", {})[command_id] = {
                "result": "failed", "detail": reason[:200],
                "at": time.time()}
        _record_attempt(state, tag, "failed", reason)
        save_state(state)
        if quiesced:
            problems = restart_agents()
            _remove_marker()
            if problems:
                reason += " restart:" + ",".join(problems)
        _enqueue_notice(f"[MCS] 更新 {tag or ''} を中止しました: {reason}",
                        use_run_lock=run_fd is None)
        return 1

    try:
        # update.lock BEFORE the first journal write (F6/H2): from now
        # on a free lock proves no updater is alive, and every state
        # write below is serialized.
        upd_fd = acquire_update_lock()
        if upd_fd is None:
            return 0                    # another updater is alive
        state = load_state()            # fresh view under the lock
        if state.get("applying") or state.get("stages"):
            os.close(upd_fd)
            upd_fd = None
            return recover_interrupted()

        journal(state, "local_checks")
        errors = precheck_local(cfg)
        if errors:
            return bail("precheck_local: " + ",".join(errors))

        journal(state, "remote_verify")
        remote = remote_tag_sha(tag)
        if sha is None:
            sha = remote                # manual `apply --tag` convenience
        if sha is None or remote != sha:
            return bail("tag_moved: remote sha changed — 要再レビュー")

        journal(state, "fetch")
        _git(["fetch", "--tags", "origin"], timeout=T_FETCH)
        local_sha = _git_out(
            ["rev-parse", f"refs/tags/{tag}^{{commit}}"]).strip()
        if local_sha != sha:
            return bail("local_tag_sha_mismatch")

        journal(state, "tag_checks")
        errors = precheck_tag(tag)
        bump = [e for e in errors if e.startswith("schema_bump:")]
        errors = [e for e in errors if not e.startswith("schema_bump:")]
        if errors:
            return bail("tag_checks: " + ",".join(errors))
        if bump and command_id is None \
                and _update_mode(cfg) == "auto":
            # schema-bumping updates NEVER auto-apply (R2); a human
            # receipt is the only way past this gate
            return bail("schema_bump_auto_blocked: " + bump[0])

        journal(state, "backup")
        import maintenance
        bpath = maintenance.preupdate_backup(LEDGER)
        snap = None
        try:
            with open(MANIFEST_PATH, encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        state["baseline_check"] = _baseline_check(cfg)
        save_state(state)

        journal(state, "lock")
        run_fd = _acquire_run_lock_wait()
        if run_fd is None:
            return bail("run_lock_timeout")
        # re-verify inside the locks (TOCTOU on state + tree, S11/S20)
        if not _tree_clean():
            return bail("tree_dirty_after_lock")
        if base_sha and _head_sha() != base_sha:
            return bail("base_sha_mismatch: 承認時点と HEAD が異なる"
                        " — 要再レビュー")
        if _head_sha() == sha:
            # idempotent: already on the target — record and exit
            state["stages"] = []
            if command_id:
                state.setdefault("executed", {})[command_id] = {
                    "result": "already_applied", "at": time.time()}
            save_state(state)
            return 0
        if command_id and state.get("executed", {}).get(command_id):
            state["stages"] = []
            save_state(state)
            return 0

        state["applying"] = {
            "tag": tag, "sha": sha, "prev_sha": _head_sha(),
            "plugin_changed": bool(_git_out(
                ["diff", "--name-only", "-z", "HEAD", tag, "--",
                 "hermes_plugin"]).strip("\0")),
            "schema_bump": bool(bump),
            "backup_path": bpath, "manifest_snapshot": snap,
            "command_id": command_id, "at": time.time()}
        journal(state, "applying")

        journal(state, "quiesce")
        # mark BEFORE quiesce: a partial quiesce (one drainer stopped,
        # then a stop failure) must still trigger restart in bail —
        # restart_agents() is idempotent
        quiesced = True
        quiesce()

        journal(state, "merge")
        r = _git(["merge", "--ff-only", f"refs/tags/{tag}"],
                 timeout=T_MERGE)
        if r.returncode != 0:
            raise UpdateError("merge_failed: " + r.stderr.strip()[:200])
        if _head_sha() != sha or not _tree_clean():
            raise UpdateError("merge_verify_failed")

        # post-merge under NEW code; parent keeps both locks held.
        # start_new_session so a timeout can killpg() grandchildren too.
        journal(state, "post_merge")
        env = {k: v for k, v in os.environ.items()
               if k not in _UPDATE_ENV_STRIP}
        env[_UPDATE_PM_ENV] = sha       # child handshake token (L17)
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--post-merge"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, start_new_session=True)
        try:
            cout, cerr = child.communicate(timeout=T_POST_MERGE)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, 9)
            except OSError:
                pass
            cout, cerr = "", "post_merge_timeout"
            child.returncode = -9
        if child.returncode != 0:
            state = load_state()
            stages = [s.get("stage") for s in state.get("stages", [])]
            if not ("done" in stages
                    and state.get("applying") is None):
                # the child may have already written 'done' and died
                # between save and exit — that IS a success (H5)
                raise UpdateError(
                    "post_merge_failed: "
                    + (cerr or cout or "").strip()[:300])

        state = load_state()
        state.setdefault("executed", {})
        if command_id:
            state["executed"][command_id] = {"result": "applied",
                                             "at": time.time()}
        _record_attempt(state, tag, "applied")
        state["stages"] = []
        save_state(state)
        quiesced = False
        # gateway restart is fire-and-forget AFTER applied is durable —
        # a synchronous restart would deadlock when this updater is a
        # gateway descendant (S12)
        applied_entry = state.get("applied") or [{}]
        if applied_entry[-1].get("plugin_changed"):
            restart_gateway(cfg)
        return 0
    except UpdateError as e:
        return bail(str(e))
    except Exception as e:
        return bail(f"unexpected:{type(e).__name__}")
    finally:
        if quiesced:
            _remove_marker()
        for fd in (run_fd, upd_fd):
            if fd is not None:
                os.close(fd)


def _baseline_check(cfg: dict) -> list[str]:
    try:
        import mcs_setup
        errs, _ = mcs_setup.check_environment(cfg)
        return errs
    except Exception:
        return []


def _gateway_restart_if_needed(state: dict) -> None:
    applying = state.get("applying") or {}
    applied = (state.get("applied") or [{}])[-1]
    if applying.get("plugin_changed") or applied.get("plugin_changed"):
        restart_gateway(load_config())


# -------------------------------------------------------------- rollback

def _reconcile_membership(desired: dict) -> list[str]:
    """Delete owned-but-undesired cron jobs + agents. Recreation is
    left to `services` (old code re-renders content)."""
    problems = []
    import mcs_setup
    cfg = load_config()
    hermes = mcs_setup._hermes_exe(cfg)
    desired_cron = {(d.get("script") or ""): d for d in
                    (desired or {}).get("cron", [])}
    owned_scripts = {s for _, _, s in mcs_setup.CRON_JOBS}
    owned_scripts |= set(desired_cron)
    if mcs_setup._hermes_ok(hermes):
        try:
            r = subprocess.run([hermes, "cron", "list", "--all"],
                               capture_output=True, text=True,
                               timeout=30)
            for block in re.finditer(
                    r"^\s{2}([0-9a-f]{6,})\s+\[[^\]]*\]\n"
                    r"((?:\s{4}\S[^\n]*\n?)+)", r.stdout, re.M):
                jid, body = block.group(1), block.group(2)
                fields = dict(re.findall(
                    r"^\s{4}(\w[\w ]*?):\s{2,}(.+)$", body, re.M))
                script = (fields.get("Script") or "").strip()
                if script and script in owned_scripts \
                        and script not in desired_cron:
                    rr = subprocess.run(
                        [hermes, "cron", "remove", jid],
                        capture_output=True, text=True, timeout=30)
                    if rr.returncode != 0:
                        problems.append(f"cron_remove_failed:{script}")
        except (OSError, subprocess.TimeoutExpired):
            problems.append("cron_list_unverifiable")
    desired_agents = {a.get("label") for a in
                      (desired or {}).get("agents", [])}
    def _owned(label: str) -> bool:
        return (label.startswith("ai.mcs.extract-")
                or label.startswith("local.mcs-")) \
            and label not in EXCLUDED_LABELS
    for path in glob.glob(os.path.join(AGENTS_DIR, "*.plist")):
        label = os.path.basename(path)[:-6]
        if not _owned(label):
            continue
        if label not in desired_agents and label not in \
                set(mcs_setup.AGENT_LABELS):
            subprocess.run(["launchctl", "bootout",
                            f"gui/{_uid()}/{label}"],
                           capture_output=True, timeout=T_GIT)
            try:
                os.unlink(path)
            except OSError:
                pass
    return problems


def _rollback_tree(entry: dict) -> None:
    """Surgical restore of prev_sha — shared by rollback() and the
    post-merge failure path. Caller holds both locks and drainers are
    already quiesced. The delete set is pinned to the recorded SHAs —
    never a tag name that could have been re-pointed (F4)."""
    prev = entry["prev_sha"]
    _git_out(["reset", "--hard", prev])
    _surgical_delete(prev, entry.get("sha") or prev)
    if _head_sha() != prev or not _tree_clean():
        raise UpdateError("rollback_verify_failed")
    if entry.get("schema_bump") and entry.get("backup_path"):
        _restore_db(entry["backup_path"])
    problems = _reconcile_membership(
        entry.get("manifest_snapshot")
        if "manifest_snapshot" in entry
        else load_state().get("manifest_snapshot"))
    _services_reconcile()
    if problems:
        raise UpdateError("membership: " + ",".join(problems))


def rollback(command_id: str | None = None) -> int:
    """Restore the latest applied entry's prev_sha (⑤).

    Journaled exactly like apply(): a crash after quiesce must still be
    recoverable, so a rollback-shaped `applying` record is written
    before anything is stopped."""
    state = load_state()
    if state.get("_corrupt"):
        print("update_state corrupt — refusing to act")
        return 2
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        print("another updater is active — retry later")
        return 2
    run_fd = None
    quiesced = False
    try:
        state = load_state()            # fresh view under the lock
        applied = state.get("applied") or []
        entry = applied[-1] if applied else state.get("applying")
        if not entry or not entry.get("prev_sha"):
            if command_id:
                state.setdefault("executed", {})[command_id] = {
                    "result": "nothing_to_rollback", "at": time.time()}
                save_state(state)
            print("nothing_to_rollback")
            return 0
        run_fd = _acquire_run_lock_wait()
        if run_fd is None:
            return 2                    # transient — receipt stays
        prev = entry["prev_sha"]
        try:
            if not _tree_clean():
                raise UpdateError("tree_dirty_before_rollback")
            # crash-visible journal BEFORE quiesce (H4): recover can
            # converge the tree toward prev even if we die mid-reset
            state["applying"] = {
                "tag": "rollback:" + (entry.get("tag") or "?"),
                "sha": prev, "prev_sha": _head_sha(), "rollback": True,
                "plugin_changed": entry.get("plugin_changed"),
                "schema_bump": entry.get("schema_bump"),
                "backup_path": entry.get("backup_path"),
                "manifest_snapshot": entry.get("manifest_snapshot"),
                "command_id": command_id, "at": time.time()}
            journal(state, "rollback")
            quiesced = True
            quiesce()
            rb_error = None
            try:
                _rollback_tree(entry)
            except UpdateError as e:
                rb_error = e
            # restart ALWAYS runs after quiesce — a failed rollback
            # must never leave drainers down (H4)
            problems = restart_agents()
            _remove_marker()
            quiesced = False
            if rb_error or problems:
                raise UpdateError(
                    "rollback_failed: "
                    + ",".join(filter(None,
                                      [str(rb_error) if rb_error else None,
                                       ",".join(problems)])))
            state = load_state()
            state["applied"] = applied[:-1]
            state["applying"] = None
            state["stages"] = []
            state.setdefault("attempts", {})[
                entry.get("tag") or "?"] = {
                "result": "rolled_back", "at": time.time()}
            if command_id:
                state.setdefault("executed", {})[command_id] = {
                    "result": "rolled_back", "at": time.time()}
            save_state(state)
            # gateway restart AFTER the durable save (self-deadlock)
            _gateway_restart_if_needed(state)
            _enqueue_notice(f"[MCS] ロールバックしました: "
                            f"{entry.get('tag')} → {prev[:12]}")
            return 0
        except UpdateError as e:
            # consume the receipt — the attempt genuinely ran and the
            # human must see a result, not an infinite retry
            if command_id:
                state.setdefault("executed", {})[command_id] = {
                    "result": "rollback_failed", "detail": str(e)[:200],
                    "at": time.time()}
                save_state(state)
            _enqueue_notice(f"[MCS] ロールバックに失敗しました: {e}",
                            use_run_lock=run_fd is None)
            print("rollback failed:", e)
            return 1
    finally:
        if quiesced:
            _remove_marker()
        for fd in (run_fd, upd_fd):
            if fd is not None:
                os.close(fd)


def _db_version(path: str) -> int | None:
    try:
        con = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _restore_db(backup_path: str) -> None:
    """Verified restore — validate the backup before touching the live
    DB. Skips when the live schema already matches the backup (a failed
    apply may never have migrated). Removes WAL/SHM sidecars FIRST so a
    stale journal can never be replayed against the restored file
    (F9); caller already quiesced every writer.
    Loses records written between apply and restore; Discord posts
    already sent cannot be retracted (design R2)."""
    import ledger
    if not ledger.valid_mcs_db(backup_path):
        raise UpdateError("backup_invalid: " + backup_path)
    live_ver = _db_version(LEDGER)
    back_ver = _db_version(backup_path)
    if live_ver is not None and live_ver == back_ver:
        return                              # already at backup schema
    for side in (LEDGER + "-wal", LEDGER + "-shm", LEDGER + "-journal"):
        try:
            os.unlink(side)
        except OSError:
            pass
    tmp = LEDGER + ".restore-tmp"
    with open(backup_path, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, LEDGER)
    dfd = os.open(os.path.dirname(LEDGER), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    if not ledger.valid_mcs_db(LEDGER):
        raise UpdateError("restore_verify_failed")


# -------------------------------------------------------------- recover

def recover_interrupted(if_stale: bool = False) -> int:
    """Journal-driven recovery (⑤). Safe to call any time.

    Classification is by MEASUREMENT (MERGE_HEAD / HEAD / tree), never
    by stage labels alone — 'merge' is journaled BEFORE the merge runs,
    so a stage name cannot distinguish pre-merge from mid-merge
    crashes. Lock discipline: update.lock first (a free lock proves no
    updater is alive), run.lock second; if run.lock stays busy we
    defer and touch NOTHING (H1/F7)."""
    state = load_state()
    if state.get("_corrupt"):
        _report("corrupt_state", "update_state.json unreadable — escalate")
        return 2
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        return 0                       # apply alive — leave it alone
    run_fd = None
    try:
        run_fd = _acquire_run_lock_wait(tries=2)
        if run_fd is None:
            return 0                   # a tick is running — retry next
        state = load_state()           # fresh view under both locks
        applying = state.get("applying")
        stages = [s.get("stage") for s in state.get("stages", [])]
        if not applying and not stages:
            _remove_marker()           # orphan marker — writer is dead
            return 0
        if if_stale:
            last = max(
                [s.get("at", 0) for s in state.get("stages", [])]
                + [(applying or {}).get("at", 0)])
            if last and time.time() - last < STALE_S:
                return 0               # fresh journal — leave it alone
        removed = _clean_stale_git_locks()
        prev = (applying or {}).get("prev_sha")
        target = (applying or {}).get("sha")

        def escalate(detail: str) -> int:
            """Fail closed on the journal but keep the rest of the
            system alive: drainers back up, marker gone, human told."""
            cid = (applying or {}).get("command_id")
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": "escalated", "detail": detail[:200],
                    "at": time.time()}
                save_state(state)
            _report("escalate", detail)
            problems = restart_agents()
            _remove_marker()
            _enqueue_notice(
                f"[MCS] 更新の中断復旧ができません（要手動対応）: {detail}"
                + ((" restart:" + ",".join(problems)) if problems else ""))
            return 1

        if os.path.exists(os.path.join(REPO, ".git", "MERGE_HEAD")):
            if not prev:
                return escalate("MERGE_HEAD without known prev_sha")
            r = _git(["merge", "--abort"])
            if r.returncode != 0 or _head_sha() != prev:
                return escalate("merge --abort failed")
            _finish_recovery(state, "merge_aborted", removed)
            return 0
        if "done" in stages and not applying:
            # child wrote 'done' then died before the parent finished
            # bookkeeping — complete it
            applied = state.get("applied") or []
            cid = (applied[-1] or {}).get("command_id") if applied else None
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": "applied", "at": time.time()}
            state["stages"] = []
            save_state(state)
            _remove_marker()
            _report("resumed_done", "completed bookkeeping after crash")
            _enqueue_notice("[MCS] 中断された更新の後処理を完了しました")
            return 0
        if not applying:
            # pre-'applying' remnant: nothing was ever mutated — safe
            # to clean (previously wedged all updates, F10)
            _finish_recovery(state, "interrupted_pre_merge", removed)
            return 0

        head = _head_sha()
        clean = _tree_clean()
        if head == target:
            if not clean:
                # crash during the target checkout — converge to target
                _git_out(["reset", "--hard", target])
                _surgical_delete(target, prev or target)
                if not _tree_clean():
                    return escalate("target tree could not be cleaned")
            if applying.get("rollback") and applying.get("backup_path"):
                try:
                    _restore_db(applying["backup_path"])
                except UpdateError as e:
                    return escalate("rollback db restore: " + str(e))
            try:
                _services_reconcile()
                problems = restart_agents()
                errors = _postcheck(state, target)
                if problems or errors:
                    return escalate("resume postcheck: "
                                    + ",".join(problems + errors))
                state.setdefault("applied", []).append(applying)
                state["applying"] = None
                state["stages"] = []
                cid = applying.get("command_id")
                if cid:
                    state.setdefault("executed", {})[cid] = {
                        "result": "applied", "at": time.time()}
                save_state(state)
                _remove_marker()
                _report("resumed", "post-merge completed after crash")
                _enqueue_notice(
                    "[MCS] 更新の中断を検出し、post-merge を完了しました")
                _gateway_restart_if_needed(state)
                return 0
            except UpdateError as e:
                return escalate("resume failed: " + str(e))
        if prev and head == prev:
            if not clean:
                # crash mid-merge checkout without MERGE_HEAD — the
                # mixed-tree case stage-gating could never reach (F1)
                _git_out(["reset", "--hard", prev])
                _surgical_delete(prev, target or prev)
                if _head_sha() != prev or not _tree_clean():
                    return escalate("prev tree could not be cleaned")
                _finish_recovery(state, "mixed_tree_reset", removed)
                return 0
            _finish_recovery(state, "interrupted_pre_merge", removed)
            return 0
        return escalate("unclassifiable repo state — "
                        f"HEAD={head[:12]} prev={str(prev)[:12]} "
                        f"target={str(target)[:12]} clean={clean}")
    finally:
        for fd in (run_fd, upd_fd):
            if fd is not None:
                os.close(fd)


def _finish_recovery(state: dict, result: str, removed: list) -> None:
    applying = state.get("applying") or {}
    cid = applying.get("command_id")
    if cid:
        # consume the receipt — without this the next check would
        # re-apply the tag and loop crash→recover→apply (S9 followup)
        state.setdefault("executed", {})[cid] = {
            "result": "interrupted_recovered", "at": time.time()}
    if applying.get("tag"):
        state.setdefault("attempts", {})[applying["tag"]] = {
            "result": result, "at": time.time()}
    state["applying"] = None
    state["stages"] = []
    save_state(state)
    _remove_marker()
    problems = restart_agents()
    _report(result, "removed locks: " + ",".join(removed)
            + (" restart:" + ",".join(problems) if problems else ""))
    _enqueue_notice(f"[MCS] 更新が中断され復旧しました: {result}")


def _report(result: str, detail: str) -> None:
    """Atomic report write — a torn report must never mislead a human
    checking `status` after a crash."""
    try:
        fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".ureport.",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"result": result, "detail": detail,
                       "at": time.time()}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REPORT_PATH)
    except OSError:
        pass


# -------------------------------------------------------------------- CLI

def cmd_check() -> int:
    """Daily driver: detect -> notify -> pending approvals -> auto.

    Two phases: (1) lock-free network detection, (2) ALL state
    mutations inside update.lock after a fresh reload — a stale dict
    can never clobber a live journal (F5). Receipt scanning runs even
    when remote detection failed, so a committed human approval never
    waits on a flaky network (P16)."""
    state = load_state()
    if state.get("_corrupt"):
        print("update_state corrupt — refusing to act")
        return 2
    cfg = load_config()
    upd = cfg.get("update") if isinstance(cfg.get("update"), dict) else {}
    mode = _update_mode(cfg)
    # --- lock-free detection -----------------------------------------
    tag = sha = None
    if mode != "off":
        try:
            tag, sha = detect_latest(bool(upd.get("include_prerelease")))
        except UpdateError:
            tag = sha = None            # receipts still get processed
    cur_tag, cur_sha = current_version()
    notes = impact = None
    if tag and tag != state.get("latest_tag"):
        try:
            # fetch objects BEFORE notes/impact — otherwise the summary
            # silently runs against missing objects (dead code, F15)
            _git(["fetch", "--tags", "origin"], timeout=T_FETCH)
        except UpdateError:
            pass
        notes = fetch_notes(tag)
        try:
            impact = impact_summary(cur_sha, tag) if cur_sha else []
        except UpdateError:
            impact = []
    # --- serialized state update + decision --------------------------
    picked = auto = None
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        return 0                        # another updater is alive
    try:
        state = load_state()            # fresh view under the lock
        if tag:
            if tag != state.get("latest_tag"):
                state.update({"latest_tag": tag, "latest_sha": sha,
                              "current_tag": cur_tag,
                              "current_sha": cur_sha,
                              "first_seen": time.time()})
            need_notify = cur_sha and sha != cur_sha \
                and state.get("notified_at") != tag
            if need_notify:
                lines = [f"[MCS] 新しいバージョン {tag} を検出しました"
                         f"（現在 {cur_tag or cur_sha[:12]}）",
                         "", (notes or "(release notes 取得不可)")[:1500],
                         ""]
                lines += ["影響: " + i for i in (impact or [])]
                lines += ["", "適用: /mcs "
                          '{"op":"control","phase":"preview",'
                          f'"action":"update_apply","tag":"{tag}",'
                          '"reason":"<理由>"}']
                if _enqueue_notice("\n".join(lines), use_run_lock=True):
                    state["notified_at"] = tag
        pendings, consumed = scan_pending_approvals(state)
        for cid, result in consumed:
            state.setdefault("executed", {})[cid] = {
                "result": result, "at": time.time()}
        picked = pendings[0] if pendings else None
        if not picked and mode == "auto" and tag and sha \
                and sha != cur_sha:
            delay = upd.get("auto_delay_h", 24)
            first_seen = state.get("first_seen") or 0
            if time.time() - first_seen >= delay * 3600 \
                    and not state.get("attempts", {}).get(tag):
                auto = (tag, sha)
        save_state(state)
    finally:
        os.close(upd_fd)
    # --- execution (apply/rollback re-acquire their own locks) --------
    if mode == "off":
        return 0                        # receipts stay dormant (Q17)
    if picked and picked.get("rollback"):
        return rollback(command_id=picked.get("command_id"))
    if picked:
        return apply(picked["tag"], picked["target_sha"],
                     picked["command_id"], picked.get("base_sha"))
    if auto:
        return apply(auto[0], auto[1], None)
    return 0


def cmd_status() -> int:
    state = load_state()
    if state.get("_corrupt"):
        print("update_state.json: corrupt/unknown — escalate")
        return 2
    cur_tag, cur_sha = current_version()
    print(json.dumps({"current_tag": cur_tag, "current_sha": cur_sha,
                      "state": state}, ensure_ascii=False, indent=2,
                     default=str))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--post-merge", action="store_true",
                    help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    ap_p = sub.add_parser("apply")
    ap_p.add_argument("--tag")
    ap_p.add_argument("--sha")
    ap_p.add_argument("--base-sha")
    ap_p.add_argument("--command-id")
    sub.add_parser("check")
    sub.add_parser("status")
    sub.add_parser("rollback")
    sub.add_parser("recover")
    args = ap.parse_args()
    if args.post_merge:
        state = load_state()
        applying = state.get("applying") or {}
        # the post-merge stage is only reachable from the parent apply
        # — the env token proves we were spawned by it, not invoked
        # directly against whatever journal happens to exist (L17)
        if state.get("_corrupt") or not applying \
                or os.environ.get(_UPDATE_PM_ENV) != applying.get("sha"):
            return 2
        return _post_merge(state)
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "check" or args.cmd is None:
        return cmd_check()
    if args.cmd == "apply":
        return apply(args.tag, args.sha, args.command_id,
                     base_sha=args.base_sha)
    if args.cmd == "rollback":
        return rollback()
    if args.cmd == "recover":
        return recover_interrupted()
    return 2


if __name__ == "__main__":
    sys.exit(main())
