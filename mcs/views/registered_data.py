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


def registered_data(db, scope, project_id, limit):
    """Read optional registered clinical data separately from chat and memo reports."""
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
          AND json_extract(a.content,'$.entity_id')=p.karte_id
          AND json_type(a.content,'$.item_id')='integer'
          AND typeof(json_extract(a.content,'$.item_id'))='integer'
          AND json_extract(a.content,'$.item_id')>0 ELSE 0 END
        ) """
    total = db.execute(sql + "SELECT COUNT(*) FROM targets", params).fetchone()[0]
    query = sql + "SELECT * FROM targets ORDER BY project_id,dataset,item_id"
    if limit is not None:
        query += " LIMIT ?"
        params = [*params, limit]
    now = snapshot_meta(db)["generated_at"]
    records = []
    for target in db.execute(query, params):
        result = get_project_metadata(db, target["project_id"], target["dataset"],
                                      item_id=target["item_id"], now=now)
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
