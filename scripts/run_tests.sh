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
    set -- tests integration
fi

# The per-run test home holds every temporary DB, so a RAM-backed parent makes
# the suite much faster than fsyncs on a real disk. Product disk guards read
# the real free space of this volume (attachments need 2.5 GiB, the health
# report warns under 4.5 GiB), so /dev/shm is used only with 6 GiB free plus
# 256 MiB for each further runner that starts beside this one at the same
# moment (ci/run_pytest_groups.py sets MCS_TEST_PARALLEL to its shard count).
shm_dir=${MCS_TEST_SHM_DIR:-/dev/shm}
case ${MCS_TEST_PARALLEL:-} in
    [1-9] | [1-5][0-9] | 6[0-4]) test_parallel=$MCS_TEST_PARALLEL ;;
    *) test_parallel=1 ;;
esac
shm_min_kb=$((6291456 + (test_parallel - 1) * 262144))

# usable_parent DIR: the suite writes, links and executes scripts in DIR.
usable_parent() {
    [ -d "$1" ] && [ -w "$1" ] || return 1
    probe=$(mktemp -d "$1/mcs-test-probe.XXXXXX" 2>/dev/null) || return 1
    printf '#!/bin/sh\nexit 0\n' >"$probe/run" 2>/dev/null &&
        chmod 700 "$probe/run" && "$probe/run" 2>/dev/null &&
        mkdir "$probe/dir" && ln -s "$probe/dir" "$probe/link"
    probe_rc=$?
    rm -rf "$probe" 2>/dev/null || :
    return "$probe_rc"
}

available_kb() {
    df -Pk "$1" 2>/dev/null | awk 'NR == 2 { print $4 }'
}

if [ -n "${MCS_TEST_TMPDIR:-}" ]; then
    if ! usable_parent "$MCS_TEST_TMPDIR"; then
        echo "MCS_TEST_TMPDIR=$MCS_TEST_TMPDIR must be an existing directory where scripts can be written and run." >&2
        exit 2
    fi
    test_parent=$MCS_TEST_TMPDIR
elif [ -z "${TMPDIR:-}" ] && shm_kb=$(available_kb "$shm_dir") &&
    [ -n "$shm_kb" ] && [ "$shm_kb" -ge "$shm_min_kb" ] 2>/dev/null && usable_parent "$shm_dir"; then
    test_parent=$shm_dir
else
    test_parent=${TMPDIR:-/tmp}
fi

test_home=$(mktemp -d "$test_parent/mcs-test-home.XXXXXX")
remove_test_home() {
    rm -rf "$test_home" 2>/dev/null || :
}
trap remove_test_home EXIT
# A signal must remove the test home too, or it stays behind (in RAM on /dev/shm).
# dash may skip the EXIT trap when a signal trap calls exit, so each signal
# trap removes the home itself before exiting.
trap 'remove_test_home; trap - EXIT; exit 129' HUP
trap 'remove_test_home; trap - EXIT; exit 130' INT
trap 'remove_test_home; trap - EXIT; exit 143' TERM
echo "test home: $test_home" >&2
mkdir -p "$test_home/tmp" "$test_home/config" "$test_home/cache" "$test_home/data"
mkdir -p "$test_home/tool-bin"
if test_uv=$(command -v uv); then
    # Keep only uv reachable when the test guard removes a shared Hermes PATH entry.
    case "$test_uv" in /*) ;; *) test_uv="$repo_root/$test_uv" ;; esac
    ln -s "$test_uv" "$test_home/tool-bin/uv"
fi

env -i \
    HOME="$test_home" \
    TMPDIR="$test_home/tmp" \
    XDG_CONFIG_HOME="$test_home/config" \
    XDG_CACHE_HOME="$test_home/cache" \
    XDG_DATA_HOME="$test_home/data" \
    PATH="$test_home/tool-bin:${PATH:-/usr/bin:/bin}" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    MCS_TEST_SANDBOX=1 \
    MCS_TEST_TIMEOUT="${MCS_TEST_TIMEOUT:-300}" \
    "$test_python" -m pytest "$@"
