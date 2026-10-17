"""固定した公式マスターの版間差分と採用辞書への影響を読取り専用で示す。"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import drug_map
from import_drug_master import catalog


def compare(before_path, before_pin, after_path, after_pin, *, adopted=None, limit=100):
    """Compare every medicine code, retaining retired rows and ambiguous adoption evidence."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("drug_master_compare_limit")
    if adopted is not None and not isinstance(adopted, drug_map.DrugMap):
        raise ValueError("drug_master_compare_dictionary")
    before, after = catalog(Path(before_path), before_pin), catalog(Path(after_path), after_pin)
    adopted_codes = set()
    missing_metadata = False
    if adopted is not None:
        entries = {e.id: e for hits in adopted.aliases.values() for e in hits}
        for entry in entries.values():
            adopted_codes.update(entry.source_codes)
            missing_metadata |= not bool(entry.source_codes)
    counts, items, total, affected = Counter(), [], 0, 0
    old_rows, new_rows = before["rows"], after["rows"]
    fields = ("name", "unit", "form_code", "change", "abolished_on",
              "transition_until", "general_code", "general_name")
    for code in sorted(old_rows.keys() | new_rows.keys()):
        old, new = old_rows.get(code), new_rows.get(code)
        changed = [f for f in fields if old[f] != new[f]] if old and new else []
        if old and new and not changed:
            continue
        reasons = []
        if old is None:
            reasons.append("added")
        elif new is None:
            reasons.append("missing_from_new_master")
        else:
            if "name" in changed or "general_name" in changed:
                reasons.append("name_changed")
            if "abolished_on" in changed or ("change" in changed and new["change"] in ("1", "9")):
                reasons.append("retirement_review")
            if "transition_until" in changed:
                reasons.append("transition_deadline_changed")
            if any(f in changed for f in ("unit", "form_code", "general_code")):
                reasons.append("specification_or_identity_changed")
            if not reasons:
                reasons.append("master_field_changed")
        adoption = ("matched" if code in adopted_codes else
                    "unknown" if adopted is None or missing_metadata else "not_matched")
        affected += adoption == "matched"
        counts.update(reasons)
        total += 1
        if len(items) < limit:
            items.append({"medicine": code, "reasons": reasons, "changed_fields": changed,
                          "before": old, "after": new, "adoption": adoption,
                          "requires_review": True})
    return {"schema": "mcs-drug-master-impact/1", "read_only": True,
            "candidate_only": True, "activation": False,
            "before": {k: before[k] for k in ("sha256", "edition", "layout")},
            "after": {k: after[k] for k in ("sha256", "edition", "layout")},
            "adopted_dictionary_sha256": adopted.sha256 if adopted else None,
            "adoption_coverage": "unknown" if adopted is None else "partial" if missing_metadata else "complete",
            "total": total, "returned": len(items), "limit": limit,
            "truncated": total > len(items), "counts": dict(counts),
            "adopted_affected_count": affected, "items": items}
