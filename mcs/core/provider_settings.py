"""Read and compose provider-specific configuration without implicit migration."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Literal, TypedDict
import re
import stat
import sys
import uuid

from mcs_util import CONFIG_MAX_BYTES, _provider_artifact_present, _weak_warned, canonical, strict_json, until_epoch
from safe_io import create_exclusive, fdopen_owned


PROVIDERS = ("anthropic", "openai", "jev")
_JEV_KEYS = frozenset({"model", "daily_request_budget", "until", "attempt_timeout_seconds",
                       "max_attempts_per_try", "max_questions_per_request"})
_CLOUD_KEYS = frozenset({"model", "budget", "profiles"})
_FILES = ("config", *PROVIDERS)
ALIAS_CONFIG_MAX_BYTES = 8 * 1024 * 1024
_INDEPENDENT_ROOTS = frozenset({"drug_map", "sdc", "install_args"})


def _config_limit(value):
    if type(value) is not int or value not in (CONFIG_MAX_BYTES, ALIAS_CONFIG_MAX_BYTES):
        raise ValueError("provider_config_limit_invalid")
    return value


def common_policy_digest(common):
    """Bind all common roots except those owned by independent writers."""
    if not isinstance(common, dict):
        raise ValueError("provider_config_invalid")
    return hashlib.sha256(canonical({key: value for key, value in common.items()
                                    if key not in _INDEPENDENT_ROOTS})).hexdigest()


def _budget(value):
    fields = {"daily_cost_limit_usd", "monthly_cost_limit_usd"}
    if (not isinstance(value, dict) or set(value) != fields
            or any(type(v) not in (int, float) or not 0 <= v <= 1e15 or not math.isfinite(v)
                   for v in value.values())):
        raise ValueError("provider_budgets_invalid")


def _jev_values(value, jev_model):
    if "model" in value and (not isinstance(jev_model, str) or value["model"] != jev_model):
        raise ValueError("provider_model_invalid")
    for key, low, high in (("daily_request_budget", 0, 100000), ("max_attempts_per_try", 1, 3),
                           ("max_questions_per_request", 1, 48)):
        if key in value and (type(value[key]) is not int or not low <= value[key] <= high):
            raise ValueError("provider_config_invalid")
    if "attempt_timeout_seconds" in value:
        number = value["attempt_timeout_seconds"]
        if type(number) not in (int, float) or not 1 <= number <= 60 or not math.isfinite(number):
            raise ValueError("provider_config_invalid")
    if value.get("until") is not None:
        try:
            until_epoch(value["until"])
        except (TypeError, ValueError, OverflowError):
            raise ValueError("provider_config_invalid") from None


def _profiles(value):
    import openai_client
    if (not isinstance(value, dict) or value.keys() - openai_client.PROFILE_PURPOSES
            or any(not isinstance(role, str) or not isinstance(profile, str)
                   or profile not in openai_client.PROFILE_DEFINITIONS for role, profile in value.items())):
        raise ValueError("provider_profiles_invalid")


def _owner(model, model_owners):
    if not isinstance(model, str) or not isinstance(model_owners, dict):
        raise ValueError("provider_model_invalid")
    name = model_owners.get(model)
    if name is None:
        import openai_client
        if model == openai_client.SOL_MODEL and model_owners.get(openai_client.MODEL) == "openai":
            name = "openai"
    if name not in ("anthropic", "openai"):
        raise ValueError("provider_model_invalid")
    return name


def split_legacy(raw, model_owners, *, jev_model=None):
    """Separate explicit legacy values without adding normalized defaults."""
    if not isinstance(raw, dict):
        raise ValueError("provider_config_invalid")
    common = deepcopy(raw)
    providers = {name: {} for name in PROVIDERS}
    cloud = common.get("cloud_llm")
    if isinstance(cloud, dict):
        if "model" in cloud:
            model = cloud.pop("model")
            providers[_owner(model, model_owners)]["model"] = model
        if "profiles" in cloud:
            profiles = cloud.pop("profiles")
            _profiles(profiles)
            providers["openai"]["profiles"] = deepcopy(profiles)
        if "provider_budgets" in cloud:
            budgets = cloud["provider_budgets"]
            if not isinstance(budgets, dict) or budgets.keys() - {"anthropic", "openai"}:
                raise ValueError("provider_budgets_invalid")
            if budgets:
                for name, budget in budgets.items():
                    _budget(budget)
                    providers[name]["budget"] = deepcopy(budget)
                del cloud["provider_budgets"]
    semantic = common.get("semantic")
    if isinstance(semantic, dict):
        for key in _JEV_KEYS:
            if key in semantic:
                providers["jev"][key] = semantic.pop(key)
    _jev_values(providers["jev"], jev_model)
    return common, providers


def compose(common, providers, *, selected_provider, model_owners, jev_model=None):
    """Rebuild the legacy-shaped view using an already selected provider."""
    if (not isinstance(common, dict) or not isinstance(providers, dict)
            or set(providers) != set(PROVIDERS) or selected_provider not in ("anthropic", "openai")):
        raise ValueError("provider_config_invalid")
    result = deepcopy(common)
    for name in PROVIDERS:
        value = providers[name]
        allowed = _JEV_KEYS if name == "jev" else _CLOUD_KEYS
        if not isinstance(value, dict) or value.keys() - allowed:
            raise ValueError("provider_config_invalid")
        if name != "jev" and "model" in value and _owner(value["model"], model_owners) != name:
            raise ValueError("provider_model_invalid")
        if name != "jev" and "budget" in value:
            _budget(value["budget"])
        if "profiles" in value:
            if name != "openai":
                raise ValueError("provider_profiles_invalid")
            _profiles(value["profiles"])
        if name == "jev":
            _jev_values(value, jev_model)
    cloud_values = any(providers[name] for name in ("anthropic", "openai"))
    if cloud_values:
        cloud = result.setdefault("cloud_llm", {})
        if (not isinstance(cloud, dict) or "model" in cloud or "profiles" in cloud
                or "provider_budgets" in cloud and cloud["provider_budgets"] != {}):
            raise ValueError("provider_config_ambiguous")
        model = providers[selected_provider].get("model")
        if model is not None:
            cloud["model"] = model
        if selected_provider == "openai" and "profiles" in providers["openai"]:
            cloud["profiles"] = deepcopy(providers["openai"]["profiles"])
        budgets = {name: deepcopy(providers[name]["budget"]) for name in ("anthropic", "openai")
                   if "budget" in providers[name]}
        if budgets:
            cloud["provider_budgets"] = budgets
    semantic = result.get("semantic")
    if isinstance(semantic, dict) and semantic.keys() & _JEV_KEYS:
        raise ValueError("provider_config_ambiguous")
    if providers["jev"]:
        semantic = result.setdefault("semantic", {})
        if not isinstance(semantic, dict):
            raise ValueError("provider_config_ambiguous")
        semantic.update(deepcopy(providers["jev"]))
    return result


def execution_view(common, providers, *, selected_provider, model_owners, jev_model=None):
    """Build the validation and runtime view for the selected provider."""
    result = compose(common, providers, selected_provider=selected_provider,
                     model_owners=model_owners, jev_model=jev_model)
    cloud = result.get("cloud_llm")
    if not isinstance(cloud, dict) or cloud.get("provider", "auto") != "auto" or "model" not in cloud:
        return result
    order = cloud.get("provider_order", ["anthropic", "openai"])
    if (not isinstance(order, list) or not order
            or any(not isinstance(name, str) or name not in ("anthropic", "openai") for name in order)
            or len(set(order)) != len(order)):
        return result
    if selected_provider in order and selected_provider != order[0]:
        if _owner(cloud["model"], model_owners) != selected_provider:
            raise ValueError("provider_model_invalid")
        # Preserve an explicit model belonging to the selected provider.
    return result


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _RootHandle:
    def __init__(self, path):
        self.path = Path(os.path.abspath(path))
        self.parent_fd = self.fd = None
        try:
            self.parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            parent = os.fstat(self.parent_fd)
            self.parent_identity = _directory_anchor(parent)
            named = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
            self.link = None
            if stat.S_ISLNK(named.st_mode):
                if (named.st_uid != os.getuid() or parent.st_uid != os.getuid()
                        or parent.st_mode & 0o022):
                    raise ValueError("provider_directory_invalid")
                self.link = {"identity": _identity(named),
                             "text": os.readlink(self.path.name, dir_fd=self.parent_fd)}
            elif not stat.S_ISDIR(named.st_mode):
                raise ValueError("provider_directory_invalid")
            flags = os.O_RDONLY | os.O_DIRECTORY
            if self.link is None:
                flags |= os.O_NOFOLLOW
            try:
                self.fd = os.open(self.path.name, flags, dir_fd=self.parent_fd)
            except FileNotFoundError:
                raise ValueError("provider_directory_invalid") from None
            info = os.fstat(self.fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("provider_directory_invalid")
            self.identity = _identity(info)
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        parent = os.fstat(self.parent_fd)
        named_parent = os.stat(self.path.parent)
        named = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
        target = os.stat(self.path.name, dir_fd=self.parent_fd)
        held = os.fstat(self.fd)
        if (_directory_anchor(parent) != self.parent_identity
                or _directory_anchor(named_parent) != self.parent_identity
                or _directory_anchor(held) != self.identity[:4]
                or _directory_anchor(target) != self.identity[:4]):
            raise ValueError("provider_snapshot_changed")
        if self.link is None:
            if _directory_anchor(named) != self.identity[:4]:
                raise ValueError("provider_snapshot_changed")
        elif (_identity(named) != self.link["identity"]
              or os.readlink(self.path.name, dir_fd=self.parent_fd) != self.link["text"]
              or _identity(os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False))
              != self.link["identity"]):
            raise ValueError("provider_snapshot_changed")
        if (_directory_anchor(os.stat(self.path.name, dir_fd=self.parent_fd)) != self.identity[:4]
                or _directory_anchor(os.stat(self.path.parent)) != self.parent_identity):
            raise ValueError("provider_snapshot_changed")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None


def _directory(parent, name, *, private=False):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    info = os.fstat(fd)
    if (info.st_uid != os.getuid() or info.st_mode & 0o022
            or private and stat.S_IMODE(info.st_mode) != 0o700):
        os.close(fd)
        raise ValueError("provider_directory_invalid")
    return fd


def _read_file(parent, name, *, private=True, limit=CONFIG_MAX_BYTES):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None, None
    with fdopen_owned(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode)
                or private and (before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1)
                or before.st_size > limit):
            raise ValueError("provider_file_invalid")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        path_info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if len(raw) > limit or _identity(before) != _identity(after) or _identity(after) != _identity(path_info):
            raise ValueError("provider_file_changed")
        return raw, {"identity": _identity(after), "sha256": hashlib.sha256(raw).hexdigest()}


def _document(raw):
    if raw is None:
        raise ValueError("provider_file_missing")
    try:
        value = strict_json(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("provider_json_invalid") from None
    if not isinstance(value, dict):
        raise ValueError("provider_json_invalid")
    return value


def _must_be_absent(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise ValueError("provider_settings_changed")


def is_canonical_config(path, root):
    """Match only the fixed absolute config path; actual reads still verify links."""
    try:
        path, root = os.fspath(path), os.fspath(root)
        return (isinstance(path, str) and isinstance(root, str) and os.path.isabs(path)
                and os.path.isabs(root) and os.path.normpath(path) == os.path.join(os.path.normpath(root), "config.json"))
    except TypeError:
        return False


def _config_presence(root_fd):
    try:
        info = os.stat("config.json", dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("provider_file_invalid")
    return _identity(info)


def probe(root):
    """Observe fixed settings state without creating files or enabling a mode."""
    root = Path(root)
    if not root.is_absolute():
        raise ValueError("provider_root_invalid")
    try:
        root_handle = _RootHandle(root)
    except FileNotFoundError:
        return {"state": "root_missing", "config_present": False}
    root_fd = root_handle.fd
    data_fd = settings_fd = None
    try:
        root_info = os.fstat(root_fd)
        if root_info.st_uid != os.getuid() or root_info.st_mode & 0o022:
            raise ValueError("provider_directory_invalid")
        root_identity = _identity(root_info)
        config = _config_presence(root_fd)
        try:
            data_fd = _directory(root_fd, "data")
        except FileNotFoundError:
            pass
        state = "legacy_unverified" if config is not None else "uninitialized_or_legacy_unverified"
        if data_fd is not None:
            data_identity = _identity(os.fstat(data_fd))
            try:
                settings_fd = _directory(data_fd, "provider_settings", private=True)
            except FileNotFoundError:
                pass
            settings_identity = _identity(os.fstat(settings_fd)) if settings_fd is not None else None
            _, pending = _read_file(data_fd, ".provider-settings-pending.json")
            _, authority = _read_file(data_fd, ".provider-settings-authority.json")
            if pending is not None:
                state = "pending"
            elif authority is not None or settings_fd is not None or _provider_artifact_present(data_fd):
                state = "requires_checked_read"
            if (_read_file(data_fd, ".provider-settings-pending.json")[1] != pending
                    or _read_file(data_fd, ".provider-settings-authority.json")[1] != authority
                    or _identity(os.fstat(data_fd)) != data_identity
                    or _identity(os.stat("data", dir_fd=root_fd, follow_symlinks=False)) != data_identity):
                raise ValueError("provider_settings_changed")
            if settings_fd is None:
                _must_be_absent(data_fd, "provider_settings")
            elif (_identity(os.fstat(settings_fd)) != settings_identity
                    or _identity(os.stat("provider_settings", dir_fd=data_fd, follow_symlinks=False)) != settings_identity):
                raise ValueError("provider_settings_changed")
        else:
            _must_be_absent(root_fd, "data")
        root_handle.check()
        if _config_presence(root_fd) != config or _identity(os.fstat(root_fd)) != root_identity:
            raise ValueError("provider_settings_changed")
        return {"state": state, "config_present": config is not None}
    finally:
        if settings_fd is not None:
            os.close(settings_fd)
        if data_fd is not None:
            os.close(data_fd)
        root_handle.close()


class VerifiedCommonUnavailable(TypedDict):
    """Common snapshot retained when only provider files are unavailable."""

    mode: Literal["provider_unavailable"]
    common: dict
    providers: None
    snapshot: dict


def read(root, *, model_owners=None, jev_model=None, config_limit=CONFIG_MAX_BYTES):
    """Read a stable private four-file snapshot; never create or migrate files."""
    return _read(root, model_owners=model_owners, jev_model=jev_model, config_limit=config_limit,
                 allow_unavailable=False)


def read_with_verified_common(root, *, model_owners=None, jev_model=None,
                              config_limit=CONFIG_MAX_BYTES) -> dict | VerifiedCommonUnavailable:
    """Return checked common on isolated provider failure, never on a snapshot conflict.

    Common is checked against the authority policy and snapshot, not validated
    against the complete application schema; callers must still validate its use.
    """
    return _read(root, model_owners=model_owners, jev_model=jev_model, config_limit=config_limit,
                 allow_unavailable=True)


def _read(root, *, model_owners, jev_model, config_limit, allow_unavailable):
    config_limit = _config_limit(config_limit)
    root = Path(root)
    root_handle = _RootHandle(root)
    root_fd = root_handle.fd
    data_fd = settings_fd = None
    try:
        root_info = os.fstat(root_fd)
        if root_info.st_uid != os.getuid() or root_info.st_mode & 0o022:
            raise ValueError("provider_directory_invalid")
        root_identity = _identity(root_info)
        try:
            data_fd = _directory(root_fd, "data")
        except FileNotFoundError:
            data_fd = None
        pending_snapshot = None
        if data_fd is not None:
            _, pending_snapshot = _read_file(data_fd, ".provider-settings-pending.json")
            if pending_snapshot is not None:
                raise ValueError("provider_settings_pending")
        try:
            common_raw, common_snapshot = _read_file(root_fd, "config.json", private=False, limit=config_limit)
            common = _document(common_raw)
        except (OSError, ValueError):
            # A writer may have moved the common path after the first check.
            if data_fd is not None and _read_file(data_fd, ".provider-settings-pending.json")[1] is not None:
                raise ValueError("provider_settings_pending") from None
            raise
        snapshots = {"config": common_snapshot, **{name: None for name in PROVIDERS}}
        providers = {name: {} for name in PROVIDERS}
        if data_fd is not None:
            try:
                settings_fd = _directory(data_fd, "provider_settings", private=True)
            except FileNotFoundError:
                settings_fd = None
        data_identity = _identity(os.fstat(data_fd)) if data_fd is not None else None
        settings_identity = _identity(os.fstat(settings_fd)) if settings_fd is not None else None
        mode = "legacy_unverified"
        authority_snapshot = None
        configuration_state = None
        if data_fd is not None:
            if _read_file(data_fd, ".provider-settings-pending.json")[1] is not None:
                raise ValueError("provider_settings_pending")
            authority_raw, authority_snapshot = _read_file(data_fd, ".provider-settings-authority.json")
            values = {}
            invalid_files = {}
            for name in PROVIDERS:
                observed = None
                if allow_unavailable and settings_fd is not None:
                    try:
                        observed = _identity(os.stat(name + ".json", dir_fd=settings_fd, follow_symlinks=False))
                    except FileNotFoundError:
                        pass
                try:
                    values[name], snapshots[name] = (_read_file(settings_fd, name + ".json")
                                                     if settings_fd is not None else (None, None))
                except ValueError as exc:
                    if not allow_unavailable or str(exc) != "provider_file_invalid" or settings_fd is None:
                        raise
                    # Record a stable path identity without consuming an invalid file.
                    info = os.stat(name + ".json", dir_fd=settings_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or _identity(info) != observed:
                        raise ValueError("provider_settings_changed") from None
                    invalid_files[name] = observed
                    values[name], snapshots[name] = None, None
                else:
                    if allow_unavailable and settings_fd is not None and (
                            snapshots[name]["identity"] if snapshots[name] is not None else None) != observed:
                        raise ValueError("provider_settings_changed")
            if authority_raw is None:
                if settings_fd is not None or invalid_files or any(value is not None for value in values.values()) \
                        or _provider_artifact_present(data_fd):
                    raise ValueError("provider_authority_missing")
            else:
                authority = _document(authority_raw)
                hashes = authority.get("files")
                policy_hash = authority.get("common_policy_sha256")
                required_authority = {"version", "state", "files", "common_policy_sha256"}
                if (set(authority) not in (required_authority, required_authority | {"configuration_state"})
                        or "configuration_state" in authority and authority["configuration_state"] != "bootstrap"
                        or type(authority["version"]) is not int or authority["version"] != 2
                        or authority["state"] != "committed" or not isinstance(policy_hash, str)
                        or re.fullmatch(r"[0-9a-f]{64}", policy_hash) is None
                        or policy_hash != common_policy_digest(common)
                        or not isinstance(hashes, dict) or set(hashes) != set(PROVIDERS)
                        or any(not isinstance(hashes[key], str)
                               or re.fullmatch(r"[0-9a-f]{64}", hashes[key]) is None for key in PROVIDERS)):
                    raise ValueError("provider_authority_invalid")
                configuration_state = authority.get("configuration_state")
                provider_error = bool(invalid_files) or any(
                    snapshots[key] is None or hashes[key] != snapshots[key]["sha256"] for key in PROVIDERS)
                if provider_error and not allow_unavailable:
                    raise ValueError("provider_authority_invalid")
                if not provider_error:
                    try:
                        providers = {name: _document(values[name]) for name in PROVIDERS}
                        if any(providers[name].keys() - (_JEV_KEYS if name == "jev" else _CLOUD_KEYS)
                               for name in PROVIDERS):
                            raise ValueError("provider_config_invalid")
                        for name in ("anthropic", "openai"):
                            if "model" in providers[name] and model_owners is None and allow_unavailable:
                                raise ValueError("provider_validation_context_missing")
                            if "model" in providers[name] and _owner(providers[name]["model"], model_owners) != name:
                                raise ValueError("provider_model_invalid")
                            if "budget" in providers[name]:
                                _budget(providers[name]["budget"])
                            if "profiles" in providers[name]:
                                if name != "openai":
                                    raise ValueError("provider_profiles_invalid")
                                _profiles(providers[name]["profiles"])
                        if "model" in providers["jev"] and jev_model is None and allow_unavailable:
                            raise ValueError("provider_validation_context_missing")
                        _jev_values(providers["jev"], jev_model)
                    except ValueError as exc:
                        if not allow_unavailable or str(exc) == "provider_validation_context_missing":
                            raise
                        provider_error = True
                cloud, semantic = common.get("cloud_llm"), common.get("semantic")
                if provider_error:
                    # Without trusted provider documents, reject every possible overlap.
                    if (settings_fd is None or not isinstance(cloud, (dict, type(None)))
                            or not isinstance(semantic, (dict, type(None)))
                            or isinstance(cloud, dict) and ("model" in cloud or "profiles" in cloud
                                or "provider_budgets" in cloud and cloud["provider_budgets"] != {})
                            or isinstance(semantic, dict) and bool(semantic.keys() & _JEV_KEYS)):
                        raise ValueError("provider_config_ambiguous")
                    providers = None
                    mode = "provider_unavailable"
                else:
                    if (isinstance(cloud, dict) and ("model" in cloud or "profiles" in cloud
                            or "provider_budgets" in cloud and cloud["provider_budgets"] != {})
                            or isinstance(semantic, dict) and bool(semantic.keys() & _JEV_KEYS)):
                        raise ValueError("provider_config_ambiguous")
                    mode = "split"
            if (_read_file(data_fd, ".provider-settings-authority.json")[1] != authority_snapshot
                    or _read_file(data_fd, ".provider-settings-pending.json")[1] is not None):
                raise ValueError("provider_settings_changed")
            if settings_fd is not None:
                for name in PROVIDERS:
                    if name in invalid_files:
                        if _identity(os.stat(name + ".json", dir_fd=settings_fd, follow_symlinks=False)) != invalid_files[name]:
                            raise ValueError("provider_settings_changed")
                    elif _read_file(settings_fd, name + ".json")[1] != snapshots[name]:
                        raise ValueError("provider_settings_changed")
        root_handle.check()
        if (_read_file(root_fd, "config.json", private=False, limit=config_limit)[1] != common_snapshot
                or _identity(os.fstat(root_fd)) != root_identity):
            raise ValueError("provider_settings_changed")
        if data_fd is None:
            _must_be_absent(root_fd, "data")
        elif settings_fd is None:
            _must_be_absent(data_fd, "provider_settings")
        if data_fd is not None and (data_identity != _identity(os.fstat(data_fd))
                or data_identity != _identity(os.stat("data", dir_fd=root_fd, follow_symlinks=False))):
            raise ValueError("provider_settings_changed")
        if settings_fd is not None and (settings_identity != _identity(os.fstat(settings_fd))
                or settings_identity != _identity(os.stat("provider_settings", dir_fd=data_fd, follow_symlinks=False))):
            raise ValueError("provider_settings_changed")
        identity = common_snapshot["identity"]
        path = str(root / "config.json")
        if (identity[2] & 0o022 or identity[3] != os.getuid()) and path not in _weak_warned:
            _weak_warned.add(path)
            print("mcs: local_file_permissions_weak (config.json)", file=sys.stderr)
        root_handle.check()
        return {"mode": mode, "common": common, "providers": providers,
                **({"configuration_state": configuration_state} if configuration_state is not None else {}),
                "snapshot": {"files": snapshots, "authority": authority_snapshot,
                             "pending": pending_snapshot, "root": root_identity,
                             "data": data_identity, "provider_settings": settings_identity,
                             **({"root_link": root_handle.link} if root_handle.link is not None else {})}}
    finally:
        if settings_fd is not None:
            os.close(settings_fd)
        if data_fd is not None:
            os.close(data_fd)
        root_handle.close()


def _json_bytes(value, *, limit=CONFIG_MAX_BYTES):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > limit:
        raise ValueError("provider_file_too_large")
    return raw


def _write_new(parent, name, raw, *, limit=CONFIG_MAX_BYTES):
    fd = create_exclusive(name, 0o600, dir_fd=parent)
    with fdopen_owned(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(parent)
    actual, snapshot = _read_file(parent, name, limit=limit)
    if actual != raw:
        raise ValueError("provider_readback_failed")
    return snapshot


def _replace(parent, name, raw, stage_fd, expected, moved_name, moved, private, limit):
    temporary = "new-" + uuid.uuid4().hex
    _write_new(stage_fd, temporary, raw, limit=limit)
    if expected is not None:
        os.rename(name, moved_name, src_dir_fd=parent, dst_dir_fd=stage_fd)
        os.fsync(parent)
        os.fsync(stage_fd)
        _, captured = _read_file(stage_fd, moved_name, private=private, limit=limit)
        # Rename can update ctime. The inode, other attributes and bytes must
        # still match the snapshot, so an intervening writer is retained and held.
        if (captured is None or captured["identity"][:7] != expected["identity"][:7]
                or captured["sha256"] != expected["sha256"]):
            raise ValueError("provider_snapshot_changed")
        moved.append((moved_name, captured, private))
    # A late old writer may create the destination after the rename. Never
    # overwrite it; the pending receipt keeps all new readers held.
    os.link(temporary, name, src_dir_fd=stage_fd, dst_dir_fd=parent, follow_symlinks=False)
    os.unlink(temporary, dir_fd=stage_fd)
    os.fsync(stage_fd)
    os.fsync(parent)
    actual, snapshot = _read_file(parent, name, limit=limit)
    if actual != raw:
        raise ValueError("provider_readback_failed")
    return snapshot


def _directory_anchor(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid)


def _verify_lock(root, lock_fd, *, root_fd=None):
    if type(lock_fd) is not int or lock_fd < 0:
        raise ValueError("provider_lock_invalid")
    info = os.fstat(lock_fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size):
        raise ValueError("provider_lock_invalid")
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    name = "config.json.edit.lock" if root_fd is not None else str(root / "config.json") + ".edit.lock"
    if _identity(info) != _identity(os.stat(name, dir_fd=root_fd, follow_symlinks=False)):
        raise ValueError("provider_lock_invalid")


@contextmanager
def _edit_lock_at(root, expected):
    from config_edit import ConfigConflict

    handle = _RootHandle(root)
    fd = None
    try:
        if (handle.identity[:4] != expected.get("root", ())[:4]
                or handle.link != expected.get("root_link")):
            raise ValueError("provider_snapshot_changed")
        handle.check()
        fd = os.open("config.json.edit.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=handle.fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size):
            raise ValueError("config_lock_invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigConflict("config_busy") from None
        handle.check()
        if _identity(os.stat("config.json.edit.lock", dir_fd=handle.fd, follow_symlinks=False)) != _identity(info):
            raise ConfigConflict("config_lock_changed")
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        handle.close()


def commit(root, expected_snapshot, common, providers, *, selected_provider, model_owners, jev_model=None,
           config_limit=CONFIG_MAX_BYTES):
    """Commit after taking the normal config editor lock."""
    return _commit(root, expected_snapshot, common, providers, selected_provider=selected_provider,
                   model_owners=model_owners, jev_model=jev_model, lock_fd=None, config_limit=config_limit,
                   validate=_validate_standard)


def commit_locked(root, expected_snapshot, common, providers, *, selected_provider, model_owners,
                  lock_fd, jev_model=None, config_limit=CONFIG_MAX_BYTES):
    """Commit using a borrowed editor lock descriptor without closing it."""
    return _commit(root, expected_snapshot, common, providers, selected_provider=selected_provider,
                   model_owners=model_owners, jev_model=jev_model, lock_fd=lock_fd, config_limit=config_limit,
                   validate=_validate_standard)


def _validate_standard(config):
    from mcs_config import validate_config
    return validate_config(config)[0]


def _validate_bootstrap(config):
    from mcs_config import CONFIG_RULES, validate_config
    permitted = {f"必須の設定項目がありません: {key}" for key, (required, _) in CONFIG_RULES.items()
                 if required and key not in config}
    return [error for error in validate_config(config)[0] if error not in permitted]


def _bootstrap_common_valid(common):
    from mcs_config import CONFIG_RULES

    allowed = {"runtime_mode", "recovery_python", "install_args"}
    known = set(CONFIG_RULES) | _INDEPENDENT_ROOTS
    return (isinstance(common, dict) and all(isinstance(key, str) for key in common)
            and not common.keys() & (known - allowed))


def bootstrap_locked(root, expected_snapshot, common, *, model_owners, lock_fd, jev_model=None):
    """Publish empty provider files for a separately verified fresh installation."""
    if not _bootstrap_common_valid(common):
        raise ValueError("provider_bootstrap_invalid")
    common = deepcopy(common)
    root = Path(root)
    _verify_lock(root, lock_fd)
    before = read(root, model_owners=model_owners, jev_model=jev_model)
    if (before["mode"] != "legacy_unverified" or canonical(before["common"]) != canonical(common)
            or before["snapshot"] != expected_snapshot):
        raise ValueError("provider_bootstrap_unverified")
    providers = {name: {} for name in PROVIDERS}
    return _commit(root, expected_snapshot, common, providers, selected_provider="anthropic",
                   model_owners=model_owners, jev_model=jev_model, lock_fd=lock_fd,
                   config_limit=CONFIG_MAX_BYTES, validate=_validate_bootstrap, configuration_state="bootstrap")


def bootstrap_runtime_locked(root, expected_snapshot, changes, *, model_owners, lock_fd, jev_model=None):
    """Edit only runtime choices in an authority-verified unconfigured bootstrap."""
    if not isinstance(changes, dict) or changes.keys() - {"runtime_mode", "recovery_python"}:
        raise ValueError("provider_bootstrap_invalid")
    root = Path(root)
    _verify_lock(root, lock_fd)
    before = read(root, model_owners=model_owners, jev_model=jev_model)
    if (before["mode"] != "split" or before.get("configuration_state") != "bootstrap"
            or not _bootstrap_common_valid(before["common"])
            or before["providers"] != {name: {} for name in PROVIDERS}):
        raise ValueError("provider_bootstrap_unverified")
    common = deepcopy(before["common"])
    common.update(deepcopy(changes))
    return _commit(root, expected_snapshot, common, before["providers"], selected_provider="anthropic",
                   model_owners=model_owners, jev_model=jev_model, lock_fd=lock_fd,
                   config_limit=CONFIG_MAX_BYTES, validate=_validate_bootstrap, configuration_state="bootstrap",
                   require_completed_bootstrap=True)


def _commit(root, expected_snapshot, common, providers, *, selected_provider, model_owners, jev_model, lock_fd,
            config_limit, validate, configuration_state=None, require_completed_bootstrap=False):
    """Commit four settings with durable pending evidence and independent backups.

    Initial migration requires the updater's verified quiescence before new
    services start. Later edits require the verified split snapshot and editor
    lock; pending and readback fence the cooperating readers during publication.
    """
    if configuration_state not in (None, "bootstrap"):
        raise ValueError("provider_configuration_state_invalid")
    config_limit = _config_limit(config_limit)
    effective = execution_view(common, providers, selected_provider=selected_provider,
                               model_owners=model_owners, jev_model=jev_model)
    errors = validate(effective)
    if errors:
        raise ValueError("provider_config_invalid")
    new_bytes = {"config": _json_bytes(common, limit=config_limit),
                 **{name: _json_bytes(providers[name]) for name in PROVIDERS}}
    root = Path(root)
    if lock_fd is not None:
        _verify_lock(root, lock_fd)
    before_lock = read(root, model_owners=model_owners, jev_model=jev_model, config_limit=config_limit)["snapshot"]
    if (not isinstance(expected_snapshot, dict)
            or (before_lock != expected_snapshot if lock_fd is None else
                any(before_lock.get(key) != expected_snapshot.get(key) for key in ("files", "authority", "pending", "data", "provider_settings", "root_link"))
                or before_lock["root"][:4] != expected_snapshot.get("root", ())[:4])):
        raise ValueError("provider_snapshot_changed")
    with _edit_lock_at(root, expected_snapshot) if lock_fd is None else nullcontext(lock_fd) as held_fd:
        current = read(root, model_owners=model_owners, jev_model=jev_model, config_limit=config_limit)
        actual = current["snapshot"]
        # The lock file can alter directory link counts on APFS; the stable
        # directory identity and the complete protected snapshots must match.
        if (any(actual.get(key) != expected_snapshot.get(key) for key in ("files", "authority", "pending", "data", "provider_settings", "root_link"))
                or actual["root"][:4] != expected_snapshot["root"][:4]):
            raise ValueError("provider_snapshot_changed")
        root_handle = _RootHandle(root)
        root_fd = root_handle.fd
        data_fd = settings_fd = stage_fd = None
        try:
            root_handle.check()
            if (root_handle.identity[:4] != actual["root"][:4]
                    or root_handle.link != actual.get("root_link")):
                raise ValueError("provider_snapshot_changed")
            _verify_lock(root, held_fd, root_fd=root_fd)
            data_fd = _directory(root_fd, "data")
            if actual["data"] is None or _identity(os.fstat(data_fd)) != actual["data"]:
                raise ValueError("provider_snapshot_changed")
            root_anchor, data_anchor = _directory_anchor(os.fstat(root_fd)), _directory_anchor(os.fstat(data_fd))
            old = {}
            old["config"], config_snapshot = _read_file(root_fd, "config.json", private=False, limit=config_limit)
            if config_snapshot != actual["files"]["config"]:
                raise ValueError("provider_snapshot_changed")
            if actual["provider_settings"] is not None:
                settings_fd = _directory(data_fd, "provider_settings", private=True)
                if _identity(os.fstat(settings_fd)) != actual["provider_settings"]:
                    raise ValueError("provider_snapshot_changed")
            for name in PROVIDERS:
                old[name], found = (_read_file(settings_fd, name + ".json") if settings_fd is not None else (None, None))
                if found != actual["files"][name]:
                    raise ValueError("provider_snapshot_changed")
            old_authority, authority_snapshot = _read_file(data_fd, ".provider-settings-authority.json")
            if authority_snapshot != actual["authority"]:
                raise ValueError("provider_snapshot_changed")
            if require_completed_bootstrap:
                for name in (".provider-bootstrap-fresh.json", ".provider-bootstrap-complete.json"):
                    _must_be_absent(data_fd, name)
            operation = uuid.uuid4().hex
            pending = {"version": 1, "operation": operation,
                       "before": {name: actual["files"][name]["sha256"] if actual["files"][name] else None for name in _FILES},
                       "after": {name: hashlib.sha256(new_bytes[name]).hexdigest() for name in _FILES},
                       "authority_before": authority_snapshot["sha256"] if authority_snapshot else None}
            pending_snapshot = _write_new(data_fd, ".provider-settings-pending.json", _json_bytes(pending))
            for name, raw in {**old, "authority": old_authority}.items():
                if raw is None:
                    continue
                backup = _write_new(data_fd, ".provider-settings-backup-" + operation + "-" + name + ".bin", raw,
                                    limit=config_limit if name == "config" else CONFIG_MAX_BYTES)
                original = authority_snapshot if name == "authority" else actual["files"][name]
                if backup["sha256"] != original["sha256"] or backup["identity"][:2] == original["identity"][:2]:
                    raise ValueError("provider_backup_invalid")
            if settings_fd is None:
                os.mkdir("provider_settings", 0o700, dir_fd=data_fd)
                os.fsync(data_fd)
                settings_fd = _directory(data_fd, "provider_settings", private=True)
            stage_name = ".provider-settings-stage-" + operation
            os.mkdir(stage_name, 0o700, dir_fd=data_fd)
            os.fsync(data_fd)
            stage_fd = _directory(data_fd, stage_name, private=True)
            settings_anchor = _directory_anchor(os.fstat(settings_fd))
            stage_anchor = _directory_anchor(os.fstat(stage_fd))
            expected = dict(actual["files"])
            moved = []

            def check():
                root_handle.check()
                _verify_lock(root, held_fd, root_fd=root_fd)
                if require_completed_bootstrap:
                    for name in (".provider-bootstrap-fresh.json", ".provider-bootstrap-complete.json"):
                        _must_be_absent(data_fd, name)
                if (_directory_anchor(os.fstat(root_fd)) != root_anchor
                        or _directory_anchor(os.stat("data", dir_fd=root_fd, follow_symlinks=False)) != data_anchor
                        or _directory_anchor(os.stat("provider_settings", dir_fd=data_fd, follow_symlinks=False)) != settings_anchor
                        or _directory_anchor(os.stat(stage_name, dir_fd=data_fd, follow_symlinks=False)) != stage_anchor
                        or _read_file(data_fd, ".provider-settings-pending.json")[1] != pending_snapshot
                        or _read_file(data_fd, ".provider-settings-authority.json")[1] != authority_snapshot):
                    raise ValueError("provider_snapshot_changed")
                for key in _FILES:
                    parent, name = (root_fd, "config.json") if key == "config" else (settings_fd, key + ".json")
                    if _read_file(parent, name, private=key != "config",
                                  limit=config_limit if key == "config" else CONFIG_MAX_BYTES)[1] != expected[key]:
                        raise ValueError("provider_snapshot_changed")
                for moved_name, snapshot, private in moved:
                    if _read_file(stage_fd, moved_name, private=private,
                                  limit=config_limit if moved_name == "config" else CONFIG_MAX_BYTES)[1] != snapshot:
                        raise ValueError("provider_snapshot_changed")

            for key in _FILES:
                check()
                parent, name = (root_fd, "config.json") if key == "config" else (settings_fd, key + ".json")
                expected[key] = _replace(parent, name, new_bytes[key], stage_fd, expected[key], key, moved,
                                         key != "config", config_limit if key == "config" else CONFIG_MAX_BYTES)
            check()
            authority = {"version": 2, "state": "committed",
                         "common_policy_sha256": common_policy_digest(common),
                         "files": {name: pending["after"][name] for name in PROVIDERS}}
            if configuration_state is not None:
                authority["configuration_state"] = configuration_state
            authority_snapshot = _replace(data_fd, ".provider-settings-authority.json", _json_bytes(authority),
                                          stage_fd, authority_snapshot, "authority", moved, True, CONFIG_MAX_BYTES)
            check()
            os.unlink(".provider-settings-pending.json", dir_fd=data_fd)
            os.fsync(data_fd)
            final = read(root, model_owners=model_owners, jev_model=jev_model, config_limit=config_limit)
            root_handle.check()
            if require_completed_bootstrap:
                for name in (".provider-bootstrap-fresh.json", ".provider-bootstrap-complete.json"):
                    _must_be_absent(data_fd, name)
            if (final["mode"] != "split" or final["snapshot"]["files"] != expected
                    or final.get("configuration_state") != configuration_state
                    or final["snapshot"].get("root_link") != actual.get("root_link")
                    or final["snapshot"]["root"][:4] != actual["root"][:4]):
                raise ValueError("provider_readback_failed")
            return final
        finally:
            if stage_fd is not None:
                os.close(stage_fd)
            if settings_fd is not None:
                os.close(settings_fd)
            if data_fd is not None:
                os.close(data_fd)
            root_handle.close()
