"""v1.0.26 sweep: patient-context view regressions (fully synthetic dict evidence)."""
import patient_context_view as pcv

T0 = 1_790_000_000
DAY = 86400


def _item(mid, category, text, ts=None):
    return {"category": category, "text": text, "quote": text, "subject": "patient", "details": [],
            "message_id": mid, "posted_at_ts": T0 + mid * DAY if ts is None else ts, "source": "chat"}


def test_mention_groups_have_japanese_headings_and_a_fixed_rank():
    assert pcv.SECTION_LABELS["medications"][0] == "薬剤の記載"
    assert pcv.SECTION_LABELS["symptoms"][0] == "症状の記載"
    view = {"sections": [{"category": c, "items": []} for c in ("symptoms", "medications", "followup",
                                                                 "medication_management", "diagnoses")]}
    order = [s["category"] for s in pcv.role_order(view, "")["sections"]]
    assert order == ["diagnoses", "medication_management", "medications", "symptoms", "followup"]


def test_transition_anchors_once_per_post():
    ev = {"items": [_item(6, "course", "架空病院から退院"), _item(6, "followup", "退院後の架空薬の残数"),
                    _item(5, "adl", "歩行は介助"), _item(1, "diagnoses", "架空病A")]}
    course = pcv.transition_course(ev)["items"]
    assert len(course) == 1
    group = course[0]
    assert group["anchor"]["text"] == "架空病院から退院"
    assert [a["text"] for a in group["anchors"]] == ["架空病院から退院", "退院後の架空薬の残数"]
    assert sorted(i["message_id"] for i in group["nearby"]) == [1, 5]
    # the anchor post's own followup counts as written in the window
    assert group["no_record"] == ["care_services", "medication_management"]


def test_provider_not_posting_checks_each_written_provider():
    ev = {"items": [_item(1, "care_team", "主治医: 架空クリニックA、訪問看護: 架空ステーションB"),
                    _item(2, "care_team", "主治医: 架空クリニックA")]}
    out = pcv.discrepancies(ev, posters=[{"organization": "架空クリニックA"}])["items"]
    flagged = [g for g in out if g["kind"] == "provider_not_posting"]
    assert [g["records"][0]["value"] for g in flagged] == ["主治医: 架空クリニックA、訪問看護: 架空ステーションB"]
