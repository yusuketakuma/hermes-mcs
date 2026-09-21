#!/bin/sh
set -eu

# shellcheck disable=SC1007
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1007
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

if [ -n "${MCS_TEST_PYTHON:-}" ]; then
    test_python=$MCS_TEST_PYTHON
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    test_python=$VIRTUAL_ENV/bin/python
elif [ -x "$repo_root/.venv/bin/python" ]; then
    test_python=$repo_root/.venv/bin/python
elif [ -x "$repo_root/venv/bin/python" ]; then
    test_python=$repo_root/venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    test_python=$(command -v python3)
else
    echo "No usable Python interpreter found; set MCS_TEST_PYTHON." >&2
    exit 2
fi

cd "$repo_root"

if [ "$#" -eq 0 ]; then
    set -- tests
fi

test_home=$(mktemp -d "${TMPDIR:-/tmp}/mcs-test-home.XXXXXX")
trap 'rm -rf "$test_home"' EXIT
mkdir -p "$test_home/tmp" "$test_home/config" "$test_home/cache" "$test_home/data"

env -i \
    HOME="$test_home" \
    TMPDIR="$test_home/tmp" \
    XDG_CONFIG_HOME="$test_home/config" \
    XDG_CACHE_HOME="$test_home/cache" \
    XDG_DATA_HOME="$test_home/data" \
    PATH="${PATH:-/usr/bin:/bin}" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    MCS_TEST_SANDBOX=1 \
    "$test_python" -m pytest "$@"
