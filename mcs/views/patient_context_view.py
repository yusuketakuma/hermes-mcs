"""患者コンテキストの背景・変化・返信・訪問前確認などを原文根拠付きで読み出す。

All views are thin projections of one evidence list (``evidence``): the
current, source-bound ``patient_context`` items of ``read_model`` plus the
published context facts of ``patient_context_refresh``. Nothing here decides a
current clinical state, a formal completion or which of two records is
right; authorization stays with the caller and ``role_order`` only
reorders sections.
"""
from datetime import datetime, timedelta
import hashlib
import json
import math
import re
import sqlite3

from clinical_scope import source_axes, source_polarity
from patient_context import LABELS, extract_context
from mcs_util import html_to_text, locate_quote_span
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
_TIMELINE_CONTEXT = frozenset(("living", "adl", "care_level", "care_services", "services_provided",
                               "nutrition", "skin_wound", "oral_health", "medication_management",
                               "allergies", "adverse_events", "devices", "cognition", "excretion",
                               "incidents", "plan_goals", "followup", "observations", "course", "symptoms"))
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
    facts = (context_refresh or {}).get("facts") or []
    scoped_facts = [fact for fact in facts if fact.get("project_id") is None
                    or type(fact["project_id"]) is int and fact["project_id"] == project_id]
    summary = (context_refresh or {}).get("summary") or []
    if len(scoped_facts) != len(facts):
        ids = {fact.get("id") for fact in scoped_facts if isinstance(fact.get("id"), str)}
        foreign_ids = {fact.get("id") for fact in facts if fact.get("project_id") is not None
                       and (type(fact["project_id"]) is not int or fact["project_id"] != project_id)
                       and isinstance(fact.get("id"), str)}
        ids -= foreign_ids
        summary = [sentence for sentence in summary if isinstance(sentence, dict)
                   and isinstance(sentence.get("fact_ids"), list) and sentence["fact_ids"]
                   and all(fact_id in ids for fact_id in sentence["fact_ids"])]
    bodies = {}
    for fact in scoped_facts:
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
        if (not matches and fact.get("project_id") == project_id and entry["source"] == "chat"
                and entry["message_id"] and entry["content_hash"]):
            source_key = (entry["message_id"], entry["content_hash"])
            if source_key not in bodies:
                raw = db.execute("SELECT body_text FROM messages WHERE project_id=? AND message_id=? "
                                 "AND content_hash=? AND body_state='full'", (project_id, *source_key)).fetchone()
                bodies[source_key] = raw[0] if raw and isinstance(raw[0], str) else None
            body = bodies[source_key]
            quote = entry.get("quote")
            span = locate_quote_span(body, quote) if body and isinstance(quote, str) else None
            if span and body[span[0]:span[1]] == quote == entry.get("evidence"):
                for item in items:
                    if item.get("evidence_span") is not None or item.get("reported_subject") is not None:
                        continue
                    candidate = {**item, "subject": "unspecified", "reported_subject": item.get("subject"),
                                 "evidence_span": list(span)}
                    if _assertion_key(candidate) == _assertion_key(entry):
                        matches.append(item)
        for match in matches:
            match.update(extra)
            match["subject"] = entry["subject"]
            match["reported_subject"] = entry["reported_subject"]
        if matches:
            continue
        items.append(entry)
    items.sort(key=lambda i: (i["posted_at_ts"] or 0, i["message_id"] or 0), reverse=True)
    return {"project_id": project_id, "snapshot": snapshot_meta(db), "items": items,
            "summary": summary,
            # summary fact_ids resolve here; empty when unpublished or no longer valid
            "nightly_facts": scoped_facts,
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


def _allergy_correction(text, target):
    return (isinstance(text, str) and re.search(r"訂正|撤回|誤り|誤記|修正|間違い", text)
            and ("アレルギー" in text or target in text
                 or re.search(r"前回|先日|先ほど|先程|その件|投稿\s*[#＃]|"
                              r"(?:^|[:：])\s*(?:本人(?:は|の))?(?:記録|記載|情報|内容)(?:を|は|が)", text)
                 or re.fullmatch(r"\s*(?:それ|こちら|本件)?(?:は|が)?間違い(?:でした|です)?[。\s]*", text)))


def allergy_posted_records(db, ev):
    """Keep a directly written allergy report separate from current clinical state."""
    import patient_context_refresh
    import patient_context_state
    from mcs_queries import JST

    pid = ev.get("project_id")
    coverage = ev.get("coverage") or {}
    inference = coverage.get("inference") or {}
    nightly = coverage.get("nightly") or {}
    attachments = nightly.get("attachments") or {}
    karte = nightly.get("karte_records") or {}
    facts = ev.get("nightly_facts") or []
    if (not db.in_transaction or type(pid) is not int or pid <= 0 or inference.get("complete") is not True
            or inference.get("publication_eligible") is not True or attachments.get("count") != 0
            or karte.get("stored") != 0
            or nightly.get("unreflected") != 0 or nightly.get("incomplete") != 0
            or nightly.get("unscanned") != 0 or not isinstance(facts, list)):
        return []
    candidates = [f for f in facts if isinstance(f, dict) and f.get("category") == "allergies"
                  and (f.get("temporal") or {}).get("subject") not in ("family", "other")]
    # Another allergy assertion may refer to the same substance, even with a different spelling.
    if len(candidates) != 1:
        return []
    fact = candidates[0]
    temporal = fact.get("temporal") or {}
    if (fact.get("project_id") != pid or fact.get("source") != "chat" or fact.get("historical")
            or fact.get("view_bucket") != "unknown" or temporal.get("subject") != "patient"
            or temporal.get("period") != "current" or temporal.get("unknown") is not False
            or temporal.get("negated") is True or temporal.get("corrections")
            or fact.get("relations") or patient_context_state._unbound_target(fact) is None
            or type(fact.get("message_id")) is not int
            or type(fact.get("posted_at_ts")) not in (int, float)):
        return []
    try:
        if not math.isfinite(fact["posted_at_ts"]):
            return []
        datetime.fromtimestamp(fact["posted_at_ts"], JST)
    except (ValueError, OverflowError, OSError):
        return []
    target = patient_context_state._unbound_target(fact)
    family_sources = {(f.get("message_id"), f.get("content_hash"), quote)
                      for f in facts if isinstance(f, dict) and f.get("category") == "allergies"
                      and (f.get("temporal") or {}).get("subject") in ("family", "other")
                      for quote in (f.get("quote"), f.get("evidence")) if isinstance(quote, str)}
    for other in [*facts, *(ev.get("items") or [])]:
        if not isinstance(other, dict):
            continue
        text = other.get("evidence") or other.get("quote") or ""
        same_source = (other.get("message_id"), other.get("content_hash")) == (
            fact.get("message_id"), fact.get("content_hash"))
        if other.get("id") == fact.get("id") or same_source and text == fact.get("evidence"):
            continue
        if _allergy_correction(text, target):
            return []
        if (other.get("category") == "allergies"
                and (other.get("message_id"), other.get("content_hash"), text) not in family_sources):
            return []
    body = fact.get("evidence")
    if (not isinstance(body, str) or len(body) > 300 or fact.get("quote") != body
            or fact.get("evidence_span") != [0, len(body)]
            or not re.fullmatch(r"\s*(?:アレルギー\s*[:：]\s*)?"
                                r"(?:ご本人|本人|患者(?:さん|様)?|利用者(?:さん|様)?)(?:は|に|が)\s*"
                                r"[一-龯々ァ-ヶーA-Za-z0-9]{1,30}(?:の)?アレルギー(?:が|は)"
                                r"(?:ある|あります)(?:[。]\s*)?", body)):
        return []
    source = db.execute("SELECT * FROM messages WHERE project_id=? AND message_id=? AND body_state='full'",
                        (pid, fact["message_id"])).fetchone()
    if source is None:
        return []
    source = {**dict(source), "source": "chat"}
    html = source.get("body_html")
    if (source.get("content_hash") != fact.get("content_hash") or source.get("body_text") != body
            or source.get("posted_at_ts") != fact["posted_at_ts"] or not isinstance(html, str)
            or hashlib.sha256(html.encode()).hexdigest() != fact["content_hash"] or html_to_text(html) != body
            or not patient_context_refresh._validated_facts(source, [fact])):
        return []
    try:
        rows = db.execute("SELECT message_id,body_state,content_hash,"
                          "CASE WHEN length(body_text)<=3000 THEN body_text END AS body_text,"
                          "CASE WHEN length(body_html)<=12000 THEN body_html END AS body_html "
                          "FROM messages WHERE project_id=? LIMIT 201", (pid,)).fetchall()
    except sqlite3.OperationalError:
        return []
    if not rows or len(rows) > 200:
        return []
    for row in rows:
        text, html = row["body_text"], row["body_html"]
        if (row["body_state"] != "full" or not isinstance(text, str) or not isinstance(html, str)
                or hashlib.sha256(html.encode()).hexdigest() != row["content_hash"] or html_to_text(html) != text
                or row["message_id"] != fact["message_id"] and _allergy_correction(text, target)):
            return []
        if (row["message_id"] != fact["message_id"] and ("アレルギー" in text or target in text)
                and (row["message_id"], row["content_hash"], text) not in family_sources):
            return []
    return [fact]


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
            "JOIN messages m ON m.message_id=r.message_id "
            "JOIN message_revisions prior ON prior.message_id=r.message_id AND prior.seq=("
            "SELECT MAX(p.seq) FROM message_revisions p WHERE p.message_id=r.message_id AND p.seq<r.seq) "
            "WHERE m.project_id=? AND r.observed_at>=? "
            "AND r.body_state IN ('full','deleted') AND prior.body_state IN ('full','deleted') "
            "AND r.prev_content_hash IS NOT NULL AND (r.content_hash IS NOT r.prev_content_hash OR "
            "r.body_state IS NOT prior.body_state) GROUP BY r.message_id", (ev["project_id"], since_ts)):
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
         "reply_events": [dict(event) for event in r.get("reply_events", [])],
         "reply": ("返信あり（完了と取消の記録あり）" if r.get("reply_conflict") is True else
                   f"返信あり（{REPLY_LABELS[r['reply_state']]}・記録上）"
                   if r.get("reply_state") in REPLY_LABELS else "返答が見つからない"),
         "reply_conflict": r.get("reply_conflict") is True} for r in rows],
        "next_planned": roll.get("next_planned")}


def previsit(db, ev, now, *, days=7) -> dict:
    """訪問前の確認ノート: recent changes, then every written item to check — written
    follow-ups first, then requests with no reply found; distinct assertions of one post remain.
    Only quotes what was written — no new clinical instruction is made."""
    from intervention_schedule import read_source_schedule

    talk = replies(db, ev["project_id"])
    interventions = read_source_schedule(db, ev["project_id"])
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
            "next_planned": talk["next_planned"], "interventions": interventions,
            "as_of": ev["as_of"], "coverage": ev["coverage"]}


def timeline(ev, categories=None) -> dict:
    """経過タイムライン: oldest first; the written event date stays apart from the posting time
    and adjacent entries carry no causal link."""
    rows = []
    for item in reversed(ev["items"]):
        if categories is not None and item["category"] not in categories:
            continue
        dates = {(detail["key"], detail["value"]) for detail in item.get("details") or []
                 if isinstance(detail, dict) and detail.get("key") in _EVENT_KEYS
                 and isinstance(detail.get("value"), str) and detail["value"]}
        date_kind, date_text = next(iter(dates)) if len(dates) == 1 else (None, None)
        row = {**item, "event_date_text": date_text, "event_date_kind": date_kind}
        # A source quote alone does not establish current state or a relationship.
        if item["category"] in _TIMELINE_CONTEXT and not item.get("view_bucket"):
            row["view_bucket"] = "unknown"
        rows.append(row)
    return {"view": "timeline", "items": rows, "order_basis": "posting_time",
            "coverage": ev.get("coverage")}


def observations(db, ev, *, source_limit=50) -> dict:
    """Project a bounded set of current source-bound observation records."""
    from observation_series import observation_series

    project_id = ev.get("project_id")
    if type(project_id) is not int or project_id <= 0 or type(source_limit) is not int or not 1 <= source_limit <= 200:
        raise ValueError("observation_scope_invalid")
    keys, seen = [], set()
    for item in ev.get("items") or []:
        if (item.get("category") != "observations" or item.get("source") != "chat"
                or type(item.get("message_id")) is not int or not isinstance(item.get("content_hash"), str)):
            continue
        key = (item["message_id"], item["content_hash"])
        if key not in seen:
            keys.append(key)
            seen.add(key)
    selected = keys[:source_limit]
    ids = sorted({mid for mid, _ in selected})
    originals = {}
    if ids:
        rows = db.execute("SELECT message_id,content_hash,body_text,body_html FROM messages "
                          "WHERE project_id=? AND body_state='full' AND message_id IN "
                          "(SELECT value FROM json_each(?)) AND length(body_text)<=20000 "
                          "AND length(body_html)<=80000", (project_id, json.dumps(ids)))
        wanted = set(selected)
        for mid, digest, body, html in rows:
            key = (mid, digest)
            if (key in wanted and isinstance(body, str) and isinstance(html, str)
                    and hashlib.sha256(html.encode()).hexdigest() == digest and html_to_text(html) == body):
                originals[key] = body
    result = observation_series(ev, source_bodies=originals)
    result["source_checks"] = {"eligible": len(keys), "selected": len(selected), "verified": len(originals),
                               "unavailable": len(selected) - len(originals), "limited": len(keys) > source_limit}
    return result


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
            date_keys = [k for k in _EVENT_KEYS if attrs.get(k)]
            date_kind = date_keys[0] if len(date_keys) == 1 and one(attrs, date_keys[0]) else None
            groups.setdefault((category, subject, key, identity), []).append(
                {**source, "value": value,
                 "event_date_kind": date_kind,
                 "event_date_text": one(attrs, date_kind) if date_kind else None})
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
                or len({r["event_date_kind"] for r in rows}) > 1
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
