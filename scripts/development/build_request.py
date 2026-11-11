#!/usr/bin/env python3
"""PUB10 resolve_build_request: event/policy/github_readからBuildRequestを組立。

spec 01 §4の手順を実装する。GitHub API読取りはrelease_publish_apiの
no-redirect client経由。曖昧・不一致・取得不能は全てfail-closed。
acceptance文書とpublisher_policy文書はfile入力 — 取得経路は
workflow/artifact側の責務(中央read routeは未定義)。
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'mcs'))
import _mcs_path  # noqa: E402,F401
import release_publish_api as _api  # noqa: E402
from adapters.sdc import release_auto_contract as _contract  # noqa: E402

TAG_LIMIT = 2 ** 31 - 1
MAX_TAG_LEN = 64
MAX_TAG_DEPTH = 8
CI_WORKFLOW_NAME = 'ci'


def _fail(code):
    raise SystemExit('build_request: ' + code)


def _get(path, token, repo, *, opener=None):
    status, doc = _api._github_call(
        'GET', _api.GITHUB_API + '/repos/' + repo + path, token,
        opener=opener, not_found_none=True)
    return doc


def _tag_parts(tag):
    """厳密vX.Y.Z・各数値<=2^31-1・全長<=64。"""
    if not isinstance(tag, str) or len(tag) > MAX_TAG_LEN:
        return False
    parts = tag.split('.')
    if len(parts) != 3 or not parts[0].startswith('v'):
        return False
    nums = [parts[0][1:], parts[1], parts[2]]
    if not all(n.isdigit() and (n == '0' or not n.startswith('0'))
               for n in nums):
        return False
    return all(int(n) <= TAG_LIMIT for n in nums)


def _peel_tag(repo, tag, token, *, opener=None):
    """refs/tags/{tag}を辿り(tag_oid, source_commit)を返す。最大8段。"""
    ref = _api.github_get_tag_ref(repo, tag, token, opener=opener)
    if ref is None:
        _fail('TAG_MISSING')
    tag_oid = ref['sha']
    if ref['type'] == 'commit':
        return tag_oid, tag_oid
    if ref['type'] != 'tag':
        _fail('TAG_TYPE_INVALID')
    seen = {tag_oid}
    oid = tag_oid
    for _depth in range(MAX_TAG_DEPTH):
        status, doc = _api._github_call(
            'GET', _api.GITHUB_API + '/repos/' + repo + '/git/tags/' + oid,
            token, opener=opener)
        obj = doc.get('object') if isinstance(doc, dict) else None
        if not isinstance(obj, dict) or not isinstance(obj.get('sha'), str) \
                or len(obj['sha']) != 40:
            _fail('TAG_PEEL_INVALID')
        if obj.get('type') == 'commit':
            return tag_oid, obj['sha']
        if obj.get('type') != 'tag' or obj['sha'] in seen:
            _fail('TAG_PEEL_INVALID')
        seen.add(obj['sha'])
        oid = obj['sha']
    _fail('TAG_PEEL_TOO_DEEP')


def _verify_upstream_run(repo, run_id, run_attempt, token, *,
                         expected_event, expected_sha, expected_repo_id,
                         expected_workflow_id, opener=None):
    """upstream ci runをID+attemptで取得しevent/head_sha/path/IDを照合。"""
    run = _get('/actions/runs/%d/attempts/%d' % (run_id, run_attempt),
               token, repo, opener=opener)
    if not isinstance(run, dict):
        _fail('UPSTREAM_RUN_MISSING')
    if run.get('event') != expected_event \
            or run.get('head_sha') != expected_sha \
            or run.get('conclusion') != 'success' \
            or run.get('status') != 'completed':
        _fail('UPSTREAM_RUN_MISMATCH')
    path = (run.get('path') or '')
    if path != '.github/workflows/ci.yml':
        _fail('UPSTREAM_WORKFLOW_MISMATCH')
    if run.get('workflow_id') != expected_workflow_id:
        _fail('UPSTREAM_WORKFLOW_MISMATCH')
    head_repo = run.get('head_repository')
    if not isinstance(head_repo, dict) \
            or head_repo.get('id') != expected_repo_id:
        _fail('UPSTREAM_FORK_REJECTED')
    return run


def _main_snapshot(repo, token, *, opener=None):
    status, doc = _api._github_call(
        'GET', _api.GITHUB_API + '/repos/' + repo + '/git/ref/heads/main',
        token, opener=opener, not_found_none=True)
    obj = doc.get('object') if isinstance(doc, dict) else None
    if not isinstance(obj, dict) or obj.get('type') != 'commit' \
            or not isinstance(obj.get('sha'), str):
        _fail('MAIN_SNAPSHOT_UNAVAILABLE')
    return obj['sha']


def resolve(event_path, policy_path, acceptance_path, output,
            *, gh_token=None, opener=None, hint_path=None,
            expect_request_id=None, expect_acceptance_id=None):
    """PUB10本体。event/policy/acceptanceはfile、github_readはtoken経由。"""
    event = json.loads(Path(event_path).read_text())
    policy = json.loads(Path(policy_path).read_text())
    acceptance = json.loads(Path(acceptance_path).read_text())
    return _resolve_docs(event, policy, acceptance, output,
                         gh_token=gh_token, opener=opener,
                         hint_path=hint_path,
                         expect_request_id=expect_request_id,
                         expect_acceptance_id=expect_acceptance_id)


def _resolve_docs(event, policy, acceptance, output,
                  *, gh_token=None, opener=None, hint_path=None,
                  expect_request_id=None, expect_acceptance_id=None):
    gh_token = gh_token or os.environ.get('GITHUB_TOKEN') \
        or os.environ.get('GH_TOKEN')
    if not gh_token:
        _fail('GITHUB_TOKEN_MISSING')
    # 未信頼hint(publication-request artifact)はevent欠損分の補完にのみ
    # 使う — env/github context優先。値の正当性は後段のAPI再照合と
    # acceptance.source完全一致でfail-closed検証される。
    if hint_path:
        hint = json.loads(Path(hint_path).read_text())
        for key in ('tag', 'source_sha', 'upstream_run_id',
                    'upstream_run_attempt'):
            if event.get(key) in (None, '') and hint.get(key) is not None:
                event[key] = hint[key]
    repo_env = _api.github_repo_slug()
    # 1. repo/owner ID一致
    repository_id = event.get('repository_id')
    owner_id = event.get('repository_owner_id')
    _require_int(repository_id, 'REPO_ID_INVALID')
    _require_int(owner_id, 'OWNER_ID_INVALID')
    if repository_id != policy.get('repository_id') \
            or owner_id != policy.get('repository_owner_id'):
        _fail('REPO_POLICY_MISMATCH')
    # 4. 厳密tag。eventに無い経路(dispatch)ではacceptance.source.tagを
    # 対象選択に使う — peel結果とacceptance.sourceの完全一致照合が
    # 後段でfail-closed検証するため、虚偽tagは必ず不一致になる。
    acc_source = acceptance.get('source')
    tag = event.get('tag') \
        or (acc_source.get('tag') if isinstance(acc_source, dict) else None)
    if not _tag_parts(tag):
        _fail('TAG_INVALID')
    # 2/3/6. upstream run照合(eventはpush tag起点のみ)
    run_id = event.get('upstream_run_id')
    run_attempt = event.get('upstream_run_attempt')
    source_sha = event.get('source_sha') or event.get('commit')
    if run_id is None and run_attempt is None:
        # composed受入: 管理面登録経路 — upstream照合は中央側責務
        pass
    else:
        _require_int(run_id, 'RUN_ID_INVALID')
        _require_int(run_attempt, 'RUN_ATTEMPT_INVALID')
        # spec §4手順3: workflow IDをpolicy allowlist値と照合する。
        # policyがci_workflow_idを持たない場合は照合不能=fail-closed。
        workflow_id = policy.get('ci_workflow_id')
        _require_int(workflow_id, 'POLICY_WORKFLOW_ID_INVALID')
        _verify_upstream_run(repo_env, run_id, run_attempt, gh_token,
                             expected_event='push',
                             expected_sha=source_sha,
                             expected_repo_id=repository_id,
                             expected_workflow_id=workflow_id,
                             opener=opener)
    # 5. tag peel — 読取りを2回行い同一結果を要求する(PUB10再読照合)
    tag_oid, commit = _peel_tag(repo_env, tag, gh_token, opener=opener)
    tag_oid2, commit2 = _peel_tag(repo_env, tag, gh_token, opener=opener)
    if (tag_oid, commit) != (tag_oid2, commit2):
        _fail('TAG_REREAD_MISMATCH')
    if source_sha is not None and source_sha != commit:
        _fail('SOURCE_COMMIT_MISMATCH')
    # 7. main snapshot固定(祖先照合はpackage/create-bundle側で実施)
    main_snapshot = _main_snapshot(repo_env, gh_token, opener=opener)
    expected_source = {'repository_id': repository_id,
                       'repository_owner_id': owner_id, 'tag': tag,
                       'tag_oid': tag_oid, 'commit': commit,
                       'main_snapshot_sha': main_snapshot}
    # 8. acceptance照合: source完全一致+6lane schema
    if acc_source != expected_source:
        _fail('ACCEPTANCE_SOURCE_MISMATCH')
    try:
        _contract.parse_document(
            json.dumps(acceptance, ensure_ascii=False).encode(),
            'ReleaseAcceptance', max_bytes=1048576)
    except Exception:
        _fail('ACCEPTANCE_INVALID')
    acceptance_id = acceptance.get('acceptance_id')
    # 手動dispatch入力のacceptance_idは解決した文書と一致必須。
    if expect_acceptance_id \
            and acceptance_id != expect_acceptance_id:
        _fail('EXPECT_ACCEPTANCE_ID_MISMATCH')
    acceptance_sha = _contract.hash_v3('release-acceptance', acceptance)
    # request_id: request本体からの決定的導出(同一入力→同一idで冪等)
    request = {'schema_version': 'mcs.build-request.v3',
               'request_id': '0' * 64,
               'source': expected_source,
               'acceptance_id': acceptance_id,
               'acceptance_sha256': acceptance_sha,
               'upstream_run_id': run_id,
               'upstream_run_attempt': run_attempt,
               'publisher_source_commit': event.get(
                   'publisher_source_commit'),
               'publisher_policy_sha256': _contract.hash_v3(
                   'publisher-policy', policy),
               'created_at': int(time.time())}
    if not _COMMIT_RE.fullmatch(
            str(request['publisher_source_commit'] or '')):
        _fail('PUBLISHER_SOURCE_INVALID')
    # request_id: 内容からの決定的導出 — created_atをpreimageから除き
    # 同一入力のretryで同一idを返す(中央reserveの冪等と整合)。
    request['request_id'] = _contract.hash_v3(
        'build-request', {k: v for k, v in request.items()
                          if k not in ('request_id', 'created_at')})
    # 手動dispatch入力のrequest_idは決定的導出値と一致必須 —
    # admin登録値と再計算値の相違はfail-closedで検出する。
    if expect_request_id and request['request_id'] != expect_request_id:
        _fail('EXPECT_REQUEST_ID_MISMATCH')
    try:
        _contract.parse_document(
            json.dumps(request, ensure_ascii=False).encode(),
            'BuildRequest', max_bytes=65536)
    except Exception:
        _fail('BUILD_REQUEST_INVALID')
    Path(output).write_text(json.dumps(request, ensure_ascii=False,
                                       indent=2, sort_keys=True) + '\n')
    return request


def _require_int(value, code):
    if type(value) is not int or value < 1:
        _fail(code)


_COMMIT_RE = re.compile(r'^[0-9a-f]{40}$')


def _event_from_env():
    """workflowが環境変数からevent相当を構成する(--from-env用)。"""
    def _opt(name):
        value = os.environ.get(name)
        return int(value) if value and value.isdigit() else value

    return {'repository_id': _opt('REPOSITORY_ID'),
            'repository_owner_id': _opt('REPOSITORY_OWNER_ID'),
            'tag': os.environ.get('TAG'),
            'upstream_run_id': _opt('UPSTREAM_RUN_ID'),
            'upstream_run_attempt': _opt('UPSTREAM_RUN_ATTEMPT'),
            'source_sha': os.environ.get('SOURCE_SHA'),
            'publisher_source_commit':
                os.environ.get('PUBLISHER_SOURCE_COMMIT')}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='build_request')
    parser.add_argument('--event')
    parser.add_argument('--from-env', action='store_true',
                        help='eventを環境変数から構成する')
    parser.add_argument('--hint',
                        help='publication-request artifact JSON(未信頼)')
    parser.add_argument('--expect-request-id', default='',
                        help='手動dispatch入力request_id(空=照合しない)')
    parser.add_argument('--expect-acceptance-id', default='',
                        help='手動dispatch入力acceptance_id(空=照合しない)')
    parser.add_argument('--policy', required=True)
    parser.add_argument('--acceptance', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if args.from_env == (args.event is not None):
        _fail('EVENT_SOURCE_AMBIGUOUS')
    expects = {'expect_request_id': args.expect_request_id or None,
               'expect_acceptance_id': args.expect_acceptance_id or None}
    if args.from_env:
        event = _event_from_env()
        policy = json.loads(Path(args.policy).read_text())
        acceptance = json.loads(Path(args.acceptance).read_text())
        _resolve_docs(event, policy, acceptance, args.output,
                      hint_path=args.hint, **expects)
    else:
        resolve(args.event, args.policy, args.acceptance, args.output,
                hint_path=args.hint, **expects)
    return 0


if __name__ == '__main__':
    sys.exit(main())
