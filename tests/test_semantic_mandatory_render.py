"""Mandatory render layer tests (T11).

The canonical contract drives a code-generated layer: every verified
fact is listed and every non-terminal obligation is disclosed, so an
omitting model summary can never make adjudicated content invisible.
"""
import semantic
import semantic_render as render
from test_mcs_semantic import _seeded


def _doc(facts=(), obligations=()):
    return {"version": "semantic-facts/v2",
            "source": {"message_id": "m1", "revision": "r1",
                       "content_hash": "h", "body_codepoints": 10,
                       "content_quality": "full",
                       "attachments_complete": True,
                       "source_fingerprint": "sf_x"},
            "atoms": [], "chunks": [], "evidence": [],
            "facts": list(facts), "obligations": list(obligations),
            "relations": [],
            "coverage": {"category_counts": {},
                         "open_obligation_ids": [], "limitations": [],
                         "status": "complete"}}


def _fact(statement, kind="medication_event", verified=True):
    return {"fact_id": f"fact_{statement}", "kind": kind,
            "subject": "patient:1", "actor": "sender:s1",
            "statement": statement, "polarity": "affirmed",
            "epistemic": "asserted", "workflow_status": "performed",
            "event_time": "unknown", "valid_time": "unknown",
            "evidence_ids": [], "obligation_ids": [],
            "importance": "T1", "provenance": "local_llm",
            "validation_status": "verified" if verified else "unverified"}


def _obligation(category, status):
    return {"obligation_id": f"ob_{category}_{status}",
            "owner_id": "chk_1", "category": category,
            "source": "deterministic", "importance": "unknown",
            "status": status, "fact_ids": []}


def test_mandatory_render_lists_verified_facts_with_category():
    doc = _doc(facts=[
        _fact("アムロジピン継続"),
        _fact("血圧120/80", kind="vital_lab"),
        _fact("検証外の事実", verified=False),
    ])
    out = render.mandatory_render(doc)
    assert any("アムロジピン継続" in line for line in out["facts"])
    assert any(line.startswith("バイタル・検査｜")
               for line in out["facts"])
    assert not any("検証外の事実" in line for line in out["facts"])


def test_mandatory_render_discloses_nonterminal_obligations():
    doc = _doc(obligations=[
        _obligation("medication", "open"),
        _obligation("vital_lab", "ambiguous"),
        _obligation("preference", "failed"),
        _obligation("care_event", "covered"),
        _obligation("request_pending", "explicit_no_fact"),
    ])
    lims = render.mandatory_render(doc)["limitations"]
    joined = " ".join(lims)
    assert "薬剤" in joined and "バイタル・検査" in joined \
        and "希望" in joined
    assert "診療・ケア" not in joined and "依頼・未決" not in joined


def test_notice_includes_mandatory_section(tmp_path):
    db = _seeded(tmp_path)
    try:
        db.db.execute("UPDATE messages SET posted_at=? WHERE message_id=1",
                      ("2026-09-20T00:00:00+09:00",))
        db.db.commit()
        summary = {"claims": [], "limitations": ["既存の制約"],
                   "mandatory_facts": ["薬剤｜アムロジピン継続"]}
        text = semantic.render_notice(db, 1, 1, summary, "PASS",
                                      quality="full")
        assert "抽出済み事実" in text
        assert "薬剤｜アムロジピン継続" in text
        assert "既存の制約" in text
    finally:
        db.close()


def test_mandatory_render_dedupes_and_caps():
    doc = _doc(facts=[_fact(f"事実{i}") for i in range(60)]
               + [_fact("事実0")])
    out = render.mandatory_render(doc)
    assert len(out["facts"]) == 40
    assert len(set(out["facts"])) == len(out["facts"])
    # FIX-SR1: capped verified facts are disclosed, not silently dropped
    assert any("20件" in lim and "省略" in lim
               for lim in out["limitations"])
