"""release_auto_git の実git観測を検証。

実git binaryを使うが、repoはtmp_path配下の合成repoのみ。実MCS・実config・
実dataには触れない。commitは-c user.*注入でidentity不要。
"""
import subprocess

import pytest

import release_auto_git as ag


def _git(repo, *args):
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(repo),
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'}
    return subprocess.run(['/usr/bin/git', '-C', str(repo), *args],
                          env=env, capture_output=True, check=False)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    assert _git(path, 'init', '-q').returncode == 0
    (path / 'f.txt').write_text('one')
    assert _git(path, 'add', 'f.txt').returncode == 0
    assert _git(path, '-c', 'user.email=t@t', '-c', 'user.name=t',
                'commit', '-qm', 'c1').returncode == 0
    return path


def test_run_shape_and_failure(repo):
    git = ag.Git(str(repo))
    head = git.run(['-C', str(repo), 'rev-parse', 'HEAD'])
    assert head['rc'] == 0 and len(head['stdout'].strip()) == 40
    bad = git.run(['-C', str(repo), 'definitely-not-a-command'])
    assert bad['rc'] != 0


def test_run_timeout_and_spawn_failure(tmp_path):
    sleeper = tmp_path / 'sleeper.sh'
    sleeper.write_text('#!/bin/sh\nexec /bin/sleep 30\n')
    sleeper.chmod(0o700)
    hung = ag.Git(str('.'), git_bin=str(sleeper))
    assert hung.run(['rev-parse'], timeout_s=0.2)['error'] == 'TIMEOUT'
    missing = ag.Git(str('.'), git_bin='/nonexistent-git-bin')
    assert missing.run(['rev-parse'])['error'] == 'SPAWN_FAILED'


def test_repository_path_injected(tmp_path):
    git = ag.Git(str(tmp_path / 'x'))
    assert git.repository_path() == str(tmp_path / 'x')


def test_inspect_clean_repo(repo):
    git = ag.Git(str(repo))
    findings = git.git_inspect(str(repo))
    assert findings == {k: False for k in
                        ('alternates', 'replace_refs', 'includes',
                         'hooks_path', 'fsmonitor', 'external_filter')}


def test_inspect_flags(repo):
    git = ag.Git(str(repo))
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'config',
                    'include.path', '/tmp/evil.inc'], check=True)
    assert git.git_inspect(str(repo))['includes'] is True
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'config', '--unset',
                    'include.path'], check=True)
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'config',
                    'core.fsmonitor', 'true'], check=True)
    assert git.git_inspect(str(repo))['fsmonitor'] is True
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'config',
                    'filter.lfs.clean', 'evil'], check=True)
    assert git.git_inspect(str(repo))['external_filter'] is True


def test_inspect_alternates_and_replace(repo):
    git = ag.Git(str(repo))
    info = repo / '.git' / 'objects' / 'info'
    info.mkdir(parents=True, exist_ok=True)
    (info / 'alternates').write_text('/tmp/other\n')
    assert git.git_inspect(str(repo))['alternates'] is True
    (info / 'alternates').unlink()
    head = _git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
    (repo / 'g.txt').write_text('two')
    _git(repo, 'add', 'g.txt')
    _git(repo, '-c', 'user.email=t@t', '-c', 'user.name=t',
         'commit', '-qm', 'c2')
    head2 = _git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
    assert _git(repo, 'replace', head, head2).returncode == 0
    assert git.git_inspect(str(repo))['replace_refs'] is True


def test_inspect_non_repo_fails_closed(tmp_path):
    git = ag.Git(str(tmp_path))
    findings = git.git_inspect(str(tmp_path / 'absent'))
    assert all(findings.values())
