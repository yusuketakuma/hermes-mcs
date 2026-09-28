"""Mandatory render layer tests (T11).

The canonical contract drives a code-generated layer: every verified
fact is listed and every non-terminal obligation is disclosed, so an
omitting model summary can never make adjudicated content invisible.
"""
import json

import semantic
import semantic_render as render
from semantic_testkit import SOURCE, _seeded, v2_doc, v2_fact


def _doc(facts=(), obligations=()):
    return v2_doc(facts, obligations=list(obligations),
                  source={**SOURCE, "body_codepoints": 10})


def _fact(statement, kind="medication_event", verified=True):
    return v2_fact(f"fact_{statement}", kind=kind, statement=statement,
                   validation_status="verified" if verified else "unverified")


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


def test_mandatory_render_dedupes_without_cap():
    """Every verified fact renders — no count cap, no 'omitted'
    disclosure line (the cap itself was the bug: 40件打切りはしない)."""
    doc = _doc(facts=[_fact(f"事実{i}") for i in range(60)]
               + [_fact("事実0")])
    out = render.mandatory_render(doc)
    assert len(out["facts"]) == 60
    assert len(set(out["facts"])) == len(out["facts"])
    assert not any("省略" in lim for lim in out["limitations"])
    assert set(out["fact_ids"]) == {f"fact_事実{i}" for i in range(60)}


def test_mandatory_render_covers_41_facts_with_identity():
    """41 verified facts (one past the old cap): rendered fact IDs must
    equal the verified set one-to-one, each line carrying
    subject/time/ID/evidence identity."""
    facts = []
    for i in range(41):
        f = _fact(f"事実{i}")
        f["fact_id"] = f"fid_{i:03d}"
        f["subject"] = f"patient:{i % 3}"
        f["event_time"] = f"2026-09-{(i % 28) + 1:02d}T10:00"
        f["evidence_ids"] = [f"ev_{i}"]
        facts.append(f)
    out = render.mandatory_render(_doc(facts=facts))
    assert out["fact_ids"] == [f"fid_{i:03d}" for i in range(41)]
    assert len(out["facts"]) == 41
    for i, line in enumerate(out["facts"]):
        assert f"ID:fid_{i:03d}" in line
        assert f"対象:patient:{i % 3}" in line
        assert f"証拠:ev_{i}" in line
    assert out["complete"] is True


def test_mandatory_render_pages_120_facts_within_budget():
    """120 facts split into immutable source-bound pages: every page
    within the char budget, union of page fact_ids == all rendered,
    each page listing exactly the facts its text carries."""
    facts = []
    for i in range(120):
        f = _fact(f"薬剤変更{i}:5mg継続")
        f["fact_id"] = f"fid_{i:03d}"
        f["evidence_ids"] = [f"ev_{i}"]
        facts.append(f)
    out = render.mandatory_render(_doc(facts=facts))
    assert out["complete"] is True
    assert len(out["pages"]) > 1
    seen = []
    for page in out["pages"]:
        assert len(page["text"]) <= render.MANDATORY_PAGE_BUDGET
        for fid in page["fact_ids"]:
            assert f"ID:{fid}" in page["text"]
        seen.extend(page["fact_ids"])
    assert seen == out["fact_ids"]
    assert out["source_fingerprint"] == "sf_x"


def test_verify_mandatory_pages_detects_tampering():
    """Failure oracle: drop fact 41 or bloat a late page past budget —
    publication is incomplete, never PASS."""
    facts = []
    for i in range(60):
        f = _fact(f"事実{i}")
        f["fact_id"] = f"fid_{i:03d}"
        facts.append(f)
    out = render.mandatory_render(_doc(facts=facts))
    assert render.verify_mandatory_pages(out)["complete"] is True

    dropped = json.loads(json.dumps(out))
    victim = dropped["pages"][-1]["fact_ids"].pop()
    res = render.verify_mandatory_pages(dropped)
    assert res["complete"] is False
    assert res["missing"] == [victim]

    bloated = json.loads(json.dumps(out))
    bloated["pages"][-1]["text"] += "x" * render.MANDATORY_PAGE_BUDGET
    res = render.verify_mandatory_pages(bloated)
    assert res["complete"] is False
    assert res["oversized_pages"] == [bloated["pages"][-1]["index"]]


def test_mandatory_render_overview_is_concise_plain_language():
    out = render.mandatory_render(_doc(facts=[
        _fact("薬A"), _fact("薬B"),
        {**_fact("血圧130"), "kind": "vital_lab"},
    ]))
    overview = out["overview"]
    assert "確認済み事実3件" in overview
    assert "薬剤2" in overview and "バイタル・検査1" in overview
    assert len(overview) < 120


def test_mandatory_render_keeps_distinct_facts_with_identical_statements():
    patient = _fact("服用を継続")
    family = {**patient, "fact_id": "fact_family", "subject": "role:family"}
    out = render.mandatory_render(_doc(facts=[patient, family, patient]))
    assert len(out["facts"]) == 2
    assert len(set(out["facts"])) == 2
    assert all(fact["subject"] in "\n".join(out["facts"])
               for fact in (patient, family))


def test_page_id_requires_exact_identity_field():
    out = render.mandatory_render(_doc(facts=[
        {**_fact("synthetic"), "fact_id": "fid_1"}]))
    out["pages"][0]["text"] = out["pages"][0]["text"].replace(
        "ID:fid_1、", "ID:fid_10、")
    result = render.verify_mandatory_pages(out)
    assert not result["complete"]
    assert result["unbound"] == [{"page": 0, "fact_id": "fid_1"}]


def test_mandatory_line_carries_stated_attributes():
    """A reader must not see '739 mg' stripped or a negated fact
    rendered as an assertion: stated attributes ride on the line (and
    its page) while the exact ID/evidence binding stays intact."""
    fact = {**_fact("バルプロ酸"), "fact_id": "fid_q",
            "quantity": "739 mg", "polarity": "negated",
            "epistemic": "reported", "workflow_status": "cancelled",
            "action": "stop", "valid_time": "2026-09-01",
            "actor": "sender:s2", "evidence_ids": ["ev_q"]}
    out = render.mandatory_render(_doc(facts=[fact]))
    line = out["facts"][0]
    for text in ("量:739 mg", "極性:negated", "確度:reported",
                 "状態:cancelled", "行為:stop", "有効時点:2026-09-01",
                 "記録者:sender:s2"):
        assert text in line and text in out["pages"][0]["text"], text
    assert "、ID:fid_q、証拠:ev_q" in line
    assert out["complete"] is True
    assert render.verify_mandatory_pages(out)["complete"] is True


def test_mandatory_line_omits_unknown_attributes():
    fact = {**_fact("アムロジピン"), "fact_id": "fid_u",
            "quantity": "unknown", "action": "unknown",
            "actor": "unknown", "valid_time": "unknown"}
    line = render.mandatory_render(_doc(facts=[fact]))["facts"][0]
    for label in ("量:", "行為:", "記録者:", "有効時点:"):
        assert label not in line, label
