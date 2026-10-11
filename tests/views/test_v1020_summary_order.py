"""Plain-surface order ranks the free-text summary by where it was built, not by its words."""
import json

import pytest

import ledger
import structured_view


@pytest.fixture
def db(tmp_path):
    store = ledger.Ledger(str(tmp_path / "ledger.db"))
    with store.db:
        store.db.execute(
            "INSERT INTO messages(message_id,project_id,body_state,content_hash) "
            "VALUES(1,1,'full','synthetic-hash')")
    yield store
    store.close()


@pytest.mark.parametrize("summary", ["薬剤変更の連絡あり", "依頼内容を合成確認", "区分けして共有",
                                     "症状は落ち着いている", "合成｜区切りを含む概要"])
def test_summary_starting_with_a_label_word_stays_in_the_summary_slot(db, summary):
    db.artifact_add("extract_llm", json.dumps({
        "summary": summary, "points": ["合成の要点"], "events": ["visit"],
        "requests": [{"to": "医師", "action": "合成確認"}]}),
        project_id=1, message_id=1, meta={"hash": "synthetic-hash"})
    lines = structured_view.structured_lines(db.db, 1, plain=True)
    assert lines == [summary, "要点: 合成の要点", "依頼: 医師へ合成確認", "区分: 訪問"]
