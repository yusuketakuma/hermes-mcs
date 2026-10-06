"""MCS environment setup + required-condition validation.

`init` interactively (or via flags) provisions what a fresh machine
needs: ~/.mcs/config.json, ~/.mcs/.env secrets, and the macOS Keychain
entry the adapter reads at re-login (service 'mcs-adapter' — on the
Mac mini this is the same login keychain Chrome uses). Existing values
are merged, never silently overwritten. An interactive run walks EVERY
config key as a guided wizard (sectioned, defaults shown, Enter keeps
the current value); `--yes` runs flags only and `--set KEY=JSON` covers
any key non-interactively. With notify.interactive="slack"/"discord",
init also mirrors the plugin settings + bot token into hermes through
the public `hermes [-p profile] config set` CLI — never hermes-agent
internals.

`check` is the typesafe gate: every required key must exist with the
right type, optional keys are range-checked, the semantic block is
validated by the production `semantic_config` validator, and the
environment probes (Keychain entry, Chrome binary, local-LLM endpoint,
secret resolution, gateway supervision) report as warnings vs errors.
Exit 1 on any error so it can gate automation.

  python3 mcs/ops/mcs_setup.py init [--login-id ID ...]
  python3 mcs/ops/mcs_setup.py check    # blockers summarized last
  python3 mcs/ops/mcs_setup.py doctor   # local read-only counts by scope;
                                        # exit 1 only on a blocked scope

Secrets are never accepted as argv flags (they would persist in shell
history and `ps`). MCS / Jev and the existing Hermes integration keep
their environment or interactive inputs. Standalone connector tokens
use hidden prompts and private files; they are not inferred from the environment.
"""
from __future__ import annotations

import argparse
import fcntl
import stat
import getpass
import hashlib
from html import escape as xml_escape
import json
import math
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, suppress
from pathlib import Path
from typing import Literal, TypedDict

# mcs/ requires >=3.10 (runtime PEP-604 unions) — fail loudly before the
# first project import instead of a cryptic TypeError inside mcs_util.
if sys.version_info < (3, 10):
    raise SystemExit(
        "mcs_setup requires Python >= 3.10 — use the install-time "
        "interpreter ~/.hermes/hermes-agent/venv/bin/python "
        "(/usr/bin/python3 is too old)")

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

import bounded_http
import mcs_runtime

from mcs_util import (CONF_PATH, HOME, REPO, atomic_write, env_value,
                      load_config)

ENV_PATH = os.path.join(HOME, ".env")
KEYCHAIN_SERVICE = "mcs-adapter"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

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


def _drug_map_config(value):
    import drug_map
    return drug_map.config_error(value)


def _backup_config(value):
    keys = {"enabled", "policy", "snapshot", "snapshot_dir", "schedule",
            "verify_interval_s", "drill_interval_s"}
    if (not isinstance(value, dict) or set(value) - keys
            or type(value.get("enabled")) is not bool):
        return "must contain a boolean enabled and only supported backup keys"
    if not value["enabled"]:
        return None
    sources = [key for key in ("snapshot", "snapshot_dir") if key in value]
    if len(sources) != 1:
        return "exactly one of snapshot or snapshot_dir must be explicit"
    for key in ("policy", *sources):
        if not isinstance(value.get(key), str) or not os.path.isabs(value[key]):
            return f"{key} must be an explicit absolute path"
    for key in ("verify_interval_s", "drill_interval_s"):
        if type(value.get(key)) is not int or value[key] < 1:
            return f"{key} must be an explicit positive integer"
    schedule = value.get("schedule")
    match = re.fullmatch(
        r"([0-5]?[0-9])\s+((?:[01]?[0-9]|2[0-3])(?:,(?:[01]?[0-9]|2[0-3])){0,23})"
        r"\s+\*\s+\*\s+\*", schedule) if isinstance(schedule, str) else None
    if match is None:
        return "schedule must have one explicit minute and 1–24 distinct explicit hours"
    hours = [int(hour) for hour in match[2].split(",")]
    if len(hours) != len(set(hours)):
        return "schedule hours must be distinct"
    return None


def _recovery_python_problem(path, update_repo=None):
    """An independent runtime cannot live in either mutable checkout."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return "must be an explicit absolute interpreter path"
    try:
        for root in (REPO_ROOT, update_repo):
            if root is not None and Path(path).resolve().is_relative_to(Path(root).resolve()):
                return "recovery interpreter must be outside the mutable checkout"
    except (OSError, RuntimeError):
        return "recovery interpreter path unverifiable"
    return None


def _urgency_escalation(v):
    """Reuse notify_urgent.settings(): on/shadow with a policy it would
    silently drop (no explicit room_cooldown_min, bad type) is an error."""
    if not isinstance(v, dict):
        return "must be an object"
    mode = v.get("mode", "off")
    if mode not in ("off", "on", "shadow"):
        return 'mode: must be "off", "on" or "shadow"'
    if mode == "off":
        return None
    import notify_urgent
    if notify_urgent.settings({"urgency_escalation": v}) is None:
        return ("policy incomplete: source must be \"llm\", room_cooldown_min "
                "is required, after_min/repeat_min/room_cooldown_min must be "
                "positive numbers and max_repeats/max_per_day non-negative integers")
    return None


CONFIG_RULES = {
    "runtime_mode":          (False, lambda v: None if v in ("hermes", "standalone")
                             else 'must be "hermes" or "standalone"'),
    "recovery_python":       (False, _recovery_python_problem),
    "mcs_login_id":          (True,  _nonempty_str),
    "notify_target":         (True,  _nonempty_str),
    "notify_bot_profile":    (False, _bot_profile),
    "notify_system_target":  (False, _nonempty_str),
    "hermes_bin":            (False, _nonempty_str),
    "discover_archived":     (False, _bool),
    "deep_history":          (False, _bool),
    "trickle_pages":         (False, _int_range(1, 40)),
    "notify_max_age_h":      (False, _num),
    "notify_all_replies":    (False, _bool),
    "job_budget_seconds":    (False, _num),
    "self_posts":            (False, _bool),
    "metadata_shadow":       (False, _bool),
    "metadata_refresh_publish": (False, _bool),
    "metadata_actors":       (False, _bool),
    "notify":                (False, _dict),
    "signals":               (False, _dict),
    "semantic":              (False, _dict),
    "update":                (False, _dict),
    "local_llm":             (False, _dict),
    "health":                (False, _dict),
    "daily_digest":          (False, _dict),
    "drug_map":              (False, _drug_map_config),
    "backup":                (False, _backup_config),
    "urgency_escalation":    (False, _urgency_escalation),
    "watchdog_grace_s":      (False, lambda v: None if type(v) is int and 0 <= v <= 3600
                             else "must be an integer between 0 and 3600"),
}


def _validate_standalone(cfg: dict) -> list[str]:
    """runtime_mode=standalone: the grants Hermes plugin settings carry
    (allowed users, projects, Discord chats/roles) live in notify.<t>,
    and Slack/Discord targets must name a concrete channel."""
    if cfg.get("runtime_mode") != "standalone":
        return []
    errors = []
    for key in ("notify_target", "notify_system_target"):
        target = cfg.get(key)
        if not isinstance(target, str):
            continue
        if target == "local":  # Existing placeholder for an installation without notifications.
            continue
        transport, _, channel = target.strip().partition(":")
        if transport not in ("discord", "slack", "lineworks"):
            errors.append(f"{key}: standalone sends only to discord:, slack: "
                          "or lineworks: targets")
        elif transport == "discord" and not channel.isdecimal():
            errors.append(f"{key}: standalone needs discord:<channel id>")
        elif transport == "slack" and not re.fullmatch(r"[CGD][A-Z0-9]{8,}", channel):
            errors.append(f"{key}: standalone needs slack:<C/G/D conversation id> "
                          "(thread and user targets are Hermes-only)")
    return errors


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
            and ntf["interactive"] not in ("discord", "slack", "lineworks", "off"):
        errors.append('notify.interactive: must be "discord", '
                      '"slack", "lineworks" or "off"')
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
                              ("slack", "team_id"),
                              ("lineworks", "team_id")):
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
            if transport in ("slack", "lineworks") and "guild_id" in d:
                errors.append(f"notify.{transport}.guild_id: not allowed "
                              f"({transport} scope uses team_id)")
            if transport == "lineworks":
                for key in ("application_id", "team_id"):
                    v = d.get(key)
                    if not (isinstance(v, str) and re.fullmatch(r"[1-9][0-9]{0,18}", v)
                            and 0 < int(v) < 2**63):
                        errors.append(f"notify.lineworks.{key}: must be a positive decimal string")
                for key in ("channel_id", "profile"):
                    v = d.get(key)
                    pattern = r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}" if key == "channel_id" else r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
                    if not isinstance(v, str) or not re.fullmatch(pattern, v):
                        errors.append(f"notify.lineworks.{key}: invalid identifier")
                users = d.get("allowed_user_ids")
                if not (isinstance(users, list) and users
                        and all(isinstance(v, str) and re.fullmatch(
                            r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}", v) for v in users)):
                    errors.append("notify.lineworks.allowed_user_ids: non-empty identifier list required")
                names = d.get("user_names", {})
                if not (isinstance(names, dict) and all(
                        isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9._@-]{1,128}", k)
                        and isinstance(v, str) and 0 < len(v.strip()) <= 40
                        for k, v in names.items())):
                    errors.append("notify.lineworks.user_names: {userId: 表示名(40字以内)} required")
                automatic = d.get("project_ids_auto", False)
                if type(automatic) is not bool:
                    errors.append("notify.lineworks.project_ids_auto: must be a boolean")
                projects = d.get("project_ids", [])
                if not (isinstance(projects, list)
                        and all(type(v) is int and 0 < v < 2**63 for v in projects)
                        and (projects or automatic is True)):
                    errors.append("notify.lineworks.project_ids: positive integer list required (or project_ids_auto=true)")
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
    schedule settings require an int in [0,100])."""
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
        for key in ("digest", "self_reaction_response"):
            if key in sig and type(sig[key]) is not bool:
                errors.append(f"signals.{key}: must be a boolean")
        if "digest_interval_h" in sig:
            err = _num(sig["digest_interval_h"])
            if err:
                errors.append(f"signals.digest_interval_h: {err}")
        if "tiers" in sig and not isinstance(sig["tiers"], dict):
            errors.append("signals.tiers: must be an object")
    dd = cfg.get("daily_digest")
    if isinstance(dd, dict):
        if "enabled" in dd and type(dd["enabled"]) is not bool:
            errors.append("daily_digest.enabled: must be a boolean")
        if "hour_jst" in dd:
            err = _int_range(0, 23)(dd["hour_jst"])
            if err:
                errors.append(f"daily_digest.hour_jst: {err}")
        if "include_names" in dd and type(dd["include_names"]) is not bool:
            errors.append("daily_digest.include_names: must be a boolean")
        if "scope" in dd:
            import notify_digest
            flt = (notify_digest.parse_scope(dd["scope"])
                   if isinstance(dd["scope"], str) and len(dd["scope"]) <= 200
                   else "must be a string")
            if isinstance(flt, str) or flt["mine"]:
                errors.append("daily_digest.scope: " + (
                    flt if isinstance(flt, str) else
                    "mine needs a clicker — use station:/project:/days:"))
    if isinstance(cfg.get("notify"), dict):
        errors.extend(_validate_notify(cfg["notify"]))
        # a scope block for the transport that is NOT active is stale —
        # keep it as a warning so a discord->slack switch leaves a
        # visible note instead of a silently ignored config
        act = cfg["notify"].get("interactive")
        # standalone sends to notify_target/notify_system_target through
        # their own scope block (_validate_standalone_scope requires it)
        used = {t.split(":", 1)[0] for t in (
            cfg.get("notify_target"), cfg.get("notify_system_target"))
            if isinstance(t, str)} if cfg.get("runtime_mode") == "standalone" else set()
        for other in ("discord", "slack", "lineworks"):
            if other != act and other not in used and isinstance(cfg["notify"].get(other),
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
    if cfg.get("runtime_mode") == "standalone":
        errors.extend(_validate_standalone(cfg))
        errors.extend(_validate_standalone_scope(cfg))
    return errors, warnings


def _validate_standalone_scope(cfg: dict) -> list[str]:
    """Require explicit actor and project scopes for independent connectors."""
    errors = []
    ntf = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    targets = [cfg.get("notify_target"), cfg.get("notify_system_target")]
    active = {ntf.get("interactive")} if isinstance(ntf.get("interactive"), str) else set()
    active |= {
        value.split(":", 1)[0] for value in targets if isinstance(value, str)}
    for transport in ("slack", "discord"):
        if transport not in active:
            continue
        scope = ntf.get(transport)
        if not isinstance(scope, dict):
            errors.append(f"notify.{transport}: explicit scope required in standalone mode")
            continue
        users = scope.get("allowed_user_ids")
        if not (isinstance(users, list) and users and all(
                isinstance(uid, str) and uid.strip() for uid in users)):
            errors.append(f"notify.{transport}.allowed_user_ids: non-empty identifier list required")
        automatic = scope.get("project_ids_auto", False)
        if type(automatic) is not bool:
            errors.append(f"notify.{transport}.project_ids_auto: must be a boolean")
        projects = scope.get("project_ids", [])
        if not (isinstance(projects, list) and all(
                type(pid) is int and 0 < pid < 2**63 for pid in projects)
                and (projects or automatic is True)):
            errors.append(f"notify.{transport}.project_ids: positive integer list required (or project_ids_auto=true)")
        if transport == "discord" and "allowed_chat_ids" in scope:
            chats = scope["allowed_chat_ids"]
            if not (isinstance(chats, list) and chats and all(
                    isinstance(chat, str) and chat.isdecimal() and int(chat) > 0 for chat in chats)):
                errors.append("notify.discord.allowed_chat_ids: non-empty channel identifier list required")
        if transport == "discord" and "allowed_role_ids" in scope:
            roles = scope["allowed_role_ids"]
            if not (isinstance(roles, list) and all(isinstance(role, str)
                    and role.strip() and role != scope.get("guild_id") for role in roles)):
                errors.append("notify.discord.allowed_role_ids: identifier list required; @everyone is not allowed")
    return errors


def _hermes_exe(cfg: dict, path: str | None = None) -> str:
    """config hermes_bin -> PATH (or `path`) -> the standard user-local
    install."""
    exe = cfg.get("hermes_bin")
    if isinstance(exe, str) and exe.strip():
        return exe.strip()
    return shutil.which("hermes", os.F_OK | os.X_OK, path) \
        or os.path.expanduser("~/.local/bin/hermes")


def _hermes_ok(exe: str) -> bool:
    return os.path.isfile(exe) and os.access(exe, os.X_OK)


def _run(argv: list, *, timeout: int = 60, input_text: str | None = None,
         cwd: str | None = None):
    """subprocess.run bounded by `timeout` — a wedged launchd, a keychain
    prompt this context cannot show, or a stuck gateway must fail the
    check, not hang the session. Timeout yields a returncode=124 result
    (mirroring timeout(1)); a binary that cannot start yields 127. Both
    degrade through the caller's normal failure handling instead of
    raising."""
    try:
        return subprocess.run(argv, input=input_text, capture_output=True,
                              text=True, timeout=timeout, **({"cwd": cwd} if cwd else {}))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv, 124, "", f"timed out after {timeout}s")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


_GATEWAY_LIFECYCLE_VERBS = frozenset({"install", "uninstall", "restart", "start", "stop"})


def _hermes_cli(exe: str, profile: str, *argv: str, input_text: str | None = None):
    """Public hermes CLI against a named profile ("" = launch/default).
    Returns the CompletedProcess, or None when the call can't run."""
    cmd = [exe] + (["-p", profile] if profile else []) + list(argv)
    if (os.environ.get("MCS_TEST_SANDBOX") == "1" and argv[:1] == ("gateway",)
            and argv[1:2] and argv[1] in _GATEWAY_LIFECYCLE_VERBS
            and (os.path.exists(exe) or shutil.which(exe))):
        # hermes writes the gateway LaunchAgent under the real account home and
        # loads it into the real launchd, whatever HOME/HERMES_HOME a test set
        # (the live gateway plist was rewritten this way on 2026-10-04).
        return subprocess.CompletedProcess(
            cmd, 126, "", "refused under MCS_TEST_SANDBOX: gateway lifecycle")
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
    """Detect gateway-loaded plugin or adapter sources newer than the running process.

    Unparseable states fail
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
    newest = _gateway_sources_mtime()
    return newest is not None and newest > started


def _gateway_sources_mtime() -> float | None:
    """Newest gateway-loaded source timestamp; absent sources remain unknown."""
    newest = 0.0
    # __pycache__/*.pyc are regenerated BY the gateway at plugin load —
    # counting them makes the warning permanent. Only files the gateway
    # loads (Python sources and the plugin manifest) represent code the
    # running process hasn't picked up; a docs-only edit (README) never
    # needs a restart.
    for source in ("hermes_plugin", "adapters/common", "adapters/slack", "adapters/discord"):
        for base, dirs, files in os.walk(os.path.join(REPO_ROOT, source)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if not name.endswith((".py", ".yaml", ".yml")):
                    continue
                with suppress(OSError):
                    newest = max(newest, os.path.getmtime(
                        os.path.join(base, name)))
    return newest or None


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
    try:
        mcs_runtime.mode(cfg)
    except ValueError:
        return ["runtime_mode is invalid — select hermes or standalone before checking runtime services"], []
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
                        "unreadable — the keychain may be locked or access "
                        "may require an interactive permission prompt. "
                        + (".env fallback present — re-login still works "
                           "without Keychain access."
                           if env_pw else
                           "Check the login keychain lock and this process's "
                           "access permission in an interactive session, then "
                           "re-run check; or use the existing init password "
                           "input to configure the .env fallback."))
                else:
                    errors.append(
                        f"Keychain entry '{KEYCHAIN_SERVICE}' exists but its "
                        f"password cannot be read (rc={w.returncode}) — "
                        "re-register via `mcs_setup init`")
    if not os.path.exists(CHROME_BIN):
        errors.append(f"Chrome binary missing: {CHROME_BIN}")
    llm_errors, llm_warnings = _check_llm(cfg)
    errors.extend(llm_errors)
    warnings.extend(llm_warnings)
    profile = cfg.get("notify_bot_profile")
    if isinstance(profile, str) and profile \
            and not re.fullmatch(r"[a-z0-9_-]+", profile):
        errors.append("notify_bot_profile: must match [a-z0-9_-]+")
    # Delivery is `hermes send` — the binary must resolve the same way
    # notify_flush._hermes_exe does: config hermes_bin, else PATH, else the
    # standard user-local install (launchd PATH is minimal).
    ntf = cfg.get("notify") or {}
    exe = _hermes_exe(cfg) if cfg.get("runtime_mode") != "standalone" else ""
    lineworks = isinstance(ntf, dict) and ntf.get("interactive") == "lineworks"
    target = cfg.get("notify_target", "")
    if mcs_runtime.standalone(cfg):
        # Slack/Discord run on MCS's own connector — Hermes is not used.
        r = _run([mcs_runtime.python_executable(cfg),
                  os.path.join(REPO_ROOT, "mcs_standalone", "__main__.py"),
                  "check", "--root", HOME], timeout=30)
        if r.returncode:
            errors.append("standalone check failed — see "
                          "docs/guides/STANDALONE.md")
        if sys.platform == "darwin" and STANDALONE_LABEL in _agent_labels(cfg) \
                and _agent_loaded("ai.hermes.gateway"):
            warnings.append("ai.hermes.gateway is loaded while MCS serves "
                            "Slack/Discord itself — remove that platform from "
                            "Hermes or stop the gateway (docs/guides/STANDALONE.md)")
    elif lineworks or (isinstance(target, str) and target.startswith("lineworks:")):
        r = _run([sys.executable, os.path.join(REPO_ROOT, "lineworks_adapter", "__main__.py"),
                  "check", "--root", HOME], timeout=20)
        if r.returncode:
            errors.append("LINE WORKS adapter local check failed — run `python -m lineworks_adapter check` and follow docs/guides/LINEWORKS.md to repair the existing credentials")
    elif not _hermes_ok(exe):
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
    if sys.platform == "darwin" and cfg.get("runtime_mode") != "standalone":
        agents = os.path.expanduser("~/Library/LaunchAgents")
        labels = _agent_labels(cfg)
        warnings.extend(
            f"LaunchAgent {label} not installed — templates and "
            "install steps in deployment/launchagents/README.md"
            for label in labels
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
        for label in labels:
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


def _keychain_store(account: str, pw: str, *, service: str = KEYCHAIN_SERVICE) -> bool:
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
    existing = _run(["security", "find-generic-password", "-s", service])
    if existing.returncode == 0:
        m = re.search(r'^\s*"acct"<blob>="(.*)"\s*$', existing.stdout or "",
                      re.MULTILINE)
        if m:
            account = m.group(1)
    previous = _run(
        ["security", "find-generic-password", "-s", service,
         "-a", account, "-w"])
    if previous.returncode not in (0, 44):  # 44: no existing item
        return False
    old_password = previous.stdout.rstrip("\n") if previous.returncode == 0 else None
    if old_password is not None and any(c in old_password for c in "\r\n\x00"):
        return False
    cmd = (f"add-generic-password -s {_q(service)} "
           f"-a {_q(account)} -U -w {_q(pw)}\n")
    written = _run(["security", "-i"], input_text=cmd)
    chk = _run(
        ["security", "find-generic-password", "-s", service,
         "-w"])
    if chk.returncode == 0 and chk.stdout.rstrip("\n") == pw:
        return True
    # Preserve a credential that existed before this attempt. Only a
    # successfully created item with verified prior absence may be deleted.
    if old_password is not None:
        rollback = (f"add-generic-password -s {_q(service)} "
                    f"-a {_q(account)} -U -w {_q(old_password)}\n")
        _run(["security", "-i"], input_text=rollback)
    elif written.returncode == 0:
        _run(["security", "delete-generic-password", "-s",
              service, "-a", account])
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
    return _transport_on(cfg, "discord")


def _slack_on(cfg):
    return _transport_on(cfg, "slack")


def _transport_on(cfg, transport):
    return (cfg.get("notify") or {}).get("interactive") == transport or (
        cfg.get("runtime_mode") == "standalone" and any(
            isinstance(cfg.get(key), str) and cfg[key].startswith(transport + ":")
            for key in ("notify_target", "notify_system_target")))


def _lineworks_on(cfg):
    return (cfg.get("notify") or {}).get("interactive") == "lineworks"


def _cards_on(cfg):
    return (cfg.get("notify") or {}).get("interactive") \
        in ("discord", "slack", "lineworks")


def _signals_on(cfg):
    return (cfg.get("signals") or {}).get("notify") is True


def _digest_on(cfg):
    return (cfg.get("daily_digest") or {}).get("enabled") is True


def _semantic_on(cfg):
    m = (cfg.get("semantic") or {}).get("mode", "off")
    return m != "off"


WIZARD = [
    ("基本設定", [
        ("runtime_mode", "choice:hermes,standalone", "hermes",
         "実行モード — hermes=既存Hermes接続 / standalone=Hermesなしで全機能", None),
        ("mcs_login_id", "req", None,
         "MCS のログインID", None),
        ("notify_target", "req", None,
         "通知の送り先（例: slack:<チャンネルID>、"
         "discord:<チャンネルID>、lineworks:<トークルームID>）", None),
    ]),
    ("カード通知（Slack / Discord / LINE WORKS）", [
        ("notify.interactive", "choice:off,slack,discord,lineworks", "off",
         "通知形式 — slack/discord/lineworks=カード / off=従来テキストのみ", None),
        ("notify.lineworks.profile", "req", "default",
         "独自アダプターの配送プロファイル名", _lineworks_on),
        ("notify.lineworks.application_id", "req", None,
         "LINE WORKS Bot ID", _lineworks_on),
        ("notify.lineworks.team_id", "req", None,
         "LINE WORKS ドメインID", _lineworks_on),
        ("notify.lineworks.channel_id", "req", None,
         "配信先トークルームのチャンネルID", _lineworks_on),
        ("notify.lineworks.allowed_user_ids", "reqcsv", None,
         "操作を許可するユーザーID・カンマ区切り（必須）", _lineworks_on),
        ("notify.lineworks.project_ids", "reqintlist", None,
         "配信・操作を許可するMCSプロジェクトID・カンマ区切り（必須）", _lineworks_on),
        ("notify.lineworks.project_ids_auto", "bool", False,
         "公開済みscope snapshotからプロジェクトを解決（通常はオフ）", _lineworks_on),
        ("notify.slack.profile", "req", None,
         "配送スコープのプロファイル名", _slack_on),
        ("notify.slack.application_id", "req", None,
         "Slack アプリID", _slack_on),
        ("notify.slack.team_id", "req", None,
         "Slack ワークスペース（team）ID", _slack_on),
        ("notify.slack.channel_id", "req", None,
         "カードの投稿先チャンネルID", _slack_on),
        ("notify.slack.allowed_user_ids", "reqcsv", None,
         "操作を許可するSlackユーザーID・カンマ区切り", lambda cfg: cfg.get("runtime_mode") == "standalone" and _slack_on(cfg)),
        ("notify.slack.project_ids_auto", "bool", False,
         "公開済みscope snapshotからプロジェクトを解決", lambda cfg: cfg.get("runtime_mode") == "standalone" and _slack_on(cfg)),
        ("notify.slack.project_ids", "intlist", [],
         "閲覧・操作を許可するMCSプロジェクトID・カンマ区切り（自動解決がオフなら必須）", lambda cfg: cfg.get("runtime_mode") == "standalone" and _slack_on(cfg)),
        ("notify.discord.profile", "req", None,
         "配送スコープのプロファイル名", _discord_on),
        ("notify.discord.application_id", "req", None,
         "Discord アプリケーションID", _discord_on),
        ("notify.discord.guild_id", "req", None,
         "Discord サーバーID", _discord_on),
        ("notify.discord.channel_id", "req", None,
         "カードの投稿先チャンネルID", _discord_on),
        ("notify.discord.allowed_user_ids", "reqcsv", None,
         "操作を許可するDiscordユーザーID・カンマ区切り", lambda cfg: cfg.get("runtime_mode") == "standalone" and _discord_on(cfg)),
        ("notify.discord.allowed_role_ids", "csv", None,
         "操作を許可するDiscordロールID（@everyone不可・空欄可）", lambda cfg: cfg.get("runtime_mode") == "standalone" and _discord_on(cfg)),
        ("notify.discord.project_ids_auto", "bool", False,
         "公開済みscope snapshotからプロジェクトを解決", lambda cfg: cfg.get("runtime_mode") == "standalone" and _discord_on(cfg)),
        ("notify.discord.project_ids", "intlist", [],
         "閲覧・操作を許可するMCSプロジェクトID・カンマ区切り（自動解決がオフなら必須）", lambda cfg: cfg.get("runtime_mode") == "standalone" and _discord_on(cfg)),
        ("notify.operator", "opt", None,
         "運用者の Discord ユーザーID（空欄可）", _discord_on),
        ("notify.card_thread", "bool", True,
         "患者スレッドごとにカードをまとめる", lambda cfg: _slack_on(cfg) or _discord_on(cfg)),
        ("notify.card_thread_archive_min", "int", 10080,
         "カードスレッドをアーカイブするまでの分数", lambda cfg: _slack_on(cfg) or _discord_on(cfg)),
        ("notify.route_epoch", "int", 1,
         "配送先を変えたとき+1する番号（通常はそのまま）", _cards_on),
        ("notify_bot_profile", "opt", None,
         "通知投稿に使う hermes プロファイル（空欄=既定）", lambda cfg: cfg.get("runtime_mode") != "standalone"),
        ("notify_system_target", "opt", None,
         "障害・システム通知の送り先（空欄=notify_target と同じ）", None),
        ("notify_max_age_h", "num", None,
         "この時間より古い投稿は通知しない。期間内の新規取込みは既読でも通知（時間・空欄=制限なし）", None),
        ("notify_all_replies", "bool", False,
         "リアルタイムで新規取得した返信は既読・投稿時刻によらず全件通知（履歴一括取込みは対象外）", None),
        ("hermes_bin", "opt", None,
         "hermes コマンドのパス（空欄=自動検出）", lambda cfg: cfg.get("runtime_mode") != "standalone"),
        ("daily_digest.enabled", "bool", False,
         "朝の日次ダイジェスト（件数とID。患者名は include_names で追加）を notify_target に送る", None),
        ("daily_digest.hour_jst", "int", 8,
         "日次ダイジェストを送る時刻（JST・0-23時）", _digest_on),
        ("daily_digest.include_names", "bool", False,
         "日次ダイジェストの一覧に患者名を添える（送信先は notify_target）",
         _digest_on),
        ("daily_digest.scope", "opt", None,
         "日次ダイジェストの対象患者（例: station:○○ / project:1,2 / days:3。空欄=全患者）",
         _digest_on),
    ]),
    ("収集ポリシー", [
        ("self_posts", "bool", False,
         "自分自身の投稿も取り込んで通知する（未読APIは自投稿を返さない"
         "ため、最新probe経由で検出）", None),
        ("metadata_shadow", "bool", False,
         "未読保持の実証・運用合意後のみスタンプを再取得（shadow・反応値は非公開）",
         None),
        ("metadata_refresh_publish", "bool", False,
         "shadow再取得の成功結果を表示用の観測へ反映（未読保持と照合の確認後のみ）",
         None),
        ("metadata_actors", "bool", False,
         "スタンプを押した人の氏名・所属・職種を取得してスレッドに表示（無期限保持）",
         None),
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
    ("アラート（機械が要確認箇所を列挙）", [
        ("signals.notify", "bool", False,
         "アラートを通知カードに出す", None),
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


def _has_key(cfg: dict, dotted: str) -> bool:
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


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
    if kind in ("csv", "reqcsv", "intlist", "reqintlist"):
        parts = [x.strip() for x in raw.split(",") if x.strip()]
        if kind.startswith("req") and not parts:
            return False, "1件以上の値をカンマ区切りで入力してください"
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


def _ask(key: str, kind: str, default, desc: str, cur, present=False):
    """One wizard item. Returns the value to store, or _UNSET.
    `present`: the key exists in cfg (so cur=None is an explicit null)."""
    eff = cur if cur is not None else default
    # explicit null on a required item is a valid stored value (e.g.
    # semantic.project_ids: null = all projects) — Enter keeps it
    keep_null = present and cur is None and kind.startswith("req")
    marks = []
    if keep_null:
        marks.append("現在: null（全対象）")
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
            if keep_null:
                return None
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
                     _ask(key, kind, default, desc, _get_key(cfg, key),
                          _has_key(cfg, key)))


PLUGIN_SETTINGS = "plugins.entries.mcs-discord-commands.settings"


def _csv_yaml(text: str) -> str:
    """Comma-separated ids -> YAML list literal, so `config set` stores a
    real list (the plugin schema expects lists, not csv strings)."""
    return json.dumps([x.strip() for x in text.split(",") if x.strip()],
                      ensure_ascii=False)


def _project_ids_yaml(text: str) -> str | None:
    """project ids (csv or a JSON/YAML list) -> int list literal; the
    plugin rejects string ids. None when any id is not a positive int."""
    text = text.strip()
    if text.startswith("["):
        text = text.strip("[]")
    items = [x.strip().strip("'\"") for x in text.split(",") if x.strip()]
    if not items or not all(x.isdecimal() and int(x) > 0 for x in items):
        return None
    return json.dumps([int(x) for x in items])


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
    if mcs_runtime.standalone(cfg):
        interactive = ntf.get("interactive") if isinstance(ntf, dict) else None
        used = {interactive} | {
            str(cfg.get(k) or "").partition(":")[0]
            for k in ("notify_target", "notify_system_target")}
        wanted = [("DISCORD_BOT_TOKEN", "Discord bot token")] if "discord" in used else []
        if "slack" in used:
            wanted.append(("SLACK_BOT_TOKEN", "Slack bot token (xoxb-…)"))
        if interactive == "slack":
            wanted.append(("SLACK_APP_TOKEN", "Slack app-level token (xapp-…)"))
        # Credentials already provisioned by the native initializer need no dotenv copy.
        from mcs_standalone.config import credentials_path, load_credentials
        if used & {"slack", "discord"} and all(
                os.path.lexists(credentials_path(HOME, transport))
                for transport in used & {"slack", "discord"}):
            try:
                for transport in used & {"slack", "discord"}:
                    load_credentials(HOME, transport)
                return True
            except ValueError:
                return False
        updates, missing = {}, []
        for env_key, desc in wanted:
            if env_value(env_key, paths=[ENV_PATH], check_env=False):
                print(f"  {env_key}: 設定済み")
                continue
            tok = os.environ.get(env_key)
            hermes_tok = None if tok or args.yes else env_value(
                env_key, paths=[os.path.expanduser("~/.hermes/.env")], check_env=False)
            # switching from Hermes: reuse its token only on explicit consent
            if hermes_tok and input(f"  {env_key}: ~/.hermes/.env の値を使いますか？"
                                    "[y/N]: ").strip().lower() in ("y", "yes"):
                tok = hermes_tok
            if not tok and not args.yes:
                tok = getpass.getpass(f"  {desc}（{ENV_PATH} へ保存。空欄=スキップ）: ") or None
            if tok:
                updates[env_key] = tok
            else:
                missing.append(env_key)
        if updates:
            try:
                _env_write(ENV_PATH, updates)
            except ValueError:
                print("  .env: invalid token format; existing file retained")
                return False
            print(f"  {', '.join(sorted(updates))}: {ENV_PATH} へ保存 (0600)")
        if missing:
            print("  未設定: " + ", ".join(missing)
                  + f" — {ENV_PATH} に KEY=値 で追加するか init を再実行")
        return not missing
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
        if key == "project_ids":
            lit = _project_ids_yaml(flag_val)
            if lit is None:
                print(f"  settings.{key}: 正の整数の project ID を"
                      "カンマ区切りで指定してください")
                missing.append(key)
                failed = True
                continue
        else:
            lit = flag_val if flag_val.lstrip().startswith("[") \
                else _csv_yaml(flag_val)
        if _hermes_config_set(exe, profile, f"{PLUGIN_SETTINGS}.{key}",
                              lit):
            print(f"  settings.{key}: 設定")
        else:
            missing.append(key)
            failed = True
    # optional Discord role grant for card actions — written only when
    # given (no prompt: an empty role list is the normal setup)
    roles = getattr(args, "plugin_role_ids", None)
    if transport == "discord" and roles:
        lit = roles if roles.lstrip().startswith("[") else _csv_yaml(roles)
        try:
            role_ids = {str(r).strip() for r in json.loads(lit)}
        except (ValueError, TypeError):
            role_ids = set()
        if nd.get("guild_id") and str(nd["guild_id"]) in role_ids:
            # the guild id IS the @everyone role — every member would pass
            print("  settings.allowed_role_ids: guild_id（@everyone ロール）"
                  "は指定できません")
            missing.append("allowed_role_ids")
            failed = True
        elif _hermes_config_set(exe, profile,
                                f"{PLUGIN_SETTINGS}.allowed_role_ids", lit):
            print("  settings.allowed_role_ids: 設定")
        else:
            missing.append("allowed_role_ids")
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
    def _store(env_key, tok) -> bool:
        ok = _hermes_config_set(exe, profile, env_key, tok)
        print(f"  {env_key}: "
              + (f"hermes {'-p ' + profile if profile else '既定'} "
                 ".env へ保存" if ok else "保存失敗 — Hermes の config set --stdin 対応を確認してください"))
        return ok

    for env_key, desc in tokens:
        tok = os.environ.get(env_key)
        if tok:
            failed |= not _store(env_key, tok)
        elif _hermes_config_get(exe, profile, env_key):
            print(f"  {env_key}: 設定済み")
        else:
            if not args.yes:
                tok = getpass.getpass(
                    f"  {desc}（hermes .env へ保存。"
                    "空欄=スキップ）: ") or None
                if tok:
                    failed |= not _store(env_key, tok)
            if not tok:
                print(f"  {env_key}: 未設定 — `hermes "
                      + (f"-p {profile} " if profile else "")
                      + "setup` で後から設定")
    return not failed


def _config_problem() -> str | None:
    """None when config.json is absent or a JSON object, else why not.
    load_config() maps both cases to {} — a command that REWRITES the
    file must not treat a broken one as empty (it would silently drop
    every setting)."""
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as e:
        return f"{type(e).__name__}: {e}"
    return None if isinstance(raw, dict) else \
        f"top level is {type(raw).__name__}, not an object"


def _guard_config(yes: bool) -> bool:
    """False = stop, nothing written: config.json exists but is not a
    readable JSON object. With --yes it is moved aside to
    config.json.corrupt-<ts> (0600, bytes kept) and setup continues
    from defaults."""
    why = _config_problem()
    if why is None:
        return True
    if not yes:
        print(f"config: {CONF_PATH} is unreadable or invalid ({why}) — "
              "nothing written. Fix it by hand, or re-run `mcs_setup.py "
              "init --yes` to move it aside and start from defaults")
        return False
    base = f"{CONF_PATH}.corrupt-{time.strftime('%Y%m%dT%H%M%S')}"
    dst, n = base, 0
    while os.path.lexists(dst):
        n += 1
        dst = f"{base}.{n}"
    os.rename(CONF_PATH, dst)
    with suppress(OSError):
        os.chmod(dst, 0o600)
    print(f"config: {CONF_PATH} was unreadable or invalid ({why}) — "
          f"moved to {dst}; continuing from defaults")
    return True


def _init_recovery_problem(cfg) -> str | None:
    if "recovery_python" not in cfg:
        return None
    return _recovery_python_problem(cfg["recovery_python"]) or _runtime_problem(
        _runtime_probe(cfg["recovery_python"]), recovery=True)


def cmd_init(args) -> int:
    # --yes may archive a corrupt config: defer that write until the desired
    # interpreter (including --set overrides) has been verified.
    if not args.yes and not _guard_config(False):
        return 1
    cfg = load_config()
    try:
        mode_before = mcs_runtime.mode(cfg)
    except ValueError:
        mode_before = None  # an explicit --runtime-mode may repair it
    if getattr(args, "runtime_mode", None):
        cfg["runtime_mode"] = args.runtime_mode
    if getattr(args, "recovery_python", None) is not None:
        cfg["recovery_python"] = args.recovery_python
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

    # before any secret reaches Keychain/.env
    try:
        mcs_runtime.mode(cfg)
    except ValueError:
        print("init: runtime_mode must be 'hermes' or 'standalone' — nothing written")
        return 1
    why = _init_recovery_problem(cfg)
    if why:
        print("init: recovery_python " + why + " — nothing written")
        return 1
    checked_recovery = cfg.get("recovery_python")

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

    if mcs_runtime.standalone(cfg):
        # the --plugin-* grants Hermes keeps in plugin settings live in
        # notify.<transport> when MCS serves Slack/Discord itself
        ntf = cfg.get("notify")
        scope = ntf.get(ntf.get("interactive")) if isinstance(ntf, dict) else None
        ignored = ["--plugin-profile"] \
            if getattr(args, "plugin_profile", None) else []
        if isinstance(scope, dict):
            for flag, key in (("plugin_user_ids", "allowed_user_ids"),
                              ("plugin_chat_ids", "allowed_chat_ids"),
                              ("plugin_role_ids", "allowed_role_ids"),
                              ("plugin_project_ids", "project_ids")):
                value = getattr(args, flag, None)
                if value:
                    items = _csv_list([value])
                    scope[key] = [int(v) for v in items if v.isdecimal()] \
                        if key == "project_ids" else items
        else:
            ignored += ["--plugin-" + f[7:].replace("_", "-") for f in
                        ("plugin_user_ids", "plugin_chat_ids",
                         "plugin_role_ids", "plugin_project_ids")
                        if getattr(args, f, None)]
        if ignored:
            print(f"init: {', '.join(ignored)} は standalone では適用されません"
                  " — カード権限は config.json の notify.<transport> に設定してください")

    if not args.yes:
        _wizard(cfg)

    # re-checked: the wizard edits cfg after the secrets above were stored
    why = None if cfg.get("recovery_python") == checked_recovery else _init_recovery_problem(cfg)
    if why:
        print("init: recovery_python " + why + " — config.json not written")
        return 1
    if args.yes and not _guard_config(True):
        return 1

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
    if cfg.get("runtime_mode") == "standalone":
        for transport in ("slack", "discord"):
            if _transport_on(cfg, transport):
                argv = [sys.executable, "-m", "mcs_standalone", "init", "--root", HOME,
                        "--transport", transport] + (["--yes"] if args.yes else [])
                # Terminal-bound getpass owns credentials; no secret ever travels in argv.
                try:
                    result = subprocess.run(argv, timeout=300 if not args.yes else 30,
                                            cwd=REPO_ROOT)
                    integration_ok = integration_ok and result.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    integration_ok = False
    # an interactive transport needs the supervised gateway — on a
    # first install `services` ran BEFORE init could configure
    # interactive, so sync just the gateway here rather than leaving
    # a manual `services` rerun as the last step
    ntf = cfg.get("notify")
    gateway_result = 0
    if isinstance(ntf, dict) and (ntf.get("interactive") == "lineworks" or (
            mcs_runtime.standalone(cfg)
            and ntf.get("interactive") in ("discord", "slack"))) \
            and not _validate_notify(ntf):
        # Provision runner-owned directories/flags before the independent worker starts.
        import notify_cards
        notify_cards.ensure_dirs(os.path.join(HOME, "data"))
        notify_cards.publish_flags(cfg, os.path.join(HOME, "data"))
    if cfg.get("runtime_mode") != "standalone" and isinstance(ntf, dict) \
            and ntf.get("interactive") in ("discord", "slack"):
        exe = _hermes_exe(cfg)
        if _hermes_ok(exe):
            gateway_result = _sync_gateway(
                cfg, exe, lambda m: print(f"  {m}"), dry=False)
    if mcs_runtime.mode(cfg) != mode_before:
        # config alone does not switch runtimes: the interpreter, the
        # schedule and the Slack/Discord connection belong to install.sh
        print(f"init: runtime_mode is now {mcs_runtime.mode(cfg)} — run "
              f"`./install.sh --mode {mcs_runtime.mode(cfg)}` to install its "
              "runtime and move the scheduled jobs (docs/guides/STANDALONE.md)")
    check_result = cmd_check(args)
    if not integration_ok:
        if cfg.get("runtime_mode") == "standalone":
            print("init: FAIL — standalone credentials were not fully written; repair the connector and re-run init")
        else:
            print("init: FAIL — Hermes plugin settings were not fully "
                  "written; repair the serving profile/CLI and re-run init")
        return 1
    return 1 if gateway_result else check_result


def _shipped_g6_criteria() -> tuple[dict, str]:
    """The shipped G6 criteria, normalized and hashed exactly as
    semantic_evaluation embeds them in a report (criteria_sha256)."""
    import semantic_evaluation
    with open(G6_CRITERIA_PATH, encoding="utf-8") as f:
        criteria = semantic_evaluation.validate_criteria(json.load(f))
    return criteria, semantic_evaluation.criteria_sha256(criteria)


def cmd_fact_source(args) -> int:
    """Set semantic.fact_source with an evidence gate for ``canonical``.

    Production promotion is pinned: ``--gate-evidence`` must be a
    semantic_evaluation report scored against the shipped G6 criteria
    (criteria_sha256 match) whose gate passed with no reasons on at
    least min_human_labels human-labelled cases.  Synthetic, failing or
    foreign-criteria reports can never mint the token, and the written
    config is validated fail-closed before it lands.
    """
    if not _guard_config(False):
        return 1
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
    if not math.isfinite(args.deadline) or args.deadline <= 0:
        print("jev-value: deadline must be finite and positive")
        return 1
    try:
        with open(args.cases, "rb") as stream:
            raw = stream.read(8 * 1024 * 1024)
        payload = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        print("jev-value: cases unreadable")
        return 1
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        print("jev-value: cases file has no cases list")
        return 1
    try:
        allowed = {}
        for name in ("extract_cases.json", "extract_cases_labs.json",
                     "semantic_completeness_cases.json"):
            asset = Path(__file__).resolve().parents[2] / "evaluation" / name
            allowed.update((case["id"], case)
                           for case in json.loads(asset.read_text(encoding="utf-8"))["cases"])
        if any(not isinstance(case, dict) or not isinstance(case.get("id"), str)
               or case != allowed.get(case["id"]) for case in cases) \
                or len({case["id"] for case in cases}) != len(cases):
            raise ValueError("synthetic_cases_required")
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("jev-value: checked-in synthetic cases required")
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
               "drainers (ai.mcs.extract-drainer*) serve newest eligible first; "
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
        with closing(sqlite3.connect(
                Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
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
    # blocker priority: what every scheduled job runs on, then the
    # config itself, then machine probes, then deployment drift
    e0, w0 = check_runtime(cfg)
    why = _config_problem()
    if why:
        e0 = e0 + [f"{CONF_PATH} is unreadable or invalid ({why}) — "
                   "every setting reads as unset; fix it by hand or run "
                   "`mcs_setup.py init --yes` to move it aside"]
    e1, w1 = validate_config(cfg)
    e2, w2 = check_environment(cfg)
    e3, w3 = _script_drift() if cfg.get("runtime_mode", "hermes") in ("hermes", "standalone") else ([], [])
    errors = e0 + e1 + e2 + e3
    warnings = w0 + w1 + w2 + w3 + _queue_warnings(cfg)
    for w in warnings:
        print(f"  warn : {w}")
    for e in errors:
        print(f"  error: {e}")
    if errors:
        print(f"blockers ({len(errors)}) — fix in this order:")
        for i, e in enumerate(errors, 1):
            m = re.search(r"`([^`]+)`", e)
            fix = m.group(1) if m else (
                "mcs_setup.py init" if e in e1 else "see the error above")
            print(f"  {i}. {e.split(' — ')[0]}\n     fix: {fix}")
    print("check: " + ("FAIL" if errors else "OK")
          + f" ({len(errors)} errors, {len(warnings)} warnings)")
    return 1 if errors else 0


class DoctorCheck(TypedDict):
    status: Literal["healthy", "warning", "blocked", "not_checked"]
    errors: int
    warnings: int


def _doctor_check(errors: list[str], warnings: list[str]) -> DoctorCheck:
    """Discard arbitrary diagnostic text at the shared-output boundary."""
    return {"status": "blocked" if errors else "warning" if warnings else "healthy",
            "errors": len(errors), "warnings": len(warnings)}


def _check_llm(cfg: dict) -> tuple[list[str], list[str]]:
    """Probe only configured model metadata and slot availability, not inference."""
    import local_llm

    errors, warnings = [], []
    try:
        endpoint, _ = local_llm.resolve(cfg)
        models_url, slots_url = local_llm.probe_urls(endpoint)
        status, _, raw = local_llm.bounded_request(models_url, "GET", None, 3)
        if status != 200 or len(raw) > bounded_http.MAX_RESPONSE_BYTES:
            return [], ["local LLM models probe failed"]
    except (OSError, ValueError, RuntimeError):
        return [], ["local LLM models unavailable"]
    try:
        status, _, raw = local_llm.bounded_request(slots_url, "GET", None, 3)
        if len(raw) > bounded_http.MAX_RESPONSE_BYTES:
            return [], ["local LLM /slots unreadable — response too large"]
        slots = json.loads(raw.decode("utf-8"))
        if status != 200 or not isinstance(slots, list) \
                or not all(isinstance(slot, dict) for slot in slots):
            return [], ["local LLM /slots unreadable — mismatch cannot be verified"]
        if len(slots) < local_llm.SLOT_COUNT:
            errors.append(
                f"llama-server advertises {len(slots)} slots but the "
                f"selected count is {local_llm.SLOT_COUNT} — fix -np "
                "or lower SLOT_COUNT")
    except (OSError, ValueError, RuntimeError, RecursionError):
        warnings.append("local LLM /slots unreadable — mismatch cannot be verified")
    return errors, warnings


def sqlite_wal_safe(version: tuple[int, ...]) -> bool:
    """Official WAL-reset fixes, including the two maintained backports."""
    return version >= (3, 51, 3) or (
        version[:2] == (3, 50) and version >= (3, 50, 7)) or (
        version[:2] == (3, 44) and version >= (3, 44, 6))


class RuntimeFacts(TypedDict):
    python: str
    sqlite: str
    packages: dict[str, str | None]


def _runtime_probe(exe: str) -> RuntimeFacts | None:
    # Only stdlib and distribution metadata: never import SDK clients or MCS.
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        return None
    r = _run([exe, "-I", "-c", """
import sys, sqlite3, json
from importlib.metadata import version, PackageNotFoundError
packages = {}
for name in ('discord.py', 'slack-bolt', 'slack-sdk'):
    try:
        packages[name] = version(name)
    except PackageNotFoundError:
        packages[name] = None
print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])),
                  'sqlite': sqlite3.sqlite_version, 'packages': packages}))
"""], timeout=20)
    if r.returncode:
        return None
    try:
        data = json.loads(getattr(r, "stdout", ""))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not all(
            isinstance(data.get(key), str) and
            re.fullmatch(r"\d+\.\d+\.\d+", data[key])
            for key in ("python", "sqlite")):
        return None
    packages = data.get("packages")
    names = ("discord.py", "slack-bolt", "slack-sdk")
    if not isinstance(packages, dict) or set(packages) != set(names):
        return None
    if any(value is not None and (
            not isinstance(value, str) or
            re.fullmatch(r"\d+(?:\.\d+)+(?:[a-z]+\d+)?", value) is None)
           for value in packages.values()):
        return None
    return {"python": data["python"], "sqlite": data["sqlite"],
            "packages": packages}


def _runtime_problem(facts: RuntimeFacts | None, *, recovery: bool = False) -> str | None:
    if facts is None:
        return "runtime metadata unavailable"
    minimum = (3, 9) if recovery else (3, 10)
    if tuple(map(int, facts["python"].split("."))) < minimum:
        return "runtime Python version unsupported"
    if not sqlite_wal_safe(tuple(map(int, facts["sqlite"].split(".")))):
        return "runtime SQLite WAL-reset fix missing"
    return None


def _recovery_python(cfg) -> str:
    return cfg.get("recovery_python", "/usr/bin/python3")


def _recovery_executable(plist) -> str | None:
    if not isinstance(plist, dict):
        return None
    if "Program" in plist:
        exe = plist["Program"]
    else:
        argv = plist.get("ProgramArguments")
        exe = argv[0] if isinstance(argv, list) and argv else None
    return exe if isinstance(exe, str) and os.path.isabs(exe) else None


def _recovery_runtime(plist_path: str | Path | None = None) -> RuntimeFacts | None:
    # Inspect the deployed watchdog's command, not ambient /usr/bin/python3.
    try:
        with open(plist_path if plist_path is not None else
                  os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist"), "rb") as f:
            plist = plistlib.load(f)
        exe = _recovery_executable(plist)
        if exe is None or _recovery_python_problem(exe):
            return None
    except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
        return None
    return _runtime_probe(exe)


def _recovery_owned() -> bool:
    """Existing private tool, repo pointer and exact command prove ownership."""
    try:
        root = Path(RECOVERY_DIR)
        st = root.lstat()
        if (root.resolve() != root or not stat.S_ISDIR(st.st_mode)
                or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700):
            return False
        contents = {}
        for name, path, mode in (
                ("tool", root / "mcs_recover.py", 0o700),
                ("pointer", root / "repo_path", 0o600),
                ("plist", Path(AGENTS_DIR, f"{RECOVERY_LABEL}.plist"), 0o600)):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                st = os.fstat(stream.fileno())
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                        or stat.S_IMODE(st.st_mode) != mode or st.st_size > 1048576):
                    return False
                contents[name] = stream.read(1048577)
        if Path(contents["pointer"].decode().strip()).resolve() != Path(REPO_ROOT).resolve():
            return False
        plist = plistlib.loads(contents["plist"])
        if not isinstance(plist, dict):
            return False
        argv = plist.get("ProgramArguments")
        return (plist.get("Label") == RECOVERY_LABEL and isinstance(argv, list)
                and len(argv) == 3 and argv[1:] == [str(root / "mcs_recover.py"), "--if-stale"]
                and _recovery_executable(plist) is not None)
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def runtime_gate_errors(cfg, *, repair_recovery=False, update_repo=None) -> list[str]:
    """Refuse mutations on unsafe selected or explicitly deployed runtimes."""
    errors = []
    try:
        python = _service_subs(cfg)["PYTHON"]
    except ValueError as e:     # backup / watchdog_grace_s invalid
        errors.append(f"config: {e}")
    else:
        why = _runtime_problem(_runtime_probe(python))
        if why:
            errors.append("selected_runtime: " + why)
    # Absence is the existing optional-watchdog warning, not an invented
    # interpreter choice. A present but broken plist must fail closed.
    explicit = "recovery_python" in cfg
    desired = _recovery_python(cfg)
    desired_problem = None
    if explicit:
        desired_problem = _recovery_python_problem(desired, update_repo) or _runtime_problem(
            _runtime_probe(desired), recovery=True)
        if desired_problem:
            errors.append("desired_recovery_runtime: " + desired_problem)
    if os.path.lexists(os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")):
        why = _runtime_problem(_recovery_runtime(), recovery=True)
        try:
            with open(os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist"), "rb") as stream:
                actual = _recovery_executable(plistlib.load(stream))
        except (OSError, ValueError, TypeError):
            actual = None
        if actual is not None:
            why = _recovery_python_problem(actual, update_repo) or why
        repair = (repair_recovery and explicit and actual != desired
                  and not desired_problem and _recovery_owned())
        if explicit and actual != desired and not repair:
            errors.append("recovery_runtime: desired/deployed selection drift")
        if why and not repair:
            errors.append("recovery_runtime: " + why)
    return errors


def recovery_repair_pending(cfg) -> bool:
    """An explicit selection differs from the deployed watchdog command."""
    path = os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")
    if "recovery_python" not in cfg or not os.path.lexists(path):
        return False
    try:
        with open(path, "rb") as stream:
            return _recovery_executable(plistlib.load(stream)) != _recovery_python(cfg)
    except (OSError, ValueError, TypeError):
        return True


def _update_parent_holds_locks(data: Path) -> bool:
    """True only in the update's post-merge child: the parent apply keeps
    update.lock and run.lock, and spawned it with the journal's sha as
    handshake token (mcs_update._UPDATE_PM_ENV)."""
    token = os.environ.get("_MCS_UPDATE_PM")
    try:
        with open(data / "update_state.json", encoding="utf-8") as stream:
            applying = json.load(stream).get("applying")
    except (OSError, ValueError, AttributeError):
        return False
    return bool(token) and isinstance(applying, dict) and applying.get("sha") == token


def _sync_recovery(cfg, note, dry) -> int:
    """Change only an owned, quiescent installed watchdog; never enable a missing one."""
    path = Path(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")
    if "recovery_python" not in cfg or not os.path.lexists(path):
        return 0
    desired = _recovery_python(cfg)
    with path.open("rb") as stream:
        actual = _recovery_executable(plistlib.load(stream))
    if actual == desired:
        return 0
    if sys.platform != "darwin" or not _recovery_owned():
        note("recovery: ownership or platform unverifiable — not replaced")
        return 1
    try:
        text = Path(REPO_ROOT, "deployment/launchagents",
                    f"{RECOVERY_LABEL}.plist").read_text()
        body = _render_template(text, {key: xml_escape(value, quote=False) for key, value in {
            "RECOVERY_PYTHON": desired, "RECOVERY": RECOVERY_DIR,
            "DATA": str(Path(HOME, "data"))}.items()})
        if _recovery_executable(plistlib.loads(body.encode())) != desired:
            note("recovery: template differs from desired executable — not replaced")
            return 1
    except (OSError, ValueError, TypeError):
        note("recovery: template unverifiable — not replaced")
        return 1
    uid = os.getuid()
    target = f"gui/{uid}/{RECOVERY_LABEL}"
    current = _run(["launchctl", "print", target], timeout=10)
    loaded = current.returncode == 0
    if (loaded and (re.search(r"^\s*pid\s*=", current.stdout or "", re.M)
                    or not re.search(r"^\s*state = (?:not running|waiting)\s*$",
                                     current.stdout or "", re.M))) \
            or (not loaded and current.returncode != 113):
        note("recovery: active or state unknown — not replaced")
        return 1
    note("recovery: owned idle interpreter selection drift")
    if dry:
        return 0
    data = Path(HOME, "data")
    try:
        st = data.lstat()
    except OSError:
        note("recovery: data lock directory unavailable — not replaced")
        return 1
    if (data.resolve() != data or not stat.S_ISDIR(st.st_mode)
            or st.st_uid != uid or stat.S_IMODE(st.st_mode) != 0o700):
        note("recovery: data lock directory unsafe — not replaced")
        return 1
    fds = []
    parent_held = _update_parent_holds_locks(data)
    try:
        for name in ("update.lock", "run.lock"):
            fd = os.open(data / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600)
            fds.append(fd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
                note("recovery: lock ownership unknown — not replaced")
                return 1
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not parent_held:
                    raise
        if loaded:
            if _run(["launchctl", "bootout", target]).returncode != 0:
                return 1
            if _run(["launchctl", "print", target], timeout=10).returncode != 113:
                note("recovery: unload unverifiable — plist unchanged")
                return 1
        _write_atomic(str(path), body, 0o600)
        if loaded and _run(["launchctl", "bootstrap", f"gui/{uid}", str(path)]).returncode != 0:
            note("recovery: safe desired plist installed but bootstrap failed — no fallback")
            return 1
        if loaded and _run(["launchctl", "print", target], timeout=10).returncode != 0:
            note("recovery: desired watchdog load unverifiable — no fallback")
            return 1
        return 0
    except (OSError, BlockingIOError):
        note("recovery: lock busy or operation failed — not replaced")
        return 1
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _doctor_source(cfg: dict, *, probe: bool) -> dict:
    """Report disk revision separately from observed process start evidence."""
    revision, dirty = None, None
    try:
        r = subprocess.run(["git", "-C", REPO_ROOT, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        value = (r.stdout or "").strip()
        if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value):
            revision = value
            r = subprocess.run(["git", "-C", REPO_ROOT, "diff-index", "--quiet", "HEAD"],
                               capture_output=True, timeout=5)
            dirty = bool(r.returncode) if r.returncode in (0, 1) else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    running = {"status": "not_checked", "pid": None, "started_at": None,
               "source_revision": None, "matches_disk": None}
    if probe and cfg.get("runtime_mode") != "standalone" and sys.platform == "darwin":
        running["status"] = "unknown"
        pids = set()
        for domain in ("user", "gui"):
            r = _run(["launchctl", "print", f"{domain}/{os.getuid()}/ai.hermes.gateway"],
                     timeout=10)
            match = re.search(r"^\s*pid = (\d+)$", getattr(r, "stdout", "") or "", re.M)
            if r.returncode == 0 and match and int(match.group(1)) > 0:
                pids.add(int(match.group(1)))
        if len(pids) == 1:
            running["pid"] = pids.pop()
            r = _run(["ps", "-o", "lstart=", "-p", str(running["pid"])], timeout=10)
            if r.returncode == 0:
                try:
                    running["started_at"] = time.mktime(time.strptime(
                        re.sub(r"\s+", " ", (getattr(r, "stdout", "") or "").strip()),
                        "%a %b %d %H:%M:%S %Y"))
                except (ValueError, OverflowError):
                    pass
            # A PID/start time proves liveness, never the SHA of loaded code.
            running["status"] = "running_revision_unknown"
    modified = _gateway_sources_mtime()
    if type(modified) not in (int, float) or not math.isfinite(modified) or modified < 0:
        modified = None
    started = running["started_at"]
    if type(started) not in (int, float) or not math.isfinite(started) or started < 0:
        running["started_at"] = started = None
    running["restart_required"] = (modified > started
                                   if modified is not None and started is not None else None)
    running["restart_inference"] = ("source_timestamp_vs_process_start"
                                     if running["restart_required"] is not None else None)
    restart = {"status": "not_checked" if not probe else "unknown"}
    if probe:
        try:
            with open(os.path.join(HOME, "data", "gateway_restart.json"), "rb") as stream:
                raw = stream.read(8193)
            value = json.loads(raw) if len(raw) <= 8192 else None
            statuses = {"requested", "supervisor_restart_verified", "failed", "unknown"}
            if isinstance(value, dict) and value.get("status") in statuses \
                    and type(value.get("at")) in (int, float) \
                    and math.isfinite(value["at"]) and 0 <= time.time() - value["at"] <= 3600:
                status = value["status"]
                if status == "supervisor_restart_verified" and value.get("pid") != running["pid"]:
                    status = "stale"
                restart = {"status": status}
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            pass
    return {"disk": {"revision": revision, "dirty": dirty,
                     "gateway_sources_modified_at": modified}, "gateway": running,
            "restart_request": restart}


def cmd_doctor(args) -> int:
    """Local read-only diagnostics; explicit probes return only safe counts."""
    cfg = load_config()
    config_errors, config_warnings = validate_config(cfg)
    if _config_problem():
        config_errors.append("configuration unreadable")
    checks: dict[str, DoctorCheck] = {
        "configuration": _doctor_check(config_errors, config_warnings),
        "interpreter": _doctor_check(
            [] if sqlite_wal_safe(sqlite3.sqlite_version_info)
            else ["SQLite WAL-reset fix missing"], []),
    }
    selected = None
    recovery = None
    checks["recovery_runtime"] = {"status": "not_checked", "errors": 0, "warnings": 0}
    try:
        mcs_runtime.mode(cfg)
    except ValueError:
        mode = "invalid"
        checks["runtime"] = _doctor_check(["runtime mode invalid"], [])
    else:
        mode = mcs_runtime.mode(cfg)
        selected = _runtime_probe(_services_py(cfg))
        why = _runtime_problem(selected)
        checks["runtime"] = _doctor_check([why] if why else [], [])
        if sys.platform == "darwin" and os.path.isfile(
                os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")):
            recovery = _recovery_runtime()
            why = _runtime_problem(recovery, recovery=True)
            checks["recovery_runtime"] = _doctor_check([why] if why else [], [])
    probes = frozenset(getattr(args, "probe", None) or [])
    for scope in ("llm", "services", "credentials", "data"):
        checks[scope] = {"status": "not_checked", "errors": 0, "warnings": 0}
    if not config_errors and mode != "invalid":
        if "llm" in probes:
            checks["llm"] = _doctor_check(*_check_llm(cfg))
        if "services" in probes:
            if sys.platform == "darwin":
                domain = _run(["launchctl", "print", f"gui/{os.getuid()}"],
                              timeout=10)
                if domain.returncode:
                    checks["services"]["warnings"] = 1
                else:
                    checks["services"] = _doctor_check(*check_runtime(cfg))
            elif mcs_runtime.standalone(cfg):
                checks["services"] = _doctor_check(*check_runtime(cfg))
    # No paths, values, messages from validators/subprocesses, IDs or raw
    # responses cross this boundary. Only process IDs/source hashes are exposed
    # by the explicit source probe; application/patient IDs stay private.
    # Unchecked scopes never imply health.
    desired = None
    matches = None
    if not config_errors and "recovery_python" in cfg:
        desired = _runtime_probe(_recovery_python(cfg))
        why = _runtime_problem(desired, recovery=True)
        checks["desired_recovery_runtime"] = _doctor_check([why] if why else [], [])
        if os.path.lexists(os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")):
            try:
                with open(os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist"), "rb") as stream:
                    matches = _recovery_executable(plistlib.load(stream)) == _recovery_python(cfg)
            except (OSError, ValueError, TypeError):
                matches = False
            checks["recovery_selection"] = _doctor_check(
                ["desired/deployed recovery selection drift"] if not matches else [], [])
    runtimes: dict[str, RuntimeFacts | None] = {
        "selected": selected, "recovery": recovery, "desired_recovery": desired}
    report = {"mode": mode, "python": sys.version.split()[0],
              "sqlite": sqlite3.sqlite_version, "checks": checks,
              "runtimes": runtimes,
              "source": _doctor_source(cfg, probe="services" in probes and not config_errors),
              "recovery_selection": {"explicit": "recovery_python" in cfg,
                                     "matches_deployed": matches}}
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"doctor: mode={mode}, Python={report['python']}, SQLite={report['sqlite']}")
        source = report["source"]
        print(f"  disk revision: {source['disk']['revision'] or 'unknown'}, "
              f"dirty={source['disk']['dirty']}")
        print(f"  gateway source: {source['gateway']['status']}, "
              f"PID={source['gateway']['pid']}, start={source['gateway']['started_at']}, "
              f"loaded revision={source['gateway']['source_revision'] or 'unknown'}")
        print(f"  gateway restart request: {source['restart_request']['status']} "
              "(supervisor PID evidence only; application health is separate)")
        for role, facts in runtimes.items():
            if facts is not None:
                print(f"  {role}: Python={facts['python']}, SQLite={facts['sqlite']}, "
                      f"SDK metadata={json.dumps(facts['packages'])}")
        for scope, check in checks.items():
            print(f"  {scope}: {check['status']} "
                  f"({check['errors']} errors, {check['warnings']} warnings)")
        print("configuration: mcs setup check (fix steps); runtime/SQLite: review mcs install "
              "(SQLite >=3.51.3, 3.50.7 or 3.44.6); "
              "unchecked probes: --probe llm / --probe services")
    return 1 if any(check["status"] == "blocked" for check in checks.values()) else 0


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
REPO_ROOT = REPO
G6_CRITERIA_PATH = os.path.join(REPO_ROOT, "evaluation",
                                "g6-criteria-v1.json")

CRON_JOBS = [
    ("MCS unread check", "*/10 * * * *", "mcs_check.sh"),
    ("MCS health watch", "*/5 * * * *", "mcs_health.sh"),
    ("MCS durable drain", "10,40 * * * *", "mcs_deep.sh"),
    ("MCS retry maintenance", "0 */6 * * *", "mcs_llm_catchup.sh"),
    ("llamacpp daily restart", "0 4 * * *", "llamacpp_restart_if_idle.sh"),
    ("MCS update check", "10 5 * * *", "mcs_update.sh"),
]


def configured_cron_jobs(cfg: dict) -> list[tuple[str, str, str]]:
    """Keep default jobs and append only an explicitly configured offsite schedule."""
    jobs = list(CRON_JOBS)
    backup = cfg.get("backup", {"enabled": False})
    if _backup_config(backup):
        raise ValueError("backup_config_invalid")
    if backup["enabled"]:
        jobs.append(("MCS offsite backup", backup["schedule"], "mcs_offsite.sh"))
    return jobs


# RESIDENT = KeepAlive drainers the updater quiesces/restarts itself;
# WATCHER = WatchPaths triggers — verified loaded, never keep-alive.
RESIDENT_LABELS = ["ai.mcs.extract-drainer", "ai.mcs.extract-drainer-2"]
WATCHER_LABELS = ["local.mcs-cmd", "local.mcs-int"]
AGENT_LABELS = WATCHER_LABELS + RESIDENT_LABELS
# install.sh-owned labels that must never enter MCS ownership (S5).
EXCLUDED_LABELS = frozenset({"ai.mcs.llamaserver", "org.mcs.recovery"})
MANIFEST_PATH = os.path.join(HOME, "data", "service_manifest.json")
# the PATH every cron wrapper exports (deployment/scripts/*.sh) — launchd
# and hermes cron never see the login shell's PATH
LAUNCHD_PATH = os.pathsep.join([os.path.expanduser("~/.local/bin"),
                                "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
RECOVERY_DIR = os.path.expanduser("~/.mcs-recovery")
RECOVERY_LABEL = "org.mcs.recovery"
# whichever is loaded serves :8080 (see llamacpp_restart_if_idle.sh)
LLAMA_LABELS = ("ai.hermes.llamacpp", "ai.mcs.llamaserver")
STANDALONE_LABEL = "ai.mcs.standalone"
STANDALONE_UNIT = "mcs-standalone.service"


CRON_LABEL_PREFIX = "ai.mcs.cron."


def _services_py(cfg: dict) -> str:
    """The interpreter every rendered wrapper and agent runs on."""
    return mcs_runtime.python_executable(cfg) if mcs_runtime.standalone(cfg) \
        else HERMES_PY


def _hermes_py_problem() -> str | None:
    """None when HERMES_PY is an executable Python >= 3.10."""
    return _py_problem(HERMES_PY)


def _py_problem(exe: str) -> str | None:
    """None when `exe` is an executable Python >= 3.10."""
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        return f"interpreter {exe} is missing or not executable"
    r = _run([exe, "-c",
              "import sys; sys.exit(sys.version_info < (3, 10))"],
             timeout=20)
    if r.returncode != 0:
        return (f"interpreter {exe} is not a working Python >= 3.10 "
                f"(rc={r.returncode})")
    # Inspect the actual service interpreter's linked SQLite, not the
    # version of a sqlite3 executable or a different Python on PATH.
    r = _run([exe, "-c",
              "import sqlite3,sys; v=sqlite3.sqlite_version_info; "
              "sys.exit(0 if v >= (3,51,3) or "
              "(v[:2] == (3,50) and v >= (3,50,7)) or "
              "(v[:2] == (3,44) and v >= (3,44,6)) else 1)"],
             timeout=20)
    if r.returncode:
        return "service interpreter lacks the SQLite WAL-reset fix (need >=3.51.3, 3.50.7 or 3.44.6)"
    return None


def _cron_label(script: str) -> str:
    return CRON_LABEL_PREFIX + script.removesuffix(".sh").replace("_", "-")


def _calendar(schedule: str) -> list[dict]:
    """launchd StartCalendarInterval entries for a CRON_JOBS schedule —
    launchd has no `*/n`, so minute/hour lists are expanded."""
    minute, hour, *rest = schedule.split()
    if rest != ["*", "*", "*"]:
        raise ValueError(f"unsupported schedule {schedule!r}")

    def expand(field, size):
        if field == "*":
            return [None]
        if field.startswith("*/"):
            return list(range(0, size, int(field[2:])))
        return [int(v) for v in field.split(",")]
    return [{k: v for k, v in (("Hour", h), ("Minute", m)) if v is not None}
            for h in expand(hour, 24) for m in expand(minute, 60)]


CRON_TIMEOUT_S = 3600      # = hermes cron's script timeout
_TIMEOUT_PL = ("my $t = shift; my $p = 0;"
               # launchd's SIGTERM (bootout, logout) must reach the job too
               " $SIG{$_} = sub { kill 'TERM', -$p if $p; exit 143 } for qw(TERM INT HUP);"
               " $p = fork; die unless defined $p;"
               " if (!$p) { setpgrp(0, 0); exec @ARGV; exit 127 }"
               " $SIG{ALRM} = sub { kill 'TERM', -$p; sleep 5; kill 'KILL', -$p; exit 124 };"
               " alarm $t; waitpid($p, 0); exit($? >> 8)")


def _cron_plist(label: str, script: str, schedule: str, cfg: dict) -> str:
    """Standalone replacement for one `hermes cron` job (--deliver local:
    output only goes to a log). launchd never starts a second instance
    of a job that is still running."""
    log = os.path.join(HOME, "data", "cron.log")
    return plistlib.dumps({
        "Label": label,
        # hermes cron killed a script after 3600s; launchd has no timeout and
        # never starts a job that is still running, so one wedged tick would
        # stop the job for good. The cap kills the wrapper's process group.
        "ProgramArguments": ["/usr/bin/perl", "-e", _TIMEOUT_PL, str(CRON_TIMEOUT_S),
                             "/bin/bash", os.path.join(_scripts_dir(cfg), script)],
        "StartCalendarInterval": _calendar(schedule),
        # the CDP Chrome a collection tick starts must outlive the tick
        # (hermes cron never killed it); launchd would reap the group
        "AbandonProcessGroup": True,
        "StandardOutPath": log, "StandardErrorPath": log,
    }).decode("utf-8")


def _agent_labels(cfg: dict) -> list[str]:
    """Every LaunchAgent `services` owns for the selected runtime."""
    if not mcs_runtime.standalone(cfg):
        return list(AGENT_LABELS)
    return [STANDALONE_LABEL]


def _owned_label(label: str) -> bool:
    """MCS's LaunchAgent naming space (never install.sh-owned labels)."""
    return label not in EXCLUDED_LABELS and (
        label.startswith(("local.mcs-", "ai.mcs.extract-", CRON_LABEL_PREFIX))
        or label == STANDALONE_LABEL)


def _install_fix(stage: str) -> str:
    return f"re-run `{os.path.join(REPO_ROOT, 'install.sh')}` ({stage})"


def _standalone_py_problem(cfg: dict) -> str | None:
    return _py_problem(mcs_runtime.python_executable(cfg, root=HOME))


def check_runtime(cfg: dict) -> tuple[list[str], list[str]]:
    """What the scheduled jobs run on: the services interpreter, hermes
    as launchd resolves it, the update-recovery watchdog and the
    llama-server agent."""
    errors, warnings = [], []
    try:
        mcs_runtime.mode(cfg)
    except ValueError:
        return errors, warnings
    standalone = cfg.get("runtime_mode") == "standalone"
    why = _standalone_py_problem(cfg) if standalone else _hermes_py_problem()
    if why:
        errors.append(f"{why} — cron wrappers and launchd agents cannot "
                      "start; " + _install_fix(
                          "stage 2 rebuilds the standalone venv" if standalone else "stage 2 rebuilds the hermes-agent venv")
                      + ", then `mcs_setup.py services`")
    exe = _hermes_exe(cfg) if not standalone else ""
    if not standalone and _hermes_ok(exe) and not _hermes_ok(_hermes_exe(cfg, LAUNCHD_PATH)):
        errors.append(
            f"hermes resolves here ({exe}) but not on the launchd PATH "
            f"({LAUNCHD_PATH}) — scheduled jobs cannot send; "
            f"`ln -s {shlex.quote(exe)} ~/.local/bin/hermes` (or "
            "init --set hermes_bin=...)")
    if standalone:
        path = _standalone_service_path()
        if not os.path.exists(path):
            warnings.append("standalone supervised service not installed — run `mcs_setup.py services` or run `python -m mcs_standalone run` in a supervisor")
        elif not _standalone_loaded():
            errors.append("standalone service installed but not loaded — run `mcs_setup.py services`")
        else:
            try:
                from mcs_standalone.service import render
                _, desired = render(HOME, platform=sys.platform)
                if Path(path).read_text(encoding="utf-8") != desired:
                    errors.append("standalone service differs from current source — run `mcs_setup.py services`")
            except (OSError, ValueError):
                errors.append("standalone service content unverifiable")
    if sys.platform != "darwin":
        return errors, warnings
    uid = os.getuid()
    if _run(["launchctl", "print", f"gui/{uid}"],
            timeout=10).returncode != 0:
        return errors, warnings   # check_environment already warns
    tool = os.path.join(RECOVERY_DIR, "mcs_recover.py")
    plist = os.path.join(AGENTS_DIR, f"{RECOVERY_LABEL}.plist")
    fix = _install_fix("stage 6 installs the recovery tool + watchdog")
    try:
        deployed = Path(tool).read_bytes()
    except OSError:
        deployed = None
    if deployed is None or not os.path.exists(plist):
        warnings.append(f"update recovery watchdog ({RECOVERY_LABEL}) not "
                        f"installed — a broken update cannot self-heal; "
                        + fix)
    else:
        why = _runtime_problem(_recovery_runtime(), recovery=True)
        if why:
            errors.append(f"recovery watchdog {why} — restore-time writes/checkpoints "
                          "are not verified safe; " + fix)
        with suppress(OSError):
            if deployed != Path(REPO_ROOT, "deployment", "recovery",
                                "mcs_recover.py").read_bytes():
                warnings.append(f"{tool} differs from the repo copy — "
                                + fix)
        try:
            recorded = Path(RECOVERY_DIR, "repo_path").read_text(
                encoding="utf-8").strip()
        except OSError:
            recorded = HOME   # mcs_recover.py's own fallback
        if os.path.realpath(recorded) != os.path.realpath(REPO_ROOT):
            warnings.append(f"recovery watchdog recovers {recorded}, not "
                            f"this checkout {REPO_ROOT} — " + fix)
        if not _agent_loaded(RECOVERY_LABEL):
            errors.append(f"LaunchAgent {RECOVERY_LABEL} installed but not "
                          f"loaded — `launchctl bootstrap gui/{uid} "
                          f"{plist}`")
    if not any(_agent_loaded(lb) for lb in LLAMA_LABELS):
        installed = [lb for lb in LLAMA_LABELS if os.path.exists(
            os.path.join(AGENTS_DIR, f"{lb}.plist"))]
        if installed:
            lp = os.path.join(AGENTS_DIR, f"{installed[0]}.plist")
            errors.append(f"LaunchAgent {installed[0]} installed but not "
                          "loaded — the local LLM is down; "
                          f"`launchctl bootstrap gui/{uid} {lp}`")
        else:
            warnings.append(
                "no llama-server LaunchAgent (" + " / ".join(LLAMA_LABELS)
                + ") — fine for a self-managed server on local_llm.url; "
                "otherwise " + _install_fix("stage 4"))
    return errors, warnings


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
    # Hermes >= 2026-09-28 prints "No scheduled jobs in profile '<name>'."
    if not out and not re.search(r"^No scheduled jobs(?: in profile '[^'\n]*')?\.",
                                 output, re.M):
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
    if os.environ.get("XPC_SERVICE_NAME") == label:
        # bootout would kill this very process (the updater running as
        # ai.mcs.cron.mcs-update): a detached helper reloads the job once
        # this run has ended (the job abandons its process group).
        note(f"agent {label}: reload after this run exits")
        if not dry:
            target = f"gui/{uid}/{label}"
            script = ('while kill -0 "$1" 2>/dev/null; do sleep 5; done; '
                      'launchctl bootout "$2"; launchctl bootstrap "$3" "$4"')
            env = {k: v for k, v in os.environ.items()
                   if k not in ("XPC_SERVICE_NAME", "MCS_JOB_PID")}
            waiter = os.environ.get("MCS_JOB_PID") or str(os.getpid())
            subprocess.Popen(["/bin/sh", "-c", script, "mcs-reload", waiter,
                              target, f"gui/{uid}", dst],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env, close_fds=True,
                             start_new_session=True)
        return True
    if _agent_loaded(label):
        note(f"agent {label}: reload (content changed)")
        if not dry:
            _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
    else:
        note(f"agent {label}: bootstrap")
    if dry:
        return True
    # imported here, not at module top: an older-generation mcs_update
    # (mid update/rollback) lazily imports this module against its own
    # already-loaded mcs_util, which may predate launchd_bootstrap
    from mcs_util import launchd_bootstrap
    err = launchd_bootstrap(label, dst, _run)
    if err:
        note(f"  bootstrap failed: {err}")
    return err is None


def _load_manifest() -> dict:
    return load_config(MANIFEST_PATH)


def _save_manifest(manifest: dict) -> None:
    _write_atomic(MANIFEST_PATH,
                  json.dumps(manifest, ensure_ascii=False,
                             indent=1, sort_keys=True))


def _service_subs(cfg=None) -> dict:
    cfg = load_config() if cfg is None else cfg
    python = mcs_runtime.python_executable(cfg, root=HOME) if cfg.get("runtime_mode") == "standalone" else HERMES_PY
    subs = {"PYTHON": python, "REPO": REPO_ROOT,
            "DATA": os.path.join(HOME, "data"),
            "RUNTIME_HOME": mcs_runtime.runtime_home(cfg, root=HOME)}
    backup = cfg.get("backup", {"enabled": False})
    error = _backup_config(backup)
    if error:
        raise ValueError("backup_config_invalid")
    subs.update(BACKUP_ENABLED="1" if backup["enabled"] else "0",
                BACKUP_POLICY=backup["policy"] if backup["enabled"] else "",
                BACKUP_SNAPSHOT=backup.get("snapshot", "") if backup["enabled"] else "",
                BACKUP_SNAPSHOT_DIR=backup.get("snapshot_dir", "") if backup["enabled"] else "")
    grace = cfg.get("watchdog_grace_s", 60)
    if CONFIG_RULES["watchdog_grace_s"][1](grace):
        raise ValueError("watchdog_grace_config_invalid")
    subs["WATCHDOG_GRACE"] = str(grace)
    return subs


def _scripts_dir(cfg=None):
    cfg = load_config() if cfg is None else cfg
    return mcs_runtime.scripts_dir(cfg, root=HOME) if cfg.get("runtime_mode") == "standalone" else SCRIPTS_DIR


def _rendered_scripts(subs):
    """Yield (name, rendered body) for every deployment/scripts/*.sh —
    the single rendering path for both `services` and `check`."""
    src = os.path.join(REPO_ROOT, "deployment", "scripts")
    for name in sorted(os.listdir(src)):
        if not name.endswith(".sh"):
            continue
        yield name, _render_template(
            Path(os.path.join(src, name)).read_text(encoding="utf-8"),
            {key: shlex.quote(value) for key, value in subs.items()})


def _script_drift() -> tuple[list[str], list[str]]:
    """Deployed cron wrappers vs what `services` would render now. A
    checkout update without a `services` rerun leaves stale wrappers
    running (2026-09 incident: the old night filter in mcs_check.sh
    turned a delayed tick into a no-op and unread polling gapped past
    the session limit) — drift is an error, absence only a warning."""
    drifted, missing = [], []
    directory = _scripts_dir()
    try:
        subs = _service_subs()
    except ValueError as e:     # validate_config names the offending key
        return [f"config: {e} — scripts not compared"], []
    for name, body in _rendered_scripts(subs):
        try:
            cur = Path(directory, name).read_text(encoding="utf-8")
        except OSError:
            missing.append(name)
            continue
        if cur != body:
            drifted.append(name)
    errors = [f"deployed scripts differ from the repo in {directory}: "
              + ", ".join(drifted)
              + " — run `mcs_setup.py services` to resync"] if drifted else []
    warnings = [f"scripts not deployed to {directory}: "
                + ", ".join(missing)
                + " — run `mcs_setup.py services`"] if missing else []
    return errors, warnings


def _sync_scripts(subs, manifest, note, dry, directory=None) -> None:
    """Stage 1: cron wrapper scripts -> ~/.hermes/scripts, or ~/.mcs/scripts
    in standalone mode (atomic)."""
    directory = directory or _scripts_dir()
    for name, body in _rendered_scripts(subs):
        dst = os.path.join(directory, name)
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
            os.makedirs(directory, mode=0o700, exist_ok=True)
            _write_atomic(dst, body, 0o755)


def _desired_agents(subs, cfg) -> dict[str, str]:
    """label -> rendered plist: repo templates plus, in standalone mode,
    one calendar agent per CRON_JOBS entry."""
    pdir = os.path.join(REPO_ROOT, "deployment", "launchagents")
    jobs = {_cron_label(script): (script, sched) for _, sched, script in CRON_JOBS}
    out = {}
    for label in _agent_labels(cfg):
        if label in jobs:
            out[label] = _cron_plist(label, *jobs[label], cfg)
            continue
        out[label] = _render_template(
            Path(pdir, label + ".plist").read_text(encoding="utf-8"),
            {key: xml_escape(value, quote=False) for key, value in subs.items()})
    return out


def _sync_agents(subs, prev, manifest, note, dry, cfg=None, keep=frozenset()) -> int:
    """Stage 2: launchd agents (macOS only) — converge content AND
    loaded state; retire owned-but-undesired agents from our naming
    space only, never install.sh-owned labels (S5)."""
    if sys.platform != "darwin":
        note("launchd: not macOS — skipping agents")
        return 0
    problems = 0
    desired = _desired_agents(subs, load_config() if cfg is None else cfg)
    for label, body in desired.items():
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
    installed = {p[:-6] for p in os.listdir(AGENTS_DIR) if p.endswith(".plist")} \
        if os.path.isdir(AGENTS_DIR) else set()
    return problems + _retire_agents(
        {label for label in installed if _owned_label(label)} - set(desired) - set(keep),
        note, dry)


def _retire_agents(labels, note, dry) -> int:
    """Bootout + remove installed MCS-owned agents in `labels`."""
    if sys.platform.startswith("linux") and STANDALONE_LABEL in labels:
        path = _standalone_service_path()
        if not os.path.exists(path):
            return 0
        note("standalone service: disable + stop")
        if dry:
            return 0
        if _run(["systemctl", "--user", "disable", "--now", STANDALONE_UNIT]).returncode:
            return 1
        state = _run(["systemctl", "--user", "is-active", "--quiet", STANDALONE_UNIT])
        if state.returncode not in (3, 4):  # inactive / unknown unit; other errors are unverifiable
            note("standalone service: stop unverifiable; keeping unit")
            return 1
        try:
            os.unlink(path)
        except OSError:
            return 1
        return int(_run(["systemctl", "--user", "daemon-reload"]).returncode != 0)
    if sys.platform != "darwin" or not os.path.isdir(AGENTS_DIR):
        return 0
    problems = 0
    uid = os.getuid()
    for label in sorted(labels):
        path = os.path.join(AGENTS_DIR, label + ".plist")
        if not _owned_label(label) or not os.path.exists(path):
            continue
        note(f"agent {label}: undesired — bootout + remove")
        if not dry:
            _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
            if _agent_loaded(label):
                note(f"  agent {label}: still loaded; keeping plist")
                problems += 1
                continue
            try:
                os.unlink(path)
            except OSError:
                problems += 1
    return problems


def _cron_converged(after, sched, script) -> bool:
    jobs = [e for e in after if e.get("script") == script]
    if len(jobs) != 1:
        return False
    cur, want = (_norm_sched(jobs[0].get("schedule")),
                 _norm_sched(sched))
    return not (cur and want and cur != want)


def _verify_cron_after(hermes, created, owned_scripts, desired_scripts,
                       manifest, note, jobs=None) -> tuple[int, bool]:
    """Post-change convergence check for _sync_cron. Returns (problems,
    persist_ownership); mutates manifest['cron'] in place."""
    problems = 0
    # a zero exit is not proof — confirm desired jobs converged AND
    # owned-but-undesired jobs are gone. A survivor keeps problems
    # raised. No confirmed create: do not rewrite, so the previous
    # file (which lists that script) stays. A create this run that
    # `after` shows must be saved with the survivor, or the new job
    # is unowned once it leaves the desired set. same tolerance as
    # the pre-check: schedules are compared only when both sides
    # parse — an undecodable display is not drift
    after = _cron_list(hermes)
    if after is None or not all(_cron_converged(after, sched, script)
                                for _, sched, script in
                                (CRON_JOBS if jobs is None else jobs)):
        note("cron: post-change state unverifiable or not exactly one "
             "job per script — re-run services")
        problems += 1
    # owned-but-undesired jobs still live after the change
    survivors = [e for e in after or []
                 if isinstance(e.get("script"), str)
                 and e["script"] in owned_scripts
                 and e["script"] not in desired_scripts]
    left = list(dict.fromkeys(e["script"] for e in survivors))
    if left:
        note("cron: undesired job still present after remove ("
             + ", ".join(left) + ")")
        problems += 1
    # any confirmed create stays owned whatever else failed (this
    # stage or a later one) — else the job is orphaned once retired
    if after is not None and created:
        present = {e.get("script") for e in after}
        confirmed = [s for s in created if s in present]
        if confirmed:
            manifest["cron"] = [
                c for c in manifest["cron"]
                if not (isinstance(c, dict)
                        and c.get("script") in created
                        and c.get("script") not in present)]
            have = {c.get("script") for c in manifest["cron"]
                    if isinstance(c, dict)}
            for entry in survivors:
                script = entry["script"]
                if script not in have:
                    manifest["cron"].append({
                        "name": entry.get("name"),
                        "id": entry.get("id"),
                        "schedule": entry.get("schedule"),
                        "script": script})
                    have.add(script)
            return problems, True
    return problems, False


def _sync_cron(prev, hermes, manifest, note,
               dry, jobs=None) -> tuple[int, bool]:
    """Stage 3: hermes cron jobs — create missing, edit drifted,
    remove owned-but-undesired; an unparseable list fails CLOSED
    (M1): 'unverifiable' is NOT 'no jobs', and creating on that
    assumption produces duplicates. Returns (problems,
    persist_ownership): the latter is True when a verified create must
    be saved even though problems were raised."""
    # jobs=[] (standalone) retires every owned job: launchd runs them
    jobs = CRON_JOBS if jobs is None else jobs
    persist = False
    entries = _cron_list(hermes)
    if entries is None:
        note("cron: list unparseable — unverifiable, "
             "no cron mutations performed")
        return 1, persist
    problems = 0
    mutated = False
    created: list[str] = []
    # derive identities from the SAME verified list — a second
    # `cron list` call could fail transiently and return an
    # empty set that looks like 'no jobs' (fail-open, M1)
    existing = {v for e in entries for v in
                (e.get("name"), e.get("script")) if v}
    by_script = {e.get("script"): e for e in entries
                 if e.get("script")}
    desired_scripts = {s for _, _, s in jobs}
    # one job per owned script is the invariant every step below
    # relies on — which duplicate is authoritative is an operator call
    dups = sorted(s for s in desired_scripts
                  if sum(e.get("script") == s for e in entries) > 1)
    if dups:
        note("cron: duplicate owned scripts — resolve job IDs manually "
             f"({', '.join(dups)}), then re-run services")
        return 1, persist
    owned_scripts = desired_scripts | {
        c.get("script") for c in prev.get("cron", [])
        if isinstance(c, dict) and c.get("script")}
    for name, sched, script in jobs:
        entry = by_script.get(script)
        if entry is None and (name in existing
                              or script in existing):
            # identity seen but its script is unparseable — neither
            # 'exists' (unverified) nor safe to create (duplicate)
            note(f"cron '{name}': a job with this name exists but its "
                 f"script is not {script} or is unreadable — no create; "
                 "inspect `hermes cron list --all` and remove or fix it")
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
                created.append(script)
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
        p, ps = _verify_cron_after(hermes, created, owned_scripts,
                                   desired_scripts, manifest, note, jobs)
        problems += p
        persist = persist or ps
    return problems, persist


def _sync_gateway(cfg, hermes, note, dry) -> int:
    """Stage 4: hermes gateway — needed for either interactive
    transport (Discord interactions / Slack socket mode); `gateway
    install` creates the launchd service hermes owns."""
    ntf = cfg.get("notify") if isinstance(cfg, dict) else None
    if cfg.get("runtime_mode") == "standalone" or not (isinstance(ntf, dict)
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


def _standalone_service_path():
    if sys.platform == "darwin":
        return os.path.join(AGENTS_DIR, STANDALONE_LABEL + ".plist")
    return os.path.expanduser("~/.config/systemd/user/" + STANDALONE_UNIT)


def _standalone_loaded():
    if sys.platform == "darwin":
        return _agent_loaded(STANDALONE_LABEL)
    if sys.platform.startswith("linux"):
        return _run(["systemctl", "--user", "is-active", "--quiet", STANDALONE_UNIT]).returncode == 0
    return False


def _retire_previous_runtime(prev, desired_mode, note, dry):
    """Stop only the previous manifest's owned jobs before switching runtimes."""
    if not prev or prev.get("runtime_mode", "hermes") == desired_mode:
        return 0
    if prev.get("runtime_mode") == "standalone":
        # Keep its native entry until the replacement cron is verified.
        return 0
    labels = {row.get("label") for row in prev.get("agents", []) if isinstance(row, dict)} & set(AGENT_LABELS)
    scripts = {row.get("script") for row in prev.get("cron", []) if isinstance(row, dict)} & (
        {script for _, _, script in CRON_JOBS} | {"mcs_offsite.sh"})
    if dry:
        note("previous Hermes MCS jobs: stop " + ", ".join(sorted(labels | scripts)))
        return 0
    for label in sorted(labels):
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        if _agent_loaded(label):
            note(f"previous agent {label}: stop unverifiable — no new service started")
            return 1
        # bootout does not persist: launchd reloads a left plist at next login
        with suppress(FileNotFoundError):
            os.unlink(os.path.join(AGENTS_DIR, label + ".plist"))
    if scripts:
        exe = _hermes_exe(load_config())
        entries = _cron_list(exe)
        if entries is None:
            note("previous Hermes cron state unverifiable — no new service started")
            return 1
        for entry in entries:
            if entry.get("script") in scripts:
                if _run([exe, "cron", "remove", entry["id"]]).returncode:
                    return 1
        after = _cron_list(exe)
        if after is None or any(row.get("script") in scripts for row in after):
            return 1
    return 0


def _sync_standalone_service(manifest, note, dry):
    """Install one supervised host; its scheduler owns every MCS job."""
    from mcs_standalone.service import render, request_restart
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        note("standalone services require launchd or systemd; use an external supervisor")
        return 1
    _, body = render(HOME, platform=sys.platform)
    path = _standalone_service_path()
    try:
        current = Path(path).read_text(encoding="utf-8")
    except OSError:
        current = None
    changed = current != body
    running = _standalone_loaded()
    note(f"standalone service: {'update' if changed else 'up to date'} {path}")
    if not dry:
        if changed:
            _write_atomic(path, body, 0o600)
        if sys.platform.startswith("linux") and changed:
            if _run(["systemctl", "--user", "daemon-reload"]).returncode:
                return 1
        # launchd keeps the definition loaded at bootstrap: a rewritten plist
        # (moved checkout, new interpreter) takes effect only via bootout+bootstrap.
        # A loaded label with no plist on disk keeps the non-killing start path.
        reload = sys.platform == "darwin" and running and changed and current is not None
        if running and not reload:
            # Synchronization also reloads changed Python sources whose service argv is unchanged.
            try:
                request_restart(HOME)
            except (OSError, ValueError):
                # A loaded launchd label can be waiting after init was incomplete.
                # Starting without -k never kills a live host or its updater.
                start = ["launchctl", "kickstart", f"gui/{os.getuid()}/{STANDALONE_LABEL}"] if sys.platform == "darwin" else ["systemctl", "--user", "start", STANDALONE_UNIT]
                if _run(start).returncode:
                    note("standalone start failed; service was preserved")
                    return 1
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        request_restart(HOME)
                        break
                    except (OSError, ValueError):
                        time.sleep(0.2)
                else:
                    note("standalone restart request failed; service was preserved")
                    return 1
        elif sys.platform == "darwin":
            if not _agent_reconcile(STANDALONE_LABEL, path, note, False):
                return 1
        elif _run(["systemctl", "--user", "enable", "--now", STANDALONE_UNIT]).returncode:
            return 1
        if not _standalone_loaded():
            note("standalone service did not become active")
            return 1
    manifest["agents"].append({"label": STANDALONE_LABEL if sys.platform == "darwin" else STANDALONE_UNIT,
                              "sha256": _sha256(body), "loaded": not dry})
    manifest["scheduler"] = "standalone"
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
    cfg = load_config()
    try:
        selected_mode = mcs_runtime.mode(cfg)
    except ValueError:
        print("services: invalid runtime_mode — nothing changed")
        return 1
    try:
        subs = _service_subs(cfg)
    except ValueError as e:
        print(f"services: config: {e} — nothing changed")
        return 1
    problems = 0
    persist_partial = False
    prev = _load_manifest()
    manifest = {"v": 1, "at": time.time(), "runtime_mode": selected_mode,
                "python": subs["PYTHON"], "scripts": [],
                "agents": [], "cron": []}

    def note(msg):
        print(("  [dry] " if dry else "  ") + msg)

    why = _standalone_py_problem(cfg) if selected_mode == "standalone" else _hermes_py_problem()
    if why:
        # every wrapper/plist is rendered with __PYTHON__ — a missing
        # interpreter would make each job fail at every run
        print(f"services: {why} — nothing rendered; "
              + _install_fix("stage 2 rebuilds the standalone venv" if selected_mode == "standalone" else "stage 2 rebuilds the hermes-agent venv")
              + ", then re-run services")
        return 1
    runtime_errors = runtime_gate_errors(cfg, repair_recovery=True)
    if runtime_errors:
        print("services: " + "; ".join(runtime_errors) + " — nothing changed")
        return 1
    if _sync_recovery(cfg, note, dry):
        return 1
    if _retire_previous_runtime(prev, selected_mode, note, dry):
        return 1
    _sync_scripts(subs, manifest, note, dry)
    if selected_mode == "standalone":
        problems += _sync_standalone_service(manifest, note, dry)
        _record_llm_slots(manifest, note)
        if not dry and not problems:
            try:
                _save_manifest(manifest)
            except OSError:
                note("standalone manifest write failed")
                problems += 1
        return 1 if problems else 0
    hermes = _hermes_exe(cfg)
    cron_labels = frozenset(
        _cron_label(script) for _, _, script in configured_cron_jobs(cfg))
    problems += _sync_agents(subs, prev, manifest, note, dry, cfg,
                             cron_labels | {STANDALONE_LABEL})
    # the Hermes plugin decides from these flags whether to stand down
    if not dry and isinstance(cfg.get("notify"), dict) \
            and not _validate_notify(cfg["notify"]):
        with suppress(Exception):
            import notify_cards
            notify_cards.ensure_dirs(os.path.join(HOME, "data"))
            notify_cards.publish_flags(cfg, os.path.join(HOME, "data"))

    if not _hermes_ok(hermes):
        note(f"cron: hermes not resolvable ({hermes}) — skipped")
        problems += 1
    else:
        cron_problems, persist_partial = _sync_cron(
            prev, hermes, manifest, note, dry, jobs=configured_cron_jobs(cfg))
        problems += cron_problems
        retire_problems = 0
        if not cron_problems:
            retire_problems = _retire_agents(cron_labels | {STANDALONE_LABEL}, note, dry)
            problems += retire_problems
        switching = prev.get("runtime_mode") == "standalone"
        if not switching or not (cron_problems or retire_problems):
            problems += _sync_gateway(cfg, hermes, note, dry)
        if switching and not (cron_problems or retire_problems):
            # a gateway that loaded while standalone owned the bot skipped
            # /mcs and the card workers — reload it under the new flags
            note("gateway: restart (switched back from standalone)")
            if not dry:
                r = _hermes_cli(hermes, "", "gateway", "restart")
                if not (r and r.returncode == 0):
                    note("  gateway restart failed — run `hermes gateway restart`")
                    problems += 1

    _record_llm_slots(manifest, note)
    # manifest — the rollback snapshot's source of truth (R6).
    # A partial cron failure still persists verified creates plus
    # surviving owned jobs; an unverified create does not set the flag.
    if not dry and (not problems or persist_partial):
        try:
            _save_manifest(manifest)
            note(f"manifest: {MANIFEST_PATH}")
        except OSError as e:
            note(f"manifest write failed: {e}")
            problems += 1
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser(
        "init", help="provision config/env/keychain — "
                     "interactive run walks every setting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="non-interactive example (secrets via environment):\n"
               "  MCS_SETUP_PASSWORD=... python3 mcs/ops/mcs_setup.py init "
               "--yes \\\n"
               "    --login-id you@example.com "
               "--notify-target slack:C0CHANNELID \\\n"
               "    --set self_posts=true "
               "--set 'notify.interactive=\"slack\"'\n"
               "--set values are JSON (strings need inner quotes); a "
               "config.json that is\nnot valid JSON stops init — --yes "
               "moves it to config.json.corrupt-<ts> first.")
    p.add_argument("--login-id")
    p.add_argument("--runtime-mode", choices=("hermes", "standalone"),
                   help="select the runtime explicitly (omitted keeps the existing mode)")
    p.add_argument("--recovery-python",
                   help="explicit absolute independent Python path outside the mutable checkout")
    p.add_argument("--notify-target",
                   help="hermes send target for notifications "
                        "(e.g. slack:C0CHANNELID, discord:1234)")
    p.add_argument("--set", action="append", metavar="KEY=JSON",
                   help="set any config key (repeatable; dotted keys "
                        "nest, values are JSON — e.g. "
                        "--set self_posts=true "
                        '--set notify.interactive=\'"slack"\')')
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
    p.add_argument("--plugin-role-ids", metavar="A,B",
                   help="Discord guild role ids whose members may "
                        "operate cards (allowed_role_ids; discord only)")
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
                   help="JSON with exact copied cases from evaluation's "
                        "extract_cases, extract_cases_labs or semantic_completeness_cases "
                        "(unique synthetic subset allowed)")
    p.add_argument("--out", required=True, help="report output path")
    p.add_argument("--deadline", type=float, default=120.0,
                   help="evaluation deadline in seconds")
    p.set_defaults(fn=cmd_jev_value)
    p = sub.add_parser("services",
                       help="install the selected runtime's supervised services "
                            "(renders deployment/ templates; idempotent)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the actions without applying them")
    p.set_defaults(fn=cmd_services, yes=True)
    p = sub.add_parser("check", help="validate required conditions")
    p.set_defaults(fn=cmd_check, yes=True)
    p = sub.add_parser("doctor", help="local read-only counts by scope "
                                      "(not_checked is not healthy; exit 1 only "
                                      "on a blocked scope); use check for blockers")
    p.add_argument("--json", action="store_true", help="safe shared JSON result")
    p.add_argument("--probe", action="append", choices=("llm", "services"),
                   help="explicit read-only scope (repeatable); default is local only")
    p.set_defaults(fn=cmd_doctor, yes=True)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
