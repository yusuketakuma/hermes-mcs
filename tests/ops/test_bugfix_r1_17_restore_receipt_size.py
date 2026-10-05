import pytest

import mcs_restore as restore
from test_backup_restore_consent import approved, resumed, restored as restored  # noqa: F401
from test_mcs_backup import world as world  # noqa: F401


def test_oversized_receipt_is_refused_before_publication(restored):
    report = restore.plan(str(restored), source_sha256="a" * 64)
    with pytest.raises(restore.RestoreConsentError,
                       match="restore_receipt_too_large"):
        restore.approve(
            str(restored), source_sha256="a" * 64,
            plan_sha256=report["plan_sha256"], confirm_human=True,
            actor="o", reason="\x01" * 2000, custody_ref="\x01" * 1000,
            delivery_policy="hold_all")
    assert not (restored / restore.APPROVAL).exists()
    resumed(restored, approved(restored))
    assert restore.writers_resumed(str(restored))
