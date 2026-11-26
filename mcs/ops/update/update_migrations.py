"""同じ保存先の旧導入記録を版に応じて安全に引き継ぐ。"""
from __future__ import annotations

from pathlib import Path

import install_options


class MigrationError(ValueError):
    """The migration cannot be proven safe or its publication is uncertain."""


def plan(config_path, legacy_path):
    base = install_options.inspect(config_path, legacy_path)
    central, legacy, _ = install_options._records(config_path, legacy_path)
    if install_options.inspect(config_path, legacy_path) != base:
        raise MigrationError('settings_changed')
    return central is None and legacy is not None, base, legacy


def apply(config_path, legacy_path, expected_base, before_publication):
    """Copy a verified legacy selection once; leave unrelated data in place."""
    try:
        needed, base, legacy = plan(config_path, legacy_path)
        if base != expected_base:
            raise MigrationError('settings_changed')
        if not needed:
            return False
        before_publication()
        if install_options.save(config_path, legacy_path, None, base):
            raise MigrationError('settings_publication_unverified')
        if install_options.selection(config_path, legacy_path) != legacy:
            raise MigrationError('settings_publication_unverified')
        return True
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError('settings_unverified') from None


def migrate_provider_settings(root, *, before_publication=None):
    """Consolidate legacy or split provider settings after the updater has quiesced writers."""
    try:
        import config_edit
        import cloud_policy
        import provider_settings
        import semantic_jev

        owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
        options = {"model_owners": owners, "jev_model": semantic_jev.JEV_MODEL}
        before = provider_settings.read(root, **options)
        if before.get("layout") == "single":
            if provider_settings.read(root, **options) != before:
                raise MigrationError("provider_settings_changed")
            return False
        if before["mode"] not in ("legacy_unverified", "split"):
            raise MigrationError("provider_settings_unverified")
        raw = before["common"]
        settings, errors = cloud_policy.resolve_settings(raw, str(root))
        if cloud_policy.blocking_errors(errors):
            raise MigrationError("provider_settings_invalid")
        common, providers = ((raw, before["providers"]) if before["mode"] == "split" else
                             provider_settings.split_legacy(raw, owners, jev_model=semantic_jev.JEV_MODEL))
        if before_publication is not None:
            before_publication()
        config = Path(root) / "config.json"
        legacy = Path(root) / "data" / "install_options.json"
        with config_edit._edit_lock(config) as lock_fd:
            if install_options.settings_migration_receipt.pending(legacy.parent):
                raise MigrationError("settings_migration_pending")
            central, recorded, legacy_digest = install_options._records(config, legacy)
            if recorded is not None and recorded != central:
                # The pre-merge receipt must prove the old choice made it to config first.
                raise MigrationError("settings_migration_unverified")
            saved = provider_settings.consolidate_locked(root, before["snapshot"],
                selected_provider=settings["provider"], lock_fd=lock_fd, **options)
            if recorded is not None:
                install_options.retire_legacy(legacy, legacy_digest)
        checked = provider_settings.read(root, **options)
        if (saved != checked or checked.get("layout") != "single" or checked["common"] != common
                or checked["providers"] != providers):
            raise MigrationError("provider_settings_readback_unverified")
        return True
    except MigrationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
        raise MigrationError("provider_settings_unverified") from None
