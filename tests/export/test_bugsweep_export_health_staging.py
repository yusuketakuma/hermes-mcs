"""A crash-left atomic_write staging file never turns export health unknown."""
import export_delivery


def test_stale_staging_file_is_counted_apart_from_corrupt_journal(tmp_path):
    journal = tmp_path / "state" / "journal"
    journal.mkdir(parents=True)
    (journal / ".t-x.tmp").write_text("{\"partial\"")

    report = export_delivery.health(tmp_path / "state")
    assert report["status"] == "ok"
    assert report["complete"] is True
    assert report["counts"]["stale_staging"] == 1
    assert report["counts"]["corrupt"] == 0

    (journal / "zz.json").write_text("{}")
    report = export_delivery.health(tmp_path / "state")
    assert report["status"] == "unknown"
    assert report["counts"]["corrupt"] == 1
    assert "journal_invalid_or_unreadable" in report["reasons"]
