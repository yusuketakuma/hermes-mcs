#!/bin/sh
# Install the MCS Discord command plugin into a Hermes profile's
# user-plugin directory. The plugin reads the mcs/ package next to
# hermes_plugin/ inside this repo — the whole checkout must stay in
# place; only the plugin dir is symlinked.
#
#   ./install.sh [HERMES_HOME]
#
# HERMES_HOME defaults to ~/.hermes. Re-running is idempotent.
set -eu

REPO="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${1:-$HOME/.hermes}"
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
required — an unset value denies the command):

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
EOF
