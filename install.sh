#!/bin/sh
# Install the MCS Discord command plugin into a Hermes profile's
# user-plugin directory. The plugin reads the mcs/ package next to
# hermes_plugin/ inside this repo — the whole checkout must stay in
# place; only the plugin dir is symlinked.
#
#   ./install.sh [HERMES_HOME]
#
# HERMES_HOME defaults to ~/.hermes (the gateway launch profile's home —
# multiplexed gateways discover plugins under the LAUNCH profile, not the
# routed one). Re-running is idempotent.
#
# When `hermes` is not installed, this bootstraps hermes-agent at a pinned
# revision (the one this MCS deployment was validated against) into
# $HERMES_HOME/hermes-agent with the messaging extra, then links
# ~/.local/bin/hermes.
set -eu

REPO="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${1:-$HOME/.hermes}"
HERMES_DIR="$HERMES_HOME/hermes-agent"
HERMES_REPO="https://github.com/yusuketakuma/hermes-agent.git"
# Pinned: v0.21.4 + audit repairs + plugin command_context injection — the
# revision this deployment runs (fork branch: mcs-deploy).
HERMES_PIN="1e8fbfe6b41399a46e232f6cde5618cfdfa2729a"
HERMES_BIN_DIR="$HOME/.local/bin"

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

PLUGINS_DIR="$HERMES_HOME/plugins"
LINK="$PLUGINS_DIR/mcs-discord-commands"

mkdir -p "$PLUGINS_DIR"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    echo "error: $LINK exists and is not a symlink — remove it manually" >&2
    exit 1
fi
ln -sfn "$REPO/hermes_plugin" "$LINK"
echo "linked: $LINK -> $REPO/hermes_plugin"

cat <<'EOF'

Enable and configure it in the profile's config.yaml (all scopes are
required — an unset value denies the command). Note: on a multiplexed
gateway `plugins.enabled` is evaluated under the LAUNCH profile's home,
while `entries.<id>.settings` are read under the profile the command was
routed to — enable in the launch profile, set settings on the serving
profile.

  plugins:
    enabled: [mcs-discord-commands]
    entries:
      mcs-discord-commands:
        settings:
          snapshot: /path/to/mcs/snapshots/ledger-snapshot.db
          inbox: /path/to/mcs/cmd
          allowed_user_ids: ["<discord user id>"]
          allowed_chat_ids: ["<discord chat/channel id>"]
          project_ids: [1]

`mcs_view.py stats/signals` and the scraping pipeline keep their own
data dir at ~/.mcs — see README.md. See hermes_plugin/README.md for the
full command surface and the preview/confirm flow.

For a full machine setup (MCS credentials into macOS Keychain, config.json,
.env secrets, local-LLM and typesafe/Jev requirements) run:

  python3 $REPO/mcs/mcs_setup.py init      # interactive provisioning
  python3 $REPO/mcs/mcs_setup.py check    # validate all conditions

Scheduled collection: the 15-minute unread check and the twice-hourly
durable-job drain are hermes cron jobs (`--no-agent` scripts under
$HERMES_HOME/scripts/); only the event-driven command drain stays on
launchd (local.mcs-cmd, WatchPaths) — template in
$REPO/deployment/launchagents/.
EOF
