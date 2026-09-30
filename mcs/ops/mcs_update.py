"""MCS self-update — detection, apply, rollback, interrupted recovery.

Drives the update lifecycle documented in docs/auto-update-plan.md:

  status    print update_state.json + current/latest tag summary
  check     daily cron driver: detect latest tag, notify once, scan the
            command_receipts approval queue, auto-apply when mode=auto
  apply     run the staged apply pipeline for a pending approval (or
            --tag explicit). Takes update.lock -> run.lock, journals
            every stage, quiesces resident drainers, merges, then runs
            post-merge steps in a NEW-code subprocess (locks held).
  rollback  restore the last applied entry's prev_sha (+ DB restore when
            the apply carried a schema_bump)
  recover   journal-driven interrupted-apply recovery (also invoked by
            the independent launchd watchdog via mcs_recover.py)

Safety invariants: never touches protected paths (data/, config.json,
.env), never runs `git clean`, treats unverifiable as failure, and the
approval boundary is the command_receipts commit on the receipt-driven
path (check -> detached spawn, which passes no argv). A local operator's
`apply --command-id` is NOT looked up in receipts — the CLI trusts the
shell user it runs as.
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import unicodedata
import urllib.request
from contextlib import suppress

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401,E402  registers every subdir as import root

from mcs_util import (UPDATE_MARKER_NAME, acquire_run_lock,  # noqa: E402
                      atomic_write, launchd_bootstrap, load_config)

HOME = os.path.expanduser("~/.mcs")
DATA = os.path.join(HOME, "data")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
LEDGER = os.path.join(DATA, "ledger.db")
STATE_PATH = os.path.join(DATA, "update_state.json")
UPDATE_LOCK = os.path.join(DATA, "update.lock")
RUN_LOCK = os.path.join(DATA, "run.lock")
MARKER_PATH = os.path.join(DATA, UPDATE_MARKER_NAME)
MANIFEST_PATH = os.path.join(DATA, "service_manifest.json")
REPORT_PATH = os.path.join(DATA, "recovery_report.json")
RESTORE_REPORT_PATH = os.path.join(DATA, "restore_report.json")
BACKUP_DIR = os.path.join(DATA, "backups")
SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
WRAPPER = os.path.join(SCRIPTS_DIR, "mcs_update.sh")
RECOVERY_TOOL = os.path.expanduser("~/.mcs-recovery/mcs_recover.py")

RESIDENT_LABELS = ("ai.mcs.extract-drainer", "ai.mcs.extract-drainer-2")
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
# launchctl answers in well under 1s and the drainers install no SIGTERM
# handler (bootout returns as soon as they die), so 10s is >10x headroom
# for a loaded machine while bounding a wedged launchd. Budget inside
# the post-merge child (T_POST_MERGE=600), every call hung:
#   restart_agents: a label is only STARTED before RESTART_BUDGET_S, and
#     one resident label costs at most bootout 10 + bootstrap 3x(10+1s)
#     + print 10 + pid wait (15 + last 10) = 78s  =>  <= 120 + 78 = 198s
#     (per-call bound alone: 2x78 + 2x(10+33+10) = 262s for 4 labels)
#   + services 120 + postcheck (git 2x30 + keychain 2x60 + urlopen 2x3
#     + print 2x10) = 326  =>  child worst ~524s < 600: the child is not
#     killed mid-restart only for the parent's bail to redo the restart.
T_LAUNCHCTL = 10
RESTART_BUDGET_S = 120
STALE_S = 1800            # no stage progress for this long => stale apply
# an unchanged unresolved escalation re-notifies at most this often
# (health_watch.REALERT_S dedup convention; longer here because the
# human was already told it needs manual action)
ESCALATE_REALERT_S = 6 * 3600
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


class RestoreConsentPending(UpdateError):
    """The schema-bump DB replace is durably held: no ops.restore_approve
    receipt matches the current loss report. Writers/senders stay frozen
    and the next check/watchdog pass re-evaluates — this is a hold, not
    a failure, so callers must NOT consume the receipt or restart
    drainers."""

    def __init__(self, report: dict):
        super().__init__("restore_consent_pending:" + report["report_id"])
        self.report = report


# ---------------------------------------------------------------- state

def _default_state() -> dict:
    return {"v": 1, "stages": [], "applied": [], "attempts": {},
            "executed": {}, "applying": None}


def _valid_state(state):
    """Check journal shapes before any recovery decision or mutation."""
    if not isinstance(state, dict) or type(state.get("v")) is not int \
            or state["v"] != 1:
        return False
    for key in ("stages", "applied"):
        if not isinstance(state.get(key, []), list) \
                or any(not isinstance(row, dict) for row in state.get(key, [])):
            return False
    if state.get("applying") is not None and not isinstance(state["applying"], dict):
        return False
    for key in ("attempts", "executed", "restore_consent"):
        if key in state and not isinstance(state[key], dict):
            return False
    records = [*state.get("stages", []), *state.get("applied", []),
               state.get("applying") or {}]
    for row in records:
        stamp = row.get("at", 0)
        if type(stamp) not in (int, float) or not 0 <= stamp < 1e12:
            return False
    return all(isinstance(row.get("stage"), str)
               for row in state.get("stages", []))


def load_state() -> dict:
    """Lock-free read — writers publish atomically so a torn read never
    happens. Unparsable state is 'corrupt': callers must escalate and
    touch nothing (S17/S24)."""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return _default_state()
    except (OSError, ValueError, RecursionError):
        return {"_corrupt": True}
    if not _valid_state(state):
        return {"_corrupt": True}
    return {**_default_state(), **state}


def save_state(state: dict) -> None:
    """tmp -> fsync -> os.replace -> dir fsync (R13). Caller holds
    update.lock (or is the --locks-held post-merge child)."""
    atomic_write(STATE_PATH,
                 lambda f: json.dump(state, f, ensure_ascii=False,
                                     sort_keys=True),
                 tmp_prefix=".update_state.")


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
    except subprocess.TimeoutExpired as e:
        raise UpdateError("git_timeout: " + " ".join(args[:1])) from e
    except OSError as e:
        raise UpdateError(f"git_spawn_failed: {e}") from e


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
    if not isinstance(name, str) or len(name) > 255:
        return None
    m = SEMVER_RE.fullmatch(name)
    if not m:
        return None
    pre = m.group(4)
    if pre and any(not part for part in pre.split(".")):
        return None
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part)
                        for part in pre.split(".")) if pre else ()
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
            0 if pre else 1, identifiers)


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
    with suppress(OSError):
        need = os.path.getsize(LEDGER) * 2 + 64 * 1024 * 1024
        if shutil.disk_usage(DATA).free < need:
            errors.append("insufficient_disk")
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
            errors.extend(f"ignored_path_tracked: {name}"
                          for name in r.stdout.split("\0") if name)
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
                errors.extend("new_config: " + e
                              for e in json.loads(probe.stdout.strip() or "[]"))
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


def _run(argv: list, timeout: int = T_LAUNCHCTL
         ) -> subprocess.CompletedProcess:
    """subprocess.run that never raises — mcs_setup._run's contract,
    kept local so a rolled-back tree never mixes generations. A hung
    command yields returncode 124 (as timeout(1)), one that cannot start
    127: every launchctl caller records a per-label problem and moves on,
    so one wedged label never aborts a restart loop mid-way (H4)."""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "",
                                           f"timed out after {timeout}s")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def _agent_pid(label: str, unknown: int | None = None) -> int | None:
    """Running pid, or None when the label is not running. A launchctl
    that hung or could not start answers `unknown` — quiesce passes a
    non-None value so an unverifiable stop is never taken as stopped."""
    r = _run(["launchctl", "print", f"gui/{_uid()}/{label}"])
    if r.returncode in (124, 127):
        return unknown
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


def _awaiting_consent_marker() -> dict | None:
    """notify_cards.restore_awaiting_consent, kept local: recover runs on
    whatever tree HEAD is (a rolled-back prev may predate that helper),
    and a failed import there would leave drainers down. Same fail-closed
    reading: an awaiting_consent phase, or a marker present but
    unreadable/unknown, holds."""
    path = os.path.join(DATA, "restore_pending.json")
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except FileNotFoundError:
        return {"unreadable": True} if os.path.lexists(path) else None
    except (OSError, ValueError, RecursionError):
        return {"unreadable": True}
    if not isinstance(data, dict):
        return {"unreadable": True}
    if data.get("phase") == "awaiting_consent":
        return data
    if data.get("phase") == "restored" \
            or ("phase" not in data and "restored_at" in data):
        return None
    return {"unreadable": True}


def _remove_marker() -> None:
    with suppress(OSError):
        os.unlink(MARKER_PATH)


def _stray_drainer_pids() -> list[int] | None:
    """Same-uid python processes running the drainer scripts — the
    pattern requires an interpreter argv0 so editors, test runners and
    unrelated commands containing the filename never match (H7).
    None = unverifiable (pgrep hung, missing, or exit >1 — 1 is "no
    match"): quiesce must never read that as "no strays"."""
    r = _run(["pgrep", "-u", str(_uid()), "-f", _STRAY_RE], timeout=T_GIT)
    if r.returncode not in (0, 1):
        return None
    return [int(p) for p in r.stdout.split()
            if p.isdigit() and int(p) != os.getpid()]


def quiesce() -> list[str]:
    """Stop resident drainers + stray helpers; returns old pids' labels
    for restart verification. Marker first — helper launchers (cron
    catchup) must see it before any process is stopped (S8)."""
    _write_marker()
    stopped = []
    for label in RESIDENT_LABELS:
        _run(["launchctl", "bootout", f"gui/{_uid()}/{label}"])
        deadline = time.time() + 15
        while time.time() < deadline:
            if _agent_pid(label, unknown=-1) is None:
                break
            time.sleep(0.5)
        if _agent_pid(label, unknown=-1) is not None:
            raise UpdateError(f"drainer_stop_failed: {label}")
        stopped.append(label)
    # stray sweep: helpers may have spawned drainers outside launchd
    for i in range(4):
        pids = _stray_drainer_pids()
        if not pids:                   # [] swept, None decided below
            break
        sig = 15 if i < 3 else 9       # TERM, then KILL on the last pass
        for pid in pids:
            with suppress(OSError):
                os.kill(pid, sig)
        time.sleep(1)
    else:
        pids = _stray_drainer_pids()
    if pids is None:
        # fail closed like drainer_stop_failed: callers restart what
        # was stopped and never merge beside an unseen live drainer
        raise UpdateError("stray_drainer_unverifiable")
    if pids:
        raise UpdateError("stray_drainer_survived")
    return stopped


def _bootstrap_agent(label: str, plist: str) -> bool:
    """Verified bootstrap (mcs_util.launchd_bootstrap — imported at
    process start, so a rolled-back tree never mixes generations)."""
    return launchd_bootstrap(label, plist, _run) is None


def restart_agents(bounce: bool = True) -> list[str]:
    """Re-bootstrap resident drainers and verify a NEW pid; watchers are
    verified loaded only (R20). Returns list of verify failures. Labels
    not yet started when RESTART_BUDGET_S runs out are reported as
    restart_deadline:<label> (see the T_LAUNCHCTL budget).

    bounce=False is the idempotent ensure-running pass (a repeated
    escalation of the same condition — H4 only needs drainers UP): a
    drainer launchd shows running is never touched; one not running,
    not loaded or unverifiable (hung print — fail closed) is started:
    bootstrap unless loaded, then `kickstart` without -k, which starts
    a stopped job and never kills a running one."""
    problems = []
    deadline = time.time() + RESTART_BUDGET_S
    for label in RESIDENT_LABELS:
        if time.time() >= deadline:
            problems.append(f"restart_deadline:{label}")
            continue
        plist = os.path.join(AGENTS_DIR, label + ".plist")
        target = f"gui/{_uid()}/{label}"
        if bounce:
            _run(["launchctl", "bootout", target])
        elif _agent_pid(label):
            continue                   # running — never bounce it
        if (bounce or _run(["launchctl", "print", target]).returncode != 0) \
                and not _bootstrap_agent(label, plist):
            problems.append(f"bootstrap_failed:{label}")
            continue
        if not bounce:
            _run(["launchctl", "kickstart", target])
        until = time.time() + 15       # never shadow the budget deadline
        pid = None
        while time.time() < until:
            pid = _agent_pid(label)
            if pid:
                break
            time.sleep(0.5)
        if not pid:
            problems.append(f"drainer_not_running:{label}")
    for label in WATCHER_LABELS:
        if time.time() >= deadline:
            problems.append(f"restart_deadline:{label}")
            continue
        r = _run(["launchctl", "print", f"gui/{_uid()}/{label}"])
        if r.returncode != 0 and not _bootstrap_agent(
                label, os.path.join(AGENTS_DIR, label + ".plist")):
            problems.append(f"watcher_not_loaded:{label}")
    _remove_marker()
    return problems


def restart_gateway(cfg: dict) -> None:
    """Fire-and-forget — a cron-spawned updater is a gateway descendant;
    a synchronous `gateway restart` would wait on ourselves (S12)."""
    with suppress(OSError):  # runs after durable bookkeeping — never undo it
        subprocess.Popen(
            ["launchctl", "kickstart", "-k",
             f"gui/{_uid()}/ai.hermes.gateway"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, close_fds=True,
            start_new_session=True)


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
        with suppress(OSError):
            if os.path.getmtime(path) >= cutoff:
                continue
            os.unlink(path)
            removed.append(path)
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
        except (ValueError, TypeError, RecursionError):
            continue
        if not isinstance(rec, dict) or rec.get("scheduled") is not True:
            continue
        cmd = rec.get("cmd")
        if cmd == "ops.update_apply":
            if _ver_key(rec.get("tag")) is None \
                    or not isinstance(rec.get("target_sha"), str) \
                    or not HEX_RE.fullmatch(rec["target_sha"]):
                continue
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
    with open(os.path.join(DATA, "update.log"), "ab") as log:
        subprocess.Popen([WRAPPER], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, env=env,
                         close_fds=True, start_new_session=True)


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
    r = _run([sys.executable,
              os.path.join(REPO, "mcs", "ops", "mcs_setup.py"), "services"],
             timeout=120)
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
    errors.extend(f"drainer_not_running:{label}"
                  for label in RESIDENT_LABELS if _agent_pid(label) is None)
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
        "manifest_snapshot": applying.get("manifest_snapshot"),
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


def _consent_hold_record(e, backup_path) -> dict:
    """The restore_consent hold record — the SAME report_id binds every
    later ops.restore_approve receipt, so all three hold sites build it
    identically."""
    return {"report_id": e.report["report_id"],
            "backup_path": backup_path,
            "backup_sha256": e.report["backup_sha256"],
            "backup_schema": e.report["backup_schema"],
            "intervening_messages": e.report["intervening_messages"],
            "external_effect_rows": e.report["external_effect_rows"],
            "at": time.time()}


def _consent_hold(state, e, backup_path) -> str:
    """Persist a consent hold (record + state save + operator-facing
    pending report). Returns the binding report_id."""
    rid = e.report["report_id"]
    state["restore_consent"] = _consent_hold_record(e, backup_path)
    save_state(state)
    _report("restore_consent_pending",
            rid + " — restore_report.json を確認し "
            "ops.restore_approve で承認")
    return rid


def _rollback_applying(entry, prev_sha, command_id) -> dict:
    """The rollback-shaped 'applying' journal record (mirrored by
    mcs_recover's rollback_shaped check): target is entry's prev_sha."""
    return {
        "tag": "rollback:" + (entry.get("tag") or "?"),
        "sha": entry["prev_sha"],
        "prev_sha": prev_sha,
        "rollback": True,
        "plugin_changed": entry.get("plugin_changed"),
        "schema_bump": entry.get("schema_bump"),
        "backup_path": entry.get("backup_path"),
        "manifest_snapshot": entry.get("manifest_snapshot"),
        "command_id": command_id,
        "at": time.time()}


def _hold_rollback_for_consent(state, e, tag, command_id,
                               reason) -> int:
    """Rollback reached the DB replace and is held on consent: keep
    drainers stopped, both markers up, and a rollback-shaped 'applying'
    record — the tree is already reset to prev_sha, so the next recover
    pass must see head == target and reach _restore_db again (an
    apply-shaped record would classify as head == prev and drop the
    consent hold without ever restoring)."""
    rid = e.report["report_id"]
    entry = state["applying"]
    state["applying"] = _rollback_applying(
        entry, entry.get("sha"), command_id or entry.get("command_id"))
    reason += " (rollback held: consent pending " + rid[:16] + "…)"
    if command_id:
        state.setdefault("executed", {})[command_id] = {
            "result": "failed", "detail": reason[:200],
            "at": time.time()}
    _record_attempt(state, tag, "failed", reason)
    _consent_hold(state, e, state["applying"].get("backup_path"))
    return 2


def _run_post_merge(sha: str) -> None:
    """Spawn the post-merge child under the NEW code while the parent
    keeps both locks held — start_new_session so a timeout can killpg()
    grandchildren too. Raises UpdateError unless the child either exits
    cleanly or proves it already wrote 'done' (H5)."""
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
        with suppress(OSError):
            os.killpg(child.pid, 9)
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
    consent_hold = False
    delegated = False

    def bail(reason: str) -> int:
        nonlocal rollback_failed, consent_hold
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
            except RestoreConsentPending as e:
                # the post-merge child may already have restarted the
                # drainers (and removed the marker) — hold exactly like
                # rollback(): writers stopped, marker kept until consent
                # record the hold FIRST: a failing quiesce must never
                # lose the consent record or let finally drop the marker
                consent_hold = True
                rc = _hold_rollback_for_consent(
                    state, e, tag, command_id, reason)
                try:
                    quiesce()
                except Exception as qe:
                    # keep the consent report intact — the hold (and its
                    # awaiting-consent freeze) already stands; log only
                    print("consent hold: quiesce failed:",
                          f"{type(qe).__name__}: {qe}"[:200])
                return rc
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
        if state.get("_corrupt"):
            return 2
        if state.get("applying") or state.get("stages"):
            os.close(upd_fd)
            upd_fd = None
            # the journal is ANOTHER run's: whatever recover raises must
            # propagate (as in rollback()), never reach bail() below,
            # which would treat that journal as this apply's own and
            # _rollback_tree it unlocked, then overwrite it
            delegated = True
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
        try:
            bpath = maintenance.preupdate_backup(LEDGER)
        except maintenance.MaintenanceError as e:
            return bail(str(e))
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
        journal(state, "post_merge")
        _run_post_merge(sha)

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
    except Exception as e:
        if delegated:
            raise
        if isinstance(e, UpdateError):
            return bail(str(e))
        return bail(f"unexpected:{type(e).__name__}")
    finally:
        if quiesced and not consent_hold:
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
            if r.returncode != 0:
                problems.append("cron_list_unverifiable")
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
    else:
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
            _run(["launchctl", "bootout", f"gui/{_uid()}/{label}"])
            with suppress(OSError):
                os.unlink(path)
    return problems


def _rollback_tree(entry: dict) -> None:
    """Restore tracked files to prev_sha — shared by rollback() and the
    post-merge failure path. Caller holds both locks and drainers are
    already quiesced. Do not additionally delete untracked paths just
    because their names occur in the update diff; ownership is unproven."""
    prev = entry["prev_sha"]
    _git_out(["reset", "--hard", prev])
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
    # an interrupted apply/rollback journal belongs to recovery — rolling
    # back the last APPLIED entry would overwrite it (same rule as apply)
    if state.get("applying") or state.get("stages"):
        return recover_interrupted()
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        print("another updater is active — retry later")
        return 2
    run_fd = None
    quiesced = False
    consent_hold = False
    try:
        state = load_state()            # fresh view under the lock
        if state.get("_corrupt"):
            return 2
        if state.get("applying") or state.get("stages"):
            os.close(upd_fd)
            upd_fd = None
            return recover_interrupted()
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
            state["applying"] = _rollback_applying(
                entry, _head_sha(), command_id)
            journal(state, "rollback")
            quiesced = True
            quiesce()
            rb_error = None
            try:
                _rollback_tree(entry)
            except RestoreConsentPending as e:
                # HOLD, don't unwind: drainers stay stopped, senders
                # stay denied (awaiting_consent marker + update marker),
                # the rollback receipt stays pending, and every later
                # check/watchdog pass re-evaluates the same report until
                # a bound ops.restore_approve receipt lands.
                state = load_state()
                rid = _consent_hold(state, e, entry.get("backup_path"))
                print("restore held: consent pending", rid)
                consent_hold = True
                return 2
            except Exception as e:
                # not just UpdateError — any failure here must still
                # reach restart_agents() below (drainers are quiesced)
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
            state.pop("restore_consent", None)
            state.setdefault("attempts", {})[
                entry.get("tag") or "?"] = {
                "result": "rolled_back", "at": time.time()}
            if command_id:
                state.setdefault("executed", {})[command_id] = {
                    "result": "rolled_back", "at": time.time()}
            save_state(state)
            # gateway restart AFTER the durable save (self-deadlock)
            if entry.get("plugin_changed"):
                restart_gateway(load_config())
            _enqueue_notice(f"[MCS] ロールバックしました: "
                            f"{entry.get('tag')} → {prev[:12]}")
            return 0
        except UpdateError as e:
            if quiesced:
                # quiesce itself failed part-way (one drainer already
                # stopped) — restart before reporting (H4)
                problems = restart_agents()
                _remove_marker()
                quiesced = False
                if problems:
                    e = UpdateError(f"{e} restart:" + ",".join(problems))
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
        # a consent hold keeps BOTH markers: they are what durably holds
        # writers/senders frozen until the bound approval arrives
        if quiesced and not consent_hold:
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


# ----------------------------------------------------- restore consent

# Records the swap would silently erase. Stored data = rows written
# after the backup's watermark; external effects = per-table deltas of
# every notification/send-tracking table (send evidence, queued work).
_STORED_TABLES = ("messages", "attachments")
_EFFECT_TABLES = ("notification_cards", "notification_renders",
                  "notification_delivery_attempts",
                  "notification_render_parts", "notification_restore_holds",
                  "notification_view_manifests", "notify_outbox")


def _table_count(con: sqlite3.Connection, table: str) -> int:
    """COUNT(*) or 0 when the table is absent (older schema) — a missing
    table can lose nothing."""
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return 0


def _restore_content_digest(con):
    """Bind consent to the contents of the stored-data and delivery tables."""
    digest = hashlib.sha256()
    for table in (*_STORED_TABLES, *_EFFECT_TABLES):
        columns = con.execute(f"PRAGMA table_info({table})").fetchall()
        digest.update(json.dumps([table, columns], separators=(",", ":")).encode())
        if not columns:
            continue
        for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            encoded = json.dumps(
                row, ensure_ascii=False, separators=(",", ":"),
                default=lambda value: {"blob": value.hex()}).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _restore_loss_report(backup_path: str) -> dict:
    """Loss report a human must approve before the live DB is replaced.
    Binds backup bytes/schema, row deltas, and the contents of the stored-data
    and delivery tables. Changes to an existing row invalidate stale consent."""
    try:
        live = sqlite3.connect("file:" + LEDGER + "?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise UpdateError(f"restore_report_live_db: {e}") from e
    try:
        back = sqlite3.connect(
            "file:" + backup_path + "?mode=ro", uri=True)
    except sqlite3.Error as e:
        live.close()
        raise UpdateError(f"restore_report_backup_db: {e}") from e
    try:
        live.execute("BEGIN")
        back.execute("BEGIN")
        live_digest = _restore_content_digest(live)
        try:
            watermark = back.execute(
                "SELECT MAX(posted_at_ts) FROM messages").fetchone()[0]
        except sqlite3.Error:
            watermark = None
        stored = {t: _table_count(live, t) - _table_count(back, t)
                  for t in _STORED_TABLES}
        effects = {t: _table_count(live, t) - _table_count(back, t)
                   for t in _EFFECT_TABLES}
    finally:
        live.close()
        back.close()
    metrics = {"v": 1,
               "live_content_sha256": live_digest,
               "backup_sha256": _file_sha256(backup_path),
               "backup_schema": _db_version(backup_path),
               "watermark_ts": watermark,
               "stored_since_backup": stored,
               "external_effects": effects,
               "intervening_messages": sum(max(0, n)
                                           for n in stored.values()),
               "external_effect_rows": sum(max(0, n)
                                           for n in effects.values())}
    metrics["report_id"] = hashlib.sha256(
        json.dumps(metrics, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    report = dict(metrics, computed_at=time.time())
    atomic_write(RESTORE_REPORT_PATH,
                 lambda f: json.dump(report, f, ensure_ascii=False,
                                     sort_keys=True),
                 tmp_prefix=".rreport.")
    return report


def _restore_consent(report: dict) -> str | None:
    """Scan command_receipts (read-only, same discipline as
    scan_pending_approvals) for an ops.restore_approve approval bound
    to THIS exact loss report — an earlier update/rollback approval
    does not substitute (the human must see the loss numbers first)."""
    try:
        con = sqlite3.connect("file:" + LEDGER + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        try:
            rows = con.execute(
                "SELECT command_id, receipt_json FROM command_receipts"
                " WHERE outcome='applied' ORDER BY processed_at DESC,"
                " rowid DESC").fetchall()
        except sqlite3.Error:
            return None
    finally:
        con.close()
    for cid, rj in rows:
        try:
            rec = json.loads(rj)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("cmd") != "ops.restore_approve" \
                or rec.get("scheduled") is not True:
            continue
        if rec.get("report_id") == report["report_id"] \
                and rec.get("backup_sha256") == report["backup_sha256"] \
                and rec.get("backup_schema") == report["backup_schema"]:
            return cid
    return None


def _reconcile_restored() -> None:
    """Post-swap journal-vs-DB reconcile while writers stay quiesced —
    on failure the restore_pending marker stays up (send grants keep
    denying) and the caller escalates for the next pass to retry."""
    import ledger
    import notify_reconcile
    restored = ledger.Ledger(LEDGER)
    try:
        report = notify_reconcile.reconcile_after_restore(restored, load_config())
        if report.get("journal_incomplete"):
            raise ValueError("restore_journal_incomplete")
    except Exception as e:
        raise UpdateError(
            f"restore_reconcile_failed: {type(e).__name__}") from e
    finally:
        restored.close()


def _replace_database(backup_path, expected_sha, before_replace):
    """Stage and verify the backup before checkpointing and replacing the live DB."""
    directory = os.path.dirname(LEDGER)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".restore.", suffix=".db")
    try:
        with os.fdopen(fd, "wb") as dst, open(backup_path, "rb") as src:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if _file_sha256(temporary) != expected_sha:
            raise OSError("backup_changed_during_restore")
        candidate = sqlite3.connect(
            Path(temporary).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if candidate.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise OSError("backup_integrity_failed")
        finally:
            candidate.close()
        # Preserve committed WAL contents even if the subsequent replace fails.
        # SQLite owns journal removal; never unlink a live WAL by hand.
        live = sqlite3.connect(Path(LEDGER).resolve().as_uri() + "?mode=rw",
                               uri=True, timeout=5)
        try:
            if live.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                raise OSError("live_checkpoint_busy")
            if live.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise OSError("live_journal_busy")
        finally:
            live.close()
        if any(os.path.lexists(LEDGER + suffix)
               for suffix in ("-wal", "-shm", "-journal")):
            raise OSError("live_sidecars_remaining")
        before_replace()
        os.replace(temporary, LEDGER)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except sqlite3.Error as exc:
        raise OSError("restore_database_unverifiable") from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _restore_db(backup_path: str) -> None:
    """Verified restore — validate the backup before touching the live
    DB. Skips when the live schema already matches the backup (a failed
    apply may never have migrated). Stages the private copy, verifies its
    approved hash, then checkpoints the live DB before replacement so a
    failed copy cannot erase committed WAL data. Callers quiesce every writer.
    A real replace requires a per-restore human consent receipt bound
    to the exact backup bytes/schema/loss report (R2 — an earlier
    update or rollback approval does not substitute): missing consent
    raises RestoreConsentPending and callers must HOLD, not unwind.
    Loses records written between apply and restore; Discord posts
    already sent cannot be retracted (design R2)."""
    import ledger
    import notify_cards
    marker = notify_cards.restore_pending(DATA)
    if marker and marker.get("unreadable"):
        raise UpdateError("restore_marker_unreadable")
    if not ledger.valid_mcs_db(backup_path):
        raise UpdateError("backup_invalid: " + backup_path)
    live_ver = _db_version(LEDGER)
    back_ver = _db_version(backup_path)
    if live_ver is not None and live_ver == back_ver:
        # Nothing to replace — but a crash between the post-swap marker
        # rewrite and the swap can leave an awaiting_consent marker
        # pinning every send; promote it and reconcile normally.
        if notify_cards.restore_awaiting_consent(DATA) is not None:
            try:
                notify_cards.mark_restored(DATA, backup_path=backup_path,
                                           by="mcs_update")
            except OSError as e:
                raise UpdateError(
                    f"restore_marker_failed: {e}") from e
            _reconcile_restored()
        return
    # Sender hold BEFORE measuring the loss — a grant slipping between
    # the count and the swap would lose its record silently.
    try:
        notify_cards.mark_restored(DATA, backup_path=backup_path,
                                   by="mcs_update",
                                   phase="awaiting_consent")
    except OSError as e:
        raise UpdateError(f"restore_marker_failed: {e}") from e
    report = _restore_loss_report(backup_path)
    try:
        # re-pin the hold with the report identity so operators and the
        # watchdog can correlate marker <-> restore_report.json
        notify_cards.mark_restored(DATA, backup_path=backup_path,
                                   by="mcs_update",
                                   phase="awaiting_consent",
                                   report_id=report["report_id"])
    except OSError as e:
        raise UpdateError(f"restore_marker_failed: {e}") from e
    if _restore_consent(report) is None:
        raise RestoreConsentPending(report)
    # Mark BEFORE the file swap: a crash after the replace but before
    # the marker write must not leave senders running against a rewound
    # DB. A stale marker is harmless — the next tick's reconcile finds
    # nothing to hold and clears it.
    try:
        _replace_database(backup_path, report["backup_sha256"],
                          lambda: notify_cards.mark_restored(
                              DATA, backup_path=backup_path, by="mcs_update",
                              report_id=report["report_id"]))
    except OSError as e:
        # callers catch UpdateError (apply bail / recover escalate /
        # rollback rb_error); a raw OSError would slip past them and
        # leave quiesced drainers down
        raise UpdateError(f"restore_failed: {e}") from e
    if not ledger.valid_mcs_db(LEDGER):
        raise UpdateError("restore_verify_failed")
    # The DB just rewound — records of sends between the backup and now
    # are gone while the remote messages/journal survive. Reconcile
    # journal vs restored DB here, while drainers are still quiesced;
    # if it fails the restore_pending marker stays up (send grants keep
    # denying) and the escalate path reports it for the next tick to
    # retry.
    _reconcile_restored()


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
        if state.get("_corrupt"):
            _report("corrupt_state", "update_state.json unreadable under lock")
            return 2
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
        rollback_shaped = bool(applying and applying.get("rollback")
                               and applying.get("backup_path"))
        if not state.get("restore_consent") and rollback_shaped:
            marker = _awaiting_consent_marker()
            if marker is not None:
                # A hold without its journal record: entered before
                # holds were journaled (older watchdog) or a crash
                # between the awaiting_consent marker and
                # _consent_hold. Record it so this and every later pass
                # (either tool) keeps the freeze; the head==target pass
                # rewrites it from a fresh loss report.
                state["restore_consent"] = {
                    "report_id": marker.get("report_id"),
                    "backup_path": applying["backup_path"],
                    "from_marker": True, "at": time.time()}
                save_state(state)
        prior_drainers = _last_report().get("drainers_key")
        head = None                    # measured below (restart identity)
        tree_reset = False
        restarted = False

        def held() -> bool:
            """Restore-consent hold: the journal record, or (fail
            closed — a hold whose record is missing) the awaiting_consent
            / unreadable restore_pending marker."""
            return bool(state.get("restore_consent")) \
                or _awaiting_consent_marker() is not None

        def drainers(dkey: str) -> list:
            """Bounce drainers once per journal + HEAD (or after this
            pass reset the tree); a later pass of the same unresolved
            condition only ensures they run — H4 without bouncing
            healthy drainers on every check / consent respawn."""
            nonlocal restarted
            bounce = not restarted and (tree_reset
                                        or prior_drainers != dkey)
            restarted = True
            return restart_agents() if bounce \
                else restart_agents(bounce=False)

        def escalate(detail: str) -> int:
            """Fail closed on the journal but keep the rest of the
            system alive: drainers back up, marker gone, human told.
            Inside a restore-consent hold every escalation keeps the
            freeze instead (canonical; mcs_recover.py mirrors it): the
            tree is on prev_sha while the DB may still be the newer
            schema, so drainers stay down, both markers, 'applying' and
            the receipt stay, and the human is told via the report only
            — an outbox notice would change notify_outbox, i.e. the
            loss report's content digest, voiding the consent it
            awaits. An invalid hold record (wrong journal shape) is
            held the same way: only a human can repair it. The journal
            stays, so the next check / consent respawn retries."""
            if held():
                _report("restore_consent_blocked",
                        detail[:240] + " — hold kept, retried next pass")
                return 1
            cid = (applying or {}).get("command_id")
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": "escalated", "detail": detail[:200],
                    "at": time.time()}
                save_state(state)
            key = _alert_key(state, detail)
            dkey = _drainers_key(state, head)
            notify, notified_at = _alert_due("escalate", key, time.time())
            _report("escalate", detail, alert_key=key,
                    notified_at=notified_at, drainers_key=dkey)
            problems = drainers(dkey)
            _remove_marker()
            # every pass (daily check, consent respawn) re-escalates the
            # same stuck journal — notify once per condition (dedup)
            if notify and not _enqueue_notice(
                    f"[MCS] 更新の中断復旧ができません（要手動対応）: {detail}"
                    + ((" restart:" + ",".join(problems))
                       if problems else "")):
                _report("escalate", detail, alert_key=key,
                        drainers_key=dkey)          # retry next
            return 1

        if held() and not rollback_shaped:
            # A held schema-bump DB replace must reach _restore_db again
            # via the head==target rollback branch — any other journal
            # shape is corruption; fail closed rather than 'finish' into
            # a wedge that keeps every send denied forever.
            return escalate("restore_consent without a rollback "
                            "journal — refusing to classify")
        if os.path.exists(os.path.join(REPO, ".git", "MERGE_HEAD")):
            if held():
                # the rollback tree reset already completed before the
                # consent hold began — a MERGE_HEAD here is drift
                return escalate("restore_consent with MERGE_HEAD — "
                                "refusing to classify")
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
            _gateway_restart_if_needed(state)
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
                tree_reset = True
                _git_out(["reset", "--hard", target])
                if not _tree_clean():
                    return escalate("target tree could not be cleaned")
            if applying.get("rollback") and applying.get("backup_path"):
                try:
                    _restore_db(applying["backup_path"])
                except RestoreConsentPending as e:
                    # held, not escalated: drainers stay stopped, both
                    # markers stay up, the receipt and 'applying' stay —
                    # each later recover pass re-checks the consent
                    # receipt against a fresh loss report.
                    _consent_hold(state, e, applying.get("backup_path"))
                    return 0
                except UpdateError as e:
                    return escalate("rollback db restore: " + str(e))
                # the DB is at the backup schema and the marker left
                # awaiting_consent: the hold is over — later failures
                # escalate normally (drainers up on a consistent DB)
                if state.pop("restore_consent", None) is not None:
                    save_state(state)
            try:
                if applying.get("rollback"):
                    problems = _reconcile_membership(applying.get("manifest_snapshot"))
                    if problems:
                        return escalate("membership: " + ",".join(problems))
                _services_reconcile()
                problems = drainers(_drainers_key(state, head))
                errors = _postcheck(state, target)
                if problems or errors:
                    return escalate("resume postcheck: "
                                    + ",".join(problems + errors))
                if applying.get("rollback"):
                    applied = state.get("applied") or []
                    if applied and applied[-1].get("sha") == applying.get("prev_sha"):
                        reverted = applied[-1]
                        state["applied"] = applied[:-1]
                        state.setdefault("attempts", {})[reverted.get("tag") or "?"] = {
                            "result": "rolled_back", "at": time.time()}
                    result = "rolled_back"
                else:
                    state.setdefault("applied", []).append(applying)
                    result = "applied"
                state["applying"] = None
                state["stages"] = []
                state.pop("restore_consent", None)
                cid = applying.get("command_id")
                if cid:
                    state.setdefault("executed", {})[cid] = {
                        "result": result, "at": time.time()}
                save_state(state)
                _remove_marker()
                _report("resumed", "post-merge completed after crash")
                _enqueue_notice(
                    "[MCS] 更新の中断を検出し、post-merge を完了しました")
                if applying.get("plugin_changed"):
                    restart_gateway(load_config())
                return 0
            except UpdateError as e:
                return escalate("resume failed: " + str(e))
        if prev and head == prev:
            if held():
                # a held restore resolves ONLY through the head==target
                # rollback branch; landing here means the journal
                # drifted — escalate, never 'finish' into a wedge
                return escalate("restore_consent hold lost its "
                                "rollback target — refusing to classify")
            if not clean:
                # crash mid-merge checkout without MERGE_HEAD — the
                # mixed-tree case stage-gating could never reach (F1)
                _git_out(["reset", "--hard", prev])
                if _head_sha() != prev or not _tree_clean():
                    return escalate("prev tree could not be cleaned")
                _finish_recovery(state, "mixed_tree_reset", removed)
                return 0
            _finish_recovery(state, "interrupted_pre_merge", removed)
            return 0
        return escalate("unclassifiable repo state — "
                        f"HEAD={head[:12]} prev={str(prev)[:12]} "
                        f"target={str(target)[:12]} clean={clean}")
    except UpdateError as e:
        # git itself failed (timeout / spawn / nonzero: HEAD, status,
        # reset, merge --abort) — the tree is unmeasurable: escalate,
        # which inside a consent hold keeps the freeze (see escalate).
        return escalate(f"git unverifiable: {e}")
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
    state.pop("restore_consent", None)
    save_state(state)
    _remove_marker()
    problems = restart_agents()
    _report(result, "removed locks: " + ",".join(removed)
            + (" restart:" + ",".join(problems) if problems else ""))
    _enqueue_notice(f"[MCS] 更新が中断され復旧しました: {result}")


def _report(result: str, detail: str, **extra) -> None:
    """Atomic report write — a torn report must never mislead a human
    checking `status` after a crash."""
    with suppress(OSError):
        atomic_write(REPORT_PATH,
                     lambda f: json.dump({"result": result,
                                          "detail": detail,
                                          "at": time.time(), **extra},
                                         f, ensure_ascii=False),
                     tmp_prefix=".ureport.")


def _alert_key(state: dict, detail: str) -> str:
    """Dedup key of an unresolved recovery condition: the stuck journal
    (applying + stages, untouched by escalate) and the reason class
    (detail up to its first ':', '—' or '(' — volatile tails such as
    stderr stay out). Identical in mcs_recover.py so both tools dedup
    against the same recovery_report.json."""
    reason = re.split(r"[:—(]", detail, maxsplit=1)[0].strip()
    return hashlib.sha256(json.dumps(
        [state.get("applying"), state.get("stages"), reason],
        sort_keys=True, default=str).encode()).hexdigest()[:32]


def _alert_due(result: str, key: str, now: float) -> tuple[bool, float]:
    """(notify?, notified_at to record). The previous report suppresses
    only the same (result, key) notified less than ESCALATE_REALERT_S
    ago; any other report in between (a state change) or an unreadable
    one re-alerts — the first alert is never suppressed."""
    last = _last_report()
    at = last.get("notified_at")
    if (last.get("result"), last.get("alert_key")) == (result, key) \
            and type(at) in (int, float) \
            and 0 <= now - at < ESCALATE_REALERT_S:
        return False, at
    return True, now


def _last_report() -> dict:
    """recovery_report.json as a dict — {} when missing or unreadable."""
    try:
        with open(REPORT_PATH, encoding="utf-8") as f:
            last = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    return last if isinstance(last, dict) else {}


def _drainers_key(state: dict, head: str | None) -> str:
    """Identity of what an escalation (re)started drainers for: the
    stuck journal + the HEAD measured in that pass. A later pass with
    the same key only ensures drainers run — the code they run has not
    changed, so bouncing them again every pass fixes nothing. Identical
    in mcs_recover.py (both tools share recovery_report.json)."""
    return hashlib.sha256(json.dumps(
        [state.get("applying"), state.get("stages"), head or None],
        sort_keys=True, default=str).encode()).hexdigest()[:32]
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
        with suppress(UpdateError):
            # fetch objects BEFORE notes/impact — otherwise the summary
            # silently runs against missing objects (dead code, F15)
            _git(["fetch", "--tags", "origin"], timeout=T_FETCH)
        notes = fetch_notes(tag)
        try:
            impact = impact_summary(cur_sha, tag) if cur_sha else []
        except UpdateError:
            impact = []
    # --- serialized state update + decision --------------------------
    picked = auto = None
    held = False
    upd_fd = acquire_update_lock()
    if upd_fd is None:
        return 0                        # another updater is alive
    try:
        state = load_state()            # fresh view under the lock
        if state.get("_corrupt"):
            return 2
        if state.get("applying") or state.get("stages"):
            # an interrupted/held update owns the journal — check must
            # defer to recovery AFTER releasing update.lock (a
            # consent-held restore re-enters here via spawn_detached
            # once its receipt lands)
            held = True
        elif tag:
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
        if not held:
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
        if upd_fd is not None:
            os.close(upd_fd)
    if held:
        return recover_interrupted()
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
