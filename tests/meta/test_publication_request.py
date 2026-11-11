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
