#!/usr/bin/env bash
# Local counterpart of every .github/workflows/ci.yml job, run serially on an
# isolated clean clone of one exact SHA. Only for releases whose hosted CI
# could not start (billing, runner outage) — never a substitute for a CI run
# that failed. The run lines below are copied verbatim from ci.yml and
# tests/meta/test_ci_local_runner.py fails when the two drift.
#
# Usage: scripts/development/ci_local.sh [--out DIR] [--fetch] [JOB ...]
#   --out DIR   evidence directory outside the repository
#               (default: ${TMPDIR:-/tmp}/mcs-ci-local/<sha>-<UTC time>)
#   --fetch     allow network: pip/uv/git fetch of the ci.yml pins into DIR
#   JOB ...     run only these jobs (the run is then reported as partial)
# Interpreters (optional; --fetch builds the missing ones into DIR):
#   MCS_CI_PYTHON             Python of ci.yml python-version with the ci.yml pytest
#   MCS_CI_STANDALONE_PYTHON  same, plus deployment/requirements-standalone.txt
#   MCS_CI_HERMES_AGENT       hermes-agent checkout at the ci.yml ref, with .venv
# Exit: 0 only when all six jobs pass on a clean clone; 1 otherwise; 2 usage.
set -u

JOBS="lint-test hygiene standalone-sdk incident-gates macos hermes-integration"

repo=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd -P)
out=""
fetch=0
selected=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --out) out=${2:?--out needs a directory}; shift 2 ;;
        --fetch) fetch=1; shift ;;
        --list-jobs) for j in $JOBS; do echo "$j"; done; exit 0 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *)
            case " $JOBS " in *" $1 "*) ;; *) echo "unknown job: $1" >&2; exit 2 ;; esac
            selected="$selected $1"; shift ;;
    esac
done
[ -n "$selected" ] || selected=" $JOBS"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

if [ "${_MCS_CI_LOCAL_SRC:-}" != "$repo" ]; then
    # Stage 1: bind the run to one SHA. Any uncommitted or untracked change —
    # in this runner, ci.yml, the Makefile or anything a job reads — would make
    # the evidence describe code that the SHA does not contain, so refuse it.
    if [ -n "$(git -C "$repo" status --porcelain --untracked-files=all)" ]; then
        echo "refusing: $repo has uncommitted or untracked changes; commit them so the result is bound to its SHA" >&2
        exit 2
    fi
    sha=$(git -C "$repo" rev-parse HEAD) || exit 2
    [ -n "$out" ] || out=${TMPDIR:-/tmp}/mcs-ci-local/${sha:0:12}-$(date -u +%Y%m%dT%H%M%SZ)
    mkdir -p "$out" || exit 2
    out=$(cd "$out" && pwd -P)
    case "$out/" in "$repo/"*) echo "--out must be outside the repository" >&2; exit 2 ;; esac
    [ -z "$(ls -A "$out")" ] || { echo "--out must be empty: $out" >&2; exit 2; }
    src=$out/checkout
    git clone --quiet --no-checkout "$repo" "$src" && git -C "$src" checkout --quiet --detach "$sha" || exit 2
    [ "$(git -C "$src" rev-parse HEAD)" = "$sha" ] || { echo "clone is not at $sha" >&2; exit 2; }
    # Stage 2 is the clone's own copy of this runner: jobs, pins and run lines
    # all come from the recorded SHA, never from the caller's files
    fetch_flag=""; [ "$fetch" = 0 ] || fetch_flag=--fetch
    # shellcheck disable=SC2086  # $selected is a word list of validated job names
    exec env _MCS_CI_LOCAL_SRC="$src" _MCS_CI_LOCAL_OUT="$out" _MCS_CI_LOCAL_ORIGIN="$repo" \
        bash "$src/scripts/development/ci_local.sh" $fetch_flag $selected
fi

# Stage 2: running from the isolated clone
src=$repo
out=${_MCS_CI_LOCAL_OUT:?}
sha=$(git -C "$src" rev-parse HEAD) || exit 2
clean_before=$(git -C "$src" status --porcelain --untracked-files=all)
workflow=$src/.github/workflows/ci.yml

# --- pins, read from the clone's ci.yml/Makefile; the runner keeps no copy ---
pin() { grep -oE "$1" "$workflow" | head -n 1 | sed -E "s/$2//"; }
py_pin=$(pin 'python-version: "[0-9.]+"' '^python-version: "|"$')
pytest_pin=$(pin 'pytest==[0-9.]+' '^pytest==')
uv_pin=$(pin 'uv==[0-9.]+' '^uv==')
job_block() { awk -v j="  $1:" '$0==j{f=1;next} f&&/^  [a-z]/{exit} f' "$workflow"; }
hermes_repo=$(job_block hermes-integration | sed -nE 's/^ *repository: *//p' | head -n 1)
hermes_ref=$(job_block hermes-integration | sed -nE 's/^ *ref: *([0-9a-f]{40}).*/\1/p' | head -n 1)
ruff_pin=$(sed -nE 's/.*ruff==([0-9.]+).*/\1/p' "$src/Makefile" | head -n 1)
if [ -z "$py_pin" ] || [ -z "$pytest_pin" ] || [ -z "$uv_pin" ] || [ -z "$ruff_pin" ] ||
    [ -z "$hermes_repo" ] || [ -z "$hermes_ref" ]; then
    echo "could not read the pins from ci.yml/Makefile" >&2; exit 2
fi

if [ "$fetch" = 0 ]; then
    # no network unless asked: pip and uv resolve only from local caches
    export PIP_NO_INDEX=1 UV_OFFLINE=1
fi

# wrapper (not symlink) so a venv interpreter keeps its pyvenv.cfg
shim() {
    mkdir -p "$out/shim-$1"
    for n in python python3; do
        printf '#!/bin/sh\nexec "%s" "$@"\n' "$2" >"$out/shim-$1/$n"
        chmod +x "$out/shim-$1/$n"
    done
    printf '%s' "$out/shim-$1"
}
py_ok() { "$1" -c "import sys; sys.exit(sys.version_info[:2] != tuple(map(int, '$py_pin'.split('.'))))" 2>/dev/null; }
pytest_ok() { "$1" -c "import pytest, sys; sys.exit(pytest.__version__ != '$pytest_pin')" 2>/dev/null; }
make_venv() {  # make_venv DIR PKG...  (network, --fetch only)
    "$base_python" -m venv "$1" >>"$out/fetch.log" 2>&1 &&
        "$1/bin/python" -m pip install --quiet "$@" >>"$out/fetch.log" 2>&1
}

base_python=${MCS_CI_PYTHON:-$(command -v python3 || true)}
reason=""
python_ready() {
    if ! py_ok "$base_python"; then reason="Python $py_pin が見つからない（MCS_CI_PYTHON）"; return 1; fi
    if pytest_ok "$base_python"; then return 0; fi
    if [ "$fetch" = 1 ] && [ ! -x "$out/venv-base/bin/python" ]; then
        make_venv "$out/venv-base" "pytest==$pytest_pin"
    fi
    if pytest_ok "$out/venv-base/bin/python"; then base_python=$out/venv-base/bin/python; return 0; fi
    reason="pytest==$pytest_pin の入った Python $py_pin がない（MCS_CI_PYTHON か --fetch）"; return 1
}

# --- prerequisites: a missing one makes the job 実行不能, never pass ---
prereq_lint_test() {
    python_ready || return 1
    if command -v uv >/dev/null 2>&1; then
        uv run --no-project --with "ruff==$ruff_pin" ruff --version >/dev/null 2>&1 ||
            { reason="uv で ruff==$ruff_pin を用意できない（キャッシュなし。--fetch）"; return 1; }
    else
        [ "$(ruff --version 2>/dev/null)" = "ruff $ruff_pin" ] ||
            { reason="ruff $ruff_pin がない（uv か ruff を導入）"; return 1; }
    fi
}
prereq_hygiene() { command -v shellcheck >/dev/null 2>&1 || { reason="shellcheck が未導入"; return 1; }; }
prereq_incident_gates() { py_ok "$base_python" || { reason="Python $py_pin が見つからない（MCS_CI_PYTHON）"; return 1; }; }
prereq_macos() {
    [ "$(uname -s)" = Darwin ] || { reason="macOS 以外のホスト"; return 1; }
    python_ready
}
prereq_standalone_sdk() {
    standalone_python=${MCS_CI_STANDALONE_PYTHON:-}
    if [ -z "$standalone_python" ] && [ "$fetch" = 1 ] && py_ok "$base_python"; then
        make_venv "$out/venv-standalone" "pytest==$pytest_pin" -r "$src/deployment/requirements-standalone.txt" &&
            standalone_python=$out/venv-standalone/bin/python
    fi
    if [ -z "$standalone_python" ] || ! py_ok "$standalone_python" || ! pytest_ok "$standalone_python"; then
        reason="固定SDKの独立 venv がない（MCS_CI_STANDALONE_PYTHON か --fetch）"; return 1
    fi
}
prereq_hermes_integration() {
    hermes=${MCS_CI_HERMES_AGENT:-}
    if [ -z "$hermes" ] && [ "$fetch" = 1 ] && py_ok "$base_python"; then
        hermes=$out/hermes-agent
        uv_bin=$out/venv-uv/bin/uv
        {
            make_venv "$out/venv-uv" "uv==$uv_pin" &&
                git clone --quiet --no-checkout "https://github.com/$hermes_repo.git" "$hermes" &&
                git -C "$hermes" checkout --quiet --detach "$hermes_ref" &&
                PATH="$(shim base "$base_python"):$PATH" "$uv_bin" venv --python python "$hermes/.venv" &&
                (cd "$hermes" &&
                    "$uv_bin" pip install --python .venv/bin/python -e '.[messaging]' &&
                    "$uv_bin" pip install --python .venv/bin/python --group dev)
        } >>"$out/fetch.log" 2>&1 || { reason="hermes-agent の取得・導入に失敗（fetch.log）"; return 1; }
    fi
    [ -n "$hermes" ] || { reason="hermes-agent checkout がない（MCS_CI_HERMES_AGENT か --fetch）"; return 1; }
    [ "$(git -C "$hermes" rev-parse HEAD 2>/dev/null)" = "$hermes_ref" ] ||
        { reason="hermes-agent が ci.yml の ref $hermes_ref ではない"; return 1; }
    [ -x "$hermes/.venv/bin/python" ] || { reason="$hermes/.venv/bin/python がない"; return 1; }
}

# --- job bodies: ci.yml run lines, verbatim except where noted ---
job_lint_test() {
    make lint
    python3 scripts/development/check_drug_master_bundle.py
    scripts/run_tests.sh
    # PR-only step regenerates and diffs; --check proves the same without writing
    python3 scripts/development/update_readme.py --check
}
job_hygiene() {
    bad=$(git ls-files | grep -E '(^|/)(data/|chrome-profile/|\.env$|\.env\.[^/]*$|config\.json$|config\.json\.[^/]*$|token_cache\.json$)' || true)
    if [ -n "$bad" ]; then
      echo "::error::forbidden files tracked:"; echo "$bad"; exit 1
    fi
    bad=$(grep -rnE '(^|[^[:alnum:]_])adapter/' --include='*.py' --include='*.sh' \
          mcs/ tests/ hermes_plugin/ adapters/ lineworks_adapter/ mcs_standalone/ integration/ install.sh || true)
    if [ -n "$bad" ]; then
      echo "::error::stale adapter/ references:"; echo "$bad"; exit 1
    fi
    bad=$(git ls-files -z | xargs -0 grep -lnE \
          '(xox[baprs]-[0-9A-Za-z-]+|xapp-[0-9]-[A-Z0-9]{8,}-[0-9]+-[0-9a-f]{20,}|ghp_[0-9A-Za-z]{36}|sk-[0-9A-Za-z]{20,}|BEGIN [A-Z ]*PRIVATE KEY)' || true)
    if [ -n "$bad" ]; then
      echo "::error::possible secret committed:"; echo "$bad"; exit 1
    fi
    shellcheck install.sh scripts/*.sh deployment/scripts/*.sh
    grep -q "kind: standalone" hermes_plugin/plugin.yaml
    grep -q "register" hermes_plugin/__init__.py
}
job_standalone_sdk() {
    PATH="$(shim standalone "$standalone_python"):$PATH"
    MCS_TEST_PYTHON="$(command -v python)" sh scripts/check_pinned_sdks.sh standalone
}
job_incident_gates() {
    python3 ci/gates.py
    python3 ci/mine_gates.py --check
}
job_macos() {
    scripts/run_tests.sh tests/meta tests/ops -r fEs
}
job_hermes_integration() {
    MCS_TEST_PYTHON=$hermes/.venv/bin/python MCS_HERMES_SOURCE=$hermes MCS_HERMES_REFERENCE=$hermes \
        sh scripts/check_pinned_sdks.sh hermes
}

summary=$out/summary.tsv
printf 'job\tstatus\texit\tstarted\tfinished\truns_on\thost\tnote\n' >"$summary"
{
    echo "sha=$sha"
    echo "checkout=$src"
    echo "checkout_clean_before=$([ -z "$clean_before" ] && echo yes || echo no)"
    echo "caller=${_MCS_CI_LOCAL_ORIGIN:-}（未commit・untracked の変更なしを確認済み）"
    echo "host=$(uname -srm)"
    echo "fetch=$fetch"
    echo "pins: python=$py_pin pytest=$pytest_pin uv=$uv_pin hermes=$hermes_repo@$hermes_ref"
    echo "started=$(now)"
} >"$out/meta.txt"

all_pass=1
[ -z "$clean_before" ] || all_pass=0
for job in $JOBS; do
    fn=${job//-/_}
    runs_on=$(job_block "$job" | sed -nE 's/^ *runs-on: *//p' | head -n 1)
    case "$selected " in *" $job "*) ;; *)
        printf '%s\t未実行\t-\t-\t-\t%s\t-\t対象外に指定\n' "$job" "$runs_on" >>"$summary"; all_pass=0; continue ;;
    esac
    started=$(now); reason=""; log=$out/$job.log
    if ! "prereq_$fn"; then
        echo "実行不能: $reason" >"$log"
        printf '%s\t実行不能\t-\t%s\t%s\t%s\t%s\t%s\n' "$job" "$started" "$(now)" "$runs_on" "$(uname -s)" "$reason" >>"$summary"
        all_pass=0; continue
    fi
    # set -e must apply inside the subshell, so it is never run in a condition
    (
        cd "$src" || exit 2
        PATH="$(shim base "$base_python"):$PATH"
        export MCS_TEST_PYTHON="$base_python"
        set -ex
        "job_$fn"
    ) >"$log" 2>&1
    rc=$?
    status=pass; [ "$rc" = 0 ] || { status=fail; all_pass=0; }
    note=""; [ "$runs_on" = macos-latest ] || note="ubuntu runner 固有の差は未検証"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$job" "$status" "$rc" "$started" "$(now)" "$runs_on" "$(uname -s)" "$note" >>"$summary"
done

clean_after=$(git -C "$src" status --porcelain --untracked-files=all)
[ -z "$clean_after" ] || all_pass=0
{
    echo "checkout_clean_after=$([ -z "$clean_after" ] && echo yes || echo no)"
    echo "finished=$(now)"
} >>"$out/meta.txt"

column -t -s "$(printf '\t')" "$summary" 2>/dev/null || cat "$summary"
echo "evidence: $out"
if [ "$all_pass" = 1 ]; then
    result="ローカル同等チェック: 全6 job pass（$sha）"
else
    result="ローカル同等チェック: 未成立（fail・実行不能・未実行・dirty のいずれかあり）"
fi
printf '%s\n%s\n' "$result" "これは hosted CI の代替記録であり、CI の実行成功ではない。" | tee -a "$out/meta.txt"
[ "$all_pass" = 1 ]
