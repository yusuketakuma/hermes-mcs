"""Repo-root pytest guard.

`integration/` requires a hermes-agent checkout on sys.path (it imports
`gateway.*`). On a plain clone of this repo there is no hermes — skip the
directory instead of failing at import. Inside a hermes checkout the
test is collected and run normally (scripts/run_tests.sh <abs path>).
"""
import importlib.util

collect_ignore = []
if importlib.util.find_spec("gateway") is None:
    collect_ignore.append("integration")
