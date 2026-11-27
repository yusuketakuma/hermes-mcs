"""Prepare and deliver a bounded private previsit note through notify_outbox."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import re
import sqlite3
import time as clock
from zoneinfo import ZoneInfo

import notify_views
from intervention_schedule import read_source_schedule


KIND = "private_previsit_note"
ROUTE = "text"
PRIVATE_ROUTE_MARKER = "private_previsit"
NOTE_KIND = "private_previsit_note_v1"
ASSIGNMENT_KIND = "private_previsit_assignment_v1"
VISIT_DATE_KIND = "private_previsit_visit_date_v1"
JST = ZoneInfo("Asia/Tokyo")
MAX_PROJECTS = 500
MAX_NOTE_CHARS = 1800
MAX_TITLE_CHARS = 120
MAX_SOURCE_IDS = 200
SEND_LEASE_SECONDS = 120


class PrevisitError(ValueError):
    pass


def rollout_enabled(cfg: dict) -> bool:
    block = cfg.get("private_previsit") if isinstance(cfg, dict) else None
    return (type(block) is dict and block.get("enabled") is True
            and block.get("owner_gate_approved") is True)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _instant(now=None) -> float:
    if now is None:
        return datetime.now(timezone.utc).timestamp()
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError("invalid_previsit_time")
    return float(now)


def _local(now):
    return datetime.fromtimestamp(_instant(now), timezone.utc).astimezone(JST)


def _preparation_day(now):
    local = _local(now)
    if local.hour >= 20:
        return local.date() + timedelta(days=1)
    if local.hour < 6:
        return local.date()
    return None


def _dispatch_time(day: date) -> float:
    return datetime.combine(day, time(8), JST).timestamp()


def _latest(db, kind, project_id):
    row = db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=? "
        "ORDER BY artifact_id DESC LIMIT 1", (kind, project_id)).fetchone()
    if row is None:
        return None
    try:
        content = json.loads(row["content"])
        meta = json.loads(row["meta"] or "{}")
    except (TypeError, ValueError, RecursionError):
        raise PrevisitError("previsit_state_corrupt") from None
    if (type(content) is not dict or type(meta) is not dict
            or type(meta.get("schema")) is not int or meta["schema"] != 1
            or type(meta.get("revision")) is not int or meta["revision"] < 1
            or not isinstance(meta.get("command_id"), str) or not meta["command_id"]):
        raise PrevisitError("previsit_state_corrupt")
    return {"artifact_id": row["artifact_id"], "content": content, "meta": meta}


def _assignment(db, project_id):
    latest = _latest(db, ASSIGNMENT_KIND, project_id)
    if latest is None:
        raise PrevisitError("assignment_missing")
    value = latest["content"]
    if value.get("active") is not True:
        raise PrevisitError("assignment_revoked")
    scope = value.get("account_scope")
    scope_fields = {"profile", "application_id", "team_id", "route_epoch"}
    if (value.keys() != {"active", "staff_id", "transport", "account_scope", "recipient_id"}
            or not isinstance(value.get("staff_id"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", value["staff_id"]) is None
            or value.get("transport") not in ("lineworks", "slack", "discord")
            or not isinstance(value.get("recipient_id"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}", value["recipient_id"]) is None
            or not isinstance(scope, dict) or scope.keys() != scope_fields
            or any(not isinstance(scope.get(key), str)
                   or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", scope[key]) is None
                   for key in ("profile", "application_id", "team_id"))
            or type(scope.get("route_epoch")) is not int or scope["route_epoch"] < 1):
        raise PrevisitError("assignment_invalid")
    return latest


def _registered_day(db, project_id):
    latest = _latest(db, VISIT_DATE_KIND, project_id)
    if latest is None:
        return None, None
    content = latest["content"]
    if content.keys() != {"visit_date"}:
        raise PrevisitError("registered_date_invalid")
    value = content.get("visit_date")
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise PrevisitError("registered_date_invalid") from None
    return date.fromisoformat(value), latest


def _schedule(db, project_id):
    try:
        value = read_source_schedule(db, project_id, limit=200, max_records=200)
    except (ValueError, sqlite3.Error, TypeError, RecursionError):
        raise PrevisitError("schedule_unavailable") from None
    if (not isinstance(value, dict) or value.get("project_id") != project_id
            or not isinstance(value.get("records"), list) or len(value["records"]) > 200
            or not isinstance(value.get("coverage"), dict)):
        raise PrevisitError("schedule_unavailable")
    return value


def _fallback_day(schedule):
    coverage = schedule["coverage"]
    if (coverage.get("state") != "observed" or coverage.get("fetch_state") != "complete"
            or coverage.get("truncated") is not False
            or coverage.get("records_truncated") is not False
            or any(coverage.get(key) != 0 for key in
                   ("incomplete", "deleted", "invalid_source", "oversize"))):
        raise PrevisitError("schedule_coverage_unknown")
    records = schedule["records"]
    if not records or any(not isinstance(row, dict) for row in records):
        raise PrevisitError("schedule_unknown")
    if any(row.get("state") in ("unknown", "schedule_mention_unconfirmed", "planned_date_unknown")
           for row in records):
        raise PrevisitError("schedule_ambiguous")
    project_id = schedule.get("project_id")
    if type(project_id) is not int or project_id <= 0:
        raise PrevisitError("schedule_scope_invalid")
    pharmacy = [row for row in records if row.get("role") == "pharmacist"]
    if any(row.get("state") == "cancelled" for row in pharmacy):
        cancelled = [row for row in pharmacy if row.get("state") == "cancelled"]
        if any(not _source_bound_pharmacist_record(row, project_id) for row in cancelled):
            raise PrevisitError("schedule_ambiguous")
        raise PrevisitError("schedule_cancelled")
    planned = [row for row in pharmacy if row.get("state") == "planned"
               and isinstance(row.get("event_day"), str)]
    if any(not _source_bound_pharmacist_record(row, project_id) for row in planned):
        raise PrevisitError("schedule_ambiguous")
    days = {row["event_day"] for row in planned}
    if len(planned) != 1 or len(days) != 1:
        raise PrevisitError("schedule_conflict" if len(days) > 1 else "schedule_unknown")
    try:
        value = date.fromisoformat(next(iter(days)))
    except ValueError:
        raise PrevisitError("schedule_date_invalid") from None
    return value


def _source_bound_pharmacist_record(row, project_id):
    span = row.get("span")
    grounded = (row.get("assertion_only") is True
            and type(row.get("project_id")) is int and row["project_id"] == project_id
            and type(row.get("message_id")) is int and row["message_id"] > 0
            and isinstance(row.get("content_hash"), str)
            and re.fullmatch(r"[0-9a-f]{64}", row["content_hash"]) is not None
            and isinstance(row.get("quote"), str) and bool(row["quote"])
            and row.get("role_text") == "薬剤師"
            and isinstance(span, list) and len(span) == 2
            and type(span[0]) is int and type(span[1]) is int
            and 0 <= span[0] < span[1]
            and span[1] - span[0] == len(row["quote"]))
    if not grounded:
        return False
    if row.get("state") == "planned":
        try:
            return (isinstance(row.get("event_day"), str)
                    and date.fromisoformat(row["event_day"]).isoformat() == row["event_day"])
        except ValueError:
            return False
    return row.get("state") == "cancelled"


def _resolve_day(db, project_id, schedule):
    registered, registration = _registered_day(db, project_id)
    return (registered if registered is not None else _fallback_day(schedule), registration)


def _patient_state(db, project_id):
    row = db.execute("SELECT is_archived FROM patients WHERE project_id=?", (project_id,)).fetchone()
    if row is None or row[0] != 0:
        raise PrevisitError("patient_archived_or_missing")


def _source_pins(db, project_id, items):
    ids = sorted({row.get("message_id") for row in items
                  if isinstance(row, dict) and type(row.get("message_id")) is int
                  and row["message_id"] > 0})
    if len(ids) > MAX_SOURCE_IDS:
        raise PrevisitError("previsit_sources_unbounded")
    pins = []
    for mid in ids:
        row = db.execute(
            "SELECT content_hash,body_state,posted_at_ts FROM messages "
            "WHERE project_id=? AND message_id=?", (project_id, mid)).fetchone()
        if row is None or row["body_state"] != "full":
            raise PrevisitError("previsit_source_unavailable")
        if (not isinstance(row["content_hash"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["content_hash"])):
            raise PrevisitError("previsit_source_unavailable")
        pins.append([mid, row["content_hash"], row["body_state"], row["posted_at_ts"]])
    watermark = db.execute(
        "SELECT COALESCE(MAX(message_id),0),COUNT(*) FROM messages WHERE project_id=?",
        (project_id,)).fetchone()
    return pins, [watermark[0], watermark[1]]


def _render_view(db, project_id, cfg, now, visit_day, registration):
    view = notify_views.patient_context_list(
        db, project_id, "previsit", cfg=cfg, since=now - 30 * 86400,
        now=now, page=0)
    if (not isinstance(view, dict) or not isinstance(view.get("items"), list)
            or type(view.get("pages")) is not int or not 1 <= view["pages"] <= 1000
            or not isinstance(view.get("title"), str) or not view["title"].strip()):
        raise PrevisitError("previsit_view_invalid")
    items = view["items"]
    title = " ".join("".join(char if char.isprintable() else " "
                              for char in view["title"]).split())
    if not title:
        raise PrevisitError("previsit_view_invalid")
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS - 1] + "…"
    date_basis = ("登録済み薬局訪問日" if registration is not None
                  else "原文の薬剤師予定")
    lines = [f"訪問前メモ: {title}",
             f"予定日: {visit_day.isoformat()}（日付根拠: {date_basis}）"]
    content_added = False
    content_truncated = False
    for row in items:
        if not isinstance(row, dict):
            continue
        group = row.get("group")
        if not isinstance(group, str) or not group:
            continue
        quotes = row.get("quotes")
        if not isinstance(quotes, list):
            text = row.get("text")
            quotes = [text] if isinstance(text, str) else []
        for quote in quotes:
            if not isinstance(quote, str) or not quote.strip():
                continue
            line = "・" + group + ": " + " ".join(quote.split())
            source = row.get("source_rest")
            if isinstance(source, str) and source:
                line += "（" + " ".join(source.split()) + "）"
            page_marker = (f"（先頭ページのみ表示／全{view['pages']}ページ）"
                           if view["pages"] > 1 else "")
            truncation_marker = (f"（先頭ページのみ表示／全{view['pages']}ページ・文字数上限のため一部省略）"
                                 if view["pages"] > 1
                                 else "（文字数上限のため一部省略）")
            reserve = max(len("\n" + page_marker), len("\n" + truncation_marker))
            if len("\n".join([*lines, line])) + reserve > MAX_NOTE_CHARS:
                content_truncated = True
                break
            lines.append(line)
            content_added = True
        if content_truncated:
            break
    if view["pages"] > 1 or content_truncated:
        marker = (f"（先頭ページのみ表示／全{view['pages']}ページ・文字数上限のため一部省略）"
                  if view["pages"] > 1 and content_truncated
                  else f"（先頭ページのみ表示／全{view['pages']}ページ）"
                  if view["pages"] > 1 else "（文字数上限のため一部省略）")
        lines.append(marker)
    if not content_added:
        raise PrevisitError("previsit_empty")
    note = "\n".join(lines)
    if len(note) > MAX_NOTE_CHARS:
        raise PrevisitError("previsit_note_unbounded")
    pins, watermark = _source_pins(db, project_id, items)
    return note, pins, watermark


def _logical_key(project_id, assignment, visit_day):
    value = assignment["content"]
    return _digest({"project_id": project_id, "transport": value["transport"],
                    "recipient_id": value["recipient_id"], "visit_date": visit_day.isoformat()})


def _existing_event(db, project_id, logical_key):
    rows = db.execute(
        "SELECT event_id,payload FROM notify_outbox WHERE project_id=? "
        "AND (kind=? OR route=?) ORDER BY event_id DESC",
        (project_id, KIND, PRIVATE_ROUTE_MARKER)).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError, RecursionError):
            raise PrevisitError("previsit_outbox_corrupt") from None
        if not isinstance(payload, dict):
            raise PrevisitError("previsit_outbox_corrupt")
        if payload.get("logical_key") == logical_key:
            return row["event_id"]
    return None


def _prepare_project(ledger, project_id, cfg, now, target_day):
    db = ledger.db
    try:
        _patient_state(db, project_id)
        assignment = _assignment(db, project_id)
        if assignment["content"]["transport"] != "lineworks":
            return {"state": "skipped", "reason": "unsupported_transport", "project_id": project_id}
        schedule = _schedule(db, project_id)
        visit_day, registration = _resolve_day(db, project_id, schedule)
        if visit_day != target_day:
            return {"state": "skipped", "reason": "not_next_visit_day", "project_id": project_id}
        note, source_pins, watermark = _render_view(
            db, project_id, cfg, now, visit_day, registration)
        schedule_pin = _digest(schedule)
        logical_key = _logical_key(project_id, assignment, visit_day)
        db.execute("BEGIN IMMEDIATE")
        try:
            _patient_state(db, project_id)
            current_assignment = _assignment(db, project_id)
            current_schedule = _schedule(db, project_id)
            current_day, current_registration = _resolve_day(db, project_id, current_schedule)
            current_pins, current_watermark = _source_pins(db, project_id,
                                                            [dict(message_id=mid) for mid, *_ in source_pins])
            if (current_assignment["artifact_id"] != assignment["artifact_id"]
                    or current_day != visit_day or _digest(current_schedule) != schedule_pin
                    or (current_registration or {}).get("artifact_id") != (registration or {}).get("artifact_id")
                    or current_pins != source_pins or current_watermark != watermark):
                raise PrevisitError("previsit_source_changed")
            existing = _existing_event(db, project_id, logical_key)
            if existing is not None:
                db.commit()
                return {"state": "duplicate", "event_id": existing,
                        "project_id": project_id, "visit_date": visit_day.isoformat()}
            meta = {"schema": 1, "visit_date": visit_day.isoformat(),
                    "logical_key": logical_key, "assignment_artifact_id": assignment["artifact_id"],
                    "assignment_revision": assignment["meta"]["revision"],
                    "registration_artifact_id": (registration or {}).get("artifact_id"),
                    "schedule_pin": schedule_pin, "source_pins": source_pins,
                    "source_watermark": watermark,
                    "date_basis": "registered" if registration is not None else "source_schedule"}
            cursor = db.execute(
                "INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES(?,?,?,?,?)",
                (NOTE_KIND, project_id, note, _json(meta), now))
            event_id = ledger.outbox_add_tx(
                KIND, project_id,
                {"version": 1, "logical_key": logical_key,
                 "prepared_artifact_id": cursor.lastrowid,
                 "visit_date": visit_day.isoformat()},
                next_try=_dispatch_time(visit_day), route=ROUTE)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return {"state": "prepared", "event_id": event_id, "project_id": project_id,
                "visit_date": visit_day.isoformat(), "note": note}
    except PrevisitError as exc:
        return {"state": "skipped", "reason": str(exc), "project_id": project_id}
    except (sqlite3.Error, TypeError, ValueError, RecursionError):
        return {"state": "skipped", "reason": "previsit_state_unavailable", "project_id": project_id}


def prepare_night(ledger, cfg, now=None) -> dict:
    """Prepare tomorrow's bounded view during the night; never call an LLM."""
    if not rollout_enabled(cfg):
        return {"state": "disabled", "prepared": 0, "items": []}
    current = _instant(now)
    target_day = _preparation_day(current)
    if target_day is None:
        return {"state": "outside_night_window", "prepared": 0, "items": []}
    rows = ledger.db.execute(
        "SELECT DISTINCT project_id FROM artifacts WHERE kind=? AND project_id IS NOT NULL "
        "ORDER BY project_id LIMIT ?", (ASSIGNMENT_KIND, MAX_PROJECTS)).fetchall()
    items = [_prepare_project(ledger, row[0], cfg, current, target_day) for row in rows]
    return {"state": "prepared", "prepared": sum(item["state"] == "prepared" for item in items),
            "items": items}


def _progress(value):
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        raise PrevisitError("previsit_progress_corrupt") from None
    if type(parsed) is not dict:
        raise PrevisitError("previsit_progress_corrupt")
    return parsed


def _event_payload(row):
    try:
        value = json.loads(row["payload"])
    except (TypeError, ValueError, RecursionError):
        raise PrevisitError("previsit_outbox_corrupt") from None
    if (type(value) is not dict
            or value.keys() != {"version", "logical_key", "prepared_artifact_id", "visit_date"}
            or type(value.get("version")) is not int
            or value["version"] != 1
            or type(value.get("prepared_artifact_id")) is not int
            or not isinstance(value.get("logical_key"), str)
            or not isinstance(value.get("visit_date"), str)):
        raise PrevisitError("previsit_outbox_corrupt")
    return value


def _live_pins(db, project_id, meta):
    _patient_state(db, project_id)
    try:
        assignment = _assignment(db, project_id)
    except PrevisitError:
        raise PrevisitError("previsit_assignment_stale") from None
    if (assignment["artifact_id"] != meta.get("assignment_artifact_id")
            or assignment["meta"]["revision"] != meta.get("assignment_revision")):
        raise PrevisitError("previsit_assignment_stale")
    schedule = _schedule(db, project_id)
    try:
        visit_day, registration = _resolve_day(db, project_id, schedule)
    except PrevisitError:
        raise PrevisitError("previsit_schedule_stale") from None
    if (visit_day.isoformat() != meta.get("visit_date")
            or _digest(schedule) != meta.get("schedule_pin")
            or (registration or {}).get("artifact_id") != meta.get("registration_artifact_id")):
        raise PrevisitError("previsit_schedule_stale")
    pins = meta.get("source_pins")
    if not isinstance(pins, list) or len(pins) > MAX_SOURCE_IDS:
        raise PrevisitError("previsit_source_pin_invalid")
    if any(not isinstance(entry, list) or len(entry) != 4
           or type(entry[0]) is not int or entry[0] <= 0
           or not isinstance(entry[1], str) or not re.fullmatch(r"[0-9a-f]{64}", entry[1])
           or entry[2] != "full" for entry in pins):
        raise PrevisitError("previsit_source_pin_invalid")
    current_pins, watermark = _source_pins(
        db, project_id, [dict(message_id=entry[0]) for entry in pins
                         if isinstance(entry, list) and entry and type(entry[0]) is int])
    if current_pins != pins or watermark != meta.get("source_watermark"):
        raise PrevisitError("previsit_source_stale")
    return assignment


def _write_hold(db, event_id, reason, now):
    db.execute("UPDATE notify_outbox SET state='failed',next_try=NULL,updated_at=? "
               "WHERE event_id=?", (now, event_id))
    db.commit()
    # Keep the existing outbox hold marker format used by other uncertain sends.
    from mcs_queries import HOLD_PROGRESS_SET
    db.execute(f"UPDATE notify_outbox SET {HOLD_PROGRESS_SET},updated_at=? WHERE event_id=?",
               (reason, now, event_id))
    db.commit()


def _suppress(db, event_id, reason, now):
    db.execute("UPDATE notify_outbox SET state='suppressed',next_try=NULL,progress=?,updated_at=? "
               "WHERE event_id=?", (_json({"private_previsit": {"state": "suppressed",
                                                                    "reason": reason}}), now, event_id))
    db.commit()


def _claim(ledger, event, now):
    db = ledger.db
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM notify_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
        if row is None:
            db.commit()
            return None, "missing"
        if row["kind"] != KIND or row["route"] != ROUTE:
            _write_hold(db, row["event_id"], "private_event_scope_invalid", now)
            return None, "held"
        try:
            payload = _event_payload(row)
            progress = _progress(row["progress"])
            artifact = db.execute(
                "SELECT content,meta FROM artifacts WHERE artifact_id=? AND project_id=? AND kind=?",
                (payload["prepared_artifact_id"], row["project_id"], NOTE_KIND)).fetchone()
            if artifact is None or not isinstance(artifact["content"], str):
                raise PrevisitError("previsit_note_missing")
            meta = json.loads(artifact["meta"] or "{}")
            metadata_fields = {"schema", "visit_date", "logical_key", "assignment_artifact_id",
                               "assignment_revision", "registration_artifact_id", "schedule_pin",
                               "source_pins", "source_watermark", "date_basis"}
            registration_id = meta.get("registration_artifact_id") if type(meta) is dict else None
            expected_basis = "registered" if registration_id is not None else "source_schedule"
            try:
                valid_visit_date = (isinstance(meta.get("visit_date"), str)
                                   and date.fromisoformat(meta["visit_date"]).isoformat()
                                   == meta["visit_date"])
            except (TypeError, ValueError):
                valid_visit_date = False
            watermark = meta.get("source_watermark") if type(meta) is dict else None
            if (type(meta) is not dict or meta.keys() != metadata_fields
                    or type(meta.get("schema")) is not int or meta["schema"] != 1
                    or meta.get("logical_key") != payload["logical_key"]
                    or meta.get("visit_date") != payload["visit_date"]
                    or not valid_visit_date
                    or not isinstance(payload.get("logical_key"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", payload["logical_key"]) is None
                    or type(meta.get("assignment_artifact_id")) is not int
                    or meta["assignment_artifact_id"] <= 0
                    or type(meta.get("assignment_revision")) is not int
                    or meta["assignment_revision"] < 1
                    or (registration_id is not None
                        and (type(registration_id) is not int or registration_id <= 0))
                    or meta.get("date_basis") != expected_basis
                    or not isinstance(meta.get("schedule_pin"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", meta["schedule_pin"]) is None
                    or not isinstance(watermark, list) or len(watermark) != 2
                    or any(type(value) is not int or value < 0 for value in watermark)
                    or len(artifact["content"]) > MAX_NOTE_CHARS
                    or len(artifact["content"].splitlines()) < 3
                    or not artifact["content"].startswith("訪問前メモ: ")):
                raise PrevisitError("previsit_note_invalid")
            if type(row["attempts"]) is not int or row["attempts"] < 0:
                raise PrevisitError("previsit_attempts_invalid")
            if (row["state"] not in ("pending", "failed") or row["next_try"] is None
                    or row["accepted_ref"] is not None
                    or row["next_try"] > now):
                db.commit()
                return None, "not_due"
            if progress is not None:
                marker = progress.get("private_previsit")
                if (not isinstance(marker, dict) or marker.get("state") != "sending"
                        or type(marker.get("started_at")) not in (int, float)):
                    raise PrevisitError("previsit_progress_ambiguous")
                if now - marker["started_at"] < SEND_LEASE_SECONDS:
                    db.commit()
                    return None, "sending"
                _write_hold(db, row["event_id"], "private_send_crash_unknown", now)
                return None, "held"
            local = _local(now)
            visit_day = date.fromisoformat(payload["visit_date"])
            if local.date() > visit_day:
                _suppress(db, row["event_id"], "next_day_stale", now)
                return None, "suppressed"
            if local.date() != visit_day or local.hour < 8:
                db.commit()
                return None, "not_due"
            assignment = _live_pins(db, row["project_id"], meta)
            if _logical_key(row["project_id"], assignment, visit_day) != payload["logical_key"]:
                raise PrevisitError("previsit_authorization_stale")
            if assignment["content"].get("transport") != "lineworks":
                raise PrevisitError("unsupported_transport")
            marker = {"private_previsit": {"state": "sending", "started_at": now,
                                            "logical_key": payload["logical_key"]}}
            db.execute("UPDATE notify_outbox SET progress=?,updated_at=? WHERE event_id=?",
                       (_json(marker), now, row["event_id"]))
            db.commit()
            return {"event": dict(row), "payload": payload, "meta": meta,
                    "content": artifact["content"], "assignment": assignment}, None
        except PrevisitError as exc:
            if str(exc) in ("previsit_assignment_stale", "previsit_schedule_stale",
                            "previsit_source_stale", "previsit_authorization_stale",
                            "patient_archived_or_missing"):
                _suppress(db, row["event_id"], str(exc), now)
                return None, "suppressed"
            _write_hold(db, row["event_id"], str(exc), now)
            return None, "held"
        except (sqlite3.Error, TypeError, ValueError, RecursionError):
            _write_hold(db, row["event_id"], "private_previsit_state_invalid", now)
            return None, "held"
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise


def _settle(ledger, claimed, outcome, now):
    db = ledger.db
    event_id = claimed["event"]["event_id"]
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT state,progress,attempts FROM notify_outbox WHERE event_id=?",
                         (event_id,)).fetchone()
        progress = _progress(row["progress"]) if row else None
        marker = progress.get("private_previsit") if progress else None
        if row is None or row["state"] not in ("pending", "failed") \
                or not isinstance(marker, dict) or marker.get("state") != "sending" \
                or marker.get("logical_key") != claimed["payload"]["logical_key"]:
            if row is not None and row["state"] != "accepted":
                _write_hold(db, event_id, "private_send_claim_lost", now)
            else:
                db.commit()
            return "held"
        attempts = row["attempts"] + 1
        if outcome.get("result") == "delivered":
            receipt = outcome.get("accepted_ref")
            if not isinstance(receipt, str) or not receipt or len(receipt) > 80:
                _write_hold(db, event_id, "private_receipt_invalid", now)
                return "held"
            db.execute("UPDATE notify_outbox SET state='accepted',attempts=?,next_try=NULL,"
                       "accepted_ref=?,progress=NULL,updated_at=? WHERE event_id=?",
                       (attempts, receipt, now, event_id))
            db.commit()
            return "accepted"
        error = outcome.get("error_code") if isinstance(outcome.get("error_code"), str) else "send_failed"
        permanent = (error in {"scope_mismatch", "validation_invalid", "private_verification_failed",
                               "private_provider_unavailable", "unsupported_transport",
                               "credentials_invalid", "private_key_permissions", "signature_failed",
                               "upload_url_invalid", "rate_state_invalid"}
                     or (error.startswith("http_4") and error != "http_429"))
        if outcome.get("result") == "not_sent" and not permanent:
            from notify_flush import TRANSIENT_HOLD_AFTER_S, _retry_backoff, _transient_failing_for
            if _transient_failing_for(row["attempts"]) < TRANSIENT_HOLD_AFTER_S:
                delay = _retry_backoff(row["attempts"])
                db.execute("UPDATE notify_outbox SET state='failed',attempts=?,next_try=?,"
                           "accepted_ref=NULL,progress=NULL,updated_at=? WHERE event_id=?",
                           (attempts, now + delay, now, event_id))
                db.commit()
                return "retry"
            _write_hold(db, event_id, "private_retry_exhausted", now)
            return "held"
        if outcome.get("result") == "not_sent":
            _write_hold(db, event_id, "private_send_rejected", now)
            return "held"
        _write_hold(db, event_id, "private_send_unknown", now)
        return "held"
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise


def _provider(root):
    try:
        from adapters.lineworks.private_previsit import PrivatePrevisitProvider
        return PrivatePrevisitProvider(root)
    except (ImportError, OSError, ValueError):
        return None


def _authorization_current(ledger, claimed):
    db = ledger.db
    db.execute("BEGIN IMMEDIATE")
    try:
        assignment = _live_pins(db, claimed["event"]["project_id"], claimed["meta"])
        current = (_logical_key(claimed["event"]["project_id"], assignment,
                                date.fromisoformat(claimed["payload"]["visit_date"]))
                   == claimed["payload"]["logical_key"])
        db.commit()
        return current
    except (PrevisitError, sqlite3.Error, TypeError, ValueError, RecursionError):
        if db.in_transaction:
            db.rollback()
        return False


def dispatch_event(ledger, event, cfg, sender=None, now=None) -> dict:
    """Dispatch one private outbox event, never using shared sender routing."""
    current = _instant(now)
    event_id = event.get("event_id") if isinstance(event, dict) else None
    if type(event_id) is not int:
        return {"state": "held", "reason": "private_event_invalid"}
    if not rollout_enabled(cfg):
        ledger.outbox_hold(event_id, "private_previsit_rollout_gate_closed")
        return {"state": "held", "reason": "rollout_gate_closed"}
    try:
        import notify_spool
        if notify_spool.restore_pending(notify_spool.data_root(ledger)) is not None:
            ledger.db.execute(
                "UPDATE notify_outbox SET next_try=?,updated_at=? WHERE event_id=?",
                (current + 300, current, event_id))
            ledger.db.commit()
            return {"state": "restore_pending", "reason": "restore_pending"}
    except Exception:
        ledger.outbox_hold(event_id, "private_restore_state_unknown")
        return {"state": "held", "reason": "restore_state_unknown"}
    if sender is None:
        from mcs_util import HOME
        sender = _provider(HOME)
    if sender is None:
        ledger.outbox_hold(event_id, "private_provider_unavailable")
        return {"state": "held", "reason": "private_provider_unavailable"}
    claimed, reason = _claim(ledger, event, current)
    if claimed is None:
        return {"state": reason or "skipped"}
    assignment = claimed["assignment"]["content"]
    try:
        result = sender.send(
            claimed["content"], project_id=claimed["event"]["project_id"],
            recipient_id=assignment["recipient_id"], transport=assignment["transport"],
            account_scope=assignment["account_scope"],
            verify_authorization=lambda: _authorization_current(ledger, claimed))
    except Exception as exc:
        result = {"result": "unknown", "error_code": type(exc).__name__.lower()}
    if type(result) is not dict:
        result = {"result": "unknown", "error_code": "private_result_invalid"}
    if result.get("result") == "delivered" and "accepted_ref" not in result:
        result = {**result, "accepted_ref": "lineworks:201"}
    state = _settle(ledger, claimed, result, current)
    return {"state": state, "event_id": event_id}


def dispatch_due(ledger, cfg, sender=None, now=None, limit=20, *, deadline=None) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("invalid_previsit_limit")
    current = _instant(now)
    rows = ledger.db.execute(
        "SELECT event_id,kind,project_id,payload,attempts,progress,route,state,next_try,updated_at "
        "FROM notify_outbox WHERE (kind=? OR route=?) AND state IN ('pending','failed') "
        "AND next_try<=? ORDER BY event_id LIMIT ?",
        (KIND, PRIVATE_ROUTE_MARKER, current, limit)).fetchall()
    outcomes = []
    for row in rows:
        if deadline is not None and clock.monotonic() >= deadline:
            break
        # A preceding send may have crossed midnight since the queue was read.
        outcomes.append(dispatch_event(ledger, dict(row), cfg, sender=sender, now=now))
    return outcomes
