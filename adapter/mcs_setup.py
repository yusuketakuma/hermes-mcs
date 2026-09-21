"""MCS environment setup + required-condition validation.

`init` interactively (or via flags) provisions what a fresh machine
needs: ~/.mcs/config.json, ~/.mcs/.env secrets, and the macOS Keychain
entry the adapter reads at re-login (service 'mcs-adapter' — on the
Mac mini this is the same login keychain Chrome uses). Existing values
are merged, never silently overwritten.

`check` is the typesafe gate: every required key must exist with the
right type, optional keys are range-checked, the semantic block is
validated by the production `semantic_config` validator, and the
environment probes (Keychain entry, Chrome binary, local-LLM endpoint,
secret resolution) report as warnings vs errors. Exit 1 on any error
so it can gate automation.

  python3 adapter/mcs_setup.py init [--login-id ID --password PW ...]
  python3 adapter/mcs_setup.py check
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcs_util import CONF_PATH, HOME, env_value, load_config

ENV_PATH = os.path.join(HOME, ".env")
KEYCHAIN_SERVICE = "mcs-adapter"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LLM_MODELS_URL = "http://127.0.0.1:8080/v1/models"

# ---- typesafe config rules ------------------------------------------
# name -> (required, check) — check(v) -> error string | None
def _nonempty_str(v):
    return None if isinstance(v, str) and v.strip() else "must be a non-empty string"


def _channel_id(v):
    ok = (isinstance(v, str) and v.isdigit()) or type(v) is int
    return None if ok else "must be a numeric channel id"


def _bool(v):
    return None if type(v) is bool else "must be a boolean"


def _int_range(lo, hi):
    def f(v):
        if type(v) is not int or not (lo <= v <= hi):
            return f"must be an integer in [{lo},{hi}]"
        return None
    return f


def _num(v):
    return None if type(v) in (int, float) and v > 0 else "must be a positive number"


def _bot_profile(v):
    ok = isinstance(v, str) and re.fullmatch(r"[a-z0-9_-]+", v)
    return None if ok else "must match [a-z0-9_-]+"


def _dict(v):
    return None if isinstance(v, dict) else "must be an object"


CONFIG_RULES = {
    "mcs_login_id":        (True,  _nonempty_str),
    "discord_channel_id":  (True,  _channel_id),
    "notify_bot_profile":  (False, _bot_profile),
    "discover_archived":   (False, _bool),
    "deep_history":        (False, _bool),
    "trickle_pages":       (False, _int_range(1, 40)),
    "job_budget_seconds":  (False, _num),
    "signals":             (False, _dict),
    "semantic":            (False, _dict),
}


def validate_config(cfg: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for config.json — typesafe required
    conditions. Unknown keys warn (forward-compat) but never fail."""
    errors, warnings = [], []
    if not isinstance(cfg, dict):
        return ["config.json is not a JSON object"], []
    for key in sorted(set(cfg) - set(CONFIG_RULES)):
        warnings.append(f"unknown config key: {key}")
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
    if isinstance(cfg.get("semantic"), dict):
        try:
            from semantic_policy import semantic_config
            errors.extend(semantic_config(cfg)[1])
        except Exception as e:  # validator itself must not crash check
            errors.append(f"semantic: validator failed ({type(e).__name__})")
    return errors, warnings


def check_environment(cfg: dict) -> tuple[list[str], list[str]]:
    """Machine probes — things config.json can't express. Keychain and
    token resolution are errors only when the feature needs them."""
    errors, warnings = [], []
    if sys.platform != "darwin":
        warnings.append("not macOS — Keychain/launchd steps do not apply")
    elif subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True).returncode != 0:
        errors.append(f"Keychain entry '{KEYCHAIN_SERVICE}' not found — "
                      "re-login cannot inject the MCS password")
    if not os.path.exists(CHROME_BIN):
        errors.append(f"Chrome binary missing: {CHROME_BIN}")
    try:
        urllib.request.urlopen(LLM_MODELS_URL, timeout=3).close()
    except Exception:
        warnings.append("local LLM endpoint 127.0.0.1:8080 not reachable — "
                        "extract_llm/semantic jobs will stall until "
                        "llama.cpp is up")
    if env_value("DISCORD_BOT_TOKEN") is None:
        errors.append("DISCORD_BOT_TOKEN not resolvable "
                      "(~/.mcs/.env or ~/.hermes/.env) — notifications "
                      "cannot be sent")
    sem = cfg.get("semantic")
    if isinstance(sem, dict) and sem.get("mode", "off") != "off" \
            and env_value("TYPESAFE_API_KEY") is None:
        errors.append("semantic.mode is enabled but TYPESAFE_API_KEY "
                      "is not resolvable — Jev verdicts fail as "
                      "no_api_key")
    elif env_value("TYPESAFE_API_KEY") is None:
        warnings.append("TYPESAFE_API_KEY not set — needed when "
                        "semantic.mode is enabled")
    return errors, warnings


# ---- .env merge ------------------------------------------------------

def _env_write(path: str, updates: dict[str, str]):
    """Merge KEY=value lines — existing keys preserved unless updated."""
    lines, seen = [], set()
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        pass
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(path, 0o600)  # enforce even when the file pre-existed


def cmd_init(args) -> int:
    cfg = load_config()
    def pick(flag, key, prompt, secret=False):
        v = flag if flag is not None else cfg.get(key)
        if v is None and not args.yes:
            v = (getpass.getpass(prompt + ": ") if secret
                 else input(prompt + ": ")).strip()
        return v

    login_id = pick(args.login_id, "mcs_login_id", "MCS login ID")
    channel = pick(args.discord_channel, "discord_channel_id",
                   "Discord channel ID")
    updates = {}
    if login_id:
        cfg["mcs_login_id"] = login_id
    if channel:
        cfg["discord_channel_id"] = str(channel)

    pw = args.password
    if pw is None and not args.yes:
        pw = getpass.getpass(
            "MCS password (stored in Keychain 'mcs-adapter', "
            "leave empty to skip): ")
    if pw:
        r = subprocess.run(
            ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", login_id or "mcs", "-w", pw, "-U"],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"keychain write failed: {r.stderr.strip()}")
            return 1
        print(f"keychain: '{KEYCHAIN_SERVICE}' registered")

    if args.discord_token:
        updates["DISCORD_BOT_TOKEN"] = args.discord_token
    if args.typesafe_key:
        updates["TYPESAFE_API_KEY"] = args.typesafe_key
    if updates:
        _env_write(ENV_PATH, updates)
        print(f".env: wrote {sorted(updates)} to {ENV_PATH} (0600)")

    if args.semantic_mode:
        sem = cfg.setdefault("semantic", {})
        sem["mode"] = args.semantic_mode
        if args.project_ids is not None:
            sem["project_ids"] = args.project_ids
    if args.signals_notify is not None:
        cfg.setdefault("signals", {})["notify"] = args.signals_notify

    os.makedirs(os.path.join(HOME, "data"), exist_ok=True)
    os.makedirs(os.path.join(HOME, "chrome-profile"), exist_ok=True)
    fd = os.open(CONF_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"config: wrote {CONF_PATH}")
    return cmd_check(args)


def cmd_check(args) -> int:
    cfg = load_config()
    errors, warnings = validate_config(cfg)
    e2, w2 = check_environment(cfg)
    errors += e2
    warnings += w2
    for w in warnings:
        print(f"  warn : {w}")
    for e in errors:
        print(f"  error: {e}")
    print("check: " + ("FAIL" if errors else "OK")
          + f" ({len(errors)} errors, {len(warnings)} warnings)")
    return 1 if errors else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="provision config/env/keychain")
    p.add_argument("--login-id")
    p.add_argument("--password")
    p.add_argument("--discord-channel")
    p.add_argument("--discord-token")
    p.add_argument("--typesafe-key")
    p.add_argument("--semantic-mode", choices=["off", "shadow", "enforce"])
    p.add_argument("--project-ids", type=int, nargs="*")
    p.add_argument("--signals-notify", action=argparse.BooleanOptionalAction)
    p.add_argument("--yes", action="store_true",
                   help="non-interactive — never prompt")
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("check", help="validate required conditions")
    p.set_defaults(fn=cmd_check, yes=True)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
