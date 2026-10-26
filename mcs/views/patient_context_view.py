"""患者コンテキストの背景・変化・返信・訪問前確認などを原文根拠付きで読み出す。

All views are thin projections of one evidence list (``evidence``): the
current, source-bound ``patient_context`` items of ``read_model`` plus the
published context facts of ``patient_context_refresh``. Nothing here decides a
current clinical state, a formal completion or which of two records is
right; authorization stays with the caller and ``role_order`` only
reorders sections.
"""
from datetime import timedelta
import json
import math
import re

from clinical_scope import source_axes, source_polarity
from patient_context import LABELS, extract_context
from mcs_util import locate_quote_span
from read_model import _records_coverage
from snapshot_access import snapshot_meta

# One source for every role surface (CLI --role, chat 表示順 buttons): labels and order only,
# never authorization. "" is the default order.
ROLE_LABELS = {"": "標準", "nurse": "看護", "physician": "医師", "pharmacy": "薬局", "care": "ケアマネ・介護",
               "helper": "訪問介護", "facility": "施設・通所", "rehab": "リハビリ", "nutrition": "栄養",
               "dental": "歯科", "msw": "相談員・連携"}
ROLE_ORDER = {
    "nurse": ("adl", "devices", "excretion", "skin_wound", "nutrition", "cognition", "observations",
              "services_provided", "orders_documents", "care_services", "living"),
    "physician": ("diagnoses", "history", "course", "observations", "adverse_events", "orders_documents",
                  "skin_wound", "preferences"),
    "pharmacy": ("medication_management", "adverse_events", "allergies", "observations", "excretion",
                 "oral_health", "followup"),
    "care": ("care_level", "care_services", "plan_goals", "living", "adl", "orders_documents", "contacts",
             "care_team"),
    "helper": ("services_provided", "excretion", "nutrition", "skin_wound", "adl", "cognition",
               "medication_management", "incidents"),
    "facility": ("observations", "nutrition", "excretion", "living", "cognition", "skin_wound", "incidents",
                 "medication_management", "preferences"),
    "rehab": ("adl", "devices", "observations", "plan_goals", "preferences"),
    "nutrition": ("nutrition", "observations", "oral_health", "skin_wound", "excretion", "medication_management"),
    "dental": ("oral_health", "nutrition", "devices", "medication_management"),
    "msw": ("course", "care_level", "care_services", "living", "contacts", "preferences", "orders_documents"),
}
# value-bearing detail keys whose differing values are worth showing side by side; the
# drug-specific ones are compared only under one written drug name
_COMPARED = ("actual_dose", "actual_frequency", "residual_quantity", "medication_manager", "supporter",
             "storage_place", "value", "self_management", "swallowing", "food_texture")
_PER_DRUG = ("actual_dose", "actual_frequency", "residual_quantity")
_EVENT_KEYS = ("measured_on", "observed_on", "onset", "last_confirmed_on")
_TRANSITION = re.compile(r"入院|退院|転院|入所|退所")
_WINDOW = timedelta(days=14).total_seconds()
# a recorded profession word -> the role whose display order fits it (first match wins; a hint only)
_ROLE_OF_PROFESSION = (("看護", "nurse"), ("歯科", "dental"), ("医師", "physician"), ("薬剤", "pharmacy"),
                       ("ケアマネ", "care"), ("介護支援", "care"), ("相談", "msw"), ("ソーシャル", "msw"),
                       ("ヘルパー", "helper"), ("訪問介護", "helper"), ("介護福祉", "helper"),
                       ("理学", "rehab"), ("作業療法", "rehab"), ("言語", "rehab"), ("リハ", "rehab"),
                       ("栄養", "nutrition"), ("施設", "facility"), ("通所", "facility"), ("デイ", "facility"))
# medication/symptom mentions are view-only groups (not extraction categories): their headings,
# ordered right after 服薬管理; every heading and the fixed section order read SECTION_LABELS
MENTION_LABELS = {"medications": ("薬剤の記載",), "symptoms": ("症状の記載",)}
SECTION_LABELS = {}
for _c, _labels in LABELS.items():
    SECTION_LABELS[_c] = _labels
    if _c == "medication_management":
        SECTION_LABELS.update(MENTION_LABELS)
_ORG_WORD = re.compile(r"事業所|ステーション|クリニック|薬局|病院|医院|診療所|センター|施設|居宅|訪問看護|訪問介護|デイ")


def _assertion_key(item):
    """One written assertion, including its source revision and grounded attributes."""
    values = {key: item.get(key) for key in (
        "source", "message_id", "content_hash", "category", "subject", "reported_subject", "text", "quote", "evidence_span")}
    values["details"] = sorted(json.dumps(d, sort_keys=True, ensure_ascii=False)
                               for d in item.get("details") or [])
    return json.dumps(values, sort_keys=True, ensure_ascii=False)


def _heading_groups(body):
    """Bind adjacent explicit attribute headings to their own written owner only."""
    groups, owner, previous = {}, None, None
    for item in extract_context(body):
        span = locate_quote_span(body, item["evidence"])
        if span is None:
            owner, previous = None, None
            continue
        keys = {d["key"] for d in item.get("details") or []}
        if (previous is None or previous["category"] != item["category"]
                or body[previous["end"]:span[0]].strip()
                or re.search(r"\r?\n[ \t　]*\r?\n", body[previous["end"]:span[0]])):
            owner = None
        if keys & {"drug_name", "observation_name"}:
            owner = span[0]
        if owner is not None:
            groups[(item["category"], item["text"], item["evidence"], item["subject"],
                    json.dumps(item.get("details") or [], sort_keys=True, ensure_ascii=False))] = owner
        previous = {"category": item["category"], "end": span[1]}
    return groups


def _mentions(db, project_id, senders):
    """Decorate raw source mentions with the already-read sender display fields."""
    from mcs_queries import source_patient_mentions
    from patient_context_state import clinical_state_fact

    entries = []
    for item in source_patient_mentions(db, project_id):
        raw = {**item["mention"], "name" if item["category"] == "medications" else "text": item["text"],
               "subject": item["subject"], "evidence": item["evidence"], "evidence_span": item["evidence_span"]}
        fact = clinical_state_fact({**item, "project_id": project_id, "body_state": "full",
                                    "body_text": item["reference_context"]["text"]}, raw, item["category"], "source_mentions")
        entry = {"category": item["category"], "text": item["text"], "quote": item["quote"],
             "subject": item["subject"], "details": [], "mention": item["mention"],
             "message_id": item["message_id"], "content_hash": item["content_hash"],
             "posted_at_ts": item["posted_at_ts"], "evidence": item["evidence"],
             "evidence_span": item["evidence_span"], "reference_context": item["reference_context"],
             "sender": senders.get(item["message_id"], (None, None))[0],
             "profession": senders.get(item["message_id"], (None, None))[1],
             "temporality": None, "discovery": None, "source": "chat"}
        if fact is not None:
            entry["temporal"] = fact["temporal"]
            entry["assertion"] = fact["reported"]
        # Raw mentions remain as-written mentions. Only the complete context
        # or rollup projection can establish a cross-source current state.
        entries.append(entry)
    return entries

def evidence(db, project_id, *, cfg=None) -> dict:
    """The shared evidence list every view projects from.

    Chat items are read_model's current, source-checked context (edited,
    deleted, foreign or unmatched quotes never appear). Published context facts are
    added only when the core helper returns a published, still-valid row."""
    import patient_context_refresh
    records, coverage, _ = _records_coverage(db, "detail", project_id, None)
    senders = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT message_id, sender_name, profession FROM messages WHERE project_id=?", (project_id,))}
    items = []
    heading_groups = {}
    for record in records:
        for item in record["patient_context"]:
            source = item["source"]
            entry = {"category": item["category"], "text": item["text"], "quote": item["evidence"],
                     "subject": item["subject"], "details": item.get("details") or [],
                     "message_id": source["message_id"], "content_hash": source["content_hash"],
                     "posted_at_ts": source["posted_at_ts"],
                     "sender": senders.get(source["message_id"], (None, None))[0],
                     "profession": senders.get(source["message_id"], (None, None))[1],
                     "temporality": None, "discovery": None, "source": "chat"}
            if item.get("origin") == "explicit_heading":
                source_key = (source["message_id"], source["content_hash"])
                if source_key not in heading_groups:
                    raw = db.execute("SELECT body_text FROM messages WHERE project_id=? AND message_id=? "
                                     "AND content_hash=? AND body_state IS NOT 'deleted'",
                                     (project_id, *source_key)).fetchone()
                    heading_groups[source_key] = _heading_groups(raw[0]) if raw and isinstance(raw[0], str) else {}
                group_key = (item["category"], item["text"], item["evidence"], item["subject"],
                             json.dumps(item.get("details") or [], sort_keys=True, ensure_ascii=False))
                group = heading_groups[source_key].get(group_key)
                if group is not None:
                    entry["_heading_group"] = group
            items.append(entry)
    items.extend(_mentions(db, project_id, senders))
    context_refresh = patient_context_refresh.context_view_input(db, project_id, cfg=cfg)
    for fact in (context_refresh or {}).get("facts") or []:
        extra = {"temporality": fact.get("temporality"), "discovery": fact.get("discovery"),
                 **{key: fact.get(key) for key in ("evidence", "evidence_span", "reference_context", "assertion", "temporal", "view_bucket", "relations")},
                 "found_night_id": fact.get("found_night_id"), "found_at": fact.get("found_at")}
        entry = {"category": fact.get("category"), "text": fact.get("text"),
                 "quote": fact.get("quote"), "subject": fact.get("subject"),
                 "reported_subject": fact.get("reported_subject"), "details": fact.get("details") or [],
                 "message_id": fact.get("message_id"), "content_hash": fact.get("content_hash"),
                 "posted_at_ts": fact.get("posted_at_ts"), "sender": fact.get("sender"),
                 "source": fact.get("source", "chat"), **extra}
        # A larger quote can contain different assertions, people or attributes.
        # Only identical source-bound meaning receives the discovery annotation.
        matches = [i for i in items if entry["content_hash"] and entry["source"]
                   and _assertion_key(i) == _assertion_key(entry)]
        for match in matches:
            match.update(extra)
        if matches:
            continue
        items.append(entry)
    items.sort(key=lambda i: (i["posted_at_ts"] or 0, i["message_id"] or 0), reverse=True)
    return {"project_id": project_id, "snapshot": snapshot_meta(db), "items": items,
            "summary": (context_refresh or {}).get("summary") or [],
            # summary fact_ids resolve here; empty when unpublished or no longer valid
            "nightly_facts": (context_refresh or {}).get("facts") or [],
            "as_of": (context_refresh or {}).get("as_of"),
            "coverage": {"read_model": coverage, "nightly": (context_refresh or {}).get("coverage"),
                         "inference": patient_context_refresh.inference_coverage(db, project_id, cfg or {})}}


def background(ev) -> dict:
    """患者さんの全体像: every distinct grounded item per category, newest first and dated —
    a newer post on another topic never hides an older item."""
    sections = {}
    for item in ev["items"]:
        sections.setdefault(item["category"], []).append(item)
    return {"view": "background", "summary": ev["summary"], "as_of": ev["as_of"],
            "sections": [{"category": c, "items": rows} for c, rows in sections.items()],
            "coverage": ev["coverage"]}


def participants(db, project_id) -> dict:
    """関係者・事業者: every poster observed in this patient's room, taken from the posts' own
    sender metadata (profession, name, organization). It lists who has written here, newest
    last post first; it never decides who is contracted, in charge or still involved."""
    # One row per poster (sender_id when MCS gave one, else the written name) and organization:
    # same-named people with different ids stay apart, a name whose profession changed shows
    # its latest post's profession, deleted posts are not evidence of anyone.
    rows = db.execute(
        "SELECT sender_id, sender_name, profession, organization, posted_at, message_id FROM messages "
        "WHERE project_id=? AND sender_name IS NOT NULL AND sender_name != '' "
        "AND body_state IS NOT 'deleted' ORDER BY posted_at DESC, message_id DESC", (project_id,))
    items, by_key = [], {}
    for sender_id, name, profession, organization, posted_at, mid in rows:
        key = (sender_id if sender_id is not None else name, organization or "")
        if key not in by_key:
            by_key[key] = {"name": name, "profession": profession or None, "organization": organization or None,
                           "sender_id": sender_id, "messages": 0, "first_seen": posted_at, "last_seen": posted_at,
                           "last_message_id": mid}
            items.append(by_key[key])
        by_key[key]["messages"] += 1
        by_key[key]["first_seen"] = posted_at
    return {"view": "participants", "project_id": project_id, "items": items, "roles": role_candidates(items)}


def role_candidates(items) -> list:
    """表示順の候補: the roles whose professions have posted here, in ROLE_ORDER order. The viewer's
    own profession is unknown, so this is offered, never applied."""
    found = {next((role for word, role in _ROLE_OF_PROFESSION if word in (who.get("profession") or "")), None)
             for who in items}
    return [role for role in ROLE_ORDER if role in found]


def changes(db, ev, since_ts) -> dict:
    """前回から変わったこと, from the ledger's own observation record (messages.first_seen,
    message_revisions) so edits and deletions not yet reflected at night still show:
    new_report = first seen and posted since; retrospective = first seen since but posted
    earlier; edited / deleted = a revision observed since (a deleted post returns its id and
    observation time only); backfill_found = a context fact found since in an older record."""
    seen = {r[0]: (r[1], r[2]) for r in db.execute(
        "SELECT message_id, first_seen, posted_at_ts FROM messages WHERE project_id=?", (ev["project_id"],))}
    revised = {}
    for mid, state, at in db.execute(
            "SELECT r.message_id, r.body_state, MAX(r.observed_at) FROM message_revisions r "
            "JOIN messages m ON m.message_id=r.message_id WHERE m.project_id=? AND r.observed_at>=? "
            "AND r.prev_content_hash IS NOT NULL AND (r.content_hash IS NOT r.prev_content_hash OR "
            "r.body_state IS NOT (SELECT p.body_state FROM message_revisions p WHERE p.message_id=r.message_id "
            "AND p.seq<r.seq ORDER BY p.seq DESC LIMIT 1)) GROUP BY r.message_id", (ev["project_id"], since_ts)):
        revised[mid] = (state, at)

    def kind(item):
        # the reader's own observation decides first: a post first seen since is new (or
        # retrospective) even when the context refresh scan also tagged it backfill_found
        first, posted = seen.get(item["message_id"], (None, None))
        if isinstance(first, (int, float)) and first >= since_ts:  # unseen before: new to the reader
            return "new_report" if isinstance(posted, (int, float)) and posted >= since_ts else "retrospective"
        if item["discovery"] == "backfill_found" and (item.get("found_at") or -1) >= since_ts:
            return "backfill_found"
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
    from mcs_queries import item_unverified
    from notify_views import REPLY_LABELS
    from rollup import build_rollup
    # The cached rollup can outlive an edited/deleted request, reply or plan post; rebuild
    # these rows from the current hash-bound extractions so nothing stale reads as current.
    # A read failure propagates: an unreadable source is not "no requests" (取得失敗≠記録なし).
    roll = build_rollup(db, project_id, request_limit=None)
    live = {r[0] for r in db.execute("SELECT message_id FROM messages WHERE project_id=? AND body_state='full'",
                                     (project_id,))}
    # a request whose post was since edited away or deleted is no longer a written request
    rows = [r for r in (roll.get("recent_requests") or []) if isinstance(r, dict) and r.get("mid") in live]
    posters = {r[0]: r[1:] for r in db.execute(
        "SELECT message_id, sender_id, sender_name, profession, organization FROM messages WHERE project_id=?",
        (project_id,))}
    last = {(w["sender_id"] if w["sender_id"] is not None else w["name"], w["organization"] or ""): w["last_seen"]
            for w in participants(db, project_id)["items"]}

    def asked_by(mid):  # the poster as recorded on the request post, and when they last posted here
        sender_id, name, profession, organization = posters.get(mid, (None, None, None, None))
        if not name:
            return None
        text = " ".join(v for v in (profession, name) if v) + (f"／{organization}" if organization else "")
        seen = last.get((sender_id if sender_id is not None else name, organization or ""))
        return text + (f"（最終投稿 {str(seen)[:16].replace('T', ' ')}）" if seen else "")
    return {"view": "replies", "items": [
        {"message_id": r.get("mid"), "at": r.get("at"), "text": r.get("ctx"), "sender": asked_by(r.get("mid")),
         "reply_state": r.get("reply_state"), "unverified": item_unverified(r),
         "reply": (f"返信あり（{REPLY_LABELS[r['reply_state']]}・記録上）"
                   if r.get("reply_state") in REPLY_LABELS else "返答が見つからない"),
         "reply_conflict": r.get("reply_conflict") is True} for r in rows],
        "next_planned": roll.get("next_planned")}


def previsit(db, ev, now, *, days=7) -> dict:
    """訪問前の確認ノート: recent changes, then every written item to check — written
    follow-ups first, then requests with no reply found; distinct assertions of one post remain.
    Only quotes what was written — no new clinical instruction is made."""
    talk = replies(db, ev["project_id"])
    picked = {"written_checks": [], "unanswered": []}
    for group, rows in (("written_checks", [i for i in ev["items"] if i["category"] == "followup"]),
                        # an unverified request candidate is not a written request awaiting a reply
                        ("unanswered", [r for r in talk["items"] if r["reply_state"] is None
                                        and not r["unverified"]])):
        seen = set()
        for row in rows:
            identity = _assertion_key(row)
            if identity not in seen:
                seen.add(identity)
                picked[group].append(row)
    return {"view": "previsit", "recent": changes(db, ev, now - days * 86400)["by_discovery"], **picked,
            "next_planned": talk["next_planned"], "as_of": ev["as_of"], "coverage": ev["coverage"]}


def timeline(ev, categories=None) -> dict:
    """経過タイムライン: oldest first; the written event date stays apart from the posting time
    and adjacent entries carry no causal link."""
    rows = [{**i, "event_date_text": next((d["value"] for d in i["details"] if d.get("key") in _EVENT_KEYS), None)}
            for i in reversed(ev["items"]) if categories is None or i["category"] in categories]
    return {"view": "timeline", "items": rows}


def discrepancies(ev, posters=None) -> dict:
    """記録の食い違い: differing written values of one attribute, shown with their sources.
    An observation is compared only with the same named item, unit and condition; only written
    event dates decide timing (unknown dates are never assumed equal) and nothing is decided.
    With posters, a written provider whose organization has not posted here is listed as a
    candidate too (no post is not no involvement)."""
    # Only source-verified adjacent explicit headings share an owner block.
    # Other assertions, even inside one post/category/person, stay separate.
    written = {}
    for item in ev["items"]:
        group = (item.get("source"), item.get("message_id"), item.get("content_hash"),
                 item["category"], item["subject"],
                 ("heading", item["_heading_group"]) if "_heading_group" in item
                 else ("assertion", _assertion_key(item)))
        report = written.setdefault(group, {"attrs": {}, "items": []})
        report["items"].append(item)
        for d in item["details"]:
            report["attrs"].setdefault(d.get("key"), set()).add(d.get("value"))

    def one(attrs, key):  # a single written value, or None when absent or ambiguous
        values = attrs.get(key) or set()
        return next(iter(values)) if len(values) == 1 else None

    groups = {}
    for group, report in written.items():
        category, subject = group[3:5]
        attrs = report["attrs"]
        for key in _COMPARED:
            value = one(attrs, key)
            if value is None:
                continue
            identity = ((one(attrs, "observation_name"), one(attrs, "unit"), one(attrs, "condition"))
                        if key == "value" else (one(attrs, "drug_name"),) if key in _PER_DRUG else ())
            if identity[:1] == (None,):
                continue  # which observation or drug the value belongs to is not written unambiguously
            source = next(i for i in report["items"]
                          if any(d.get("key") == key and d.get("value") == value for d in i["details"]))
            groups.setdefault((category, subject, key, identity), []).append(
                {**source, "value": value,
                 "event_date_text": next((one(attrs, k) for k in _EVENT_KEYS if one(attrs, k)), None)})
    out = []
    for (category, subject, key, identity), rows in groups.items():
        if len({r["value"] for r in rows}) < 2:
            continue
        # compare the written dates as calendar days; the original wording stays on each record
        from clinical_values import written_day
        dates = [written_day(r["event_date_text"]) for r in rows]
        same_day = {}
        for day, r in zip(dates, rows):
            same_day.setdefault(day, set()).add(r["value"])
        kind = ("time_unknown" if None in dates or len({d[0] for d in dates}) > 1
                else "differing_records" if any(len(v) > 1 for v in same_day.values()) else "change_over_time")
        out.append({"category": category, "subject": subject, "key": key, "identity": list(identity),
                    "kind": kind, "records": rows})
    orgs = {s.strip() for w in posters or [] for s in (w.get("organization") or "").split(",") if s.strip()}
    if orgs:
        # Match complete known names before separators, including names with an internal slash.
        names = "|".join(re.escape(o) for o in sorted(orgs, key=len, reverse=True))
        provider_parts = re.compile(rf"(?:{names}|[^、,，／/\n])+")
    for item in ev["items"] if orgs else []:
        text = item.get("text") or ""
        # each written provider separately: one posting organization does not cover the others
        written = [s for s in provider_parts.findall(text) if _ORG_WORD.search(s)]
        if item["category"] == "care_team" and any(not any(o in s for o in orgs) for s in written):
            out.append({"category": "care_team", "subject": item["subject"], "key": "organization", "identity": [],
                        "kind": "provider_not_posting", "records": [{**item, "value": text}]})
    return {"view": "discrepancies", "items": out}


def handoff(db, ev) -> dict:
    """引継ぎ用の記録: what range was checked and what written checks/requests remain."""
    talk = replies(db, ev["project_id"])
    return {"view": "handoff", "coverage": ev["coverage"], "as_of": ev["as_of"],
            "written_checks": [i for i in ev["items"] if i["category"] == "followup"],
            "unanswered": [r for r in talk["items"] if r["reply_state"] is None and not r["unverified"]]}


def search(db, project_id, query) -> dict:
    """患者別の質問・検索: stored posts with sources and the coverage line (existing 🔎 rule)."""
    from notify_views import patient_search_view
    return {"view": "search", **patient_search_view(db, project_id, query)}


def transition_course(ev) -> dict:
    """退院・移行時の経過: retain every transition report; only explicitly affirmed,
    current patient transitions open a posting-time window, never family/plans/unknowns."""
    by_post = {}
    for i in ev["items"]:
        quote = i.get("quote") or i.get("text") or ""
        scopes = []
        for match in _TRANSITION.finditer(quote):
            left = max(quote.rfind(mark, 0, match.start()) for mark in "。！？!?;；\n") + 1
            stop = re.search(r"[。！？!?;；\n]", quote[match.end():])
            right = match.end() + stop.start() if stop else len(quote)
            subject, period = source_axes(quote, match.start(), match.end())
            scopes.append({"subject": subject, "period": period,
                           "negated": source_polarity(quote, left, right, target_end=match.end()),
                           "span": [match.start(), match.end()]})
        if not scopes:
            continue
        ts = i.get("posted_at_ts")
        current = (type(ts) in (int, float) and math.isfinite(ts)
                   and all(s["subject"] == "patient" and s["period"] == "current"
                           and s["negated"] is False for s in scopes))
        kind = "patient_current" if current else "reported"
        key = (i.get("source"), i.get("message_id") if i.get("message_id") is not None else id(i),
               i.get("content_hash"), kind)
        by_post.setdefault(key, []).append((i, scopes))
    out = []
    for key, reports in by_post.items():
        anchors = [{**item, "transition_scope": scopes} for item, scopes in reports]
        anchor = anchors[0]
        current = key[-1] == "patient_current"
        near = [i for i in ev["items"] if current and type(i.get("posted_at_ts")) in (int, float)
                and math.isfinite(i["posted_at_ts"])
                and abs(i["posted_at_ts"] - anchor["posted_at_ts"]) <= _WINDOW
                and not any(i is original for original, _ in reports)]
        out.append({"anchor": anchor, "anchors": anchors, "anchor_kind": key[-1], "nearby": near,
                    "no_record": sorted({"medication_management", "adl", "care_services", "followup"}
                                        - {i["category"] for i in near + anchors}) if current else []})
    return {"view": "transition_course", "items": out}


def role_order(view, role) -> dict:
    """職種に合う並び順: reorders background sections only; never adds or removes content.
    The role's categories come first; the rest (and the standard view) follow the fixed
    category order, so 病名・既往 never sink below whatever was posted last."""
    first = ROLE_ORDER.get(role, ())
    rank = {c: n for n, c in enumerate(first)}
    base = {c: n for n, c in enumerate(SECTION_LABELS)}
    return {**view, "sections": sorted(view["sections"], key=lambda s: (
        rank.get(s["category"], len(first)), base.get(s["category"], len(base))))}
