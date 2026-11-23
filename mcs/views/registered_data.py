"""Project registered clinical data from the caller’s borrowed snapshot connection."""

from snapshot_access import snapshot_meta, table_exists


def field_counts(rows):
    """Count allowlisted registration fields without exporting their values."""
    counts = {}

    def visit(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, path + "." + key if path else key)
        elif isinstance(value, list):
            for item in value:
                visit(item, path)
        elif value is not None:
            counts[path] = counts.get(path, 0) + 1

    for row in rows:
        visit(row, "")
    return [{"key": key, "count": count} for key, count in sorted(counts.items())]


VALUE_KEYS = ("scalar", "max", "min", "left", "right")


def first_valued_row(rows):
    """The first stored observation-value row that carries a measurement, or None when every row's
    measurements are missing (such rows are kept as received, so the first row alone does not decide)."""
    return next((row for row in rows or () if isinstance(row, dict)
                 and any(row.get(key) is not None for key in VALUE_KEYS)), None)


def _with_unfetched_values(db, targets, now):
    """Add an observation_values target for every item of the validated observation-item list that has no
    stored values attempt, so an item the rotation has not reached reads 未取得 (state unknown, reason
    not_fetched) instead of being missing; never a fact that the item has no values."""
    from project_metadata_view import get_project_metadata

    fetched = {(t["project_id"], t["item_id"]) for t in targets if t["dataset"] == "observation_values"}
    added = []
    for target in targets:
        if target["dataset"] != "observation_items":
            continue
        target["_result"] = get_project_metadata(db, target["project_id"], "observation_items", now=now)
        for row in target["_result"]["rows"]:
            lab = row.get("lab_test_item") if isinstance(row, dict) else None
            item_id = lab.get("id") if isinstance(lab, dict) else None
            if type(item_id) is not int or item_id <= 0 or (target["project_id"], item_id) in fetched:
                continue
            fetched.add((target["project_id"], item_id))
            added.append({"project_id": target["project_id"], "dataset": "observation_values",
                          "item_id": item_id, "_item_row": row})
    return sorted(targets + added, key=lambda t: (t["project_id"], t["dataset"], t["item_id"] or 0))


def registered_data(db, scope, project_id, limit, *, unfetched_items=False):
    """Read optional registered clinical data separately from chat and memo reports.

    ``unfetched_items`` (for a reader that shows each item's state) also lists every item of the validated
    observation-item list whose values were never attempted, as 未取得; without it the records are the
    stored targets only, which the rollup freshness signature hashes."""
    if not table_exists(db, "patients") or not {"project_type", "karte_id"} <= {
            row[1] for row in db.execute("PRAGMA table_info(patients)")}:
        return None
    from project_metadata_view import get_project_metadata

    params = [project_id] if project_id is not None else []
    sql = """WITH clinical AS (
        SELECT project_id,karte_id FROM patients WHERE project_type='medical'
        AND typeof(karte_id)='integer' AND karte_id>0""" + (
        " AND project_id=?" if project_id is not None else "") + """
        ), targets AS (
        SELECT project_id,'medication_periods' AS dataset,NULL AS item_id FROM clinical
        UNION ALL SELECT project_id,'observation_items',NULL FROM clinical
        UNION ALL SELECT DISTINCT p.project_id,'observation_values',json_extract(a.content,'$.item_id')
        FROM clinical p JOIN artifacts a ON a.project_id=p.project_id
        WHERE a.kind='project_metadata_v1' AND CASE WHEN json_valid(a.content) THEN
          json_extract(a.content,'$.dataset')='observation_values'
          AND json_type(a.content,'$.entity_id')='integer'
          AND typeof(json_extract(a.content,'$.entity_id'))='integer'
          AND json_extract(a.content,'$.entity_id')=p.karte_id
          AND json_type(a.content,'$.item_id')='integer'
          AND typeof(json_extract(a.content,'$.item_id'))='integer'
          AND json_extract(a.content,'$.item_id')>0 ELSE 0 END
        ) """
    now = snapshot_meta(db)["generated_at"]
    query = sql + "SELECT * FROM targets ORDER BY project_id,dataset,item_id"
    if unfetched_items:
        targets = _with_unfetched_values(db, [dict(row) for row in db.execute(query, params)], now)
        total = len(targets)
        targets = targets if limit is None else targets[:limit]
    else:
        total = db.execute(sql + "SELECT COUNT(*) FROM targets", params).fetchone()[0]
        targets = [dict(row) for row in db.execute(query + (" LIMIT ?" if limit is not None else ""),
                                                   [*params, limit] if limit is not None else params)]
    records = []
    for target in targets:
        result = target.pop("_result", None) or get_project_metadata(
            db, target["project_id"], target["dataset"], item_id=target["item_id"], now=now)
        if result["definition"] is None and target.get("_item_row") is not None:
            # no values attempt carries a definition yet: the validated item list names the item
            result = {**result, "definition": target["_item_row"]}
        record = {"project_id": target["project_id"], "dataset": target["dataset"],
                  "source": "mcs_structured", "state": result["state"],
                  "rows_total": len(result["rows"]),
                  "rows_truncated": limit is not None and len(result["rows"]) > limit}
        if scope == "detail":
            record.update({key: result[key] for key in (
                "reason", "scope", "last_complete_at", "attempted_at", "age_s", "current_known",
                "historical", "stale", "definition", "http_status", "chat_comparison",
                "definition_binding", "definition_source_artifact_id", "definition_source",
                "attempt_reason") if key in result})
            record["item_id"] = target["item_id"]
            record["rows"] = result["rows"] if limit is None else result["rows"][:limit]
        else:
            record["fields"] = field_counts(result["rows"])
            record["definition_fields"] = field_counts([result["definition"]] if result["definition"] else [])
        records.append(record)
    return {"records": records, "total": total, "truncated": limit is not None and total > limit,
            "absence_confirmed": False, "clinical_state": "not_inferred"}
