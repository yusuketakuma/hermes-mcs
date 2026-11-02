"""Freeze count-only initial-history snapshots on the existing card ledger."""
from __future__ import annotations

import json

import clinical_urgency
import extraction_progress
import initial_import_state
from mcs_contract import project_url
from mcs_requests import payload_hash
from mcs_util import loads_dict
from notify_render import project_identity

UPDATE_SECONDS = 300


def _patient_identity(db, project_id, *, with_group=False):
    row = project_identity(db, project_id, extra="is_archived")
    if row is None:
        return (None, False) if with_group else None
    name = row["group_name"] if row["group"] else row["patient_name"]
    identity = [name, row["extra"]]
    return (identity, row["group"]) if with_group else identity


def anchor(card):
    value = loads_dict(card["anchor_key"]) or {}
    return value if value.get("source") == "initial_history" else {}


def targets(db, project_id, payload, transport, *, scope=None):
    state = initial_import_state.load(db, project_id)
    if (state is None or payload.get("source") != "initial_history"
            or payload.get("cohort_id") != state["cohort_id"]):
        return []
    phase = "progress"
    if transport == "lineworks":
        phase = "start"
        scope = scope or {}
        prior = db.execute("SELECT delivery_state,ui_state FROM notification_cards "
            "WHERE kind='patient_history' AND project_id=? AND transport='lineworks' "
            "AND json_extract(anchor_key,'$.cohort_id')=? "
            "AND json_extract(anchor_key,'$.phase')='start' AND profile IS ? AND application_id IS ? AND team_id IS ? "
            "AND channel_id IS ? ORDER BY card_id DESC LIMIT 1",
            (project_id, state["cohort_id"], scope.get("profile"), scope.get("application_id"),
             scope.get("team_id"), scope.get("channel_id"))).fetchone()
        if prior and prior["delivery_state"] == "delivered" and (
                (loads_dict(prior["ui_state"]) or {}).get("history_latest") or {}).get("phase") == "complete":
            phase = "complete"
        elif prior and prior["delivery_state"] == "delivered":
            return []  # intermediate LINE WORKS progress has no remote phase
    return [{"card_key": f"v1|patient_history|{project_id}|{state['cohort_id']}|{phase}",
             "kind": "patient_history", "project_id": project_id,
             "coverage": [state["cohort_id"]], "anchor": {
                 "source": "initial_history", "cohort_id": state["cohort_id"], "phase": phase}}]


def _members(db, project_id, cohort, *, before=None):
    join, where = initial_import_state.history_predicate()
    return db.execute(f"SELECT m.* FROM messages m {join} WHERE m.project_id=? AND {where} "
        + ("AND (m.first_seen IS NULL OR m.first_seen<=?) " if before is not None else "")
        + "ORDER BY m.message_id", (project_id, cohort, *((before,) if before is not None else ())))


def snapshot(db, card, cfg, now):
    state = initial_import_state.progress(db, card["project_id"])
    key = anchor(card)
    if state is None or state["cohort_id"] != key.get("cohort_id"):
        raise ValueError("initial_history_source_changed")
    counts = {"saved": 0, "analyzed": 0, "processing": 0, "held": 0, "unknown": 0,
              "deleted": 0, "no_body": 0, "high": 0, "attachments": 0}
    for row in _members(db, card["project_id"], state["cohort_id"]):
        counts["saved"] += 1
        if row["body_state"] == "deleted":
            counts["deleted"] += 1
            continue
        if row["body_state"] == "unknown" and not row["parent_id"]:
            counts["no_body"] += 1
            continue
        progress = extraction_progress.message_progress(db, row, cfg=cfg)
        status = progress["state"] if progress else "unknown"
        if status == "complete":
            counts["analyzed"] += 1
        elif status == "attention":
            counts["held"] += 1
        else:
            counts[status if status in ("processing", "unknown") else "unknown"] += 1
        # The same guarded LLM-first/rule-fallback verdict used by live cards.
        if clinical_urgency.message_urgency(db, row["message_id"]) is not None:
            counts["high"] += 1
    join, where = initial_import_state.history_predicate()
    counts["attachments"] = db.execute(
        f"SELECT COUNT(*) FROM attachments a JOIN messages m ON m.message_id=a.message_id "
        f"{join} WHERE m.project_id=? AND {where}", (card["project_id"], state["cohort_id"])).fetchone()[0]
    counts["unknown"] += max(0, state["saved_count"] - counts["saved"])
    counts["saved"] = max(counts["saved"], state["saved_count"])
    complete = state["capture_phase"] == "complete" and counts["saved"] == counts["analyzed"] + counts["deleted"] + counts["no_body"]
    phase = "complete" if complete else "held" if state["capture_phase"] == "held" else (
        "analyzing" if state["capture_phase"] == "complete" else "capturing")
    return {**counts, "phase": phase, "capture_phase": state["capture_phase"],
            "cohort_id": state["cohort_id"], "observed_at": now,
            "artifact_cutoff": db.execute("SELECT COALESCE(MAX(artifact_id),0) FROM artifacts").fetchone()[0]}


def fingerprint(db, card):
    """New progress never changes an adopted source; source edits still invalidate it."""
    ui = loads_dict(card["ui_state"]) or {}
    adopted = ui.get("history_snapshot") or {}
    key = anchor(card)
    state = initial_import_state.load(db, card["project_id"])
    patient = _patient_identity(db, card["project_id"])
    if state is None or state["cohort_id"] != key.get("cohort_id"):
        return payload_hash(["missing", key])
    sources = [(row["message_id"], row["content_hash"], row["body_state"])
               for row in _members(db, card["project_id"], state["cohort_id"],
                                   before=adopted.get("observed_at", 0))]
    # Only already referenced high findings participate: first-time analysis
    # on other posts is progress, not a reason to cancel a sealed count snapshot.
    join, where = initial_import_state.history_predicate()
    high = []
    for row in db.execute(f"SELECT DISTINCT m.message_id FROM messages m {join} "
            f"JOIN artifacts a ON a.message_id=m.message_id AND a.project_id=m.project_id "
            f"WHERE m.project_id=? AND {where} AND a.artifact_id<=? "
            "AND a.kind IN ('extract_llm','extract_v1') AND json_valid(a.content) "
            "AND json_extract(a.content,'$.urgency')='high' AND json_valid(a.meta) "
            "AND json_extract(a.meta,'$.hash')=m.content_hash ORDER BY m.message_id",
            (card["project_id"], state["cohort_id"], adopted.get("artifact_cutoff", 0))):
        high.append((row[0], clinical_urgency.message_urgency(db, row[0])))
    return payload_hash([key, tuple(patient) if patient else None, state["boundary_ts"], sources, high])


def refresh_needed(db, card, now):
    """Cheap adoption gate; send-time fingerprinting still checks actual source rows."""
    ui = loads_dict(card["ui_state"]) or {}
    latest = ui.get("history_latest") or {}
    state = initial_import_state.load(db, card["project_id"])
    identity = _patient_identity(db, card["project_id"])
    in_flight = db.execute("SELECT 1 FROM notification_renders WHERE card_id=? "
                          "AND state IN ('sending','unknown') LIMIT 1", (card["card_id"],)).fetchone()
    if (state is None or not latest or ui.get("history_source_dirty") and not in_flight
            or state["capture_phase"] != latest.get("capture_phase")
            or identity != ui.get("history_patient_identity")):
        return True
    if (latest.get("phase") == "complete" and card["delivery_state"] == "delivered"
            and ui.get("history_dirty_since") is None):
        return False
    return now - ui.get("history_checked_at", 0) >= UPDATE_SECONDS


def refresh(db, card, cfg, now, *, force=False):
    """Refresh small latest counts; only an idle delivery may adopt them."""
    if not force and not refresh_needed(db, card, now):
        return dict(card)
    ui = loads_dict(card["ui_state"]) or {}
    state = initial_import_state.load(db, card["project_id"])
    if state is None:
        raise ValueError("initial_history_source_changed")
    latest = ui.get("history_latest") or {}
    adopted = ui.get("history_snapshot") or {}
    source_changed = bool(adopted and fingerprint(db, card) != ui.get("history_source_fp"))
    if card["transport"] == "lineworks" and card["message_id"] is not None:
        source_changed = False
    if source_changed:
        ui.setdefault("history_dirty_since", now)
    changed_phase = state["capture_phase"] != latest.get("capture_phase")
    refreshed = False
    if force or source_changed or ui.get("history_source_dirty") or not latest or changed_phase or now - ui.get("history_checked_at", 0) >= UPDATE_SECONDS:
        current = snapshot(db, card, cfg, now)
        if {k: v for k, v in current.items() if k not in ("observed_at", "artifact_cutoff")} != {
                k: v for k, v in latest.items() if k not in ("observed_at", "artifact_cutoff")}:
            ui.setdefault("history_dirty_since", now)
        patient = _patient_identity(db, card["project_id"])
        ui.update(history_latest=current, history_checked_at=now, history_analysis_changes=0,
                  history_patient_identity=list(patient) if patient else None)
        latest = current
        refreshed = True
    adopted = ui.get("history_snapshot") or {}
    live = db.execute("SELECT state FROM notification_renders WHERE card_id=? "
        "AND state IN ('queued','sending','unknown','held') ORDER BY render_rev DESC LIMIT 1", (card["card_id"],)).fetchone()
    phase = anchor(card).get("phase")
    # LINE WORKS emits only its two immutable phase posts.
    can_adopt = (not live or source_changed and live[0] in ("queued", "held")) and (
        card["transport"] != "lineworks" or not adopted or card["message_id"] is None)
    due = (source_changed or ui.get("history_source_dirty") or not adopted or latest.get("phase") != adopted.get("phase")
           or now - ui.get("history_rendered_at", 0) >= UPDATE_SECONDS)
    if can_adopt and due and ui.get("history_dirty_since") is not None:
        ui["history_snapshot"] = {**latest, **({"phase": "capturing"} if phase == "start" else {})}
        ui["history_rendered_at"] = now
        ui.pop("history_dirty_since", None)
        ui.pop("history_source_dirty", None)
    if refreshed and card["transport"] == "lineworks" and card["message_id"] is not None and not live:
        # This phase's delivered post is immutable; its latest counts were consumed locally.
        ui.pop("history_dirty_since", None)
        ui.pop("history_source_dirty", None)
    if not adopted and "history_snapshot" not in ui:
        ui["history_snapshot"] = {**latest, **({"phase": "capturing"} if phase == "start" else {})}
    if ui.get("history_snapshot") != adopted or "history_source_fp" not in ui:
        ui["history_source_fp"] = fingerprint(db, {**dict(card), "ui_state": json.dumps(ui)})
    db.execute("UPDATE notification_cards SET ui_state=? WHERE card_id=?", (json.dumps(ui), card["card_id"]))
    return {**dict(card), "ui_state": json.dumps(ui)}


def content(db, card):
    """Render adopted counts only, without reading histories, bodies or attachments."""
    ui = loads_dict(card["ui_state"]) or {}
    snap = ui.get("history_snapshot") or {}
    name, group = _patient_identity(db, card["project_id"], with_group=True)
    if name is not None and not name[0] and group:
        name[0] = f"project {card['project_id']}"
    title = {"capturing": "初回履歴の取込み開始", "analyzing": "履歴の保存完了・解析中",
             "complete": "初回履歴の保存・解析完了", "held": "初回履歴の取込みを確認中"}.get(snap.get("phase"), "初回履歴を取得中")
    if snap.get("phase") == "complete" and snap.get("no_body"):
        title = "初回履歴の取込み完了"
    text = [f"保存済み {snap.get('saved', 0)}件・解析済み {snap.get('analyzed', 0)}件"]
    for key, label in (("processing", "解析中"), ("held", "再試行・保留"), ("unknown", "解析状況未取得")):
        if snap.get(key):
            text.append(f"{label} {snap[key]}件")
    if snap.get("no_body"):
        text.append(f"本文未提供の親投稿 {snap['no_body']}件（解析対象外）")
    text.append(f"現行抽出で緊急度が高い投稿 {snap.get('high', 0)}件（MCSで確認）")
    if snap.get("attachments"):
        text.append(f"添付 {snap['attachments']}件（MCSで確認）")
    if snap.get("capture_phase") == "complete":
        text.append("取得時点の履歴の列挙を完了しました。")
    title = f"{name[0] if name else '患者'} — {title}"
    return {"containers": [{"type": "heading", "text": title},
                           {"type": "text", "rule": True, "text": "\n".join(text)}],
            "footer": [], "shown": [anchor(card)["cohort_id"]], "shown_kind": "cohort_ids",
            "page": 0, "pages": 1, "source_fp": ui.get("history_source_fp", payload_hash([])),
            "toggles": {"acked": False, "assigned": False, "has_tasks": False},
            "preview_text": (title + "\n" + "\n".join(text))[:600]}


def actions(card):
    return [[{"id": "link", "ui": "link", "label": "MCSで開く", "url": project_url(card["project_id"])}]]


def coalesce_tx(db, card, scope, epoch, now):
    """Bind due progress intents to one cohort card without inventing delivery."""
    key = anchor(card)
    rows = db.execute("SELECT event_id,payload FROM notify_outbox o WHERE kind='patient_history' "
        "AND project_id=? AND route='interactive' AND state IN ('pending','failed') AND next_try<=? "
        "AND json_valid(payload) AND json_extract(payload,'$.source')='initial_history' "
        "AND json_extract(payload,'$.cohort_id')=? AND NOT EXISTS (SELECT 1 "
        "FROM notification_intent_batches b WHERE b.event_id=o.event_id) ORDER BY event_id",
        (card["project_id"], now, key["cohort_id"])).fetchall()
    from mcs_requests import canonical
    for row in rows:
        payload = loads_dict(row["payload"])
        db.execute("INSERT INTO notification_intent_batches(event_id,frozen_payload,payload_hash,"
            "route_epoch,sealed_at,transport,scope_json) VALUES(?,?,?,?,?,?,?)",
            (row["event_id"], canonical(payload).decode(), payload_hash(payload), epoch, now,
             card["transport"], canonical(scope).decode() if card["transport"] in ("slack", "lineworks") else None))
        db.execute("INSERT INTO notification_intent_cards(event_id,card_id,coverage) VALUES(?,?,?)",
            (row["event_id"], card["card_id"], json.dumps([key["cohort_id"]])))
        db.execute("UPDATE notify_outbox SET next_try=? WHERE event_id=?", (now + 3600, row["event_id"]))


def analysis_changed(db, card, cfg, now):
    """Mark one changed source; aggregate work belongs to the bounded sweep."""
    ui = loads_dict(card["ui_state"]) or {}
    ui["history_analysis_changes"] = ui.get("history_analysis_changes", 0) + 1
    ui.setdefault("history_dirty_since", now)
    if (ui.get("history_latest") or {}).get("phase") == "complete":
        ui["history_source_dirty"] = True
    db.execute("UPDATE notification_cards SET ui_state=? WHERE card_id=?", (json.dumps(ui), card["card_id"]))
    return {**dict(card), "ui_state": json.dumps(ui)}


def emit_completion_tx(ledger, card, now):
    """Queue LINE WORKS' second phase only after its start receipt is proven."""
    if card["transport"] != "lineworks" or anchor(card).get("phase") != "start" or card["delivery_state"] != "delivered":
        return
    latest = (loads_dict(card["ui_state"]) or {}).get("history_latest") or {}
    if latest.get("phase") != "complete":
        return
    cohort = anchor(card)["cohort_id"]
    if ledger.db.execute("SELECT 1 FROM notification_cards WHERE kind='patient_history' AND project_id=? "
            "AND transport='lineworks' AND profile IS ? AND application_id IS ? AND team_id IS ? AND channel_id IS ? "
            "AND json_extract(anchor_key,'$.cohort_id')=? AND json_extract(anchor_key,'$.phase')='complete'",
            (card["project_id"], card["profile"], card["application_id"], card["team_id"], card["channel_id"], cohort)).fetchone():
        return
    if not ledger.db.execute("SELECT 1 FROM notify_outbox WHERE kind='patient_history' AND project_id=? "
            "AND state IN ('pending','failed') AND next_try IS NOT NULL AND json_valid(payload) "
            "AND json_extract(payload,'$.cohort_id')=?", (card["project_id"], cohort)).fetchone():
        ledger._outbox_insert("patient_history", card["project_id"],
            {"source": "initial_history", "cohort_id": cohort, "message_ids": []}, route="interactive")
