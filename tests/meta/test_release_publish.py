"""release_publish.py (M9/W11) のlocal command・dry-run境界を合成fileで検証。"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "development" / "release_publish.py"
COMMIT = "a" * 40


def _run(*args, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, timeout=30,
                          env=full_env)


def _event(tmp_path, **over):
    event = {'ref': 'refs/tags/v1.2.3', 'after': COMMIT,
             'repository': {'id': 12345,
                            'owner': {'id': 777}}}
    event.update(over)
    path = tmp_path / 'event.json'
    path.write_text(json.dumps(event))
    return path


def _policy(tmp_path, **over):
    policy = {'repository_id': 12345, 'owner_id': 777,
              'tag_pattern': '^v\\d+\\.\\d+\\.\\d+$'}
    policy.update(over)
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(policy))
    return path


def test_inspect_event_happy(tmp_path):
    out = tmp_path / 'build-request.json'
    result = _run('inspect-event', '--event', str(_event(tmp_path)),
                  '--policy', str(_policy(tmp_path)),
                  '--output', str(out))
    assert result.returncode == 0, result.stderr
    doc = json.loads(out.read_text())
    assert doc['tag'] == 'v1.2.3' and doc['commit'] == COMMIT
    assert doc['repository_id'] == 12345 and doc['owner_id'] == 777
    assert len(doc['publisher_policy_sha256']) == 64


def test_inspect_event_rejects_fork_repo(tmp_path):
    """publisher fork拒否: repository.id不一致は拒否。"""
    out = tmp_path / 'out.json'
    result = _run('inspect-event', '--event', str(_event(tmp_path)),
                  '--policy', str(_policy(tmp_path, repository_id=999)),
                  '--output', str(out))
    assert result.returncode != 0
    assert 'REPOSITORY_MISMATCH' in result.stderr
    assert not out.exists()


def test_inspect_event_rejects_non_tag_ref(tmp_path):
    event = _event(tmp_path, ref='refs/heads/main')
    result = _run('inspect-event', '--event', str(event),
                  '--policy', str(_policy(tmp_path)),
                  '--output', str(tmp_path / 'o.json'))
    assert result.returncode != 0


def test_inspect_event_rejects_bad_tag(tmp_path):
    event = _event(tmp_path, ref='refs/tags/nightly')
    result = _run('inspect-event', '--event', str(event),
                  '--policy', str(_policy(tmp_path)),
                  '--output', str(tmp_path / 'o.json'))
    assert result.returncode != 0
    assert 'TAG_INVALID' in result.stderr


def test_check_acceptance_binds_same_commit(tmp_path):
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': COMMIT, 'tag': 'v1.2.3'}))
    acc = tmp_path / 'acc.json'
    acc.write_text(json.dumps({
        'acceptance_id': '12345678-1234-4234-8234-123456789abc',
        'source_commit': COMMIT,
        'acceptance_sha256': 'b' * 64}))
    out = tmp_path / 'check.json'
    result = _run('check-acceptance', '--request', str(req),
                  '--acceptance', str(acc), '--output', str(out))
    assert result.returncode == 0
    assert json.loads(out.read_text())['ok'] is True


def test_check_acceptance_rejects_other_sha(tmp_path):
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': COMMIT, 'tag': 'v1.2.3'}))
    acc = tmp_path / 'acc.json'
    acc.write_text(json.dumps({
        'acceptance_id': '12345678-1234-4234-8234-123456789abc',
        'source_commit': 'c' * 40,
        'acceptance_sha256': 'b' * 64}))
    out = tmp_path / 'check.json'
    _run('check-acceptance', '--request', str(req),
         '--acceptance', str(acc), '--output', str(out))
    doc = json.loads(out.read_text())
    assert doc['ok'] is False
    assert 'SOURCE_COMMIT_MISMATCH' in doc['reasons']


def test_package_emits_fixed_asset_list(tmp_path):
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': COMMIT, 'tag': 'v1.2.3'}))
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'bundle.tar.gz').write_bytes(b'payload')
    (assets / 'meta.json').write_text('{}')
    out = tmp_path / 'assets.json'
    result = _run('package', '--request', str(req), '--assets', str(assets),
                  '--output', str(out))
    assert result.returncode == 0
    doc = json.loads(out.read_text())
    assert len(doc['assets']) == 2
    assert all(len(a['sha256']) == 64 for a in doc['assets'])
    assert len(doc['asset_set_sha256']) == 64


def test_package_rejects_empty_asset_set(tmp_path):
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': COMMIT}))
    assets = tmp_path / 'assets'
    assets.mkdir()
    result = _run('package', '--request', str(req), '--assets', str(assets),
                  '--output', str(tmp_path / 'o.json'))
    assert result.returncode != 0
    assert 'ASSET_SET_EMPTY' in result.stderr


def _git_repo(tmp_path):
    """実git repoを合成 — 3 commitの到達履歴を持つ。"""
    import subprocess as sp
    repo = tmp_path / 'repo'
    repo.mkdir()
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME='t',
               GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t')
    def git(*args):
        return sp.run(['/usr/bin/git', '-C', str(repo), *args],
                      capture_output=True, text=True, env=env, check=True)
    git('init', '--quiet')
    for i in range(3):
        (repo / f'f{i}.txt').write_text(str(i))
        git('add', '.')
        git('commit', '--quiet', '-m', f'c{i}')
    return repo, git


def test_create_bundle_happy(tmp_path):
    repo, git = _git_repo(tmp_path)
    commit = git('rev-parse', 'HEAD').stdout.strip()
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': commit, 'tag': 'v1.2.3'}))
    out_dir = tmp_path / 'out'
    proof = tmp_path / 'proof.json'
    result = _run('create-bundle', '--request', str(req),
                  '--repo', str(repo), '--output-dir', str(out_dir),
                  '--output', str(proof))
    assert result.returncode == 0, result.stderr
    doc = json.loads(proof.read_text())
    bundle = out_dir / 'hermes-mcs.bundle'
    assert bundle.is_file()
    assert doc['asset']['name'] == 'hermes-mcs.bundle'
    assert doc['asset']['size'] == bundle.stat().st_size
    assert len(doc['asset']['sha256']) == 64
    assert doc['reachable_commits'] == 3
    assert doc['commit'] == commit
    # 一時refは除去済み — repoにmcs-publish refが残らない
    refs = git('for-each-ref', 'refs/mcs-publish').stdout.strip()
    assert refs == ''


def test_create_bundle_rejects_missing_commit(tmp_path):
    repo, _git = _git_repo(tmp_path)
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': 'd' * 40, 'tag': 'v1.2.3'}))
    result = _run('create-bundle', '--request', str(req),
                  '--repo', str(repo), '--output-dir', str(tmp_path / 'o'),
                  '--output', str(tmp_path / 'p.json'))
    assert result.returncode != 0
    assert 'GIT_FAILED' in result.stderr


def test_create_bundle_rejects_bad_tag(tmp_path):
    repo, git = _git_repo(tmp_path)
    commit = git('rev-parse', 'HEAD').stdout.strip()
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': commit, 'tag': 'nightly'}))
    result = _run('create-bundle', '--request', str(req),
                  '--repo', str(repo), '--output-dir', str(tmp_path / 'o'),
                  '--output', str(tmp_path / 'p.json'))
    assert result.returncode != 0
    assert 'TAG_INVALID' in result.stderr


def test_create_bundle_partial_history(tmp_path):
    """root commitを対象にするとその到達履歴のみ — reachable数で検証。"""
    repo, git = _git_repo(tmp_path)
    first = git('rev-list', '--max-parents=0', 'HEAD').stdout.strip()
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': first, 'tag': 'v1.0.0'}))
    proof = tmp_path / 'proof.json'
    result = _run('create-bundle', '--request', str(req),
                  '--repo', str(repo), '--output-dir', str(tmp_path / 'o'),
                  '--output', str(proof))
    assert result.returncode == 0, result.stderr
    assert json.loads(proof.read_text())['reachable_commits'] == 1


def test_external_commands_are_dry_run_by_default(tmp_path):
    """reserve/publish/verify-registerは既定で外部を変更しない。"""
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': COMMIT, 'tag': 'v1.2.3',
                               'repository_id': 1}))
    assets = tmp_path / 'assets.json'
    assets.write_text(json.dumps({'assets': [{'name': 'a'}],
                                  'asset_set_sha256': 'd' * 64}))
    plan = tmp_path / 'plan.json'
    env = {k: v for k, v in os.environ.items()
           if k not in ('MCS_PUBLISH_EXECUTE', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN',
                        'MCS_PUBLISH_OIDC_TOKEN')}
    result = subprocess.run(
        [sys.executable, str(CLI), 'reserve', '--request', str(req),
         '--assets', str(assets), '--output', str(plan)],
        capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0
    doc = json.loads(plan.read_text())
    assert doc['dry_run'] is True
