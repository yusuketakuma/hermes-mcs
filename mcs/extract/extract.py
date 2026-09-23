#!/usr/bin/env python3
"""MCS structured extraction — rule-based v1.

Parses each stored message body into a structured JSON artifact
(kind='extract_v1') designed for reuse: timelines, search, rollups,
and later LLM passes (which can overwrite/extend with kind='extract_v2').

Fields are grounded in the NCGG home-visit pharmacy guide + observed MCS
conventions (visit-date headers, med periods "8/17-9/6", SOAP sections,
vital strings, speaker labels). All work is LOCAL — no data leaves the box.

Usage:
  python3 extract.py --all              # every message lacking extract_v1
  python3 extract.py --project <id>     # one patient
  python3 extract.py --stats            # aggregate stats only
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import _mcs_path  # noqa: F401
from ledger import Ledger, LedgerReader
from mcs_util import acquire_run_lock

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")
KIND = "extract_v1"

# ---------- patterns ----------

_SYMPTOMS = ("発熱", "血尿", "疼痛", "痛み", "嘔気", "吐き気", "下痢", "便秘",
             "発疹", "めまい", "転倒", "むくみ", "浮腫", "咳", "痰", "喘鳴",
             "食欲低下", "食欲不振", "不眠", "せん妄", "誤嚥", "嚥下",
             "出血", "褥瘡", "皮膚", "倦怠", "疲労", "脱水", "低血糖")
_ADHERENCE = ("残薬", "飲み忘れ", "飲み残し", "未使用", "服薬不良",
              "アドヒアランス", "一包化", "お薬カレンダー", "自己注射",
              "残あり", "残なし")
_URGENT = ("至急", "緊急", "早急", "すぐに", "急ぎ", "救急", "搬送")
_REQUESTS = ((r"ご?確認(?:を|お願い|ください|をお願い)", "confirm"),
             (r"(?:ご)?連絡(?:ください|をお願い|いただき)", "contact"),
             (r"共有(?:いたします|します|をお願い|させて)", "share"),
             (r"お願い(?:いたします|します|申し上げ)", "request"),
             (r"教えて(?:ください|いただけ|欲しい)", "ask"),
             (r"報告(?:いたします|します|をお願い)", "report"))

_MED_TOKEN = re.compile(
    r"([ァ-ヶー一-龥][ァ-ヶー一-龥A-Za-z0-9０-９・ー\-]*?)"
    r"(\d+(?:\.\d+)?)\s*(mg|μg|mcg|g|mL|単位|錠|cap|カプセル|包|枚|本)")
_MED_CTX = re.compile(r"薬|処方|内服|外用|点眼|貼付|mg|錠|剤|坐薬|座薬|注射")
# date range like 8/17-9/6 or 9/24-10/7 (med periods)
_MED_PERIOD = re.compile(
    r"(?<![\d/])(?:(\d{4})/)?(\d{1,2}/\d{1,2})"
    r"\s*[-–~〜]\s*(?:(\d{4})/)?(\d{1,2}/\d{1,2})(?!\d)")
RULE_VERSION = 2
_VISIT_DATE = re.compile(
    r"(?:(\d{4})[-/年])?(\d{1,2})[/月](\d{1,2})日?[　\s]*(?:\(|（)?[月火水木金土日]?"
    r"(?:\)|）)?[　\s]*(?:訪問|診察|往診)")
_VITAL_PATTERNS = {
    "bt":   r"(?:体温|BT)[:：]?\s*(\d{2}(?:\.\d)?)\s*[℃度]?",
    "hr":   r"(?:脈拍|HR|心拍数?)[:：]?\s*(\d{2,3})",
    "rr":   r"(?:呼吸(?:数)?|RR)[:：]?\s*(\d{1,2})",
    "sbp":  r"(?:血圧|BP)[:：]?\s*(\d{2,3})\s*[/／]\s*(\d{2,3})",
    "spo2": r"(?:SpO2|SPO2|spo2|酸素)[:：]?\s*(\d{2,3})\s*[%％]?",
    "bs":   r"(?:血糖|BS|Glu)[:：]?\s*(\d{2,3})",
}


def _ymd(month: int, day: int, year: int,
         posted: datetime | None = None,
         mode: str = "past") -> str | None:
    """Resolve an M/D (no year) against the post date. `mode`:
      'past'   — event already happened (visit_date): never after posted
      'future' — planned date (next_planned): never before posted
      'any'    — explicit year or neutral
    Year wraps at Dec/Jan are resolved by shifting the year, not by
    guessing (Oracle B26)."""
    for y in (year, year + 1, year - 1):
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
    """Structured view of one message. Missing fields are simply absent."""
    out: dict = {"v": 1}
    year = 2026
    posted = None
    try:
        posted = datetime.fromisoformat(posted_at)
        year = posted.year
    except (ValueError, TypeError):
        pass

    # --- events ---
    ev = set()
    if re.search(r"訪問(?:し|した|時|実施|致し)", body):
        ev.add("visit")
    if re.search(r"診察|往診|診療", body):
        ev.add("exam")
    if re.search(r"入院|退院|搬送|急性期", body):
        ev.add("admission")
    if re.search(r"看取り|緩和|終末期|ACP|オピオイド|モルヒネ|逝去", body):
        ev.add("eol")
    if re.search(r"デイ|ショートステイ|ケアプラン|要介護|介護度", body):
        ev.add("care")
    if re.search(r"写真|画像|添付", body):
        ev.add("media_ref")
    if re.search(r"残薬|服薬|服用|一包化", body):
        ev.add("adherence")
    if re.search(r"処方|変更|開始|中止|減量|増量", body):
        ev.add("medication")
    if ev:
        out["events"] = sorted(ev)

    # --- visit date (first M/D preceding 訪問/診察) — a past event ---
    m = _VISIT_DATE.search(body)
    if m:
        d = _ymd(int(m.group(2)), int(m.group(3)),
                 int(m.group(1)) if m.group(1) else year,
                 posted if not m.group(1) else None, "past")
        if d:
            out["visit_date"] = d

    # --- next planned date — a FUTURE date ---
    m = re.search(r"次回.{0,8}?(\d{1,2})[/月](\d{1,2})", body)
    if m:
        d = _ymd(int(m.group(1)), int(m.group(2)), year, posted, "future")
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
    for name, dose, unit in _MED_TOKEN.findall(body):
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
        ctx = body[max(0, m.start() - 14):m.end() + 4]
        before = body[max(0, m.start() - 10):m.start()]
        if not (_MED_CTX.search(ctx)
                or re.search(r"[ァ-ヶー・]{3,}$", before)):
            continue
        code = {"変更なし": "no_change", "開始": "start", "追加": "start",
                "中止": "stop", "停止": "stop", "終了": "stop",
                "減量": "decrease", "増量": "increase"}.get(s, "change")
        acts.append({"action": code, "ctx": ctx.strip()})
    if acts:
        out["rx_actions"] = acts[:10]

    # --- vitals ---
    vit = {}
    for k, pat in _VITAL_PATTERNS.items():
        m = re.search(pat, body)
        if m:
            if k == "sbp":
                vit["sbp"], vit["dbp"] = int(m.group(1)), int(m.group(2))
            else:
                vit[k] = float(m.group(1)) if "." in m.group(1) \
                    else int(m.group(1))
    if vit:
        out["vitals"] = vit

    # --- symptoms / adherence flags ---
    sym = [s for s in _SYMPTOMS if s in body]
    if sym:
        out["symptoms"] = sym
    adh = [s for s in _ADHERENCE if s in body]
    if adh:
        out["adherence_flags"] = adh

    # --- requests to other professions ---
    reqs = []
    for pat, kind in _REQUESTS:
        m = re.search(pat, body)
        if m:
            reqs.append({"kind": kind,
                         "ctx": body[max(0, m.start() - 20):m.end() + 10]
                             .replace("\n", " ").strip()[:80]})
    if reqs:
        out["requests"] = reqs[:6]

    # --- actors / voices ---
    actors = set()
    if re.search(r"ご家族|家族|娘|息子|奥さ|ご主人|姉|妹|兄|弟|親族|妻|夫",
                 body):
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

    # --- urgency ---
    if any(u in body for u in _URGENT):
        out["urgency"] = "high"
    return out


def _delete_stale(ledger) -> int:
    """Drop extract_v1 artifacts whose pinned hash no longer matches the
    live body (content_hash drift — MCS allows edits). NULL meta hashes
    (legacy CLI artifacts) count as stale — a missing hash must never
    shield a changed body (Oracle B19). Returns the dropped row count."""
    stale = ledger.db.execute("""
      SELECT DISTINCT a.message_id FROM artifacts a
      JOIN messages m ON m.message_id=a.message_id
      WHERE a.kind=? AND CASE WHEN json_valid(a.meta) THEN
        json_extract(a.meta,'$.hash') IS NULL
        OR json_extract(a.meta,'$.hash') != m.content_hash
        OR COALESCE(json_extract(a.meta,'$.rule_version'),0) != ?
      ELSE 0 END
    """, (KIND, RULE_VERSION)).fetchall()
    for r in stale:
        ledger.db.execute(
            "DELETE FROM artifacts WHERE kind=? AND message_id=?",
            (KIND, r["message_id"]))
    ledger.db.commit()
    return len(stale)


def run_pending(ledger) -> dict:
    """Extract messages lacking an extract_v1 artifact, and re-extract
    ones whose body changed on the server (content_hash drift — see
    _delete_stale). Returns {done, pids} so rollups can rebuild touched
    patients."""
    _delete_stale(ledger)
    rows = ledger.db.execute("""
      SELECT m.message_id, m.project_id, m.body_text, m.posted_at,
             m.content_hash
      FROM messages m
      WHERE m.body_text IS NOT NULL AND m.body_text != ''
        AND (m.body_state IS NULL OR m.body_state='full')
        AND m.message_id NOT IN (SELECT message_id FROM artifacts
                                 WHERE kind=? AND message_id IS NOT NULL)
      ORDER BY m.posted_at_ts DESC
    """, (KIND,)).fetchall()
    pids = set()
    for r in rows:
        d = extract_message(r["body_text"], r["posted_at"])
        ledger.artifact_add(KIND, json.dumps(d, ensure_ascii=False),
                            project_id=r["project_id"],
                            message_id=r["message_id"], model="rules-v1",
                            meta={"hash": r["content_hash"],
                                  "rule_version": RULE_VERSION})
        pids.add(r["project_id"])
    return {"done": len(rows), "pids": sorted(pids)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
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
    except Exception:
        if lock_fd is not None:
            os.close(lock_fd)
        raise
    q = """SELECT m.message_id, m.project_id, m.body_text, m.posted_at,
                  m.content_hash
           FROM messages m WHERE m.body_text IS NOT NULL
             AND m.body_text != ''
             AND (m.body_state IS NULL OR m.body_state='full')"""
    params: list = []
    if args.project:
        q += " AND m.project_id=?"
        params.append(args.project)
    rows = led.db.execute(q + " ORDER BY m.posted_at_ts", params).fetchall()

    if not args.stats:
        # Same stale cleanup as the tick path: a hash-drifted artifact
        # (edited body, or a legacy row without a pinned hash) must not
        # count as done — otherwise --all could never re-extract it.
        _delete_stale(led)
    done = {r["message_id"] for r in led.db.execute(
        "SELECT message_id FROM artifacts WHERE kind=? "
        "AND message_id IS NOT NULL", (KIND,))}
    todo = [r for r in rows if r["message_id"] not in done]
    if args.stats:
        todo = []
    if args.limit:
        todo = todo[:args.limit]

    if args.stats:
        pass  # fall through to stats below after optional extraction
    n = 0
    for r in todo:
        d = extract_message(r["body_text"], r["posted_at"])
        led.artifact_add(KIND, json.dumps(d, ensure_ascii=False),
                       project_id=r["project_id"],
                       message_id=r["message_id"], model="rules-v1",
                       meta={"hash": r["content_hash"],
                             "rule_version": RULE_VERSION})
        n += 1
    print(json.dumps({"extracted": n, "skipped_existing": len(done),
                      "total_msgs": len(rows)}, ensure_ascii=False))

    # aggregate stats over all extract_v1 artifacts
    import collections
    keys = collections.Counter()
    events = collections.Counter()
    for a in led.db.execute(
            "SELECT content FROM artifacts WHERE kind=?", (KIND,)):
        try:
            d = json.loads(a["content"])
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        for k in d:
            if k != "v":
                keys[k] += 1
        for e in d.get("events", []):
            events[e] += 1
    print(json.dumps({"field_coverage": dict(keys),
                      "events": dict(events)}, ensure_ascii=False, indent=1))
    led.close()
    if lock_fd is not None:
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
