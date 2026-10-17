"""Applied receipts say what changed (fully synthetic receipts)."""
from adapters.common import text


def test_applied_text_names_the_change():
    assert text.ja({"outcome": "applied", "request_id": 7, "before": None}) == "タスク #7 を作成しました。"
    assert text.ja({"outcome": "applied", "request_id": 7, "before": {"request_id": 7}}) == "タスク #7 を更新しました。"
    assert text.ja({"outcome": "applied", "signal_key": "k"}) == "候補を却下しました。"
    assert text.ja({"outcome": "applied", "field": "summary"}) == "誤りの報告を記録しました。"
    assert text.ja({"outcome": "applied", "action": "ack"}) == "確認済みにしました。"
    assert text.ja({"outcome": "applied", "action": "ack", "withdrawn": True}) == "確認を取り消しました。"
    assert text.ja({"outcome": "applied", "action": "assign", "released": True}) == "担当を外しました。"
    # a notification receipt's uuid request_id is not a task number
    assert text.ja({"outcome": "applied", "request_id": "0b7c-uuid"}) == "反映しました。"
    assert text.ja({"applied": True}) == "反映しました。"
