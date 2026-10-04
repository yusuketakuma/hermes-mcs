#!/bin/sh
# Exact SDK lanes. Prepare isolated pinned dependencies offline first.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
: "${MCS_TEST_PYTHON:?select an isolated pinned SDK interpreter}"
case "${1:-}" in
    standalone)
        exec "$repo/scripts/run_tests.sh" -p integration.sdk_safety --sdk-lane standalone \
            "$repo/integration/test_pinned_sdk_versions.py" \
            "$repo/integration/test_standalone_sdk.py" \
            "$repo/integration/test_standalone_discord.py" \
            "$repo/integration/test_standalone_slack_sdk.py" \
            -o addopts= -q -s -r fEs
        ;;
    hermes)
        : "${MCS_HERMES_SOURCE:?select an immutable archive of the CI Hermes commit}"
        : "${MCS_HERMES_REFERENCE:?select the local repository containing CI Git objects}"
        exec "$repo/scripts/run_tests.sh" -p integration.sdk_safety --sdk-lane hermes \
            --hermes-project "$MCS_HERMES_SOURCE/pyproject.toml" \
            --hermes-reference-root "$MCS_HERMES_REFERENCE" \
            "$repo/integration/test_pinned_sdk_versions.py" \
            "$repo/integration/test_hermes_discord.py" \
            "$repo/integration/test_hermes_slack.py" \
            "$repo/integration/test_hermes_config.py" \
            "$repo/tests/adapters/discord/test_discord_sdk_real.py" \
            -o addopts= -q -s -r fEs
        ;;
    *)
        printf '%s\n' 'Usage: sh scripts/check_pinned_sdks.sh standalone|hermes' >&2
        exit 2
        ;;
esac
