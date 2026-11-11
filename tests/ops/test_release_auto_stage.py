"""release_auto_stage の私有stage/sink/verify/digestを検証。

stage_root・repoはtmp_path配下の合成物のみ。git bundleは実gitで生成する
(bundle verifyが実際に走る)。実MCS・実config・実dataには触れない。
"""
import hashlib
import os
import stat
import subprocess

import pytest

import release_auto_git as ag
import release_auto_stage as stage_mod


def _git(repo, *args):
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(repo),
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}
    return subprocess.run(['/usr/bin/git', '-C', str(repo), *args],
                          env=env, capture_output=True, check=False)


@pytest.fixture
def bundle(tmp_path):
    """実git bundleを作り、(repo, bundle_path, target)を返す。"""
    repo = tmp_path / 'src'
    repo.mkdir()
    assert _git(repo, 'init', '-q').returncode == 0
    (repo / 'f.txt').write_text('payload')
    assert _git(repo, 'add', 'f.txt').returncode == 0
    assert _git(repo, '-c', 'user.email=t@t', '-c', 'user.name=t',
                'commit', '-qm', 'c1').returncode == 0
    out = tmp_path / 'artifact.bundle'
    assert _git(repo, 'bundle', 'create', str(out), '--all').returncode == 0
    raw = out.read_bytes()
    target = {'artifact_sha256': hashlib.sha256(raw).hexdigest(),
              'artifact_size': len(raw)}
    return repo, out, target


@pytest.fixture
def stage(tmp_path, bundle):
    repo, _, _ = bundle
    root = tmp_path / 'stage-root'
    root.mkdir(mode=0o700)
    return stage_mod.Stage(stage_root=str(root),
                           git_run=ag.Git(str(repo)).run), root


def _write_stage(stage_obj, target, src):
    sink = stage_obj.stage_sink(target)
    try:
        with open(src, 'rb') as f:
            while chunk := f.read(65536):
                sink.write(chunk)
    finally:
        sink.close()


def test_stage_sink_layout_and_modes(stage, bundle):
    stage_obj, root = stage
    _, src, target = bundle
    sink = stage_obj.stage_sink(target)
    path = stage_obj.bundle_path(target)
    assert os.path.dirname(os.path.dirname(path)) == str(root)
    info = os.lstat(os.path.dirname(path))
    assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
    sink.close()


def test_stage_verify_happy(stage, bundle):
    stage_obj, _ = stage
    _, src, target = bundle
    _write_stage(stage_obj, target, src)
    with open(stage_obj.bundle_path(target), 'rb') as sink:
        assert stage_obj.stage_verify(target, sink) is True


def test_stage_verify_rejects(stage, bundle):
    stage_obj, _ = stage
    _, src, target = bundle
    _write_stage(stage_obj, target, src)
    path = stage_obj.bundle_path(target)
    no_git = stage_mod.Stage(stage_root=os.path.dirname(os.path.dirname(path)))
    with open(path, 'rb') as sink:
        assert no_git.stage_verify(target, sink) is False
    with open(path, 'rb') as good_sink:
        bad = dict(target, artifact_size=target['artifact_size'] + 1)
        assert stage_obj.stage_verify(bad, good_sink) is False
    with open(path, 'r+b') as f:
        f.seek(10)
        f.write(b'X')
    with open(path, 'rb') as sink:
        assert stage_obj.stage_verify(target, sink) is False
    with open(src, 'rb') as other:
        assert stage_obj.stage_verify(target, other) is False


def test_stage_sink_invalid_target(stage):
    stage_obj, _ = stage
    for bad in ({}, {'artifact_sha256': 'x' * 64},
                {'artifact_sha256': 'g' * 64}, None):
        with pytest.raises((ValueError, TypeError)):
            stage_obj.stage_sink(bad)


def test_bundle_path_deterministic(stage, bundle):
    stage_obj, _ = stage
    _, _, target = bundle
    assert (stage_obj.bundle_path(target)
            == stage_obj.bundle_path(dict(target)))
    assert target['artifact_sha256'] in stage_obj.bundle_path(target)


def test_file_digest(stage, bundle):
    _, src, _ = bundle
    digest = stage_mod.file_digest(str(src))
    raw = open(src, 'rb').read()
    assert digest == {'sha256': hashlib.sha256(raw).hexdigest(),
                      'size': len(raw)}
    assert stage_mod.file_digest(str(src) + '.absent') is None
    assert stage_mod.file_digest('/dev/null') is None
