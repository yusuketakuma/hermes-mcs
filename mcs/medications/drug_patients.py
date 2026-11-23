"""公開snapshotから、指定した薬の言及がある患者を横断して一覧にする。"""
from __future__ import annotations

import drug_timeline
from mcs_queries import incomplete_reply_roots

MAX_IDENTITIES = 1000
MAX_NAME = 120


def _latest(found, row, med, change, source):
    at = row["posted_at_ts"]
    key = (at if isinstance(at, int) else -1, row["message_id"] or row.get("parent_message_id") or 0)
    if found["latest"] is None or key > found["_key"]:
        found["_key"] = key
        found["latest"] = {"posted_at": row["posted_at"], "posted_at_ts": at,
                           "message_id": row["message_id"] or None,
                           "parent_message_id": row.get("parent_message_id"),
                           "attachment_id": row.get("attachment_id"), "page": row.get("page"),
                           "source": source, "name": med["name"], "change": change["description"],
                           "time_scope": change["time_scope"]}


def _strings(values, *, max_len=None):
    return (isinstance(values, (set, frozenset, list, tuple)) and len(values) <= MAX_IDENTITIES
            and all(isinstance(v, str) and v and (max_len is None or len(v) <= max_len) for v in values))


def find(view, *, name=None, identities=None, names=(), cfg=None, limit=50, include_archived=False):
    """List patients with a current mention of the drug; mentions are never confirmed current use.

    ``identities`` may come with exact ``names`` (e.g. revised insert names) that also match
    when no dictionary identity covers them."""
    if (name is None) == (identities is None) or (name is not None and names):
        raise ValueError("bad_query")
    if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME):
        raise ValueError("bad_name")
    if identities is not None and not _strings(identities):
        raise ValueError("bad_identities")
    if not _strings(names, max_len=MAX_NAME):
        raise ValueError("bad_names")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("bad_limit")
    dictionary, progress, active, active_state = drug_timeline.dictionary_state(view.db, cfg)
    codes = frozenset(identities) if identities is not None else frozenset(
        drug_timeline.query_identities(active, name))
    exact = (name,) if name is not None else tuple(sorted(set(names)))
    patients = view.db.execute(
        "SELECT project_id,patient_name,fetch_state,history_floor,COALESCE(is_archived,0) AS archived "
        "FROM patients ORDER BY project_id").fetchall()
    found_items, archived_skipped = [], 0
    totals = {"stored_posts": 0, "incomplete_bodies": 0, "no_current_extraction": 0,
              "patients_with_unextracted_posts": 0, "patients_history_incomplete": 0,
              "patients_fetch_failed": 0, "incomplete_reply_roots": 0, "failed_fetch_jobs": 0}
    for patient in patients:
        if patient["archived"] and not include_archived:
            archived_skipped += 1
            continue
        pid = patient["project_id"]
        rows = [dict(r) for r in view.db.execute(
            "SELECT message_id,project_id,posted_at,posted_at_ts,body_state,content_hash FROM messages "
            "WHERE project_id=? AND body_state IS NOT 'deleted' ORDER BY message_id", (pid,))]
        found = {"project_id": pid, "patient_name": patient["patient_name"], "archived": bool(patient["archived"]),
                 "mentions": 0, "posts": set(), "latest": None, "_key": None}
        seen_sources = set()
        posts = {r["message_id"]: r for r in rows}
        for row, source, meds, refs, original in drug_timeline.mention_sources(
                view, pid, rows, dictionary=dictionary, progress=progress, cfg=cfg):
            if original is None:
                seen_sources.add(row["message_id"])
            else:
                # An attachment page is dated by its parent post, as in the timeline.
                parent = posts.get(row["parent_message_id"]) or {}
                row = {**row, "posted_at": parent.get("posted_at"), "posted_at_ts": parent.get("posted_at_ts")}
            for i, med in enumerate(meds):
                ref, known = drug_timeline.mention_candidate(
                    med, refs.get(i) if source[1] == "extract_llm" else None, active)
                if not known or not drug_timeline.matches(med, ref, names=exact, identities=codes):
                    continue
                change = drug_timeline.change_details(med, "unknown", ref)
                found["mentions"] += 1
                found["posts"].add(row["message_id"] or row.get("parent_message_id"))
                _latest(found, row, med, change, "attachment_ocr" if original is not None else "post")
        full = sum(r["body_state"] == "full" for r in rows)
        missing = full - len(seen_sources)
        totals["stored_posts"] += len(rows)
        totals["incomplete_bodies"] += len(rows) - full
        totals["no_current_extraction"] += missing
        totals["patients_with_unextracted_posts"] += missing > 0
        totals["patients_history_incomplete"] += patient["history_floor"] != -1
        totals["patients_fetch_failed"] += patient["fetch_state"] == "failed"
        totals["incomplete_reply_roots"] += incomplete_reply_roots(view.db, pid)
        totals["failed_fetch_jobs"] += view.db.execute(
            "SELECT count(*) FROM fetch_jobs WHERE project_id=? AND state='failed'", (pid,)).fetchone()[0]
        if found["mentions"]:
            found["posts"] = len(found["posts"])
            del found["_key"]
            found["current_use_confirmed"] = False
            found_items.append(found)
    found_items.sort(key=lambda f: ((f["latest"]["posted_at_ts"] or -1), f["project_id"]), reverse=True)
    states = []
    if totals["no_current_extraction"]:
        states.append(f"未抽出{totals['no_current_extraction']}投稿")
    if totals["incomplete_bodies"] or totals["patients_history_incomplete"] or totals["incomplete_reply_roots"]:
        states.append("未取得あり")
    if totals["patients_fetch_failed"] or totals["failed_fetch_jobs"]:
        states.append("取得失敗あり")
    if dictionary in ("pending", "invalid", "unconfigured", "reference_only") and not codes and identities is None:
        states.append("名称の完全一致のみ")
    if identities is not None and active is None:
        states.append("有効な辞書がなく品名の完全一致のみ" if exact else "有効な辞書がなく照合できません")
    return {"kind": "drug_patients", "read_only": True, "candidate_only": True,
            "items": found_items[:limit], "total": len(found_items), "returned": min(limit, len(found_items)),
            "truncated": len(found_items) > limit,
            "query": {"name": name, "names": list(exact), "identities": sorted(codes),
                      "name_matching": "exact_or_identity" if codes else "exact_only"},
            "coverage": {**totals, "patients_scanned": len(patients) - archived_skipped,
                         "archived_skipped": archived_skipped, "dictionary": dictionary,
                         "active_dictionary_state": active_state, "snapshot": view.meta,
                         "gapless_verified": False},
            "explanation": "言及のある患者（服用中の確定ではありません"
                           + ("・" + "・".join(states) if states else "") + "）"}


def master_identities(active, report):
    """Dictionary identities whose official source codes appear in a master-impact report."""
    items = report.get("items") if isinstance(report, dict) else None
    if not isinstance(items, list) or report.get("schema") != "mcs-drug-master-impact/1":
        raise ValueError("bad_master_impact")
    changed = {item.get("medicine") for item in items if isinstance(item, dict)}
    if active is None:
        return set()
    return {code for code, entry in active.entries.items() if changed & set(entry.source_codes)}


def formulary_identities(active, watch):
    """Identities and exact names of watched inserts whose observed text differs from the accepted one.

    Same rule as the revision notice: a later failed re-check keeps the observed revision.
    Names find() cannot take (over 120 characters, or beyond its bounds) are counted, not raised."""
    names = set()
    for item in watch.get("inserts", []):
        observed = item.get("observed_sha256") if isinstance(item, dict) else None
        if observed and observed != item.get("sha256"):
            names.update(n for n in [item.get("name"), *item.get("aliases", [])] if isinstance(n, str) and n)
    codes = set()
    for name in sorted(names):
        codes |= drug_timeline.query_identities(active, name)
    usable = sorted(n for n in names if len(n) <= MAX_NAME)[:MAX_IDENTITIES]
    return set(sorted(codes)[:MAX_IDENTITIES]), usable, {
        "names_not_matched": len(names) - len(usable), "identities_truncated": len(codes) > MAX_IDENTITIES}
