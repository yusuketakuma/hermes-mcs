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
# `mcs_setup.py init` (guided all-settings wizard), the Discord/Slack
# tokens, and the plugin settings block — the final summary lists them.
set -eu
# files this script creates are the owner's only; existing modes are kept
umask 077

usage() {
    # print the header comment block (everything after the shebang)
    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
}

MODE=install
RUNTIME_MODE=hermes
RUNTIME_EXPLICIT=0
SKIP_BREW=0; SKIP_LLM=0; SKIP_PLUGIN=0; SKIP_SERVICES=0; SKIP_RECOVERY=0
FORCE_REPO=0
ARG_HOME=""
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)       [ $# -ge 2 ] || { printf '%s\n' '--mode needs hermes or standalone' >&2; exit 2; }
                      RUNTIME_MODE="$2"; RUNTIME_EXPLICIT=1; shift
                      case "$RUNTIME_MODE" in hermes|standalone) ;; *) printf '%s\n' 'invalid --mode' >&2; exit 2 ;; esac ;;
        --no-brew)     SKIP_BREW=1 ;;
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

say()  { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  ok: %s\n' "$*"; }
skip() { printf '  skip: %s\n' "$*"; }
warn() { printf '  warn: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
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
if [ "$RUNTIME_EXPLICIT" = 0 ] && [ -e "$HOME/.mcs/config.json" ]; then
    MODE_PY="$HOME/.mcs/venv/bin/python3"
    if [ ! -x "$MODE_PY" ]; then MODE_PY="$(command -v python3 || true)"; fi
    [ -n "$MODE_PY" ] || die "existing config requires Python to select its runtime mode; pass --mode explicitly"
    RUNTIME_MODE="$("$MODE_PY" - "$HOME/.mcs/config.json" <<'PYMODE'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        cfg = json.load(stream)
    mode = cfg.get("runtime_mode", "hermes")
    if mode not in ("hermes", "standalone"):
        raise ValueError
    print(mode)
except (OSError, ValueError, AttributeError, TypeError):
    raise SystemExit(1) from None
PYMODE
)" || die "cannot select runtime from existing config; repair config or pass --mode explicitly"
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
# Pinned: fork main — org build merged with upstream main (v2026.9.24),
# including config set --stdin, native command attestation, and the
# async pre-dispatch hook await fixes.
HERMES_PIN="fd50a275e2616118c48fe07e7e1c878782b15ccd"
CLONE_MARK="$HERMES_HOME/.hermes-agent.install-in-progress"
VENV="$HERMES_DIR/venv"
if [ "$RUNTIME_MODE" = standalone ]; then VENV="$RUNTIME_HOME/venv"; fi
VENV_PY="$VENV/bin/python"
if [ "$RUNTIME_MODE" = standalone ]; then VENV_PY="$VENV/bin/python3"; fi
VENV_HERMES="$VENV/bin/hermes"
PIP_MARK="$VENV/.mcs-pip-incomplete"
HERMES_BIN_DIR="$HOME/.local/bin"
SHIM="$HERMES_BIN_DIR/hermes"
PLUGIN_LINK="$HERMES_HOME/plugins/mcs-discord-commands"
LLM_MODELS_URL="http://127.0.0.1:8080/v1/models"
MODEL_DIR="$RUNTIME_HOME/models"
MODEL_FILE="$MODEL_DIR/Qwen3.5-9B-Q4_K_M.gguf"
MODEL_URL="https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q4_K_M.gguf"
# optional integrity check — set when the published sha256 is known
MODEL_SHA256="${MCS_MODEL_SHA256:-}"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LLAMA_PLIST="$AGENTS_DIR/ai.mcs.llamaserver.plist"
HERMES_LLAMA_PLIST="$AGENTS_DIR/ai.hermes.llamacpp.plist"
MCS_DATA="$HOME/.mcs/data"
RECOVERY_DIR="$HOME/.mcs-recovery"
WATCH_PLIST="$AGENTS_DIR/org.mcs.recovery.plist"
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
# hermes-agent requires >=3.11,<3.14 — prefer an explicitly versioned
# interpreter over a possibly-too-new python3
find_py() {
    for _c in python3.13 python3.12 python3.11 python3; do
        if py_ok "$_c" 11 14; then command -v "$_c"; return 0; fi
    done
    return 1
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
    if [ -f "$2" ] && cmp -s "$1" "$2"; then rm -f "$1"; return 1; fi
    mv -f "$1" "$2"
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
    say "preflight (read-only — nothing is written)"
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
    if _py="$(find_py)"; then pf_ok "Python 3.11–3.13: $_py"
    elif [ "$_brew_fills" -eq 1 ]; then pf_info "no Python 3.11–3.13 yet — stage 1 installs python@3.13"
    else pf_ng "no Python 3.11–3.13 on PATH" "brew install python@3.13"; fi
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
    if py_ok "$VENV_PY" 11 14; then pf_ok "hermes venv python: $VENV_PY"
    elif [ -e "$VENV" ]; then pf_info "hermes venv has no usable Python 3.11–3.13 — stage 2 recreates it"
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
    if [ "$RUNTIME_MODE" = hermes ] && [ "$HERMES_HOME" != "$DEFAULT_HERMES_HOME" ] && [ "$SKIP_SERVICES" -eq 0 ]; then
        pf_ng "custom HERMES_HOME ($HERMES_HOME) is not supported by the services stage" "use the default ~/.hermes, or add --no-services"
    fi

    if [ "$OS" = Darwin ]; then
        if [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then pf_ok "Google Chrome installed"
        elif [ "$_brew_fills" -eq 1 ]; then pf_info "Google Chrome missing — stage 1 installs it"
        else pf_warn "Google Chrome missing (MCS login needs it)" "brew install --cask google-chrome"; fi
    fi
    if curl -sf -m 3 "$LLM_MODELS_URL" >/dev/null 2>&1; then
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
    say "plan (--dry-run: nothing below is executed)"
    if [ "$SKIP_BREW" -eq 1 ]; then plan_line "1/6 brew" "skipped (--no-brew)"
    else plan_line "1/6 brew" "brew install any of: git python@3.13 uv llama.cpp, cask google-chrome (installed ones are skipped)"; fi
    if [ "$RUNTIME_MODE" = standalone ]; then
        plan_line "2/6 standalone" "venv $VENV + official SDKs from deployment/requirements-standalone.txt; runtime_mode=standalone"
    else
    plan_line "2/6 hermes" "checkout $HERMES_DIR [$(state "$HERMES_DIR")] @ $HERMES_PIN (existing checkouts are kept)"
    plan_line "" "venv $VENV [$(state "$VENV_PY")] + pip install -e hermes-agent[messaging]"
    plan_line "" "shim $SHIM [$(state "$SHIM")] (a foreign file there is left alone)"
    fi
    if [ "$SKIP_PLUGIN" -eq 1 ]; then plan_line "3/6 plugin" "skipped (--no-plugin)"
    else plan_line "3/6 plugin" "symlink $PLUGIN_LINK [$(state "$PLUGIN_LINK")] -> $REPO/hermes_plugin; hermes plugins enable mcs-discord-commands"; fi
    if [ "$SKIP_LLM" -eq 1 ] || [ "$OS" != Darwin ]; then plan_line "4/6 llm" "skipped"
    else
        # mirror stage 4's order: a hermes-managed agent or a foreign
        # server on :8080 is kept — no second llama-server is planned
        if [ -f "$HERMES_LLAMA_PLIST" ]; then
            plan_line "4/6 llm" "hermes-managed $HERMES_LLAMA_PLIST [exists] is kept (load state checked; no model download, no second server)"
        elif curl -sf -m 3 "$LLM_MODELS_URL" >/dev/null 2>&1 \
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
}

if [ "$MODE" != install ]; then
    preflight
    if [ "$MODE" = dry-run ]; then dry_run_plan; fi
    [ "$PF_NG" -eq 0 ] || exit 1
    exit 0
fi

# ---------------------------------------------------------- guards
[ "$IS_ROOT" -eq 0 ] || die "do not run install.sh as root or with sudo — it installs into your own home; re-run as your normal user"
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
if [ -f "$REPO/.git" ]; then
    warn "running from a git worktree ($REPO) — services and the plugin will point here and break if the worktree is removed; prefer the main checkout"
fi

# every non-zero exit from here on is a stopped install, not a usage error
trap 'if [ "$?" -ne 0 ]; then
    printf "%s\n" "error: installation stopped; repair the failed stage and re-run install.sh" >&2
fi' EXIT

# ---------------------------------------------------------------- 1. brew
say "1/6 Homebrew packages"
if [ "$SKIP_BREW" -eq 1 ]; then
    skip "stage skipped (--no-brew) — assuming git/python/uv/llama.cpp/Chrome are already provided"
    sum "1/6 brew:     skipped (--no-brew)"
elif ! command -v brew >/dev/null 2>&1; then
    die "brew not found — install Homebrew first:
  $BREW_INSTALL
then re-run install.sh (or pass --no-brew if you manage these tools yourself)"
else
    _new=""
    for pkg in git python@3.13 uv llama.cpp; do
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
    say "2/6 standalone runtime"
    mkdir -p "$RUNTIME_HOME"
    if ! py_ok "$VENV_PY" 11 14; then
        PYBIN="$(find_py)" || die "Python 3.11–3.13 required — brew install python@3.13"
        if [ -e "$VENV" ]; then rm -rf "$VENV"; fi
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
    py_ok "$VENV_PY" 11 14 || die "standalone interpreter unavailable after install"
    MCS_PY="$VENV_PY"
    sum "2/6 runtime:  standalone venv and official connector SDKs"
else
say "2/6 hermes-agent"
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
    git -C "$HERMES_DIR" checkout -q --detach "$HERMES_PIN" \
        || die "checkout of the pinned commit $HERMES_PIN failed — re-run install.sh (the interrupted clone is redone)"
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
elif [ "$HEAD_SHA" = "$HERMES_PIN" ]; then
    skip "hermes-agent checkout already at pin $HERMES_PIN"
    _ck="kept existing checkout (pinned)"
else
    warn "$HERMES_DIR is at $HEAD_SHA, not pinned $HERMES_PIN"
    warn "  (keeping existing checkout; update deliberately)"
    _ck="kept existing checkout at $HEAD_SHA (NOT the validated pin)"
fi

# venv: converge on re-run — a venv without a usable python is rebuilt,
# and PIP_MARK brackets the package install so a failed one is retried
if ! py_ok "$VENV_PY" 11 14; then
    PYBIN="$(find_py)" \
        || die "Python >=3.11,<3.14 is required for hermes-agent — brew install python@3.13, then re-run install.sh"
    if [ -e "$VENV" ]; then
        warn "$VENV has no usable Python 3.11–3.13 — recreating it"
        rm -rf "$VENV"
    fi
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
py_ok "$VENV_PY" 11 14 \
    || die "$VENV_PY is missing or not Python 3.11–3.13 — remove $VENV and re-run install.sh"
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

# Persist only an explicitly selected mode; never discard an unreadable config.
if [ "$RUNTIME_EXPLICIT" -eq 1 ]; then
    "$MCS_PY" - "$REPO" "$RUNTIME_MODE" <<'PYMODE'
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "mcs"))
import _mcs_path
from mcs_util import CONF_PATH, atomic_write
path = Path(CONF_PATH)
try:
    cfg = json.loads(path.read_text()) if path.exists() else {}
except (OSError, ValueError):
    raise SystemExit("config.json unreadable — repair it before selecting a runtime")
if not isinstance(cfg, dict):
    raise SystemExit("config.json must be an object")
cfg["runtime_mode"] = sys.argv[2]
atomic_write(str(path), lambda handle: json.dump(cfg, handle, ensure_ascii=False, indent=2), 0o600)
PYMODE
fi

# -------------------------------------------------------- 3. plugin
say "3/6 Discord/Slack command plugin"
if [ "$SKIP_PLUGIN" -eq 1 ]; then
    skip "stage skipped (--no-plugin) — interactive cards need this plugin; re-run without the flag before enabling notify.interactive"
    sum "3/6 plugin:   skipped (--no-plugin)"
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
say "4/6 local LLM (llama-server :8080)"
if [ "$SKIP_LLM" -eq 1 ]; then
    skip "stage skipped (--no-llm) — keep your own OpenAI-compatible server answering on 127.0.0.1:8080"
    sum "4/6 llm:      skipped (--no-llm)"
elif [ "$OS" != Darwin ]; then
    skip "not macOS — the llama-server LaunchAgent is macOS-only; serve an OpenAI-compatible endpoint on 127.0.0.1:8080 yourself"
    sum "4/6 llm:      skipped (not macOS)"
else
    SERVING=0
    if curl -sf -m 3 "$LLM_MODELS_URL" >/dev/null 2>&1; then SERVING=1; fi
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
            printf '  downloading model (~5.7 GB, resumable): %s\n' "$MODEL_FILE"
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
                    || die "model download failed — re-run install.sh to resume (partial file: $MODEL_FILE.part)"
            fi
            if [ -n "$MODEL_SHA256" ]; then
                _got="$(shasum -a 256 "$MODEL_FILE.part" | awk '{print $1}')"
                if [ "$_got" != "$MODEL_SHA256" ]; then
                    rm -f "$MODEL_FILE.part"
                    die "model checksum mismatch (got $_got) — the partial file was removed; re-run install.sh"
                fi
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
say "5/6 scheduled services ($RUNTIME_MODE)"
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
say "6/6 update recovery (watchdog + tool)"
if [ "$SKIP_RECOVERY" -eq 1 ]; then
    skip "stage skipped (--no-recovery)"
    sum "6/6 recovery: skipped (--no-recovery)"
else
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
            "$REPO/deployment/launchagents/org.mcs.recovery.plist" \
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

# ------------------------------------------------------------ summary
PYQ="$(shell_quote "$MCS_PY")"
SETUPQ="$(shell_quote "$SETUP")"
printf '\n============================================================\n'
printf 'Installed. Summary:\n'
printf '%s' "$SUMMARY"
printf '============================================================\n'
printf '\nOne guided step remains — it needs your secrets/choices:\n\n'
printf '    %s %s init\n' "$PYQ" "$SETUPQ"
if [ "$RUNTIME_MODE" = standalone ]; then
    cat <<'EOF'

Select your notification destination and explicit actor/project scopes.
The wizard stores connector secrets with hidden prompts in owner-only files.
Hermes profiles, plugins and gateway are not used.
Run init first; the host waits until configuration and credentials are ready.
Then run the required-condition check and inspect status:
EOF
    printf '    %s %s check\n' "$PYQ" "$SETUPQ"
    printf '    %s -m mcs_standalone status\n' "$PYQ"
    printf '\nSee docs/guides/STANDALONE.md (including switching from Hermes).\n'
    exit 0
fi
cat <<'EOF'

The wizard covers EVERY config.json setting (Enter keeps the
current/default — a partially configured install just confirms
existing values). When you pick notify.interactive=slack or
discord it also, through the public hermes CLI only:

  - writes the plugin settings block (snapshot/inbox/allowlists and
    the notify.<transport> scope — slack_* keys for Slack) into the
    hermes profile you name — multiplex setups: the profile that
    serves that transport
  - stores DISCORD_BOT_TOKEN (or SLACK_BOT_TOKEN + SLACK_APP_TOKEN)
    in the profile .env (env var or prompt — never an argv flag),
    skipping tokens already configured
  - syncs the supervised gateway when interactive is configured
  - runs the final required-condition check and reports blockers

Non-interactive equivalent (slack):

EOF
printf '    %s %s init --yes \\\n' "$PYQ" "$SETUPQ"
cat <<'EOF'
        --login-id <ID> --notify-target slack:<channel_id> \
        --set 'notify.interactive="slack"' \
        --set 'notify.slack={"profile":"P","application_id":"A","team_id":"T","channel_id":"C"}' \
        --plugin-profile <serving profile> \
        --plugin-user-ids <uid> \
        --plugin-project-ids <pid>
    # SLACK_BOT_TOKEN=<token> SLACK_APP_TOKEN=<token> in the
    #   environment store the tokens
    # Discord: notify.interactive="discord" + notify.discord={profile,
    #   application_id, guild_id, channel_id}; the token comes from
    #   DISCORD_BOT_TOKEN in the environment

init already syncs the gateway and validates the install.
Only when services were skipped, or check reports deployed-script drift,
run services and check again:

EOF
printf '    %s %s services   # picks up gateway\n' "$PYQ" "$SETUPQ"
printf '    %s %s check\n' "$PYQ" "$SETUPQ"
cat <<'EOF'

Notes:
- node.js is NOT a dependency — the pipeline and plugin are pure
  Python (stdlib + hermes-bundled discord.py).
- Jev/typesafe needs only TYPESAFE_API_KEY — the init wizard stores
  it in ~/.mcs/.env. Nothing to install.
- ollama is shared embedding infra, not required by MCS itself.
EOF
