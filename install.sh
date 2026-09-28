#!/bin/sh
# Install everything MCS needs on a fresh machine, in idempotent stages:
#
#   1. Homebrew packages (git, python, uv, llama.cpp, Google Chrome)
#   2. hermes-agent checkout + venv + ~/.local/bin/hermes shim
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
# Re-running is safe: every stage checks first and skips what exists,
# so partially-installed machines converge instead of starting over.
# A stage that fails stops the install with a non-zero exit — repair
# it and re-run; later stages never run on top of a broken one.
# What is NOT automated (needs your secrets / interactive choices):
# `mcs_setup.py init` (guided all-settings wizard), the Discord/Slack
# tokens, and the plugin settings block — the final summary lists them.
set -eu

usage() {
    # print the header comment block (everything after the shebang)
    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
}

SKIP_BREW=0; SKIP_LLM=0; SKIP_PLUGIN=0; SKIP_SERVICES=0; SKIP_RECOVERY=0
HERMES_HOME=""
while [ $# -gt 0 ]; do
    case "$1" in
        --no-brew)     SKIP_BREW=1 ;;
        --no-llm)      SKIP_LLM=1 ;;
        --no-plugin)   SKIP_PLUGIN=1 ;;
        --no-services) SKIP_SERVICES=1 ;;
        --no-recovery) SKIP_RECOVERY=1 ;;
        -h|--help)     usage; exit 0 ;;
        --*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)  if [ -z "$HERMES_HOME" ]; then HERMES_HOME="$1"
            else echo "unexpected argument: $1" >&2; exit 2; fi ;;
    esac
    shift
done
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# mcs_setup services renders cron wrappers/plists against ~/.hermes (its
# HERMES_PY and scripts dir are fixed there) — a custom HERMES_HOME would
# install services pointing at an interpreter that does not exist
if [ "$HERMES_HOME" != "$HOME/.hermes" ] && [ "$SKIP_SERVICES" -eq 0 ]; then
    echo "custom HERMES_HOME ($HERMES_HOME) is not supported by the services stage;" >&2
    echo "use the default ~/.hermes, or pass --no-services and register services yourself" >&2
    exit 2
fi
# every non-zero exit from here on is a stopped install, not a usage error
trap 'if [ "$?" -ne 0 ]; then
    echo "error: installation stopped; repair the failed stage and re-run install.sh" >&2
fi' EXIT

REPO="$(cd "$(dirname "$0")" && pwd)"
HERMES_DIR="$HERMES_HOME/hermes-agent"
HERMES_REPO="https://github.com/yusuketakuma/hermes-agent.git"
# Pinned: fork main — org build merged with upstream main (v2026.9.24),
# including config set --stdin, native command attestation, and the
# async pre-dispatch hook await fixes.
HERMES_PIN="fd50a275e2616118c48fe07e7e1c878782b15ccd"
HERMES_BIN_DIR="$HOME/.local/bin"
LLM_MODELS_URL="http://127.0.0.1:8080/v1/models"
MODEL_DIR="$HERMES_HOME/models"
MODEL_FILE="$MODEL_DIR/Qwen3.5-9B-Q4_K_M.gguf"
MODEL_URL="https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q4_K_M.gguf"

say()  { printf '\n=== %s ===\n' "$1"; }
ok()   { echo "  ok: $*"; }
skip() { echo "  skip: $*"; }
warn() { echo "  warn: $*" >&2; }
die()  { echo "error: $*" >&2; exit 1; }

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

# ---------------------------------------------------------------- 1. brew
say "1/6 Homebrew packages"
if [ "$SKIP_BREW" -eq 1 ]; then
    skip "stage skipped (--no-brew) — assuming git/python/uv/llama.cpp/Chrome are already provided"
elif ! command -v brew >/dev/null 2>&1; then
    warn "brew not found — install it first:"
    warn "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    warn "then re-run this script (or pass --no-brew if you manage these tools yourself)."
    exit 1
fi
if [ "$SKIP_BREW" -eq 0 ]; then
for pkg in git python@3.13 uv llama.cpp; do
    if brew list --versions "$pkg" >/dev/null 2>&1; then
        skip "$pkg already installed"
    else
        echo "  install: $pkg"
        brew install "$pkg"
    fi
done
if [ "$(uname -s)" != "Darwin" ]; then
    skip "google-chrome cask: not macOS — install Chrome/Chromium yourself"
elif brew list --cask --versions google-chrome >/dev/null 2>&1 \
        || [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
    skip "google-chrome already installed"
else
    echo "  install: google-chrome (cask)"
    brew install --cask google-chrome
fi
fi  # SKIP_BREW

# ----------------------------------------------------------- 2. hermes
say "2/6 hermes-agent"
ensure_hermes() {
    if command -v hermes >/dev/null 2>&1; then
        echo "hermes already installed: $(command -v hermes)"
        return 0
    fi

    if [ -d "$HERMES_DIR/.git" ]; then
        # An existing checkout is left untouched — only fill in the missing
        # runnable bits (venv + shim). Warn when it is not the validated pin.
        head_sha="$(git -C "$HERMES_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
        if [ "$head_sha" != "$HERMES_PIN" ]; then
            echo "warn: $HERMES_DIR is at $head_sha, not pinned $HERMES_PIN" >&2
            echo "      (keeping existing checkout; update deliberately)" >&2
        fi
    else
        command -v git >/dev/null 2>&1 || {
            echo "error: git is required to install hermes-agent" >&2; exit 1; }
        echo "installing hermes-agent @$HERMES_PIN -> $HERMES_DIR"
        git clone --filter=blob:none "$HERMES_REPO" "$HERMES_DIR" 2>/dev/null \
            || git clone "$HERMES_REPO" "$HERMES_DIR"
        git -C "$HERMES_DIR" checkout --detach "$HERMES_PIN"
    fi

    if [ ! -x "$HERMES_DIR/venv/bin/hermes" ]; then
        # hermes-agent requires >=3.11,<3.14 — prefer an explicitly
        # versioned interpreter over a possibly-too-new python3.
        PYBIN=""
        for cand in python3.13 python3.12 python3.11 python3; do
            if command -v "$cand" >/dev/null 2>&1 && "$cand" -c \
'import sys; sys.exit(0 if (3,11)<=sys.version_info[:2]<(3,14) else 1)'; then
                PYBIN="$cand"; break
            fi
        done
        [ -n "$PYBIN" ] || {
            echo "error: Python >=3.11,<3.14 required for hermes-agent" >&2
            exit 1; }
        if command -v uv >/dev/null 2>&1; then
            uv venv --python "$PYBIN" "$HERMES_DIR/venv"
            uv pip install --python "$HERMES_DIR/venv/bin/python" \
                -e "${HERMES_DIR}[messaging]"
        else
            "$PYBIN" -m venv "$HERMES_DIR/venv"
            "$HERMES_DIR/venv/bin/pip" install -e "${HERMES_DIR}[messaging]"
        fi
    fi

    mkdir -p "$HERMES_BIN_DIR"
    printf '#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\nexec %s "$@"\n' \
        "$(shell_quote "$HERMES_DIR/venv/bin/hermes")" > "$HERMES_BIN_DIR/hermes"
    chmod +x "$HERMES_BIN_DIR/hermes"
    echo "installed: $HERMES_BIN_DIR/hermes (pinned $HERMES_PIN)"
    echo "note: ensure $HERMES_BIN_DIR is on PATH"
}

ensure_hermes
HERMES_BIN="$(command -v hermes || echo "$HERMES_BIN_DIR/hermes")"

# mcs_setup.py requires Python >= 3.10 — ambient `python3` on a fresh
# mac can be absent (CLT install prompt) or too old (/usr/bin/python3
# is 3.9-era), while stage 2 guaranteed the hermes venv at 3.11–3.13.
# Fall back to any >=3.10 interpreter when that venv is absent
# (e.g. a pre-existing hermes install on PATH).
MCS_PY="$HERMES_DIR/venv/bin/python"
if [ ! -x "$MCS_PY" ]; then
    MCS_PY=""
    for cand in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cand" >/dev/null 2>&1 && "$cand" -c \
'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
                2>/dev/null; then
            MCS_PY="$(command -v "$cand")"
            break
        fi
    done
fi

# -------------------------------------------------------- 3. plugin
say "3/6 Discord/Slack command plugin"
if [ "$SKIP_PLUGIN" -eq 1 ]; then
    skip "stage skipped (--no-plugin) — interactive cards need this plugin; re-run without the flag before enabling notify.interactive"
else
PLUGINS_DIR="$HERMES_HOME/plugins"
LINK="$PLUGINS_DIR/mcs-discord-commands"

mkdir -p "$PLUGINS_DIR"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    echo "error: $LINK exists and is not a symlink — remove it manually" >&2
    exit 1
fi
ln -sfn "$REPO/hermes_plugin" "$LINK"
echo "linked: $LINK -> $REPO/hermes_plugin"

if "$HERMES_BIN" config get plugins.enabled 2>/dev/null \
        | grep -q "mcs-discord-commands"; then
    skip "plugin already enabled"
else
    if "$HERMES_BIN" plugins enable mcs-discord-commands --no-allow-tool-override; then
        ok "plugin enabled"
    else
        die "plugins enable failed — repair the Hermes CLI/plugin configuration (or add 'mcs-discord-commands' to plugins.enabled in the profile config.yaml), then re-run install.sh"
    fi
fi
fi  # SKIP_PLUGIN

# -------------------------------------------------- 4. local LLM server
say "4/6 local LLM (llama-server :8080)"
if [ "$SKIP_LLM" -eq 1 ]; then
    skip "stage skipped (--no-llm) — keep your own OpenAI-compatible server answering on 127.0.0.1:8080"
elif [ "$(uname -s)" != "Darwin" ]; then
    skip "not macOS — the llama-server LaunchAgent is macOS-only; serve an OpenAI-compatible endpoint on 127.0.0.1:8080 yourself"
elif curl -sf -m 3 "$LLM_MODELS_URL" >/dev/null 2>&1; then
    skip "an OpenAI-compatible server is already answering on :8080 — leaving it in place"
elif [ -f "$HOME/Library/LaunchAgents/ai.hermes.llamacpp.plist" ]; then
    skip "a hermes-managed llamacpp LaunchAgent exists — it will serve :8080 once loaded"
else
    LLAMA_BIN="$(command -v llama-server || true)"
    if [ -z "$LLAMA_BIN" ] && command -v brew >/dev/null 2>&1; then
        LLAMA_BIN="$(brew --prefix)/bin/llama-server"
    fi
    if [ ! -x "$LLAMA_BIN" ]; then
        die "llama-server not found even after brew — install it (or pass --no-llm and serve :8080 yourself), then re-run install.sh"
    else
        if [ ! -f "$MODEL_FILE" ]; then
            echo "  downloading model (~6 GB): $MODEL_FILE"
            mkdir -p "$MODEL_DIR"
            if curl -fL --progress-bar -o "$MODEL_FILE.part" "$MODEL_URL" \
                    && mv "$MODEL_FILE.part" "$MODEL_FILE"; then
                ok "model downloaded"
            else
                die "model download failed — fetch $MODEL_URL into $MODEL_FILE, then re-run install.sh"
            fi
        else
            skip "model already present"
        fi
        PLIST_SRC="$REPO/deployment/launchagents/ai.mcs.llamaserver.plist"
        PLIST_DST="$HOME/Library/LaunchAgents/ai.mcs.llamaserver.plist"
        mkdir -p "$HOME/Library/LaunchAgents" "$HERMES_HOME/logs"
        sed -e "s|__LLAMA_BIN__|$(xml_sed "$LLAMA_BIN")|g" \
            -e "s|__MODEL__|$(xml_sed "$MODEL_FILE")|g" \
            -e "s|__HERMES_HOME__|$(xml_sed "$HERMES_HOME")|g" \
            "$PLIST_SRC" > "$PLIST_DST"
        if [ ! -f "$MODEL_FILE" ]; then
            # KeepAlive with no model would crash-loop llama-server
            warn "model missing — ai.mcs.llamaserver not loaded; re-run after fetching the model"
        elif launchctl print "gui/$(id -u)/ai.mcs.llamaserver" >/dev/null 2>&1; then
            skip "ai.mcs.llamaserver already loaded"
        else
            if launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"; then
                ok "llama-server LaunchAgent started"
            else
                die "launchctl bootstrap failed for ai.mcs.llamaserver — inspect $PLIST_DST, then re-run install.sh"
            fi
        fi
    fi
fi

# ------------------------------------------- 5. launchd + hermes cron
say "5/6 scheduled services (launchd + hermes cron)"
if [ "$SKIP_SERVICES" -eq 1 ]; then
    skip "stage skipped (--no-services) — run it later:"
    skip "  ${MCS_PY:-python3} $REPO/mcs/ops/mcs_setup.py services"
elif [ -z "$MCS_PY" ]; then
    die "no Python >=3.10 interpreter found — services not installed; install one (or pass --no-services), then re-run install.sh"
elif "$MCS_PY" "$REPO/mcs/ops/mcs_setup.py" services; then
    ok "services installed"
else
    die "services reported problems — see deployment/launchagents/README.md, then re-run install.sh"
fi

# --------------------------------- 6. update recovery (independent)
say "6/6 update recovery (watchdog + tool)"
if [ "$SKIP_RECOVERY" -eq 1 ]; then
    skip "stage skipped (--no-recovery)"
else
RECOVERY_DIR="$HOME/.mcs-recovery"
mkdir -p "$RECOVERY_DIR"
RECOVERY_SRC="$REPO/deployment/recovery/mcs_recover.py"
RECOVERY_DST="$RECOVERY_DIR/mcs_recover.py"
# preserve the prior generation — on a bad update it may be the only
# thing that can still run
# (only when the generation actually changes — a same-release re-run
# must not overwrite the real previous generation with the current one).
# Both files are published via tmp + rename so an interrupted install
# never leaves a truncated tool or .prev behind.
if ! cmp -s "$RECOVERY_SRC" "$RECOVERY_DST"; then
    if [ -f "$RECOVERY_DST" ]; then
        cp -p "$RECOVERY_DST" "$RECOVERY_DST.prev.tmp"
        mv -f "$RECOVERY_DST.prev.tmp" "$RECOVERY_DST.prev"
    fi
    cp "$RECOVERY_SRC" "$RECOVERY_DST.tmp"
    chmod 755 "$RECOVERY_DST.tmp"
    mv -f "$RECOVERY_DST.tmp" "$RECOVERY_DST"
fi
# the tool runs outside the repo — record which checkout it recovers
printf '%s\n' "$REPO" > "$RECOVERY_DIR/repo_path"
ok "recovery tool: $RECOVERY_DST"

if [ "$(uname -s)" = "Darwin" ]; then
    WATCH_PLIST="$HOME/Library/LaunchAgents/org.mcs.recovery.plist"
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.mcs/data"
    sed -e "s|__RECOVERY__|$(xml_sed "$RECOVERY_DIR")|g" \
        -e "s|__DATA__|$(xml_sed "$HOME/.mcs/data")|g" \
        "$REPO/deployment/launchagents/org.mcs.recovery.plist" \
        > "$WATCH_PLIST"
    launchctl bootout "gui/$(id -u)/org.mcs.recovery" 2>/dev/null || true
    if launchctl bootstrap "gui/$(id -u)" "$WATCH_PLIST"; then
        ok "recovery watchdog loaded (StartInterval 900)"
    else
        die "watchdog bootstrap failed — inspect $WATCH_PLIST (or load it: launchctl bootstrap gui/$(id -u) $WATCH_PLIST), then re-run install.sh"
    fi
else
    skip "watchdog: not macOS — install the timer manually"
fi
fi  # SKIP_RECOVERY

cat <<EOF

============================================================
Done. One guided step remains — it needs your secrets/choices:
============================================================

    ${MCS_PY:-python3} $REPO/mcs/ops/mcs_setup.py init

The wizard covers EVERY config.json setting (Enter keeps the
current/default — a partially configured install just confirms
existing values). When you pick notify.interactive=discord or
slack it also, through the public hermes CLI only:

  - writes the plugin settings block (snapshot/inbox/allowlists and
    the notify.<transport> scope — slack_* keys for Slack) into the
    hermes profile you name — multiplex setups: the profile that
    serves that transport
  - stores DISCORD_BOT_TOKEN (or SLACK_BOT_TOKEN + SLACK_APP_TOKEN)
    in the profile .env (env var or prompt — never an argv flag),
    skipping tokens already configured
  - and 'services' (already run above) installs the gateway via
    'hermes gateway install' when interactive is configured

Non-interactive equivalent (discord):

    ${MCS_PY:-python3} $REPO/mcs/ops/mcs_setup.py init --yes \
        --login-id <ID> --notify-target discord:<channel> \
        --set 'notify.interactive="discord"' \
        --set 'notify.discord={"profile":"P","application_id":"A","guild_id":"G","channel_id":"C"}' \
        --plugin-profile <serving profile> \
        --plugin-user-ids <uid> --plugin-chat-ids <chid> \
        --plugin-project-ids <pid>
    # DISCORD_BOT_TOKEN=<token> in the environment stores the token
    # Slack: notify.interactive="slack" + notify.slack={profile,
    #   application_id, team_id, channel_id}; tokens come from
    #   SLACK_BOT_TOKEN / SLACK_APP_TOKEN in the environment

Then re-run services + validate:

    ${MCS_PY:-python3} $REPO/mcs/ops/mcs_setup.py services   # picks up gateway
    ${MCS_PY:-python3} $REPO/mcs/ops/mcs_setup.py check

Notes:
- node.js is NOT a dependency — the pipeline and plugin are pure
  Python (stdlib + hermes-bundled discord.py).
- Jev/typesafe needs only TYPESAFE_API_KEY — the init wizard stores
  it in ~/.mcs/.env. Nothing to install.
- ollama is shared embedding infra, not required by MCS itself.
EOF
