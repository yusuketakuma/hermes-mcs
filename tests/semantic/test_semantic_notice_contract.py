"""Audited claims and timestamps survive the final notification render."""
import semantic
from semantic_testkit import _seeded


def test_notice_preserves_all_sections_and_uses_instant_order(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.db.execute("UPDATE messages SET posted_at=? WHERE message_id=1",
                      ("2026-09-20T00:00:00+09:00",))
        db.db.execute("UPDATE messages SET posted_at=? WHERE message_id=2",
                      ("2026-09-19T20:00:00+00:00",))
        db.db.commit()
        claims = [{"section": section, "text": f"文{section}",
                   "claim_kind": "inference"}
                  for section in semantic.CLAIM_SECTIONS]
        limitations = [f"制約{i}" for i in range(8)]
        text = semantic.render_notice(db, 1, 1,
                {"claims": claims, "limitations": limitations}, "PASS",
                targets=[1, 2], quality="full")
        assert "2026/09/20 05:00 JST" in text
        assert all(f"【提案】{c['text']}" in text for c in claims)
        assert all(item in text for item in limitations)
    finally:
        db.close()
