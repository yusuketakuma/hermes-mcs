"""Persist explicit Slack channel setup requests without changing patient routes."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import uuid

import config_edit
import safe_io

_ID = re.compile(r'[A-Z][A-Z0-9]{8,}')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def path(root):
    root = Path(root).absolute()
    if root.resolve() != root:
        raise ValueError('slack_setup_root_unverified')
    for directory in (root, root / 'data'):
        if not directory.exists():
            continue
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022):
            raise ValueError('slack_setup_root_unverified')
    return root / 'data' / 'slack-channel-setup.json'


def _validate(root, state):
    try:
        if (not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != 1
                or state.get('root') != str(Path(root).absolute())
                or str(uuid.UUID(state['request_id'])) != state['request_id']
                or state.get('state') not in ('pending', 'blocked', 'unknown', 'verified', 'applied')):
            raise ValueError
        if set(state) - {'version', 'root', 'request_id', 'plan_hash', 'plan', 'roles',
                         'state', 'reason', 'candidate_hash', 'applied_hash', 'worker_ready'}:
            raise ValueError
        if 'candidate_hash' in state and (state['state'] not in ('verified', 'applied')
                or not re.fullmatch(r'[0-9a-f]{64}', state['candidate_hash'])
                or state['state'] == 'applied' and state['candidate_hash'] != state.get('applied_hash')):
            raise ValueError
        if 'worker_ready' in state and (type(state['worker_ready']) is not bool or state['state'] != 'applied'):
            raise ValueError
        if state['state'] == 'applied' and not re.fullmatch(r'[0-9a-f]{64}', state.get('applied_hash', '')):
            raise ValueError
        planned = state['plan']
        if set(planned) != {'binding', 'users', 'roles', 'base_hash', 'runtime_mode'}:
            raise ValueError
        if state['plan_hash'] != _hash(planned) or not re.fullmatch(r'[0-9a-f]{64}', planned['base_hash']):
            raise ValueError
        binding = planned['binding']
        if (set(binding) != {'team_id', 'application_id', 'profile'}
                or not re.fullmatch(r'T[A-Z0-9]{8,}', binding['team_id'])
                or not re.fullmatch(r'A[A-Z0-9]{8,}', binding['application_id'])
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', binding['profile'])
                or planned['runtime_mode'] not in ('hermes', 'standalone')):
            raise ValueError
        users = planned['users']
        if (not isinstance(users, list) or not users or len(users) > 100
                or users != sorted(set(users))
                or any(not re.fullmatch(r'[UW][A-Z0-9]{8,}', user) for user in users)):
            raise ValueError
        if set(planned['roles']) != {'patient', 'system'} or set(state['roles']) != {'patient', 'system'}:
            raise ValueError
        for role, name in (('patient', 'mcs'), ('system', 'mcslog')):
            original, record = planned['roles'][role], state['roles'][role]
            if (set(original) != {'name', 'id', 'state'} or original['name'] != name
                    or record['name'] != name or record['state'] not in
                    ('pending', 'existing', 'creating', 'created', 'unknown', 'blocked', 'verified')):
                raise ValueError
            original_id, channel_id = original['id'], record['id']
            if original_id is not None:
                if (not re.fullmatch(r'[CG][A-Z0-9]{8,}', original_id)
                        or original['state'] != 'existing' or channel_id != original_id
                        or record.get('create_receipt') is not None):
                    raise ValueError
            elif original['state'] != 'pending':
                raise ValueError
            if channel_id is not None and not re.fullmatch(r'[CG][A-Z0-9]{8,}', channel_id):
                raise ValueError
            receipt = record.get('create_receipt')
            if channel_id is not None and original_id is None:
                if (not isinstance(receipt, dict) or set(receipt) != {'request_id', 'channel_id', 'team_id', 'bot_id'}
                        or receipt['request_id'] != state['request_id'] or receipt['channel_id'] != channel_id
                        or receipt['team_id'] != binding['team_id'] or not re.fullmatch(r'B[A-Z0-9]{8,}', receipt['bot_id'])):
                    raise ValueError
            elif receipt is not None:
                raise ValueError
            if set(record) - {'name', 'id', 'state', 'create_receipt', 'invite_state', 'create_error'}:
                raise ValueError
            if record.get('invite_state') not in (None, 'sending', 'confirmed'):
                raise ValueError
            if record.get('create_error') is not None and not isinstance(record['create_error'], str):
                raise ValueError
            if state['state'] in ('verified', 'applied') and (
                    not channel_id or record['state'] != 'verified'
                    or receipt is not None and record.get('invite_state') != 'confirmed'):
                raise ValueError
        if state['state'] in ('verified', 'applied') and state['roles']['patient']['id'] == state['roles']['system']['id']:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('slack_setup_state_unverified') from None
    return state


def read(root):
    try:
        state = json.loads(safe_io.read_private_bytes(path(root)))
    except FileNotFoundError:
        return None
    return _validate(root, state)


def system_send_profile(root, cfg, destination):
    """Resolve only a created system channel's verified serving profile."""
    state = read(root)
    if state is None:
        return None
    system = state['roles']['system']
    if not system.get('create_receipt') or destination != 'slack:' + system['id']:
        return None
    if state['state'] != 'applied':
        raise ValueError('slack_setup_system_profile_unverified')
    notify = cfg.get('notify')
    scope = notify.get('slack') if isinstance(notify, dict) else None
    binding = ({key: scope.get(key) for key in ('team_id', 'application_id', 'profile')}
               if isinstance(scope, dict) else None)
    configured = cfg.get('notify_system_target')
    if (not isinstance(configured, str) or configured.strip() != destination
            or state['plan']['runtime_mode'] != 'hermes'
            or binding != state['plan']['binding']
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', binding['profile'])):
        raise ValueError('slack_setup_system_profile_unverified')
    return binding['profile']


def save(root, state):
    _validate(root, state)
    safe_io.atomic_write(str(path(root)), lambda stream: stream.write(
        json.dumps(state, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n'), mode=0o600)


def required(cfg):
    """Identify Slack setup targets without reading credentials or external settings."""
    patient = cfg.get('notify_target')
    system = cfg.get('notify_system_target')
    if system and system != patient and not str(system).startswith('slack:'):
        return False
    notify = cfg.get('notify') or {}
    if not isinstance(notify, dict):
        raise ValueError('slack_setup_unverified')
    return notify.get('interactive') == 'slack'


def _channel_roles(cfg):
    patient = cfg.get('notify_target')
    system = cfg.get('notify_system_target')
    if patient not in (None, '') and (not isinstance(patient, str) or not patient.strip()):
        raise ValueError('slack_setup_patient_channel_invalid')
    if system and str(system).startswith('slack:') and not re.fullmatch(r'slack:[CG][A-Z0-9]{8,}', system):
        raise ValueError('slack_setup_system_channel_invalid')
    notify = cfg.get('notify') or {}
    slack_text = isinstance(patient, str) and patient.startswith('slack:')
    scope = notify.get('slack') or {}
    if not isinstance(scope, dict):
        raise ValueError('slack_setup_identity_required')
    card = scope.get('channel_id')
    if card not in (None, '') and (not isinstance(card, str)
            or not re.fullmatch(r'[CG][A-Z0-9]{8,}', card)):
        raise ValueError('slack_setup_card_channel_invalid')
    text_channel = patient.partition(':')[2] if slack_text else None
    if slack_text and not re.fullmatch(r'[CG][A-Z0-9]{8,}', text_channel):
        raise ValueError('slack_setup_patient_channel_invalid')
    patient_channel = text_channel or card or None
    surfaces = {channel for channel in (text_channel, card) if channel}
    system_channel = (system.partition(':')[2]
                      if system and str(system).startswith('slack:') else None)
    system_existing = (system_channel
                       if system_channel and system_channel not in surfaces else None)
    return {
        'patient': {'name': 'mcs', 'id': patient_channel,
                    'state': 'existing' if patient_channel else 'pending'},
        'system': {'name': 'mcslog', 'id': system_existing,
                   'state': 'existing' if system_existing else 'pending'}}


def _configured_channels(cfg):
    if not required(cfg):
        return False
    roles = _channel_roles(cfg)
    scope = (cfg.get('notify') or {}).get('slack') or {}
    return (bool(cfg.get('notify_target')) and bool(scope.get('channel_id'))
            and all(role['state'] == 'existing' for role in roles.values()))


def _post_complete(state):
    return (state['state'] == 'applied'
            and (state['plan']['runtime_mode'] != 'hermes'
                 or state.get('worker_ready') is True
                 or state.get('worker_ready') is not False
                    and state['plan']['roles']['patient']['state'] != 'pending'))


def creation_unneeded(cfg, state):
    """Identify a creation-only no-op without approving current routes or grants."""
    return _configured_channels(cfg) and (state is None or _post_complete(state))


def plan(cfg, *, users=None):
    """Keep explicit separate system routes and describe only missing Slack destinations."""
    if not required(cfg):
        return None
    roles = _channel_roles(cfg)
    scope = (cfg.get('notify') or {}).get('slack') or {}
    binding = {key: scope.get(key) for key in ('team_id', 'application_id', 'profile')}
    binding['profile'] = binding['profile'] or cfg.get('notify_bot_profile') or 'default'
    if any(not isinstance(binding[key], str) or not binding[key].strip()
           for key in binding):
        raise ValueError('slack_setup_identity_required')
    if not all(_ID.fullmatch(binding[key]) for key in ('team_id', 'application_id')):
        raise ValueError('slack_setup_identity_invalid')
    users = scope.get('allowed_user_ids') if users is None else users
    if users is None and cfg.get('runtime_mode', 'hermes') == 'hermes':
        import mcs_util
        import service_management
        import setup_integration
        result = service_management.hermes_cli(mcs_util.hermes_exe(cfg), binding['profile'],
            'config', 'get', setup_integration.PLUGIN_SETTINGS + '.slack_allowed_user_ids', '--json')
        try:
            users = json.loads(result.stdout) if result is not None and result.returncode == 0 else None
        except (ValueError, TypeError, RecursionError):
            users = None
    if not isinstance(users, list) or not users or any(
            not isinstance(user, str) or not re.fullmatch(r'[UW][A-Z0-9]{8,}', user) for user in users):
        raise ValueError('slack_setup_operators_required')
    return {'binding': binding, 'users': sorted(set(users)), 'roles': roles,
        'base_hash': _hash(cfg), 'runtime_mode': cfg.get('runtime_mode', 'hermes')}


def prepare(root, cfg, *, users=None):
    """Only an explicit installer/setup/update call may create this durable intent."""
    if not required(cfg) or creation_unneeded(cfg, read(root)):
        return None
    desired = plan(cfg, users=users)
    if desired is None:
        return None
    path(root).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with config_edit._edit_lock(str(path(root))):
        state = read(root)
        if creation_unneeded(cfg, state):
            return None
        key = _hash(desired)
        if state:
            if state.get('state') == 'verified' and state.get('candidate_hash') == _hash(cfg):
                state.update(state='applied', applied_hash=state['candidate_hash'])
                if state['plan']['runtime_mode'] == 'hermes':
                    state['worker_ready'] = False
                save(root, state)
                return None
            if state.get('state') == 'applied' and state.get('applied_hash') == _hash(cfg):
                return None
            if state.get('plan_hash') != key:
                raise ValueError('slack_setup_existing_intent_conflict')
            return state
        state = {'version': 1, 'root': str(Path(root).absolute()), 'request_id': str(uuid.uuid4()), 'plan_hash': key,
                 'plan': desired, 'roles': deepcopy(desired['roles']), 'state': 'pending'}
        save(root, state)
        return state


def candidate(root, cfg):
    """Return a verified route-only candidate; the setup owner performs its existing CAS."""
    state = read(root)
    if creation_unneeded(cfg, state) or state is None or state.get('state') == 'applied' and state.get('applied_hash') == _hash(cfg):
        return cfg
    if state.get('state') != 'verified' or state.get('plan', {}).get('base_hash') != _hash(cfg):
        raise ValueError('slack_setup_not_ready_or_config_changed')
    roles = state['roles']
    patient, system = roles['patient']['id'], roles['system']['id']
    if patient == system or not all(isinstance(v, str) and re.fullmatch(r'[CG][A-Z0-9]{8,}', v)
                                    for v in (patient, system)):
        raise ValueError('slack_setup_receipt_invalid')
    result = deepcopy(cfg)
    scope = result.setdefault('notify', {}).setdefault('slack', {})
    if not result.get('notify_target'):
        result['notify_target'] = 'slack:' + patient
        if scope.get('channel_id') not in (None, '', patient):
            raise ValueError('slack_setup_patient_scope_conflict')
        scope['channel_id'] = patient
    if not scope.get('channel_id'):
        scope['channel_id'] = patient
    result['notify_system_target'] = 'slack:' + system
    return result


def run_setup(root, cfg, args):
    """Complete an explicit setup request before the caller's final config CAS."""
    import time
    import mcs_util
    import service_management
    import setup_integration
    state = prepare(root, cfg)
    if state is None:
        if required(cfg) and cfg.get('runtime_mode', 'hermes') == 'hermes':
            scope = (cfg.get('notify') or {}).get('slack') or {}
            profile = scope.get('profile') or cfg.get('notify_bot_profile') or 'default'
            if getattr(args, 'plugin_profile', None) not in (None, profile):
                raise ValueError('slack_setup_binding_mismatch')
            args.plugin_profile = profile
        print('既存の通知先を保持し、チャンネルの新規作成は行いません。')
        return cfg
    binding = state['plan']['binding']
    if getattr(args, 'plugin_profile', None) not in (None, binding['profile']):
        raise ValueError('slack_setup_binding_mismatch')
    args.plugin_profile = binding['profile']
    explicit_users = getattr(args, 'plugin_user_ids', None)
    if explicit_users is not None:
        try:
            selected = json.loads(explicit_users) if explicit_users.lstrip().startswith('[') else [
                item.strip() for item in explicit_users.split(',') if item.strip()]
        except (ValueError, TypeError, AttributeError):
            raise ValueError('slack_setup_operators_required') from None
        if not isinstance(selected, list) or sorted(set(selected)) != state['plan']['users']:
            raise ValueError('slack_setup_operator_binding_mismatch')
    args.plugin_user_ids = ','.join(state['plan']['users'])
    with config_edit._edit_lock(str(path(root))):
        state = read(root)
        if state is None:
            raise ValueError('slack_setup_existing_intent_conflict')
        if state and state['state'] == 'blocked' and state.get('reason') == 'slack_setup_scope_required':
            rejected = [record for record in state['roles'].values()
                        if record['state'] == 'blocked' and not record['id']
                        and record.get('create_error') == 'missing_scope']
            if rejected:
                for record in rejected:
                    record['state'] = 'pending'
                    record.pop('create_error', None)
                state.update(state='pending', reason=None)
                save(root, state)
    if state.get('state') in ('verified', 'applied'):
        return candidate(root, cfg)
    if not setup_integration._apply_plugin_integration(cfg, args):
        raise ValueError('slack_setup_credentials_or_scope_required')
    if state['plan']['runtime_mode'] == 'standalone':
        from adapters.slack.standalone import setup_channels
        setup_channels(root, binding)
    else:
        exe = mcs_util.hermes_exe(cfg)
        result = service_management.hermes_cli(exe, binding['profile'], 'gateway', 'status')
        up = result is not None and result.returncode == 0
        commands = ('restart',) if up else ('install', 'start')
        for command in commands:
            result = service_management.hermes_cli(exe, binding['profile'], 'gateway', command)
            if result is None or result.returncode:
                raise ValueError('slack_setup_gateway_unavailable')
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            current = read(root)
            if current and current.get('state') == 'verified':
                break
            if current and current.get('state') in ('blocked', 'unknown'):
                raise ValueError(current.get('reason') or 'slack_setup_unverified')
            time.sleep(0.25)
        else:
            raise ValueError('slack_setup_receipt_pending')
    return candidate(root, cfg)


def preflight_errors(cfg):
    """Defer only absent channel IDs for an explicit new-channel setup candidate."""
    from mcs_config import validate_config
    desired = None if _configured_channels(cfg) else plan(cfg)
    errors, warnings = validate_config(cfg)
    if desired:
        deferred = []
        if cfg.get('notify_target') in (None, ''):
            deferred.append('notify_target')
        if (cfg.get('notify') or {}).get('slack', {}).get('channel_id') in (None, ''):
            deferred.append('notify.slack.channel_id')
        # No fictitious ID enters configuration. Full validation is mandatory after receipt.
        errors = [error for error in errors if not any(
            error.startswith(key + ':')
            or error == f'必須の設定項目がありません: {key}' for key in deferred)]
    return errors, warnings


def report_problem(error):
    code = str(error)
    known = {'slack_setup_identity_required', 'slack_setup_identity_invalid',
             'slack_setup_operators_required', 'slack_setup_patient_channel_invalid',
             'slack_setup_existing_intent_conflict', 'slack_setup_scope_required',
             'slack_setup_name_conflict', 'slack_setup_create_unverified',
             'slack_setup_channel_unverified', 'slack_setup_members_unverified',
             'slack_setup_invite_unverified', 'slack_setup_gateway_unavailable',
             'slack_setup_receipt_pending', 'slack_setup_credentials_or_scope_required',
             'slack_setup_not_ready_or_config_changed'}
    if code not in known:
        code = 'slack_setup_unverified'
    print('Slackのチャンネル準備は未完了です（' + code + '）。既存の患者情報先は保持しています。')
    if code == 'slack_setup_scope_required':
        print('Slackアプリにgroups:write・groups:readを許可して再インストールし、設定を確認してください。')
    return 1


def stage_config(root, previous, saved):
    """Seal the exact verified config before its owner's config CAS."""
    if not required(previous):
        return
    if creation_unneeded(previous, read(root)) and saved == previous:
        return
    with config_edit._edit_lock(str(path(root))):
        state = read(root)
        if creation_unneeded(previous, state) and saved == previous:
            return
        if state is None or candidate(root, previous) != saved:
            raise ValueError('slack_setup_saved_config_mismatch')
        if state['state'] == 'applied':
            return
        state['candidate_hash'] = _hash(saved)
        save(root, state)


def applied(root, previous, saved):
    """Record successful CAS only when its result matches the verified receipt exactly."""
    if creation_unneeded(previous, read(root)) and saved == previous:
        return
    with config_edit._edit_lock(str(path(root))):
        state = read(root)
        if creation_unneeded(previous, state) and saved == previous:
            return
        if state is None:
            return
        if state.get('state') == 'applied':
            if state.get('applied_hash') != _hash(saved):
                raise ValueError('slack_setup_saved_config_mismatch')
            return
        if candidate(root, previous) != saved:
            raise ValueError('slack_setup_saved_config_mismatch')
        if state.get('candidate_hash') not in (None, _hash(saved)):
            raise ValueError('slack_setup_saved_config_mismatch')
        state.update(state='applied', applied_hash=_hash(saved))
        if (state['plan']['runtime_mode'] == 'hermes'
                and (previous.get('notify') or {}).get('slack', {}).get('channel_id')
                    != saved['notify']['slack']['channel_id']):
            state['worker_ready'] = False
        save(root, state)


def reconnect_ready(root, cfg):
    """Use the existing lifecycle owner and require a fresh normal worker witness."""
    import time
    import card_worker_status
    import mcs_util
    import service_management
    profile = cfg['notify']['slack']['profile']
    status_path = card_worker_status._path(Path(root) / 'data', 'slack', profile)
    def witness():
        try:
            return safe_io.read_private_bytes(status_path, 2048)
        except FileNotFoundError:
            return None
    before = witness()
    result = service_management.hermes_cli(mcs_util.hermes_exe(cfg), profile, 'gateway', 'restart')
    if result is None or result.returncode:
        raise ValueError('slack_setup_gateway_unavailable')
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if witness() != before and card_worker_status.status(Path(root) / 'data', 'slack', profile).get('state') == 'started':
            return
        time.sleep(0.25)
    raise ValueError('slack_setup_worker_pending')


def finish_runtime(root, cfg, args):
    """Resume the post-CAS normal-worker step without making another create request."""
    import notify_spool
    import setup_integration
    state = read(root)
    if state is None or state.get('state') != 'applied' or _post_complete(state):
        return
    if state.get('applied_hash') != _hash(cfg):
        raise ValueError('slack_setup_not_ready_or_config_changed')
    args.plugin_profile = state['plan']['binding']['profile']
    args.plugin_user_ids = ','.join(state['plan']['users'])
    if not setup_integration._apply_plugin_integration(cfg, args):
        raise ValueError('slack_setup_credentials_or_scope_required')
    notify_spool.ensure_dirs(str(Path(root) / 'data'))
    notify_spool.publish_flags(cfg, str(Path(root) / 'data'))
    reconnect_ready(root, cfg)
    with config_edit._edit_lock(str(path(root))):
        current = read(root)
        if current != state:
            raise ValueError('slack_setup_existing_intent_conflict')
        current['worker_ready'] = True
        save(root, current)
