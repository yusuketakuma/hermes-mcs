"""Bound lifecycle subprocesses and safely publish local credentials."""
from __future__ import annotations

from contextlib import suppress
import json
import math
import time
import os
import stat
import re
import subprocess
from pathlib import Path

from safe_io import atomic_write, fdopen_owned

KEYCHAIN_SERVICE = "mcs-adapter"

def run_bounded(argv: list, *, timeout: int = 60, input_text: str | None = None,
         cwd: str | None = None, env: dict | None = None, deadline: float | None = None):
    """subprocess.run bounded by `timeout` — a wedged launchd, a keychain
    prompt this context cannot show, or a stuck gateway must fail the
    check, not hang the session. Timeout yields a returncode=124 result
    (mirroring timeout(1)); a binary that cannot start yields 127. Both
    degrade through the caller's normal failure handling instead of
    raising."""
    if deadline is not None:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError('lifecycle_deadline_invalid')
        left = deadline - time.monotonic()
        if left <= 0:
            return subprocess.CompletedProcess(argv, 124, "", "deadline elapsed")
        timeout = min(timeout, left)
    try:
        if deadline is None:
            result = subprocess.run(argv, input=input_text, capture_output=True,
                                  text=True, timeout=timeout, **({"cwd": cwd} if cwd else {}),
                                  **({"env": env} if env is not None else {}))
        else:
            import owned_process
            result = owned_process.run_owned(argv, timeout, dict(os.environ) if env is None else env,
                deadline=deadline, input_text=input_text, cwd=cwd)
        if deadline is not None and time.monotonic() >= deadline:
            return subprocess.CompletedProcess(argv, 124, "", "deadline elapsed")
        return result
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv, 124, "", f"timed out after {timeout}s")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


# ---- .env merge ------------------------------------------------------

def write_env(path: str, updates: dict[str, str]):
    """Merge updates while preserving unrelated lines and their order."""
    encoded = {}
    for key, value in updates.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) \
                or not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
            raise ValueError("invalid_env_update")
        encoded[key] = (json.dumps(value, ensure_ascii=False)
                        if value != value.strip() or any(c in value for c in "\"'\\")
                        else value)
    updates = encoded
    # Share the short publication lock with interactive API-key edits.  Do not
    # acquire it during input or Keychain access.
    from api_key_setup import EnvLockError, _env_edit_lock
    env_path = Path(path)
    os.makedirs(env_path.parent, exist_ok=True)
    try:
        with _env_edit_lock(env_path):
            lines = []
            with suppress(FileNotFoundError), open(path, encoding="utf-8", newline="") as stream:
                lines = stream.read().splitlines(keepends=True)
            kept = []
            replaced = set()
            for line in lines:
                key = line.split("=", 1)[0] if "=" in line else None
                if key not in updates:
                    kept.append(line)
                elif key not in replaced:
                    suffix = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else '\r' if line.endswith('\r') else ''
                    kept.append(f"{key}={updates[key]}{suffix}")
                    replaced.add(key)
            for key, val in updates.items():
                if key not in replaced:
                    if kept and not kept[-1].endswith(('\n', '\r')):
                        kept[-1] += '\n'
                    kept.append(f"{key}={val}\n")
            atomic_write(path, lambda stream: stream.write("".join(kept)),
                         0o600, tmp_prefix=".svc.")
    except EnvLockError as exc:
        raise OSError("env_edit_lock_unavailable") from exc


def store_keychain(account: str, pw: str, *, service: str = KEYCHAIN_SERVICE) -> bool:
    """Store the MCS password in the login keychain without exposing it
    in child-process argv (`ps` shows argv to every local user).

    `security -i` reads commands from stdin, so the credential travels
    over the pipe instead of `-w` on the command line. The value is
    double-quoted with backslash escapes; a read-back verify turns any
    quoting deviation into a loud failure — a silently wrong password is
    worse than no write. The verify uses the adapter's exact argv —
    service-only, no ``-a`` filter — so it proves what auto_login will
    actually read (a stale second entry under the same service would
    shadow the fresh write and must surface as failure here, not as a
    mysterious wrong-password rejection at login)."""
    def _q(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    if (not service.strip()
            or any(c in value for value in (account, pw, service) for c in "\r\n\x00")):
        return False
    # Update the item the adapter's service-only read resolves to, under
    # its own account: a first interactive init stores under "mcs" before
    # mcs_login_id is known, and a write under another account would add
    # a shadowed second item instead of replacing the password.
    existing = run_bounded(["security", "find-generic-password", "-s", service])
    if existing.returncode == 0:
        m = re.search(r'^\s*"acct"<blob>="(.*)"\s*$', existing.stdout or "",
                      re.MULTILINE)
        if m:
            account = m.group(1)
    previous = run_bounded(
        ["security", "find-generic-password", "-s", service,
         "-a", account, "-w"])
    if previous.returncode not in (0, 44):  # 44: no existing item
        return False
    old_password = previous.stdout.rstrip("\n") if previous.returncode == 0 else None
    if old_password is not None and any(c in old_password for c in "\r\n\x00"):
        return False
    cmd = (f"add-generic-password -s {_q(service)} "
           f"-a {_q(account)} -U -w {_q(pw)}\n")
    written = run_bounded(["security", "-i"], input_text=cmd)
    chk = run_bounded(
        ["security", "find-generic-password", "-s", service,
         "-w"])
    if chk.returncode == 0 and chk.stdout.rstrip("\n") == pw:
        return True
    # Preserve a credential that existed before this attempt. Only a
    # successfully created item with verified prior absence may be deleted.
    if old_password is not None:
        rollback = (f"add-generic-password -s {_q(service)} "
                    f"-a {_q(account)} -U -w {_q(old_password)}\n")
        run_bounded(["security", "-i"], input_text=rollback)
    elif written.returncode == 0:
        run_bounded(["security", "delete-generic-password", "-s",
              service, "-a", account])
    return False


def read_owner_config(path, limit=65536) -> dict:
    """Read config.json for install.sh: a regular file the user owns, bounded, a JSON object."""
    if not os.path.lexists(path):
        return {}
    try:
        with fdopen_owned(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > limit:
                raise ValueError("size/ownership/type invalid")
            raw = stream.read(limit + 1)
    except OSError:
        raise ValueError("unreadable") from None
    if len(raw) > limit:
        raise ValueError("too large")
    try:
        cfg = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("not valid JSON") from None
    if not isinstance(cfg, dict):
        raise ValueError("must be an object")
    return cfg
