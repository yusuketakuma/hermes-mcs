"""Layout 2 / notice summaries list two or more drugs, requests or lab
values one per row; a single item and the legacy surface stay inline."""
import structured_view
from notify_testkit import _llm_extract, _seed_thread, led  # noqa: F401

__all__ = ["led"]


def test_two_or_more_items_break_into_rows_only_on_plain_surfaces():
    assert structured_view._item_line("薬剤", ["合成薬A 5mg[開始]"], "、", True) == "薬剤: 合成薬A 5mg[開始]"
    assert structured_view._item_line("薬剤", ["合成薬A 5mg[開始]", "合成薬B 0.5T[変更]"], "、", True) \
        == "薬剤:\n　・合成薬A 5mg[開始]\n　・合成薬B 0.5T[変更]"
    assert structured_view._item_line("薬剤", ["合成薬A 5mg[開始]", "合成薬B 0.5T[変更]"], "、", False) \
        == "薬剤: 合成薬A 5mg[開始]、合成薬B 0.5T[変更]"


def test_structured_lines_plain_breaks_requests_and_meds(led):
    _seed_thread(led, mids=(100,))
    med = lambda n: {"name": n, "dose": "5mg", "action": "start", "subject": "patient", "status": "current",  # noqa: E731
                     "negated": False, "unverified": False, "evidence": "x"}
    _llm_extract(led, 100, {"summary": "合成", "meds": [med("合成薬A"), med("合成薬B")],
                            "requests": [{"to": "医師", "action": "合成確認一"}, {"to": "薬剤師", "action": "合成確認二"}]})
    led.db.commit()
    plain = structured_view.structured_lines(led.db, 100, plain=True)
    legacy = structured_view.structured_lines(led.db, 100)
    assert "薬剤:\n　・合成薬A 5mg[開始]\n　・合成薬B 5mg[開始]" in plain
    assert "依頼:\n　・医師へ合成確認一\n　・薬剤師へ合成確認二" in plain
    assert "薬剤: 合成薬A 5mg[開始]、合成薬B 5mg[開始]" in legacy
    assert "依頼: 医師へ合成確認一 / 薬剤師へ合成確認二" in legacy


def test_capped_card_text_drops_a_row_whole():
    import notify_render
    rows = "\n".join(f"・薬剤: 合成薬{i} {i}mg[開始]" for i in range(40))
    capped = notify_render._cap_card_text(rows, cap=200)
    kept, marker = capped.split("…\n", 1)
    assert marker == "（省略 — 本文表示または原本を参照）"
    assert kept.splitlines() and all(line in rows.splitlines() for line in kept.splitlines())
    assert len(kept) < 200
    # one long line without a boundary still cuts mid-text
    assert notify_render._cap_card_text("x" * 300, cap=100).startswith("x" * 99 + "…")
