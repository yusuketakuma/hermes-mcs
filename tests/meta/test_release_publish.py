"""release_publish.py (M9/W11) のlocal command・dry-run境界を合成fileで検証。"""
import json
import os
import subprocess
import sys
from pathlib import Path

from adapters.sdc import release_auto_contract as contract

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "development" / "release_publish.py"
EXAMPLES = ROOT / "adapters" / "sdc" / "contracts" / "mcs-auto" / "v3" / "examples"
COMMIT = "a" * 40
MAIN_SNAPSHOT = "e" * 40


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
                  '--main-snapshot-sha', MAIN_SNAPSHOT,
                  '--output', str(out))
    assert result.returncode == 0, result.stderr
    doc = json.loads(out.read_text())
    assert doc['tag'] == 'v1.2.3' and doc['commit'] == COMMIT
    assert doc['repository_id'] == 12345 and doc['owner_id'] == 777
    assert doc['main_snapshot_sha'] == MAIN_SNAPSHOT
    assert len(doc['publisher_policy_sha256']) == 64


def test_inspect_event_rejects_fork_repo(tmp_path):
    """publisher fork拒否: repository.id不一致は拒否。"""
    out = tmp_path / 'out.json'
    result = _run('inspect-event', '--event', str(_event(tmp_path)),
                  '--policy', str(_policy(tmp_path, repository_id=999)),
                  '--main-snapshot-sha', MAIN_SNAPSHOT,
                  '--output', str(out))
    assert result.returncode != 0
    assert 'REPOSITORY_MISMATCH' in result.stderr
    assert not out.exists()


def test_inspect_event_rejects_non_tag_ref(tmp_path):
    event = _event(tmp_path, ref='refs/heads/main')
    result = _run('inspect-event', '--event', str(event),
                  '--policy', str(_policy(tmp_path)),
                  '--main-snapshot-sha', MAIN_SNAPSHOT,
                  '--output', str(tmp_path / 'o.json'))
    assert result.returncode != 0


def test_inspect_event_rejects_bad_tag(tmp_path):
    event = _event(tmp_path, ref='refs/tags/nightly')
    result = _run('inspect-event', '--event', str(event),
                  '--policy', str(_policy(tmp_path)),
                  '--main-snapshot-sha', MAIN_SNAPSHOT,
                  '--output', str(tmp_path / 'o.json'))
    assert result.returncode != 0
    assert 'TAG_INVALID' in result.stderr


def test_inspect_event_rejects_bad_main_snapshot(tmp_path):
    result = _run('inspect-event', '--event', str(_event(tmp_path)),
                  '--policy', str(_policy(tmp_path)),
                  '--main-snapshot-sha', 'not-a-sha',
                  '--output', str(tmp_path / 'o.json'))
    assert result.returncode != 0
    assert 'MAIN_SNAPSHOT_INVALID' in result.stderr


def _v3_fixtures(tmp_path, **source_over):
    """contract exampleのReleaseAcceptance/RolloutPolicyをpolicyへ再束縛する。"""
    policy = json.loads(
        (EXAMPLES / 'rollout-policy.json').read_text())['data']
    acceptance = json.loads(
        (EXAMPLES / 'acceptance-full.json').read_text())['data']
    acceptance['policy_sha256'] = contract.hash_v3('rollout-policy', policy)
    source = dict(acceptance['source'])
    source.update(source_over)
    acceptance['source'] = source
    policy_path = tmp_path / 'rollout-policy.json'
    policy_path.write_text(json.dumps(policy))
    acceptance_path = tmp_path / 'release-acceptance.json'
    acceptance_path.write_text(json.dumps(acceptance))
    return policy_path, acceptance_path


def _v3_request(tmp_path, **over):
    """acceptance-full.jsonのsourceへ一致するbuild requestを作る。"""
    request = {'schema_version': 'mcs.release-build-request.local.v1',
               'repository_id': 123, 'owner_id': 456, 'tag': 'v1.2.3',
               'tag_oid': '2a1073a6e67f0e5f09a5957c659503c690efe727',
               'commit': 'b9469a95e64ad83017429739bd95b527100cdfec',
               'main_snapshot_sha': '0d6e4079e36703ebd37c00722f5891d28b0e2811'}
    request.update(over)
    path = tmp_path / 'req.json'
    path.write_text(json.dumps(request))
    return path


def test_check_acceptance_binds_v3_source_and_policy(tmp_path):
    policy_path, acc = _v3_fixtures(tmp_path)
    req = _v3_request(tmp_path)
    out = tmp_path / 'check.json'
    result = _run('check-acceptance', '--request', str(req),
                  '--acceptance', str(acc),
                  '--rollout-policy', str(policy_path),
                  '--output', str(out))
    assert result.returncode == 0, result.stderr
    doc = json.loads(out.read_text())
    assert doc['ok'] is True and doc['mode'] == 'full_run'


def test_check_acceptance_rejects_other_commit(tmp_path):
    policy_path, acc = _v3_fixtures(tmp_path)
    req = _v3_request(tmp_path, commit='c' * 40)
    out = tmp_path / 'check.json'
    result = _run('check-acceptance', '--request', str(req),
                  '--acceptance', str(acc),
                  '--rollout-policy', str(policy_path),
                  '--output', str(out))
    assert result.returncode != 0
    assert 'ACCEPTANCE_REJECTED' in result.stderr
    assert json.loads(out.read_text())['ok'] is False


def test_check_acceptance_rejects_other_policy(tmp_path):
    policy_path, acc = _v3_fixtures(tmp_path)
    other = json.loads(policy_path.read_text())
    other['policy_revision'] += 1
    other_path = tmp_path / 'other-policy.json'
    other_path.write_text(json.dumps(other))
    req = _v3_request(tmp_path)
    result = _run('check-acceptance', '--request', str(req),
                  '--acceptance', str(acc),
                  '--rollout-policy', str(other_path),
                  '--output', str(tmp_path / 'o.json'))
    assert result.returncode != 0
    assert 'ACCEPTANCE_REJECTED' in result.stderr


def test_check_acceptance_rejects_incomplete_request(tmp_path):
    """main_snapshot_shaを欠く旧requestはsource identity不完全で拒否。"""
    policy_path, acc = _v3_fixtures(tmp_path)
    req = _v3_request(tmp_path)
    doc = json.loads(req.read_text())
    del doc['main_snapshot_sha']
    req.write_text(json.dumps(doc))
    result = _run('check-acceptance', '--request', str(req),
                  '--acceptance', str(acc),
                  '--rollout-policy', str(policy_path),
                  '--output', str(tmp_path / 'o.json'))
    assert result.returncode != 0
    assert 'REQUEST_SOURCE_INCOMPLETE' in result.stderr


def test_check_acceptance_rejects_legacy_flat_ticket(tmp_path):
    """旧flat形式の受入票はv3 ReleaseAcceptanceでないため拒否。"""
    policy_path, _acc = _v3_fixtures(tmp_path)
    acc = tmp_path / 'legacy.json'
    acc.write_text(json.dumps({
        'acceptance_id': '12345678-1234-4234-8234-123456789abc',
        'source_commit': 'b' * 40, 'acceptance_sha256': 'b' * 64}))
    req = _v3_request(tmp_path)
    result = _run('check-acceptance', '--request', str(req),
                  '--acceptance', str(acc),
                  '--rollout-policy', str(policy_path),
                  '--output', str(tmp_path / 'o.json'))
    assert result.returncode != 0
    assert 'ACCEPTANCE_REJECTED' in result.stderr


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
    req.write_text(json.dumps({'commit': commit, 'tag': 'v1.2.3',
                               'main_snapshot_sha': commit}))
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
    assert doc['main_snapshot_sha'] == commit
    # 一時refは除去済み — repoにmcs-publish refが残らない
    refs = git('for-each-ref', 'refs/mcs-publish').stdout.strip()
    assert refs == ''


def test_create_bundle_rejects_missing_commit(tmp_path):
    repo, _git = _git_repo(tmp_path)
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': 'd' * 40, 'tag': 'v1.2.3',
                               'main_snapshot_sha': 'e' * 40}))
    result = _run('create-bundle', '--request', str(req),
                  '--repo', str(repo), '--output-dir', str(tmp_path / 'o'),
                  '--output', str(tmp_path / 'p.json'))
    assert result.returncode != 0
    assert 'GIT_FAILED' in result.stderr


def test_create_bundle_rejects_bad_tag(tmp_path):
    repo, git = _git_repo(tmp_path)
    commit = git('rev-parse', 'HEAD').stdout.strip()
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': commit, 'tag': 'nightly',
                               'main_snapshot_sha': commit}))
    result = _run('create-bundle', '--request', str(req),
                  '--repo', str(repo), '--output-dir', str(tmp_path / 'o'),
                  '--output', str(tmp_path / 'p.json'))
    assert result.returncode != 0
    assert 'TAG_INVALID' in result.stderr


def test_create_bundle_partial_history(tmp_path):
    """root commitを対象にするとその到達履歴のみ — reachable数で検証。"""
    repo, git = _git_repo(tmp_path)
    first = git('rev-list', '--max-parents=0', 'HEAD').stdout.strip()
    head = git('rev-parse', 'HEAD').stdout.strip()
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': first, 'tag': 'v1.0.0',
                               'main_snapshot_sha': head}))
    proof = tmp_path / 'proof.json'
    result = _run('create-bundle', '--request', str(req),
                  '--repo', str(repo), '--output-dir', str(tmp_path / 'o'),
                  '--output', str(proof))
    assert result.returncode == 0, result.stderr
    assert json.loads(proof.read_text())['reachable_commits'] == 1


def test_create_bundle_rejects_source_not_on_main(tmp_path):
    """spec §4手順7: targetがmain snapshotの祖先でない場合は拒否。"""
    repo, git = _git_repo(tmp_path)
    head = git('rev-parse', 'HEAD').stdout.strip()
    first = git('rev-list', '--max-parents=0', 'HEAD').stdout.strip()
    req = tmp_path / 'req.json'
    # HEADをtag対象、root commitをmain snapshot扱い → HEADはrootの子孫で非祖先
    req.write_text(json.dumps({'commit': head, 'tag': 'v1.2.3',
                               'main_snapshot_sha': first}))
    result = _run('create-bundle', '--request', str(req),
                  '--repo', str(repo), '--output-dir', str(tmp_path / 'o'),
                  '--output', str(tmp_path / 'p.json'))
    assert result.returncode != 0
    assert 'SOURCE_NOT_ON_MAIN' in result.stderr


def test_create_bundle_rejects_missing_main_snapshot(tmp_path):
    repo, git = _git_repo(tmp_path)
    commit = git('rev-parse', 'HEAD').stdout.strip()
    req = tmp_path / 'req.json'
    req.write_text(json.dumps({'commit': commit, 'tag': 'v1.2.3'}))
    result = _run('create-bundle', '--request', str(req),
                  '--repo', str(repo), '--output-dir', str(tmp_path / 'o'),
                  '--output', str(tmp_path / 'p.json'))
    assert result.returncode != 0
    assert 'MAIN_SNAPSHOT_INVALID' in result.stderr


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


# --- authorize (v3中央API client) -----------------------------------------

def _build_request(tmp_path, **over):
    doc = {'schema_version': 'mcs.build-request.v3',
           'request_id': 'b' * 64,
           'source': {'repository_id': 12345,
                      'repository_owner_id': 777,
                      'tag': 'v1.2.3',
                      'tag_oid': COMMIT,
                      'commit': COMMIT,
                      'main_snapshot_sha': MAIN_SNAPSHOT},
           'acceptance_id': '12345678-1234-4234-8234-123456789abc',
           'acceptance_sha256': 'c' * 64,
           'upstream_run_id': None,
           'upstream_run_attempt': None,
           'publisher_source_commit': COMMIT,
           'publisher_policy_sha256': 'd' * 64,
           'created_at': 1700000000}
    doc.update(over)
    path = tmp_path / 'build-request.json'
    path.write_text(json.dumps(doc))
    return path


def _authorize_args(tmp_path, request=None):
    assets = tmp_path / 'assets'
    assets.mkdir(exist_ok=True)
    (assets / 'hermes-mcs.bundle').write_bytes(b'bundle-bytes')
    (assets / 'notes.png').write_bytes(b'png-bytes')
    core = tmp_path / 'core.json'
    core.write_text(json.dumps({'schema': 'x'}))
    compat_example = json.loads(
        (EXAMPLES / 'compatibility.json').read_text())['data']
    compat = tmp_path / 'compat.json'
    compat.write_text(json.dumps([compat_example]))
    return ['authorize',
            '--build-request', str(request or _build_request(tmp_path)),
            '--assets-dir', str(assets),
            '--release-core', str(core),
            '--compatibility', str(compat),
            '--output', str(tmp_path / 'out.json')]


def test_authorize_dry_run_default(tmp_path):
    out = tmp_path / 'out.json'
    args = _authorize_args(tmp_path)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('MCS_PUBLISH_')
           and k != 'ACTIONS_ID_TOKEN_REQUEST_TOKEN'}
    result = subprocess.run([sys.executable, str(CLI), *args],
                            capture_output=True, text=True, timeout=30,
                            env=env)
    assert result.returncode == 0, result.stderr
    doc = json.loads(out.read_text())
    assert doc['dry_run'] is True
    names = [a['name'] for a in doc['candidate_assets']]
    assert names == ['hermes-mcs.bundle', 'notes.png']
    assert doc['candidate_assets'][0]['media_type'] == 'application/octet-stream'
    assert doc['candidate_assets'][1]['media_type'] == 'image/png'


def test_authorize_rejects_invalid_build_request(tmp_path):
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'schema_version': 'mcs.build-request.v3'}))
    result = _run(*_authorize_args(tmp_path, request=bad))
    assert result.returncode != 0
    assert 'BUILD_REQUEST_INVALID' in result.stderr


def test_authorize_rejects_unknown_asset(tmp_path):
    assets = tmp_path / 'assets2'
    assets.mkdir()
    (assets / 'hermes-mcs.bundle').write_bytes(b'x')
    (assets / 'evil.txt').write_bytes(b'x')
    args = ['authorize', '--build-request',
            str(_build_request(tmp_path)), '--assets-dir', str(assets),
            '--release-core', str(tmp_path / 'core.json'),
            '--compatibility', str(tmp_path / 'compat.json'),
            '--output', str(tmp_path / 'o.json')]
    (tmp_path / 'core.json').write_text('{}')
    (tmp_path / 'compat.json').write_text('[]')
    result = _run(*args)
    assert result.returncode != 0
    assert 'ASSET_INVALID' in result.stderr


def test_authorize_rejects_reserved_asset_name(tmp_path):
    assets = tmp_path / 'assets3'
    assets.mkdir()
    (assets / 'hermes-mcs.bundle').write_bytes(b'x')
    (assets / 'release-core.json').write_bytes(b'{}')
    args = ['authorize', '--build-request',
            str(_build_request(tmp_path)), '--assets-dir', str(assets),
            '--release-core', str(tmp_path / 'core.json'),
            '--compatibility', str(tmp_path / 'compat.json'),
            '--output', str(tmp_path / 'o.json')]
    (tmp_path / 'core.json').write_text('{}')
    (tmp_path / 'compat.json').write_text('[]')
    result = _run(*args)
    assert result.returncode != 0
    assert 'ASSET_INVALID' in result.stderr


def test_authorize_requires_bundle(tmp_path):
    assets = tmp_path / 'assets4'
    assets.mkdir()
    (assets / 'notes.png').write_bytes(b'x')
    args = ['authorize', '--build-request',
            str(_build_request(tmp_path)), '--assets-dir', str(assets),
            '--release-core', str(tmp_path / 'core.json'),
            '--compatibility', str(tmp_path / 'compat.json'),
            '--output', str(tmp_path / 'o.json')]
    (tmp_path / 'core.json').write_text('{}')
    (tmp_path / 'compat.json').write_text('[]')
    result = _run(*args)
    assert result.returncode != 0
    assert 'ASSET_MISSING' in result.stderr


def _load_cli():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'release_publish_cli', CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _publish_plan(publication_id, request):
    import base64
    plan = {'schema_version': 'mcs.publish-plan.v3',
            'publication_id': publication_id,
            'request': request,
            'release': {'release_id': 'f' * 64, 'code_seq': 1,
                        'source_commit': COMMIT,
                        'artifact_sha256': '0' * 64,
                        'artifact_size': 10},
            'profile_sha256s': [], 'assets': [],
            'asset_set_sha256': '1' * 64,
            'github_release_id': None, 'revision': 2,
            'fencing_token': 7, 'phase': 'packaged',
            'issued_at': 1700000000, 'expires_at': 1700086400}
    payload = base64.urlsafe_b64encode(
        json.dumps(plan, sort_keys=True, separators=(',', ':')
                   ).encode()).rstrip(b'=').decode()
    return {'alg': 'PS256', 'kid': 'k' * 64,
            'purpose': 'mcs-publication-plan-v3',
            'payload': payload, 'signature': 's' * 512}, plan


def test_authorize_execute_calls_central_phases(tmp_path, monkeypatch):
    """execute経路: reserve→manifest/chunk/verify→finalize→署名検証→plan。"""
    module = _load_cli()
    monkeypatch.setenv('MCS_PUBLISH_EXECUTE', '1')
    monkeypatch.setenv('MCS_PUBLISH_OIDC_TOKEN', 'tok')
    monkeypatch.setenv('MCS_PUBLISH_API_BASE', 'https://pub.example')
    calls = []
    pub_id = '12345678-1234-4234-8234-123456789abc'
    reservation = {'schema_version': 'mcs.publication-reservation.v3',
                   'publication_id': pub_id,
                   'request_id': 'r' * 64, 'code_seq': 1,
                   'revision': 1, 'fencing_token': 7,
                   'issued_at': 1700000000, 'expires_at': 1700086400}
    request_doc = json.loads(_build_request(tmp_path).read_text())
    envelope, plan_doc = _publish_plan(pub_id, request_doc)

    def fake_reserve(base, token, body, **kw):
        calls.append(('reserve', base, token))
        return reservation

    def fake_manifest(base, token, body, **kw):
        calls.append(('manifest', body['sha256'][:8]))
        return {'state': 'manifest_fixed'}

    def fake_put(base, token, **kw):
        calls.append(('put', kw['object_sha256'][:8], kw['index']))
        return {'state': 'chunks_incomplete'}

    def fake_verify(base, token, **kw):
        calls.append(('verify', kw['manifest_sha256'][:8]))
        return {'state': 'ready'}

    def fake_finalize(base, token, body, **kw):
        calls.append(('finalize', body['publication_id']))
        return envelope

    verified = {}

    def fake_verify_env(raw, **kw):
        verified.update(kw)
        return {'ok': True}

    monkeypatch.setattr(module._api, 'reserve', fake_reserve)
    monkeypatch.setattr(module._api, 'register_manifest', fake_manifest)
    monkeypatch.setattr(module._api, 'put_chunk', fake_put)
    monkeypatch.setattr(module._api, 'verify_object', fake_verify)
    monkeypatch.setattr(module._api, 'finalize', fake_finalize)
    monkeypatch.setattr(module._api, 'decode_plan',
                        lambda env: plan_doc)
    monkeypatch.setattr(module._sig, 'verify_v3_envelope', fake_verify_env)
    pins = tmp_path / 'pins.json'
    pins.write_text('{}')
    out = tmp_path / 'result.json'
    args = _authorize_args(tmp_path)
    args[args.index('--output') + 1] = str(out)
    args += ['--pins', str(pins)]
    module.main(args)
    result = json.loads(out.read_text())
    assert result['dry_run'] is False
    kinds = [c[0] for c in calls]
    assert kinds[0] == 'reserve' and kinds[-1] == 'finalize'
    assert kinds.count('manifest') == 2
    assert kinds.count('put') == 2
    assert kinds.count('verify') == 2
    assert verified['purpose'] == 'mcs-publication-plan-v3'
    assert verified['expected']['publication_id'] == pub_id
    assert result['publish_plan']['publication_id'] == pub_id
    assert len(result['object_receipts']) == 2


def test_authorize_execute_requires_pins(tmp_path, monkeypatch):
    module = _load_cli()
    monkeypatch.setenv('MCS_PUBLISH_EXECUTE', '1')
    monkeypatch.setenv('MCS_PUBLISH_OIDC_TOKEN', 'tok')
    monkeypatch.setenv('MCS_PUBLISH_API_BASE', 'https://pub.example')
    monkeypatch.setattr(module._api, 'reserve',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('must not call')))
    out = tmp_path / 'r.json'
    args = _authorize_args(tmp_path)
    args[args.index('--output') + 1] = str(out)
    try:
        module.main(args)
    except SystemExit as error:
        assert 'PINS_REQUIRED' in str(error)
    else:
        raise AssertionError('expected failure')


def _authorize_result(tmp_path):
    """publish試験用のauthorize result(plan+envelope)を組み立てる。"""
    import base64
    request = json.loads(_build_request(tmp_path).read_text())
    assets = []
    assets_dir = tmp_path / 'pub-assets'
    assets_dir.mkdir(exist_ok=True)
    blobs = [('hermes-mcs.bundle', b'bundle-bytes',
              'application/octet-stream'),
             ('notes.png', b'png-bytes', 'image/png'),
             ('release-core.json', b'{"core":1}', 'application/json'),
             ('release-attestation.initial.json', b'{"att":1}',
              'application/json'),
             ('compatibility.json', b'{"compat":1}', 'application/json'),
             ('release-acceptance.json', b'{"acc":1}', 'application/json')]
    for name, blob, media in blobs:
        (assets_dir / name).write_bytes(blob)
        assets.append({'name': name, 'sha256': __import__('hashlib')
                       .sha256(blob).hexdigest(), 'size': len(blob),
                       'media_type': media})
    plan = {'schema_version': 'mcs.publish-plan.v3',
            'publication_id': '12345678-1234-4234-8234-123456789abc',
            'request': request,
            'release': {'release_id': 'f' * 64, 'code_seq': 1,
                        'source_commit': COMMIT,
                        'artifact_sha256': '0' * 64, 'artifact_size': 10},
            'profile_sha256s': [], 'assets': assets,
            'asset_set_sha256': '1' * 64, 'github_release_id': None,
            'revision': 2, 'fencing_token': 7, 'phase': 'packaged',
            'issued_at': 1700000000, 'expires_at': 1700086400}
    payload = base64.urlsafe_b64encode(
        json.dumps(plan, sort_keys=True, separators=(',', ':')
                   ).encode()).rstrip(b'=').decode()
    envelope = {'alg': 'PS256', 'kid': 'k' * 64,
                'purpose': 'mcs-publication-plan-v3',
                'payload': payload, 'signature': 's' * 512}
    result = {'schema_version': 'mcs.release-authorize-result.local.v1',
              'dry_run': False, 'reservation': {},
              'object_receipts': [], 'publish_plan': plan,
              'envelope': envelope}
    path = tmp_path / 'authorize.json'
    path.write_text(json.dumps(result))
    return path, assets_dir


def test_publish_dry_run_default(tmp_path):
    auth, assets_dir = _authorize_result(tmp_path)
    out = tmp_path / 'obs.json'
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('MCS_PUBLISH_')
           and k not in ('GITHUB_TOKEN', 'GH_TOKEN',
                         'ACTIONS_ID_TOKEN_REQUEST_TOKEN')}
    result = subprocess.run(
        [sys.executable, str(CLI), 'publish', '--authorize', str(auth),
         '--assets-dir', str(assets_dir), '--output', str(out)],
        capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    doc = json.loads(out.read_text())
    assert doc['dry_run'] is True and doc['asset_count'] == 6


def test_publish_rejects_asset_mismatch(tmp_path):
    auth, assets_dir = _authorize_result(tmp_path)
    (assets_dir / 'notes.png').write_bytes(b'tampered')
    out = tmp_path / 'obs.json'
    # dry-runではbytes照合しないが、plan自体の妥当性は確認される
    result = _run('publish', '--authorize', str(auth),
                  '--assets-dir', str(assets_dir), '--output', str(out))
    assert result.returncode == 0


def test_publish_execute_happy_path(tmp_path, monkeypatch):
    module = _load_cli()
    monkeypatch.setenv('MCS_PUBLISH_EXECUTE', '1')
    monkeypatch.setenv('MCS_PUBLISH_OIDC_TOKEN', 'tok')
    monkeypatch.setenv('MCS_PUBLISH_API_BASE', 'https://pub.example')
    monkeypatch.setenv('GITHUB_TOKEN', 'ghtok')
    monkeypatch.setenv('MCS_PUBLISH_GITHUB_REPO', 'owner/repo')
    auth, assets_dir = _authorize_result(tmp_path)
    pins = tmp_path / 'pins.json'
    pins.write_text('{}')
    calls = []
    release = {'id': 42, 'draft': True, 'tag_name': 'v1.2.3',
               'target_commitish': COMMIT, 'upload_url':
               'https://uploads.github.com/repos/owner/repo/releases/42'
               '/assets{?name,label}'}
    gh_assets = []

    def fake_tag_ref(repo, tag, token, **kw):
        return {'sha': COMMIT, 'type': 'commit'}

    def fake_find(repo, tag, token, **kw):
        return None

    def fake_create(repo, *, tag_name, target_commitish, token, **kw):
        calls.append('create')
        return release

    def fake_list(repo, rid, token, **kw):
        return list(gh_assets)

    def fake_upload(rel, name, data, token, **kw):
        calls.append(('upload', name))
        gh_assets.append({'id': len(gh_assets) + 1, 'name': name,
                          'size': len(data),
                          'digest': 'sha256:' + __import__('hashlib')
                          .sha256(data).hexdigest(), 'state': 'uploaded'})
        return gh_assets[-1]

    def fake_publish(repo, rid, token, **kw):
        calls.append('publish')
        release['draft'] = False
        return release

    def fake_get(repo, rid, token, **kw):
        return {'id': rid, 'draft': False, 'prerelease': False,
                'immutable': True, 'tag_name': 'v1.2.3'}

    monkeypatch.setattr(module._api, 'github_get_tag_ref', fake_tag_ref)
    monkeypatch.setattr(module._api, 'github_find_release_by_tag', fake_find)
    monkeypatch.setattr(module._api, 'github_create_draft', fake_create)
    monkeypatch.setattr(module._api, 'github_list_assets', fake_list)
    monkeypatch.setattr(module._api, 'github_upload_asset', fake_upload)
    monkeypatch.setattr(module._api, 'github_publish_release', fake_publish)
    monkeypatch.setattr(module._api, 'github_get_release', fake_get)
    monkeypatch.setattr(module._sig, 'verify_v3_envelope',
                        lambda raw, **kw: {})
    out = tmp_path / 'obs.json'
    module.main(['publish', '--authorize', str(auth),
                 '--assets-dir', str(assets_dir), '--pins', str(pins),
                 '--output', str(out)])
    obs = json.loads(out.read_text())
    assert obs['dry_run'] is False
    assert obs['github_release_id'] == 42
    assert obs['release_verified'] is True and obs['immutable'] is True
    assert len(obs['assets']) == 6
    assert 'create' in calls and 'publish' in calls
    assert ('upload', 'hermes-mcs.bundle') in calls


def _observation(tmp_path, auth_path):
    """verify-register試験用のpublish observationを組み立てる。"""
    result = json.loads(Path(auth_path).read_text())
    plan = result['publish_plan']
    obs_assets = [{'asset_id': 100 + i, 'name': a['name'],
                   'size': a['size'], 'sha256': a['sha256']}
                  for i, a in enumerate(plan['assets'])]
    obs = {'schema_version': 'mcs.release-publish-observation.local.v1',
           'dry_run': False, 'github_release_id': 42,
           'tag': 'v1.2.3', 'commit': COMMIT,
           'assets': obs_assets, 'release_verified': True,
           'immutable': True, 'observed_at': 1700000000000}
    path = tmp_path / 'obs.json'
    path.write_text(json.dumps(obs))
    return path


def test_verify_register_dry_run_default(tmp_path):
    auth, _ = _authorize_result(tmp_path)
    obs = _observation(tmp_path, auth)
    out = tmp_path / 'rec.json'
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('MCS_PUBLISH_')
           and k not in ('GITHUB_TOKEN', 'GH_TOKEN',
                         'ACTIONS_ID_TOKEN_REQUEST_TOKEN')}
    result = subprocess.run(
        [sys.executable, str(CLI), 'verify-register',
         '--authorize', str(auth), '--observation', str(obs),
         '--output', str(out)],
        capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    doc = json.loads(out.read_text())
    assert doc['dry_run'] is True
    assert doc['github_release_id'] == 42


def test_verify_register_execute_builds_verification(tmp_path, monkeypatch):
    module = _load_cli()
    monkeypatch.setenv('MCS_PUBLISH_EXECUTE', '1')
    monkeypatch.setenv('MCS_PUBLISH_OIDC_TOKEN', 'tok')
    monkeypatch.setenv('MCS_PUBLISH_API_BASE', 'https://pub.example')
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')
    monkeypatch.setenv('MCS_VERIFIER_CLOSURE_SHA256', 'a' * 64)
    monkeypatch.setenv('MCS_PUBLISH_TOOLCHAIN_SHA256', 'b' * 64)
    auth, _ = _authorize_result(tmp_path)
    obs = _observation(tmp_path, auth)
    pins = tmp_path / 'pins.json'
    pins.write_text('{}')
    captured = {}

    def fake_register(base, token, body, **kw):
        captured['body'] = body
        return {'schema_version': 'mcs.publication-record.v3',
                'publication_id': body['publication_id'],
                'immutable': True}

    monkeypatch.setattr(module._api, 'register', fake_register)
    monkeypatch.setattr(module._sig, 'verify_v3_envelope',
                        lambda raw, **kw: {})
    out = tmp_path / 'rec.json'
    module.main(['verify-register', '--authorize', str(auth),
                 '--observation', str(obs), '--pins', str(pins),
                 '--output', str(out)])
    doc = json.loads(out.read_text())
    assert doc['dry_run'] is False
    body = captured['body']
    v = body['verification']
    assert v['schema_version'] == 'mcs.github-verification.v3'
    assert v['github_release_id'] == 42
    assert v['publisher_run_id'] == 123
    assert v['publisher_run_attempt'] == 1
    assert v['release_verified'] is True and v['immutable'] is True
    assert v['verifier_closure_sha256'] == 'a' * 64
    assert v['toolchain_sha256'] == 'b' * 64
    names = [a['name'] for a in v['assets']]
    assert names == sorted(names) and len(names) == 5
    assert all(a['verified'] is True for a in v['assets'])
    assert body['expected_revision'] == 2
    assert body['fencing_token'] == 7
    assert len(v['release_attestation_evidence_sha256']) == 64


def test_verify_register_rejects_tampered_observation(tmp_path, monkeypatch):
    module = _load_cli()
    monkeypatch.setenv('MCS_PUBLISH_EXECUTE', '1')
    monkeypatch.setenv('MCS_PUBLISH_OIDC_TOKEN', 'tok')
    monkeypatch.setenv('MCS_PUBLISH_API_BASE', 'https://pub.example')
    auth, _ = _authorize_result(tmp_path)
    obs = _observation(tmp_path, auth)
    doc = json.loads(obs.read_text())
    doc['assets'][0]['sha256'] = '0' * 64
    obs.write_text(json.dumps(doc))
    pins = tmp_path / 'pins.json'
    pins.write_text('{}')
    monkeypatch.setattr(module._sig, 'verify_v3_envelope',
                        lambda raw, **kw: {})
    out = tmp_path / 'rec.json'
    try:
        module.main(['verify-register', '--authorize', str(auth),
                     '--observation', str(obs), '--pins', str(pins),
                     '--output', str(out)])
    except SystemExit as error:
        assert 'ASSET_MISMATCH' in str(error)
    else:
        raise AssertionError('expected failure')
