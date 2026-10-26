"""保存済みの構造化薬歴と投稿の薬剤言及を確認候補として照合する。"""
from __future__ import annotations

import drug_map
import drug_timeline
from project_metadata_view import get_project_metadata


def _identity(dictionary, name):
    if dictionary is None or not isinstance(name, str) or not name.strip():
        return set()
    ref = dictionary.resolve(name)
    return {c["code"] for c in ref["cands"] if c["kind"] in ("general_name", "product")} if ref["status"] == "resolved" else set()


def reconcile(view, project_id, *, since=None, until=None, limit=50, cursor=None, cfg=None):
    """Compare one stored-post page, retaining acquisition dates and non-comparable mentions."""
    timeline = drug_timeline.timeline(view, project_id, since=since, until=until,
                                     limit=limit, cursor=cursor, cfg=cfg)
    structured = get_project_metadata(view.db, project_id, "medication_periods")
    dictionary, dictionary_state = drug_map.active_dictionary(view.db, cfg or {})
    medicines = []
    for period in structured["rows"]:
        for medicine in period["medicine_informations"]:
            medicines.append({"id": medicine["id"], "name": medicine["name"],
                              "begin_date": period["begin_date"], "end_date": period["end_date"],
                              "information_acquired_at": structured["last_complete_at"]})
    unknown_names = sum(not isinstance(m["name"], str) or not m["name"].strip() for m in medicines)
    # Per-structured-name values do not depend on the mention; compute them once.
    references = [(medicine, drug_map.fold(medicine["name"]),
                   drug_map.split_surface(medicine["name"])["base"],
                   _identity(dictionary, medicine["name"]))
                  for medicine in medicines
                  if isinstance(medicine["name"], str) and medicine["name"].strip()]
    candidates = []
    for item in timeline["items"]:
        med = item["mention"]
        change = item["change"]
        context = []
        if item["subject_bucket"] != "patient":
            context.append("subject_not_patient" if item["subject_bucket"] in ("family", "other")
                           else "subject_unknown")
        if change["time_scope"] == "past":
            context.append("historical_mention")
        if change["implementation"] == "planned":
            context.append("planned_mention")
        if change["polarity"] != "affirmed":
            context.append("polarity_not_affirmed")
        if not structured["current_known"]:
            context.append("structured_not_current")
        base = {"message_id": item["message_id"], "source_artifact_id": item["source_artifact_id"],
                "posted_at": item["posted_at"], "mention": med, "change": change,
                "context": context, "candidate_only": True, "absence_confirmed": False,
                "current_use_confirmed": False, "prescription_authority": False}
        if item["subject_bucket"] != "patient":
            candidates.append({**base, "status": "not_comparable", "reason": context[0], "comparisons": []})
            continue
        if med.get("name_state") in ("unknown", "ambiguous"):
            candidates.append({**base, "status": "unknown", "reason": "mention_name_not_established", "comparisons": []})
            continue
        if structured["last_complete_at"] is None:
            candidates.append({**base, "status": "unknown", "reason": "structured_not_observed", "comparisons": []})
            continue
        name = med["name"]
        codes = _identity(dictionary, name)
        surface = drug_map.split_surface(name)
        folded = drug_map.fold(name)
        comparisons = []
        for medicine, reference_fold, reference_base, other_codes in references:
            reference = medicine["name"]
            # A shared textual base permits surfacing a strength/form discrepancy;
            # it does not establish official-code identity or clinical equivalence.
            same_text = folded == reference_fold
            same_base = bool(surface["base"]) and surface["base"] == reference_base
            same_code = bool(codes & other_codes)
            if not (same_text or same_base or same_code):
                continue
            comparison = drug_map.compare_surfaces(name, reference)
            comparisons.append({"structured": medicine, "comparison": comparison,
                                "match_basis": "single_identity_candidate" if same_code else
                                    "exact_surface" if same_text else "textual_base_only"})
        if not comparisons and unknown_names:
            status, reason = "unknown", "structured_names_missing"
        elif not comparisons:
            status, reason = "review", "name_not_observed_in_structured_snapshot"
        elif any(c["comparison"]["status"] == "review" for c in comparisons):
            status, reason = "review", "surface_difference"
        elif any(c["comparison"]["status"] == "unknown" for c in comparisons):
            status, reason = "unknown", "comparison_fields_missing"
        else:
            status, reason = "no_difference_observed", "surface_only"
        candidates.append({**base, "status": status, "reason": reason, "comparisons": comparisons})
    return {"kind": "drug_reconciliation", "items": candidates,
            "next_cursor": timeline["next_cursor"], "coverage": timeline["coverage"],
            "structured": {**structured, "comparison_scope": "all_stored_periods",
                           "prescription_authority": False, "missing_names": unknown_names},
            "dictionary_state": dictionary_state, "candidate_only": True, "read_only": True,
            "absence_confirmed": False, "current_use_confirmed": False,
            "explanation": ("薬歴と投稿の照合候補（情報時点・対象者・予定/過去と取得範囲を併記）"
                            + ("・削除された投稿あり" if timeline["coverage"]["withdrawn_posts"] else "")
                            + ("・削除された投稿の時刻不明" if timeline["coverage"]["withdrawn_time_unknown"] else "")),
            "comparison_scope": "stored_post_page", "clinical_equivalence_confirmed": False}
