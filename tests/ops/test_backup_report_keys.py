"""The backup verification report keeps the exact key set restore fences on."""
import backup_evidence
import held_restore


def test_report_matches_the_verification_contract():
    report = held_restore.report("0" * 64, "1" * 64, {}, restore_pending=True)
    assert set(report) == set(held_restore.Verification.__annotations__) == {
        "v", "sha256", "plain_sha256", "verified_at", "inventory",
        "restore_pending", "attachment_payloads_included"}
    assert report["restore_pending"] is True
    assert report["attachment_payloads_included"] is False


def test_bundle_ceiling_adds_framing_and_one_mib():
    class Policy:
        max_snapshot_bytes = 100 * 1048576
    assert backup_evidence.bundle_ceiling(Policy) == 101 * 1048576 + 1048576
