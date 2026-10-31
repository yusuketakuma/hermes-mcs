"""同じ保存先の旧導入記録を版に応じて安全に引き継ぐ。"""
from __future__ import annotations

import install_options


class MigrationError(ValueError):
    """The migration cannot be proven safe or its publication is uncertain."""


def plan(config_path, legacy_path):
    base = install_options.inspect(config_path, legacy_path)
    central, _ = install_options._central(config_path)
    legacy, _ = install_options._legacy(legacy_path)
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
    """Split verified legacy settings after the updater has quiesced writers."""
    try:
        import cloud_policy
        import provider_settings
        import semantic_jev

        owners = {model: name for name, model in cloud_policy.PROVIDERS.items()}
        options = {"model_owners": owners, "jev_model": semantic_jev.JEV_MODEL}
        before = provider_settings.read(root, **options)
        if before["mode"] == "split":
            if provider_settings.read(root, **options) != before:
                raise MigrationError("provider_settings_changed")
            return False
        if before["mode"] != "legacy_unverified":
            raise MigrationError("provider_settings_unverified")
        raw = before["common"]
        settings, errors = cloud_policy.resolve_settings(raw, str(root))
        if cloud_policy.blocking_errors(errors):
            raise MigrationError("provider_settings_invalid")
        common, providers = provider_settings.split_legacy(raw, owners, jev_model=semantic_jev.JEV_MODEL)
        if before_publication is not None:
            before_publication()
        saved = provider_settings.commit(root, before["snapshot"], common, providers,
            selected_provider=settings["provider"], **options)
        checked = provider_settings.read(root, **options)
        if (saved != checked or checked["mode"] != "split" or checked["common"] != common
                or checked["providers"] != providers):
            raise MigrationError("provider_settings_readback_unverified")
        return True
    except MigrationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError, ImportError):
        raise MigrationError("provider_settings_unverified") from None
