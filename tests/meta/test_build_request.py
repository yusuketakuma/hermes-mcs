"""PUB10 build_request: tag/upstream/acceptance照合のfail-closed検証。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts' / 'development'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'mcs'))

import build_request as br  # noqa: E402
import release_publish_api as api  # noqa: E402

EXAMPLES = ROOT / 'adapters/sdc/contracts/mcs-auto/v3/examples'
COMMIT = 'a' * 40
MAIN = 'b' * 40
TAG_OID = 'c' * 40
REPO_ID = 12345
OWNER_ID = 678


def _policy():
    return {'repository_id': REPO_ID, 'repository_owner_id': OWNER_ID,
            'publisher_workflow': '.github/workflows/ci.yml'}


def _acceptance(tag='v1.2.3', tag_oid=COMMIT, commit=COMMIT,
                main=MAIN):
    data = json.loads(
        (EXAMPLES / 'acceptance-composed.json').read_text())['data']
    data['source'] = {'repository_id': REPO_ID,
                      'repository_owner_id': OWNER_ID, 'tag': tag,
                      'tag_oid': tag_oid, 'commit': commit,
                      'main_snapshot_sha': main}
    return data


def _run_doc(**over):
    doc = {'event': 'push', 'head_sha': COMMIT, 'conclusion': 'success',
           'status': 'completed', 'path': '.github/workflows/ci.yml',
           'head_repository': {'id': REPO_ID}}
    doc.update(over)
    return doc


def _fake_github(calls, routes):
    def call(method, url, token, **kw):
        calls.append(url)
        for needle, doc in routes:
            if needle in url:
                return (200, doc)
        return (404, None)
    return call


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('MCS_PUBLISH_GITHUB_REPO', 'o/r')
    monkeypatch.setenv('GITHUB_TOKEN', 'tok')
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps(_policy()))
    acc = tmp_path / 'acc.json'
    acc.write_text(json.dumps(_acceptance()))
    event = tmp_path / 'event.json'
    event.write_text(json.dumps({
        'repository_id': REPO_ID, 'repository_owner_id': OWNER_ID,
        'tag': 'v1.2.3', 'upstream_run_id': None,
        'upstream_run_attempt': None, 'source_sha': None,
        'publisher_source_commit': 'd' * 40}))
    return {'policy': str(policy), 'acc': str(acc), 'event': str(event),
            'out': str(tmp_path / 'out.json')}


def _resolve(env, monkeypatch, routes, extra_event=None):
    calls = []
    monkeypatch.setattr(api, '_github_call',
                        _fake_github(calls, routes))
    if extra_event:
        doc = json.loads(Path(env['event']).read_text())
        doc.update(extra_event)
        Path(env['event']).write_text(json.dumps(doc))
    return br.resolve(env['event'], env['policy'], env['acc'],
                      env['out'], gh_token='tok'), calls


def _lightweight_routes():
    return [('/git/ref/tags/v1.2.3',
             {'object': {'type': 'commit', 'sha': COMMIT}}),
            ('/git/ref/heads/main',
             {'object': {'type': 'commit', 'sha': MAIN}})]


def test_tag_parts():
    assert br._tag_parts('v1.2.3') and br._tag_parts('v0.0.1')
    for bad in ('1.2.3', 'v01.2.3', 'v1.2', 'v1.2.3.4', 'v1.2.3-rc',
                'v2147483648.0.0', 'v1.2.3 ' + 'x' * 60, '', None, 1):
        assert not br._tag_parts(bad), bad


def test_composed_path_produces_request(env, monkeypatch):
    req, calls = _resolve(env, monkeypatch, _lightweight_routes())
    assert req['schema_version'] == 'mcs.build-request.v3'
    assert req['source']['commit'] == COMMIT
    assert req['source']['tag_oid'] == COMMIT
    assert req['upstream_run_id'] is None
    assert req['upstream_run_attempt'] is None
    out = json.loads(Path(env['out']).read_text())
    assert out['request_id'] == req['request_id']


def test_request_id_deterministic(env, monkeypatch):
    _resolve(env, monkeypatch, _lightweight_routes())
    first = json.loads(Path(env['out']).read_text())['request_id']
    _resolve(env, monkeypatch, _lightweight_routes())
    second = json.loads(Path(env['out']).read_text())['request_id']
    assert first == second


def test_automatic_path_verifies_run(env, monkeypatch):
    routes = _lightweight_routes() + [
        ('/actions/runs/777/attempts/2', _run_doc())]
    req, calls = _resolve(env, monkeypatch, routes,
                          {'upstream_run_id': 777,
                           'upstream_run_attempt': 2,
                           'source_sha': COMMIT})
    assert req['upstream_run_id'] == 777
    assert req['upstream_run_attempt'] == 2
    assert any('/actions/runs/777/attempts/2' in u for u in calls)


@pytest.mark.parametrize('patch,code', [
    ({'head_repository': {'id': 999}}, 'UPSTREAM_FORK_REJECTED'),
    ({'conclusion': 'failure'}, 'UPSTREAM_RUN_MISMATCH'),
    ({'head_sha': 'e' * 40}, 'UPSTREAM_RUN_MISMATCH'),
    ({'path': '.github/workflows/other.yml'},
     'UPSTREAM_WORKFLOW_MISMATCH'),
])
def test_upstream_run_rejections(env, monkeypatch, patch, code):
    routes = _lightweight_routes() + [
        ('/actions/runs/777/attempts/2', _run_doc(**patch))]
    with pytest.raises(SystemExit, match=code):
        _resolve(env, monkeypatch, routes,
                 {'upstream_run_id': 777, 'upstream_run_attempt': 2,
                  'source_sha': COMMIT})


def test_source_commit_mismatch(env, monkeypatch):
    with pytest.raises(SystemExit, match='SOURCE_COMMIT_MISMATCH'):
        _resolve(env, monkeypatch, _lightweight_routes(),
                 {'source_sha': 'e' * 40})


def test_acceptance_source_mismatch(env, monkeypatch):
    acc = json.loads(Path(env['acc']).read_text())
    acc['source']['commit'] = 'f' * 40
    Path(env['acc']).write_text(json.dumps(acc))
    with pytest.raises(SystemExit, match='ACCEPTANCE_SOURCE_MISMATCH'):
        _resolve(env, monkeypatch, _lightweight_routes())


def test_annotated_tag_peels(env, monkeypatch):
    routes = [('/git/ref/tags/v1.2.3',
               {'object': {'type': 'tag', 'sha': TAG_OID}}),
              ('/git/tags/' + TAG_OID,
               {'object': {'type': 'commit', 'sha': COMMIT}}),
              ('/git/ref/heads/main',
               {'object': {'type': 'commit', 'sha': MAIN}})]
    acc = _acceptance(tag_oid=TAG_OID)
    Path(env['acc']).write_text(json.dumps(acc))
    req, _ = _resolve(env, monkeypatch, routes)
    assert req['source']['tag_oid'] == TAG_OID
    assert req['source']['commit'] == COMMIT


def test_tag_reread_mutation_fails(env, monkeypatch):
    state = {'n': 0}

    def call(method, url, token, **kw):
        state['n'] += 1
        if '/git/ref/tags/' in url:
            # 1回目のpeelはCOMMIT、2回目(再読)で別shaを返す
            sha = COMMIT if state['n'] == 1 else 'e' * 40
            return (200, {'object': {'type': 'commit', 'sha': sha}})
        if '/git/ref/heads/main' in url:
            return (200, {'object': {'type': 'commit', 'sha': MAIN}})
        return (404, None)
    monkeypatch.setattr(api, '_github_call', call)
    with pytest.raises(SystemExit, match='TAG_REREAD_MISMATCH'):
        br.resolve(env['event'], env['policy'], env['acc'], env['out'],
                   gh_token='tok')


def test_repo_policy_mismatch(env, monkeypatch):
    policy = json.loads(Path(env['policy']).read_text())
    policy['repository_id'] = 999
    Path(env['policy']).write_text(json.dumps(policy))
    with pytest.raises(SystemExit, match='REPO_POLICY_MISMATCH'):
        _resolve(env, monkeypatch, _lightweight_routes())


def test_tag_missing_fails(env, monkeypatch):
    with pytest.raises(SystemExit, match='TAG_MISSING'):
        _resolve(env, monkeypatch,
                 [('/git/ref/heads/main',
                   {'object': {'type': 'commit', 'sha': MAIN}})])


def test_dispatch_tag_from_acceptance(env, monkeypatch, tmp_path):
    """event.tag無しのdispatch経路はacceptance.source.tagを対象選択に使う。"""
    doc = json.loads(Path(env['event']).read_text())
    doc['tag'] = None
    Path(env['event']).write_text(json.dumps(doc))
    req, _ = _resolve(env, monkeypatch, _lightweight_routes())
    assert req['source']['tag'] == 'v1.2.3'
    assert req['upstream_run_id'] is None
