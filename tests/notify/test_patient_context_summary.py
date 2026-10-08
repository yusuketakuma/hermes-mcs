"""Private summary excerpts stay source-current and omit identifier categories."""
import extract
import notify_views
import rollup
from extract_testkit import _ledger, _message


def test_background_summary_is_private_excerpt_and_rejects_edited_source(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        led.save_messages([_message(body="住所：合成町\n連絡先：合成窓口\n既往歴：合成疾患。\nアレルギー：なし")])
        extract.run_pending(led)
        roll = rollup.build_rollup(led, 1)
        lines = "\n".join(notify_views._patient_context_lines(led.db, 1, roll))
        assert "合成疾患。" in lines and "アレルギー" in lines
        assert "合成町" not in lines and "合成窓口" not in lines
        assert "投稿#1" in lines and "背景・療養情報" in lines
        led.save_messages([_message(body="既往歴：訂正した合成記載。")])
        lines = "\n".join(notify_views._patient_context_lines(led.db, 1, roll))
        assert "合成疾患。" not in lines and "まとめに反映された情報なし" in lines
    finally:
        led.close()


def test_background_summary_reads_updated_memo_without_waiting_for_rollup(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        led.karte_summary_store(1, 100, {"comment": "方針：合成の旧方針。", "updated_at": "2026-10-05"})
        roll = rollup.build_rollup(led, 1)
        led.karte_summary_store(1, 100, {"comment": "方針：合成の訂正方針。", "updated_at": "2026-10-06"})
        text = "\n".join(notify_views._patient_context_lines(led.db, 1, roll))
        assert "訂正方針" in text and "旧方針" not in text
        assert "連携サマリー" in text
    finally:
        led.close()


def test_raw_vital_without_extraction_is_unconfirmed_in_summary(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        led.save_messages([_message(body="本人の血圧120/80です。")])
        rollup.rebuild(led, 1)
        _, text = notify_views.patient_summary_text(led.db, 1, cfg={})
        assert "■ バイタル: 未確認" in text
        assert "■ バイタル: なし" not in text
    finally:
        led.close()


def test_unclassified_memo_is_not_reported_as_no_record(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        comment = "合成疾患で療養中。歩行には介助を要します。"
        led.karte_summary_store(1, 100, {"comment": comment, "updated_at": "2026-10-08"})
        assert extract.extract_context(comment) == []
        _, text = notify_views.patient_summary_text(led.db, 1, cfg={})
        assert "まとめに反映された情報なし" in text
        assert "・記載なし" not in text
        assert comment in text
    finally:
        led.close()
