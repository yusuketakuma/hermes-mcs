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

  python3 mcs/ops/mcs_setup.py init [--login-id ID ...]
  python3 mcs/ops/mcs_setup.py check

Secrets are never accepted as argv flags (they would persist in shell
history and `ps`): export MCS_SETUP_PASSWORD and/or TYPESAFE_API_KEY in
the environment, or answer the interactive prompts.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401

from mcs_util import CONF_PATH, HOME, env_value, load_config

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
    return None if type(v) in (int, float) and v > 0 else "must be a positive number"


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
    "job_budget_seconds":    (False, _num),
    "signals":               (False, _dict),
    "semantic":              (False, _dict),
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
    else:
        # The .env MCS_PASSWORD fallback covers a rebooted machine whose
        # login keychain is still locked — when present, keychain
        # unreadability is a warning, not a blocker.
        env_pw = bool(env_value("MCS_PASSWORD", check_env=False))
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True)
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
            w = subprocess.run(
                ["security", "find-generic-password", "-s",
                 KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True)
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
    try:
        urllib.request.urlopen(LLM_MODELS_URL, timeout=3).close()
    except Exception:
        warnings.append("local LLM endpoint 127.0.0.1:8080 not reachable — "
                        "extract_llm/semantic jobs will stall until "
                        "llama.cpp is up")
    profile = cfg.get("notify_bot_profile")
    if isinstance(profile, str) and profile \
            and not re.fullmatch(r"[a-z0-9_-]+", profile):
        errors.append("notify_bot_profile: must match [a-z0-9_-]+")
    # Delivery is `hermes send` — the binary must resolve the same way
    # notifier._hermes_exe does: config hermes_bin, else PATH, else the
    # standard user-local install (launchd PATH is minimal).
    exe = cfg.get("hermes_bin")
    if not isinstance(exe, str) or not exe.strip():
        exe = shutil.which("hermes") \
            or os.path.expanduser("~/.local/bin/hermes")
    else:
        exe = exe.strip()
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        errors.append(f"hermes CLI not resolvable ({exe}) — "
                      "notifications cannot be sent")
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
        for label in ("ai.mcs.extract-drainer", "ai.mcs.extract-drainer-rt",
                      "local.mcs-cmd"):
            if not os.path.exists(os.path.join(agents, f"{label}.plist")):
                warnings.append(
                    f"LaunchAgent {label} not installed — templates and "
                    "install steps in deployment/launchagents/README.md")
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

    cmd = (f"add-generic-password -s {_q(KEYCHAIN_SERVICE)} "
           f"-a {_q(account)} -U -w {_q(pw)}\n")
    subprocess.run(["security", "-i"], input=cmd,
                   capture_output=True, text=True)
    chk = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
         "-w"], capture_output=True, text=True)
    if chk.returncode == 0 and chk.stdout.rstrip("\n") == pw:
        return True
    # verification failed — drop any partially/incorrectly stored item
    subprocess.run(["security", "delete-generic-password", "-s",
                    KEYCHAIN_SERVICE, "-a", account], capture_output=True)
    return False


def cmd_init(args) -> int:
    cfg = load_config()
    def pick(flag, key, prompt, secret=False):
        v = flag if flag is not None else cfg.get(key)
        if v is None and not args.yes:
            v = (getpass.getpass(prompt + ": ") if secret
                 else input(prompt + ": ")).strip()
        return v

    login_id = pick(args.login_id, "mcs_login_id", "MCS login ID")
    target = pick(args.notify_target, "notify_target",
                  "hermes send target (e.g. slack, slack:#mcs)")
    updates = {}
    if login_id:
        cfg["mcs_login_id"] = login_id
    if target:
        cfg["notify_target"] = str(target)

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
            print("keychain write failed — stored value did not read "
                  "back; the .env fallback was still written, and the "
                  "keychain can be repaired later with `security "
                  "add-generic-password -s mcs-adapter`")
            keychain_failed = True
        else:
            print(f"keychain: '{KEYCHAIN_SERVICE}' registered")

    ts_key = os.environ.get("TYPESAFE_API_KEY")
    if ts_key:
        updates["TYPESAFE_API_KEY"] = ts_key
    if updates:
        _env_write(ENV_PATH, updates)
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

    os.makedirs(os.path.join(HOME, "data"), exist_ok=True)
    os.makedirs(os.path.join(HOME, "chrome-profile"), exist_ok=True)
    fd = os.open(CONF_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"config: wrote {CONF_PATH}")
    return cmd_check(args)


def cmd_fact_source(args) -> int:
    """Set semantic.fact_source with an evidence gate for ``canonical``.

    Production promotion is pinned: ``--gate-evidence`` must be a
    semantic_evaluation report whose gate passed on human-labelled data
    (gate.pass and gate.g6_eligible).  Synthetic or failing reports can
    never mint the token, and the written config is validated
    fail-closed before it lands.
    """
    import hashlib
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
            raw = os.read(os.open(args.gate_evidence, os.O_RDONLY),
                          8 * 1024 * 1024)
            report = json.loads(raw)
        except (OSError, ValueError) as e:
            print(f"fact_source: gate evidence unreadable ({e})")
            return 1
        gate = report.get("gate") if isinstance(report, dict) else None
        provenance = report.get("label_provenance") \
            if isinstance(report, dict) else None
        human = provenance.get("human", 0) \
            if isinstance(provenance, dict) else 0
        if not isinstance(report, dict) \
                or not report.get("schema_version") \
                or not isinstance(gate, dict) or not gate.get("pass") \
                or not gate.get("g6_eligible") \
                or not isinstance(human, int) or human < 1:
            reasons = gate.get("reasons") if isinstance(gate, dict) \
                else None
            print("fact_source: gate evidence does not pass on "
                  f"human-labelled data{f' ({reasons})' if reasons else ''}")
            return 1
        digest = hashlib.sha256(raw).hexdigest()[:16]
        criteria_version = str(report.get("criteria_version", "unknown"))
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
    fd = os.open(CONF_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
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
        raw = os.read(os.open(args.cases, os.O_RDONLY), 8 * 1024 * 1024)
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
    p.add_argument("--notify-target",
                   help="hermes send target for notifications "
                        "(e.g. slack, slack:#mcs, discord:1234)")
    p.add_argument("--semantic-mode", choices=["off", "shadow", "enforce"])
    p.add_argument("--project-ids", type=int, nargs="*")
    p.add_argument("--signals-notify", action=argparse.BooleanOptionalAction)
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
    p = sub.add_parser("check", help="validate required conditions")
    p.set_defaults(fn=cmd_check, yes=True)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
