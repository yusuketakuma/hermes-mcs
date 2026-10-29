"""The repository launcher forwards jobs to the Python CLI without running it."""
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_repository_launcher_forwards_jobs_arguments(tmp_path):
    runtime = tmp_path / 'runtime'
    python = runtime / 'venv/bin/python3'
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nprintf "%s\\n" "${PYTHONPATH-unset}" "${PYTHONHOME-unset}" "$@"\n',
                      encoding='ascii')
    python.chmod(0o700)
    env = {**os.environ, 'HOME': str(tmp_path), 'MCS_ROOT': str(runtime),
           'PYTHONPATH': 'synthetic-unused', 'PYTHONHOME': 'synthetic-unused'}
    result = subprocess.run(['/bin/sh', str(ROOT / 'scripts/mcs'), 'jobs', 'failed', '--limit', '3'],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        'unset', 'unset', str(ROOT / 'mcs/ops/mcs_cli.py'), 'jobs', 'failed', '--limit', '3']
    assert result.stderr == ''


def test_repository_launcher_help_lists_jobs():
    result = subprocess.run(['/bin/sh', str(ROOT / 'scripts/mcs'), '--help'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert '|jobs ' in result.stdout
    assert 'jobs: 失敗ジョブの調査と再試行' in result.stdout
