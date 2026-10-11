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
import json

import pytest

collect_ignore_glob = []
# the hermes checkout's own venv carries every third-party import the
# hermes-bound files need at collection time — a partial environment
# (gateway importable, discord/slack_sdk/dotenv missing) must
# skip too, not fail at import
_hermes_deps = ("gateway", "hermes_cli", "discord", "dotenv", "slack_sdk")
if any(importlib.util.find_spec(mod) is None for mod in _hermes_deps):
    collect_ignore_glob.append("integration/test_hermes_*")


def _deeper_than(text, limit):
    depth, in_string, escaped = 0, False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth -= 1
    return False


@pytest.fixture
def recursion_limited_json(monkeypatch):
    """Decode like the recursion-limited json of Python <= 3.12.

    Python 3.13+ decodes deep nesting iteratively, so the unreadable-record
    branches are kept under test by failing nesting deeper than the old
    limit. The returned list records each deep payload that was refused."""
    original, hits = json.JSONDecoder.raw_decode, []

    def raw_decode(self, text, idx=0):   # json.loads/load decode through here too
        if _deeper_than(text[idx:], 990):
            hits.append(len(text))
            raise RecursionError("synthetic decoder depth failure")
        return original(self, text, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", raw_decode)
    return hits
