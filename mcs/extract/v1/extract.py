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
import time
from datetime import datetime

# flat-import bootstrap: put mcs/ root on sys.path, then _mcs_path
# registers every first-level subdir as an import root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
import _mcs_path  # noqa: F401
from ledger import Ledger, LedgerReader
from mcs_util import acquire_run_lock

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")
KIND = "extract_v1"

# ---------- patterns ----------

_SYMPTOMS = ("発熱", "お熱", "高熱", "血尿", "疼痛", "痛み", "嘔気", "吐き気",
             "嘔吐", "下痢", "便秘", "発疹", "めまい", "転倒", "むくみ", "浮腫",
             "咳", "痰", "喘鳴", "息苦し", "呼吸困難", "呼吸停止",
             "意識消失", "意識障害", "意識低下", "意識がない", "意識なし",
             "意識不明", "反応がない", "食欲低下", "食欲不振", "不眠", "せん妄",
             "誤嚥", "嚥下", "出血", "褥瘡", "皮膚", "倦怠", "疲労", "脱水",
             "低血糖")
_ADHERENCE = ("残薬", "飲み忘れ", "飲み残し", "未使用", "服薬不良",
              "アドヒアランス", "一包化", "お薬カレンダー", "自己注射",
              "残あり", "残なし")
_URGENT = re.compile(
    r"至急|緊急(?:搬送|受診|対応)?|早急|すぐに|急ぎ|救急(?:搬送|受診)?|搬送")
_URGENT_CLAUSES = re.compile(
    r"[。！？!?;\n]|しかし|ただし|けれど(?:も)?|"
    r"(?<=ない)が|(?<=ません)が|(?<=した)が|(?<=ました)が|(?<=です)が")
# A past/future word negates only when it directly modifies the urgent
# word (「来週、緊急受診」); 「明日の訪問前に至急」 or 「昨日の採血で…、至急」 stay current,
# and 「昨日から/より」「明日までに」 bound a current need, so they never match here.
_URGENT_NONCURRENT = re.compile(
    r"(?:昨日|一昨日|先日|先週|先月|昨年|以前|過去|"
    r"明日|明後日|来週|来月|来年|後日|今度|\d+日前|\d+日後)"
    r"(?:には|にも|は|に|も)?[、,\s　]*$")
# A request in the same clause stays current even after a time word
# (「明日、至急ご連絡ください」); only plans/reports are cancelled.
_URGENT_REQUEST = re.compile(r"ください|下さい|お願い|ほしい|欲しい|願います")
_URGENT_INACTIVE = re.compile(
    r"^(?:性|(?:の|な|に)?(?:対応|連絡|確認|受診|処置|搬送|要請))?"
    r"の?(?:は|も|が|を)?(?:で(?:は)?|じゃ|し|する)?"
    r"(?:ありません|ません|ない|なく|なし|"
    r"不要(?!では(?:ありません|ない))|必要(?:は|が)?(?:ありません|ない|なし)|"
    r"済み|完了|(?:た|ました|された|されました)"
    # A past ending closes the phrase; 「ただちに」「たすけて」 are not past tense.
    r"(?=$|[、,]|ので|ため|から|けど|けれど|が|と))")
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
RULE_VERSION = 7
_VISIT_DATE = re.compile(
    r"(?:(\d{4})[-/年])?(\d{1,2})[/月](\d{1,2})日?[　\s]*(?:\(|（)?[月火水木金土日]?"
    r"(?:\)|）)?[　\s]*(?:訪問|診察|往診)")
_PLANNED_BEFORE = re.compile(r"次回|予定(?!通り|どおり)|明日|明後日|今度|来週")
# 予定通りなら (conditional) right before the date, beyond the 6-char window
_PLANNED_IF = re.compile(r"予定(?:通り|どおり)なら[　\s、,，]*$")
_PLANNED_AFTER = re.compile(r"[　\s]*(?:の|を)?[　\s]*(?:予定|します|いたします|致します)")
_VITAL_PATTERNS = {
    "bt":   r"(?:体温|BT)[:：は]?\s*(\d{2}(?:\.\d)?)\s*[℃度]?",
    # 不整脈 is a finding, not a pulse label ("不整脈は20回" ≠ HR 20)
    "hr":   r"(?:脈拍|(?<!静)(?<!動)(?<!整)脈|HR|心拍数?)[:：は]?\s*(\d{2,3})",
    "rr":   r"(?:呼吸(?:数)?|RR)[:：は]?\s*(\d{1,2})",
    "sbp":  r"(?:血圧|BP)[:：は]?\s*(\d{2,3})\s*[/／]\s*(\d{2,3})",
    # a number followed by a flow unit is oxygen delivery (酸素10L),
    # never a saturation reading
    "spo2": r"(?:SpO2|Spo2|SPO2|spo2|酸素)[:：は]?\s*(\d{2,3})(?![\d.])"
            r"(?!\s*(?:[LＬlℓ]|リットル))\s*[%％]?",
    "bs":   r"(?:血糖|BS|Glu)[:：は]?\s*(\d{2,3})",
}


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
    """Structured view of one message. Missing fields are simply absent."""
    out: dict = {"v": 1}
    year = None
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
    if re.search(r"看取り|緩和|終末期|ACP|オピオイド|モルヒネ|逝去|"
                 r"お亡くなり|亡くなっ|死亡確認|息を引き取|心肺停止", body):
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
    # A planned mention (次回10/5訪問予定) is not a visit that happened:
    # resolving it "past" would fabricate a date one year back.
    for m in _VISIT_DATE.finditer(body):
        # the look-behind stays inside the date's own sentence
        pre = re.split(r"[。．\n!！?？]",
                       body[max(0, m.start() - 6):m.start()])[-1]
        if _PLANNED_BEFORE.search(pre) \
                or _PLANNED_IF.search(body, 0, m.start()) \
                or _PLANNED_AFTER.match(body, m.end()):
            continue
        d = _ymd(int(m.group(2)), int(m.group(3)),
                 int(m.group(1)) if m.group(1) else year,
                 posted, "any" if m.group(1) else "past")
        if d:
            out["visit_date"] = d
        break

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
    # ponytail: explicit lexical scope only; implicit tense needs grounded LLM extraction.
    for clause in _URGENT_CLAUSES.split(body):
        # Adjacent urgent words (「すぐに搬送」「緊急で搬送」) share one scope.
        spans: list[list[int]] = []
        for match in _URGENT.finditer(clause):
            if spans and clause[spans[-1][1]:match.start()].strip() in ("", "で"):
                spans[-1][1] = match.end()
            else:
                spans.append([match.start(), match.end()])
        cursor = 0
        for i, (start, end) in enumerate(spans):
            before = clause[cursor:start]
            cursor = end
            stop = spans[i + 1][0] if i + 1 < len(spans) else len(clause)
            if ((_URGENT_NONCURRENT.search(before)
                 and not _URGENT_REQUEST.search(clause[end:]))
                    or _URGENT_INACTIVE.match(clause[end:stop].strip())):
                continue
            out["urgency"] = "high"
            break
        if out.get("urgency") == "high":
            break
    return out


_STALE = """CASE WHEN json_valid(a.meta) THEN
        json_extract(a.meta,'$.hash') IS NULL
        OR json_extract(a.meta,'$.hash') != m.content_hash
        OR COALESCE(json_extract(a.meta,'$.rule_version'),0) != ?
      ELSE 0 END"""
_EXTRACTABLE = """m.body_text IS NOT NULL AND m.body_text != ''
        AND (m.body_state IS NULL OR m.body_state='full')"""


def _delete_stale(ledger) -> set:
    """Find extract_v1 artifacts whose pinned hash no longer matches the
    live body (content_hash drift — MCS allows edits) or whose rule
    version is old. NULL meta hashes (legacy CLI artifacts) count as stale
    — a missing hash must never shield a changed body (Oracle B19).
    Stale rows whose body can no longer be extracted are dropped here; the
    rest are returned and kept until _extract_rows replaces them, so a
    deadline cut never leaves a message without extract_v1."""
    stale = ledger.db.execute(f"""
      SELECT DISTINCT a.message_id, {_EXTRACTABLE} AS ok FROM artifacts a
      JOIN messages m ON m.message_id=a.message_id
      WHERE a.kind=? AND {_STALE}
    """, (KIND, RULE_VERSION)).fetchall()
    with ledger.db:
        for r in stale:
            if not r["ok"]:
                ledger.db.execute(
                    "DELETE FROM artifacts WHERE kind=? AND message_id=?",
                    (KIND, r["message_id"]))
    return {r["message_id"] for r in stale if r["ok"]}


def run_pending(ledger, *, deadline: float | None = None) -> dict:
    """Extract messages lacking an extract_v1 artifact, and re-extract
    ones whose body changed on the server (content_hash drift — see
    _delete_stale). Returns {done, pids} so rollups can rebuild touched
    patients."""
    if deadline is not None and time.monotonic() >= deadline:
        return {"done": 0, "pids": []}
    _delete_stale(ledger)
    rows = ledger.db.execute(f"""
      SELECT m.message_id, m.project_id, m.body_text, m.posted_at,
             m.content_hash
      FROM messages m
      WHERE {_EXTRACTABLE}
        AND (m.message_id NOT IN (SELECT message_id FROM artifacts
                                  WHERE kind=? AND message_id IS NOT NULL)
             OR EXISTS (SELECT 1 FROM artifacts a
                        WHERE a.kind=? AND a.message_id=m.message_id
                          AND {_STALE}))
      ORDER BY m.posted_at_ts DESC
    """, (KIND, KIND, RULE_VERSION)).fetchall()
    done, pids = _extract_rows(ledger, rows, deadline=deadline)
    return {"done": done, "pids": sorted(pids)}


_CHUNK = 200   # rows per commit: a RULE_VERSION bump re-extracts ~17k rows,
               # but the drainer's per-write lock must not wait long


def _extract_rows(ledger, rows, *, deadline: float | None = None) -> tuple[int, set[int]]:
    """Replace each row's extract_v1 artifact (delete + insert in the same
    transaction), committing every _CHUNK rows; a crash keeps committed
    chunks and the rest keeps its old artifact until the next pass.
    Returns the touched project ids."""
    pids = set()
    done = 0
    for i in range(0, len(rows), _CHUNK):
        with ledger.db:
            for r in rows[i:i + _CHUNK]:
                if deadline is not None and time.monotonic() >= deadline:
                    return done, pids
                d = extract_message(r["body_text"], r["posted_at"])
                ledger.db.execute(
                    "DELETE FROM artifacts WHERE kind=? AND message_id=?",
                    (KIND, r["message_id"]))
                ledger.artifact_add_tx(
                    KIND, json.dumps(d, ensure_ascii=False),
                    project_id=r["project_id"], message_id=r["message_id"],
                    model="rules-v1", meta={"hash": r["content_hash"],
                                            "rule_version": RULE_VERSION})
                pids.add(r["project_id"])
                done += 1
    return done, pids


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

    _extract_rows(led, todo)
    n = len(todo)
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
