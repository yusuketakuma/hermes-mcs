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
#   ./install.sh [HERMES_HOME]     HERMES_HOME defaults to ~/.hermes
#
# Re-running is safe: every stage checks first and skips what exists.
# What is NOT automated (needs your secrets / interactive choices):
# `mcs_setup.py init` (guided all-settings wizard), the Discord bot
# token, and the plugin settings block — the final summary lists them.
set -eu

REPO="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${1:-$HOME/.hermes}"
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

# ---------------------------------------------------------------- 1. brew
say "1/6 Homebrew packages"
if ! command -v brew >/dev/null 2>&1; then
    warn "brew not found — install it first:"
    warn "  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    warn "then re-run this script."
    exit 1
fi
for pkg in git python@3.13 uv llama.cpp; do
    if brew list --versions "$pkg" >/dev/null 2>&1; then
        skip "$pkg already installed"
    else
        echo "  install: $pkg"
        brew install "$pkg"
    fi
done
if brew list --cask --versions google-chrome >/dev/null 2>&1 \
        || [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
    skip "google-chrome already installed"
else
    echo "  install: google-chrome (cask)"
    brew install --cask google-chrome
fi

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
    cat > "$HERMES_BIN_DIR/hermes" <<SHIM
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
exec "$HERMES_DIR/venv/bin/hermes" "\$@"
SHIM
    chmod +x "$HERMES_BIN_DIR/hermes"
    echo "installed: $HERMES_BIN_DIR/hermes (pinned $HERMES_PIN)"
    echo "note: ensure $HERMES_BIN_DIR is on PATH"
}

ensure_hermes
HERMES_BIN="$(command -v hermes || echo "$HERMES_BIN_DIR/hermes")"

# -------------------------------------------------------- 3. plugin
say "3/6 Discord command plugin"
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
        warn "plugins enable failed — add 'mcs-discord-commands' to plugins.enabled in the profile config.yaml"
    fi
fi

# -------------------------------------------------- 4. local LLM server
say "4/6 local LLM (llama-server :8080)"
if curl -sf -m 3 "$LLM_MODELS_URL" >/dev/null 2>&1; then
    skip "llama-server already answering on :8080"
elif [ -f "$HOME/Library/LaunchAgents/ai.hermes.llamacpp.plist" ]; then
    skip "a hermes-managed llamacpp LaunchAgent exists — it will serve :8080 once loaded"
else
    LLAMA_BIN="$(command -v llama-server || true)"
    [ -n "$LLAMA_BIN" ] || LLAMA_BIN="$(brew --prefix)/bin/llama-server"
    if [ ! -x "$LLAMA_BIN" ]; then
        warn "llama-server not found even after brew — install manually"
    else
        if [ ! -f "$MODEL_FILE" ]; then
            echo "  downloading model (~6 GB): $MODEL_FILE"
            mkdir -p "$MODEL_DIR"
            if curl -fL --progress-bar -o "$MODEL_FILE.part" "$MODEL_URL" \
                    && mv "$MODEL_FILE.part" "$MODEL_FILE"; then
                ok "model downloaded"
            else
                warn "model download failed — fetch $MODEL_URL into $MODEL_FILE"
            fi
        else
            skip "model already present"
        fi
        PLIST_SRC="$REPO/deployment/launchagents/ai.mcs.llamaserver.plist"
        PLIST_DST="$HOME/Library/LaunchAgents/ai.mcs.llamaserver.plist"
        mkdir -p "$HOME/Library/LaunchAgents" "$HERMES_HOME/logs"
        sed -e "s|__LLAMA_BIN__|$LLAMA_BIN|g" \
            -e "s|__MODEL__|$MODEL_FILE|g" \
            -e "s|__HERMES_HOME__|$HERMES_HOME|g" \
            "$PLIST_SRC" > "$PLIST_DST"
        if launchctl print "gui/$(id -u)/ai.mcs.llamaserver" >/dev/null 2>&1; then
            skip "ai.mcs.llamaserver already loaded"
        else
            if launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"; then
                ok "llama-server LaunchAgent started"
            else
                warn "launchctl bootstrap failed for ai.mcs.llamaserver"
            fi
        fi
    fi
fi

# ------------------------------------------- 5. launchd + hermes cron
say "5/6 scheduled services (launchd + hermes cron)"
if python3 "$REPO/mcs/ops/mcs_setup.py" services; then
    ok "services installed"
else
    warn "services reported problems — see deployment/launchagents/README.md"
fi

# --------------------------------- 6. update recovery (independent)
say "6/6 update recovery (watchdog + tool)"
RECOVERY_DIR="$HOME/.mcs-recovery"
mkdir -p "$RECOVERY_DIR"
# preserve the prior generation — on a bad update it may be the only
# thing that can still run
if [ -f "$RECOVERY_DIR/mcs_recover.py" ]; then
    cp -p "$RECOVERY_DIR/mcs_recover.py" "$RECOVERY_DIR/mcs_recover.py.prev"
fi
cp "$REPO/deployment/recovery/mcs_recover.py" "$RECOVERY_DIR/mcs_recover.py"
chmod 755 "$RECOVERY_DIR/mcs_recover.py"
ok "recovery tool: $RECOVERY_DIR/mcs_recover.py"

if [ "$(uname -s)" = "Darwin" ]; then
    WATCH_PLIST="$HOME/Library/LaunchAgents/org.mcs.recovery.plist"
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.mcs/data"
    sed -e "s|__RECOVERY__|$RECOVERY_DIR|g" \
        -e "s|__DATA__|$HOME/.mcs/data|g" \
        "$REPO/deployment/launchagents/org.mcs.recovery.plist" \
        > "$WATCH_PLIST"
    launchctl bootout "gui/$(id -u)/org.mcs.recovery" 2>/dev/null || true
    if launchctl bootstrap "gui/$(id -u)" "$WATCH_PLIST"; then
        ok "recovery watchdog loaded (StartInterval 900)"
    else
        warn "watchdog bootstrap failed — load manually: launchctl bootstrap gui/$(id -u) $WATCH_PLIST"
    fi
else
    skip "watchdog: not macOS — install the timer manually"
fi

cat <<EOF

============================================================
Done. One guided step remains — it needs your secrets/choices:
============================================================

    python3 $REPO/mcs/ops/mcs_setup.py init

The wizard covers EVERY config.json setting (Enter keeps the
current/default). When you pick notify.interactive=discord it also,
through the public hermes CLI only:

  - writes the plugin settings block (snapshot/inbox/allowlists and
    the notify.discord scope) into the hermes profile you name —
    multiplex setups: the profile that serves Discord
  - stores DISCORD_BOT_TOKEN in the profile .env (env var or prompt —
    never an argv flag)
  - and 'services' (already run above) installs the Discord gateway
    via 'hermes gateway install' when interactive is configured

Non-interactive equivalent:

    python3 $REPO/mcs/ops/mcs_setup.py init --yes \
        --login-id <ID> --notify-target discord:<channel> \
        --set 'notify.interactive="discord"' \
        --set 'notify.discord={"profile":"P","application_id":"A","guild_id":"G","channel_id":"C"}' \
        --plugin-profile <serving profile> \
        --plugin-user-ids <uid> --plugin-chat-ids <chid> \
        --plugin-project-ids <pid>
    # DISCORD_BOT_TOKEN=<token> in the environment stores the token

Then re-run services + validate:

    python3 $REPO/mcs/ops/mcs_setup.py services   # picks up gateway
    python3 $REPO/mcs/ops/mcs_setup.py check

Notes:
- node.js is NOT a dependency — the pipeline and plugin are pure
  Python (stdlib + hermes-bundled discord.py).
- Jev/typesafe needs only TYPESAFE_API_KEY — the init wizard stores
  it in ~/.mcs/.env. Nothing to install.
- ollama is shared embedding infra, not required by MCS itself.
EOF
