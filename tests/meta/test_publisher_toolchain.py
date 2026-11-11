"""publisher_toolchain.py (M9/W12) の実測lock生成・照合を合成toolで検証。

実git/ghの能力確認・ネットワークには触れない。tmp_pathの偽実行ファイルで
PATHを差し替え、version/digest/capabilityの各分岐を確認する。
pythonは実行中のinterpreterを実測対象とする(ローカル・外部通信なし)。
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "development" / "publisher_toolchain.py"


def _run(*args):
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, timeout=30,
                          env=dict(os.environ))


def _fake_tool(directory, name, version='9.9.9', caps=True):
    path = directory / name
    if name == 'gh':
        body = ('#!/bin/sh\n'
                'if [ "$1" = "--version" ]; then\n'
                f'  echo "gh version {version}"\n  exit 0\nfi\n'
                'if [ "$1" = "release" ] && [ "$3" = "--help" ]; then\n'
                + ('  exit 0\n' if caps else '  exit 1\n')
                + 'fi\nexit 1\n')
    else:
        body = f'#!/bin/sh\necho "{name} version {version}"\n'
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
               | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name in ('git', 'gh'):
        _fake_tool(bindir, name)
    monkeypatch.setenv('PATH', str(bindir))
    return bindir


def test_measure_writes_lock_with_real_digests(fake_bin, tmp_path):
    out = tmp_path / 'lock.json'
    proc = _run('measure', '--output', str(out))
    assert proc.returncode == 0, proc.stderr
    lock = json.loads(out.read_text())
    assert lock['schema_version'] == 'mcs.publisher-toolchain.v1'
    assert set(lock['tools']) == {'git', 'gh', 'python'}
    for tool in lock['tools'].values():
        assert tool['path']
        assert tool['version']
        assert len(tool['sha256']) == 64
    assert lock['gh_capabilities'] == {
        'release_verify': True, 'release_verify_asset': True}
    printed = json.loads(proc.stdout)
    assert printed['ok'] is True


def test_measure_refuses_existing_lock(fake_bin, tmp_path):
    out = tmp_path / 'lock.json'
    out.write_text('{}')
    proc = _run('measure', '--output', str(out))
    assert proc.returncode == 2
    assert json.loads(proc.stdout)['code'] == 'LOCK_EXISTS'


def test_measure_fails_closed_without_gh_capability(fake_bin, tmp_path):
    _fake_tool(fake_bin, 'gh', caps=False)
    out = tmp_path / 'l.json'
    proc = _run('measure', '--output', str(out))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result['code'] == 'TOOLCHAIN_UNAVAILABLE'
    assert 'verify-capability' in result['detail']
    assert not out.exists()


def test_measure_fails_closed_on_missing_tool(fake_bin, tmp_path):
    (fake_bin / 'git').unlink()
    proc = _run('measure', '--output', str(tmp_path / 'l.json'))
    assert proc.returncode == 2
    assert json.loads(proc.stdout)['code'] == 'TOOLCHAIN_UNAVAILABLE'


def test_check_passes_on_matching_lock(fake_bin, tmp_path):
    lock_path = tmp_path / 'lock.json'
    assert _run('measure', '--output', str(lock_path)).returncode == 0
    proc = _run('check', '--lock', str(lock_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)['ok'] is True


def test_check_detects_toolchain_drift(fake_bin, tmp_path):
    lock_path = tmp_path / 'lock.json'
    assert _run('measure', '--output', str(lock_path)).returncode == 0
    _fake_tool(fake_bin, 'git', version='1.0.0-drift')
    proc = _run('check', '--lock', str(lock_path))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result['code'] == 'TOOLCHAIN_MISMATCH'
    assert 'tools' in result['detail']


def test_check_rejects_bad_lock(fake_bin, tmp_path):
    bad = tmp_path / 'bad.json'
    bad.write_text('{"schema_version": "other"}')
    proc = _run('check', '--lock', str(bad))
    assert proc.returncode == 2
    assert json.loads(proc.stdout)['code'] == 'LOCK_SCHEMA'
