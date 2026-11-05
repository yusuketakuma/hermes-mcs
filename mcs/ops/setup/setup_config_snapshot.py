"""Validate configuration snapshots and publish only the reviewed stable settings generation."""
from __future__ import annotations

from contextlib import suppress
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import time

import mcs_util
from mcs_util import CONF_PATH

def _config_problem() -> str | None:
    """None when config.json is absent or a JSON object, else why not.
    load_config() maps both cases to {} — a command that REWRITES the
    file must not treat a broken one as empty (it would silently drop
    every setting). This checks the common file directly, so a symlink,
    non-regular or oversized file is a problem here, not an empty config."""
    try:
        raw = mcs_util.parse_config(mcs_util._read_local(
            CONF_PATH, mcs_util.CONFIG_MAX_BYTES))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as e:
        return f"{type(e).__name__}: {e}" if str(e) else \
            f"{type(e).__name__}: not a regular file within {mcs_util.CONFIG_MAX_BYTES} bytes"
    return None if isinstance(raw, dict) else \
        f"top level is {type(raw).__name__}, not an object"


class _DiagnosticConfigUnreadable(mcs_util.ProviderSettingsUnreadable):
    """Legacy configuration exists but is not a readable JSON object."""


def _diagnostic_config():
    # A single strict load supplies both the configuration and its validity.
    # Missing legacy config remains the unconfigured case; broken config does not.
    try:
        cfg = mcs_util._load_config(CONF_PATH)
    except FileNotFoundError:
        return {}
    except mcs_util.ProviderSettingsUnreadable:
        raise
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise _DiagnosticConfigUnreadable("configuration_unreadable") from error
    if not isinstance(cfg, dict):
        raise _DiagnosticConfigUnreadable("configuration_unreadable")
    return cfg


def _credential_config_snapshot():
    """Read the credential setup destination only from a trusted, unchanged config."""
    import provider_settings
    from safe_io import fdopen_owned

    path = Path(CONF_PATH)
    root = provider_settings._RootHandle(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root.fd)
        with fdopen_owned(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_nlink != 1
                    or info.st_size > mcs_util.CONFIG_MAX_BYTES):
                raise ValueError('config_untrusted')
            raw = stream.read(mcs_util.CONFIG_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = os.stat(path.name, dir_fd=root.fd, follow_symlinks=False)
        identity = provider_settings._identity
        if (len(raw) > mcs_util.CONFIG_MAX_BYTES or identity(info) != identity(after)
                or identity(info) != identity(current)):
            raise ValueError('config_untrusted')
        root.check()
        cfg = mcs_util.parse_config(raw)
        if not isinstance(cfg, dict):
            raise ValueError('config_invalid')
        root.check()
        return cfg, (identity(info), root.identity[:4], root.parent_identity, root.link, raw)
    finally:
        root.close()


def _guard_config(yes: bool) -> bool:
    """False = stop, nothing written: config.json exists but is not a
    readable JSON object. With --yes it is moved aside to
    config.json.corrupt-<ts> (0600, bytes kept) and setup continues
    from defaults."""
    why = _config_problem()
    if why is None:
        return True
    if not yes:
        print(f"設定ファイル{CONF_PATH}を読み取れないか、形式が不正です（詳細: {why}）。"
              "保存していません。ファイルを修正するか、`mcs_setup.py "
              "init --yes`で元のファイルを退避し、初期値から設定してください")
        return False
    base = f"{CONF_PATH}.corrupt-{time.strftime('%Y%m%dT%H%M%S')}"
    dst, n = base, 0
    while os.path.lexists(dst):
        n += 1
        dst = f"{base}.{n}"
    os.rename(CONF_PATH, dst)
    # a moved-aside symlink is kept as a link; its target is never touched
    if not os.path.islink(dst):
        with suppress(OSError):
            os.chmod(dst, 0o600)
    print(f"設定ファイル{CONF_PATH}を読み取れないか、形式が不正でした（詳細: {why}）。"
          f"{dst}へ退避し、初期値から設定を続けます")
    return True


def _init_legacy_snapshot():
    """Capture the initial config or the identity of an unreadable entry."""
    import config_backup
    import config_edit
    try:
        info = os.lstat(CONF_PATH)
    except FileNotFoundError:
        return {}, ('missing', None)
    identity = config_edit._identity(info) + (info.st_uid,)
    if not stat.S_ISREG(info.st_mode) or info.st_size > mcs_util.CONFIG_MAX_BYTES:
        backup = config_backup.snapshot(CONF_PATH)
        if backup.identity != identity:
            raise config_edit.ConfigConflict('config_changed')
        return {}, ('invalid_backup', backup)
    try:
        raw, base = config_edit._snapshot(CONF_PATH)
    except config_edit.ConfigConflict:
        raise
    except (OSError, ValueError):
        # An unreadable regular file is retained by identity; it is never read
        # through a symlink or silently treated as a valid empty config.
        current = os.lstat(CONF_PATH)
        if config_edit._identity(current) + (current.st_uid,) != identity:
            raise config_edit.ConfigConflict('config_changed') from None
        backup = config_backup.snapshot(CONF_PATH)
        if backup.identity != identity:
            raise config_edit.ConfigConflict('config_changed')
        return {}, ('invalid_backup', backup)
    try:
        cfg = config_edit._parse(raw)
    except (ValueError, RecursionError, UnicodeError):
        backup = config_backup.snapshot(CONF_PATH)
        if backup.identity != identity or backup.sha256 != base[1]:
            raise config_edit.ConfigConflict('config_changed')
        return {}, ('invalid_backup', backup)
    return cfg, ('valid', base)


def _init_split_snapshot():
    import cloud_policy
    import provider_settings
    import semantic_jev

    root = Path(os.path.abspath(CONF_PATH)).parent
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    state = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    if state['mode'] != 'split':
        raise ValueError('provider_settings_unreadable')
    snapshot = state['snapshot']
    data_fd = os.open(root / 'data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        namespace_before = provider_settings._provider_namespace(data_fd)
        if provider_settings._directory_anchor(os.fstat(data_fd)) != snapshot['data']:
            raise ValueError('provider_settings_changed')
        names = sorted(name for name in namespace_before if name.startswith(
            ('.provider-settings-backup-', '.provider-settings-stage-')))
        if (provider_settings._provider_namespace(data_fd) != namespace_before
                or provider_settings._directory_anchor(os.fstat(data_fd)) != snapshot['data']):
            raise ValueError('provider_settings_changed')
    finally:
        os.close(data_fd)
    if provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)['snapshot'] != snapshot:
        raise ValueError('provider_settings_changed')
    selection, errors = cloud_policy.resolve_settings(state['common'], str(root))
    if cloud_policy.blocking_errors(errors):
        raise ValueError('provider_settings_unreadable')
    selected = selection['provider']
    view = provider_settings.execution_view(state['common'], state['providers'], selected_provider=selected,
                                            model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    return view, ('split', {'root': str(root), 'state': state, 'owners': owners,
                            'selected': selected, 'view': deepcopy(view), 'artifacts': names})


def _init_config_snapshot():
    import config_edit
    try:
        if mcs_util._provider_config_active(CONF_PATH):
            return _init_split_snapshot()
        result = _init_legacy_snapshot()
        if mcs_util._provider_config_active(CONF_PATH):
            raise config_edit.ConfigConflict('config_changed')
        return result
    except mcs_util.ProviderSettingsUnreadable:
        raise ValueError('provider_settings_unreadable') from None


def _init_split_matches(initial, current):
    if initial[0] != 'split' or current[0] != 'split':
        return False
    old, new = initial[1], current[1]
    a, b = old['state']['snapshot'], new['state']['snapshot']
    return (old['root'] == new['root'] and old['selected'] == new['selected']
            and old['artifacts'] == new['artifacts']
            and mcs_util.canonical(old['view']) == mcs_util.canonical(new['view'])
            and all(a[key] == b[key] for key in ('files', 'authority', 'pending', 'provider_settings'))
            and a.get('root_link') == b.get('root_link')
            and a['root'][:4] == b['root'][:4] and a['data'][:4] == b['data'][:4])


def _init_split_changes(initial, cfg):
    import cloud_policy
    import provider_settings
    import semantic_jev

    state, owners = initial['state'], initial['owners']
    old_common, old_providers = provider_settings.split_legacy(initial['view'], owners, jev_model=semantic_jev.JEV_MODEL)
    new_common, new_providers = provider_settings.split_legacy(cfg, owners, jev_model=semantic_jev.JEV_MODEL)
    common, providers = deepcopy(state['common']), deepcopy(state['providers'])
    def changed(old, new, key):
        return mcs_util.canonical({key: old[key]} if key in old else {}) != \
            mcs_util.canonical({key: new[key]} if key in new else {})
    for key in old_common.keys() | new_common.keys():
        if changed(old_common, new_common, key):
            if key in new_common:
                common[key] = deepcopy(new_common[key])
            else:
                common.pop(key, None)
    selection, errors = cloud_policy.resolve_settings(common, initial['root'])
    if cloud_policy.blocking_errors(errors):
        raise ValueError('provider_settings_invalid')
    selected = selection['provider']
    for name in provider_settings.PROVIDERS:
        for key in old_providers[name].keys() | new_providers[name].keys():
            if key == 'model' and name == initial.get('preserve_model_for'):
                continue
            if key == 'model' and initial.get('replace_cloud') and name != selected:
                continue
            if key == 'profiles' and (initial.get('preserve_openai_profiles')
                                      or initial.get('replace_cloud') and selected != 'openai'):
                continue
            if changed(old_providers[name], new_providers[name], key):
                if key in new_providers[name]:
                    providers[name][key] = deepcopy(new_providers[name][key])
                else:
                    providers[name].pop(key, None)
    if initial.get('replace_cloud') and 'model' not in new_providers[selected]:
        providers[selected].pop('model', None)
    if initial.get('replace_cloud') and selected == 'openai' and 'profiles' not in new_providers['openai']:
        providers['openai'].pop('profiles', None)
    actual = provider_settings.execution_view(common, providers, selected_provider=selected,
                                              model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    if mcs_util.canonical(actual) != mcs_util.canonical(cfg):
        raise ValueError('provider_settings_unrepresentable')
    return common, providers, selected


def _reviewed_split_current(initial):
    import config_edit
    current = _init_split_snapshot()[1]
    if not _init_split_matches(initial, current):
        raise config_edit.ConfigConflict('config_changed')
    return current


def _publish_split_config(current, prepared, lock_fd):
    import provider_settings
    import semantic_jev

    common, providers, selected = prepared
    return provider_settings.commit_locked(current[1]['root'], current[1]['state']['snapshot'], common, providers,
        selected_provider=selected, model_owners=current[1]['owners'], jev_model=semantic_jev.JEV_MODEL,
        lock_fd=lock_fd)


def _cli_legacy_base():
    import config_edit
    try:
        if mcs_util._provider_config_active(CONF_PATH):
            raise config_edit.ConfigConflict('config_changed')
        try:
            base = config_edit._snapshot(CONF_PATH)[1]
        except FileNotFoundError:
            base = None
        if mcs_util._provider_config_active(CONF_PATH):
            raise config_edit.ConfigConflict('config_changed')
        return base
    except mcs_util.ProviderSettingsUnreadable:
        raise config_edit.ConfigConflict('config_changed') from None


def _init_reselect_provider(cfg, initial, set_args):
    if initial[0] != 'split':
        return
    import cloud_policy
    import provider_settings
    import semantic_jev

    info = initial[1]
    before, after = info['view'].get('cloud_llm'), cfg.get('cloud_llm')
    if not isinstance(before, dict) or not isinstance(after, dict):
        return
    touched = {item.partition('=')[0].strip() for item in set_args or []}
    if 'cloud_llm' in touched:
        info['replace_cloud'] = True
        return
    if not any(mcs_util.canonical({k: before[k]} if k in before else {}) !=
               mcs_util.canonical({k: after[k]} if k in after else {})
               for k in ('provider', 'provider_order')):
        return
    candidate = deepcopy(cfg)
    candidate['cloud_llm'].pop('model', None)
    candidate['cloud_llm'].pop('profiles', None)
    selection, errors = cloud_policy.resolve_settings(candidate, info['root'])
    if cloud_policy.blocking_errors(errors) or selection['provider'] == info['selected']:
        return
    info['preserve_model_for'] = info['selected']
    view = provider_settings.execution_view(info['state']['common'], info['state']['providers'],
        selected_provider=selection['provider'], model_owners=info['owners'], jev_model=semantic_jev.JEV_MODEL)
    projected = view.get('cloud_llm', {})
    profile_leaf = any(name.startswith('cloud_llm.profiles.') for name in touched)
    if (selection['provider'] == 'openai' and profile_leaf and 'cloud_llm.profiles' not in touched
            and 'profiles' not in before and isinstance(after.get('profiles'), dict)
            and isinstance(projected.get('profiles'), dict)):
        after['profiles'] = {**deepcopy(projected['profiles']), **after['profiles']}
    for key in ('model', 'profiles'):
        explicit = any(name == 'cloud_llm.' + key or name.startswith('cloud_llm.' + key + '.')
                       for name in touched)
        old_value = {key: before[key]} if key in before else {}
        new_value = {key: after[key]} if key in after else {}
        if explicit or mcs_util.canonical(old_value) != mcs_util.canonical(new_value):
            continue
        if key == 'profiles' and selection['provider'] != 'openai':
            info['preserve_openai_profiles'] = True
        if key in projected:
            after[key] = deepcopy(projected[key])
        else:
            after.pop(key, None)


def _init_save_config(cfg, initial, args):
    """Publish the reviewed config under the shared short-lived edit lock."""
    import config_backup
    import config_edit
    import safe_io
    body = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n'
    if len(body.encode('utf-8')) > mcs_util.CONFIG_MAX_BYTES:
        raise ValueError('config_too_large')
    parent = os.path.dirname(CONF_PATH) or '.'
    os.makedirs(parent, exist_ok=True)
    with config_edit._edit_lock(CONF_PATH) as lock_fd:
        current = _init_config_snapshot()[1]
        if not (_init_split_matches(initial, current) if initial[0] == 'split' else current == initial):
            raise config_edit.ConfigConflict('config_changed')
        kind = initial[0]
        if kind == 'split':
            import provider_settings
            import semantic_jev
            common, providers, selected = _init_split_changes(initial[1], cfg)
            args._config_publication_started = True
            provider_settings.commit_locked(current[1]['root'], current[1]['state']['snapshot'], common, providers,
                selected_provider=selected, model_owners=current[1]['owners'], jev_model=semantic_jev.JEV_MODEL,
                lock_fd=lock_fd)
            return
        elif kind == 'invalid_backup':
            args._config_publication_started = True
            backup = config_backup.archive_and_create(CONF_PATH, initial[1], body.encode('utf-8'))
            print(f'設定ファイルの元の内容を {backup} に退避しました')
            return
        if kind == 'missing':
            args._config_publication_started = True
            safe_io.publish_new_bytes(CONF_PATH, body.encode('utf-8'))
        else:
            def write_checked(stream):
                stream.write(body)
                if _init_config_snapshot()[1] != initial:
                    raise config_edit.ConfigConflict('config_changed')
            args._config_publication_started = True
            safe_io.atomic_write(CONF_PATH, write_checked, 0o600,
                                 tmp_prefix='.svc.', makedirs=False)
