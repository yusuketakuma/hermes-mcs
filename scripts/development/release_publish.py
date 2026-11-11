#!/usr/bin/env python3
"""v3 publisher内部CLI (M9/W11): identify/package/authorize/publish/verify-register。

全commandはpath引数のみを取り、任意URL・秘密値をstdoutへ出さない。
外部変更を伴うcommand(reserve/publish/verify-register)は既定でdry-run —
検証済みrequestを出力するだけで中央/GitHubを変更しない。実実行は
MCS_PUBLISH_EXECUTE=1 と OIDC workflow環境が揃った固定CIからのみ。
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'mcs'))
import _mcs_path  # noqa: E402,F401 — shared flat module roots
from adapters.sdc import release_auto_contract as _contract  # noqa: E402

TAG_RE = re.compile(r'^v\d+\.\d+\.\d+$')
COMMIT_RE = re.compile(r'^[0-9a-f]{40}$')


def _load(path):
    with open(path, 'rb') as f:
        return json.loads(f.read())


def _dump(path, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2,
                     sort_keys=True).encode() + b'\n'
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _require(ok, code):
    if not ok:
        raise SystemExit('release_publish: ' + code)


# --- inspect-event ---------------------------------------------------------

def inspect_event(event_path, policy_path, main_snapshot_sha, output):
    """GitHub event+固定policyを照合し、build対象のsource identityを出力。

    fork拒否: repository.id/owner.idがpolicyの固定値と一致しない限り拒否。
    tagはvX.Y.Zのみ。GETのみ・外部変更なし。
    main_snapshot_shaはspec §4手順7の読取り時main SHA。実workflowはAPIで
    取得して本CLIへ明示入力する(event payloadには含まれない)。
    """
    event = _load(event_path)
    policy = _load(policy_path)
    _require(isinstance(main_snapshot_sha, str)
             and COMMIT_RE.fullmatch(main_snapshot_sha),
             'MAIN_SNAPSHOT_INVALID')
    repo = event.get('repository') or {}
    owner = repo.get('owner') or {}
    _require(repo.get('id') == policy.get('repository_id'),
             'REPOSITORY_MISMATCH')
    _require(owner.get('id') == policy.get('owner_id'),
             'OWNER_MISMATCH')
    tag = commit = tag_oid = None
    if event.get('ref', '').startswith('refs/tags/'):
        tag = event['ref'][len('refs/tags/'):]
        tag_oid = event.get('after') or event.get('head_commit', {}).get('id')
        commit = tag_oid
    elif event.get('release', {}).get('tag_name'):
        rel = event['release']
        tag = rel['tag_name']
        commit = rel.get('target_commitish')
    _require(tag and TAG_RE.fullmatch(tag), 'TAG_INVALID')
    _require(isinstance(commit, str) and COMMIT_RE.fullmatch(commit),
             'COMMIT_INVALID')
    _require(isinstance(tag_oid, str) and COMMIT_RE.fullmatch(tag_oid or ''),
             'TAG_OID_INVALID')
    request = {'schema_version': 'mcs.release-build-request.local.v1',
               'repository_id': repo['id'], 'owner_id': owner['id'],
               'tag': tag, 'tag_oid': tag_oid, 'commit': commit,
               'main_snapshot_sha': main_snapshot_sha,
               'publisher_policy_sha256': _sha256_file(policy_path),
               'event': 'push' if event.get('ref') else 'release'}
    _dump(output, request)
    return request


# --- check-acceptance ------------------------------------------------------

def check_acceptance(request_path, acceptance_path, rollout_policy_path,
                     output):
    """v3 ReleaseAcceptanceをSEM04–06で検査し、requestのsource identityと
    rollout policyへ束縛する。release-acceptance.json assetはschemaの
    ReleaseAcceptance文書でなければならない(旧flat形式は拒否)。"""
    request = _load(request_path)
    ticket = _load(acceptance_path)
    policy = _load(rollout_policy_path)
    expected_source = {
        'repository_id': request.get('repository_id'),
        'repository_owner_id': request.get('owner_id'),
        'tag': request.get('tag'),
        'tag_oid': request.get('tag_oid'),
        'commit': request.get('commit'),
        'main_snapshot_sha': request.get('main_snapshot_sha'),
    }
    reasons = []
    if any(value is None for value in expected_source.values()):
        reasons.append('REQUEST_SOURCE_INCOMPLETE')
    else:
        try:
            _contract.validate_acceptance(
                ticket, expected_source=expected_source, policy=policy)
        except _contract.ContractError as error:
            reasons.append(error.code)
        except Exception:
            reasons.append('ACCEPTANCE_UNREADABLE')
    result = {'schema_version': 'mcs.release-acceptance-check.local.v1',
              'ok': not reasons, 'reasons': reasons,
              'commit': request.get('commit'), 'tag': request.get('tag'),
              'acceptance_id': (ticket.get('acceptance_id')
                                if isinstance(ticket, dict) else None),
              'mode': (ticket.get('mode')
                       if isinstance(ticket, dict) else None)}
    _dump(output, result)
    if reasons:
        raise SystemExit('release_publish: ACCEPTANCE_REJECTED:'
                         + ','.join(reasons))
    return result


# --- package ---------------------------------------------------------------

def package(request_path, assets_dir, output):
    """固定asset集合をローカルで計測しasset-list.jsonを出力。外部変更なし。"""
    request = _load(request_path)
    _require(COMMIT_RE.fullmatch(str(request.get('commit', ''))),
             'REQUEST_INVALID')
    entries = []
    for name in sorted(os.listdir(assets_dir)):
        path = os.path.join(assets_dir, name)
        if not os.path.isfile(path) or name.startswith('.'):
            continue
        entries.append({'name': name, 'size': os.path.getsize(path),
                        'sha256': _sha256_file(path)})
    _require(entries, 'ASSET_SET_EMPTY')
    asset_set_sha = hashlib.sha256(
        json.dumps(entries, sort_keys=True).encode()).hexdigest()
    result = {'schema_version': 'mcs.release-asset-list.local.v1',
              'commit': request['commit'], 'tag': request.get('tag'),
              'assets': entries, 'asset_set_sha256': asset_set_sha}
    _dump(output, result)
    return result


# --- create-bundle -----------------------------------------------------------

_BUNDLE_REF_PREFIX = 'refs/mcs-publish/'
_BUNDLE_NAME = 'hermes-mcs.bundle'


def _git(*args, timeout_s=120):
    from release_auto_git import Git
    result = Git().run(list(args), timeout_s=timeout_s)
    if result.get('rc') != 0:
        raise SystemExit('release_publish: GIT_FAILED:' + args[0])
    return result


def _bundle_header(path):
    """PUB20: bundle headerを自前走査 — v2・prerequisite無し・単一ref必須。"""
    with open(path, 'rb') as f:
        if f.readline(64) != b'# v2 git bundle\n':
            raise SystemExit('release_publish: BUNDLE_HEADER')
        refs = []
        while True:
            line = f.readline(4097)
            if not line.endswith(b'\n') or len(line) > 4096:
                raise SystemExit('release_publish: BUNDLE_HEADER')
            if line == b'\n':
                break
            if line.startswith(b'-'):
                raise SystemExit('release_publish: BUNDLE_PREREQUISITE')
            if not re.fullmatch(rb'[0-9a-f]{40} [!-~]+\n', line):
                raise SystemExit('release_publish: BUNDLE_HEADER')
            refs.append(line[41:-1].decode('ascii'))
    return refs


def create_bundle(request_path, repo, output_dir, output):
    """PUB20: 対象commit到達履歴のみの完全git bundle v2を作成・検証する。

    --all/pseudo-commit/prerequisite/thin packを作らない。隔離clone内で
    一時refを立ててbundle化し、検証後にrefを除去する。candidateの
    build/install/importは行わない(git plumbingのみ)。
    """
    request = _load(request_path)
    commit = request.get('commit')
    tag = request.get('tag')
    _require(isinstance(commit, str) and COMMIT_RE.fullmatch(commit),
             'REQUEST_INVALID')
    _require(isinstance(tag, str) and TAG_RE.fullmatch(tag), 'TAG_INVALID')
    ref = _BUNDLE_REF_PREFIX + tag
    _git('-C', repo, 'rev-parse', '--verify',
         '--quiet', commit + '^{commit}')
    expected = int(_git('-C', repo, 'rev-list', '--count',
                        commit).get('stdout', '0').strip() or '0')
    _require(expected > 0, 'COMMIT_INVALID')
    os.makedirs(output_dir, exist_ok=True)
    bundle_path = os.path.join(output_dir, _BUNDLE_NAME)
    try:
        _git('-C', repo, 'update-ref', ref, commit)
        _git('-C', repo, 'bundle', 'create', bundle_path, ref,
             timeout_s=600)
    finally:
        _git('-C', repo, 'update-ref', '-d', ref)
    refs = _bundle_header(bundle_path)
    _require(refs == [ref], 'BUNDLE_REFS')
    heads = _git('-C', repo, 'bundle', 'list-heads',
                 bundle_path).get('stdout', '')
    _require(re.fullmatch(r'[0-9a-f]{40} ' + re.escape(ref) + r'\n',
                          heads) is not None, 'BUNDLE_HEADS')
    # 独立repoでのverify + reachable集合の一致(疑似commit/部分履歴を排除)
    with tempfile.TemporaryDirectory(prefix='mcs-publish-verify-') as tmp:
        _git('init', '--quiet', tmp)
        _git('-C', tmp, 'bundle', 'verify', bundle_path)
        # 自分が作ったlocal bundle fileからのfetchのみfile transportを許可。
        # protocol.allow=neverの他protocol(http/ssh/git)は引き続き遮断。
        _git('-c', 'protocol.file.allow=always', '-C', tmp, 'fetch',
             '--quiet', bundle_path, ref + ':refs/mcs-publish/verify')
        got = int(_git('-C', tmp, 'rev-list', '--count',
                       'refs/mcs-publish/verify').get(
                           'stdout', '0').strip() or '0')
        _require(got == expected, 'BUNDLE_HISTORY_MISMATCH')
    result = {'schema_version': 'mcs.release-bundle-proof.local.v1',
              'asset': {'name': _BUNDLE_NAME,
                        'size': os.path.getsize(bundle_path),
                        'sha256': _sha256_file(bundle_path)},
              'commit': commit, 'tag': tag, 'ref': ref,
              'reachable_commits': expected}
    _dump(output, result)
    return result


# --- reserve / publish / verify-register (dry-run既定) ----------------------

def _external_guard():
    """外部変更commandの実実行条件。満たさなければdry-run計画のみ出力。"""
    return (os.environ.get('MCS_PUBLISH_EXECUTE') == '1'
            and bool(os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
                     or os.environ.get('MCS_PUBLISH_OIDC_TOKEN')))


def _plan_common(request_path, extra=None):
    request = _load(request_path)
    _require(COMMIT_RE.fullmatch(str(request.get('commit', ''))),
             'REQUEST_INVALID')
    plan = {'schema_version': 'mcs.release-publish-plan.local.v1',
            'dry_run': not _external_guard(),
            'commit': request['commit'], 'tag': request.get('tag'),
            'repository_id': request.get('repository_id'),
            'publisher_policy_sha256': request.get('publisher_policy_sha256')}
    if extra:
        plan.update(extra)
    return plan


def reserve(request_path, assets_path, output):
    assets = _load(assets_path)
    plan = _plan_common(request_path, {'assets': assets.get('assets'),
                                       'asset_set_sha256':
                                       assets.get('asset_set_sha256')})
    _dump(output, plan)
    return plan


def publish(plan_path, output):
    plan = _load(plan_path)
    _require(plan.get('dry_run') is not False or not _external_guard(),
             'EXECUTE_NOT_PERMITTED')
    obs = {'schema_version': 'mcs.release-publish-observation.local.v1',
           'dry_run': plan.get('dry_run', True),
           'commit': plan.get('commit'), 'tag': plan.get('tag'),
           'note': 'external publish requires fixed CI workflow + OIDC'}
    _dump(output, obs)
    return obs


def verify_register(plan_path, observation_path, output):
    plan = _load(plan_path)
    obs = _load(observation_path)
    record = {'schema_version': 'mcs.release-publication-record.local.v1',
              'dry_run': True, 'commit': plan.get('commit'),
              'tag': plan.get('tag'),
              'observation_commit': obs.get('commit'),
              'note': 'central registration requires publisher OIDC route'}
    _dump(output, record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(prog='release_publish')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('inspect-event')
    p.add_argument('--event', required=True)
    p.add_argument('--policy', required=True)
    p.add_argument('--main-snapshot-sha', required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('check-acceptance')
    p.add_argument('--request', required=True)
    p.add_argument('--acceptance', required=True)
    p.add_argument('--rollout-policy', required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('package')
    p.add_argument('--request', required=True)
    p.add_argument('--assets', required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('create-bundle')
    p.add_argument('--request', required=True)
    p.add_argument('--repo', required=True)
    p.add_argument('--output-dir', required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('reserve')
    p.add_argument('--request', required=True)
    p.add_argument('--assets', required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('publish')
    p.add_argument('--plan', required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('verify-register')
    p.add_argument('--plan', required=True)
    p.add_argument('--observation', required=True)
    p.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    handlers = {
        'inspect-event': lambda a: inspect_event(
            a.event, a.policy, a.main_snapshot_sha, a.output),
        'check-acceptance': lambda a: check_acceptance(
            a.request, a.acceptance, a.rollout_policy, a.output),
        'package': lambda a: package(a.request, a.assets, a.output),
        'create-bundle': lambda a: create_bundle(
            a.request, a.repo, a.output_dir, a.output),
        'reserve': lambda a: reserve(a.request, a.assets, a.output),
        'publish': lambda a: publish(a.plan, a.output),
        'verify-register': lambda a: verify_register(
            a.plan, a.observation, a.output),
    }
    handlers[args.command](args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
