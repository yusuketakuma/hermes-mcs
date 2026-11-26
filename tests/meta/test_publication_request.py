"""publication_request.py (W12 hint生成) を合成envで検証。"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "development" / "publication_request.py"

ENV = {
    'REPOSITORY_ID': '12345',
    'REF_NAME': 'v1.2.3',
    'SHA': 'a' * 40,
    'HEAD_COMMIT_ID': 'b' * 40,
    'RUN_ID': '987654',
    'RUN_ATTEMPT': '2',
}


def _run(tmp_path, env_over=None, extra=()):
    env = dict(os.environ)
    env.update(ENV)
    env.update(env_over or {})
    out = tmp_path / 'publication-request.json'
    return subprocess.run([sys.executable, str(SCRIPT), str(out), *extra],
                          capture_output=True, text=True, timeout=15, env=env), out


def test_hint_contains_only_allowed_fields(tmp_path):
    result, out = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    doc = json.loads(out.read_text())
    assert doc['schema_version'] == 'mcs.publication-request-hint.v1'
    assert doc['repository_id'] == 12345
    assert doc['tag'] == 'v1.2.3'
    assert doc['tag_oid'] == 'a' * 40
    assert doc['source_sha'] == 'b' * 40
    assert doc['upstream_run_id'] == 987654
    assert doc['upstream_run_attempt'] == 2
    assert out.stat().st_size <= 4096


def test_missing_head_commit_yields_null_source(tmp_path):
    result, out = _run(tmp_path, {'HEAD_COMMIT_ID': ''})
    assert result.returncode == 0
    assert json.loads(out.read_text())['source_sha'] is None


def test_missing_required_env_fails(tmp_path):
    result, out = _run(tmp_path, {'REPOSITORY_ID': ''})
    assert result.returncode != 0
    assert not out.exists()


def test_publication_job_checks_out_exact_event_source_without_credentials():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    job = workflow.split("  publication-request:\n", 1)[1].split("\n  scope:", 1)[0]
    assert "if: github.event_name == 'push'" in job
    assert "runs-on: ubuntu-latest" in job
    checkout, execute = job.split("      - name: write publication request hint", 1)
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in checkout
    assert "ref: ${{ github.sha }}" in checkout
    assert "persist-credentials: false" in checkout
    assert "run: python3 scripts/development/publication_request.py" in execute
    assert "permissions:" not in job  # inherits contents:read, never write
    assert "contents: read" in workflow.split("jobs:", 1)[0]
    assert "secrets." not in job
    assert "if-no-files-found: error" in job


def test_hint_needs_checkout_and_succeeds_from_synthetic_workspace(tmp_path):
    # No remote request, tag creation or artifact upload. The same relative
    # command fails before checkout and runs once its tracked source exists.
    import shutil

    env = dict(os.environ, **ENV)
    command = [sys.executable, "scripts/development/publication_request.py"]
    before = subprocess.run(command, cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=15)
    assert before.returncode == 2
    assert not (tmp_path / "publication-request.json").exists()
    target = tmp_path / "scripts/development/publication_request.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, target)
    after = subprocess.run(command, cwd=tmp_path, env=env,
                           capture_output=True, text=True, timeout=15)
    assert after.returncode == 0, after.stderr
    hint = json.loads((tmp_path / "publication-request.json").read_text())
    assert hint["tag_oid"] == ENV["SHA"]
    assert hint["repository_id"] == int(ENV["REPOSITORY_ID"])


def test_identify_requires_each_upstream_artifact_and_registered_dispatch_source(tmp_path):
    import textwrap

    workflow = (ROOT / ".github/workflows/release-publish.yml").read_text()
    step = workflow.split("      - name: fetch upstream hint and acceptance artifacts\n", 1)[1]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
    gh = tmp_path / "gh"
    gh.write_text("""#!/bin/sh
set -eu
[ "$1 $2 $3 $4 $6 $8 $9" = "run download 42 -n -D --repo synthetic/repo" ]
[ "$5" != "$FAIL_DOWNLOAD" ] || exit 7
mkdir -p "$7"
case "$5" in
    publication-request) file=publication-request.json ;;
    release-acceptance) file=acceptance.json ;;
    *) exit 8 ;;
esac
[ "$5" = "$EMPTY_DOWNLOAD" ] || echo '{}' > "$7/$file"
""")
    gh.chmod(0o700)
    cases = [
        ("workflow_run", "publication-request", "", "publication-request"),
        ("workflow_run", "release-acceptance", "", "release-acceptance"),
        ("workflow_run", "", "release-acceptance", "acceptance/acceptance.json"),
        ("workflow_dispatch", "", "", "acceptance_id"),
        ("workflow_run", "", "", ""),
    ]
    for index, (event, fail, empty, diagnostic) in enumerate(cases):
        work = tmp_path / str(index)
        work.mkdir()
        result = subprocess.run(["bash", "-c", script], cwd=work, timeout=10, capture_output=True, text=True,
                                env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.defpath,
                                     "GITHUB_EVENT_NAME": event, "REPO": "synthetic/repo", "UPSTREAM_RUN_ID": "42",
                                     "FAIL_DOWNLOAD": fail, "EMPTY_DOWNLOAD": empty})
        assert (result.returncode == 0) == (not diagnostic), result
        if diagnostic:
            assert "::error::" in result.stdout
            assert diagnostic in result.stdout
        if event == "workflow_dispatch":
            assert not list(work.iterdir())
