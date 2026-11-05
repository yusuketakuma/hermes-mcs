"""公開snapshotから世代と取得範囲を示す薬剤言及履歴を読む。"""
from __future__ import annotations
import semantic_transport


import drug_map
import extract_quality
from mcs_queries import incomplete_reply_roots
from mcs_util import loads_dict


def mentions(reader, mid, *, cfg=None):
    """Return all mention classes from the current generation, without rule fallback."""
    db = reader.db
    source = drug_map._source(db, mid)
    if source is None or source[1] == "extract_v1":
        return None
    row = db.execute("SELECT meta,model FROM artifacts WHERE artifact_id=?", (source[0],)).fetchone()
    meta = loads_dict(row[0]) or {}
    if source[1] == "extract_llm":
        from extract_llm import (EXTRACT_VERSION, PATIENT_CONTEXT_VERSION, _chunk_context, _thread_context, resolved_model,
                                 _thin_pending_sql, _qc_flagged_sql, _human_flagged_sql)
        from clinical_chunking import PLAN_VERSION
        if (meta.get("extract_version") != EXTRACT_VERSION
                or meta.get("plan_version") != PLAN_VERSION
                or meta.get("patient_context_version") != PATIENT_CONTEXT_VERSION
                or meta.get("actual_model") != resolved_model(cfg or {})
                or row[1] != resolved_model(cfg or {})):
            return None
        from inference_dependency_loss import (SOURCE_BINDING_COLUMNS, source_binding,
                                              source_binding_from_row)
        if not isinstance(meta.get("source_binding"), str):
            return None
        message = dict(db.execute("SELECT m.*," + SOURCE_BINDING_COLUMNS
                                  + " FROM messages m WHERE m.message_id=?", (mid,)).fetchone())
        binding = source_binding_from_row(message)
        if binding is None:
            # Preserve the single reader's handling of legacy noninteger revisions.
            binding = source_binding(db, message["project_id"], mid)
        if binding is None or binding != meta.get("source_binding"):
            return None
        message["_source_binding"] = binding
        message["_target"] = ("", resolved_model(cfg or {}))
        review = db.execute(f"SELECT ({_thin_pending_sql(db)}) OR ({_qc_flagged_sql()}) OR ({_human_flagged_sql()}) FROM messages m WHERE message_id=?", (mid,)).fetchone()
        if (review and review[0]) or any(isinstance(meta.get(k), dict) and meta[k].get("applied") is False for k in ("qc_fix", "human_fix")):
            return None
        import extraction_context
        frame = extraction_context.load(db, meta.get("context_snapshot"), message)
        if meta.get("context_snapshot") is not None:
            if frame is None or frame["digest"] != meta.get("context_digest") or frame["enabled"] != meta.get("patient_state_context"):
                return None
            message["_frame"] = frame
            context = frame["text"]
        elif meta.get("patient_state_context"):
            return None
        else:
            context = _thread_context(reader.db, message)
        if meta.get("context_binding") != _chunk_context(message, context):
            return None
        if not extract_quality.completeness_resolved(meta):
            return None
        doc = loads_dict(source[2]) or {}
        meds = doc.get("meds")
        if not isinstance(meds, list) or any(not isinstance(m, dict) or
                not isinstance(m.get("name"), str) for m in meds):
            return None
        return source, meds
    from semantic_publication import reproject_doc
    from semantic_store import thread_bundle
    from semantic_policy import runtime_config, policy_fingerprint
    message = db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone()
    scfg, errors = runtime_config(cfg or {})
    bundle = thread_bundle(reader.db, message["project_id"], message["parent_id"] or mid,
                           local_model=semantic_transport.llm_model(cfg or {}))
    if errors or bundle is None or bundle["source_fingerprint"] != meta.get("fingerprint") or policy_fingerprint(scfg) != meta.get("policy_fingerprint"):
        return None
    doc, error = reproject_doc(reader.db, mid, meta)
    if error:
        return None
    return source, [_canonical_mention(fact, doc) for fact in doc.get("facts", [])
                    if fact.get("kind") in ("medication_event", "medication_exposure")]


def _canonical_mention(fact, doc):
    """Use only drug names grounded in this fact's own evidence, retaining unknown names."""
    quotes = [e["quote"] for e in doc.get("evidence", [])
              if e.get("evidence_id") in fact.get("evidence_ids", [])
              and isinstance(e.get("quote"), str)]
    context = fact.get("patient_context") or {}
    names = []
    for detail in context.get("details", []):
        name, evidence = detail.get("value"), detail.get("evidence")
        if (detail.get("key") == "drug_name" and isinstance(name, str) and name.strip()
                and isinstance(evidence, str) and name in evidence
                and any(evidence in quote for quote in quotes) and name not in names):
            names.append(name)
    known = len(names) == 1
    return {**fact, "name": names[0] if known else "薬名不明",
            "name_state": "grounded" if known else "ambiguous" if names else "unknown",
            "name_candidates": names, "evidence_quotes": quotes}


def change_details(med, subject, candidate):
    """Keep extraction assertions separate from completed treatment or prescribing authority."""
    workflow = med.get("workflow_status")
    temporal = med.get("status") if med.get("status") in ("past", "current", "planned") else "unknown"
    planned = temporal == "planned" or workflow in ("planned", "ordered", "considering", "pending", "on_hold")
    polarity = med.get("polarity", "negated" if med.get("negated") is True else
                       "affirmed" if med.get("negated") is False else "unknown")
    epistemic = med.get("epistemic", "unknown")
    performed = (workflow in ("performed", "done") and polarity == "affirmed"
                 and epistemic in ("asserted", "reported"))
    action = med.get("action", "unknown")
    codes = [dict(c) for c in (candidate or {}).get("cands", [])]
    return {"action": action, "subject": subject, "time_scope": temporal,
            "event_time": med.get("event_time"),
            "implementation": "planned" if planned else "performed_report" if performed else "not_established",
            "polarity": polarity, "workflow_status": workflow, "epistemic": epistemic,
            "identity_candidates": codes, "prescription_authority": False,
            "current_use_confirmed": False,
            "description": {"start": "開始", "stop": "中止", "increase": "増量",
                "decrease": "減量", "change": "変更"}.get(action, "その他・不明")
                + ("予定" if planned else "の記載")}


def _groups(items):
    groups = {}
    for index, item in enumerate(items):
        ref = item["candidate"] or {}
        candidates = ref.get("cands", [])
        # Ambiguous candidates never merge separate mentions into one treatment.
        code = (candidates[0] if ref.get("status") == "resolved" and len(candidates) == 1
                and candidates[0]["kind"] in ("general_name", "product") else None)
        key = (code["system"], code["code"]) if code else ("mention", index)
        group = groups.setdefault(key, {"identity": code, "item_indices": [],
            "candidate_only": True, "scope": "page", "current_use_confirmed": False})
        group["item_indices"].append(index)
    return list(groups.values())


def timeline(view, project_id, *, name=None, since=None, until=None, limit=50, cursor=None, cfg=None):
    """Page stored posts, preserving every medication mention and explicit coverage gaps."""
    if type(project_id) is not int or project_id <= 0:
        raise ValueError("bad_project")
    if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 120):
        raise ValueError("bad_name")
    sql = "SELECT m.message_id,m.project_id,m.posted_at,m.posted_at_ts,m.body_state,m.content_hash FROM messages m WHERE m.project_id=? AND m.body_state IS NOT 'deleted'"
    deleted_sql = "SELECT COUNT(*) FROM messages m WHERE m.project_id=? AND m.body_state='deleted'"
    params = [project_id]
    for value, operator in ((since, ">="), (until, "<=")):
        if value is not None:
            if type(value) is not int or value < 0:
                raise ValueError("bad_period")
            sql += f" AND m.posted_at_ts {operator} ?"
            deleted_sql += f" AND m.posted_at_ts {operator} ?"
            params.append(value)
    if since is not None and until is not None and since > until:
        raise ValueError("bad_period")
    all_rows = view.db.execute(sql, params).fetchall()
    incomplete = sum(r["body_state"] != "full" for r in all_rows)
    withdrawn = view.db.execute(deleted_sql, params).fetchone()[0]
    # Unknown times are included in withdrawn only when no period is requested.
    withdrawn_time_unknown = view.db.execute(
        "SELECT COUNT(*) FROM messages WHERE project_id=? AND body_state='deleted' "
        "AND posted_at_ts IS NULL", (project_id,)).fetchone()[0]
    patient = view.db.execute("SELECT fetch_state,history_floor FROM patients WHERE project_id=?",
                              (project_id,)).fetchone()
    if patient is None:
        raise ValueError("unknown_project")
    failed = view.db.execute("SELECT count(*) FROM fetch_jobs WHERE project_id=? AND state='failed'",
                             (project_id,)).fetchone()[0]
    progress = drug_map.read_annotation_progress(view.db)
    dictionary = "invalid" if progress and progress.get("invalid") else (
        "unconfigured" if not progress or not progress.get("dictionary") else "derived")
    pin = (cfg or {}).get("drug_map")
    if pin is not None and drug_map.config_error(pin) is not None:
        dictionary = "invalid"
    elif pin is not None and (not progress or not progress.get("dictionary")
                              or progress["dictionary"][1] != pin["sha256"]):
        dictionary = "pending"
    active, active_state = drug_map.active_dictionary(view.db, cfg or {})
    query_codes = set()
    if name is not None and active is not None:
        related = active.related_names(name)
        if related["status"] == "resolved" and related["official_identity"]:
            query_codes = set(related["identities"])
    from mcs_queries import UNKNOWN_TIME
    page = view._page(sql, params, [f"COALESCE(m.posted_at_ts,{UNKNOWN_TIME})", "m.message_id"],
                      ["drug_timeline", project_id, name, since, until], limit, cursor)
    # One read per stored full post, in stored order, after the page is known so only
    # page rows keep their payload; a cached None (unextracted) is a hit, not a miss.
    on_page = {row["message_id"] for row in page["items"]}
    memo, missing = {}, 0
    for r in all_rows:
        if r["body_state"] == "full":
            result = mentions(view.reader, r["message_id"], cfg=cfg)
            missing += result is None
            if r["message_id"] in on_page:
                memo[r["message_id"]] = result
    items = []
    matching_pending = 0
    for row in page["items"]:
        mid = row["message_id"]
        result = (None if row["body_state"] != "full" else memo[mid] if mid in memo
                  else mentions(view.reader, mid, cfg=cfg))
        if result is None:
            continue
        source, meds = result
        refs = ({r["i"]: r for r in drug_map.current_refs_with_progress(view.db, row["message_id"], progress)}
                if dictionary == "derived" else {})
        for i, med in enumerate(meds):
            ref = refs.get(i) if source[1] == "extract_llm" else None
            known_name = med.get("name_state") not in ("unknown", "ambiguous")
            if ref is None and active is not None and known_name:
                ref = {**active.resolve(med["name"]), "dict_sha256": active.sha256,
                       "resolver_version": drug_map.RESOLVER_VERSION, "resolved_on_read": True}
            if name is not None and not known_name:
                continue
            if name is not None and drug_map.fold(name) != drug_map.fold(med["name"]):
                codes = {c["code"] for c in (ref or {}).get("cands", [])}
                if (ref or {}).get("status") != "resolved" or not (query_codes & codes):
                    continue
            if ref is None:
                matching_pending += 1
            subject = med.get("subject", "unknown")
            if subject == f"patient:{project_id}":
                subject = "patient"
            elif subject == "role:family":
                subject = "family"
            elif subject not in ("patient", "family", "other"):
                subject = "unknown"
            items.append({**row, "mention": med, "subject_bucket": subject,
                          "change": change_details(med, subject, ref),
                          "source_kind": source[1], "source_artifact_id": source[0],
                          "candidate": ref, "matching": "current" if ref else "pending",
                          "source": {"project_id": project_id, "message_id": row["message_id"]}})
    replies = incomplete_reply_roots(view.db, project_id)
    coverage = {"period": {"since": since, "until": until},
                "stored_posts": len(all_rows), "incomplete_bodies": incomplete,
                "withdrawn_posts": withdrawn,
                "withdrawn_time_unknown": withdrawn_time_unknown,
                "incomplete_reply_roots": replies, "no_current_extraction": missing,
                "failed_fetch_jobs": failed, "history_complete": patient[1] == -1,
                "attachments": "excluded_ocr_not_performed", "dictionary": dictionary,
                "dictionary_generation": progress.get("dictionary") if progress and not progress.get("invalid") else None,
                "name": name, "name_matching": "exact_or_single_identity" if query_codes else "exact_only",
                "active_dictionary_state": active_state,
                "matching_pending_on_page": matching_pending,
                "snapshot": view.meta, "gapless_verified": False}
    if (cfg or {}).get('attachment_ocr', {}).get('enabled', False):
        import mcs_queries
        coverage['attachments'] = 'excluded_from_medication_timeline'
        coverage['attachment_processing'] = mcs_queries.attachment_ocr_coverage(view.db, project_id, cfg)
    states = []
    if withdrawn:
        states.append("削除された投稿あり")
    if withdrawn_time_unknown:
        states.append("削除された投稿の時刻不明")
    if incomplete or replies or patient[1] != -1:
        states.append("未取得")
    if missing:
        states.append("未抽出")
    if failed or patient[0] == "failed":
        states.append("取得失敗")
    if dictionary != "derived" or matching_pending:
        states.append("照合中")
    return {"kind": "drug_timeline", "items": items, "drug_groups": _groups(items),
            "next_cursor": page["next_cursor"],
            "coverage": coverage, "candidate_only": True, "read_only": True,
            "explanation": "薬剤言及履歴（" + "・".join(states or ["保存済み本文の範囲"]) + "）"}
