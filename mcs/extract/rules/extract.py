#!/usr/bin/env python3
"""MCS structured extraction — rule-based v1.

Parses each stored message body into a structured JSON artifact
(kind='extract_v1') designed for reuse: timelines, search, rollups,
and later LLM passes (stored separately with kind='extract_llm').

Fields are grounded in the NCGG home-visit pharmacy guide + observed MCS
conventions (visit-date headers, med periods "8/17-9/6", SOAP sections,
vital strings, speaker labels). All work is LOCAL — no data leaves the box.

Usage:
  python3 extract.py --all              # every message lacking extract_v1
  python3 extract.py --project <id>     # one patient
  python3 extract.py --stats            # aggregate stats only
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every module dir (any depth) as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import _mcs_path  # noqa: F401
import clinical_scope
import clinical_events
import clinical_urgency
import clinical_values
import extract_schema
import inference_dependency_loss as dependency_loss
import inference_version as inference
import intervention_schedule
import writer_lock
from patient_context import extract_context
from ledger import Ledger, LedgerReader
from mcs_queries import ELIGIBLE_MESSAGE, current_extract_pred, json_or_null
from mcs_util import HOME, html_to_text, loads_dict, strict_json
from writer_lock import acquire_run_lock

DB = os.path.join(HOME, "data", "ledger.db")
KIND = "extract_v1"
_INFERENCE_VERSION = inference.CURRENT_VERSION
_WRITER_STATE = writer_lock.updater_state()
_INFERENCE_SOURCE_STAT = os.stat(inference.__file__).st_mtime_ns

# ---------- patterns ----------

# Only particles, fillers, time words or further listed symptoms may sit between a
# symptom and its negation, so 「発熱・疼痛なし」「発熱は本日はなし」 are negated while
# 「痛みが取れない」「食欲不振で食べられない」「発熱あり、疼痛なし」 keep 発熱 positive, and
# a negated negation (「ないわけではない」) stays positive.
_LISTED_SYMPTOM = "|".join(map(re.escape, sorted(clinical_scope.SYMPTOMS, key=len, reverse=True)))
_SYMPTOM_NEGATION = re.compile(
    r"さ?(?:[はもがの]|の?訴え|症状|等|など|特に|本日|今日|今回|現在|今朝|夜間|日中|明らかな|[ \t　]"
    r"|[・/／、とや](?:" + _LISTED_SYMPTOM + r"))*"
    r"(?:なし|無し|なく|無く|ありません|ない|認めず|認めない|認めません|認められず|認められない|認められません"
    r"|見られず|見られない|見られません|見当たらない"
    r"|否定(?!でき|しきれ|し(?:難|がた|ない|ません)|されない|されず)|[(（][-−－][)）])"
    # 「なくはない」「なくもない」 negate the negation: the symptom stays
    r"(?!(?:では|じゃ)(?:ない|ありません)|わけ(?:では|じゃ)(?:ない|ありません)|[はも](?:ない|ありません))")
_ADHERENCE = ("残薬", "飲み忘れ", "飲み残し", "未使用", "服薬不良",
              "アドヒアランス", "一包化", "お薬カレンダー", "自己注射",
              "残あり", "残なし")








_REQUESTS = ((r"ご?確認(?:を|お願い|ください|をお願い)", "confirm"),
             (r"(?:ご)?連絡(?:ください|をお願い|いただき)", "contact"),
             (r"共有(?:いたします|します|をお願い|させて)", "share"),
             (r"お願い(?:いたします|します|申し上げ)", "request"),
             (r"教えて(?:ください|いただけ|欲しい)", "ask"),
             (r"報告(?:いたします|します|をお願い)", "report"))

_MED_CTX = re.compile(r"薬|処方|内服|外用|点眼|貼付|mg|錠|剤|坐薬|座薬|注射")
# rx_actions keep at most RX_ACTIONS_CAP entries, each ctx starting RX_CTX_BACK chars before its word
RX_ACTIONS_CAP = 10
RX_CTX_BACK = 14
# date range like 8/17-9/6 or 9/24-10/7 (med periods)
_MED_PERIOD = re.compile(
    r"(?<![\d/])(?:(\d{4})/)?(\d{1,2}/\d{1,2})"
    r"\s*[-–~〜]\s*(?:(\d{4})/)?(\d{1,2}/\d{1,2})(?!\d)")
# Facility/provider names and service explanations are not encounter evidence.
ENCOUNTER_CUE = clinical_events.ENCOUNTER_CUE
RULE_VERSION = 28
_VISIT_DATE = re.compile(
    r"(?:(\d{4})[-/年])?(\d{1,2})[/月](\d{1,2})日?[　\s]*(?:\(|（)?[月火水木金土日]?"
    r"(?:\)|）)?[　\s]*(?:訪問(?:診療)?|診察|往診)")
_NEXT_VISIT_HEAD = (
    r"(?:次回|次の)[ \t　]*(?:の[ \t　]*)?(?:(?:訪問診療|訪問|往診|診察)"
    r"[ \t　]*(?:の[ \t　]*)?(?:予定(?:日(?:時)?)?|日時|日程|日)?|予定(?:日(?:時)?)?|日時|日程)"
    r"[ \t　]*(?:は|が)?[ \t　]*[:：]?[ \t　、,]*")
_NEXT_PLANNED = re.compile(
    rf"(?:(?P<scheduled>{_NEXT_VISIT_HEAD})(?P<newline>\r?\n[ \t　]*)?|次回[^。！？!?;；\r\n]{{0,8}}?)"
    r"(?<!\d)(?:(?P<year>\d{4})[-－/／年])?(?P<month>\d{1,2})[-－/／月](?P<day>\d{1,2})(?!\d)日?"
    r"(?:[ \t　]*[（(][月火水木金土日](?:曜(?:日)?)?[）)])?")
_NEXT_CANCELLED = re.compile(
    r"^[ \t　、,（(]*(?:\d{1,2}[:：時](?:\d{1,2}分?)?[ \t　]*)?"
    r"(?:の|に)?(?:訪問(?:診療)?|往診|診察)?(?:予定(?:していました|していた)?)?(?:です|でした)?(?:が|は|を)?"
    r"[ \t　、,]*(?:中止|キャンセル|延期|未定)")
_PLANNED_BEFORE = re.compile(r"次回|予定(?!通り|どおり)|明日|明後日|今度|来週")
# 予定通りなら (conditional) right before the date, beyond the 6-char window
_PLANNED_IF = re.compile(r"予定(?:通り|どおり)なら[　\s、,，]*$")
_PLANNED_AFTER = re.compile(r"[　\s]*(?:の|を)?[　\s]*(?:予定|します|いたします|致します)")


def _next_planned_matches(body):
    """Keep explicit upcoming dates in their own schedule field, excluding cancelled or other-person plans."""
    for match in _NEXT_PLANNED.finditer(body):
        if match["newline"] and not re.match(
                r"[ \t　]*(?:(?:[01０１]?\d|[2２][0-3０-３])[:：時][0-5０-５]\d分?[ \t　]*)?(?:\r?\n|$)",
                body[match.end():]):
            continue
        if (re.search(r"中止|キャンセル|延期|未定", body[match.start():match.start("month")])
                or _NEXT_CANCELLED.match(body[match.end():])):
            continue
        scopes = (clinical_scope.patient_source_scope(body, start, match.end(), provider_fields=True)
                  for start in (match.start(), match.start("year") if match["year"] else match.start("month")))
        if any(scope in ("family", "other", "unknown", "past", "conditional") for scope in scopes):
            continue
        yield match


def _ymd(month: int, day: int, year: int | None,
         posted: datetime | None = None,
         mode: str = "past") -> str | None:
    """Resolve an M/D (no year) against the post date. `mode`:
      'past'   — event already happened (visit_date): never after posted
      'future' — planned date (next_planned): never before posted
      'any'    — explicit year or neutral
    Year wraps at Dec/Jan are resolved by shifting the year, not by
    guessing (Oracle B26)."""
    if year is None:
        return None
    years = (year,) if mode == "any" else (year, year + 1, year - 1)
    for y in years:
        try:
            d = datetime(y, month, day).date()
        except ValueError:
            continue
        if posted is None or mode == "any":
            return d.isoformat()
        if mode == "future" and d < posted.date():
            continue        # push to next year
        if mode == "past" and d > posted.date():
            continue        # pull to last year
        return d.isoformat()
    return None


def _md_date(month: int, day: int, year: int):
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def _period_dates(a, b, start_year, end_year, posted, context):
    start_md = tuple(map(int, a.split("/")))
    end_md = tuple(map(int, b.split("/")))
    wraps = end_md < start_md
    if start_year or end_year:
        sy = int(start_year) if start_year else int(end_year) - wraps
        ey = int(end_year) if end_year else sy + wraps
        start, end = _md_date(*start_md, sy), _md_date(*end_md, ey)
        return (start, end) if start and end and start <= end else (None, None)
    if posted is None:
        return None, None
    candidates = [(_md_date(*start_md, y), _md_date(*end_md, y + wraps))
                  for y in (posted.year - 1, posted.year, posted.year + 1)]
    candidates = [(s, e) for s, e in candidates if s and e]
    day = posted.date()
    if re.search(r"予定|開始予定|投与予定", context):
        candidates = [(s, e) for s, e in candidates if e >= day]
    if not candidates:
        return None, None
    def distance(pair):
        return max((pair[0] - day).days, (day - pair[1]).days, 0)
    candidates.sort(key=distance)
    if len(candidates) > 1 and distance(candidates[0]) == distance(candidates[1]):
        return None, None
    return candidates[0]




def extract_message(body: str, posted_at: str) -> dict:
    """Extract source-bound clinical events and legacy rule classification tags."""
    out: dict = {"v": 1}
    year = None
    posted = None
    try:
        posted = datetime.fromisoformat(posted_at)
        year = posted.year
    except (ValueError, TypeError):
        pass

    # --- events ---
    event_patterns = {
        "visit": r"訪問(?:し|した|時|実施|致し)",
        "exam": ENCOUNTER_CUE + "|" + clinical_events.OUTPATIENT_CUE,
        "admission": r"入院", "discharge": r"退院|出院|退所|退館",
        "eol": r"看取り|終末期|逝去|お亡くなり|亡くなっ|死亡|臨終|永眠",
        "care": r"デイ|ショートステイ|ケアプラン|要介護|介護度",
    }
    cues = {kind: re.compile(pattern) for kind, pattern in event_patterns.items()}
    nominated = [kind for kind, cue in cues.items() if cue.search(body)]
    events, records, _ = clinical_events.normalize_events(nominated, None, body, cues=cues)
    if records:
        out["event_records"] = records
    event_mentions = [{"event": record["kind"], "scope": record["subject"],
                       "evidence": record["evidence"]}
                      for record in records if record["subject"] in ("family", "other", "unknown")]
    # These rule-only classification tags are separate from clinical occurrences.
    tags = set()
    for tag, pattern in {
        "adherence": r"残薬|服薬|服用|一包化", "medication": r"処方|変更|開始|中止|減量|増量",
    }.items():
        for match in re.finditer(pattern, body):
            scope = clinical_scope.patient_source_scope(body, match.start(), match.end())
            if scope in ("family", "other", "unknown"):
                event_mentions.append({"event": tag, "scope": scope, "evidence": match[0]})
            else:
                tags.add(tag)
    if event_mentions:
        out["event_mentions"] = event_mentions
    if re.search(r"写真|画像|添付", body):
        tags.add("media_ref")
    if events or tags:
        out["events"] = sorted(set(events) | tags)

    # --- visit date (first M/D preceding 訪問/診察) — a past event ---
    # A planned mention (次回10/5訪問予定) is not a visit that happened:
    # resolving it "past" would fabricate a date one year back.
    planned = list(_next_planned_matches(body))
    for m in _VISIT_DATE.finditer(body):
        # the look-behind stays inside the date's own sentence
        pre = re.split(r"[。．\n!！?？]",
                       body[max(0, m.start() - 6):m.start()])[-1]
        if _PLANNED_BEFORE.search(pre) \
                or _PLANNED_IF.search(body, 0, m.start()) \
                or _PLANNED_AFTER.match(body, m.end()) \
                or any(plan["scheduled"] and plan.start() <= m.start() < plan.end()
                       for plan in _NEXT_PLANNED.finditer(body)):
            continue
        d = _ymd(int(m.group(2)), int(m.group(3)),
                 int(m.group(1)) if m.group(1) else year,
                 posted, "any" if m.group(1) else "past")
        if d:
            out["visit_date"] = d
        break

    # --- next planned date — a FUTURE date ---
    if planned:
        m = planned[0]
        d = _ymd(int(m["month"]), int(m["day"]),
                 int(m["year"]) if m["year"] else year,
                 posted, "any" if m["year"] else "future")
        if d:
            out["next_planned"] = d

    # --- med periods (date ranges, typically regimens) ---
    pers = []
    for pm in _MED_PERIOD.finditer(body):
        sy, a, ey, b = pm.groups()
        # the range must sit in a medication context — a bare date span
        # (shift schedule, visit window) is not a regimen (F07)
        ctx = body[max(0, pm.start() - 60):pm.end() + 60]
        if not _MED_CTX.search(ctx):
            continue
        s, e = _period_dates(a, b, sy, ey, posted, ctx)
        period = {"raw": pm.group(0)}
        if s and e:
            period.update(start=s.isoformat(), end=e.isoformat())
        pers.append(period)
    if pers:
        out["med_periods"] = pers

    # --- medications (name + dose) ---
    meds = []
    for name, dose, unit in extract_schema.MEDICATION_TOKEN.findall(body):
        if unit in ("mg", "μg", "mcg", "g", "mL", "%") and len(name) >= 2:
            meds.append({"name": name, "dose": dose + unit})
    if meds:
        seen, uniq = set(), []
        for x in meds:
            k = x["name"] + x["dose"]
            if k not in seen:
                seen.add(k)
                uniq.append(x)
        out["medications"] = uniq[:20]

    # --- rx change actions (ctx must look medication-related, else a
    # generic 開始/中止 like 食事開始 is a false positive) ---
    acts = []
    for m in re.finditer(r"(変更なし|変更|開始|中止|減量|増量|追加|停止|終了)",
                         body):
        s = m.group(1)
        ctx = body[max(0, m.start() - RX_CTX_BACK):m.end() + 4]
        before = body[max(0, m.start() - 10):m.start()]
        if not (_MED_CTX.search(ctx)
                or re.search(r"[ァ-ヶー・]{3,}$", before)):
            continue
        code = {"変更なし": "no_change", "開始": "start", "追加": "start",
                "中止": "stop", "停止": "stop", "終了": "stop",
                "減量": "decrease", "増量": "increase"}.get(s, "change")
        acts.append({"action": code, "ctx": ctx.strip()})
    if acts:
        out["rx_actions"] = acts[:RX_ACTIONS_CAP]

    # --- vitals ---
    vit, mentions = {}, []
    for k, pat in clinical_values.VITAL_PATTERNS.items():
        for m in re.finditer(pat, body):
            scope = clinical_scope.patient_source_scope(body, m.start(), m.end(), measurement=True)
            values = clinical_values.vital_match_values(k, m)
            mentions.append({"scope": scope, "values": values, "evidence": m.group(0)})
            if scope == "patient":
                for key, value in values.items():
                    vit.setdefault(key, value)
    if vit:
        out["vitals"] = vit
    if any(mention["scope"] != "patient" for mention in mentions):
        out["vital_mentions"] = mentions

    # --- symptoms / adherence flags ---
    # A symptom counts when at least one mention is not directly negated (発熱なし, 吐き気は見られません).
    sym = [s for s in clinical_scope.SYMPTOMS
           if any(not _SYMPTOM_NEGATION.match(body, m.end()) for m in re.finditer(re.escape(s) + ("(?!定)" if s == "不安" else ""), body))]
    if sym:
        out["symptoms"] = sym
    adh = [s for s in _ADHERENCE if s in body]
    if adh:
        out["adherence_flags"] = adh

    # --- requests to other professions ---
    reqs = []
    sentences = list(re.finditer(r"[^。！？!?;；\r\n]+", body))
    for pat, kind in _REQUESTS:
        m = re.search(pat, body)
        if m:
            ctx = next((s[0].strip() for s in sentences
                        if s.start() <= m.start() and m.end() <= s.end()), "")
            if ctx and not any(r["ctx"] == ctx for r in reqs):
                reqs.append({"kind": kind, "ctx": ctx})
    if reqs:
        out["requests"] = reqs[:6]

    # --- actors / voices ---
    actors = set()
    if re.search(rf"{clinical_scope.FAMILY_PERSON}|親族", body):
        actors.add("family")
    if re.search(r"[（(]本人[)）]", body):
        actors.add("patient_voice")
    if re.search(r"[（(][^)）]{1,6}[)）]", body):
        actors.add("dialogue")
    if actors:
        out["actors"] = sorted(actors)

    # --- SOAP sections ---
    soap = [s for s in "SOAP"
            if re.search(r"(?:^|\n|\s)" + s + r"\s*[)）]", body)]
    if soap:
        out["soap"] = soap

    # Recompute the same source scopes when reading an older cached rule row.
    if clinical_urgency.patient_rule_urgency_quotes(body):
        out["urgency"] = "high"
    context = extract_context(body)
    if context:
        out["patient_context"] = context
    return out


def _delete_stale(ledger) -> set:
    """Drop only current-generation stale rows whose source is no longer eligible."""
    if ledger.db.in_transaction:
        raise ValueError("extract_transaction_owned")
    if (_WRITER_STATE[0] or os.stat(inference.__file__).st_mtime_ns != _INFERENCE_SOURCE_STAT
            or inference.CURRENT_VERSION != _INFERENCE_VERSION):
        return set()
    import inference_dependency_loss as dependency_loss
    with ledger.db:
        if not ledger.db.in_transaction:
            ledger.db.execute('BEGIN IMMEDIATE')
        rows = ledger.db.execute(f"""
            SELECT a.artifact_id,COALESCE(a.project_id,m.project_id) AS project_id,a.message_id,{ELIGIBLE_MESSAGE} AS ok
            FROM artifacts a JOIN messages m ON m.message_id=a.message_id
            WHERE a.kind=? AND (a.project_id IS NULL OR a.project_id=m.project_id)
              AND {inference.current_meta_pred('a.meta', target=_INFERENCE_VERSION)}
              AND (json_extract({json_or_null('a.meta')},'$.hash') IS NOT m.content_hash
                   OR COALESCE(json_extract({json_or_null('a.meta')},'$.rule_version'),0)!=?)
            """, (KIND, RULE_VERSION)).fetchall()
        for row in rows:
            if not row['ok']:
                footprint = dependency_loss.capture_tx(ledger.db, row['project_id'], row['message_id'],
                                                      artifact_ids=[row['artifact_id']])
                added = dependency_loss.record_tx(ledger.db, footprint)
                ledger.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (row['artifact_id'],))
                if added:
                    ledger.bump_inference_analysis_epoch_tx(row['project_id'])
    return {r['message_id'] for r in rows if r['ok']}


def run_pending(ledger, *, deadline: float | None = None) -> dict:
    """Extract messages lacking an extract_v1 artifact, and re-extract
    ones whose body changed on the server (content_hash drift — see
    _delete_stale). Returns {done, pids} so rollups can rebuild touched
    patients."""
    if deadline is not None and time.monotonic() >= deadline:
        return {"done": 0, "pids": []}
    if ledger.db.in_transaction:
        raise ValueError("extract_transaction_owned")
    _delete_stale(ledger)
    rows = ledger.db.execute(f"""
      SELECT m.message_id, m.project_id, {dependency_loss.SOURCE_BINDING_COLUMNS}
      FROM messages m
      WHERE {ELIGIBLE_MESSAGE}
        AND NOT EXISTS (SELECT 1 FROM artifacts a
          WHERE a.kind=? AND a.message_id=m.message_id
          {current_extract_pred(error_check=False)}
          AND json_extract(a.meta,'$.rule_version')=?)
      ORDER BY EXISTS (SELECT 1 FROM patients p
                       WHERE p.project_id=m.project_id AND p.is_archived),
               m.posted_at_ts DESC
    """, (KIND, RULE_VERSION)).fetchall()
    done, pids = _extract_rows(ledger, rows, deadline=deadline)
    reconciled, reconciled_pids, reconcile_error = _reconcile_intervention_page(ledger, deadline=deadline)
    result = {"done": done + reconciled, "pids": sorted(pids | reconciled_pids)}
    if reconcile_error:
        result["reconcile_error"] = reconcile_error
    return result


_CHUNK = 200   # rows per commit; keep each writer lock hold bounded


def intervention_source_payload_tx(db, row, binding):
    """Classify one source in the caller-owned transaction without committing.

    ``row`` is a selected occurrence with project, message, body, hash and
    posted timestamp; ``binding`` is that occurrence's selected source fence.
    Return None for a verified zero-candidate source, otherwise a compact
    versioned payload with records or an explicit incomplete coverage state.
    The caller must preserve the binding in the artifact and add the zero marker
    only for None. This function neither commits nor owns the connection.
    """
    if not db.in_transaction:
        raise ValueError("intervention_transaction_required")
    coverage = {"state": "invalid_source", "records_truncated": False,
                "candidates": 0, "unknown": 0, "source_complete": None,
                "source_validation": "unverified"}
    payload = {"version": 1, "records": [], "coverage": coverage}
    if (type(row["project_id"]) is not int or row["project_id"] <= 0
            or type(row["message_id"]) is not int or row["message_id"] <= 0):
        return payload
    # Limit and NUL checks happen in SQLite before fetching potentially large HTML.
    source = db.execute(
        "SELECT body_state,length(body_html),instr(body_html,char(0)),"
        "CASE WHEN length(body_html)<=? AND instr(body_html,char(0))=0 "
        "THEN body_html END FROM messages WHERE project_id=? AND message_id=?",
        (intervention_schedule._MAX_HTML_CHARS, row["project_id"], row["message_id"])).fetchone()
    if source is None or dependency_loss.source_binding(db, row["project_id"], row["message_id"]) != binding:
        return payload
    state, html_length, nul, html = source
    if state != "full":
        coverage["state"] = "deleted" if state == "deleted" else "incomplete"
        return payload
    body = row["body_text"]
    if isinstance(body, str) and len(body) > intervention_schedule._MAX_BODY_CHARS:
        coverage["state"] = "oversize"
        return payload
    if type(html_length) is int and html_length > intervention_schedule._MAX_HTML_CHARS and not nul:
        coverage["state"] = "oversize"
        return payload
    if (not isinstance(body, str) or "\x00" in body or nul or not isinstance(html, str)
            or hashlib.sha256(html.encode()).hexdigest() != row["content_hash"]
            or html_to_text(html) != body):
        return payload
    compact = intervention_schedule.compact_source_record(
        row["project_id"], row["message_id"], row["content_hash"], body, row["posted_at_ts"])
    compact["coverage"]["source_validation"] = "verified"
    # The writer stores an explicit meta marker for a verified zero; a missing
    # content key by itself is never evidence of a completed scan.
    if (compact["coverage"]["state"] == "observed"
            and compact["coverage"]["candidates"] == 0
            and not compact["coverage"]["records_truncated"]):
        return None
    return {"version": 1, **compact}


def verify_v1_insert_tx(db, row, artifact_id, affected, content, meta):
    """Verify the inserted rule artifact on the caller-owned transaction."""
    if not db.in_transaction or affected != 1 or type(artifact_id) is not int:
        raise RuntimeError("extract_v1_insert_unverified")
    stored = db.execute(
        "SELECT kind,project_id,message_id,content,model,meta FROM artifacts WHERE artifact_id=?",
        (artifact_id,)).fetchone()
    expected = (KIND, row["project_id"], row["message_id"], content, "rules-v1", meta)
    if stored is None or tuple(stored) != expected:
        raise RuntimeError("extract_v1_insert_unverified")


def _bounded_reconcile_pred(*, require_binding=False):
    """Guard all JSON evaluation in the same SQLite statement as size checks."""
    binding = (f" AND json_extract({json_or_null('a.meta')},'$.source_binding')=?"
               if require_binding else "")
    return ("CASE WHEN typeof(a.content)='text' AND typeof(a.meta)='text' "
            f"AND length(a.content)<={_RECONCILE_CONTENT_CHARS} "
            f"AND length(a.meta)<={_RECONCILE_META_CHARS} "
            "AND instr(a.content,char(0))=0 AND instr(a.meta,char(0))=0 "
            "THEN CASE WHEN (1 " + current_extract_pred(error_check=False) + ") "
            f"THEN (json_extract({json_or_null('a.meta')},'$.rule_version')=?" + binding +
            f" AND json_extract({json_or_null('a.content')},'$.intervention_schedule.coverage.state') "
            "IN ('invalid_source','oversize')) ELSE 0 END ELSE 0 END")


def _extract_rows(ledger, rows, *, deadline: float | None = None) -> tuple[int, set[int]]:
    """Replace each row's extract_v1 artifact (delete + insert in the same
    transaction), committing every _CHUNK rows; a crash keeps committed
    chunks and the rest keeps its old artifact until the next pass.
    Returns the touched project ids."""
    if ledger.db.in_transaction:
        raise ValueError("extract_transaction_owned")
    pids = set()
    done = 0
    for i in range(0, len(rows), _CHUNK):
        if (writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
                or inference.CURRENT_VERSION != _INFERENCE_VERSION):
            return done, pids
        with ledger.db:
            for r in rows[i:i + _CHUNK]:
                if deadline is not None and time.monotonic() >= deadline:
                    return done, pids
                selected_binding = dependency_loss.source_binding_from_row(r)
                if not isinstance(selected_binding, str) or not selected_binding:
                    continue
                d = extract_message(r["body_text"], r["posted_at"])
                if not ledger.db.in_transaction:
                    ledger.db.execute("BEGIN IMMEDIATE")
                source = ledger.db.execute(
                    f"SELECT 1 FROM messages m WHERE m.message_id=? AND m.project_id=? "
                    f"AND m.content_hash=? AND {ELIGIBLE_MESSAGE}",
                    (r["message_id"], r["project_id"], r["content_hash"])).fetchone()
                if source is None:
                    continue
                if dependency_loss.source_binding(ledger.db, r['project_id'], r['message_id']) != selected_binding:
                    continue
                intervention = intervention_source_payload_tx(ledger.db, r, selected_binding)
                if intervention is not None:
                    d["intervention_schedule"] = intervention
                previous = ledger.db.execute(
                    "SELECT content,meta FROM artifacts WHERE kind=? AND message_id=? "
                    "AND (project_id IS NULL OR project_id=?) AND "
                    + inference.current_meta_pred("meta", target=_INFERENCE_VERSION)
                    + " ORDER BY artifact_id DESC LIMIT 1", (KIND, r["message_id"], r["project_id"])).fetchone()
                previous_meta = loads_dict(previous["meta"]) if previous is not None else {}
                changed = (previous is None or loads_dict(previous["content"]) != d
                           or previous_meta.get("hash") != r["content_hash"]
                           or previous_meta.get("rule_version") != RULE_VERSION)
                replaced_ids = [old[0] for old in ledger.db.execute(
                    'SELECT artifact_id FROM artifacts WHERE kind=? AND message_id=? '
                    'AND (project_id IS NULL OR project_id=?) AND '
                    + inference.current_meta_pred('meta', target=_INFERENCE_VERSION),
                    (KIND, r['message_id'], r['project_id']))]
                footprint = dependency_loss.capture_tx(ledger.db, r['project_id'], r['message_id'], artifact_ids=replaced_ids)
                meta = {inference.VERSION_KEY: _INFERENCE_VERSION, 'hash': r['content_hash'],
                        'source_binding': selected_binding, 'rule_version': RULE_VERSION}
                if intervention is None:
                    meta['intervention_zero'] = 1
                content_json = json.dumps(d, ensure_ascii=False)
                meta_json = json.dumps(meta, ensure_ascii=False)
                new_id = ledger.artifact_add_tx(
                    KIND, content_json, project_id=r['project_id'], message_id=r['message_id'],
                    model='rules-v1', meta=meta)
                affected = ledger.db.execute('SELECT changes()').fetchone()[0]
                verify_v1_insert_tx(ledger.db, r, new_id, affected, content_json, meta_json)
                dependency_loss.record_tx(ledger.db, footprint, replacement_id=new_id)
                ledger.db.execute('DELETE FROM artifacts WHERE kind=? AND message_id=? '
                    'AND (project_id IS NULL OR project_id=?) AND '
                    + inference.current_meta_pred('meta', target=_INFERENCE_VERSION) + ' AND artifact_id!=?',
                    (KIND, r['message_id'], r['project_id'], new_id))
                if changed:
                    ledger.bump_inference_analysis_epoch_tx(r["project_id"])
                pids.add(r["project_id"])
                done += 1
    return done, pids


# A page bounds work even when almost none of the artifacts are rule results.
# The durable job is a cursor, not a claim that any source has been processed.
_RECONCILE_KIND = "intervention_source_reconcile"
_RECONCILE_PAGE = 64
_RECONCILE_CANDIDATES = 8
_RECONCILE_SECONDS = 0.25
_RECONCILE_CURSOR_RESERVE_SECONDS = 0.05
_RECONCILE_IDLE_SECONDS = 60
_RECONCILE_CONTENT_CHARS = 262144
_RECONCILE_META_CHARS = 32768
_RECONCILE_CURSOR_CHARS = 1024
# These source fields participate in the occurrence digest. Refuse oversized
# or malformed values instead of loading unbounded metadata into a repair.
_RECONCILE_SOURCE_GUARD = "typeof(m.content_hash)='text' AND length(m.content_hash)=64 AND " + ' AND '.join(
    f"(m.{name} IS NULL OR (typeof(m.{name})='text' AND length(m.{name})<=32768 "
    f"AND instr(m.{name},char(0))=0))"
    for name in ('sender_name', 'sender_type', 'profession', 'organization', 'posted_at'))
# SQLite's dynamic typing permits blobs and infinity even in INTEGER columns.
# Only JSON-serializable and bounded occurrence values may reach the digest.
_RECONCILE_SOURCE_GUARD += ' AND ' + ' AND '.join(
    f"(m.{name} IS NULL OR typeof(m.{name})='integer' "
    f"OR (typeof(m.{name})='real' AND m.{name} BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308) "
    f"OR (typeof(m.{name})='text' AND length(m.{name})<=32768 AND instr(m.{name},char(0))=0))"
    for name in ('parent_id', 'sender_id', 'posted_at_ts'))


def _reconcile_intervention_page(ledger, *, deadline=None):
    """Preserve completed work and expose bounded reconciliation failures."""
    progress = {'done': 0, 'pids': set()}
    try:
        _reconcile_intervention_work(ledger, progress, deadline=deadline)
    except (ValueError, RuntimeError, sqlite3.Error):
        if ledger.db.in_transaction:
            raise  # Do not disguise a transaction whose owner could not unwind it.
        return progress['done'], progress['pids'], 'intervention_reconcile_failed'
    return progress['done'], progress['pids'], None


def _strict_reconcile_object(raw):
    """Reject ambiguous or malformed artifacts before changing any field."""
    try:
        parsed = strict_json(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _reconcile_one(ledger, row, artifact_id, *, deadline=None):
    """CAS only the intervention result on the reserved writer transaction.

    Existing clinical fields, artifact identity, source occurrence and scope are
    preserved. Caller owns neither this transaction nor any dependent resources.
    """
    db = ledger.db
    if db.in_transaction:
        raise ValueError("extract_transaction_owned")
    selected_binding = dependency_loss.source_binding_from_row(row)
    if not isinstance(selected_binding, str):
        return False
    if deadline is not None and time.monotonic() >= deadline:
        return False
    with db:
        db.execute("BEGIN IMMEDIATE")
        if (writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
                or inference.CURRENT_VERSION != _INFERENCE_VERSION
                or os.stat(inference.__file__).st_mtime_ns != _INFERENCE_SOURCE_STAT
                or deadline is not None and time.monotonic() >= deadline):
            return False
        # LIMIT 2 protects against duplicate/foreign/legacy rows without parsing
        # or materializing the entire artifact history. Ambiguity is deferred.
        ids = db.execute("SELECT artifact_id FROM artifacts WHERE kind=? AND message_id=? LIMIT 2",
                         (KIND, row['message_id'])).fetchall()
        if len(ids) != 1 or ids[0][0] != artifact_id:
            return False
        current = db.execute(
            "SELECT a.kind,a.project_id,a.message_id,a.content,a.model,a.meta,a.created_at,"
            "m.message_id AS source_mid,m.project_id AS source_pid," + dependency_loss.SOURCE_BINDING_COLUMNS + ","
            "CASE WHEN typeof(m.body_html)='text' AND length(m.body_html)<=? "
            "AND instr(m.body_html,char(0))=0 THEN m.body_html END AS source_html "
            "FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
            "JOIN patients p ON p.project_id=m.project_id "
            "WHERE a.artifact_id=? AND a.project_id=m.project_id AND a.kind=? "
            "AND m.project_id=? AND m.message_id=? AND m.body_state='full' "
            "AND typeof(m.body_text)='text' AND length(m.body_text)<=? AND instr(m.body_text,char(0))=0 "
            "AND p.is_archived=0 AND p.fetch_state IS NOT 'forbidden' "
            "AND COALESCE(p.fetch_reason,'') NOT IN ('forbidden','permission_denied','access_revoked') "
            "AND " + _RECONCILE_SOURCE_GUARD + " AND " + _bounded_reconcile_pred(require_binding=True),
            (intervention_schedule._MAX_HTML_CHARS, artifact_id, KIND, row['project_id'],
             row['message_id'], intervention_schedule._MAX_BODY_CHARS, RULE_VERSION, selected_binding)).fetchone()
        if (current is None or current['model'] != 'rules-v1'
                or dependency_loss.source_binding_from_row(current) != selected_binding
                or not isinstance(current['source_html'], str)):
            return False
        content = _strict_reconcile_object(current['content'])
        meta = _strict_reconcile_object(current['meta'])
        if (content is None or meta is None or 'intervention_zero' in meta
                or not isinstance(content.get('intervention_schedule'), dict)
                or not isinstance(content['intervention_schedule'].get('coverage'), dict)
                or content['intervention_schedule']['coverage'].get('state') not in ('invalid_source', 'oversize')
                or content['intervention_schedule']['coverage'].get('source_validation') != 'unverified'):
            return False
        intervention = intervention_source_payload_tx(db, current, selected_binding)
        if intervention is not None and (intervention['coverage']['state'] != 'observed'
                                         or intervention['coverage']['source_validation'] != 'verified'
                                         or intervention['coverage']['records_truncated']
                                         or intervention['coverage']['unknown'] != 0):
            return False
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if intervention is None:
            del content['intervention_schedule']
            meta['intervention_zero'] = 1
        else:
            content['intervention_schedule'] = intervention
        content_json = json.dumps(content, ensure_ascii=False, allow_nan=False)
        meta_json = (json.dumps(meta, ensure_ascii=False, allow_nan=False)
                     if intervention is None else current['meta'])
        if len(content_json) > _RECONCILE_CONTENT_CHARS or len(meta_json) > _RECONCILE_META_CHARS:
            return False
        affected = db.execute(
            "UPDATE artifacts SET content=?,meta=? WHERE artifact_id=? AND kind=? AND project_id=? "
            "AND message_id=? AND content=? AND meta=? AND model IS ? AND created_at IS ?",
            (content_json, meta_json, artifact_id, KIND, row['project_id'], row['message_id'],
             current['content'], current['meta'], current['model'], current['created_at'])).rowcount
        if affected != 1:
            raise RuntimeError("intervention_update_unverified")
        ledger.bump_inference_analysis_epoch_tx(row['project_id'])
        # AFTER triggers, epoch invalidation and SQLite conflict handlers cannot
        # silently substitute another artifact or a different source occurrence.
        stored = db.execute("SELECT kind,project_id,message_id,content,model,meta,created_at "
                            "FROM artifacts WHERE artifact_id=? AND typeof(content)='text' AND length(content)<=? "
                            "AND typeof(meta)='text' AND length(meta)<=?",
                            (artifact_id, _RECONCILE_CONTENT_CHARS, _RECONCILE_META_CHARS)).fetchone()
        expected = (KIND, row['project_id'], row['message_id'], content_json,
                    current['model'], meta_json, current['created_at'])
        source = db.execute(
            "SELECT m.body_html," + dependency_loss.SOURCE_BINDING_COLUMNS +
            " FROM messages m JOIN patients p ON p.project_id=m.project_id "
            "WHERE m.project_id=? AND m.message_id=? AND p.is_archived=0 "
            "AND m.body_state='full' AND typeof(m.body_text)='text' AND length(m.body_text)<=? "
            "AND instr(m.body_text,char(0))=0 AND typeof(m.body_html)='text' "
            "AND length(m.body_html)<=? AND instr(m.body_html,char(0))=0 "
            "AND p.fetch_state IS NOT 'forbidden' "
            "AND COALESCE(p.fetch_reason,'') NOT IN ('forbidden','permission_denied','access_revoked') "
            "AND " + _RECONCILE_SOURCE_GUARD,
            (row['project_id'], row['message_id'], intervention_schedule._MAX_BODY_CHARS,
             intervention_schedule._MAX_HTML_CHARS)).fetchone()
        ids_after = db.execute("SELECT artifact_id FROM artifacts WHERE kind=? AND message_id=? LIMIT 2",
                               (KIND, row['message_id'])).fetchall()
        if (stored is None or tuple(stored) != expected or source is None
                or source['body_html'] != current['source_html']
                or dependency_loss.source_binding_from_row(source) != selected_binding
                or len(ids_after) != 1 or ids_after[0][0] != artifact_id
                or writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
                or inference.CURRENT_VERSION != _INFERENCE_VERSION
                or os.stat(inference.__file__).st_mtime_ns != _INFERENCE_SOURCE_STAT):
            raise RuntimeError("intervention_update_unverified")
    return True


def _reconcile_intervention_work(ledger, progress, *, deadline=None):
    """Recheck bounded invalid sources without retry writes or scope expansion."""
    if ledger.db.in_transaction:
        raise ValueError("extract_transaction_owned")
    if (writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
            or inference.CURRENT_VERSION != _INFERENCE_VERSION
            or os.stat(inference.__file__).st_mtime_ns != _INFERENCE_SOURCE_STAT
            or deadline is not None and time.monotonic() >= deadline):
        return 0, set()
    now = time.monotonic()
    local_deadline = now + _RECONCILE_SECONDS
    deadline = min(deadline, local_deadline) if deadline is not None else local_deadline
    remaining = deadline - now
    if remaining <= 0:
        return
    # Reserve a fraction of the remaining budget for the cursor transaction.
    # A blocking SQLite operation may still overrun it: in that case no late
    # progress write occurs and the next pass safely retries the same prefix.
    work_deadline = deadline - min(_RECONCILE_CURSOR_RESERVE_SECONDS, remaining / 4)
    cursor_row = ledger.db.execute(
        "SELECT CASE WHEN typeof(payload)='text' AND length(payload)<=? AND instr(payload,char(0))=0 "
        "THEN payload END FROM fetch_jobs WHERE kind=? AND project_id=0 AND message_id=0",
        (_RECONCILE_CURSOR_CHARS, _RECONCILE_KIND)).fetchone()
    old_payload = cursor_row[0] if cursor_row is not None else None
    if old_payload is None and cursor_row is None:
        cursor, highwater, not_before = 0, 0, 0
    else:
        state = _strict_reconcile_object(old_payload)
        if (not isinstance(state, dict)
                or set(state) != {'version', 'cursor', 'highwater', 'not_before'}
                or type(state['version']) is not int or state['version'] != 2
                or type(state.get('cursor')) is not int or not 0 <= state['cursor'] < 2**63
                or type(state.get('highwater')) is not int or not 0 <= state['highwater'] < 2**63
                or state['cursor'] > state['highwater']
                or type(state.get('not_before')) not in (int, float)
                or not 0 <= state['not_before'] < 2**63
                or state['highwater'] != 0 and state['not_before'] != 0):
            raise ValueError("intervention_reconcile_cursor_invalid")
        cursor, highwater, not_before = state['cursor'], state['highwater'], state['not_before']
    if cursor == 0 and highwater == 0:
        remaining = not_before - time.time()
        if 0 < remaining <= _RECONCILE_IDLE_SECONDS:
            return
        # Capture a finite pass boundary; newer artifacts wait for the next pass.
        highwater = ledger.db.execute("SELECT COALESCE(MAX(artifact_id),0) FROM artifacts").fetchone()[0]
        if not highwater:
            return
    # Primary-key scan applies LIMIT before any expensive JSON filtering.
    page = ledger.db.execute("SELECT artifact_id,kind FROM artifacts WHERE artifact_id>? AND artifact_id<=? "
                             "ORDER BY artifact_id LIMIT ?", (cursor, highwater, _RECONCILE_PAGE)).fetchall()
    processed, candidates = cursor, 0
    exhausted = True
    for artifact_id, kind in page:
        if time.monotonic() >= work_deadline:
            exhausted = False
            break
        if kind == KIND:
            row = ledger.db.execute(
                "SELECT m.message_id,m.project_id," + dependency_loss.SOURCE_BINDING_COLUMNS +
                " FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
                "JOIN patients p ON p.project_id=m.project_id "
                "WHERE a.artifact_id=? AND p.is_archived=0 AND p.fetch_state IS NOT 'forbidden' "
                "AND typeof(m.body_text)='text' AND length(m.body_text)<=? AND instr(m.body_text,char(0))=0 "
                "AND COALESCE(p.fetch_reason,'') NOT IN ('forbidden','permission_denied','access_revoked') "
                "AND " + _RECONCILE_SOURCE_GUARD + " AND " + _bounded_reconcile_pred(),
                (artifact_id, intervention_schedule._MAX_BODY_CHARS, RULE_VERSION)).fetchone()
            if row is not None:
                if time.monotonic() >= work_deadline:
                    # Nothing has been mutated. Defer this source until the
                    # next pass so a slow read cannot starve later artifacts.
                    processed = artifact_id
                    exhausted = False
                    break
                if candidates >= _RECONCILE_CANDIDATES:
                    exhausted = False
                    break
                candidates += 1
                updated = _reconcile_one(ledger, row, artifact_id, deadline=work_deadline)
                progress['done'] += int(updated)
                if updated:
                    progress['pids'].add(row['project_id'])
                if not updated and time.monotonic() >= work_deadline:
                    # False means this transaction made no update. Leave the
                    # source intact and revisit it in the next fixed pass.
                    processed = artifact_id
                    exhausted = False
                    break
        processed = artifact_id
    completed = exhausted and (len(page) < _RECONCILE_PAGE or processed == highwater)
    if completed:
        next_cursor, next_highwater, next_not_before = 0, 0, time.time() + _RECONCILE_IDLE_SECONDS
    else:
        next_cursor, next_highwater, next_not_before = processed, highwater, 0
    next_state = {'version': 2, 'cursor': next_cursor, 'highwater': next_highwater,
                  'not_before': next_not_before}
    # The old raw payload is the CAS witness; it may have a different key order.
    payload = json.dumps(next_state, separators=(',', ':'))
    if payload != old_payload:
        _commit_reconcile_cursor(ledger.db, cursor_row is None, old_payload, payload, deadline=deadline)


def _commit_reconcile_cursor(db, absent, old_payload, payload, *, deadline):
    """Persist progress only when the same cursor and deadline still hold."""
    if (deadline is not None and time.monotonic() >= deadline
            or writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
            or inference.CURRENT_VERSION != _INFERENCE_VERSION
            or os.stat(inference.__file__).st_mtime_ns != _INFERENCE_SOURCE_STAT):
        return
    with db:
        db.execute('BEGIN IMMEDIATE')
        if (deadline is not None and time.monotonic() >= deadline
                or writer_lock.updater_state() != _WRITER_STATE or _WRITER_STATE[0]
                or inference.CURRENT_VERSION != _INFERENCE_VERSION
                or os.stat(inference.__file__).st_mtime_ns != _INFERENCE_SOURCE_STAT):
            return
        now = time.time()
        if absent:
            db.execute(
                "INSERT OR IGNORE INTO fetch_jobs(kind,project_id,message_id,payload,state,next_try,created_at,updated_at) "
                "VALUES(?,0,0,?,'done',0,?,?)", (_RECONCILE_KIND, payload, now, now))
        else:
            db.execute("UPDATE fetch_jobs SET payload=?,updated_at=? "
                       "WHERE kind=? AND project_id=0 AND message_id=0 AND payload=?",
                       (payload, now, _RECONCILE_KIND, old_payload))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="compatibility only; without --project every patient is processed")
    ap.add_argument("--project", type=int, default=0)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    # writers must hold the same run lock as the scheduled tick — a manual
    # extract running concurrently could double-process pending rows
    lock_fd = None
    if not args.stats:
        lock_fd = acquire_run_lock()
        if lock_fd is None:
            print(json.dumps({"ok": False, "error": "lock_held"}))
            return 3
    try:
        led = LedgerReader(DB) if args.stats else Ledger(DB)
        try:
            q = f"""SELECT m.message_id, m.project_id, {dependency_loss.SOURCE_BINDING_COLUMNS}
           FROM messages m WHERE {ELIGIBLE_MESSAGE}"""
            params: list = []
            if args.project:
                q += " AND m.project_id=?"
                params.append(args.project)
            rows = led.db.execute(q + " ORDER BY m.posted_at_ts", params).fetchall()

            # Same stale handling as the tick path: a hash-drifted artifact
            # (edited body, or a legacy row without a pinned hash) must not
            # count as done — otherwise --all could never re-extract it.
            stale = set() if args.stats else _delete_stale(led)
            done = {r["message_id"] for r in led.db.execute(
                "SELECT message_id FROM artifacts WHERE kind=? "
                "AND message_id IS NOT NULL", (KIND,))} - stale
            todo = [r for r in rows if r["message_id"] not in done]
            if args.stats:
                todo = []
            if args.limit:
                todo = todo[:args.limit]

            n, _ = _extract_rows(led, todo)
            print(json.dumps({"extracted": n, "skipped_existing": len(done),
                              "total_msgs": len(rows)}, ensure_ascii=False))

            # aggregate stats over all extract_v1 artifacts
            import collections
            keys = collections.Counter()
            events = collections.Counter()
            for a in led.db.execute(
                    "SELECT content FROM artifacts WHERE kind=?", (KIND,)):
                d = loads_dict(a["content"])
                if not isinstance(d, dict):
                    continue
                for k in d:
                    if k != "v":
                        keys[k] += 1
                for e in d.get("events", []) if isinstance(d.get("events"), list) else []:
                    if isinstance(e, str):
                        events[e] += 1
            print(json.dumps({"field_coverage": dict(keys),
                              "events": dict(events)}, ensure_ascii=False, indent=1))
            return 0
        finally:
            led.close()
    finally:
        if lock_fd is not None:
            os.close(lock_fd)



if __name__ == "__main__":
    sys.exit(main())
