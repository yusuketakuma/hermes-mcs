"""Read current patient and responder evidence for signal detection without owning transactions."""
from __future__ import annotations

from itertools import islice
import json
import re
import sqlite3

import clinical_scope
from clinical_values import patient_item_scope
from mcs_queries import CHANGE_ACTIONS, FACT_KINDS_SQL, current_fact_pred, json_or_null, med_is_patient_current

def _member_sql(col):
    """instr() test for one ? value being an element of a ", "-joined
    multi-value column (profession/organization)."""
    return f"instr(', '||COALESCE({col},'')||', ', ', '||?||', ')"



def _self_author_pred(organizations, alias="m"):
    """SQL fragment + params excluding mentions authored by configured
    own organizations — self-authored records aren't review candidates
    FOR us. Empty config -> no exclusion."""
    if not organizations:
        return "", []
    # organization holds stations joined with ", " (mcs_adapter);
    # match each listed station, not the whole joined string.
    return ("".join(f" AND {_member_sql(f'{alias}.organization')}=0"
                    for _ in organizations), list(organizations))



def _self_post_pred(professions, organizations, alias=""):
    pred, params = [], []
    for col, vals in (("profession", professions),
                      ("organization", organizations)):
        for v in vals or ():
            column = f"{alias}.{col}" if alias else col
            pred.append(f"{_member_sql(column)}>0")
            params.append(v)
    return " OR ".join(pred), params



def _self_post_exists(db, pid, ts, professions, organizations):
    """A post authored by a responder identity (pharmacist profession
    or a configured own-org) exists in the room after ts — visible
    engagement on the record. No identity configured -> never counts."""
    pred, params = _self_post_pred(professions, organizations)
    if not pred:
        return False
    return db.execute(
        f"SELECT 1 FROM messages WHERE project_id=? AND posted_at_ts>?"
        " AND body_state IS NOT 'deleted'"
        f" AND ({pred}) LIMIT 1",
        (pid, ts, *params)).fetchone() is not None



def request_registered(db, mid):
    """A registered request on the mention message is visible
    engagement — someone already turned it into a tracked item."""
    return db.execute(
        "SELECT 1 FROM requests WHERE source_message_id=? LIMIT 1",
        (mid,)).fetchone() is not None



def _engagement_checker(db, candidates, professions, organizations):
    """Read response witnesses in bounded batches at the original check point."""
    candidates = iter(candidates)
    pred, params = _self_post_pred(professions, organizations, "reply")
    response = (" OR EXISTS (SELECT 1 FROM messages reply "
                "WHERE reply.project_id=json_extract(c.value,'$[0]') "
                "AND reply.posted_at_ts>json_extract(c.value,'$[2]') "
                "AND reply.body_state IS NOT 'deleted' "
                f"AND ({pred}))") if pred else ""
    batch, matched, changes = {}, None, -1

    def check(pid, mid, ts):
        nonlocal batch, matched, changes
        while mid not in batch:
            batch = {row[1]: tuple(row) for row in islice(candidates, 256)}
            changes = -1
            if not batch:
                return (request_registered(db, mid)
                        or _self_post_exists(db, pid, ts, professions, organizations))
        if changes != db.total_changes:
            try:
                matched = {row[0] for row in db.execute(
                    "SELECT json_extract(c.value,'$[1]') FROM json_each(?) c "
                    "WHERE EXISTS (SELECT 1 FROM requests r "
                    "WHERE r.source_message_id=json_extract(c.value,'$[1]'))"
                    + response,
                    (json.dumps(list(batch.values())), *params))}
            except (sqlite3.Error, TypeError, ValueError, OverflowError):
                # Preserve per-source failure order if a bulk read is unavailable.
                matched = None
            changes = db.total_changes
        if matched is None:
            return (request_registered(db, mid)
                    or _self_post_exists(db, pid, ts, professions, organizations))
        return mid in matched

    return check



SELF_RESPONSE_REACTIONS = ("accepted", "completed")



def _self_reaction_response(db, mid):
    """The logged-in user's 承知/完了 stamp observed on the request post
    itself (capture only). Other stamps, other people's stamps and
    unfetched or invalid metadata never count."""
    from message_observations import get_message_metadata, get_metadata_shadow_status
    meta = get_message_metadata(db, mid)
    shadow = get_metadata_shadow_status(db, mid)
    if meta["last_error"] or (shadow["last_error"] and (
            shadow["checked_at"] or 0) >= (meta["checked_at"] or 0)):
        return False
    reactions = meta["reactions"] or ()
    return any(r["self_reacted"] and r["type"] in SELF_RESPONSE_REACTIONS
               for r in reactions)



def _archived_pids(db):
    """Archived rooms — med_period_artifacts (shared with mcs_stats) does
    not join patients, so the rx_period detectors exclude them here."""
    return {r[0] for r in db.execute(
        "SELECT project_id FROM patients WHERE COALESCE(is_archived,0)=1")}



def _patient_source(db, pid, mid):
    """(body_text, patient_name) of one message for scope checks, else None."""
    return db.execute("SELECT m.body_text,p.patient_name FROM messages m "
                      "JOIN patients p ON p.project_id=m.project_id "
                      "WHERE m.project_id=? AND m.message_id=?", (pid, mid)).fetchone()



def _patient_period(db, pid, mid, period):
    source = _patient_source(db, pid, mid)
    return source is not None and patient_item_scope(
        period, source[0], patient_name=source[1], surface=period.get("raw")) == "patient"



def _patient_transition(db, pid, mid):
    source = _patient_source(db, pid, mid)
    if source is None:
        return False
    matches = list(re.finditer(r"退院|転院|退所", source[0] or ""))
    return any(clinical_scope.patient_source_scope(source[0], m.start(), m.end(), patient_name=source[1]) == "patient"
               for m in matches) if matches else patient_item_scope({}, source[0], patient_name=source[1]) == "patient"



def _patient_change_message(db, pid, mid):
    for raw, body, name in db.execute(
            f"SELECT je.value,m.body_text,p.patient_name FROM artifacts a "
            "JOIN messages m ON m.message_id=a.message_id "
            "JOIN patients p ON p.project_id=m.project_id "
            f"JOIN json_each({json_or_null('a.content')},'$.meds') je "
            f"WHERE a.kind IN ({FACT_KINDS_SQL}) {current_fact_pred()} "
            "AND m.project_id=? AND m.message_id=? AND je.type='object'", (pid, mid)):
        item = json.loads(raw)
        if (item.get("action") in CHANGE_ACTIONS and med_is_patient_current(item)
                and patient_item_scope(item, body, patient_name=name, surface=item.get("name")) == "patient"):
            return True
    return False
