"""Repo-root pytest guard.

`integration/test_hermes_*` requires a hermes-agent checkout on sys.path
(it imports `gateway.*`). On a plain clone of this repo there is no
hermes — skip those files instead of failing at import. Inside a hermes
checkout they are collected and run normally
(scripts/run_tests.sh <abs path>).

The skip is FILE-scoped, not directory-scoped: pure-synthetic
narratives (mcs + hermes_plugin + fake clients only — e.g.
`test_mcs_recovery_narrative.py`) run without the hermes checkout.
New gateway-bound tests MUST keep the `test_hermes_*` prefix.
"""
import importlib.util

collect_ignore_glob = []
# the hermes checkout's own venv carries every third-party import the
# hermes-bound files need at collection time — a partial environment
# (gateway importable, yaml/discord/slack_sdk/dotenv missing) must
# skip too, not fail at import
_hermes_deps = ("gateway", "hermes_cli", "yaml", "discord", "dotenv",
                "slack_sdk")
if any(importlib.util.find_spec(mod) is None for mod in _hermes_deps):
    collect_ignore_glob.append("integration/test_hermes_*")
