"""Audit capture validates typed aggregates before private publication and human approval."""
import operation_contract
import json
import stat
from pathlib import Path

import pytest

import ledger
import mcs_refstats
from common_testkit import make_ledger
from views_testkit import _message


def capture(tmp_path):
    snapshot = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snapshot"))
    return mcs_refstats.main(["capture", "--name", "audit", "--preset", "extraction_audit",
        "--snapshot", str(snapshot), "--data-dir", str(tmp_path)])


def test_actual_snapshot_capture_stays_pending_private_and_ascii(tmp_path, capsys):
    db = make_ledger(tmp_path)
    db.save_messages([_message(1)])
    db.db.execute("UPDATE messages SET body_text='本人の薬は合成ボックスで保管しています。'")
    db.db.commit()
    try:
        assert capture(tmp_path) == 0
        capsys.readouterr()
        pending = Path(operation_contract.reference_path(tmp_path, "audit", "pending"))
        content = pending.read_text()
        report = json.loads(content)
        assert content.isascii() and "保管" not in content
        assert report["stats"]["omission_proxy"]["population"]["no_output"] == 1
        assert report["stats"]["attachment_share"]["status"] == "ok"
        assert stat.S_IMODE(pending.stat().st_mode) == 0o600
        assert not Path(operation_contract.reference_path(tmp_path, "audit", "approved")).exists()
        assert not db.artifacts("refstat_approval_v1")
    finally:
        db.close()


@pytest.mark.parametrize("corrupt", ["raw_field", "raw_enum", "nan"])
def test_capture_rejects_content_or_types_before_replacing_pending(tmp_path, monkeypatch, capsys, corrupt):
    db = make_ledger(tmp_path)
    db.save_messages([_message(1)])
    db.close()
    assert capture(tmp_path) == 0
    capsys.readouterr()
    path = Path(operation_contract.reference_path(tmp_path, "audit", "pending"))
    original = path.read_bytes()
    import mcs_view
    real_stats = mcs_view.View.stats
    def bad_stats(self, args):
        result = real_stats(self, args)
        value = result["stats"]["feedback_by_field"]
        if corrupt == "raw_field":
            value["body_text"] = "SYNTHETIC_PRIVATE_TEXT"
        elif corrupt == "raw_enum":
            value["rows"]["items"][0]["field"] = "SYNTHETIC_PRIVATE_TEXT"
        else:
            value["invalid_reports"] = float("nan")
        return result
    monkeypatch.setattr(mcs_view.View, "stats", bad_stats)
    assert capture(tmp_path) == 1
    assert "SYNTHETIC_PRIVATE_TEXT" not in capsys.readouterr().out
    assert path.read_bytes() == original
