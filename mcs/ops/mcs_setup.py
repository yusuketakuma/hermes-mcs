"""MCS environment setup + required-condition validation.

`init` interactively (or via flags) provisions what a fresh machine
needs: ~/.mcs/config.json, ~/.mcs/.env secrets, and the macOS Keychain
entry the adapter reads at re-login (service 'mcs-adapter' — on the
Mac mini this is the same login keychain Chrome uses). Existing values
are merged, never silently overwritten. An interactive run walks EVERY
config key as a guided wizard (sectioned, defaults shown, Enter keeps
the current value); `--yes` runs flags only and `--set KEY=JSON` covers
any key non-interactively. With notify.interactive="discord", init also
mirrors the plugin settings + bot token into hermes through the public
`hermes [-p profile] config set` CLI — never hermes-agent internals.

`check` is the typesafe gate: every required key must exist with the
right type, optional keys are range-checked, the semantic block is
validated by the production `semantic_config` validator, and the
environment probes (Keychain entry, Chrome binary, local-LLM endpoint,
secret resolution, gateway supervision) report as warnings vs errors.
Exit 1 on any error so it can gate automation.

  python3 mcs/ops/mcs_setup.py init [--login-id ID ...]
  python3 mcs/ops/mcs_setup.py check

Secrets are never accepted as argv flags (they would persist in shell
history and `ps`): export MCS_SETUP_PASSWORD / TYPESAFE_API_KEY /
DISCORD_BOT_TOKEN in the environment, or answer the interactive
prompts.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
from html import escape as xml_escape
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from contextlib import closing, suppress
from pathlib import Path

# mcs/ requires >=3.10 (runtime PEP-604 unions) — fail loudly before the
# first project import instead of a cryptic TypeError inside mcs_util.
if sys.version_info < (3, 10):
    raise SystemExit(
        "mcs_setup requires Python >= 3.10 — use the install-time "
        "interpreter ~/.hermes/hermes-agent/venv/bin/python "
        "(/usr/bin/python3 is too old)")

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

from mcs_util import (CONF_PATH, HOME, atomic_write, env_value,
                      load_config)

ENV_PATH = os.path.join(HOME, ".env")
KEYCHAIN_SERVICE = "mcs-adapter"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LLM_MODELS_URL = "http://127.0.0.1:8080/v1/models"

# ---- typesafe config rules ------------------------------------------
# name -> (required, check) — check(v) -> error string | None
def _nonempty_str(v):
    return None if isinstance(v, str) and v.strip() else "must be a non-empty string"


def _bool(v):
    return None if type(v) is bool else "must be a boolean"


def _int_range(lo, hi):
    def f(v):
        if type(v) is not int or not (lo <= v <= hi):
            return f"must be an integer in [{lo},{hi}]"
        return None
    return f


def _num(v):
    return (None if type(v) in (int, float) and 0 < v <= 1e9
            else "must be a finite number in (0,1e9]")


def _bot_profile(v):
    ok = isinstance(v, str) and re.fullmatch(r"[a-z0-9_-]+", v)
    return None if ok else "must match [a-z0-9_-]+"


def _dict(v):
    return None if isinstance(v, dict) else "must be an object"


CONFIG_RULES = {
    "mcs_login_id":          (True,  _nonempty_str),
    "notify_target":         (True,  _nonempty_str),
    "notify_bot_profile":    (False, _bot_profile),
    "notify_system_target":  (False, _nonempty_str),
    "hermes_bin":            (False, _nonempty_str),
    "discover_archived":     (False, _bool),
    "deep_history":          (False, _bool),
    "trickle_pages":         (False, _int_range(1, 40)),
    "notify_max_age_h":      (False, _num),
    "job_budget_seconds":    (False, _num),
    "self_posts":            (False, _bool),
    "notify":                (False, _dict),
    "signals":               (False, _dict),
    "semantic":              (False, _dict),
    "update":                (False, _dict),
    "local_llm":             (False, _dict),
    "health":                (False, _dict),
}


def _validate_llm(ll: dict) -> list[str]:
    """local_llm subkeys — url must stay an unauthenticated loopback
    http endpoint (mirrors bounded_http._loopback_endpoint_allowed):
    message bodies are PHI and must not leave the machine."""
    errors = []
    for key in ("url", "model"):
        if key in ll and (not isinstance(ll[key], str)
                          or not ll[key].strip()):
            errors.append(f"local_llm.{key}: must be a non-empty string")
    url = ll.get("url")
    if isinstance(url, str) and url.strip():
        try:
            import bounded_http
            allowed = bounded_http._loopback_endpoint_allowed(url)
        except Exception:
            allowed = False
        if not allowed:
            errors.append(
                "local_llm.url: must be an http:// endpoint on loopback "
                "(127.0.0.1/localhost/::1), no credentials/fragment — "
                "message bodies must not leave the machine")
    return errors


def _validate_notify(ntf: dict) -> list[str]:
    """Subkey checks for the notify block — mirrors notify_cards'
    contracts: interactive is a closed enum, discord delivery scope is
    all-or-nothing, and scope is required when cards are on."""
    errors = []
    if "interactive" in ntf \
            and ntf["interactive"] not in ("discord", "slack", "off"):
        errors.append('notify.interactive: must be "discord", '
                      '"slack" or "off"')
    if "card_thread" in ntf and type(ntf["card_thread"]) is not bool:
        errors.append("notify.card_thread: must be a boolean")
    for key in ("route_epoch", "card_thread_archive_min"):
        if key in ntf:
            err = _int_range(1, 10 ** 9)(ntf[key])
            if err:
                errors.append(f"notify.{key}: {err}")
    if "operator" in ntf:
        err = _nonempty_str(ntf["operator"])
        if err:
            errors.append(f"notify.operator: {err}")
    for transport, tenant in (("discord", "guild_id"),
                              ("slack", "team_id")):
        d = ntf.get(transport)
        if d is None:
            if ntf.get("interactive") == transport:
                errors.append(f'notify.{transport}: required when '
                              f'interactive is "{transport}"')
        elif not isinstance(d, dict):
            errors.append(f"notify.{transport}: must be an object")
        else:
            errors.extend(
                f"notify.{transport}.{k}: must be a non-empty string"
                for k in ("profile", "application_id", tenant,
                          "channel_id")
                if not isinstance(d.get(k), str) or not d[k].strip())
            # delivery_scope() rejects a slack block carrying
            # guild_id outright — flag it at config time too
            if transport == "slack" and "guild_id" in d:
                errors.append("notify.slack.guild_id: not allowed "
                              "(slack scope uses team_id)")
    return errors


def _validate_update(upd: dict) -> list[str]:
    """Subkey checks for the update block — the self-updater's policy.
    mode is a killswitch/opt-in gate; auto_delay_h==0 means 'apply as
    soon as detected' (explicit opt-in, default is delayed)."""
    errors = []
    if "mode" in upd and upd["mode"] not in ("off", "notify", "auto"):
        errors.append('update.mode: must be "off", "notify" or "auto"')
    if "auto_delay_h" in upd:
        v = upd["auto_delay_h"]
        if type(v) not in (int, float) or not 0 <= v <= 1e9:
            errors.append("update.auto_delay_h: must be a finite number in [0,1e9]")
    if "include_prerelease" in upd \
            and type(upd["include_prerelease"]) is not bool:
        errors.append("update.include_prerelease: must be a boolean")
    return errors


def _validate_health(h: dict) -> list[str]:
    """Subkey checks for the health block consumed by health_watch:
    tick_interval_s is the producer's check cadence, max_missed_runs
    the missed-tick grace window before staleness is declared (the
    schedule-based deadline requires an int in [0,100]), night_thinning
    the optional day/night schedule switch."""
    errors = []
    if "tick_interval_s" in h:
        v = h["tick_interval_s"]
        if type(v) not in (int, float) or not 0 < v <= 86400:
            errors.append("health.tick_interval_s: must be a finite "
                          "number in (0,86400]")
    if "max_missed_runs" in h:
        err = _int_range(0, 100)(h["max_missed_runs"])
        if err:
            errors.append(f"health.max_missed_runs: {err}")
    if "night_thinning" in h and type(h["night_thinning"]) is not bool:
        errors.append("health.night_thinning: must be a boolean")
    return errors


def validate_config(cfg: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for config.json — typesafe required
    conditions. Unknown keys warn (forward-compat) but never fail."""
    errors, warnings = [], []
    if not isinstance(cfg, dict):
        return ["config.json is not a JSON object"], []
    warnings.extend(f"unknown config key: {key}"
                    for key in sorted(set(cfg) - set(CONFIG_RULES)))
    for key, (required, check) in CONFIG_RULES.items():
        if key not in cfg:
            if required:
                errors.append(f"missing required key: {key}")
            continue
        err = check(cfg[key])
        if err:
            errors.append(f"{key}: {err}")
    sig = cfg.get("signals")
    if isinstance(sig, dict) and "notify" in sig and type(sig["notify"]) is not bool:
        errors.append("signals.notify: must be a boolean")
    if isinstance(sig, dict):
        for key in ("self_organizations", "self_professions",
                    "request_targets", "med_exclude_names"):
            if key in sig:
                v = sig[key]
                if not (isinstance(v, list)
                        and all(isinstance(x, str) and x.strip()
                                for x in v)):
                    errors.append(f"signals.{key}: must be a list of "
                                  "non-empty strings")
        if "digest" in sig and type(sig["digest"]) is not bool:
            errors.append("signals.digest: must be a boolean")
        if "digest_interval_h" in sig:
            err = _num(sig["digest_interval_h"])
            if err:
                errors.append(f"signals.digest_interval_h: {err}")
        if "tiers" in sig and not isinstance(sig["tiers"], dict):
            errors.append("signals.tiers: must be an object")
    if isinstance(cfg.get("notify"), dict):
        errors.extend(_validate_notify(cfg["notify"]))
        # a scope block for the transport that is NOT active is stale —
        # keep it as a warning so a discord->slack switch leaves a
        # visible note instead of a silently ignored config
        act = cfg["notify"].get("interactive")
        for other in ("discord", "slack"):
            if other != act and isinstance(cfg["notify"].get(other),
                                           dict):
                warnings.append(
                    f"notify.{other}: scope configured but interactive "
                    f"is {act!r} — ignored until you switch back")
    if isinstance(cfg.get("local_llm"), dict):
        errors.extend(_validate_llm(cfg["local_llm"]))
    if isinstance(cfg.get("semantic"), dict):
        try:
            from semantic_policy import semantic_config
            errors.extend(semantic_config(cfg)[1])
        except Exception as e:  # validator itself must not crash check
            errors.append(f"semantic: validator failed ({type(e).__name__})")
    if isinstance(cfg.get("update"), dict):
        errors.extend(_validate_update(cfg["update"]))
    if isinstance(cfg.get("health"), dict):
        errors.extend(_validate_health(cfg["health"]))
    return errors, warnings


def _hermes_exe(cfg: dict) -> str:
    """config hermes_bin -> PATH -> the standard user-local install."""
    exe = cfg.get("hermes_bin")
    if isinstance(exe, str) and exe.strip():
        return exe.strip()
    return shutil.which("hermes") \
        or os.path.expanduser("~/.local/bin/hermes")


def _hermes_ok(exe: str) -> bool:
    return os.path.isfile(exe) and os.access(exe, os.X_OK)


def _run(argv: list, *, timeout: int = 60, input_text: str | None = None):
    """subprocess.run bounded by `timeout` — a wedged launchd, a keychain
    prompt this context cannot show, or a stuck gateway must fail the
    check, not hang the session. Timeout yields a returncode=124 result
    (mirroring timeout(1)); a binary that cannot start yields 127. Both
    degrade through the caller's normal failure handling instead of
    raising."""
    try:
        return subprocess.run(argv, input=input_text, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv, 124, "", f"timed out after {timeout}s")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def _hermes_cli(exe: str, profile: str, *argv: str, input_text: str | None = None):
    """Public hermes CLI against a named profile ("" = launch/default).
    Returns the CompletedProcess, or None when the call can't run."""
    cmd = [exe] + (["-p", profile] if profile else []) + list(argv)
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=30, input=input_text)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _hermes_config_get(exe: str, profile: str, key: str):
    r = _hermes_cli(exe, profile, "config", "get", key)
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def _plugin_newer_than_gateway(status_out: str) -> bool:
    """True when hermes_plugin/ holds a file newer than the running
    gateway process — a stale-worker signal. Unparseable states fail
    open to False (no warning) rather than a false alarm."""
    m = re.search(r"PID\s+(\d+)", status_out or "")
    if not m:
        return False
    r = _run(["ps", "-o", "lstart=", "-p", m.group(1)])
    if r.returncode != 0:
        return False
    stamp = re.sub(r"\s+", " ", (r.stdout or "").strip())
    try:
        started = time.mktime(
            time.strptime(stamp, "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return False
    plugin_dir = os.path.join(REPO_ROOT, "hermes_plugin")
    newest = 0.0
    # __pycache__/*.pyc are regenerated BY the gateway at plugin load —
    # counting them makes the warning permanent. Only source files
    # represent code the running process hasn't loaded yet.
    for base, dirs, files in os.walk(plugin_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            with suppress(OSError):
                newest = max(newest, os.path.getmtime(
                    os.path.join(base, name)))
    return newest > started


def _hermes_config_set(exe: str, profile: str, key: str, val) -> bool:
    # *_TOKEN keys are secrets: they travel over --stdin so `ps` never
    # exposes them in child argv (DISCORD_BOT_TOKEN, SLACK_BOT_TOKEN,
    # SLACK_APP_TOKEN, ...).
    if key.endswith("_TOKEN"):
        r = _hermes_cli(exe, profile, "config", "set", key, "--stdin",
                        input_text=str(val))
    else:
        r = _hermes_cli(exe, profile, "config", "set", key, str(val))
    return bool(r and r.returncode == 0)


def check_environment(cfg: dict) -> tuple[list[str], list[str]]:
    """Machine probes — things config.json can't express. Keychain and
    token resolution are errors only when the feature needs them."""
    errors, warnings = [], []
    if sys.platform != "darwin":
        warnings.append("not macOS — Keychain/launchd steps do not apply")
    else:
        # The .env MCS_PASSWORD fallback covers a rebooted machine whose
        # login keychain is still locked — when present, keychain
        # unreadability is a warning, not a blocker.
        env_pw = bool(env_value("MCS_PASSWORD", check_env=False))
        r = _run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE])
        if r.returncode != 0:
            (warnings if env_pw else errors).append(
                f"Keychain entry '{KEYCHAIN_SERVICE}' not found"
                + (" — .env fallback present, re-login still works"
                   if env_pw else
                   " — re-login cannot inject the MCS password"))
        else:
            # entry existence is not enough — the SECRET must be readable
            # non-interactively. A locked keychain (or an ACL prompt this
            # context cannot show) makes auto_login fail as
            # 'keychain_locked'; probing -w here surfaces that at setup
            # time (and may pop the one-time 'Always Allow' dialog in an
            # interactive session).
            w = _run(
                ["security", "find-generic-password", "-s",
                 KEYCHAIN_SERVICE, "-w"])
            if w.returncode != 0:
                err = (w.stderr or "").lower()
                if (w.returncode == 36 or "interaction is not allowed"
                        in err or "keychain is locked" in err):
                    (warnings if env_pw else errors).append(
                        f"Keychain entry '{KEYCHAIN_SERVICE}' is present but "
                        "unreadable — the login keychain is locked. "
                        + (".env fallback present — re-login still works "
                           "without unlocking."
                           if env_pw else
                           "Unlock it (`security unlock-keychain` or a GUI "
                           "login), then re-run check. To stop recurrence, "
                           "disable auto-lock: `security "
                           "set-keychain-settings "
                           "~/Library/Keychains/login.keychain-db` — or run "
                           "init with MCS_SETUP_PASSWORD to write the .env "
                           "fallback"))
                else:
                    errors.append(
                        f"Keychain entry '{KEYCHAIN_SERVICE}' exists but its "
                        f"password cannot be read (rc={w.returncode}) — "
                        "re-register via `mcs_setup init`")
    if not os.path.exists(CHROME_BIN):
        errors.append(f"Chrome binary missing: {CHROME_BIN}")
    # probe the CONFIGURED endpoint — a self-hosted server on another
    # port (local_llm.url) is a valid deployment, not a failure
    try:
        import local_llm
        llm_ep, _ = local_llm.resolve(cfg)
        models_url, slots_url = local_llm.probe_urls(llm_ep)
    except Exception:
        models_url, slots_url = LLM_MODELS_URL, None
    try:
        urllib.request.urlopen(models_url, timeout=3).close()
    except Exception:
        warnings.append(f"local LLM endpoint not reachable "
                        f"({models_url}) — extract_llm/semantic jobs "
                        "will stall until the server is up")
    else:
        # T19: a server advertising FEWER slots than the selected count
        # is a mismatch — a call pinned past the advertised width goes
        # unpinned and can land on the real-time slot. Flag it; never
        # run unpinned.
        try:
            raw = urllib.request.urlopen(slots_url, timeout=3).read()
            advertised = len(json.loads(raw.decode("utf-8")))
            if advertised < local_llm.SLOT_COUNT:
                errors.append(
                    f"llama-server advertises {advertised} slots but the "
                    f"selected count is {local_llm.SLOT_COUNT} — fix -np "
                    "or lower SLOT_COUNT; background work would pin "
                    "out-of-range (unpinned) slots")
        except Exception:
            warnings.append("local LLM /slots unreadable — slot-count "
                            "mismatch cannot be verified")
    profile = cfg.get("notify_bot_profile")
    if isinstance(profile, str) and profile \
            and not re.fullmatch(r"[a-z0-9_-]+", profile):
        errors.append("notify_bot_profile: must match [a-z0-9_-]+")
    # Delivery is `hermes send` — the binary must resolve the same way
    # notify_flush._hermes_exe does: config hermes_bin, else PATH, else the
    # standard user-local install (launchd PATH is minimal).
    exe = _hermes_exe(cfg)
    if not _hermes_ok(exe):
        errors.append(f"hermes CLI not resolvable ({exe}) — "
                      "notifications cannot be sent")
    elif isinstance(cfg.get("notify"), dict) \
            and cfg["notify"].get("interactive") in ("discord", "slack"):
        # The gateway is hermes's own supervised service — cards and
        # /mcs commands cannot deliver without it (both transports).
        r = _hermes_cli(exe, "", "gateway", "status")
        if not (r and r.returncode == 0
                and "supervised" in (r.stdout or "")):
            warnings.append("hermes gateway is not supervised — "
                            "`mcs_setup services` installs it via "
                            "`hermes gateway install`")
        elif _plugin_newer_than_gateway(r.stdout or ""):
            # the gateway loads plugin code once at startup — a stale
            # worker serves cards without companion thread bodies
            # (2026-09 incident); surface the pending restart here
            warnings.append(
                "hermes_plugin/ is newer than the running gateway — "
                "`hermes gateway restart` so workers load current code")
    sem = cfg.get("semantic")
    if isinstance(sem, dict) and sem.get("mode", "off") != "off" \
            and env_value("TYPESAFE_API_KEY") is None:
        errors.append("semantic.mode is enabled but TYPESAFE_API_KEY "
                      "is not resolvable — Jev verdicts fail as "
                      "no_api_key")
    elif env_value("TYPESAFE_API_KEY") is None:
        warnings.append("TYPESAFE_API_KEY not set — needed when "
                        "semantic.mode is enabled")
    if sys.platform == "darwin":
        agents = os.path.expanduser("~/Library/LaunchAgents")
        warnings.extend(
            f"LaunchAgent {label} not installed — templates and "
            "install steps in deployment/launchagents/README.md"
            for label in AGENT_LABELS
            if not os.path.exists(os.path.join(agents, f"{label}.plist")))
        # plist presence is not liveness: an installed-but-unloaded
        # agent stops extraction silently (2026-09-28 incident: the
        # drainers sat unloaded while the backlog grew for days and
        # `check` stayed green). _agent_loaded resolves the GUI-domain
        # service; a domain that does not answer at all means this
        # session simply cannot verify — warn once, don't per-agent fail.
        domain_ok = _run(["launchctl", "print", f"gui/{os.getuid()}"],
                         timeout=10).returncode == 0
        if not domain_ok:
            warnings.append(
                f"launchd gui/{os.getuid()} unreachable from this "
                "session — cannot verify agent load state")
        for label in AGENT_LABELS:
            plist = os.path.join(agents, f"{label}.plist")
            if not os.path.exists(plist) or not domain_ok:
                continue
            if not _agent_loaded(label):
                errors.append(
                    f"LaunchAgent {label} installed but not loaded — "
                    "run `mcs_setup.py services` or "
                    f"`launchctl bootstrap gui/{os.getuid()} {plist}`")
    # T20: with the admission boundary enabled, every MCS LLM route
    # must be registered — a missing route fails closed FOREVER, so a
    # misconfigured broker is a blocked-startup error, not a stall.
    if os.environ.get("MCS_LLM_ADMISSION") not in (None, "", "0"):
        try:
            import local_llm
            broker = local_llm._broker()
            missing = [r for r in ("mcs.semantic", "mcs.extract")
                       if broker.routes.get(r) != "BACKLOG"]
            if missing:
                errors.append(
                    "admission broker route table lacks "
                    + ", ".join(missing)
                    + " — those callers are rejected permanently while "
                      "MCS_LLM_ADMISSION is set")
            elif not broker.is_open():
                warnings.append(
                    "admission epoch is closed — LLM work defers until "
                    "the backend is verified empty and the epoch "
                    "reopens")
        except Exception as e:
            errors.append(f"admission broker unusable: {e}")
    return errors, warnings


# ---- .env merge ------------------------------------------------------

def _env_write(path: str, updates: dict[str, str]):
    """Merge KEY=value lines — existing keys preserved unless updated."""
    encoded = {}
    for key, value in updates.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) \
                or not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
            raise ValueError("invalid_env_update")
        encoded[key] = (json.dumps(value, ensure_ascii=False)
                        if value != value.strip() or any(c in value for c in "\"'\\")
                        else value)
    updates = encoded
    lines, seen = [], set()
    with suppress(FileNotFoundError), open(path, encoding="utf-8") as stream:
        lines = stream.read().splitlines()
    out = []
    for line in lines:
        key = line.split("=", 1)[0]
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    _write_atomic(path, "\n".join(out) + "\n", 0o600)


def _keychain_store(account: str, pw: str) -> bool:
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

    if any(c in value for value in (account, pw) for c in "\r\n\x00"):
        return False
    previous = _run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", account, "-w"])
    if previous.returncode not in (0, 44):  # 44: no existing item
        return False
    old_password = previous.stdout.rstrip("\n") if previous.returncode == 0 else None
    if old_password is not None and any(c in old_password for c in "\r\n\x00"):
        return False
    cmd = (f"add-generic-password -s {_q(KEYCHAIN_SERVICE)} "
           f"-a {_q(account)} -U -w {_q(pw)}\n")
    written = _run(["security", "-i"], input_text=cmd)
    chk = _run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
         "-w"])
    if chk.returncode == 0 and chk.stdout.rstrip("\n") == pw:
        return True
    # Preserve a credential that existed before this attempt. Only a
    # successfully created item with verified prior absence may be deleted.
    if old_password is not None:
        rollback = (f"add-generic-password -s {_q(KEYCHAIN_SERVICE)} "
                    f"-a {_q(account)} -U -w {_q(old_password)}\n")
        _run(["security", "-i"], input_text=rollback)
    elif written.returncode == 0:
        _run(["security", "delete-generic-password", "-s",
              KEYCHAIN_SERVICE, "-a", account])
    return False


def _csv_list(values):
    """A repeated flag or a comma-separated string -> list[str] | None."""
    if not values:
        return None
    if isinstance(values, str):
        values = [values]
    out = [x.strip() for v in values for x in v.split(",") if x.strip()]
    return out or None


# ---- guided wizard ---------------------------------------------------
# Every settable config key, grouped for a beginner walkthrough.
# Item = (dotted key, kind, default, description, gate).
#   kind: req | opt | bool | int | num | csv | intlist | choice:a,b,...
#   req* kinds loop until a value is given; everything else accepts an
#   empty answer meaning "keep current, else the shown default".
#   gate(cfg) hides keys whose feature is off — discord.* only exists
#   when interactive delivery is on, digest knobs only when signals
#   notify, semantic details only when a mode is enabled.
_UNSET = object()

def _discord_on(cfg):
    return (cfg.get("notify") or {}).get("interactive") == "discord"


def _slack_on(cfg):
    return (cfg.get("notify") or {}).get("interactive") == "slack"


def _cards_on(cfg):
    return (cfg.get("notify") or {}).get("interactive") \
        in ("discord", "slack")


def _signals_on(cfg):
    return (cfg.get("signals") or {}).get("notify") is True


def _semantic_on(cfg):
    m = (cfg.get("semantic") or {}).get("mode", "off")
    return m != "off"


WIZARD = [
    ("基本設定", [
        ("mcs_login_id", "req", None,
         "MCS のログインID", None),
        ("notify_target", "req", None,
         "通知の送り先（hermes send target。例: discord:<チャンネルID>、"
         "slack:#mcs）", None),
    ]),
    ("カード通知（interactive=discord/slack でボタン付きカード）", [
        ("notify.interactive", "choice:off,discord,slack", "off",
         "通知形式 — discord/slack=カード / off=従来テキストのみ", None),
        ("notify.discord.profile", "req", None,
         "配送に使う hermes プロファイル名", _discord_on),
        ("notify.discord.application_id", "req", None,
         "Discord アプリケーションID", _discord_on),
        ("notify.discord.guild_id", "req", None,
         "Discord サーバーID", _discord_on),
        ("notify.discord.channel_id", "req", None,
         "カードの投稿先チャンネルID", _discord_on),
        ("notify.slack.profile", "req", None,
         "配送に使う hermes プロファイル名", _slack_on),
        ("notify.slack.application_id", "req", None,
         "Slack アプリID", _slack_on),
        ("notify.slack.team_id", "req", None,
         "Slack ワークスペース（team）ID", _slack_on),
        ("notify.slack.channel_id", "req", None,
         "カードの投稿先チャンネルID", _slack_on),
        ("notify.operator", "opt", None,
         "運用者の Discord ユーザーID（空欄可）", _discord_on),
        ("notify.card_thread", "bool", True,
         "患者スレッドごとにカードをまとめる", _cards_on),
        ("notify.card_thread_archive_min", "int", 10080,
         "カードスレッドをアーカイブするまでの分数", _cards_on),
        ("notify.route_epoch", "int", 1,
         "配送先を変えたとき+1する番号（通常はそのまま）", _cards_on),
        ("notify_bot_profile", "opt", None,
         "通知投稿に使う hermes プロファイル（空欄=既定）", None),
        ("notify_system_target", "opt", None,
         "障害・システム通知の送り先（空欄=notify_target と同じ）", None),
        ("notify_max_age_h", "num", None,
         "この時間より古い未読は通知しない（時間・空欄=制限なし）", None),
        ("hermes_bin", "opt", None,
         "hermes コマンドのパス（空欄=自動検出）", None),
    ]),
    ("収集ポリシー", [
        ("self_posts", "bool", False,
         "自分自身の投稿も取り込んで通知する（未読APIは自投稿を返さない"
         "ため、最新probe経由で検出）", None),
        ("deep_history", "bool", True,
         "初回に全履歴を遡って保存する", None),
        ("discover_archived", "bool", False,
         "アーカイブ済み患者も収集対象にする", None),
        ("trickle_pages", "int", 3,
         "1回の実行で履歴を遡るページ数（1-40）", None),
        ("job_budget_seconds", "num", None,
         "内部処理の時間予算・秒（空欄=既定）", None),
    ]),
    ("ローカルLLM（空欄=既定 llama.cpp :8080 / Qwen3.5-9B）", [
        ("local_llm.url", "opt", None,
         "chat/completions エンドポイント URL（loopback http のみ）",
         None),
        ("local_llm.model", "opt", None,
         "モデル名（OpenAI互換 API の model フィールド）", None),
    ]),
    ("レビュー候補シグナル（機械が確認候補を列挙）", [
        ("signals.notify", "bool", False,
         "確認候補をDiscord通知に出す", None),
        ("signals.digest", "bool", False,
         "複数候補をダイジェストにまとめて送る", _signals_on),
        ("signals.digest_interval_h", "num", None,
         "ダイジェストの間隔・時間（空欄=既定）", _signals_on),
        ("signals.self_organizations", "csv", None,
         "自施設名・カンマ区切り（空欄=MCSプロフィールから自動検出）",
         None),
        ("signals.self_professions", "csv", None,
         "自職種・カンマ区切り（同上）", None),
        ("signals.request_targets", "csv", None,
         "依頼先として数える宛名（空欄可）", None),
        ("signals.med_exclude_names", "csv", None,
         "薬剤判定から除外する語・カンマ区切り（空欄可）", None),
    ]),
    ("意味解析 semantic（要約の自動検査など・通常は off のまま）", [
        ("semantic.mode", "choice:off,shadow,enforce", "off",
         "意味解析モード — off以外は本文を外部Jev APIへ送信 "
         "shadow=記録のみ / enforce=判定に使用", None),
        ("semantic.project_ids", "reqintlist", None,
         "対象プロジェクトID・カンマ区切り（modeがoff以外では必須）",
         _semantic_on),
        ("semantic.extract_qc", "choice:off,annotate", "off",
         "抽出結果への Jev 監査注記", _semantic_on),
        ("semantic.daily_request_budget", "int", None,
         "Jev 呼出の1日上限（空欄=既定）", _semantic_on),
    ]),
]


def _get_key(cfg: dict, dotted: str):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_key(cfg: dict, dotted: str, val):
    parts = dotted.split(".")
    if val is _UNSET:
        cur = cfg
        for p in parts[:-1]:
            cur = cur.get(p)
            if not isinstance(cur, dict):
                return
        cur.pop(parts[-1], None)
        return
    cur = cfg
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = cur[p] = {}
        cur = nxt
    cur[parts[-1]] = val


def _fmt(v) -> str:
    if type(v) is bool:
        return "オン" if v else "オフ"
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return str(v)


def _parse_answer(kind: str, raw: str):
    """Typed answer -> (ok, value|error message)."""
    if kind in ("req", "opt"):
        return True, raw
    if kind == "bool":
        v = raw.lower()
        if v in ("y", "yes", "1", "true", "on"):
            return True, True
        if v in ("n", "no", "0", "false", "off"):
            return True, False
        return False, "y か n で答えてください"
    if kind == "int":
        try:
            return True, int(raw)
        except ValueError:
            return False, "整数で入力してください"
    if kind == "num":
        try:
            v = float(raw)
        except ValueError:
            return False, "数値で入力してください"
        if _num(v) is not None:
            return False, "0より大きく10億以下の有限の数値で入力してください"
        return True, int(v) if v == int(v) else v
    if kind in ("csv", "intlist", "reqintlist"):
        parts = [x.strip() for x in raw.split(",") if x.strip()]
        if kind == "reqintlist" and not parts:
            return False, "1件以上の整数をカンマ区切りで入力してください"
        if "intlist" in kind:
            try:
                return True, [int(x) for x in parts]
            except ValueError:
                return False, "整数をカンマ区切りで入力してください"
        return True, parts
    if kind.startswith("choice:"):
        opts = kind.split(":", 1)[1].split(",")
        return ((True, raw) if raw in opts
                else (False, "次のいずれかで入力: " + ", ".join(opts)))
    return True, raw


def _ask(key: str, kind: str, default, desc: str, cur):
    """One wizard item. Returns the value to store, or _UNSET."""
    eff = cur if cur is not None else default
    marks = []
    if cur is not None:
        marks.append(f"現在: {_fmt(cur)}")
    elif eff is not None:
        marks.append(f"既定: {_fmt(eff)}")
    if kind == "bool":
        marks.append("Y/n" if eff else "y/N")
    elif kind.startswith("choice:"):
        marks.append("/".join(kind.split(":", 1)[1].split(",")))
    if cur is not None and kind in ("opt", "num", "csv", "intlist"):
        marks.append("`-` で削除")
    prompt = (f"  {key} — {desc}"
              + (f" [{' / '.join(marks)}]" if marks else "") + ": ")
    while True:
        raw = input(prompt).strip()
        if not raw:
            if kind.startswith("req") and eff is None:
                print("    必須項目です — 値を入力してください")
                continue
            return eff if eff is not None else _UNSET
        if raw == "-" and cur is not None and not kind.startswith("req"):
            return _UNSET
        ok, val = _parse_answer(kind, raw)
        if ok:
            return val
        print(f"    {val}")


def _wizard(cfg: dict):
    """Walk every config key in sections — Enter accepts the shown
    current/default value, so a beginner can step through safely.
    Items whose feature is off are skipped via their gate."""
    print("\n各項目は Enter で現在値/既定値のまま進みます。"
          "わからない項目は Enter で構いません。")
    for i, (title, items) in enumerate(WIZARD, 1):
        print(f"\n【{i}/{len(WIZARD)}】{title}")
        for key, kind, default, desc, when in items:
            if when is not None and not when(cfg):
                continue
            _set_key(cfg, key,
                     _ask(key, kind, default, desc, _get_key(cfg, key)))


PLUGIN_SETTINGS = "plugins.entries.mcs-discord-commands.settings"


def _csv_yaml(text: str) -> str:
    """Comma-separated ids -> YAML list literal, so `config set` stores a
    real list (the plugin schema expects lists, not csv strings)."""
    return json.dumps([x.strip() for x in text.split(",") if x.strip()],
                      ensure_ascii=False)


def _apply_plugin_integration(cfg: dict, args) -> bool:
    """Wire the mcs-discord-commands plugin into hermes through the
    public `hermes config` CLI — writes the serving profile's
    config.yaml / .env; never touches hermes-agent internals. Runs
    when notify.interactive is "discord" or "slack" (the one plugin
    serves both transports; slack scope keys are slack_*).

    Returns False when the CLI is unavailable or any attempted write
    failed; keys left unset by choice (--yes, empty answer) are not
    failures."""
    ntf = cfg.get("notify")
    if not isinstance(ntf, dict) \
            or ntf.get("interactive") not in ("discord", "slack"):
        return True
    transport = ntf["interactive"]
    exe = _hermes_exe(cfg)
    if not _hermes_ok(exe):
        print("\nhermes CLI が見つかりません — プラグイン設定は後で "
              "`hermes config set "
              f"{PLUGIN_SETTINGS}.<key> <値>` で行ってください")
        return False
    nd = ntf.get(transport) if isinstance(ntf.get(transport), dict) else {}
    label = {"discord": "Discord", "slack": "Slack"}[transport]
    profile = getattr(args, "plugin_profile", None)
    if profile is None:
        if args.yes:
            profile = ""
        else:
            profile = input(
                "\nプラグイン設定を書き込む hermes プロファイル名"
                f"（multiplex 構成では {label} を受け持つ profile。"
                "空欄=既定）: ").strip()
    print(f"\n{label} 連携 — hermes "
          f"{'-p ' + profile if profile else '既定'}"
          " profile にプラグイン設定を書き込みます")

    data = os.path.join(HOME, "data")
    fixed = {
        "snapshot": os.path.join(data, "snapshots",
                                 "ledger-snapshot.db"),
        "inbox": os.path.join(data, "cmd"),
        "data_root": data,
        "interactive": "true",
    }
    if transport == "discord":
        for k in ("profile", "application_id", "guild_id", "channel_id"):
            if nd.get(k):
                fixed[k] = nd[k]
        # operator-specific allowlists: flag > existing value > prompt
        lists = (
            ("allowed_user_ids", getattr(args, "plugin_user_ids", None),
             "カード操作を許可する Discord user ID", ""),
            ("allowed_chat_ids", getattr(args, "plugin_chat_ids", None),
             "コマンドを受け付ける Discord channel ID",
             nd.get("channel_id", "")),
            ("project_ids", getattr(args, "plugin_project_ids", None),
             "対象とする MCS project ID", ""),
        )
        tokens = (("DISCORD_BOT_TOKEN", "Discord bot token"),)
    else:
        fixed["slack_adapter_enabled"] = "true"
        for k in ("profile", "application_id", "team_id", "channel_id"):
            if nd.get(k):
                fixed[f"slack_{k}"] = nd[k]
        lists = (
            ("slack_allowed_user_ids",
             getattr(args, "plugin_user_ids", None),
             "カード操作を許可する Slack user ID", ""),
            ("project_ids", getattr(args, "plugin_project_ids", None),
             "対象とする MCS project ID", ""),
        )
        tokens = (("SLACK_BOT_TOKEN", "Slack bot token (xoxb-…)"),
                  ("SLACK_APP_TOKEN", "Slack app-level token (xapp-…)"))
    missing = []
    failed = False
    for key, val in fixed.items():
        if _hermes_config_set(exe, profile, f"{PLUGIN_SETTINGS}.{key}",
                              val):
            print(f"  settings.{key}: 設定")
        else:
            missing.append(key)
            failed = True

    for key, flag_val, desc, default in lists:
        if flag_val is None:
            if _hermes_config_get(exe, profile,
                                  f"{PLUGIN_SETTINGS}.{key}"):
                print(f"  settings.{key}: 設定済み")
                continue
            if args.yes:
                missing.append(key)
                continue
            flag_val = input(
                f"  {key} — {desc}（カンマ区切り"
                + (f"。Enter={default}" if default else "")
                + "）: ").strip() or default
        if not flag_val:
            missing.append(key)
            continue
        lit = flag_val if flag_val.lstrip().startswith("[") \
            else _csv_yaml(flag_val)
        if _hermes_config_set(exe, profile, f"{PLUGIN_SETTINGS}.{key}",
                              lit):
            print(f"  settings.{key}: 設定")
        else:
            missing.append(key)
            failed = True
    if missing:
        print("  未設定: " + ", ".join(missing) +
              " — 後で `hermes config set "
              f"{PLUGIN_SETTINGS}.<key> <値>` で設定してください")

    # Pipe tokens to the public writer so process listings cannot expose
    # them. Older Hermes versions must fail instead of falling back to
    # argv. `config set` routes *_TOKEN keys to the profile .env. They
    # must land in the SERVING profile's .env: under multiplex each
    # profile's secret scope is authoritative and a miss never falls
    # through to the default profile's .env (agent/secret_scope.py).
    # Already-configured installs keep their existing tokens.
    for env_key, desc in tokens:
        tok = os.environ.get(env_key)
        if tok:
            ok = _hermes_config_set(exe, profile, env_key, tok)
            failed |= not ok
            print(f"  {env_key}: "
                  + (f"hermes {'-p ' + profile if profile else '既定'} "
                     ".env へ保存" if ok else "保存失敗 — Hermes の config set --stdin 対応を確認してください"))
        elif _hermes_config_get(exe, profile, env_key):
            print(f"  {env_key}: 設定済み")
        else:
            if not args.yes:
                tok = getpass.getpass(
                    f"  {desc}（hermes .env へ保存。"
                    "空欄=スキップ）: ") or None
                if tok:
                    ok = _hermes_config_set(exe, profile, env_key, tok)
                    failed |= not ok
                    print(f"  {env_key}: "
                          + (f"hermes {'-p ' + profile if profile else '既定'}"
                             " .env へ保存" if ok else "保存失敗 — Hermes の config set --stdin 対応を確認してください"))
            if not tok:
                print(f"  {env_key}: 未設定 — `hermes "
                      + (f"-p {profile} " if profile else "")
                      + "setup` で後から設定")
    return not failed


def cmd_init(args) -> int:
    cfg = load_config()
    def pick(flag, key):
        # flag wins, else the existing value is kept — the wizard's req
        # items do the interactive prompting for these
        return flag if flag is not None else cfg.get(key)

    login_id = pick(args.login_id, "mcs_login_id")
    target = pick(args.notify_target, "notify_target")
    updates = {}
    if login_id:
        cfg["mcs_login_id"] = login_id
    if target:
        cfg["notify_target"] = str(target)

    # --set KEY=JSON gives non-interactive coverage of every config
    # key (dotted paths nest); the trailing cmd_check validates them.
    fact_selection = (_get_key(cfg, "semantic.fact_source"),
                      _get_key(cfg, "semantic.fact_source_gate"))
    for kv in args.set or []:
        k, sep, v = kv.partition("=")
        if not sep or not k.strip():
            print(f"--set: expected KEY=VALUE, got {kv!r}")
            return 1
        try:
            val = json.loads(v)
        except ValueError:
            val = v
        _set_key(cfg, k.strip(), val)
    # canonical promotion is gated by `fact-source` (evidence check +
    # pinned token) — --set, including a whole `semantic` object, must
    # not mint or re-pin it. An unchanged existing pin passes through.
    selection = (_get_key(cfg, "semantic.fact_source"),
                 _get_key(cfg, "semantic.fact_source_gate"))
    if selection[0] == "canonical" and selection != fact_selection:
        print("--set: semantic.fact_source=canonical (and its gate) can "
              "only be set via `mcs_setup.py fact-source canonical "
              "--gate-evidence <report>` — config not written")
        return 1

    # secrets come from the environment (or getpass) — never argv flags,
    # which persist in shell history and `ps` (FIX-SU1)
    pw = os.environ.get("MCS_SETUP_PASSWORD")
    if pw is None and not args.yes:
        pw = getpass.getpass(
            "MCS password (stored in Keychain 'mcs-adapter' + .env "
            "reboot fallback, leave empty to skip): ")
    keychain_failed = False
    if pw:
        # .env carries the same secret as the reboot fallback — after a
        # restart the login keychain is locked until first unlock, and
        # auto_login reads MCS_PASSWORD when the keychain can't answer.
        updates["MCS_PASSWORD"] = pw
        if not _keychain_store(login_id or "mcs", pw):
            print("keychain update not verified — check the existing entry; "
                  "the .env fallback is handled below")
            keychain_failed = True
        else:
            print(f"keychain: '{KEYCHAIN_SERVICE}' registered")

    ts_key = os.environ.get("TYPESAFE_API_KEY")
    if ts_key:
        updates["TYPESAFE_API_KEY"] = ts_key
    if updates:
        try:
            _env_write(ENV_PATH, updates)
        except ValueError:
            print(".env: invalid credential format; existing file retained")
            return 1
        print(f".env: wrote {sorted(updates)} to {ENV_PATH} (0600)")
    if keychain_failed:
        return 1

    if args.semantic_mode:
        sem = cfg.get("semantic")
        if not isinstance(sem, dict):
            if sem is not None:
                print("config: existing 'semantic' is not an object — "
                      "resetting")
            sem = cfg["semantic"] = {}
        sem["mode"] = args.semantic_mode
        if args.project_ids is not None:
            sem["project_ids"] = args.project_ids
    if args.signals_notify is not None:
        sig = cfg.get("signals")
        if not isinstance(sig, dict):
            if sig is not None:
                print("config: existing 'signals' is not an object — "
                      "resetting")
            sig = cfg["signals"] = {}
        sig["notify"] = args.signals_notify

    # own facility/professions — manual OVERRIDE of the self identity
    # the signal engine otherwise derives automatically from MCS
    # (/users/self -> self_profile_v1 artifact). Flag-only: no prompt,
    # since the fetched profile normally covers this.
    cur_sig = cfg.get("signals")
    cur_sig = cur_sig if isinstance(cur_sig, dict) else {}
    orgs = (_csv_list(args.self_orgs)
            or (cur_sig.get("self_organizations")
                if isinstance(cur_sig.get("self_organizations"), list)
                else None))
    profs = (_csv_list(args.self_professions)
             or (cur_sig.get("self_professions")
                 if isinstance(cur_sig.get("self_professions"), list)
                 else None))
    if orgs or profs:
        sig = cfg.get("signals")
        if not isinstance(sig, dict):
            if sig is not None:
                print("config: existing 'signals' is not an object — "
                      "resetting")
            sig = cfg["signals"] = {}
        if orgs:
            sig["self_organizations"] = orgs
        if profs:
            sig["self_professions"] = profs

    if not args.yes:
        _wizard(cfg)

    for d in (os.path.join(HOME, "data"),
              os.path.join(HOME, "data", "cmd"),
              os.path.join(HOME, "chrome-profile")):
        # PHI/session stores: owner-only even when the dir pre-existed
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    _write_atomic(CONF_PATH, json.dumps(cfg, ensure_ascii=False,
                                      indent=2, sort_keys=True) + "\n", 0o600)
    print(f"config: wrote {CONF_PATH}")
    integration_ok = _apply_plugin_integration(cfg, args)
    # an interactive transport needs the supervised gateway — on a
    # first install `services` ran BEFORE init could configure
    # interactive, so sync just the gateway here rather than leaving
    # a manual `services` rerun as the last step
    ntf = cfg.get("notify")
    if isinstance(ntf, dict) \
            and ntf.get("interactive") in ("discord", "slack"):
        exe = _hermes_exe(cfg)
        if _hermes_ok(exe):
            _sync_gateway(cfg, exe, lambda m: print(f"  {m}"),
                          dry=False)
    check_result = cmd_check(args)
    if not integration_ok:
        print("init: FAIL — Hermes plugin settings were not fully "
              "written; repair the serving profile/CLI and re-run init")
        return 1
    return check_result


def _shipped_g6_criteria() -> tuple[dict, str]:
    """The shipped G6 criteria, normalized and hashed exactly as
    semantic_evaluation embeds them in a report (criteria_sha256)."""
    import semantic_evaluation
    with open(G6_CRITERIA_PATH, encoding="utf-8") as f:
        criteria = semantic_evaluation.validate_criteria(json.load(f))
    digest = hashlib.sha256(json.dumps(
        criteria, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return criteria, digest


def cmd_fact_source(args) -> int:
    """Set semantic.fact_source with an evidence gate for ``canonical``.

    Production promotion is pinned: ``--gate-evidence`` must be a
    semantic_evaluation report scored against the shipped G6 criteria
    (criteria_sha256 match) whose gate passed with no reasons on at
    least min_human_labels human-labelled cases.  Synthetic, failing or
    foreign-criteria reports can never mint the token, and the written
    config is validated fail-closed before it lands.
    """
    cfg = load_config()
    existing_sem = cfg.get("semantic")
    if existing_sem is not None and not isinstance(existing_sem, dict):
        print("fact_source: 'semantic' in config.json is not an object "
              "— fix the file manually before changing fact_source")
        return 1
    sem = cfg.setdefault("semantic", {})
    source = args.fact_source
    if source == "canonical":
        if not args.gate_evidence:
            print("fact_source: canonical requires --gate-evidence "
                  "(a semantic_evaluation report with a passing "
                  "human-labelled gate)")
            return 1
        try:
            with open(args.gate_evidence, "rb") as stream:
                raw = stream.read(8 * 1024 * 1024)
            report = json.loads(raw)
        except (OSError, ValueError, RecursionError) as e:
            print(f"fact_source: gate evidence unreadable ({e})")
            return 1
        gate = report.get("gate") if isinstance(report, dict) else None
        provenance = report.get("label_provenance") \
            if isinstance(report, dict) else None
        human = provenance.get("human", 0) \
            if isinstance(provenance, dict) else 0
        if not isinstance(report, dict) \
                or not report.get("schema_version") \
                or not isinstance(gate, dict) or gate.get("pass") is not True \
                or gate.get("g6_eligible") is not True \
                or type(human) is not int or human < 1:
            reasons = gate.get("reasons") if isinstance(gate, dict) \
                else None
            print("fact_source: gate evidence does not pass on "
                  f"human-labelled data{f' ({reasons})' if reasons else ''}")
            return 1
        try:
            criteria, criteria_sha = _shipped_g6_criteria()
        except Exception as e:
            # the gate cannot be judged without its criteria — fail closed
            print(f"fact_source: shipped G6 criteria unreadable "
                  f"({type(e).__name__}: {e})")
            return 1
        reasons = gate.get("reasons")
        problem = None
        if report.get("criteria_sha256") != criteria_sha \
                or report.get("criteria_version") != criteria["version"]:
            problem = ("not scored against the shipped G6 criteria "
                       f"({os.path.relpath(G6_CRITERIA_PATH, REPO_ROOT)})")
        elif human < criteria["min_human_labels"]:
            problem = (f"{human} human labels < min_human_labels "
                       f"{criteria['min_human_labels']}")
        elif not isinstance(reasons, list) or reasons:
            problem = f"gate reasons not empty ({reasons!r})"
        if problem:
            print(f"fact_source: gate evidence rejected — {problem}")
            return 1
        digest = hashlib.sha256(raw).hexdigest()[:16]
        criteria_version = criteria["version"]
        sem["fact_source_gate"] = f"{criteria_version}:{digest}"
    else:
        sem.pop("fact_source_gate", None)
    sem["fact_source"] = source
    # Fail closed: never write a config the production validator rejects.
    errors, _ = validate_config(cfg)
    sem_errors = [e for e in errors if e.startswith("config: semantic")]
    if sem_errors:
        print("fact_source: refusing to write invalid semantic config — "
              + "; ".join(sem_errors))
        return 1
    _write_atomic(CONF_PATH, json.dumps(cfg, ensure_ascii=False,
                                      indent=2, sort_keys=True) + "\n", 0o600)
    print(f"config: semantic.fact_source = {source}"
          + (f" (gate {sem['fact_source_gate']})"
             if source == "canonical" else ""))
    return 0


def cmd_jev_value(args) -> int:
    """Run the Jev incremental-value evaluation over a labelled bench
    corpus and write the evidence report.

    The run uses the configured local LLM for extraction and a real Jev
    client when TYPESAFE_API_KEY is set; without it the audit cannot be
    evaluated and the report honestly records evaluated=False — never a
    synthetic pass."""
    import semantic
    import semantic_jev as jev
    import semantic_evaluation
    try:
        with open(args.cases, "rb") as stream:
            raw = stream.read(8 * 1024 * 1024)
        payload = json.loads(raw)
    except (OSError, ValueError) as e:
        print(f"jev-value: cases unreadable ({e})")
        return 1
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        print("jev-value: cases file has no cases list")
        return 1
    cfg = load_config()
    scfg = cfg.get("semantic") or {}
    jev_client = None
    api_key = env_value("TYPESAFE_API_KEY")
    if api_key:
        jev_client = jev.JevClient(
            api_key=api_key,
            model=scfg.get("model") or jev.JEV_MODEL)
    report = semantic_evaluation.evaluate_jev_incremental(
        cases, semantic.llm_chat, jev_client,
        deadline_s=args.deadline)
    report["cases_source"] = os.path.basename(args.cases)
    report["label_provenance"] = {"human": 0,
                                  "synthetic": len(cases)}
    try:
        semantic_evaluation.write_report(args.out, report)
    except OSError as e:
        print(f"jev-value: cannot write report ({e})")
        return 1
    audit = report["audit"]
    print(f"jev-value: cases={report['cases']} "
          f"evaluated={report['evaluated']} "
          f"deterministic={audit['deterministic_findings']} "
          f"jev_incremental={audit['jev_incremental_findings']} "
          f"-> {args.out}")
    return 0 if report["evaluated"] else 2


def _semantic_queue_warnings(db, sem: dict, now: float):
    """Yield stalled-job warnings for enabled, due work in the selected projects."""
    projects = sem.get("project_ids", [])
    if projects == []:
        return
    scope, params = "", []
    if isinstance(projects, list):
        scope = " AND project_id IN (" + ",".join("?" for _ in projects) + ")"
        params = projects
    enabled_kinds = (("extract_qc", sem.get("extract_qc") == "annotate"),
                     ("semantic", sem.get("mode", "off") != "off"))
    for kind, enabled in enabled_kinds:
        if not enabled:
            continue
        row = db.execute(
            "SELECT COUNT(*), MIN(MAX(updated_at,next_try)) FROM fetch_jobs "
            "WHERE kind=? AND state='pending' AND next_try<=?" + scope,
            (kind, now, *params)).fetchone()
        if not (row and row[0]):
            continue
        pending, oldest = row
        done_24h = db.execute(
            "SELECT COUNT(*) FROM fetch_jobs WHERE kind=? "
            "AND state='done' AND updated_at>?" + scope,
            (kind, now - 86400, *params)).fetchone()[0]
        age_d = (now - oldest) / 86400 if oldest else 0
        if done_24h == 0 and age_d >= 1:
            yield (f"{kind} jobs pending={pending} with no progress "
                   f"for {age_d:.1f}d — drain may be stalled")
        elif age_d > 3:
            yield (f"{kind} jobs pending={pending} oldest eligible "
                   f"wait={age_d:.1f}d — review drain capacity")


def _extract_queue_warnings(db):
    """Yield extraction backlog warnings using the worker's shared predicate."""
    import extract_llm
    backlog = db.execute(
        "SELECT COUNT(*) FROM messages m WHERE " + extract_llm.pending_pred()
    ).fetchone()[0]
    if backlog > 500:
        yield (f"extract_llm backlog={backlog} messages — "
               "drainers (ai.mcs.extract-drainer*) chew DESC; "
               "review eligible backlog and recent progress")


def _fact_pipeline_warnings(db, sem: dict):
    """Yield missing-output warnings for the configured shadow/canonical stages."""
    fact_source = sem.get("fact_source", "legacy")
    if sem.get("mode", "off") == "off" or fact_source == "legacy":
        return
    # Shadow produces v2 documents; canonical must also publish projections.
    llm_done, v2, canon = db.execute(
        "SELECT COUNT(*) FILTER (WHERE kind='extract_llm'),"
        " COUNT(*) FILTER (WHERE kind='semantic_facts_v2'),"
        " COUNT(*) FILTER (WHERE kind='canonical_projection')"
        " FROM artifacts WHERE kind IN ('extract_llm',"
        " 'semantic_facts_v2','canonical_projection')"
    ).fetchone()
    if llm_done > 50 and v2 == 0:
        yield ("semantic shadow pipeline produced 0 "
               f"semantic_facts_v2 against {llm_done} "
               "extract_llm — the semantic drain has never "
               "run (check semantic queue/scheduling)")
    if fact_source == "canonical" and v2 > 0 and canon == 0:
        yield ("fact_source=canonical but canonical_projection"
               " has 0 artifacts — every generation parks "
               "non-PASS (check v4_diagnostic / semantic_audit)")


def _queue_warnings(cfg: dict | None = None) -> list[str]:
    """Read queue health without creating a ledger or mutating its contents.

    Missing ledgers are silent. Read failures retain earlier findings and
    append an unreadable warning; every opened connection is closed.
    """
    out = []
    db_path = os.path.join(HOME, "data", "ledger.db")
    if not os.path.exists(db_path):
        return out
    try:
        import sqlite3
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as db:
            now = time.time()
            sem = (cfg or {}).get("semantic") or {}
            out.extend(_semantic_queue_warnings(db, sem, now))
            out.extend(_extract_queue_warnings(db))
            out.extend(_fact_pipeline_warnings(db, sem))
    except Exception as e:
        out.append(f"queue health unreadable ({type(e).__name__})")
    return out


def cmd_check(args) -> int:
    cfg = load_config()
    errors, warnings = validate_config(cfg)
    e2, w2 = check_environment(cfg)
    errors += e2
    warnings += w2 + _queue_warnings(cfg)
    for w in warnings:
        print(f"  warn : {w}")
    for e in errors:
        print(f"  error: {e}")
    print("check: " + ("FAIL" if errors else "OK")
          + f" ({len(errors)} errors, {len(warnings)} warnings)")
    return 1 if errors else 0


# ---- scheduled services (launchd + hermes cron) -----------------------
# Automates the manual steps in deployment/launchagents/README.md:
# render the placeholder templates, load the launchd agents, register
# the hermes cron jobs. Idempotent — already-loaded agents and existing
# cron names are skipped.

HERMES_HOME = os.path.expanduser("~/.hermes")
HERMES_PY = os.path.join(HERMES_HOME, "hermes-agent", "venv", "bin",
                         "python")
SCRIPTS_DIR = os.path.join(HERMES_HOME, "scripts")
AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
G6_CRITERIA_PATH = os.path.join(REPO_ROOT, "evaluation",
                                "g6-criteria-v1.json")

CRON_JOBS = [
    ("MCS unread check", "*/5 * * * *", "mcs_check.sh"),
    ("MCS health watch", "*/5 * * * *", "mcs_health.sh"),
    ("MCS durable drain", "7,37 * * * *", "mcs_deep.sh"),
    ("MCS LLM catchup", "30 22 * * *", "mcs_llm_catchup.sh"),
    ("llamacpp daily restart", "0 4 * * *", "llamacpp_restart_if_idle.sh"),
    ("MCS update check", "10 5 * * *", "mcs_update.sh"),
]
# RESIDENT = KeepAlive drainers the updater quiesces/restarts itself;
# WATCHER = WatchPaths triggers — verified loaded, never keep-alive.
RESIDENT_LABELS = ["ai.mcs.extract-drainer", "ai.mcs.extract-drainer-rt"]
WATCHER_LABELS = ["local.mcs-cmd", "local.mcs-int"]
AGENT_LABELS = WATCHER_LABELS + RESIDENT_LABELS
# install.sh-owned labels that must never enter MCS ownership (S5).
EXCLUDED_LABELS = frozenset({"ai.mcs.llamaserver", "org.mcs.recovery"})
MANIFEST_PATH = os.path.join(HOME, "data", "service_manifest.json")


def _render_template(text: str, subs: dict) -> str:
    return re.sub(r"__([A-Z_]+)__",
                  lambda match: subs.get(match.group(1), match.group(0)), text)


def _write_atomic(path: str, body: str, mode: int | None = None) -> None:
    """tmp -> fsync -> chmod -> os.replace -> dir fsync — a mid-write
    crash never leaves a truncated script/plist behind, and the file
    never exists at the destination with the wrong mode (S: atomic
    render)."""
    atomic_write(path, lambda f: f.write(body), mode, tmp_prefix=".svc.")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cron_list(hermes: str) -> list[dict] | None:
    """Tolerant parse of `hermes cron list --all` — job-id header line
    followed by indented 'Name/Schedule/Script:' fields. Returns None
    when the call fails or output is unparseable (=> 'unverifiable')."""
    try:
        r = subprocess.run([hermes, "cron", "list", "--all"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out: list[dict] = []
    cur = None
    output = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)
    for line in output.splitlines():
        m = re.match(r"^\s{2}([0-9a-f]{6,})\b", line)
        if m:
            cur = {"id": m.group(1)}
            out.append(cur)
            continue
        m = re.match(r"^\s+(Name|Schedule|Script):\s*(.+?)\s*$", line)
        if m:
            if cur is None:
                return None            # field before any header
            cur[m.group(1).lower()] = m.group(2)
    if not out and "No scheduled jobs." not in output:
        return None
    if any(not entry.get("name") or not entry.get("schedule") for entry in out):
        return None
    return out


def _norm_sched(s: str) -> str | None:
    """Extract a comparable 5-field cron expr from a Schedule field —
    tolerant of 'cron: ' prefixes or extra decoration."""
    field = r"[0-9A-Za-z*/?,#\-]+"
    m = re.search(rf"(?<!\S)({field}(?:\s+{field}){{4}})(?!\S)", s or "")
    return " ".join(m.group(1).split()) if m else None


def _agent_loaded(label: str) -> bool:
    uid = os.getuid()
    r = _run(["launchctl", "print", f"gui/{uid}/{label}"])
    return r.returncode == 0


def _agent_reconcile(label: str, dst: str, note, dry: bool) -> bool:
    """Bootout+bootstrap when the plist changed; bootstrap when absent.
    Converges loaded state to rendered content (R6)."""
    uid = os.getuid()
    if _agent_loaded(label):
        note(f"agent {label}: reload (content changed)")
        if not dry:
            _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
    else:
        note(f"agent {label}: bootstrap")
    if dry:
        return True
    r = _run(["launchctl", "bootstrap", f"gui/{uid}", dst])
    if r.returncode != 0:
        note(f"  bootstrap failed: {r.stderr.strip()}")
        return False
    return _agent_loaded(label)


def _load_manifest() -> dict:
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest(manifest: dict) -> None:
    _write_atomic(MANIFEST_PATH,
                  json.dumps(manifest, ensure_ascii=False,
                             indent=1, sort_keys=True))


def _sync_scripts(subs, manifest, note, dry) -> None:
    """Stage 1: hermes cron wrapper scripts -> ~/.hermes/scripts (atomic)."""
    src = os.path.join(REPO_ROOT, "deployment", "scripts")
    for name in sorted(os.listdir(src)):
        if not name.endswith(".sh"):
            continue
        body = _render_template(
            Path(os.path.join(src, name)).read_text(encoding="utf-8"),
            {key: shlex.quote(value) for key, value in subs.items()})
        dst = os.path.join(SCRIPTS_DIR, name)
        manifest["scripts"].append({"name": name,
                                    "sha256": _sha256(body)})
        cur = None
        with suppress(OSError):
            cur = Path(dst).read_text(encoding="utf-8")
        if cur == body:
            note(f"script {name}: up to date")
            continue
        note(f"script {name}: write {dst}")
        if not dry:
            _write_atomic(dst, body, 0o755)


def _sync_agents(subs, prev, manifest, note, dry) -> int:
    """Stage 2: launchd agents (macOS only) — converge content AND
    loaded state; retire owned-but-undesired agents from our naming
    space only, never install.sh-owned labels (S5)."""
    if sys.platform != "darwin":
        note("launchd: not macOS — skipping agents")
        return 0
    problems = 0
    pdir = os.path.join(REPO_ROOT, "deployment", "launchagents")
    uid = os.getuid()
    for label in AGENT_LABELS:
        srcp = os.path.join(pdir, label + ".plist")
        body = _render_template(
            Path(srcp).read_text(encoding="utf-8"),
            {key: xml_escape(value, quote=False) for key, value in subs.items()})
        dst = os.path.join(AGENTS_DIR, label + ".plist")
        try:
            cur = Path(dst).read_text(encoding="utf-8")
        except OSError:
            cur = None
        changed = cur != body
        if changed:
            note(f"agent {label}: write {dst}")
            if not dry:
                _write_atomic(dst, body)
        if changed or not _agent_loaded(label):
            if not _agent_reconcile(label, dst, note, dry):
                problems += 1
                manifest["agents"].append(
                    {"label": label, "sha256": _sha256(body),
                     "loaded": False})
                continue
        else:
            note(f"agent {label}: loaded")
        manifest["agents"].append({"label": label,
                                   "sha256": _sha256(body),
                                   "loaded": True})
    # retire owned-but-undesired agents: our naming space only,
    # never install.sh-owned labels (S5)
    for path in sorted(
            p for p in os.listdir(AGENTS_DIR) if p.endswith(".plist")) \
            if os.path.isdir(AGENTS_DIR) else []:
        label = path[:-6]
        mcs_owned = label.startswith("local.mcs-") \
            or label.startswith("ai.mcs.extract-")
        if not mcs_owned or label in EXCLUDED_LABELS \
                or label in AGENT_LABELS:
            continue
        note(f"agent {label}: undesired — bootout + remove")
        if not dry:
            _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
            if _agent_loaded(label):
                note(f"  agent {label}: still loaded; keeping plist")
                problems += 1
                continue
            try:
                os.unlink(os.path.join(AGENTS_DIR, path))
            except OSError:
                problems += 1
    return problems


def _sync_cron(prev, hermes, manifest, note, dry) -> int:
    """Stage 3: hermes cron jobs — create missing, edit drifted,
    remove owned-but-undesired; an unparseable list fails CLOSED
    (M1): 'unverifiable' is NOT 'no jobs', and creating on that
    assumption produces duplicates."""
    entries = _cron_list(hermes)
    if entries is None:
        note("cron: list unparseable — unverifiable, "
             "no cron mutations performed")
        return 1
    problems = 0
    mutated = False
    # derive identities from the SAME verified list — a second
    # `cron list` call could fail transiently and return an
    # empty set that looks like 'no jobs' (fail-open, M1)
    existing = {v for e in entries for v in
                (e.get("name"), e.get("script")) if v}
    by_script = {e.get("script"): e for e in entries
                 if e.get("script")}
    desired_scripts = {s for _, _, s in CRON_JOBS}
    # one job per owned script is the invariant every step below
    # relies on — which duplicate is authoritative is an operator call
    dups = sorted(s for s in desired_scripts
                  if sum(e.get("script") == s for e in entries) > 1)
    if dups:
        note("cron: duplicate owned scripts — resolve job IDs manually "
             f"({', '.join(dups)}), then re-run services")
        return 1
    owned_scripts = desired_scripts | {
        c.get("script") for c in prev.get("cron", [])
        if isinstance(c, dict) and c.get("script")}
    for name, sched, script in CRON_JOBS:
        entry = by_script.get(script)
        if entry is None and (name in existing
                              or script in existing):
            # identity seen but its script is unparseable — neither
            # 'exists' (unverified) nor safe to create (duplicate)
            note(f"cron '{name}': identity seen but script unverifiable "
                 "— no create; inspect `hermes cron list --all`")
            problems += 1
            continue
        if entry is None:
            note(f"cron '{name}': create ({sched} -> {script})")
            mutated = True
            if not dry:
                r = _run(
                    [hermes, "cron", "create", sched,
                     "--name", name, "--script", script,
                     "--no-agent", "--deliver", "local"])
                if r.returncode != 0:
                    note(f"  create failed: "
                         f"{(r.stderr or r.stdout).strip()}")
                    problems += 1
                    continue
            manifest["cron"].append(
                {"name": name, "schedule": sched,
                 "script": script})
            continue
        cur_sched = _norm_sched(entry.get("schedule"))
        want_sched = _norm_sched(sched)
        if cur_sched and want_sched and cur_sched != want_sched:
            note(f"cron '{name}': schedule "
                 f"{cur_sched} -> {sched}")
            mutated = True
            if not dry:
                r = _run(
                    [hermes, "cron", "edit", entry["id"],
                     "--schedule", sched])
                if r.returncode != 0:
                    note(f"  edit failed: "
                         f"{(r.stderr or r.stdout).strip()}")
                    problems += 1
        else:
            note(f"cron '{name}': exists")
        manifest["cron"].append(
            {"name": name, "schedule": sched, "script": script,
             "id": entry["id"]})
    for entry in entries:
        script = entry.get("script")
        if script and script in owned_scripts \
                and script not in desired_scripts:
            note(f"cron '{entry.get('name', script)}': "
                 f"undesired — remove {entry['id']}")
            mutated = True
            if not dry:
                r = _run(
                    [hermes, "cron", "remove", entry["id"]])
                if r.returncode != 0:
                    note(f"  remove failed: "
                         f"{(r.stderr or r.stdout).strip()}")
                    problems += 1
    if mutated and not dry:
        # a zero exit is not proof — confirm the converged state, else
        # the manifest would record jobs that do not exist
        after = _cron_list(hermes)
        if after is None or any(
                sum(e.get("script") == script
                    and _norm_sched(e.get("schedule")) == _norm_sched(sched)
                    for e in after) != 1
                for _, sched, script in CRON_JOBS):
            note("cron: post-change state unverifiable or not exactly one "
                 "job per script — re-run services")
            problems += 1
    return problems


def _sync_gateway(cfg, hermes, note, dry) -> int:
    """Stage 4: hermes gateway — needed for either interactive
    transport (Discord interactions / Slack socket mode); `gateway
    install` creates the launchd service hermes owns."""
    ntf = cfg.get("notify") if isinstance(cfg, dict) else None
    if not (isinstance(ntf, dict)
            and ntf.get("interactive") in ("discord", "slack")):
        return 0
    r = _hermes_cli(hermes, "", "gateway", "status")
    up = bool(r and r.returncode == 0
              and "supervised" in (r.stdout or ""))
    if up:
        note("gateway: supervised")
        return 0
    if dry:
        note("gateway: install + start")
        return 0
    for sub in ("install", "start"):
        r = _hermes_cli(hermes, "", "gateway", sub)
        if not (r and r.returncode == 0):
            detail = (r.stderr or r.stdout).strip() \
                if r else "no response"
            note(f"gateway {sub} failed: {detail}")
            return 1
    note("gateway: installed + started")
    return 0


def _record_llm_slots(manifest, note) -> None:
    """Stage 5 (T19): record the selected/rollback backend slot counts
    and the checked-in plist's -np so drift between the deployed width
    and the code-side selection is visible in the snapshot."""
    try:
        import local_llm
        plist_np = None
        src = os.path.join(REPO_ROOT, "deployment", "launchagents",
                           "ai.mcs.llamaserver.plist")
        m = re.search(r"<string>-np</string><string>(\d+)</string>",
                      Path(src).read_text(encoding="utf-8"))
        plist_np = int(m.group(1)) if m else None
        manifest["llm_slots"] = {
            "selected": local_llm.SLOT_COUNT,
            "rollback": local_llm.ROLLBACK_SLOT_COUNT,
            "plist_np": plist_np,
            "consistent": plist_np == local_llm.SLOT_COUNT}
        if plist_np != local_llm.SLOT_COUNT:
            note(f"WARNING: plist -np {plist_np} != selected "
                 f"{local_llm.SLOT_COUNT} — deployment width drifts "
                 "from the measured selection")
    except Exception as e:
        # a manifest annotation must never take the services run down —
        # the agents/cron above were the real work
        note(f"manifest llm_slots: {type(e).__name__}: {e}")


def cmd_services(args) -> int:
    dry = getattr(args, "dry_run", False)
    subs = {"PYTHON": HERMES_PY, "REPO": REPO_ROOT,
            "DATA": os.path.join(HOME, "data")}
    problems = 0
    prev = _load_manifest()
    manifest = {"v": 1, "at": time.time(), "scripts": [],
                "agents": [], "cron": []}

    def note(msg):
        print(("  [dry] " if dry else "  ") + msg)

    _sync_scripts(subs, manifest, note, dry)
    problems += _sync_agents(subs, prev, manifest, note, dry)

    cfg = load_config()
    hermes = _hermes_exe(cfg)
    if not _hermes_ok(hermes):
        note(f"cron: hermes not resolvable ({hermes}) — skipped")
        problems += 1
    else:
        problems += _sync_cron(prev, hermes, manifest, note, dry)
        problems += _sync_gateway(cfg, hermes, note, dry)

    _record_llm_slots(manifest, note)
    # manifest — the rollback snapshot's source of truth (R6)
    if not dry and not problems:
        try:
            _save_manifest(manifest)
            note(f"manifest: {MANIFEST_PATH}")
        except OSError as e:
            note(f"manifest write failed: {e}")
            problems += 1
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="provision config/env/keychain — "
                                    "interactive run walks every setting")
    p.add_argument("--login-id")
    p.add_argument("--notify-target",
                   help="hermes send target for notifications "
                        "(e.g. slack, slack:#mcs, discord:1234)")
    p.add_argument("--set", action="append", metavar="KEY=JSON",
                   help="set any config key (repeatable; dotted keys "
                        "nest, values are JSON — e.g. "
                        "--set self_posts=true "
                        '--set notify.interactive=\'"discord"\')')
    p.add_argument("--semantic-mode", choices=["off", "shadow", "enforce"])
    p.add_argument("--project-ids", type=int, nargs="*")
    p.add_argument("--signals-notify", action=argparse.BooleanOptionalAction)
    p.add_argument("--self-org", dest="self_orgs", action="append",
                   metavar="NAME",
                   help="override own facility name — repeatable or "
                        "comma-separated; normally derived from MCS "
                        "/users/self automatically "
                        "(signals.self_organizations)")
    p.add_argument("--self-professions", metavar="A,B",
                   help="override responder professions, comma-"
                        "separated (signals.self_professions; "
                        "normally derived from MCS /users/self)")
    p.add_argument("--plugin-profile", metavar="NAME",
                   help="hermes profile serving Discord/Slack — plugin "
                        "settings are written there (multiplex; "
                        "empty/omitted = default profile)")
    p.add_argument("--plugin-user-ids", metavar="A,B",
                   help="user ids allowed to operate cards "
                        "(discord: allowed_user_ids / "
                        "slack: slack_allowed_user_ids)")
    p.add_argument("--plugin-chat-ids", metavar="A,B",
                   help="Discord channel ids that accept /mcs commands "
                        "(allowed_chat_ids; discord only — slack pins "
                        "a single channel via notify.slack.channel_id)")
    p.add_argument("--plugin-project-ids", metavar="A,B",
                   help="MCS project ids the plugin may read "
                        "(plugins ... settings.project_ids)")
    p.add_argument("--yes", action="store_true",
                   help="non-interactive — never prompt")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("fact-source",
                       help="set semantic.fact_source "
                            "(legacy|shadow|canonical)")
    p.add_argument("fact_source",
                   choices=["legacy", "shadow", "canonical"])
    p.add_argument("--gate-evidence",
                   help="semantic_evaluation report JSON proving the "
                        "human-labelled gate passed (canonical only)")
    p.set_defaults(fn=cmd_fact_source)
    p = sub.add_parser("jev-value",
                       help="run Jev incremental-value evaluation and "
                            "write the evidence report JSON")
    p.add_argument("--cases", required=True,
                   help="labelled bench cases JSON (synthetic corpus)")
    p.add_argument("--out", required=True, help="report output path")
    p.add_argument("--deadline", type=float, default=120.0,
                   help="evaluation deadline in seconds")
    p.set_defaults(fn=cmd_jev_value)
    p = sub.add_parser("services",
                       help="install launchd agents + hermes cron jobs "
                            "(renders deployment/ templates; idempotent)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the actions without applying them")
    p.set_defaults(fn=cmd_services, yes=True)
    p = sub.add_parser("check", help="validate required conditions")
    p.set_defaults(fn=cmd_check, yes=True)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
