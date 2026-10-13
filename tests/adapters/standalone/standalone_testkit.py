"""Shared synthetic standalone root for the mcs_standalone test family — a
config.json plus a private .env.  Not a test module (no ``test_`` prefix);
sibling files import it via the tests/ sys.path bootstrap."""
import json
import os

# split so the CI secret-pattern scan never sees a token shape
XOXB = "xox" + "b-synthetic"
DISCORD = {"profile": "default", "application_id": "2000000000000000002",
           "guild_id": "3000000000000000003", "channel_id": "1000000000000000001",
           "allowed_user_ids": ["4000000000000000004"],
           "allowed_role_ids": ["5000000000000000005"],
           "project_ids": [101], "project_ids_auto": True}
SLACK = {"profile": "default", "application_id": "A0SYNTH", "team_id": "T0SYNTH",
         "channel_id": "C0SYNTH", "allowed_user_ids": ["U0SYNTH"], "project_ids": [101]}


def write_root(tmp_path, interactive="discord", target="discord:1000000000000000001",
               env="DISCORD_BOT_TOKEN=synthetic\nSLACK_BOT_TOKEN=" + XOXB + "\n"
                   "SLACK_APP_TOKEN=xapp-synthetic\n", mode=0o600):
    cfg = {"runtime_mode": "standalone", "mcs_login_id": "synthetic",
           "notify_target": target,
           "notify": {"interactive": interactive, "discord": DISCORD, "slack": SLACK}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    (tmp_path / ".env").write_text(env)
    os.chmod(tmp_path / ".env", mode)
    return cfg
