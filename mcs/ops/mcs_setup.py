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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
    "notify_max_age_h":      (False, _num),
    "job_budget_seconds":    (False, _num),
    "self_posts":            (False, _bool),
    "notify":                (False, _dict),
    "signals":               (False, _dict),
    "semantic":              (False, _dict),
    "update":                (False, _dict),
}


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
            for k in ("profile", "application_id", tenant,
                      "channel_id"):
                if not isinstance(d.get(k), str) or not d[k].strip():
                    errors.append(f"notify.{transport}.{k}: "
                                  "must be a non-empty string")
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
        if type(v) not in (int, float) or type(v) is bool or v < 0:
            errors.append("update.auto_delay_h: must be a number >= 0")
    if "include_prerelease" in upd \
            and type(upd["include_prerelease"]) is not bool:
        errors.append("update.include_prerelease: must be a boolean")
    return errors


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
    if isinstance(cfg.get("semantic"), dict):
        try:
            from semantic_policy import semantic_config
            errors.extend(semantic_config(cfg)[1])
        except Exception as e:  # validator itself must not crash check
            errors.append(f"semantic: validator failed ({type(e).__name__})")
    if isinstance(cfg.get("update"), dict):
        errors.extend(_validate_update(cfg["update"]))
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


def _hermes_config_set(exe: str, profile: str, key: str, val) -> bool:
    if key == "DISCORD_BOT_TOKEN":
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
                      "local.mcs-cmd", "local.mcs-int"):
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
        with open(path, encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except FileNotFoundError:
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
    ("Discord カード通知（interactive=discord でボタン付きカード）", [
        ("notify.interactive", "choice:off,discord", "off",
         "通知形式 — discord=カード / off=従来テキストのみ", None),
        ("notify.discord.profile", "req", None,
         "配送に使う hermes プロファイル名", _discord_on),
        ("notify.discord.application_id", "req", None,
         "Discord アプリケーションID", _discord_on),
        ("notify.discord.guild_id", "req", None,
         "Discord サーバーID", _discord_on),
        ("notify.discord.channel_id", "req", None,
         "カードの投稿先チャンネルID", _discord_on),
        ("notify.operator", "opt", None,
         "運用者の Discord ユーザーID（空欄可）", _discord_on),
        ("notify.card_thread", "bool", True,
         "患者スレッドごとにカードをまとめる", _discord_on),
        ("notify.card_thread_archive_min", "int", 10080,
         "カードスレッドをアーカイブするまでの分数", _discord_on),
        ("notify.route_epoch", "int", 1,
         "配送先を変えたとき+1する番号（通常はそのまま）", _discord_on),
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
         "意味解析モード — shadow=記録のみ / enforce=判定に使用", None),
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
        if v <= 0:
            return False, "0より大きい数値で入力してください"
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
    return "[" + ", ".join(f'"{x.strip()}"'
                           for x in text.split(",") if x.strip()) + "]"


def _apply_plugin_integration(cfg: dict, args) -> None:
    """Wire the mcs-discord-commands plugin into hermes through the
    public `hermes config` CLI — writes the serving profile's
    config.yaml / .env; never touches hermes-agent internals. Only
    runs when notify.interactive == "discord"."""
    ntf = cfg.get("notify")
    if not isinstance(ntf, dict) or ntf.get("interactive") != "discord":
        return
    exe = _hermes_exe(cfg)
    if not _hermes_ok(exe):
        print("\nhermes CLI が見つかりません — プラグイン設定は後で "
              "`hermes config set "
              f"{PLUGIN_SETTINGS}.<key> <値>` で行ってください")
        return
    nd = ntf.get("discord") if isinstance(ntf.get("discord"), dict) else {}
    profile = getattr(args, "plugin_profile", None)
    if profile is None:
        if args.yes:
            profile = ""
        else:
            profile = input(
                "\nプラグイン設定を書き込む hermes プロファイル名"
                "（multiplex 構成では Discord を受け持つ profile。"
                "空欄=既定）: ").strip()
    print(f"\nDiscord 連携 — hermes "
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
    for k in ("profile", "application_id", "guild_id", "channel_id"):
        if nd.get(k):
            fixed[k] = nd[k]
    missing = []
    for key, val in fixed.items():
        if _hermes_config_set(exe, profile, f"{PLUGIN_SETTINGS}.{key}",
                              val):
            print(f"  settings.{key}: 設定")
        else:
            missing.append(key)

    # allowlists are operator-specific: flag > existing value > prompt
    lists = (
        ("allowed_user_ids", getattr(args, "plugin_user_ids", None),
         "カード操作を許可する Discord user ID"),
        ("allowed_chat_ids", getattr(args, "plugin_chat_ids", None),
         "コマンドを受け付ける Discord channel ID"),
        ("project_ids", getattr(args, "plugin_project_ids", None),
         "対象とする MCS project ID"),
    )
    for key, flag_val, label in lists:
        if flag_val is None:
            if _hermes_config_get(exe, profile,
                                  f"{PLUGIN_SETTINGS}.{key}"):
                print(f"  settings.{key}: 設定済み")
                continue
            if args.yes:
                missing.append(key)
                continue
            default = nd.get("channel_id", "") \
                if key == "allowed_chat_ids" else ""
            flag_val = input(
                f"  {key} — {label}（カンマ区切り"
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
    if missing:
        print("  未設定: " + ", ".join(missing) +
              " — 後で `hermes config set "
              f"{PLUGIN_SETTINGS}.<key> <値>` で設定してください")

    # Pipe the token to the public writer so process listings cannot expose
    # it. Older Hermes versions must fail instead of falling back to argv.
    # `config set` routes *_TOKEN keys to the profile .env. It must
    # land in the SERVING profile's .env: under multiplex each profile's
    # secret scope is authoritative and a miss never falls through to
    # the default profile's .env (agent/secret_scope.py).
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if tok:
        ok = _hermes_config_set(exe, profile, "DISCORD_BOT_TOKEN", tok)
        print("  DISCORD_BOT_TOKEN: "
              + (f"hermes {'-p ' + profile if profile else '既定'} "
                 ".env へ保存" if ok else "保存失敗 — Hermes の config set --stdin 対応を確認してください"))
    elif _hermes_config_get(exe, profile, "DISCORD_BOT_TOKEN"):
        print("  DISCORD_BOT_TOKEN: 設定済み")
    else:
        if not args.yes:
            tok = getpass.getpass(
                "  Discord bot token（hermes .env へ保存。"
                "空欄=スキップ）: ") or None
            if tok:
                ok = _hermes_config_set(exe, profile,
                                        "DISCORD_BOT_TOKEN", tok)
                print("  DISCORD_BOT_TOKEN: "
                      + (f"hermes {'-p ' + profile if profile else '既定'}"
                         " .env へ保存" if ok else "保存失敗 — Hermes の config set --stdin 対応を確認してください"))
        if not tok:
            print("  DISCORD_BOT_TOKEN: 未設定 — `hermes "
                  + (f"-p {profile} " if profile else "")
                  + "setup` で後から設定")


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

    os.makedirs(os.path.join(HOME, "data"), exist_ok=True)
    os.makedirs(os.path.join(HOME, "data", "cmd"), exist_ok=True)
    os.makedirs(os.path.join(HOME, "chrome-profile"), exist_ok=True)
    _write_atomic(CONF_PATH, json.dumps(cfg, ensure_ascii=False,
                                      indent=2, sort_keys=True) + "\n", 0o600)
    print(f"config: wrote {CONF_PATH}")
    _apply_plugin_integration(cfg, args)
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
            with open(args.gate_evidence, "rb") as stream:
                raw = stream.read(8 * 1024 * 1024)
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

CRON_JOBS = [
    ("MCS unread check", "*/5 * * * *", "mcs_check.sh"),
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
    for k, v in subs.items():
        text = text.replace("__" + k + "__", v)
    return text


def _write_atomic(path: str, body: str, mode: int | None = None) -> None:
    """tmp -> fsync -> chmod -> os.replace -> dir fsync — a mid-write
    crash never leaves a truncated script/plist behind, and the file
    never exists at the destination with the wrong mode (S: atomic
    render)."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".svc.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        dfd = os.open(parent, os.O_RDONLY)
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
    for line in r.stdout.splitlines():
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
    return out


def _norm_sched(s: str) -> str | None:
    """Extract a comparable 5-field cron expr from a Schedule field —
    tolerant of 'cron: ' prefixes or extra decoration."""
    m = re.search(r"(\S+\s+\S+\s+\S+\s+\S+\s+\S+)", s or "")
    return m.group(1) if m else None


def _agent_loaded(label: str) -> bool:
    uid = os.getuid()
    r = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                       capture_output=True)
    return r.returncode == 0


def _agent_reconcile(label: str, dst: str, note, dry: bool) -> bool:
    """Bootout+bootstrap when the plist changed; bootstrap when absent.
    Converges loaded state to rendered content (R6)."""
    uid = os.getuid()
    if _agent_loaded(label):
        note(f"agent {label}: reload (content changed)")
        if not dry:
            subprocess.run(["launchctl", "bootout",
                            f"gui/{uid}/{label}"],
                           capture_output=True, text=True)
    else:
        note(f"agent {label}: bootstrap")
    if dry:
        return True
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", dst],
                       capture_output=True, text=True)
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

    # 1. hermes cron wrapper scripts -> ~/.hermes/scripts (atomic)
    src = os.path.join(REPO_ROOT, "deployment", "scripts")
    for name in sorted(os.listdir(src)):
        if not name.endswith(".sh"):
            continue
        body = _render_template(
            open(os.path.join(src, name), encoding="utf-8").read(), subs)
        dst = os.path.join(SCRIPTS_DIR, name)
        manifest["scripts"].append({"name": name,
                                    "sha256": _sha256(body)})
        cur = None
        try:
            cur = open(dst, encoding="utf-8").read()
        except OSError:
            pass
        if cur == body:
            note(f"script {name}: up to date")
            continue
        note(f"script {name}: write {dst}")
        if not dry:
            _write_atomic(dst, body, 0o755)

    # 2. launchd agents (macOS only) — converge content AND loaded state
    if sys.platform != "darwin":
        note("launchd: not macOS — skipping agents")
    else:
        pdir = os.path.join(REPO_ROOT, "deployment", "launchagents")
        uid = os.getuid()
        for label in AGENT_LABELS:
            srcp = os.path.join(pdir, label + ".plist")
            body = _render_template(
                open(srcp, encoding="utf-8").read(), subs)
            dst = os.path.join(AGENTS_DIR, label + ".plist")
            try:
                cur = open(dst, encoding="utf-8").read()
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
        owned = set(AGENT_LABELS)
        owned |= {a.get("label") for a in prev.get("agents", [])
                  if isinstance(a, dict)}
        for path in sorted(
                p for p in os.listdir(AGENTS_DIR) if p.endswith(".plist")) \
                if os.path.isdir(AGENTS_DIR) else []:
            label = path[:-6]
            mcs_owned = label.startswith("local.mcs-") \
                or label.startswith("ai.mcs.extract-")
            if not mcs_owned or label in EXCLUDED_LABELS \
                    or label in owned:
                continue
            note(f"agent {label}: undesired — bootout + remove")
            if not dry:
                subprocess.run(["launchctl", "bootout",
                                f"gui/{uid}/{label}"],
                               capture_output=True, text=True)
                try:
                    os.unlink(os.path.join(AGENTS_DIR, path))
                except OSError:
                    pass

    # 3. hermes cron jobs — create missing, edit drifted, remove
    #    owned-but-undesired; unparseable list => unverifiable
    cfg = load_config()
    hermes = _hermes_exe(cfg)
    if not _hermes_ok(hermes):
        note(f"cron: hermes not resolvable ({hermes}) — skipped")
        problems += 1
    else:
        entries = _cron_list(hermes)
        if entries is None:
            # fail CLOSED (M1): an unverifiable list is NOT 'no jobs' —
            # creating on that assumption produces duplicates
            note("cron: list unparseable — unverifiable, "
                 "no cron mutations performed")
            problems += 1
        else:
            # derive identities from the SAME verified list — a second
            # `cron list` call could fail transiently and return an
            # empty set that looks like 'no jobs' (fail-open, M1)
            existing = {v for e in entries for v in
                        (e.get("name"), e.get("script")) if v}
            by_script = {e.get("script"): e for e in entries
                         if e.get("script")}
            desired_scripts = {s for _, _, s in CRON_JOBS}
            owned_scripts = desired_scripts | {
                c.get("script") for c in prev.get("cron", [])
                if isinstance(c, dict) and c.get("script")}
            for name, sched, script in CRON_JOBS:
                entry = by_script.get(script)
                if entry is None and (name in existing
                                      or script in existing):
                    # identity seen but fields unparseable — leave it
                    note(f"cron '{name}': exists")
                    manifest["cron"].append(
                        {"name": name, "schedule": sched,
                         "script": script})
                    continue
                if entry is None:
                    note(f"cron '{name}': create ({sched} -> {script})")
                    if not dry:
                        r = subprocess.run(
                            [hermes, "cron", "create", sched,
                             "--name", name, "--script", script,
                             "--no-agent", "--deliver", "local"],
                            capture_output=True, text=True)
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
                    if not dry:
                        r = subprocess.run(
                            [hermes, "cron", "edit", entry["id"],
                             "--schedule", sched],
                            capture_output=True, text=True)
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
                    if not dry:
                        r = subprocess.run(
                            [hermes, "cron", "remove", entry["id"]],
                            capture_output=True, text=True)
                        if r.returncode != 0:
                            note(f"  remove failed: "
                                 f"{(r.stderr or r.stdout).strip()}")
                            problems += 1

        # 4. hermes gateway — needed for either interactive transport
        #    (Discord interactions / Slack socket mode); `gateway
        #    install` creates the launchd service hermes owns.
        ntf = cfg.get("notify") if isinstance(cfg, dict) else None
        if isinstance(ntf, dict) \
                and ntf.get("interactive") in ("discord", "slack"):
            r = _hermes_cli(hermes, "", "gateway", "status")
            up = bool(r and r.returncode == 0
                      and "supervised" in (r.stdout or ""))
            if up:
                note("gateway: supervised")
            elif dry:
                note("gateway: install + start")
            else:
                ok = True
                for sub in ("install", "start"):
                    r = _hermes_cli(hermes, "", "gateway", sub)
                    if not (r and r.returncode == 0):
                        detail = (r.stderr or r.stdout).strip() \
                            if r else "no response"
                        note(f"gateway {sub} failed: {detail}")
                        ok = False
                        break
                if ok:
                    note("gateway: installed + started")
                else:
                    problems += 1

    # 5. manifest — the rollback snapshot's source of truth (R6)
    if not dry:
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
                   help="hermes profile serving Discord — plugin "
                        "settings are written there (multiplex; "
                        "empty/omitted = default profile)")
    p.add_argument("--plugin-user-ids", metavar="A,B",
                   help="Discord user ids allowed to operate cards "
                        "(plugins ... settings.allowed_user_ids)")
    p.add_argument("--plugin-chat-ids", metavar="A,B",
                   help="Discord channel ids that accept /mcs commands "
                        "(plugins ... settings.allowed_chat_ids)")
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
