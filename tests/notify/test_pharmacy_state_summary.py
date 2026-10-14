"""Private pharmacy reports recheck source and fingerprints; stale cache values are not shown."""
import copy
import json

import pytest

import notify_views
import rollup
from extract_testkit import _ledger, _message
from test_pharmacy_state import context, save


def test_private_summary_shows_report_wording_with_group_and_subject(tmp_path):
    led = _ledger(tmp_path)
    quote = "以前、家族の合成薬Aと合成薬Bは残薬3錠、今後の支援は未確認。"
    try:
        save(led, 1, quote, [context(quote, names=("合成薬A", "合成薬B"), subject="family")])
        state = rollup.build_rollup(led, 1)
        led.db.execute("PRAGMA query_only=ON")
        lines = notify_views._pharmacy_state_lines(led.db, 1, state)
        text = "\n".join(lines)
        assert "■ 服薬管理の報告" in text and "残薬（09-19・家族・投稿#1）" in text
        assert quote in text and "合成薬A・合成薬B" in text
        assert len(lines) == 2
    finally:
        led.close()


@pytest.mark.parametrize("change", ["edit", "delete", "corrected_extraction", "foreign_project", "foreign_message", "value", "names", "fingerprint",
                                   "fingerprint_object", "fingerprint_array", "project_bool"])
def test_changed_or_forged_cache_report_is_not_displayed(tmp_path, change):
    led = _ledger(tmp_path)
    quote = "合成薬Aの残薬3錠。"
    try:
        save(led, 1, quote, [context(quote)])
        cached = rollup.build_rollup(led, 1)
        report = cached["pharmacy_state"]["items"][0]
        if change == "edit":
            led.save_messages([_message(body="合成薬Aの残薬は不明。")])
        elif change == "delete":
            with led.db:
                led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
        elif change == "corrected_extraction":
            with led.db:
                led.db.execute("UPDATE artifacts SET content=? WHERE kind='extract_llm'",
                               (json.dumps({"summary": "合成の訂正結果"}),))
        elif change.startswith("fingerprint_"):
            report["fingerprint"] = {} if change == "fingerprint_object" else []
        elif change == "project_bool":
            report["project_id"] = True
        else:
            report[{"foreign_project": "project_id", "foreign_message": "message_id", "names": "drug_names"}.get(change, change)] = (
                2 if change.startswith("foreign_") else ["合成薬B"] if change == "names" else "FORGED_CANARY")
        text = "\n".join(notify_views._pharmacy_state_lines(led.db, 1, cached))
        assert "現在の原文と照合できる報告なし" in text
        assert "3錠" not in text and "FORGED_CANARY" not in text
    finally:
        led.close()


def test_legacy_cache_has_no_new_state_and_display_cap_is_honest(tmp_path):
    led = _ledger(tmp_path)
    try:
        assert notify_views._pharmacy_state_lines(led.db, 1, {}) == []
        quotes = [f"合成薬Aの合成報告{n}は残薬3錠。" for n in range(10)]
        save(led, 1, "\n".join(quotes), [context(quote) for quote in quotes])
        cached = rollup.build_rollup(led, 1)
        original = copy.deepcopy(cached)
        text = "\n".join(notify_views._pharmacy_state_lines(led.db, 1, cached))
        assert "他2報告" in text and "原記録で全文確認" in text
        assert cached == original
    finally:
        led.close()
