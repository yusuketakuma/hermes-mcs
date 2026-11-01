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
