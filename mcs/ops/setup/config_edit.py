"""Validated, atomic edits of selected config.json keys from the command line."""
from __future__ import annotations

from copy import deepcopy
import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import safe_io
import mcs_util


class ConfigConflict(ValueError):
    """Another configuration writer owns or changed this edit's base."""


class ConfigSaveUnverified(ValueError):
    """The write may have succeeded but could not be verified."""


def _require_legacy_settings():
    try:
        active = mcs_util._provider_config_active(mcs_util.CONF_PATH)
    except mcs_util.ProviderSettingsUnreadable:
        raise ValueError("provider_settings_unreadable") from None
    if active:
        raise ValueError("provider_settings_write_unavailable")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _snapshot(path=None, *, limit=None):
    path = Path(mcs_util.CONF_PATH if path is None else path)
    limit = mcs_util.CONFIG_MAX_BYTES if limit is None else limit
    raw, info = safe_io.read_regular_nofollow(path, limit,
                                            lambda: ValueError("config_unreadable"), with_stat=True)
    if len(raw) > limit:
        raise ValueError("config_unreadable")
    current = path.lstat()
    if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(info):
        raise ConfigConflict("config_changed")
    return raw, (_identity(info), hashlib.sha256(raw).digest())


def _parse(raw):
    cfg = mcs_util.parse_config(raw)
    if not isinstance(cfg, dict):
        raise ValueError("config_unreadable")
    return cfg


def read_strict() -> dict:
    """Read one bounded regular no-follow descriptor, refusing an unstable pathname."""
    if mcs_util._provider_config_active(mcs_util.CONF_PATH):
        return _split_read()[-1]
    cfg = _parse(_snapshot()[0])
    _require_legacy_settings()
    return cfg


def _split_read():
    import cloud_policy
    import provider_settings
    import semantic_jev

    root = Path(mcs_util.CONF_PATH).absolute().parent
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    state = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    if state["mode"] != "split":
        raise ValueError("provider_settings_unverified")
    selected, errors = cloud_policy.resolve_settings(state["common"], str(root))
    if cloud_policy.blocking_errors(errors):
        raise ValueError("provider_settings_invalid")
    view = provider_settings.execution_view(state["common"], state["providers"],
        selected_provider=selected["provider"], model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    return root, owners, state, selected["provider"], view


def _split_changes(state, cfg, touched, selected, owners):
    import provider_settings
    import semantic_jev

    split_common, split_providers = provider_settings.split_legacy(cfg, owners, jev_model=semantic_jev.JEV_MODEL)
    common, providers = deepcopy(state["common"]), deepcopy(state["providers"])
    for dotted in touched:
        section, key = dotted.split(".", 1)
        if section == "cloud_llm" and key == "model":
            value = cfg.get(section, {}).get(key)
            if value is not None:
                provider = provider_settings._owner(value, owners)
                if provider not in ("anthropic", "openai"):
                    raise ValueError("provider_model_invalid")
                providers[provider]["model"] = split_providers[provider]["model"]
            elif not ({"cloud_llm.provider", "cloud_llm.provider_order"} & touched):
                providers[selected].pop("model", None)
        elif section == "cloud_llm" and key == "provider_budgets":
            for name in ("anthropic", "openai"):
                if "budget" in split_providers[name]:
                    providers[name]["budget"] = split_providers[name]["budget"]
                else:
                    providers[name].pop("budget", None)
            block = common.setdefault(section, {})
            if "provider_budgets" in split_common.get(section, {}):
                block[key] = split_common[section][key]
            else:
                block.pop(key, None)
        elif section == "semantic" and key in provider_settings._JEV_KEYS:
            if key in split_providers["jev"]:
                providers["jev"][key] = split_providers["jev"][key]
            else:
                providers["jev"].pop(key, None)
        else:
            block = common.setdefault(section, {})
            if not isinstance(block, dict):
                raise ValueError("config_section_invalid")
            if key in cfg.get(section, {}):
                block[key] = deepcopy(cfg[section][key])
            else:
                block.pop(key, None)
    return common, providers


@contextmanager
def _edit_lock(path=None):
    # Shared by cooperating editors, never across network or human input.
    # Older writers do not use this lock; the base snapshot below detects
    # their observed changes. It cannot exclude an external rename after
    # the last comparison and before atomic_write's final os.replace.
    path = str(mcs_util.CONF_PATH if path is None else path) + ".edit.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size):
            raise ValueError("config_lock_invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigConflict("config_busy") from None
        if _identity(os.stat(path, follow_symlinks=False)) != _identity(info):
            raise ConfigConflict("config_lock_changed")
        yield fd
    finally:
        os.close(fd)


def apply(changes: dict, defaults: dict | None = None, *, expected: dict | None = None,
          admission=None, on_saved=None) -> tuple[dict | None, list[str]]:
    """Validate the whole edited config and atomically save against its locked base.

    ``defaults`` are set only where the locked config has no value yet.
    Every known schema error blocks publication, including errors outside
    the edited section.
    """
    from mcs_config import validate_config
    if admission is not None and not callable(admission) or on_saved is not None and not callable(on_saved):
        raise ValueError("config_callback_invalid")
    with _edit_lock() as lock_fd:
        split = mcs_util._provider_config_active(mcs_util.CONF_PATH)
        if split:
            root, owners, state, selected, cfg = _split_read()
        else:
            raw, base = _snapshot()
            cfg = _parse(raw)
            _require_legacy_settings()
        if expected is not None and cfg != expected:
            raise ConfigConflict("config_changed")
        touched = set()
        for dotted, value in changes.items():
            section, key = dotted.split(".", 1)
            block = cfg.get(section, {})
            if not isinstance(block, dict):
                raise ValueError("config_section_invalid")
            block = dict(block)
            if value is None:
                block.pop(key, None)
            else:
                block[key] = value
            cfg[section] = block
            touched.add(dotted)
        for dotted, value in (defaults or {}).items():
            section, key = dotted.split(".", 1)
            block = cfg.get(section, {})
            if not isinstance(block, dict):
                raise ValueError("config_section_invalid")
            if block.get(key) is None:
                cfg[section] = {**block, key: value}
                touched.add(dotted)
        if split:
            import cloud_policy
            import provider_settings
            import semantic_jev

            common, providers = _split_changes(state, cfg, touched, selected, owners)
            selection, selection_errors = cloud_policy.resolve_settings(common, str(root))
            if cloud_policy.blocking_errors(selection_errors):
                return None, selection_errors
            selected = selection["provider"]
            requested_model = cfg.get("cloud_llm", {}).get("model")
            if "cloud_llm.model" in touched and requested_model is not None and provider_settings._owner(requested_model, owners) != selected:
                return None, ["config: cloud_llm_model_invalid"]
            cfg = provider_settings.execution_view(common, providers, selected_provider=selected,
                                                    model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
        errors, _ = validate_config(cfg)
        if errors:  # unknown keys remain warnings and are preserved verbatim
            return None, errors
        if split:
            if admission is not None:
                raw_before, stamp_before = _snapshot(root / "config.json")
                file_before = state["snapshot"]["files"]["config"]
                ident = file_before["identity"]
                expected_before = (tuple(ident[index] for index in (0, 1, 2, 5, 6, 7)),
                                   bytes.fromhex(file_before["sha256"]))
                if stamp_before != expected_before:
                    raise ConfigConflict("config_changed")
                admission(str(root / "config.json"), raw_before, stamp_before)
            try:
                result = provider_settings.commit_locked(root, state["snapshot"], common, providers,
                    selected_provider=selected, model_owners=owners, jev_model=semantic_jev.JEV_MODEL,
                    lock_fd=lock_fd)
            except ValueError as error:
                if str(error) == "provider_snapshot_changed":
                    raise ConfigConflict("config_changed") from None
                raise
            if on_saved is not None:
                raw_after, stamp_after = _snapshot(root / "config.json")
                file_after = result["snapshot"]["files"]["config"]
                ident = file_after["identity"]
                expected_after = (tuple(ident[index] for index in (0, 1, 2, 5, 6, 7)),
                                  bytes.fromhex(file_after["sha256"]))
                if stamp_after != expected_after:
                    raise ConfigSaveUnverified("config_save_unverified")
                try:
                    on_saved(str(root / "config.json"), raw_after, stamp_after)
                except (OSError, ValueError, RecursionError) as error:
                    raise ConfigSaveUnverified("config_save_unverified") from error
            return provider_settings.execution_view(result["common"], result["providers"],
                selected_provider=selected, model_owners=owners, jev_model=semantic_jev.JEV_MODEL), []
        body = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if len(body.encode("utf-8")) > mcs_util.CONFIG_MAX_BYTES:
            raise ValueError("config_unreadable")

        intended_identity = None
        def write_checked(stream):
            nonlocal intended_identity
            stream.write(body)
            if on_saved is not None:
                stream.flush()
                staged = os.fstat(stream.fileno())
                intended_identity = (staged.st_dev, staged.st_ino)
            _require_legacy_settings()
            raw_before, stamp_before = _snapshot()
            if stamp_before != base:
                raise ConfigConflict("config_changed")
            if admission is not None:
                admission(str(mcs_util.CONF_PATH), raw_before, stamp_before)
                _require_legacy_settings()
                if _snapshot()[1] != base:
                    raise ConfigConflict("config_changed")
        safe_io.atomic_write(mcs_util.CONF_PATH, write_checked, 0o600,
                             tmp_prefix=".svc.", makedirs=False)
        if on_saved is not None:
            raw_after, stamp_after = _snapshot()
            if (stamp_after[0][:2] != intended_identity or raw_after != body.encode("utf-8")
                    or stamp_after[1] != hashlib.sha256(body.encode("utf-8")).digest()):
                raise ConfigSaveUnverified("config_save_unverified")
            try:
                on_saved(str(mcs_util.CONF_PATH), raw_after, stamp_after)
            except (OSError, ValueError, RecursionError) as error:
                raise ConfigSaveUnverified("config_save_unverified") from error
        return cfg, []


def _on_off(text):
    value = {"on": True, "true": True, "off": False, "false": False}.get(text.strip().lower())
    if value is None:
        raise ValueError
    return value


def settings_command(args) -> int:
    """Show or set extract.prefilter / local_llm.disk_guard_mb / local_llm.admission."""
    try:
        changes = {}
        if args.prefilter is not None:
            changes["extract.prefilter"] = _on_off(args.prefilter)
        if args.admission is not None:
            changes["local_llm.admission"] = _on_off(args.admission)
        if args.disk_guard_mb is not None:
            guard = float(args.disk_guard_mb)
            if not guard >= 0 or guard == float("inf"):
                raise ValueError
            changes["local_llm.disk_guard_mb"] = int(guard) if guard.is_integer() else guard
    except ValueError:
        print("設定: --prefilter・--admission は on/off、--disk-guard-mb は0以上の数で指定してください")
        return 1
    try:
        if changes:
            _, errors = apply(changes)
            if errors:
                print("設定に誤りがあるため保存していません。mcs_setup.py check で確認してください")
                return 1
        else:
            from mcs_config import validate_config
            if validate_config(read_strict())[0]:
                print("設定に誤りがあるため表示していません。mcs_setup.py check で確認してください")
                return 1
    except ConfigConflict:
        print("設定: ほかの設定保存と競合したため変更していません。設定を再確認して再試行してください")
        return 1
    except FileNotFoundError:
        print("設定: 設定ファイルがないため変更していません（先に mcs_setup.py init で設定してください）")
        return 1
    except (OSError, ValueError, RecursionError):
        print("設定: 設定ファイルを読み取れないため変更していません。mcs_setup.py check で確認してください")
        return 1
    import llm_availability
    import local_llm
    import extract_llm
    print(json.dumps({"prefilter": extract_llm._prefilter_enabled(),
                      "disk_guard_mb": llm_availability.disk_floor_mb(),
                      "admission": local_llm.admission_enabled(),
                      "saved": bool(changes)}, ensure_ascii=False))
    return 0


@contextmanager
def _pending_aside(data_dir, operation):
    current = os.path.join(data_dir, ".provider-settings-pending.json")
    kept = os.path.join(data_dir, ".provider-settings-pending.resolved-" + operation + ".json")
    os.link(current, kept)          # never overwrite an earlier resolved record
    os.unlink(current)
    try:
        yield
    except BaseException:
        os.link(kept, current)
        os.unlink(kept)
        raise


def finalize_provider_pending_tx(db, req, filesystem_changes):
    """Human-approved exit from a pending provider-settings record (GA-provider-settings-writer-1).

    Accepts only a commit whose files all reached "after" (provider_settings.
    committed_pending); the record is kept beside the live name, and a checked
    read must succeed before the receipt commits, else it is put back."""
    import provider_settings
    import cloud_policy
    import semantic_jev
    if filesystem_changes is None:
        raise RuntimeError("provider_pending_transaction_scope_required")
    main = db.execute("PRAGMA database_list").fetchone()["file"]
    if not main:
        return "provider_pending_no_data_dir", {}
    data_dir = os.path.dirname(os.path.abspath(main))
    root = os.path.dirname(data_dir)
    try:
        lock_fd = filesystem_changes.enter_context(_edit_lock(os.path.join(root, "config.json")))
        operation = provider_settings.committed_pending(root, lock_fd)
    except ConfigConflict:
        return "config_busy", {}
    except (OSError, ValueError) as error:
        return (str(error) if str(error).startswith("provider_") else "provider_pending_unverified"), {}
    if operation != req["operation"]:
        return "provider_pending_changed", {}
    filesystem_changes.enter_context(_pending_aside(data_dir, operation))
    # A failing read raises out of the receipt transaction and puts the record back.
    owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
    state = provider_settings.read(root, model_owners=owners, jev_model=semantic_jev.JEV_MODEL)
    if state.get("mode") != "split":
        raise ValueError("provider_pending_readback_failed")
    return None, {"provider_pending_finalized": {"operation": operation}}


def provider_pending_command(args) -> int:
    import uuid
    import mcs_requests
    if args.confirm_human is not True or not mcs_util.valid_text(args.reason, 2000):
        print("--confirm-human と --reason による人の確認が必要です")
        return 1
    req = {"version": 1, "cmd": "ops.provider_pending_finalize", "command_id": args.command_id or str(uuid.uuid4()),
           "actor": args.actor, "human_confirmed": True, "project_id": None,
           "reason": args.reason, "operation": args.operation}
    error = mcs_requests.validate(req)
    if error:
        print("提供元設定の保留: 指定を確認してください（" + error + "）")
        return 1
    try:
        receipt = mcs_requests.apply_local_operation(req)
    except (OSError, ValueError) as exc:
        print("提供元設定の保留: 解除できませんでした（" + mcs_util.safe_error_code(exc) + "）。記録を保持しています")
        return 1
    if receipt is None:
        print("提供元設定の保留: ほかの保存処理が終わってから再試行してください")
        return 3
    print(json.dumps({k: receipt.get(k) for k in ("command_id", "outcome", "error", "provider_pending_finalized")},
                     ensure_ascii=False))
    return 0 if receipt["outcome"] == "applied" else 1


def add_parsers(sub) -> None:
    p = sub.add_parser("settings", help="抽出の事前判定・ディスク下限・LLM受付制御を表示・設定します")
    p.add_argument("--prefilter", help="on/off: 挨拶だけの投稿などをLLMに送らない事前判定")
    p.add_argument("--disk-guard-mb", dest="disk_guard_mb", help="空きがこのMB未満ならローカルLLMを止める（0で無効）")
    p.add_argument("--admission", help="on/off: ローカルLLMの共有受付制御（既定off）")
    p.set_defaults(fn=settings_command)
    p = sub.add_parser("provider-pending-finalize",
                       help="保存の最後で止まった提供元設定の保留記録を、全ファイルが保存後の状態のときだけ人の確認で解除します")
    p.add_argument("--operation", required=True, help="data/.provider-settings-pending.json の operation")
    p.add_argument("--confirm-human", action="store_true")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="local-human")
    p.add_argument("--command-id", help="同じ操作を再試行する場合の承認記録UUID")
    p.set_defaults(fn=provider_pending_command)
