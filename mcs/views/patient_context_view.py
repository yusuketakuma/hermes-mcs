"""患者コンテキストの背景・変化・返信・訪問前確認などを原文根拠付きで読み出す。

All views are thin projections of one evidence list (``evidence``): the
current, source-bound ``patient_context`` items of ``read_model`` plus the
published nightly facts of ``patient_nightly``. Nothing here decides a
current clinical state, a formal completion or which of two records is
right; authorization stays with the caller and ``role_order`` only
reorders sections.
"""
from datetime import datetime, timedelta
import json
import re

from mcs_queries import JST
from read_model import read_model

ROLE_ORDER = {
    "nurse": ("adl", "devices", "nutrition", "cognition", "observations", "care_services", "living"),
    "physician": ("diagnoses", "history", "course", "observations", "adverse_events", "preferences"),
    "pharmacy": ("medication_management", "adverse_events", "allergies", "observations", "followup"),
    "care": ("care_level", "care_services", "living", "adl", "contacts", "care_team"),
}
# value-bearing detail keys whose differing values are worth showing side by side; the
# drug-specific ones are compared only under one written drug name
_COMPARED = ("actual_dose", "actual_frequency", "residual_quantity", "medication_manager", "supporter",
             "storage_place", "value", "self_management", "swallowing")
_PER_DRUG = ("actual_dose", "actual_frequency", "residual_quantity")
_EVENT_KEYS = ("measured_on", "observed_on", "onset", "last_confirmed_on")
_TRANSITION = re.compile(r"入院|退院|転院|入所|退所")
_WINDOW = timedelta(days=14).total_seconds()


def _day(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d") if isinstance(ts, (int, float)) else None


def _mentions(db, project_id, senders):
    """Medication and symptom mentions of each post's current extraction (the rollup's own
    per-message reader), kept as written: subject, negation and status are not resolved here."""
    from rollup import _decode_blobs, _dicts, _message_artifacts
    arts, _ = _message_artifacts(db, project_id)
    posts = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT message_id, posted_at_ts, content_hash FROM messages WHERE project_id=? AND body_state='full'",
        (project_id,))}
    out = []
    for mid, blobs in arts.items():
        if mid not in posts:
            continue
        v1, lm, _, _ = _decode_blobs(blobs)
        rows = [("medications", x.get("name"), x) for x in _dicts(lm.get("meds"))]
        rows += [("symptoms", x.get("text"), x) for x in _dicts(lm.get("symptoms"))]
        # rule-only names the LLM did not mention stay unverified candidates (rollup's rule)
        named = {text for _, text, _ in rows}
        rows += [("medications", x.get("name"), {**x, "unverified": True})
                 for x in _dicts(v1.get("medications")) if x.get("name") not in named]
        rows += [("symptoms", x, {"unverified": True}) for x in v1.get("symptoms") or []
                 if isinstance(x, str) and x not in named]
        out.extend({"category": category, "text": text, "quote": None,
                    "subject": item.get("subject") or "unspecified", "details": [],
                    "mention": {k: item[k] for k in ("dose", "action", "status", "negated", "unverified")
                                if k in item},
                    "message_id": mid, "content_hash": posts[mid][1], "posted_at_ts": posts[mid][0],
                    "sender": senders.get(mid, (None, None))[0], "profession": senders.get(mid, (None, None))[1],
                    "temporality": None, "discovery": None, "source": "chat"}
                   for category, text, item in rows if isinstance(text, str) and text)
    return out


def evidence(db, project_id, *, cfg=None) -> dict:
    """The shared evidence list every view projects from.

    Chat items are read_model's current, source-checked context (edited,
    deleted, foreign or unmatched quotes never appear). Nightly facts are
    added only when the core helper returns a published, still-valid row."""
    import patient_nightly
    model = read_model(db, scope="detail", project_id=project_id)
    senders = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT message_id, sender_name, profession FROM messages WHERE project_id=?", (project_id,))}
    items = []
    for record in model["records"]:
        for item in record["patient_context"]:
            source = item["source"]
            entry = {"category": item["category"], "text": item["text"], "quote": item["evidence"],
                     "subject": item["subject"], "details": item.get("details") or [],
                     "message_id": source["message_id"], "content_hash": source["content_hash"],
                     "posted_at_ts": source["posted_at_ts"],
                     "sender": senders.get(source["message_id"], (None, None))[0],
                     "profession": senders.get(source["message_id"], (None, None))[1],
                     "temporality": None, "discovery": None, "source": "chat"}
            items.append(entry)
    items.extend(_mentions(db, project_id, senders))
    nightly = patient_nightly.nightly_view_input(db, project_id, cfg=cfg)
    for fact in (nightly or {}).get("facts") or []:
        # nightly quotes the whole source line; the same grounded item is read_model's quote inside it
        matches = [i for i in items if i["source"] == "chat" and i["message_id"] == fact.get("message_id")
                   and i["category"] == fact.get("category") and i["quote"] in (fact.get("quote") or "")]
        extra = {"temporality": fact.get("temporality"), "discovery": fact.get("discovery"),
                 "found_night_id": fact.get("found_night_id"), "found_at": fact.get("found_at")}
        for match in matches:
            match.update(extra)
        if matches:
            continue
        items.append({"category": fact.get("category"), "text": fact.get("text"),
                      "quote": fact.get("quote"), "subject": fact.get("subject"),
                      "reported_subject": fact.get("reported_subject"), "details": fact.get("details") or [],
                      "message_id": fact.get("message_id"), "content_hash": fact.get("content_hash"),
                      "posted_at_ts": fact.get("posted_at_ts"), "sender": fact.get("sender"),
                      "source": fact.get("source", "chat"), **extra})
    items.sort(key=lambda i: (i["posted_at_ts"] or 0, i["message_id"] or 0), reverse=True)
    return {"project_id": project_id, "snapshot": model["snapshot"], "items": items,
            "summary": (nightly or {}).get("summary") or [],
            # summary fact_ids resolve here; empty when unpublished or no longer valid
            "nightly_facts": (nightly or {}).get("facts") or [],
            "as_of": (nightly or {}).get("as_of"),
            "coverage": {"read_model": model["coverage"], "nightly": (nightly or {}).get("coverage")}}


def background(ev) -> dict:
    """患者さんの全体像: every distinct grounded item per category, newest first and dated —
    a newer post on another topic never hides an older item."""
    sections = {}
    for item in ev["items"]:
        sections.setdefault(item["category"], []).append(item)
    return {"view": "background", "summary": ev["summary"], "as_of": ev["as_of"],
            "sections": [{"category": c, "items": rows} for c, rows in sections.items()],
            "coverage": ev["coverage"]}


def changes(db, ev, since_ts) -> dict:
    """前回から変わったこと, from the ledger's own observation record (messages.first_seen,
    message_revisions) so edits and deletions not yet reflected at night still show:
    new_report = first seen and posted since; retrospective = first seen since but posted
    earlier; edited / deleted = a revision observed since (a deleted post returns its id and
    observation time only); backfill_found = a nightly fact found since in an older record."""
    seen = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT message_id, first_seen, posted_at_ts FROM messages WHERE project_id=?", (ev["project_id"],))}
    revised = {}
    for mid, state, at in db.execute(
            "SELECT r.message_id, r.body_state, MAX(r.observed_at) FROM message_revisions r "
            "JOIN messages m ON m.message_id=r.message_id WHERE m.project_id=? AND r.observed_at>=? "
            "AND r.prev_content_hash IS NOT NULL GROUP BY r.message_id", (ev["project_id"], since_ts)):
        revised[mid] = (state, at)

    def kind(item):
        if item["discovery"] == "backfill_found":
            return "backfill_found" if (item.get("found_at") or -1) >= since_ts else None
        first, posted = seen.get(item["message_id"], (None, None))
        if isinstance(first, (int, float)) and first >= since_ts:  # unseen before: new to the reader
            return "new_report" if isinstance(posted, (int, float)) and posted >= since_ts else "retrospective"
        return "edited" if item["message_id"] in revised else None

    groups = {k: [] for k in ("new_report", "retrospective", "edited", "backfill_found")}
    for item in ev["items"]:
        if (group := kind(item)) is not None:
            groups[group].append(item)
    groups["deleted"] = [{"message_id": mid, "observed_at": at}
                         for mid, (state, at) in revised.items() if state == "deleted"]
    return {"view": "changes", "since": since_ts, "by_discovery": groups,
            "as_of": ev["as_of"], "coverage": ev["coverage"]}


def replies(db, project_id) -> dict:
    """連絡・返答の確認: request candidates with the thread's later replies as recorded.
    A missing reply reads 返答が見つからない, never 未対応; a 完了-like reply is not a formal completion."""
    from notify_views import REPLY_LABELS
    from rollup import current_cached_refs
    row = db.execute("SELECT content, meta FROM artifacts WHERE kind='patient_rollup' AND project_id=? "
                     "AND json_valid(content) ORDER BY artifact_id DESC LIMIT 1", (project_id,)).fetchone()
    roll = json.loads(row[0]) if row else {}
    roll = current_cached_refs(db, project_id, roll, row[1]) if isinstance(roll, dict) and row else {}
    live = {r[0] for r in db.execute("SELECT message_id FROM messages WHERE project_id=? AND body_state='full'",
                                     (project_id,))}
    # a request whose post was since edited away or deleted is no longer a written request
    rows = [r for r in (roll.get("recent_requests") or []) if isinstance(r, dict) and r.get("mid") in live]
    return {"view": "replies", "items": [
        {"message_id": r.get("mid"), "at": r.get("at"), "text": r.get("ctx"),
         "reply_state": r.get("reply_state"),
         "reply": (f"返信あり（{REPLY_LABELS[r['reply_state']]}・記録上）"
                   if r.get("reply_state") in REPLY_LABELS else "返答が見つからない"),
         "reply_conflict": r.get("reply_conflict") is True} for r in rows],
        "next_planned": roll.get("next_planned")}


def previsit(db, ev, now, *, days=7) -> dict:
    """訪問前の確認ノート: recent changes, then every written item to check — written
    follow-ups first, then requests with no reply found, one entry per post (owner: no fixed cap).
    Only quotes what was written — no new clinical instruction is made."""
    talk = replies(db, ev["project_id"])
    picked, posts = {"written_checks": [], "unanswered": []}, set()
    for group, rows in (("written_checks", [i for i in ev["items"] if i["category"] == "followup"]),
                        ("unanswered", [r for r in talk["items"] if r["reply_state"] is None])):
        for row in rows:
            if row["message_id"] not in posts:
                posts.add(row["message_id"])
                picked[group].append(row)
    return {"view": "previsit", "recent": changes(db, ev, now - days * 86400)["by_discovery"], **picked,
            "next_planned": talk["next_planned"], "as_of": ev["as_of"]}


def timeline(ev, categories=None) -> dict:
    """経過タイムライン: oldest first; the written event date stays apart from the posting time
    and adjacent entries carry no causal link."""
    rows = [{**i, "event_date_text": next((d["value"] for d in i["details"] if d.get("key") in _EVENT_KEYS), None)}
            for i in reversed(ev["items"]) if categories is None or i["category"] in categories]
    return {"view": "timeline", "items": rows}


def discrepancies(ev) -> dict:
    """記録の食い違い: differing written values of one attribute, shown with their sources.
    An observation is compared only with the same named item, unit and condition; only written
    event dates decide timing (unknown dates are never assumed equal) and nothing is decided."""
    # rule extraction splits one written observation into sibling items of the same post
    written = {}
    for item in ev["items"]:
        for d in item["details"]:
            written.setdefault((item["message_id"], item["category"], item["subject"]), {}) \
                .setdefault(d.get("key"), set()).add(d.get("value"))

    def one(attrs, key):  # a single written value, or None when absent or ambiguous
        values = attrs.get(key) or set()
        return next(iter(values)) if len(values) == 1 else None

    groups = {}
    for (mid, category, subject), attrs in written.items():
        for key in _COMPARED:
            value = one(attrs, key)
            if value is None:
                continue
            identity = ((one(attrs, "observation_name"), one(attrs, "unit"), one(attrs, "condition"))
                        if key == "value" else (one(attrs, "drug_name"),) if key in _PER_DRUG else ())
            if identity[:1] == (None,):
                continue  # which observation or drug the value belongs to is not written unambiguously
            source = next(i for i in ev["items"] if i["message_id"] == mid and i["category"] == category
                          and i["subject"] == subject
                          and any(d.get("key") == key for d in i["details"]))
            groups.setdefault((category, subject, key, identity), []).append(
                {**source, "value": value,
                 "event_date_text": next((one(attrs, k) for k in _EVENT_KEYS if one(attrs, k)), None)})
    out = []
    for (category, subject, key, identity), rows in groups.items():
        if len({r["value"] for r in rows}) < 2:
            continue
        dates = [r["event_date_text"] for r in rows]
        kind = ("time_unknown" if None in dates
                else "differing_records" if len(set(dates)) < len(dates) else "change_over_time")
        out.append({"category": category, "subject": subject, "key": key, "identity": list(identity),
                    "kind": kind, "records": rows})
    return {"view": "discrepancies", "items": out}


def handoff(db, ev) -> dict:
    """引継ぎ用の記録: what range was checked and what written checks/requests remain."""
    talk = replies(db, ev["project_id"])
    return {"view": "handoff", "coverage": ev["coverage"], "as_of": ev["as_of"],
            "written_checks": [i for i in ev["items"] if i["category"] == "followup"],
            "unanswered": [r for r in talk["items"] if r["reply_state"] is None]}


def search(db, project_id, query) -> dict:
    """患者別の質問・検索: stored posts with sources and the coverage line (existing 🔎 rule)."""
    from notify_views import patient_search_view
    return {"view": "search", **patient_search_view(db, project_id, query)}


def transition_course(ev) -> dict:
    """退院・移行時の経過: items within 14 days of a written admission/discharge and the
    categories with no record in that window (absence of a record, not of care)."""
    anchors = [i for i in ev["items"] if _TRANSITION.search(i["text"] or "")
               and isinstance(i["posted_at_ts"], (int, float))]
    out = []
    for anchor in anchors:
        near = [i for i in ev["items"] if isinstance(i["posted_at_ts"], (int, float))
                and abs(i["posted_at_ts"] - anchor["posted_at_ts"]) <= _WINDOW and i is not anchor]
        out.append({"anchor": anchor, "nearby": near,
                    "no_record": sorted({"medication_management", "adl", "care_services", "followup"}
                                        - {i["category"] for i in near})})
    return {"view": "transition_course", "items": out}


def role_order(view, role) -> dict:
    """職種に合う並び順: reorders background sections only; never adds or removes content."""
    first = ROLE_ORDER.get(role, ())
    rank = {c: n for n, c in enumerate(first)}
    return {**view, "sections": sorted(view["sections"], key=lambda s: rank.get(s["category"], len(first)))}
