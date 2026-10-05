"""An unsent semantic_notice (no accepted chunk) is observed as delivered=[]."""

import pytest
from semantic_testkit import v2_fact
from test_semantic_lifecycle import TARGET, _chain, _read, _receipt


@pytest.mark.parametrize("cfg", [None, {"notify_target": TARGET}])
@pytest.mark.parametrize("progress", ["null", "zero"])
def test_unsent_notice_yields_empty_delivered(tmp_path, cfg, progress):
    db, final_id, event_id, _, _ = _chain(
        tmp_path, [v2_fact("f1", statement="薬剤A継続")])
    try:
        if progress == "zero":
            _receipt(db, event_id, "pending", 0, None)
        got = _read(tmp_path, final_id, cfg)
        assert got["fact_ids"]["delivered"] == []
        assert got["observations"]["delivered"] == "notice_receipt"
    finally:
        db.close()
