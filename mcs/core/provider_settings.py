"""Read and compose provider-specific configuration without implicit migration."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
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
from llm_retry import CLOUD_MAX_ATTEMPTS


PROVIDERS = ("anthropic", "openai", "jev")
# The cloud providers' files; provider_registry.CLOUD_PROVIDERS lists the same names.
CLOUD_PROVIDERS = PROVIDERS[:2]
# The provider whose profiles the older ``cloud_llm.profiles`` key holds.
LEGACY_PROFILES_PROVIDER = "openai"
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


# Jev model ids an earlier release fixed. Before a model could be chosen, a
# stored Jev ``model`` could only equal the fixed id, so one of these names the
# code default ``jev_model``, never itself; it is not offered as a choice.
JEV_RETIRED_PINS = frozenset({"jev-1.13.0"})
# A chosen Jev model is one fixed version id; an alias such as "latest" never is (AT-064).
_JEV_MODEL_ID = re.compile(r"jev-(\d{1,4})\.(\d{1,4})\.(\d{1,4})")


def jev_model_version(model) -> tuple[int, int, int] | None:
    """The (major, minor, patch) of a fixed Jev model id, or None for anything else."""
    match = _JEV_MODEL_ID.fullmatch(model) if isinstance(model, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def effective_jev_model(pin, jev_model) -> str | None:
    """The Jev model a stored ``model`` selects over the code default ``jev_model``, or None when invalid.

    Absent, the default itself or a retired pin selects the default; another
    fixed version id selects that version."""
    if not isinstance(jev_model, str) or pin is not None and not isinstance(pin, str):
        return None
    if pin is None or pin == jev_model or pin in JEV_RETIRED_PINS:
        return jev_model
    return pin if jev_model_version(pin) is not None else None


def jev_model_pin_ok(pin, jev_model) -> bool:
    """Whether a stored Jev ``model`` is valid: the default, a retired pin or a fixed version id."""
    return pin is not None and effective_jev_model(pin, jev_model) is not None


def _jev_values(value, jev_model):
    # Deferred: provider_settings is imported by the upgrade reconciliation
    # launcher, whose reconstructed tree does not put llm/transport on the path.
    from llm_retry import CLOUD_MAX_ATTEMPTS
    if "model" in value and not jev_model_pin_ok(value["model"], jev_model):
        raise ValueError("provider_model_invalid")
    for key, low, high in (("daily_request_budget", 0, 100000), ("max_attempts_per_try", 1, CLOUD_MAX_ATTEMPTS),
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


def _profiles(value, provider=LEGACY_PROFILES_PROVIDER):
    """Purpose profiles of one provider, from that provider's finite definitions."""
    import provider_registry
    module = provider_registry.provider_module(provider)
    if module is None:
        raise ValueError("provider_profiles_invalid")
    if (not isinstance(value, dict) or value.keys() - module.PROFILE_PURPOSES
            or any(not isinstance(role, str) or not isinstance(profile, str)
                   or profile not in module.PROFILE_DEFINITIONS for role, profile in value.items())):
        raise ValueError("provider_profiles_invalid")


def _owner(model, model_owners):
    if not isinstance(model, str) or not isinstance(model_owners, dict):
        raise ValueError("provider_model_invalid")
    name = model_owners.get(model)
    if name is None:
        # Another configurable model belongs to the provider that owns its default model.
        import provider_registry
        name = next((owner for owner, module in provider_registry.PROVIDER_MODULES.items()
                     if model in module.CONFIG_MODELS and model_owners.get(module.MODEL) == owner), None)
    if name not in CLOUD_PROVIDERS:
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
            providers[LEGACY_PROFILES_PROVIDER]["profiles"] = deepcopy(profiles)
        if "provider_profiles" in cloud:
            by_provider = cloud.pop("provider_profiles")
            if not isinstance(by_provider, dict) or by_provider.keys() - set(CLOUD_PROVIDERS):
                raise ValueError("provider_profiles_invalid")
            for name, profiles in by_provider.items():
                _profiles(profiles, name)
                if "profiles" in providers[name] and providers[name]["profiles"] != profiles:
                    raise ValueError("provider_config_ambiguous")
                providers[name]["profiles"] = deepcopy(profiles)
        if "provider_budgets" in cloud:
            budgets = cloud["provider_budgets"]
            if not isinstance(budgets, dict) or budgets.keys() - set(CLOUD_PROVIDERS):
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
            or set(providers) != set(PROVIDERS) or selected_provider not in CLOUD_PROVIDERS):
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
            if name == "jev":
                raise ValueError("provider_profiles_invalid")
            _profiles(value["profiles"], name)
        if name == "jev":
            _jev_values(value, jev_model)
    cloud_values = any(providers[name] for name in CLOUD_PROVIDERS)
    if cloud_values:
        cloud = result.setdefault("cloud_llm", {})
        if (not isinstance(cloud, dict) or "model" in cloud or "profiles" in cloud
                or "provider_profiles" in cloud
                or "provider_budgets" in cloud and cloud["provider_budgets"] != {}):
            raise ValueError("provider_config_ambiguous")
        model = providers[selected_provider].get("model")
        if model is not None:
            cloud["model"] = model
        if "profiles" in providers[selected_provider]:
            # "profiles" keeps its older meaning; other providers' purpose profiles are opt-in.
            profiles = deepcopy(providers[selected_provider]["profiles"])
            if selected_provider == LEGACY_PROFILES_PROVIDER:
                cloud["profiles"] = profiles
            else:
                cloud["provider_profiles"] = {selected_provider: profiles}
        budgets = {name: deepcopy(providers[name]["budget"]) for name in CLOUD_PROVIDERS
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
    order = cloud.get("provider_order", list(CLOUD_PROVIDERS))
    if (not isinstance(order, list) or not order
            or any(not isinstance(name, str) or name not in CLOUD_PROVIDERS for name in order)
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


def _config_presence(root_fd):
    try:
        info = os.stat("config.json", dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("provider_file_invalid")
    return _identity(info)


_PROVIDER_NAMESPACE = frozenset({
    ".provider-settings-authority.json", ".provider-settings-pending.json",
    "provider_settings"})


def _provider_namespace(data_fd):
    """data/ entries that can change the provider-settings decision — runtime
    churn of unrelated entries never invalidates a read."""
    names = set()
    with os.scandir(data_fd) as entries:
        for index, entry in enumerate(entries):
            if index >= 100000:
                raise ValueError("provider_artifact_scan_limit")
            name = entry.name
            if name in _PROVIDER_NAMESPACE or name.startswith(
                    (".provider-settings-backup-", ".provider-settings-stage-")):
                names.add(name)
    return frozenset(names)


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
        root_identity = _directory_anchor(root_info)
        config = _config_presence(root_fd)
        try:
            data_fd = _directory(root_fd, "data")
        except FileNotFoundError:
            pass
        state = "legacy_unverified" if config is not None else "uninitialized_or_legacy_unverified"
        if data_fd is not None:
            data_identity = _directory_anchor(os.fstat(data_fd))
            data_namespace = _provider_namespace(data_fd)
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
                    or _provider_namespace(data_fd) != data_namespace
                    or _directory_anchor(os.fstat(data_fd)) != data_identity
                    or _directory_anchor(os.stat("data", dir_fd=root_fd, follow_symlinks=False)) != data_identity):
                raise ValueError("provider_settings_changed")
            if settings_fd is None:
                _must_be_absent(data_fd, "provider_settings")
            elif (_identity(os.fstat(settings_fd)) != settings_identity
                    or _identity(os.stat("provider_settings", dir_fd=data_fd, follow_symlinks=False)) != settings_identity):
                raise ValueError("provider_settings_changed")
        else:
            _must_be_absent(root_fd, "data")
        root_handle.check()
        if _config_presence(root_fd) != config or _directory_anchor(os.fstat(root_fd)) != root_identity:
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
        root_identity = _directory_anchor(root_info)
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
        data_identity = _directory_anchor(os.fstat(data_fd)) if data_fd is not None else None
        data_namespace = _provider_namespace(data_fd) if data_fd is not None else None
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
                        for name in CLOUD_PROVIDERS:
                            if "model" in providers[name] and model_owners is None and allow_unavailable:
                                raise ValueError("provider_validation_context_missing")
                            if "model" in providers[name] and _owner(providers[name]["model"], model_owners) != name:
                                raise ValueError("provider_model_invalid")
                            if "budget" in providers[name]:
                                _budget(providers[name]["budget"])
                            if "profiles" in providers[name]:
                                _profiles(providers[name]["profiles"], name)
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
                            or isinstance(cloud, dict) and ("model" in cloud or "profiles" in cloud or "provider_profiles" in cloud
                                or "provider_budgets" in cloud and cloud["provider_budgets"] != {})
                            or isinstance(semantic, dict) and bool(semantic.keys() & _JEV_KEYS)):
                        raise ValueError("provider_config_ambiguous")
                    providers = None
                    mode = "provider_unavailable"
                else:
                    if (isinstance(cloud, dict) and ("model" in cloud or "profiles" in cloud or "provider_profiles" in cloud
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
                or _directory_anchor(os.fstat(root_fd)) != root_identity):
            raise ValueError("provider_settings_changed")
        if data_fd is None:
            _must_be_absent(root_fd, "data")
        elif settings_fd is None:
            _must_be_absent(data_fd, "provider_settings")
        if data_fd is not None and (
                data_identity != _directory_anchor(os.fstat(data_fd))
                or data_identity != _directory_anchor(os.stat("data", dir_fd=root_fd, follow_symlinks=False))
                or _provider_namespace(data_fd) != data_namespace):
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


def committed_pending(root, lock_fd) -> str:
    """Operation of a pending record whose files all reached their "after" state.

    A power loss or SIGKILL after the last file and the authority were
    published, but before the pending record was unlinked, leaves every reader
    held (GA-provider-settings-writer-1). Only that exact state is accepted:
    each file's hash equals pending["after"] and the authority is the committed
    v2 record for those files. Anything else raises and stays held for a person."""
    import re
    root = Path(root)
    handle = _RootHandle(root)
    data_fd = settings_fd = None
    try:
        _verify_lock(root, lock_fd, root_fd=handle.fd)
        data_fd = _directory(handle.fd, "data")
        raw, _ = _read_file(data_fd, ".provider-settings-pending.json")
        if raw is None:
            raise ValueError("provider_pending_missing")
        pending = _document(raw)
        after = pending.get("after")
        if (pending.get("version") != 1 or not isinstance(after, dict) or set(after) != set(_FILES)
                or not isinstance(pending.get("operation"), str)
                or re.fullmatch(r"[0-9a-f]{32}", pending["operation"]) is None):
            raise ValueError("provider_pending_invalid")
        settings_fd = _directory(data_fd, "provider_settings", private=True)
        for key in _FILES:
            parent, name, private = ((handle.fd, "config.json", False) if key == "config"
                                     else (settings_fd, key + ".json", True))
            _, snapshot = _read_file(parent, name, private=private,
                                     limit=ALIAS_CONFIG_MAX_BYTES if key == "config" else CONFIG_MAX_BYTES)
            if snapshot is None or snapshot["sha256"] != after[key]:
                raise ValueError("provider_pending_not_committed")
        authority = _document(_read_file(data_fd, ".provider-settings-authority.json")[0])
        if (authority.get("version") != 2 or authority.get("state") != "committed"
                or authority.get("files") != {name: after[name] for name in PROVIDERS}):
            raise ValueError("provider_pending_not_committed")
        if _read_file(data_fd, ".provider-settings-pending.json")[0] != raw:
            raise ValueError("provider_snapshot_changed")
        handle.check()
        return pending["operation"]
    finally:
        if settings_fd is not None:
            os.close(settings_fd)
        if data_fd is not None:
            os.close(data_fd)
        handle.close()


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


# Each save of a split installation leaves CHANGE_RECORD in its own stage
# directory: which entry point saved and how the operating limits changed. The
# record holds only fixed codes, hashes, times and numbers, never settings text.
SETTINGS_ORIGINS = frozenset({
    "unattributed", "cloud_llm_budget", "cloud_llm_providers", "provider_dialog", "settings",
    "remote_access", "jev_model", "setup_init", "setup_slack_channels", "setup_fact_source",
    "setup_features", "update_migration", "install_bootstrap", "install_runtime"})
CHANGE_RECORD = "change.json"
CHANGE_RECORDS_MAX = 1000
_CHANGE_RECORD_MAX_BYTES = 64 * 1024
_CHANGE_RECORD_KEYS = frozenset({"version", "operation", "saved_at", "origin", "command_id",
                                 "files", "authority", "limits", "changed"})
_STAGE_NAME = re.compile(r"\.provider-settings-stage-([0-9a-f]{32})")
_SAVED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BUDGET_FIELDS = ("daily_cost_limit_usd", "monthly_cost_limit_usd")
_JEV_LIMITS = ("daily_request_budget", "until")
# Larger integers are not limits any validator accepts, and floats lose them.
_LIMIT_INT_MAX = 2 ** 53
# "absent" falls back to the code default; an explicit null means no limit.
ABSENT, INVALID = "absent", "invalid"
_MISSING, _MALFORMED = object(), object()


def require_origin(origin, command_id=None):
    """Refuse an entry point code outside SETTINGS_ORIGINS or a command id that is not a canonical UUID."""
    try:
        valid_command = command_id is None or isinstance(command_id, str) and str(uuid.UUID(command_id)) == command_id
    except ValueError:
        valid_command = False
    if type(origin) is not str or origin not in SETTINGS_ORIGINS or not valid_command:
        raise ValueError("provider_settings_origin_invalid")


def _cloud_limit_keys():
    # Imported on use: the run-window and spending keys belong to semantic_runtime,
    # whose import reads and creates nothing, so a fresh install can load it too.
    from semantic_runtime import CLOUD_OPERATING_LIMITS
    return tuple(key for key in CLOUD_OPERATING_LIMITS if key != "provider_budgets")


def _member(section, key):
    if section is _MISSING:
        return _MISSING
    return section.get(key, _MISSING) if isinstance(section, dict) else _MALFORMED


def _number(value):
    return (type(value) is int and -_LIMIT_INT_MAX <= value <= _LIMIT_INT_MAX
            or type(value) is float and math.isfinite(value))


def _limit_value(value):
    if value is _MISSING:
        return ABSENT
    if value is None or _number(value):
        return value
    if isinstance(value, str):
        # An end time until_epoch accepts is kept in its canonical ISO form, never as typed:
        # the record carries no settings text, and every accepted separator stays comparable.
        try:
            until_epoch(value)
            # The same Z rule as until_epoch: updater helpers on Python 3.10 reject Z.
            return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value).isoformat()
        except (TypeError, ValueError, OverflowError):
            return INVALID
    return INVALID


def _limits(cloud, budgets, jev):
    result = {"cloud_llm." + key: _limit_value(_member(cloud, key)) for key in _cloud_limit_keys()}
    for name in CLOUD_PROVIDERS:
        budget = _member(budgets, name)
        for field in _BUDGET_FIELDS:
            result[f"cloud_llm.provider_budgets.{name}.{field}"] = _limit_value(_member(budget, field))
    for key in _JEV_LIMITS:
        result["semantic." + key] = _limit_value(_member(jev, key))
    return result


def _split_limits(common, providers):
    return _limits(common.get("cloud_llm", _MISSING),
                   {name: providers[name]["budget"] for name in CLOUD_PROVIDERS if "budget" in providers[name]},
                   providers["jev"])


def view_limits(view):
    """Operating limits of a legacy-shaped settings view, keyed as in change records."""
    if not isinstance(view, dict):
        raise ValueError("provider_config_invalid")
    cloud = view.get("cloud_llm", _MISSING)
    return _limits(cloud, _member(cloud, "provider_budgets"), view.get("semantic", _MISSING))


def _source_limits(old, split, model_owners, jev_model):
    # The bytes the commit verified under the edit lock, never a second read.
    common = _document(old["config"])
    if split:
        providers = {name: _document(old[name]) for name in PROVIDERS}
    else:
        common, providers = split_legacy(common, model_owners, jev_model=jev_model)
    return _split_limits(common, providers)


def _authority_state(authority):
    """The provider hashes and common-policy digest an authority binds, which chain the records."""
    return {"files": {name: authority["files"][name] for name in PROVIDERS},
            "common_policy_sha256": authority["common_policy_sha256"]}


def _change_record(operation, pending, authority, limits, origin, command_id):
    before, after = limits
    return _json_bytes({
        "version": 1, "operation": operation,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "origin": origin, "command_id": command_id,
        "files": {"before": dict(pending["before"]), "after": dict(pending["after"])},
        "authority": {"before": authority[0], "after": authority[1]},
        "limits": {"before": before, "after": after},
        "changed": sorted(key for key in after if before.get(key, ABSENT) != after[key])},
        limit=_CHANGE_RECORD_MAX_BYTES)


def _stored_limit_ok(value):
    return value is None or _number(value) or isinstance(value, str) and (
        value in (ABSENT, INVALID) or _limit_value(value) == value)


def _hashes_ok(side, keys):
    return (isinstance(side, dict) and side.keys() == set(keys)
            and all(value is None or isinstance(value, str) and _SHA256.fullmatch(value) is not None
                    for value in side.values()))


def _state_ok(state):
    return (isinstance(state, dict) and state.keys() == {"files", "common_policy_sha256"}
            and _hashes_ok(state["files"], PROVIDERS) and None not in state["files"].values()
            and isinstance(state["common_policy_sha256"], str)
            and _SHA256.fullmatch(state["common_policy_sha256"]) is not None)


def _checked_record(record, operation):
    files, authority, limits = record.get("files"), record.get("authority"), record.get("limits")
    if (record.keys() != _CHANGE_RECORD_KEYS or type(record["version"]) is not int or record["version"] != 1
            or record["operation"] != operation or not isinstance(record["saved_at"], str)
            or _SAVED_AT.fullmatch(record["saved_at"]) is None
            or not isinstance(files, dict) or files.keys() != {"before", "after"}
            or not all(_hashes_ok(side, _FILES) for side in files.values())
            or not isinstance(authority, dict) or authority.keys() != {"before", "after"}
            or not _state_ok(authority["after"])
            or authority["before"] is not None and not _state_ok(authority["before"])
            or not isinstance(limits, dict) or limits.keys() != {"before", "after"}
            or any(not isinstance(side, dict) or any(not isinstance(key, str) or not _stored_limit_ok(value)
                                                     for key, value in side.items()) for side in limits.values())
            or limits["before"].keys() != limits["after"].keys()
            or record["changed"] != sorted(key for key in limits["after"]
                                           if limits["before"][key] != limits["after"][key])):
        raise ValueError("provider_record_invalid")
    require_origin(record["origin"], record["command_id"])
    return record


def change_records(root, limit) -> dict:
    """The change records of the newest ``limit`` saves of a split installation, read without changing anything.

    Only the ``limit`` stage directories with the latest modification times are
    opened; ``more`` says whether older ones exist. A stage directory without a
    record (a save before records existed, a held or later finalized pending
    save) is counted, as is a record that is a link, too large, not private or
    malformed; neither is returned. ``current`` is the authority's state, which
    a record's ``authority.after`` equals when that save produced the current
    settings. The reader takes no lock: while a save is publishing
    (``save_in_progress``) the authority can already be new and its record not
    yet written, so a caller must not compare the two then."""
    if type(limit) is not int or not 1 <= limit <= CHANGE_RECORDS_MAX:
        raise ValueError("provider_record_limit_invalid")
    root = Path(root)
    if not root.is_absolute():
        raise ValueError("provider_root_invalid")
    handle = _RootHandle(root)
    data_fd = None
    records, unrecorded, rejected = [], 0, 0
    try:
        data_fd = _directory(handle.fd, "data")
        pending = _read_file(data_fd, ".provider-settings-pending.json")[1] is not None
        try:
            authority = _document(_read_file(data_fd, ".provider-settings-authority.json")[0])
            current = _authority_state(authority)
            if not _state_ok(current):
                current = None
        except (ValueError, KeyError, TypeError):
            current = None
        stages = []
        with os.scandir(data_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= 100000:
                    raise ValueError("provider_artifact_scan_limit")
                match = _STAGE_NAME.fullmatch(entry.name)
                if match is None:
                    continue
                try:
                    changed = os.stat(entry.name, dir_fd=data_fd, follow_symlinks=False).st_mtime_ns
                except FileNotFoundError:
                    continue
                stages.append((changed, entry.name, match.group(1)))
        stages.sort(reverse=True)
        for modified, name, operation in stages[:limit]:
            try:
                stage_fd = _directory(data_fd, name, private=True)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                rejected += 1
                continue
            try:
                raw, _ = _read_file(stage_fd, CHANGE_RECORD, limit=_CHANGE_RECORD_MAX_BYTES)
                if raw is None:
                    unrecorded += 1
                else:
                    records.append((modified, _checked_record(_document(raw), operation)))
            except (OSError, ValueError, RecursionError, ArithmeticError):
                rejected += 1
            finally:
                os.close(stage_fd)
        handle.check()
    finally:
        if data_fd is not None:
            os.close(data_fd)
        handle.close()
    # Newest first by the stage's modification time; the save time breaks ties.
    records.sort(key=lambda item: (item[0], item[1]["saved_at"]), reverse=True)
    return {"records": [record for _, record in records], "current": current, "save_in_progress": pending, "more": len(stages) > limit,
            "unrecorded_saves": unrecorded, "rejected_records": rejected}


def commit(root, expected_snapshot, common, providers, *, selected_provider, model_owners, jev_model=None,
           config_limit=CONFIG_MAX_BYTES, origin="unattributed", command_id=None):
    """Commit after taking the normal config editor lock."""
    return _commit(root, expected_snapshot, common, providers, selected_provider=selected_provider,
                   model_owners=model_owners, jev_model=jev_model, lock_fd=None, config_limit=config_limit,
                   validate=_validate_standard, origin=origin, command_id=command_id)


def commit_locked(root, expected_snapshot, common, providers, *, selected_provider, model_owners,
                  lock_fd, jev_model=None, config_limit=CONFIG_MAX_BYTES, origin="unattributed", command_id=None):
    """Commit using a borrowed editor lock descriptor without closing it."""
    return _commit(root, expected_snapshot, common, providers, selected_provider=selected_provider,
                   model_owners=model_owners, jev_model=jev_model, lock_fd=lock_fd, config_limit=config_limit,
                   validate=_validate_standard, origin=origin, command_id=command_id)


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

    # The installer owns the first local-model choice as part of the fresh
    # machine bootstrap; other known settings still mean the machine is not fresh.
    allowed = {"runtime_mode", "recovery_python", "install_args", "local_llm"}
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
    return _commit(root, expected_snapshot, common, providers, selected_provider=CLOUD_PROVIDERS[0],
                   model_owners=model_owners, jev_model=jev_model, lock_fd=lock_fd,
                   config_limit=CONFIG_MAX_BYTES, validate=_validate_bootstrap, configuration_state="bootstrap",
                   origin="install_bootstrap")


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
    return _commit(root, expected_snapshot, common, before["providers"], selected_provider=CLOUD_PROVIDERS[0],
                   model_owners=model_owners, jev_model=jev_model, lock_fd=lock_fd,
                   config_limit=CONFIG_MAX_BYTES, validate=_validate_bootstrap, configuration_state="bootstrap",
                   require_completed_bootstrap=True, origin="install_runtime")


def _commit(root, expected_snapshot, common, providers, *, selected_provider, model_owners, jev_model, lock_fd,
            config_limit, validate, configuration_state=None, require_completed_bootstrap=False,
            origin="unattributed", command_id=None):
    """Commit four settings with durable pending evidence and independent backups.

    Initial migration requires the updater's verified quiescence before new
    services start. Later edits require the verified split snapshot and editor
    lock; pending and readback fence the cooperating readers during publication.

    A rollback after a failure restores the bytes, but rename and link change
    the restored files' ctime, so callers must re-read before retrying; a stale
    snapshot or identity proof is refused as a change.

    The last write before pending is removed is the stage's CHANGE_RECORD:
    ``origin``, ``command_id``, the authority states and the operating limits
    before (from the verified old bytes) and after, built before the first
    write. Its exclusive create is handled like the other writes: an OSError or
    interrupt rolls the save back and removes the record with the stage; a
    name that already exists, a read-back mismatch, or a changed snapshot in
    the following check keeps pending and every artifact for a person.
    """
    if configuration_state not in (None, "bootstrap"):
        raise ValueError("provider_configuration_state_invalid")
    require_origin(origin, command_id)
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
        undo = None
        try:
            root_handle.check()
            if (root_handle.identity[:4] != actual["root"][:4]
                    or root_handle.link != actual.get("root_link")):
                raise ValueError("provider_snapshot_changed")
            _verify_lock(root, held_fd, root_fd=root_fd)
            data_fd = _directory(root_fd, "data")
            if actual["data"] is None or _directory_anchor(os.fstat(data_fd)) != actual["data"]:
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
            # Built before anything is written, so only the write itself can fail late.
            record_raw = _change_record(
                operation, pending,
                (_authority_state(_document(old_authority)) if old_authority is not None else None,
                 {"files": {name: pending["after"][name] for name in PROVIDERS},
                  "common_policy_sha256": common_policy_digest(common)}),
                (_source_limits(old, authority_snapshot is not None, model_owners, jev_model),
                 _split_limits(common, providers)), origin, command_id)
            stage_name = ".provider-settings-stage-" + operation
            pending_snapshot = authority_raw = None
            created_settings = False

            def rollback(error):
                # Restore the verified pre-commit files after an I/O failure or
                # interrupt under the held lock; any doubt keeps pending for a person.
                # Another party created a name proven absent: hold every artifact as evidence.
                if isinstance(error, FileExistsError):
                    return
                try:
                    # No receipt means nothing was published yet, or the commit already
                    # completed its unlink; never revert a committed state.
                    pending_now = _read_file(data_fd, ".provider-settings-pending.json")[1]
                    if pending_now is None:
                        return
                    if pending_snapshot is not None and pending_now != pending_snapshot:
                        raise ValueError("provider_snapshot_changed")
                    root_handle.check()
                    _verify_lock(root, held_fd, root_fd=root_fd)
                    if (_directory_anchor(os.stat("data", dir_fd=root_fd, follow_symlinks=False)) != data_anchor
                            or any(fd is not None and _directory_anchor(os.stat(name, dir_fd=data_fd, follow_symlinks=False))
                                   != _directory_anchor(os.fstat(fd))
                                   for fd, name in ((settings_fd, "provider_settings"), (stage_fd, stage_name)))):
                        raise ValueError("provider_snapshot_changed")
                    targets = [(root_fd, "config.json", "config", old["config"], new_bytes["config"], False, config_limit),
                               *((settings_fd, key + ".json", key, old[key], new_bytes[key], True, CONFIG_MAX_BYTES)
                                 for key in PROVIDERS),
                               (data_fd, ".provider-settings-authority.json", "authority", old_authority,
                                authority_raw, True, CONFIG_MAX_BYTES)]
                    if stage_fd is not None:
                        with os.scandir(stage_fd) as entries:
                            temporaries = [entry.name for entry in entries if entry.name.startswith("new-")]
                        for name in temporaries:
                            os.unlink(name, dir_fd=stage_fd)
                    for parent, name, moved_name, before, after, private, limit in reversed(targets):
                        if parent is None:
                            continue
                        current = _read_file(parent, name, private=private, limit=limit)[0]
                        saved = (_read_file(stage_fd, moved_name, private=private, limit=limit)[0]
                                 if stage_fd is not None else None)
                        if saved is None and current == before:
                            continue
                        if (stage_fd is None or saved != before or after is None
                                or current not in ((None, after) if saved is not None else (after,))):
                            raise ValueError("provider_snapshot_changed")
                        if current is not None:
                            discarded = "rollback-" + moved_name
                            os.rename(name, discarded, src_dir_fd=parent, dst_dir_fd=stage_fd)
                            if _read_file(stage_fd, discarded, private=private, limit=limit)[0] != after:
                                raise ValueError("provider_snapshot_changed")
                            os.unlink(discarded, dir_fd=stage_fd)
                        if saved is not None:
                            # link never overwrites a late writer's file.
                            os.link(moved_name, name, src_dir_fd=stage_fd, dst_dir_fd=parent, follow_symlinks=False)
                            os.unlink(moved_name, dir_fd=stage_fd)
                        os.fsync(stage_fd)
                        os.fsync(parent)
                    for parent, name, _, before, _, private, limit in targets:
                        if parent is not None and _read_file(parent, name, private=private, limit=limit)[0] != before:
                            raise ValueError("provider_snapshot_changed")
                    # Backups are this operation's duplicates of the originals verified in place above.
                    for name in (*_FILES, "authority"):
                        try:
                            os.unlink(".provider-settings-backup-" + operation + "-" + name + ".bin", dir_fd=data_fd)
                        except FileNotFoundError:
                            pass
                    if stage_fd is not None:
                        # This operation's own record; a rolled-back save leaves no trace.
                        try:
                            os.unlink(CHANGE_RECORD, dir_fd=stage_fd)
                        except FileNotFoundError:
                            pass
                    try:
                        os.rmdir(stage_name, dir_fd=data_fd)
                    except FileNotFoundError:
                        pass
                    if created_settings:
                        os.rmdir("provider_settings", dir_fd=data_fd)
                    os.fsync(data_fd)
                    if (pending_snapshot is not None
                            and _read_file(data_fd, ".provider-settings-pending.json")[1] != pending_snapshot):
                        raise ValueError("provider_snapshot_changed")
                    os.unlink(".provider-settings-pending.json", dir_fd=data_fd)
                    os.fsync(data_fd)
                except BaseException as failure:
                    # Keep pending and the remaining evidence; the caller re-raises the
                    # original failure, so leave a person the cause of the following hold.
                    print("mcs: 設定の保存に失敗した後、保存前の状態へ戻せませんでした。保存中の記録を残したため、"
                          "以降の処理は保留されます。担当者に確認してください"
                          f"（provider_settings_rollback_failed: {type(failure).__name__}）", file=sys.stderr)

            undo = rollback

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
                created_settings = True
                os.fsync(data_fd)
                settings_fd = _directory(data_fd, "provider_settings", private=True)
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
            authority_raw = _json_bytes(authority)
            authority_snapshot = _replace(data_fd, ".provider-settings-authority.json", authority_raw,
                                          stage_fd, authority_snapshot, "authority", moved, True, CONFIG_MAX_BYTES)
            check()
            # Written while pending still holds readers; see the docstring for its failures.
            _write_new(stage_fd, CHANGE_RECORD, record_raw, limit=_CHANGE_RECORD_MAX_BYTES)
            check()
            os.unlink(".provider-settings-pending.json", dir_fd=data_fd)
            undo = None
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
        except (OSError, KeyboardInterrupt) as error:
            if undo is not None:
                undo(error)
            raise
        finally:
            if stage_fd is not None:
                os.close(stage_fd)
            if settings_fd is not None:
                os.close(settings_fd)
            if data_fd is not None:
                os.close(data_fd)
            root_handle.close()
