#!/bin/sh
# Install everything MCS needs on a fresh machine, in idempotent stages:
#
#   1. Homebrew packages (git, python, uv, llama.cpp, Google Chrome)
#   2. hermes-agent checkout (pinned) + venv + ~/.local/bin/hermes shim
#   3. Discord command plugin (symlink + `hermes plugins enable`)
#   4. local LLM — llama-server on 127.0.0.1:8080 + Qwen3.5-9B model
#   5. scheduled services — launchd agents + hermes cron jobs
#      (delegated to `mcs_setup.py services`)
#   6. update recovery — ~/.mcs-recovery tool + independent launchd
#      watchdog (org.mcs.recovery), outside the repo so a broken new
#      release can never take the recovery path down with it
#
#   ./install.sh [OPTIONS] [HERMES_HOME]   HERMES_HOME defaults to ~/.hermes
#
#   --preflight     read-only check of every prerequisite (alias
#                   --check-only): prints OK / WARN / NG with the fix
#                   command; exits 1 when a blocker (NG) exists.
#                   Nothing is written. Run this first.
#   --mode MODE     hermes or standalone (existing config, then hermes
#                   when unspecified). Standalone installs
#                   its own venv and official connector SDKs; Hermes
#                   is neither downloaded nor configured.
#   --recovery-python PATH  existing independent Python outside the mutable checkout
#   --dry-run       preflight + the per-stage plan of what would be
#                   written or changed; nothing is written
#   --force-repo    allow re-pointing an existing install (plugin
#                   symlink, ~/.mcs-recovery/repo_path, services) at
#                   this checkout when it was installed from another one
#   --no-brew       skip stage 1 (assume git/python/uv/llama.cpp are
#                   already provided some other way)
#   --no-llm        skip stage 4 (you run your own LLM server — MCS
#                   expects an OpenAI-compatible endpoint on
#                   127.0.0.1:8080; keep it serving there)
#   --no-plugin     skip stage 3 (needed for discord/slack cards;
#                   text-only notify.interactive=off installs may skip)
#   --no-services   skip stage 5 (launchd agents + hermes cron)
#   --no-recovery   skip stage 6 (update watchdog)
#   -h, --help      show this usage
#
# Run as your normal user (never sudo). Re-running is safe: every stage
# checks first and skips what exists, and an interrupted clone, venv or
# model download is resumed, so partially-installed machines converge.
# A stage that fails stops the install with a non-zero exit — repair
# it and re-run; later stages never run on top of a broken one.
# What is NOT automated (needs your secrets / interactive choices):
# `mcs_setup.py init` (guided wizard) handles secrets and settings;
# terminal installs offer to open it after the software is prepared.
set -eu
# files this script creates are the owner's only; existing modes are kept
umask 077

usage() {
    cat <<'EOF'
MCS 導入ガイド — この Mac に必要なソフトと自動実行の仕組みを準備します。
使い方: sh install.sh [オプション] [HERMES_HOME]
通常はオプションなしで実行し、画面の番号を選んでください。

  --preflight / --check-only  導入前の確認だけ（変更しません）
  --dry-run                  確認と作業予定の表示だけ（変更しません）
  --mode hermes|standalone    連携方式を指定（省略時は既存設定、未設定は hermes）
  --no-brew                   必要ソフトの導入を省略（管理者向け）
  --no-llm                    この Mac で動く AI の導入を省略
  --no-plugin                Slack/Discord の操作ボタンの導入を省略
  --no-services              自動実行の登録を省略
  --no-recovery              復旧ツールの登録を省略
  --recovery-python PATH     復旧用 Python の絶対パス（管理者向け）
  --force-repo               既存の導入元を変更（管理者向け）
  -h / --help                この説明を表示

普段お使いのユーザーで実行してください。sudo は使いません。
途中で止まった場合は、表示された原因を解消して同じ操作を再実行できます。
EOF
}

# EOF is cancellation, never acceptance of a recommended action.
choose() {
    while :; do
        printf '%s' "$1"
        _answer=""
        if ! read -r _answer; then
            printf '\n入力を終了したため、ここで中止しました。\n'
            return 130
        fi
        case "$_answer" in
            ""|1) _answer=1; return ;;
            2) return ;;
            0) printf '中止しました。\n'; return 130 ;;
            *) printf '1 または 2 を入力してください。中止は 0 です。\n' ;;
        esac
    done
}

MODE=install
RUNTIME_MODE=hermes
RUNTIME_EXPLICIT=0
RECOVERY_OPTION=""
RECOVERY_PYTHON=""
SKIP_BREW=0; SKIP_LLM=0; SKIP_PLUGIN=0; SKIP_SERVICES=0; SKIP_RECOVERY=0
FORCE_REPO=0
ARG_HOME=""
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)       [ $# -ge 2 ] || { printf '%s\n' '--mode needs hermes or standalone' >&2; exit 2; }
                      RUNTIME_MODE="$2"; RUNTIME_EXPLICIT=1; shift
                      case "$RUNTIME_MODE" in hermes|standalone) ;; *) printf '%s\n' 'invalid --mode' >&2; exit 2 ;; esac ;;
        --no-brew)     SKIP_BREW=1 ;;
        --recovery-python) if [ $# -lt 2 ] || [ -z "$2" ]; then printf '%s\n' '--recovery-python needs an absolute path' >&2; exit 2; fi
                      RECOVERY_OPTION="$2"; shift ;;
        --no-llm)      SKIP_LLM=1 ;;
        --no-plugin)   SKIP_PLUGIN=1 ;;
        --no-services) SKIP_SERVICES=1 ;;
        --no-recovery) SKIP_RECOVERY=1 ;;
        --force-repo)  FORCE_REPO=1 ;;
        --preflight|--check-only) MODE=preflight ;;
        --dry-run)     MODE=dry-run ;;
        -h|--help)     usage; exit 0 ;;
        -*) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
        *)  if [ -z "$ARG_HOME" ]; then ARG_HOME="$1"
            else printf 'unexpected argument: %s\n' "$1" >&2; exit 2; fi ;;
    esac
    shift
done

# Ask only on a fresh interactive install. Existing JSON is parsed below
# without grep, so malformed or unsupported modes cannot silently fall back.
if [ "$RUNTIME_EXPLICIT" -eq 0 ] && [ ! -e "$HOME/.mcs/config.json" ] \
        && [ ! -L "$HOME/.mcs/config.json" ] \
        && [ "$MODE" = install ] && [ -t 0 ] && [ -t 1 ]; then
    printf '%s\n' "MCS の導入を始めます。番号を入力して Enter キーで進みます。" \
        "Slack / Discord との連携方式を選んでください。" \
        "  1) わからないので推奨を使う — Hermes Agent と連携（標準）" \
        "  2) MCS 単独で使う — Hermes Agent を導入しない" \
        "LINE WORKS を使う場合も、通知先は後の初期設定で選べます。"
    choose '選択 [1・推奨 / 2 / 0・中止]: '
    case "$_answer" in
        2) RUNTIME_MODE=standalone ;;
        1) RUNTIME_MODE=hermes ;;
    esac
    RUNTIME_EXPLICIT=1
fi

say()  { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  完了: %s\n' "$*"; }
skip() { printf '  省略: %s\n' "$*"; }
warn() { printf '  注意: %s\n' "$*" >&2; }
die()  { printf '停止: %s\n' "$*" >&2; exit 1; }
SUMMARY=""
sum()  { SUMMARY="$SUMMARY  $*
"; }

# lexical absolute path without trailing slash; an existing directory is
# cleaned of ./.. via cd (logical — ~/.hermes may itself be a symlink)
abspath() {
    case "$1" in /*) _p="$1" ;; *) _p="$PWD/$1" ;; esac
    if [ -d "$_p" ]; then _p="$(CDPATH='' cd -- "$_p" && pwd)"; fi
    while [ "${#_p}" -gt 1 ] && [ "${_p%/}" != "$_p" ]; do _p="${_p%/}"; done
    printf '%s\n' "$_p"
}
# physical path of an existing file or directory (symlinks resolved)
physpath() {
    if [ -d "$1" ]; then (CDPATH='' cd -- "$1" && pwd -P)
    else
        _d="$(CDPATH='' cd -- "$(dirname -- "$1")" && pwd -P)" || return 1
        printf '%s/%s\n' "$_d" "$(basename -- "$1")"
    fi
}
# physical path a symlink points at (one level, relative targets allowed)
link_target() {
    _t="$(readlink "$1")" || return 1
    case "$_t" in /*) ;; *) _t="$(dirname -- "$1")/$_t" ;; esac
    [ -e "$_t" ] || return 1
    physpath "$_t"
}

REPO="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)"
if [ "${MCS_ROOT+x}" = x ]; then
    [ -n "$MCS_ROOT" ] || die "MCS_ROOT is empty; unset MCS_ROOT before installing (data root is ~/.mcs)"
    case "$MCS_ROOT" in
        \~/*) _mcs_root="$HOME/${MCS_ROOT#\~/}" ;;
        *) _mcs_root="$MCS_ROOT" ;;
    esac
    _mcs_root="$(abspath "$_mcs_root")"
    # Match the runtime's lexical normalization before resolving symlinks.
    _mcs_root="$(awk 'BEGIN {
        n = split(ARGV[1], parts, "/"); top = 0
        for (i = 1; i <= n; i++) {
            if (parts[i] == "" || parts[i] == ".") continue
            if (parts[i] == "..") { if (top > 0) top--; continue }
            stack[++top] = parts[i]
        }
        out = ""; for (i = 1; i <= top; i++) out = out "/" stack[i]
        print (out == "" ? "/" : out)
    }' "$_mcs_root")"
    _mcs_default="$(physpath "$HOME/.mcs")"
    _mcs_root="$(physpath "$_mcs_root" 2>/dev/null)" \
        || die "MCS_ROOT is unsupported by the installer; unset MCS_ROOT (data root is ~/.mcs)"
    [ "$_mcs_root" = "$_mcs_default" ] \
        || die "MCS_ROOT is unsupported by the installer; unset MCS_ROOT (data root is ~/.mcs)"
fi
MCS_ROOT="$(abspath "$HOME")/.mcs"
export MCS_ROOT
if [ -e "$HOME/.mcs/config.json" ] || [ -L "$HOME/.mcs/config.json" ]; then
    MODE_PY="$HOME/.mcs/venv/bin/python3"
    if [ ! -x "$MODE_PY" ]; then MODE_PY="$(command -v python3 || true)"; fi
    [ -n "$MODE_PY" ] || die "existing config requires Python to select its runtime mode; pass --mode explicitly"
    RUNTIME_MODE="$("$MODE_PY" - "$HOME/.mcs/config.json" "$RUNTIME_EXPLICIT" "$RUNTIME_MODE" <<'PYMODE'
import json
import os
import stat
import sys
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError
        with os.fdopen(fd, encoding="utf-8", closefd=False) as stream:
            cfg = json.load(stream)
    finally:
        os.close(fd)
    if not isinstance(cfg, dict):
        raise ValueError
    mode = sys.argv[3] if sys.argv[2] == "1" else cfg.get("runtime_mode", "hermes")
    if mode not in ("hermes", "standalone"):
        raise ValueError
    print(mode)
except (OSError, ValueError, AttributeError, TypeError, RecursionError):
    raise SystemExit(1) from None
PYMODE
)" || die "config.json unreadable: cannot select runtime from existing config; repair it (use --mode only to override a readable object's runtime)"
    case "$RUNTIME_MODE" in hermes|standalone) ;; *) die "runtime mode could not be read; pass --mode explicitly" ;; esac
fi
DEFAULT_HERMES_HOME="$(abspath "$HOME")/.hermes"
ENV_HERMES_HOME="${HERMES_HOME:-}"
if [ -n "$ARG_HOME" ]; then HERMES_HOME="$(abspath "$ARG_HOME")"
else HERMES_HOME="$DEFAULT_HERMES_HOME"; fi
if [ -n "$ENV_HERMES_HOME" ] \
        && [ "$(abspath "$ENV_HERMES_HOME")" != "$HERMES_HOME" ]; then
    warn "HERMES_HOME=$ENV_HERMES_HOME from your environment is NOT used — installing into $HERMES_HOME (pass it as the argument to install there)"
fi
# every hermes command below must act on the home being installed
export HERMES_HOME
RUNTIME_HOME="$HERMES_HOME"
if [ "$RUNTIME_MODE" = standalone ]; then
    [ -z "$ARG_HOME" ] || die "standalone mode does not accept HERMES_HOME"
    RUNTIME_HOME="$HOME/.mcs"
    SKIP_PLUGIN=1
fi

HERMES_DIR="$HERMES_HOME/hermes-agent"
HERMES_REPO="https://github.com/yusuketakuma/hermes-agent.git"
# Pinned: MCS companion based on upstream v0.21.7.
# Native context/attestation and async pre-dispatch contracts remain required.
HERMES_PIN="f36c1da0016ce14c9c531cb9514ab4b0b667240d"
# Pins validated by earlier MCS releases; only these move to
# HERMES_PIN, and only during an updater-proven runtime migration.
HERMES_PREVIOUS_PINS="6f35b1ed84536f7e7fd2f605f7779dd051cce05f fd50a275e2616118c48fe07e7e1c878782b15ccd 1e8fbfe6b41399a46e232f6cde5618cfdfa2729a 16390dc80d248b7b49554c0cde52caf22e1fc3d8"
CLONE_MARK="$HERMES_HOME/.hermes-agent.install-in-progress"
VENV="$HERMES_DIR/venv"
if [ "$RUNTIME_MODE" = standalone ]; then VENV="$RUNTIME_HOME/venv"; fi
VENV_PY="$VENV/bin/python"
if [ "$RUNTIME_MODE" = standalone ]; then VENV_PY="$VENV/bin/python3"; fi
VENV_HERMES="$VENV/bin/hermes"
PIP_MARK="$VENV/.mcs-pip-incomplete"
HERMES_BIN_DIR="$HOME/.local/bin"
SHIM="$HERMES_BIN_DIR/hermes"
MCS_SHIM="$HERMES_BIN_DIR/mcs"
PLUGIN_LINK="$HERMES_HOME/plugins/mcs-discord-commands"
LLM_MODELS_URL="http://127.0.0.1:8080/v1/models"
MODEL_DIR="$RUNTIME_HOME/models"
MODEL_FILE="$MODEL_DIR/Qwen3.5-9B-Q4_K_M.gguf"
# Pinned revision and its LFS sha256 (Hugging Face API + resolve headers,
# 2026-10-11); a floating main could swap the bytes that read PHI bodies.
MODEL_URL="https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/3885219b6810b007914f3a7950a8d1b469d598a5/Qwen3.5-9B-Q4_K_M.gguf"
MODEL_SHA256="${MCS_MODEL_SHA256:-03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8}"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LLAMA_PLIST="$AGENTS_DIR/ai.mcs.llamaserver.plist"
HERMES_LLAMA_PLIST="$AGENTS_DIR/ai.hermes.llamacpp.plist"
MCS_DATA="$HOME/.mcs/data"
RECOVERY_DIR="$HOME/.mcs-recovery"
WATCH_PLIST="$AGENTS_DIR/org.mcs.recovery.plist"
RECOVERY_TEMPLATE="$REPO/deployment/launchagents/org.mcs.recovery.plist"
SETUP="$REPO/mcs/ops/mcs_setup.py"
# shellcheck disable=SC2016  # printed for the user, never run here
BREW_INSTALL='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
OS="$(uname -s)"
IS_ROOT=0; [ "$(id -u)" -ne 0 ] || IS_ROOT=1

# Quote data for the generated shell shim and XML/sed replacements.
shell_quote() {
    printf "'"
    printf '%s' "$1" | sed "s/'/'\\\\''/g"
    printf "'"
}
xml_sed() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' \
        -e 's/>/\&gt;/g' -e 's/[\\&|]/\\&/g'
}
# <interpreter> <min minor> <max minor, exclusive> — Python 3.x in range
py_ok() {
    case "$1" in
        */*) [ -x "$1" ] || return 1 ;;
        *)   command -v "$1" >/dev/null 2>&1 || return 1 ;;
    esac
    "$1" -c "import sys; sys.exit(0 if (3, $2) <= sys.version_info[:2] < (3, $3) else 1)" \
        >/dev/null 2>&1
}
# hermes-agent requires >=3.14,<3.15 — prefer an explicitly versioned
# interpreter over a possibly-too-new python3
find_py() {
    for _c in python3.14 python3; do
        if py_ok "$_c" 14 15; then command -v "$_c"; return 0; fi
    done
    return 1
}

llm_serving() {
    _probe_py="${MCS_PY:-}"
    if [ -z "$_probe_py" ]; then _probe_py="$(command -v python3 || true)"; fi
    [ -n "$_probe_py" ] || return 1
    curl -sf --noproxy "*" --max-filesize 1048576 -m 3 "$LLM_MODELS_URL" 2>/dev/null | "$_probe_py" -c '
# mcs-model-list-probe
import json, sys
try:
    value = json.load(sys.stdin)
    rows = value.get("data") if isinstance(value, dict) else None
    valid = isinstance(rows, list) and any(
        isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].strip()
        for row in rows)
except (ValueError, RecursionError):
    valid = False
raise SystemExit(0 if valid else 1)
' 2>/dev/null
}

# Updater-proven runtime migration: the inherited run.lock FD and the
# journaled backup path must both verify; prints the backup path.
migration_backup() {
    _mb_py="${PYBIN:-$(find_py || true)}"
    [ -n "$_mb_py" ] || return 1
    "$_mb_py" -I -c '
# mcs-runtime-migration-lock
import fcntl, os, re, stat, sys
try:
    number = os.environ.get("MCS_UPDATE_RUN_LOCK_FD", "")
    if not number.isdecimal() or int(number) < 3:
        raise ValueError
    fd = int(number)
    lock = os.lstat(sys.argv[2])
    held = os.fstat(fd)
    if (not stat.S_ISREG(lock.st_mode) or not stat.S_ISREG(held.st_mode)
            or lock.st_uid != os.getuid() or held.st_uid != os.getuid()
            or (lock.st_dev, lock.st_ino) != (held.st_dev, held.st_ino)):
        raise ValueError
    backup = os.environ.get("MCS_UPDATE_RUNTIME_BACKUP", "")
    if (not os.path.isabs(backup) or os.path.lexists(backup)
            or not re.fullmatch(re.escape(sys.argv[1]) + r"\.pre-python314(?:\.[1-9][0-9]*)?", backup)):
        raise ValueError
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print(backup)
except (OSError, ValueError, OverflowError):
    raise SystemExit(1) from None
' "$VENV" "$MCS_DATA/run.lock"
}

# [project].requires-python of the Hermes checkout, judged by the Python 3.14
# that builds the venv (the same script mcs_update's preflight runs).
hermes_admits_314() {
    _ha_py="${PYBIN:-$(find_py || true)}"
    [ -n "$_ha_py" ] || return 1
    "$_ha_py" -I "$REPO/mcs/ops/requires_python_admits.py" "$HERMES_DIR/pyproject.toml"
}

preserve_old_venv() {
    if [ ! -e "$VENV" ] && [ ! -L "$VENV" ]; then return; fi
    if [ -n "${MCS_UPDATE_RUN_LOCK_FD:-}" ] || [ -n "${MCS_UPDATE_RUNTIME_BACKUP:-}" ]; then
        _prior_venv="$(migration_backup)" \
            || die "runtime migration lock or backup is invalid — existing venv retained"
    else
        # A scheduled tick can start after a process check; established installs
        # require the updater's inherited run.lock for runtime migration.
        if [ -e "$MCS_DATA/ledger.db" ] || [ -e "$MCS_DATA/service_manifest.json" ]; then
            die "existing MCS installation requires a coordinated Python runtime migration — existing venv retained; follow the release upgrade procedure"
        fi
        _runtime_processes="$(ps -axo command=)" \
            || die "cannot verify runtime is stopped — existing venv retained"
        if printf '%s\n' "$_runtime_processes" | MCS_VENV_PREFIX="$VENV/" awk '
            index($0, ENVIRON["MCS_VENV_PREFIX"]) == 1 { busy=1 }
            END { exit !busy }'; then
            die "existing runtime is still in use — stop its gateway/standalone service with its normal stop command, then re-run; existing venv retained"
        fi
        unset _runtime_processes
        _prior_venv="$VENV.pre-python314"
        _prior_n=0
        while [ -e "$_prior_venv" ] || [ -L "$_prior_venv" ]; do
            _prior_n=$((_prior_n + 1))
            _prior_venv="$VENV.pre-python314.$_prior_n"
        done
    fi
    mv "$VENV" "$_prior_venv" || die "old runtime could not be preserved — nothing rebuilt"
    ok "previous runtime preserved: $_prior_venv"
}

recovery_runtime_ok() {  # <probe host Python>; never substitutes the template's executable
    "$1" -I -B -c '
import os, plistlib, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/mcs")
import _mcs_path
import mcs_setup
try:
    cfg = mcs_setup._owner_config(sys.argv[3])
except ValueError as exc:
    raise SystemExit("recovery config " + str(exc)) from None
if sys.argv[4]:
    cfg["recovery_python"] = sys.argv[4]
if sys.argv[5] == "1" and "recovery_python" not in cfg:
    sys.exit(0)
desired = mcs_setup._recovery_python(cfg)
problem = mcs_setup._recovery_python_problem(desired, sys.argv[1])
if problem:
    raise SystemExit("recovery " + problem)
if "recovery_python" in cfg and os.path.lexists(
        Path(mcs_setup.AGENTS_DIR, mcs_setup.RECOVERY_LABEL + ".plist")):
    with open(Path(mcs_setup.AGENTS_DIR, mcs_setup.RECOVERY_LABEL + ".plist"), "rb") as stream:
        deployed = mcs_setup._recovery_executable(plistlib.load(stream))
    if deployed != desired:
        raise SystemExit("existing recovery selection drift: use mcs setup init --yes then mcs setup services for owned idle repair")
from html import escape
text = Path(sys.argv[2]).read_text()
rendered = mcs_setup._render_template(text, {"RECOVERY_PYTHON":escape(desired, quote=False)})
actual = mcs_setup._recovery_executable(plistlib.loads(rendered.encode()))
problem = mcs_setup._recovery_python_problem(actual, sys.argv[1])
if problem:
    raise SystemExit("recovery template " + problem)
if "recovery_python" in cfg and actual != desired:
    raise SystemExit("recovery template target differs from explicit selection")
facts = mcs_setup._runtime_probe(actual) if actual is not None else None
problem = mcs_setup._runtime_problem(facts, recovery=True)
if problem:
    print("recovery " + problem, file=sys.stderr)
    sys.exit(1)
print(desired)
' "$REPO" "$RECOVERY_TEMPLATE" "$HOME/.mcs/config.json" "$RECOVERY_OPTION" "$SKIP_RECOVERY"
}
loaded() { launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1; }
# launchd may still be tearing down a just-booted-out job ("5: Input/
# output error") — retry briefly; success is the label being loaded, not
# a zero exit (same semantics as mcs_util.launchd_bootstrap).
bootstrap_agent() {  # <label> <plist>
    _n=0
    while [ "$_n" -lt 3 ]; do
        launchctl bootstrap "gui/$(id -u)" "$2" && break
        _n=$((_n + 1))
        sleep 1
    done
    loaded "$1"
}
# <tmp> <dst>: publish tmp over dst (rename) only when the content
# differs; returns 0 when dst changed
publish() {
    if [ -f "$2" ] && cmp -s "$1" "$2"; then
        rm -f "$1" || die "cannot remove temporary file $1 — repair permissions, then re-run install.sh"
        return 1
    fi
    # Called in an if: POSIX shells disable errexit throughout this function.
    mv -f "$1" "$2" || die "cannot publish $2 — repair permissions or disk space, then re-run install.sh"
}
# the checkout an existing install points at, when it is not $REPO
# (a recorded checkout that no longer exists is not a conflict)
repo_conflict() {
    if [ -f "$RECOVERY_DIR/repo_path" ]; then
        _r="$(sed -n 1p "$RECOVERY_DIR/repo_path")"
        if [ -d "$_r" ] && [ "$(physpath "$_r")" != "$REPO" ]; then
            printf '%s\n' "$_r"; return 0
        fi
    fi
    if [ "$RUNTIME_MODE" = hermes ] && [ -L "$PLUGIN_LINK" ] && _t="$(link_target "$PLUGIN_LINK")" \
            && [ "$_t" != "$REPO/hermes_plugin" ]; then
        dirname -- "$_t"
    fi
}
# why ~/.local/bin/mcs blocks the launcher step: "foreign" (not an MCS
# lifecycle launcher), "other" (another checkout's launcher) or nothing
mcs_shim_conflict() {
    [ -e "$MCS_SHIM" ] || [ -L "$MCS_SHIM" ] || return 0
    if ! { [ -f "$MCS_SHIM" ] && [ ! -L "$MCS_SHIM" ] \
        && grep -qx '# MCS lifecycle launcher' "$MCS_SHIM"; }; then
        printf 'foreign\n'
    elif ! grep -qF "$(shell_quote "$REPO/mcs/ops/mcs_cli.py")" "$MCS_SHIM"; then
        printf 'other\n'
    fi
}

# ------------------------------------------------------------ preflight
PF_NG=0; PF_WARN=0
pf_ok()   { printf '  OK    %s\n' "$1"; }
pf_info() { printf '  --    %s\n' "$1"; }
pf_warn() {
    printf '  WARN  %s\n' "$1"; PF_WARN=$((PF_WARN + 1))
    if [ -n "${2:-}" ]; then printf '        fix: %s\n' "$2"; fi
}
pf_ng() {
    printf '  NG    %s\n        fix: %s\n' "$1" "$2"; PF_NG=$((PF_NG + 1))
}
pf_net() {  # <label> <url> <needed: 1 = blocker when unreachable>
    if curl -sSfIL -m 10 -o /dev/null "$2" >/dev/null 2>&1; then
        pf_ok "network: $1 reachable"
    elif [ "$3" -eq 1 ]; then
        pf_ng "network: cannot reach $1" "check network/proxy settings, then: curl -I $2"
    else
        pf_warn "network: cannot reach $1 (not needed right now)"
    fi
}

preflight() {
    say "導入前の確認（preflight・変更は行いません）"
    pf_info "runtime mode: $RUNTIME_MODE (change with --mode hermes|standalone)"
    if [ "$IS_ROOT" -eq 1 ]; then
        pf_ng "running as root" "re-run as your normal user, without sudo"
    else
        pf_ok "running as a normal user"
    fi
    if [ "$OS" = Darwin ]; then
        _v="$(sw_vers -productVersion 2>/dev/null || echo unknown)"
        case "${_v%%.*}" in
            ''|*[!0-9]*) pf_warn "macOS version unknown ($_v)" ;;
            *) if [ "${_v%%.*}" -ge 13 ]; then pf_ok "macOS $_v"
               else pf_ng "macOS $_v is too old (Homebrew/llama.cpp need 13+)" "upgrade macOS (System Settings > General > Software Update)"; fi ;;
        esac
        _arch="$(uname -m)"
        if [ "$_arch" = arm64 ]; then pf_ok "arch $_arch (Apple silicon)"
        else pf_warn "arch $_arch — the local LLM runs without Metal and is slow" ""; fi
        if xcode-select -p >/dev/null 2>&1; then pf_ok "Xcode Command Line Tools"
        else pf_ng "Xcode Command Line Tools missing" "xcode-select --install"; fi
    else
        pf_warn "not macOS ($OS) — the llama-server LaunchAgent (4) and watchdog (6) are skipped" ""
    fi

    if [ "$SKIP_BREW" -eq 1 ]; then pf_info "Homebrew not used (--no-brew)"
    elif command -v brew >/dev/null 2>&1; then pf_ok "Homebrew: $(command -v brew)"
    else pf_ng "Homebrew not installed" "$BREW_INSTALL"
    fi
    _brew_fills=0
    if [ "$SKIP_BREW" -eq 0 ] && command -v brew >/dev/null 2>&1; then _brew_fills=1; fi
    if _py="$(find_py)"; then pf_ok "Python 3.14: $_py"
    elif [ "$_brew_fills" -eq 1 ]; then pf_info "no Python 3.14 yet — stage 1 installs python@3.14"
    else pf_ng "no Python 3.14 on PATH" "brew install python@3.14"; fi
    if [ -n "$RECOVERY_OPTION" ] || { [ "$OS" = Darwin ] && [ "$SKIP_RECOVERY" -eq 0 ]; }; then
        if [ -n "$_py" ] && RECOVERY_PYTHON="$(recovery_runtime_ok "$_py")"; then
            pf_ok "recovery template interpreter: Python >=3.14 and safe SQLite"
        elif [ -z "$_py" ] && [ -z "$RECOVERY_OPTION" ] && [ "$_brew_fills" -eq 1 ]; then
            # same as the install path: stage 6 re-verifies after stage 1 adds Python
            pf_warn "recovery interpreter is checked in stage 6 after python@3.14 is installed" ""
        else
            pf_ng "recovery template interpreter unsafe or unverified" \
                "select an existing safe independent executable with --recovery-python; no fallback or binary installation is performed"
        fi
    fi
    _need_clone=0
    if [ "$RUNTIME_MODE" = hermes ] && { [ -f "$CLONE_MARK" ] || [ ! -e "$HERMES_DIR" ]; }; then _need_clone=1; fi
    if command -v git >/dev/null 2>&1; then pf_ok "git: $(command -v git)"
    elif [ "$_brew_fills" -eq 1 ]; then pf_info "git missing — stage 1 installs it"
    elif [ "$_need_clone" -eq 1 ]; then pf_ng "git missing" "xcode-select --install   (or: brew install git)"
    fi

    _need_gb=12
    if [ "$SKIP_LLM" -eq 1 ] || [ -f "$MODEL_FILE" ]; then _need_gb=6; fi
    _free_kb="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
    case "$_free_kb" in
        ''|*[!0-9]*) pf_warn "disk: free space on $HOME unknown" "" ;;
        *) if [ "$_free_kb" -ge $((_need_gb * 1024 * 1024)) ]; then
               pf_ok "disk: $((_free_kb / 1024 / 1024)) GB free (need ~$_need_gb GB)"
           else
               pf_ng "disk: only $((_free_kb / 1024 / 1024)) GB free on $HOME (need ~$_need_gb GB incl. the 5.7 GB model)" "free up disk space, then re-run"
           fi ;;
    esac
    pf_net github.com https://github.com "$_need_clone"
    _need_hf=0
    if [ "$SKIP_LLM" -eq 0 ] && [ "$OS" = Darwin ] && [ ! -f "$MODEL_FILE" ]; then _need_hf=1; fi
    pf_net huggingface.co "$MODEL_URL" "$_need_hf"

    if [ "$RUNTIME_MODE" = standalone ]; then
        pf_info "standalone runtime: $VENV (Hermes is not required)"
        pf_net pypi.org https://pypi.org 1
    else
    if command -v hermes >/dev/null 2>&1; then pf_info "hermes on PATH: $(command -v hermes)"
    else pf_info "hermes not on PATH yet — stage 2 writes $SHIM"; fi
    if [ -f "$CLONE_MARK" ]; then
        pf_info "interrupted hermes-agent clone at $HERMES_DIR — stage 2 redoes it"
    elif [ -e "$HERMES_DIR/.git" ]; then
        if ! _h="$(git -C "$HERMES_DIR" rev-parse HEAD 2>/dev/null)"; then
            pf_ng "cannot read HEAD of $HERMES_DIR" "mv $(shell_quote "$HERMES_DIR") $(shell_quote "$HERMES_DIR.bak")   # stage 2 then re-clones"
        elif [ "$_h" = "$HERMES_PIN" ]; then pf_ok "hermes-agent checkout at the pinned commit"
        else pf_warn "hermes-agent checkout is at $_h, not the validated pin $HERMES_PIN (kept as is)" "git -C $(shell_quote "$HERMES_DIR") checkout --detach $HERMES_PIN"; fi
    elif [ -e "$HERMES_DIR" ]; then
        pf_ng "$HERMES_DIR exists but is not a git checkout" "mv $(shell_quote "$HERMES_DIR") $(shell_quote "$HERMES_DIR.bak")"
    else pf_info "no hermes-agent checkout yet — stage 2 clones $HERMES_PIN"; fi
    if py_ok "$VENV_PY" 14 15; then pf_ok "hermes venv python: $VENV_PY"
    elif [ -e "$VENV" ]; then pf_info "hermes venv has no usable Python 3.14 — stage 2 recreates it"
    else pf_info "no hermes venv yet — stage 2 creates it"; fi
    fi

    if [ -f "$REPO/.git" ]; then
        pf_warn "running from a git worktree ($REPO) — services and the plugin will point here and break if it is removed" "run install.sh from the main checkout"
    fi
    _c="$(repo_conflict)"
    if [ -n "$_c" ] && [ "$FORCE_REPO" -eq 0 ]; then
        pf_ng "existing install points at another checkout: $_c" "run $(shell_quote "$_c/install.sh"), or re-run with --force-repo to switch to $REPO"
    elif [ -n "$_c" ]; then
        pf_warn "--force-repo: will switch the install from $_c to $REPO" ""
    fi
    case "$(mcs_shim_conflict)" in
        foreign) pf_ng "$MCS_SHIM exists and is not an MCS lifecycle launcher" "mv $(shell_quote "$MCS_SHIM") $(shell_quote "$MCS_SHIM.bak")   # it is never overwritten" ;;
        other) if [ "$FORCE_REPO" -eq 0 ]; then
                   pf_ng "$MCS_SHIM points at another checkout" "re-run with --force-repo to switch it to $REPO"
               else pf_warn "--force-repo: will switch $MCS_SHIM to $REPO" ""; fi ;;
    esac
    if [ "$RUNTIME_MODE" = hermes ] && [ "$HERMES_HOME" != "$DEFAULT_HERMES_HOME" ] && [ "$SKIP_SERVICES" -eq 0 ]; then
        pf_ng "custom HERMES_HOME ($HERMES_HOME) is not supported by the services stage" "use the default ~/.hermes, or add --no-services"
    fi

    if [ "$OS" = Darwin ]; then
        if [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then pf_ok "Google Chrome installed"
        elif [ "$_brew_fills" -eq 1 ]; then pf_info "Google Chrome missing — stage 1 installs it"
        else pf_warn "Google Chrome missing (MCS login needs it)" "brew install --cask google-chrome"; fi
    fi
    if llm_serving; then
        pf_ok "port 8080: an OpenAI-compatible server answers"
    elif command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
        if [ "$SKIP_LLM" -eq 1 ]; then
            pf_warn "port 8080: something listens but /v1/models does not answer yet" ""
        else
            pf_ng "port 8080 is taken by a process that is not an LLM server" "lsof -nP -iTCP:8080 -sTCP:LISTEN   # stop it, or pass --no-llm"
        fi
    elif [ "$SKIP_LLM" -eq 1 ]; then
        pf_warn "port 8080: nothing answers — with --no-llm keep your own server on 127.0.0.1:8080" ""
    else pf_ok "port 8080 free (stage 4 starts llama-server there)"; fi

    printf '\npreflight: %s blocker(s), %s warning(s)\n' "$PF_NG" "$PF_WARN"
}

plan_line() { printf '  %-14s %s\n' "$1" "$2"; }
state() { if [ -e "$1" ] || [ -L "$1" ]; then printf 'exists'; else printf 'new'; fi; }
dry_run_plan() {
    say "作業予定（--dry-run・実際の変更は行いません）"
    plan_line "launcher" "$HOME/.local/bin/mcs -> selected runtime + $REPO/mcs/ops/mcs_cli.py"
    if [ "$SKIP_BREW" -eq 1 ]; then plan_line "1/6 brew" "skipped (--no-brew)"
    else plan_line "1/6 brew" "brew install any of: git python@3.14 uv llama.cpp, cask google-chrome (installed ones are skipped)"; fi
    if [ "$RUNTIME_MODE" = standalone ]; then
        plan_line "2/6 standalone" "venv $VENV + official SDKs from deployment/requirements-standalone.txt; runtime_mode=standalone"
    else
    plan_line "2/6 hermes" "checkout $HERMES_DIR [$(state "$HERMES_DIR")] @ $HERMES_PIN (existing checkouts are kept)"
    plan_line "" "venv $VENV [$(state "$VENV_PY")] + pip install -e hermes-agent[messaging]"
    plan_line "" "shim $SHIM [$(state "$SHIM")] (a foreign file there is left alone)"
    fi
    if [ "$RUNTIME_MODE" = standalone ]; then plan_line "3/6 plugin" "not used in standalone mode"
    elif [ "$SKIP_PLUGIN" -eq 1 ]; then plan_line "3/6 plugin" "skipped (--no-plugin; LINE WORKS does not use it)"
    else plan_line "3/6 plugin" "symlink $PLUGIN_LINK [$(state "$PLUGIN_LINK")] -> $REPO/hermes_plugin; hermes plugins enable mcs-discord-commands"; fi
    if [ "$SKIP_LLM" -eq 1 ] || [ "$OS" != Darwin ]; then plan_line "4/6 llm" "skipped"
    else
        # mirror stage 4's order: a hermes-managed agent or a foreign
        # server on :8080 is kept — no second llama-server is planned
        if [ -f "$HERMES_LLAMA_PLIST" ]; then
            plan_line "4/6 llm" "hermes-managed $HERMES_LLAMA_PLIST [exists] is kept (load state checked; no model download, no second server)"
        elif llm_serving \
                && [ ! -f "$LLAMA_PLIST" ] && ! loaded ai.mcs.llamaserver; then
            plan_line "4/6 llm" "a server already answers on :8080 — kept (nothing installed)"
        else
            plan_line "4/6 llm" "model $MODEL_FILE [$(state "$MODEL_FILE")] (~5.7 GB download when new)"
            plan_line "" "plist $LLAMA_PLIST [$(state "$LLAMA_PLIST")]; launchctl bootstrap ai.mcs.llamaserver when not loaded"
        fi
    fi
    if [ "$SKIP_SERVICES" -eq 1 ]; then plan_line "5/6 services" "skipped (--no-services)"
    else
        plan_line "5/6 services" "mkdir $MCS_DATA{,/cmd,/cmd_int} (mode 700)"
        plan_line "" "$VENV_PY $SETUP services (selected runtime supervisor)"
    fi
    if [ "$SKIP_RECOVERY" -eq 1 ]; then plan_line "6/6 recovery" "skipped (--no-recovery)"
    else
        plan_line "6/6 recovery" "$RECOVERY_DIR/mcs_recover.py [$(state "$RECOVERY_DIR/mcs_recover.py")], repo_path = $REPO"
        plan_line "" "plist $WATCH_PLIST [$(state "$WATCH_PLIST")]; (re)load org.mcs.recovery only when changed or not loaded"
    fi
    plan_line "opt-outs" "$MCS_DATA/install_options.json [$(state "$MCS_DATA/install_options.json")] (the update plan shows it as install_args; pass each as --install-arg)"
}

if [ "$MODE" != install ]; then
    preflight
    if [ "$MODE" = dry-run ]; then dry_run_plan; fi
    [ "$PF_NG" -eq 0 ] || exit 1
    exit 0
fi

# ---------------------------------------------------------- guards
[ "$IS_ROOT" -eq 0 ] || die "sudo や管理者 root として実行しないでください。普段のユーザーで、sudo を付けずにやり直してください"
# mcs_setup services renders cron wrappers/plists against ~/.hermes (its
# HERMES_PY and scripts dir are fixed there) — a custom HERMES_HOME would
# install services pointing at an interpreter that does not exist
if [ "$RUNTIME_MODE" = hermes ] && [ "$HERMES_HOME" != "$DEFAULT_HERMES_HOME" ] && [ "$SKIP_SERVICES" -eq 0 ]; then
    printf '%s\n' "custom HERMES_HOME ($HERMES_HOME) is not supported by the services stage;" \
        "use the default ~/.hermes, or pass --no-services and register services yourself" >&2
    exit 2
fi
CONFLICT="$(repo_conflict)"
if [ -n "$CONFLICT" ]; then
    [ "$FORCE_REPO" -eq 1 ] || die "this machine is installed from another checkout ($CONFLICT); run that checkout's install.sh, or re-run with --force-repo to switch the plugin, recovery tool and services to $REPO"
    warn "--force-repo: switching the install from $CONFLICT to $REPO"
fi
case "$(mcs_shim_conflict)" in
    foreign) die "$MCS_SHIM is not an MCS lifecycle launcher; left untouched — move it aside and re-run" ;;
    other) [ "$FORCE_REPO" -eq 1 ] || die "$MCS_SHIM points at another checkout; use --force-repo to switch" ;;
esac
if [ -f "$REPO/.git" ]; then
    warn "running from a git worktree ($REPO) — services and the plugin will point here and break if the worktree is removed; prefer the main checkout"
fi

if [ -n "$RECOVERY_OPTION" ] || { [ "$OS" = Darwin ] && [ "$SKIP_RECOVERY" -eq 0 ]; }; then
    if _probe_host="$(find_py)"; then
        RECOVERY_PYTHON="$(recovery_runtime_ok "$_probe_host")" \
            || die "desired recovery interpreter unsafe/unknown or deployed selection drift — nothing written"
    elif [ -n "$RECOVERY_OPTION" ]; then
        die "--recovery-python is verified before anything is written, which needs Python 3.14 (e.g. brew install python@3.14); install one and re-run — nothing written"
    else
        # A fresh Mac gets Python from stage 1; stage 6 checks the recovery runtime then.
        warn "no Python 3.14 yet — the recovery interpreter is checked in stage 6"
    fi
fi

if [ -t 0 ] && [ -t 1 ]; then
    printf '\nこれから行うこと\n'
    printf '  必要なソフト、通知連携、この Mac で動く AI、自動実行、復旧ツールを準備します。\n'
    printf '  指定した省略オプションは維持します。連携方式: %s\n' "$RUNTIME_MODE"
    printf '  初回はインターネット接続が必要です。AI の取得は約 5.7 GB で時間がかかります。\n'
    printf '  準備後に、MCS のログイン情報と通知先を画面に沿って設定します。\n'
    printf '  1) この内容で導入する（推奨）\n  2) 今は中止する\n'
    choose '選択 [1・推奨 / 2 / 0・中止]: '
    [ "$_answer" = 1 ] || { printf '導入を中止しました。\n'; exit 130; }
fi

# every non-zero exit from here on is a stopped install, not a usage error
trap 'if [ "$?" -ne 0 ]; then
    printf "%s\n" "導入が途中で停止しました。直前のエラーを確認し、原因を解消して同じ操作をやり直してください。設定やデータを削除してやり直す必要はありません。 (installation stopped)" >&2
fi' EXIT

# ---------------------------------------------------------------- 1. brew
say "1/6 必要なソフトを準備（Homebrew packages）"
if [ "$SKIP_BREW" -eq 1 ]; then
    skip "stage skipped (--no-brew) — assuming git/python/uv/llama.cpp/Chrome are already provided"
    sum "1/6 brew:     skipped (--no-brew)"
elif ! command -v brew >/dev/null 2>&1; then
    die "必要なソフトを導入する Homebrew が見つかりません（brew not found）。
次のコマンドで Homebrew を導入してください。Mac のログインパスワードを求められることがあります:
  $BREW_INSTALL
終わったら、もう一度同じ導入操作を行ってください。手順書: docs/guides/EASY_SETUP.md"
else
    _new=""
    for pkg in git python@3.14 uv llama.cpp; do
        if brew list --versions "$pkg" >/dev/null 2>&1; then
            skip "$pkg already installed"
        else
            printf '  install: %s\n' "$pkg"
            (umask 022; brew install "$pkg")
            _new="$_new $pkg"
        fi
    done
    if [ "$OS" != Darwin ]; then
        skip "google-chrome cask: not macOS — install Chrome/Chromium yourself"
    elif brew list --cask --versions google-chrome >/dev/null 2>&1 \
            || [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
        skip "google-chrome already installed"
    else
        printf '  install: %s\n' "google-chrome (cask)"
        (umask 022; brew install --cask google-chrome)
        _new="$_new google-chrome"
    fi
    if [ -n "$_new" ]; then sum "1/6 brew:     installed$_new"
    else sum "1/6 brew:     all packages already present"; fi
fi

# ----------------------------------------------------------- 2. hermes
if [ "$RUNTIME_MODE" = standalone ]; then
    say "2/6 MCS 単独実行の準備（standalone runtime）"
    mkdir -p "$RUNTIME_HOME"
    if ! py_ok "$VENV_PY" 14 15; then
        PYBIN="$(find_py)" || die "Python 3.14 required — brew install python@3.14"
        preserve_old_venv
        if command -v uv >/dev/null 2>&1; then
            uv venv --python "$PYBIN" "$VENV"
        else
            "$PYBIN" -m venv "$VENV"
        fi
        : > "$PIP_MARK"
    fi
    REQUIREMENTS="$REPO/deployment/requirements-standalone.txt"
    [ -f "$REQUIREMENTS" ] || die "standalone requirements file is missing"
    if [ -f "$PIP_MARK" ] || ! cmp -s "$REQUIREMENTS" "$VENV/.mcs-connector-requirements"; then
        : > "$PIP_MARK"
        if command -v uv >/dev/null 2>&1; then
            uv pip install --python "$VENV_PY" -r "$REQUIREMENTS"
        else
            "$VENV_PY" -m pip install -r "$REQUIREMENTS"
        fi
        cp "$REQUIREMENTS" "$VENV/.mcs-connector-requirements.tmp"
        mv -f "$VENV/.mcs-connector-requirements.tmp" "$VENV/.mcs-connector-requirements"
        rm -f "$PIP_MARK"
    fi
    py_ok "$VENV_PY" 14 15 || die "standalone interpreter unavailable after install"
    MCS_PY="$VENV_PY"
    sum "2/6 runtime:  standalone venv and official connector SDKs"
else
say "2/6 Hermes Agent を準備（hermes-agent）"
mkdir -p "$HERMES_HOME"
# checkout: a fresh clone is bracketed by CLONE_MARK, so an interrupted
# clone/checkout is redone on re-run instead of being accepted unpinned.
# An existing checkout without the marker is someone's — never rewritten.
if [ -f "$CLONE_MARK" ] || [ ! -e "$HERMES_DIR" ]; then
    command -v git >/dev/null 2>&1 \
        || die "git is required to install hermes-agent — brew install git (or xcode-select --install), then re-run install.sh"
    : > "$CLONE_MARK"
    rm -rf "$HERMES_DIR"   # only ever our own interrupted clone (marker)
    printf '  clone: %s @%s -> %s\n' "$HERMES_REPO" "$HERMES_PIN" "$HERMES_DIR"
    if ! git clone --filter=blob:none "$HERMES_REPO" "$HERMES_DIR"; then
        rm -rf "$HERMES_DIR"
        git clone "$HERMES_REPO" "$HERMES_DIR" \
            || die "git clone of $HERMES_REPO failed — check network access to github.com, then re-run install.sh"
    fi
    if ! git -C "$HERMES_DIR" checkout -q --detach "$HERMES_PIN"; then
        # Full clones may omit commits reachable only through non-branch refs.
        if ! git -C "$HERMES_DIR" fetch -q origin "$HERMES_PIN" \
            || ! git -C "$HERMES_DIR" checkout -q --detach "$HERMES_PIN"; then
            die "cannot retrieve pinned commit $HERMES_PIN from $HERMES_REPO — verify network access and publication of this companion commit before retrying; venv setup has not started"
        fi
    fi
fi
[ -e "$HERMES_DIR/.git" ] \
    || die "$HERMES_DIR exists but is not a git checkout — move it aside (mv $(shell_quote "$HERMES_DIR") $(shell_quote "$HERMES_DIR.bak")), then re-run install.sh"
HEAD_SHA="$(git -C "$HERMES_DIR" rev-parse HEAD)" \
    || die "cannot read HEAD of $HERMES_DIR — repair it or move it aside, then re-run install.sh"
if [ -f "$CLONE_MARK" ]; then
    [ "$HEAD_SHA" = "$HERMES_PIN" ] \
        || die "fresh checkout is at $HEAD_SHA, not the pinned $HERMES_PIN — re-run install.sh"
    rm -f "$CLONE_MARK"
    ok "hermes-agent cloned (pinned $HERMES_PIN)"
    _ck="cloned at pin $HERMES_PIN"
elif [ -n "${MCS_UPDATE_RUN_LOCK_FD:-}" ] && ! py_ok "$VENV_PY" 14 15 \
        && [ -n "${MCS_UPDATE_KEEP_HERMES_CHECKOUT:-}" ]; then
    # Operator-confirmed (mcs_upgrade --keep-hermes-checkout): keep this exact,
    # clean checkout that already declares Python 3.14 support; never move it.
    [ "$HEAD_SHA" = "$MCS_UPDATE_KEEP_HERMES_CHECKOUT" ] \
        || die "runtime migration: $HERMES_DIR is at $HEAD_SHA, not the confirmed $MCS_UPDATE_KEEP_HERMES_CHECKOUT — existing checkout and venv retained"
    [ -z "$(git -C "$HERMES_DIR" status --porcelain --untracked-files=no)" ] \
        || die "runtime migration: $HERMES_DIR has local changes — existing checkout and venv retained"
    hermes_admits_314 \
        || die "runtime migration: $HERMES_DIR does not declare Python 3.14 support — existing checkout and venv retained"
    migration_backup >/dev/null \
        || die "runtime migration lock or backup is invalid — existing checkout and venv retained"
    ok "keeping the confirmed hermes-agent checkout $HEAD_SHA for the Python 3.14 runtime"
    _ck="kept confirmed checkout $HEAD_SHA"
elif [ "$HEAD_SHA" = "$HERMES_PIN" ]; then
    skip "hermes-agent checkout already at pin $HERMES_PIN"
    _ck="kept existing checkout (pinned)"
elif [ -n "${MCS_UPDATE_RUN_LOCK_FD:-}" ] && ! py_ok "$VENV_PY" 14 15; then
    # Runtime migration: the earlier pins require Python <3.14, so the
    # rebuilt venv needs the current pin. Move only a clean, detached,
    # previously validated checkout, and only once the lock verifies.
    case " $HERMES_PREVIOUS_PINS " in *" $HEAD_SHA "*) ;; *)
        die "runtime migration: $HERMES_DIR is at an unknown commit $HEAD_SHA — move it to $HERMES_PIN yourself, then re-run; existing venv retained" ;;
    esac
    if git -C "$HERMES_DIR" symbolic-ref -q HEAD >/dev/null 2>&1 \
            || [ -n "$(git -C "$HERMES_DIR" status --porcelain --untracked-files=no)" ]; then
        die "runtime migration: $HERMES_DIR is on a branch or has local changes — existing checkout and venv retained"
    fi
    migration_backup >/dev/null \
        || die "runtime migration lock or backup is invalid — existing checkout and venv retained"
    if ! git -C "$HERMES_DIR" fetch -q "$HERMES_REPO" "$HERMES_PIN" \
        || ! git -C "$HERMES_DIR" checkout -q --detach "$HERMES_PIN"; then
        die "runtime migration: could not move $HERMES_DIR to $HERMES_PIN — existing venv retained"
    fi
    [ "$(git -C "$HERMES_DIR" rev-parse HEAD)" = "$HERMES_PIN" ] \
        || die "runtime migration: $HERMES_DIR is not at $HERMES_PIN after checkout"
    ok "hermes-agent moved $HEAD_SHA -> $HERMES_PIN for the Python 3.14 runtime"
    _ck="moved previous pin to $HERMES_PIN"
else
    warn "$HERMES_DIR is at $HEAD_SHA, not pinned $HERMES_PIN"
    warn "  (keeping existing checkout; update deliberately)"
    _ck="kept existing checkout at $HEAD_SHA (NOT the validated pin)"
fi

# venv: converge on re-run — a venv without a usable python is rebuilt,
# and PIP_MARK brackets the package install so a failed one is retried
if ! py_ok "$VENV_PY" 14 15; then
    PYBIN="$(find_py)" \
        || die "Python >=3.14,<3.15 is required for hermes-agent — brew install python@3.14, then re-run install.sh"
    if [ -e "$VENV" ]; then
        warn "$VENV has no usable Python 3.14 — recreating it"
    fi
    preserve_old_venv
    if command -v uv >/dev/null 2>&1; then
        uv venv --python "$PYBIN" "$VENV" || die "uv venv failed — re-run install.sh"
    else
        "$PYBIN" -m venv "$VENV" || die "python -m venv failed — re-run install.sh"
    fi
    : > "$PIP_MARK"
    _venv="venv created"
else
    _venv="venv kept"
fi
if [ -f "$PIP_MARK" ] || [ ! -x "$VENV_HERMES" ]; then
    : > "$PIP_MARK"
    _pip=""
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$VENV_PY" -e "${HERMES_DIR}[messaging]" || _pip=1
    else
        "$VENV_PY" -m pip install -e "${HERMES_DIR}[messaging]" || _pip=1
    fi
    [ -z "$_pip" ] \
        || die "installing hermes-agent into $VENV failed — fix the error above (network/compiler), then re-run install.sh (the install is retried)"
    rm -f "$PIP_MARK"
    _venv="$_venv, hermes-agent installed"
fi
# this interpreter is what stage 5 bakes into every plist/wrapper
py_ok "$VENV_PY" 14 15 \
    || die "$VENV_PY is missing or not Python 3.14 — remove $VENV and re-run install.sh"
[ -x "$VENV_HERMES" ] \
    || die "$VENV_HERMES is missing after the install — remove $VENV and re-run install.sh"
ok "hermes venv: $VENV_PY"
MCS_PY="$VENV_PY"
hermes_cli() { (unset PYTHONPATH PYTHONHOME; exec "$VENV_HERMES" "$@"); }

# shim: published by rename, so an existing symlink is replaced rather
# than written through (which would overwrite its target)
mkdir -p "$HERMES_BIN_DIR"
SHIM_BODY="$(printf '#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\nexec %s "$@"' \
    "$(shell_quote "$VENV_HERMES")")"
_shim=replace
if [ -f "$SHIM" ] && [ ! -L "$SHIM" ]; then
    if [ "$(cat "$SHIM")" = "$SHIM_BODY" ]; then _shim=keep
    elif ! { grep -q 'unset PYTHONHOME' "$SHIM" && grep -qF "$VENV_HERMES" "$SHIM"; }; then
        _shim=foreign
    fi
elif [ -L "$SHIM" ] && [ -e "$SHIM" ] \
        && [ "$(link_target "$SHIM")" != "$(physpath "$VENV_HERMES")" ]; then
    _shim=foreign
elif [ -e "$SHIM" ] && [ ! -L "$SHIM" ]; then
    _shim=foreign
fi
case "$_shim" in
    keep) skip "shim $SHIM up to date"; _shimsum="shim kept" ;;
    foreign)
        warn "$SHIM exists and is not the MCS shim — left untouched; make sure 'hermes' on PATH runs $VENV_HERMES"
        _shimsum="shim NOT written ($SHIM is someone else's)" ;;
    replace)
        printf '%s\n' "$SHIM_BODY" > "$SHIM.tmp.$$"
        chmod 700 "$SHIM.tmp.$$"
        mv -f "$SHIM.tmp.$$" "$SHIM"
        ok "shim: $SHIM -> $VENV_HERMES"
        _shimsum="shim written" ;;
esac
case ":$PATH:" in
    *":$HERMES_BIN_DIR:"*) ;;
    *) warn "$HERMES_BIN_DIR is not on PATH — add it to your shell profile to run 'hermes'" ;;
esac
sum "2/6 hermes:   $_ck; $_venv; $_shimsum"
fi

# Publish a pinned lifecycle entrypoint for both runtimes. A foreign command
# is never overwritten; an MCS shim from another checkout needs --force-repo.
MCS_BODY="$(printf '#!/bin/sh\n# MCS lifecycle launcher\nunset PYTHONPATH PYTHONHOME\nexport MCS_LIFECYCLE_PINNED=1\nexec %s %s "$@"' \
    "$(shell_quote "$MCS_PY")" "$(shell_quote "$REPO/mcs/ops/mcs_cli.py")")"
case "$(mcs_shim_conflict)" in
    foreign) die "$MCS_SHIM is not an MCS lifecycle launcher; left untouched" ;;
    other) [ "$FORCE_REPO" -eq 1 ] || die "$MCS_SHIM points at another checkout; use --force-repo to switch" ;;
esac
mkdir -p "$HERMES_BIN_DIR"
printf '%s\n' "$MCS_BODY" > "$MCS_SHIM.tmp.$$"
chmod 700 "$MCS_SHIM.tmp.$$"
publish "$MCS_SHIM.tmp.$$" "$MCS_SHIM" || :
sum "launcher:    $MCS_SHIM (interpreter $MCS_PY)"
case ":$PATH:" in
    *":$HERMES_BIN_DIR:"*) ;;
    *) warn "$HERMES_BIN_DIR is not on PATH — add it to your shell profile to run 'mcs'" ;;
esac

# Verify the desired target before even the first private config write.
if [ -n "$RECOVERY_OPTION" ] || { [ "$OS" = Darwin ] && [ "$SKIP_RECOVERY" -eq 0 ]; }; then
    RECOVERY_PYTHON="$(recovery_runtime_ok "$MCS_PY")" \
        || die "recovery desired interpreter unsafe or unknown — config not written"
fi
# Persist explicit selections only; never discard an unreadable config.
if [ "$RUNTIME_EXPLICIT" -eq 1 ] || [ -n "$RECOVERY_OPTION" ]; then
    "$MCS_PY" - "$REPO" "$RUNTIME_MODE" "$RECOVERY_OPTION" <<'PYMODE'
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "mcs"))
import _mcs_path
import mcs_setup
from mcs_util import CONF_PATH, atomic_write
try:
    cfg = mcs_setup._owner_config(CONF_PATH)
except ValueError as exc:
    raise SystemExit(f"config.json {exc} — repair it before selecting a runtime") from None
cfg["runtime_mode"] = sys.argv[2]
if sys.argv[3]:
    cfg["recovery_python"] = sys.argv[3]
why = mcs_setup._init_recovery_problem(cfg, sys.argv[1])
if why:
    raise SystemExit("desired recovery " + why + " — config not written")
atomic_write(CONF_PATH, lambda handle: json.dump(cfg, handle, ensure_ascii=False, indent=2), 0o600)
PYMODE
fi

# -------------------------------------------------------- 3. plugin
say "3/6 Slack / Discord の操作ボタンを準備"
if [ "$SKIP_PLUGIN" -eq 1 ]; then
    if [ "$RUNTIME_MODE" = standalone ]; then
        skip "not used in standalone mode — the standalone connector serves the cards"
        sum "3/6 plugin:   not used (standalone)"
    else
        skip "stage skipped (--no-plugin) — Slack/Discord cards need this plugin; re-run without the flag before enabling notify.interactive=slack or discord (notify.interactive=lineworks does not use it — see docs/guides/LINEWORKS.md)"
        sum "3/6 plugin:   skipped (--no-plugin)"
    fi
else
    mkdir -p "$HERMES_HOME/plugins"
    if [ -e "$PLUGIN_LINK" ] && [ ! -L "$PLUGIN_LINK" ]; then
        die "$PLUGIN_LINK exists and is not a symlink — remove it manually, then re-run install.sh"
    fi
    if [ -L "$PLUGIN_LINK" ] && [ "$(readlink "$PLUGIN_LINK")" = "$REPO/hermes_plugin" ]; then
        skip "plugin link already -> $REPO/hermes_plugin"
    else
        ln -sfn "$REPO/hermes_plugin" "$PLUGIN_LINK"
        ok "linked: $PLUGIN_LINK -> $REPO/hermes_plugin"
    fi
    if hermes_cli config get plugins.enabled 2>/dev/null \
            | grep -q "mcs-discord-commands"; then
        skip "plugin already enabled"
    elif hermes_cli plugins enable mcs-discord-commands --no-allow-tool-override; then
        ok "plugin enabled"
    else
        die "plugins enable failed — repair the Hermes CLI/plugin configuration (or add 'mcs-discord-commands' to plugins.enabled in the profile config.yaml), then re-run install.sh"
    fi
    sum "3/6 plugin:   linked + enabled"
fi

# -------------------------------------------------- 4. local LLM server
say "4/6 この Mac で動く AI を準備（llama-server :8080）"
if [ "$SKIP_LLM" -eq 1 ]; then
    skip "stage skipped (--no-llm) — keep your own OpenAI-compatible server answering on 127.0.0.1:8080"
    sum "4/6 llm:      skipped (--no-llm)"
elif [ "$OS" != Darwin ]; then
    skip "not macOS — the llama-server LaunchAgent is macOS-only; serve an OpenAI-compatible endpoint on 127.0.0.1:8080 yourself"
    sum "4/6 llm:      skipped (not macOS)"
else
    SERVING=0
    if llm_serving; then SERVING=1; fi
    if [ -f "$HERMES_LLAMA_PLIST" ]; then
        # hermes manages its own llama.cpp agent — never start a second one
        if loaded ai.hermes.llamacpp; then
            skip "hermes-managed ai.hermes.llamacpp is loaded — it serves :8080"
            sum "4/6 llm:      hermes-managed ai.hermes.llamacpp (loaded)"
        elif [ "$SERVING" -eq 1 ]; then
            skip "ai.hermes.llamacpp is not loaded, but a server answers on :8080 — leaving it"
            sum "4/6 llm:      existing server on :8080 kept"
        else
            warn "hermes-managed $HERMES_LLAMA_PLIST exists but is NOT loaded and nothing answers on :8080 — load it:"
            warn "  launchctl bootstrap gui/$(id -u) $(shell_quote "$HERMES_LLAMA_PLIST")"
            sum "4/6 llm:      WARNING ai.hermes.llamacpp not loaded — see the warning above"
        fi
    elif [ "$SERVING" -eq 1 ] && [ ! -f "$LLAMA_PLIST" ] && ! loaded ai.mcs.llamaserver; then
        skip "an OpenAI-compatible server is already answering on :8080 — leaving it in place"
        sum "4/6 llm:      existing server on :8080 kept"
    else
        LLAMA_BIN="$(command -v llama-server || true)"
        if [ -z "$LLAMA_BIN" ] && command -v brew >/dev/null 2>&1; then
            LLAMA_BIN="$(brew --prefix)/bin/llama-server"
        fi
        if [ -z "$LLAMA_BIN" ] || [ ! -x "$LLAMA_BIN" ]; then
            die "llama-server not found — brew install llama.cpp (or pass --no-llm and serve :8080 yourself), then re-run install.sh"
        fi
        if [ -f "$MODEL_FILE" ]; then
            skip "model already present"
        else
            printf '  AI を取得しています（約 5.7 GB・中断後も再開可能）: %s\n' "$MODEL_FILE"
            mkdir -p "$MODEL_DIR"
            # .part is kept on failure so the next run resumes (-C -).
            # A .part that already holds every byte (interrupted between
            # the transfer and the rename) is not re-requested: a range
            # past the end answers 416 and would fail every re-run.
            _have=0; _want=""
            if [ -f "$MODEL_FILE.part" ]; then
                _have="$(wc -c < "$MODEL_FILE.part" | tr -d ' ')"
                _want="$(curl -fsIL --retry 3 "$MODEL_URL" 2>/dev/null \
                    | tr -d '\r' | awk 'tolower($1)=="content-length:"{v=$2} END{print v}')"
            fi
            if [ -n "$_want" ] && [ "$_have" = "$_want" ]; then
                printf '  partial file already complete (%s bytes)\n' "$_have"
            else
                curl -fL --retry 5 --retry-delay 5 -C - --progress-bar \
                        -o "$MODEL_FILE.part" "$MODEL_URL" \
                    || die "AI の取得が中断しました。ネットワーク接続を確認し、同じ導入操作で再開してください（途中ファイル: $MODEL_FILE.part)"
            fi
            _got="$(shasum -a 256 < "$MODEL_FILE.part" | awk '{print $1}')"
            if [ "$_got" != "$MODEL_SHA256" ]; then
                rm -f "$MODEL_FILE.part"
                die "model checksum mismatch (got $_got) — the partial file was removed; re-run install.sh"
            fi
            mv -f "$MODEL_FILE.part" "$MODEL_FILE"
            ok "model downloaded"
        fi
        mkdir -p "$AGENTS_DIR" "$RUNTIME_HOME/logs"
        sed -e "s|__LLAMA_BIN__|$(xml_sed "$LLAMA_BIN")|g" \
            -e "s|__MODEL__|$(xml_sed "$MODEL_FILE")|g" \
            -e "s|__HERMES_HOME__|$(xml_sed "$RUNTIME_HOME")|g" \
            "$REPO/deployment/launchagents/ai.mcs.llamaserver.plist" \
            > "$LLAMA_PLIST.tmp"
        _changed=0
        if publish "$LLAMA_PLIST.tmp" "$LLAMA_PLIST"; then _changed=1; fi
        if ! loaded ai.mcs.llamaserver; then
            bootstrap_agent ai.mcs.llamaserver "$LLAMA_PLIST" \
                || die "launchctl bootstrap failed for ai.mcs.llamaserver — inspect $LLAMA_PLIST, then re-run install.sh"
            ok "llama-server LaunchAgent started"
            sum "4/6 llm:      llama-server started"
        elif [ "$_changed" -eq 0 ]; then
            skip "ai.mcs.llamaserver already loaded, plist unchanged"
            sum "4/6 llm:      llama-server already running"
        elif [ "$SERVING" -eq 1 ]; then
            # never interrupt a serving model mid-request — tell the user
            warn "ai.mcs.llamaserver plist changed but the running server was kept; apply it when idle:"
            warn "  launchctl bootout gui/$(id -u)/ai.mcs.llamaserver; launchctl bootstrap gui/$(id -u) $(shell_quote "$LLAMA_PLIST")"
            sum "4/6 llm:      plist updated — reload pending (see warning)"
        else
            launchctl bootout "gui/$(id -u)/ai.mcs.llamaserver" 2>/dev/null || true
            bootstrap_agent ai.mcs.llamaserver "$LLAMA_PLIST" \
                || die "launchctl bootstrap failed for ai.mcs.llamaserver — inspect $LLAMA_PLIST, then re-run install.sh"
            ok "llama-server reloaded with the updated plist"
            sum "4/6 llm:      llama-server reloaded (plist changed)"
        fi
    fi
fi

# ------------------------------------------- 5. launchd + hermes cron
say "5/6 自動実行を登録（${RUNTIME_MODE}）"
# the agents stage 5/6 load log to / watch these — they must exist first
# (new ones are 0700 via umask; existing modes are kept)
mkdir -p "$HOME/.mcs" "$MCS_DATA" "$MCS_DATA/cmd" "$MCS_DATA/cmd_int"
if [ "$SKIP_SERVICES" -eq 1 ]; then
    skip "stage skipped (--no-services) — run it later:"
    skip "  $(shell_quote "$MCS_PY") $(shell_quote "$SETUP") services"
    sum "5/6 services: skipped (--no-services)"
elif "$MCS_PY" "$SETUP" services; then
    ok "services installed"
    sum "5/6 services: installed (interpreter $MCS_PY)"
else
    die "services reported problems — see deployment/launchagents/README.md, then re-run install.sh"
fi

# --------------------------------- 6. update recovery (independent)
say "6/6 更新に備えた復旧ツールを登録"
if [ "$SKIP_RECOVERY" -eq 1 ]; then
    skip "stage skipped (--no-recovery)"
    sum "6/6 recovery: skipped (--no-recovery)"
else
    if [ "$OS" = Darwin ]; then
        RECOVERY_PYTHON="$(recovery_runtime_ok "$MCS_PY")" \
            || die "recovery desired interpreter unsafe or unverified — nothing in stage 6 installed; no fallback was selected"
    fi
    mkdir -p "$RECOVERY_DIR"
    RECOVERY_SRC="$REPO/deployment/recovery/mcs_recover.py"
    RECOVERY_DST="$RECOVERY_DIR/mcs_recover.py"
    # preserve the prior generation — on a bad update it may be the only
    # thing that can still run (only when the generation actually changes
    # — a same-release re-run must not overwrite the real previous
    # generation with the current one). Both files are published via
    # tmp + rename so an interrupted install never leaves a truncated
    # tool or .prev behind.
    _rchanged=0
    if ! cmp -s "$RECOVERY_SRC" "$RECOVERY_DST"; then
        if [ -f "$RECOVERY_DST" ]; then
            cp -p "$RECOVERY_DST" "$RECOVERY_DST.prev.tmp"
            mv -f "$RECOVERY_DST.prev.tmp" "$RECOVERY_DST.prev"
        fi
        cp "$RECOVERY_SRC" "$RECOVERY_DST.tmp"
        chmod 700 "$RECOVERY_DST.tmp"
        mv -f "$RECOVERY_DST.tmp" "$RECOVERY_DST"
        _rchanged=1
    fi
    # the tool runs outside the repo — record which checkout it recovers
    printf '%s\n' "$REPO" > "$RECOVERY_DIR/repo_path.tmp"
    if publish "$RECOVERY_DIR/repo_path.tmp" "$RECOVERY_DIR/repo_path"; then _rchanged=1; fi
    ok "recovery tool: $RECOVERY_DST"

    if [ "$OS" = Darwin ]; then
        mkdir -p "$AGENTS_DIR"
        sed -e "s|__RECOVERY__|$(xml_sed "$RECOVERY_DIR")|g" \
            -e "s|__DATA__|$(xml_sed "$MCS_DATA")|g" \
            -e "s|__RECOVERY_PYTHON__|$(xml_sed "$RECOVERY_PYTHON")|g" \
            "$RECOVERY_TEMPLATE" \
            > "$WATCH_PLIST.tmp"
        if publish "$WATCH_PLIST.tmp" "$WATCH_PLIST"; then _rchanged=1; fi
        if [ "$_rchanged" -eq 0 ] && loaded org.mcs.recovery; then
            skip "recovery watchdog already loaded, unchanged"
            sum "6/6 recovery: watchdog already loaded"
        else
            launchctl bootout "gui/$(id -u)/org.mcs.recovery" 2>/dev/null || true
            bootstrap_agent org.mcs.recovery "$WATCH_PLIST" \
                || die "watchdog bootstrap failed — inspect $WATCH_PLIST (or load it: launchctl bootstrap gui/$(id -u) $(shell_quote "$WATCH_PLIST")), then re-run install.sh"
            ok "recovery watchdog loaded (StartInterval 900)"
            sum "6/6 recovery: watchdog (re)loaded"
        fi
    else
        skip "watchdog: not macOS — install the timer manually"
        sum "6/6 recovery: tool installed; watchdog skipped (not macOS)"
    fi
fi

# ------------------------------------------------------------ opt-outs
# The updater's plan reports these so a reinstall repeats the same opt-outs
# without asking (mcs_update.INSTALL_ARGS; --no-services is the updater's own).
# Updater runs add migration/repair opt-outs the operator did not choose.
if [ -z "${MCS_UPDATE_REINSTALL:-}" ]; then
    _opts=""
    for _o in brew:"$SKIP_BREW" llm:"$SKIP_LLM" plugin:"$SKIP_PLUGIN" recovery:"$SKIP_RECOVERY"; do
        if [ "${_o#*:}" -eq 1 ]; then _opts="$_opts${_opts:+, }\"--no-${_o%%:*}\""; fi
    done
    printf '{"v": 1, "install_args": [%s]}\n' "$_opts" > "$MCS_DATA/install_options.json.tmp" \
        || die "cannot write $MCS_DATA/install_options.json — repair permissions or disk space, then re-run install.sh"
    publish "$MCS_DATA/install_options.json.tmp" "$MCS_DATA/install_options.json" || :
fi

# ------------------------------------------------------------ summary
# Software preparation succeeded; a later setup failure has its own guidance.
trap - EXIT
SETUP_STATUS=0
PYQ="$(shell_quote "$MCS_PY")"
SETUPQ="$(shell_quote "$SETUP")"
# `-m` resolves only from the checkout: install.sh may run from elsewhere.
INREPO="cd $(shell_quote "$REPO") &&"
printf '\n============================================================\n'
printf 'ソフトの準備が完了しました。まだ利用開始前の初期設定が必要です。\n'
printf '%s' "$SUMMARY"
printf '============================================================\n'
printf '\n次は MCS のログイン情報と通知先を設定します。\n'
printf 'アカウント情報は、ご自身の端末で入力してください。\n'
printf '準備ができていない項目は初期設定の案内に従って後で設定できます。\n'
printf '初期設定だけでは MCS に接続できません。専用 Chrome の起動・MCS へのログイン、通知先の接続確認まで行ってください。\n'
printf '手順書: docs/guides/EASY_SETUP.md\n'
printf '後で続ける場合のコマンド:\n    %s setup\n' "$(shell_quote "$MCS_SHIM")"
printf '    # 同じ初期設定を開く別のコマンド:\n'
printf '    %s %s init\n' "$PYQ" "$SETUPQ"
if [ -t 0 ] && [ -t 1 ]; then
    printf '\n  1) 続けて初期設定を始める（推奨）\n  2) 後で設定する\n'
    if ! choose '選択 [1・推奨 / 2 / 0・終了]: '; then
        _answer=2
        printf 'ソフトの準備は完了しています。初期設定は後で再開できます。\n'
    fi
    if [ "$_answer" = 1 ]; then
        "$MCS_PY" "$SETUP" init || SETUP_STATUS=$?
        if [ "$SETUP_STATUS" -ne 0 ]; then
            printf '\nソフトの準備は完了していますが、初期設定は完了していません。\n'
            printf '入力内容や表示された問題を確認し、次のコマンドで再開してください:\n    %s setup\n' "$(shell_quote "$MCS_SHIM")"
        fi
    fi
fi
printf '\n設定後の確認コマンド:\n    %s %s check\n' "$PYQ" "$SETUPQ"
if [ "$RUNTIME_MODE" = standalone ]; then
    printf '    %s %s -m mcs_standalone status\n' "$INREPO" "$PYQ"
    printf '\n詳しい説明: docs/guides/STANDALONE.md\n'
fi
cat <<'EOF'

LINE WORKS を選んだ場合は、専用の接続設定も必要です。
詳しい説明: docs/guides/LINEWORKS.md
EOF
for sub in init check; do
    printf '    %s %s -m lineworks_adapter %s\n' "$INREPO" "$PYQ" "$sub"
done
if [ "$RUNTIME_MODE" = standalone ]; then
    printf 'MCS が LINE WORKS の接続を起動します。専用サービスを別に起動しないでください。\n'
else
    printf 'LINE WORKS の接続設定と確認を終えたら、自動起動を登録します:\n'
    printf '    %s %s -m lineworks_adapter service\n' "$INREPO" "$PYQ"
fi
printf '\n自動実行の登録を省略した場合は、初期設定後に次を実行してください。\n'
printf '    %s %s services\n' "$PYQ" "$SETUPQ"
exit "$SETUP_STATUS"
