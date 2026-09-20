#!/usr/bin/env python3
"""MCS LLM extraction — extract_v2 layer via local llama.cpp (Qwen3.5-9B).

Adds what rule extraction (extract_v1) cannot: dose-less medication names,
negated symptoms ("食欲低下なし"), request targets by profession, and a
one-line summary. Output goes to artifacts kind='extract_llm' alongside
extract_v1 (provenance kept — rules and model never overwrite each other).

Endpoint: http://127.0.0.1:8080/v1/chat/completions (OpenAI-compatible,
enable_thinking=false for clean JSON). Fully local — no data leaves the box.

Incremental: run_pending() extracts only messages lacking a current
extract_llm artifact (content_hash tracked in meta). run_check calls it
with a time budget each tick so the backlog drains gradually.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger import Ledger
from mcs_util import NoRedirect, acquire_run_lock, no_proxy_opener

HOME = os.path.expanduser("~/.mcs")
DB = os.path.join(HOME, "data", "ledger.db")
KIND = "extract_llm"
ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = "Qwen3.5-9B"
TIMEOUT = 90

_PROMPT = """あなたは在宅医療の多職種チャット記録を構造化する抽出器です。
以下のメッセージ本文からJSONのみを出力してください。不明な項目は省略し、推測で補わないでください。

出力キー(全て任意):
- "meds": 薬剤名の配列 [{"name": "薬剤名", "dose": "40mg"等 または null, "action": "start|stop|change|decrease|increase|none" または null}] — 用量表記が無い薬剤も拾うこと
- "symptoms": 症状・状態変化の配列 [{"text": "症状名", "negated": false}] — 「〜なし」「低下なし」等の否定文脈は negated=true
- "events": 該当するもの ["visit","exam","admission","discharge","transfer","fall","eol","care","family_contact","other"]
- "requests": [{"to": "医師|看護師|薬剤師|ケアマネ|介護士|家族|不明", "action": "依頼内容を15字以内で"}]
- "vitals": 数値のみ {"bt": 36.5, "hr": 76, "rr": 18, "sbp": 134, "dbp": 68, "spo2": 97, "bs": 120}
- "summary": この投稿の要点を30字以内で
- "points": この投稿で次に知るべき要点の配列(最大3件、各25字以内 — 依頼・処方変更・異常値・今後の予定を優先)
- "urgency": "high" または "routine" (至急・緊急・救急・搬送等ならhigh)

本文:
<<<
%s
>>>
JSON:"""

# loopback-only opener: no proxy and no redirect may route message bodies.
_OPENER = no_proxy_opener(NoRedirect)

_VITAL_KEYS = {"bt", "hr", "rr", "sbp", "dbp", "spo2", "bs"}
_RX_ACTS = {"start", "stop", "change", "decrease", "increase", "none", None}
_EVENTS = {"visit", "exam", "admission", "discharge", "transfer", "fall",
           "eol", "care", "family_contact", "other"}


def _validate(d: dict) -> dict | None:
    """Schema check — a malformed LLM output must NOT become a success
    artifact (it poisons downstream rollups, Oracle B20). Returns the
    cleaned dict or None."""
    import math
    out = {}
    try:
        if "meds" in d:
            if not isinstance(d["meds"], list):
                return None
            out["meds"] = [m for m in d["meds"]
                           if isinstance(m, dict)
                           and isinstance(m.get("name"), str)
                           and m.get("action") in _RX_ACTS
                           and (m.get("dose") is None
                                or isinstance(m.get("dose"), str))]
        if "symptoms" in d:
            if not isinstance(d["symptoms"], list):
                return None
            if any(not isinstance(s, dict)
                   or not isinstance(s.get("text"), str)
                   or ("negated" in s
                       and type(s.get("negated")) is not bool)
                   for s in d["symptoms"]):
                return None
            out["symptoms"] = [
                {"text": s["text"], "negated": s.get("negated", False)}
                for s in d["symptoms"]
            ]
        if "events" in d:
            if not isinstance(d["events"], list):
                return None
            out["events"] = [e for e in d["events"] if e in _EVENTS]
        if "requests" in d:
            if not isinstance(d["requests"], list):
                return None
            out["requests"] = [r for r in d["requests"]
                               if isinstance(r, dict)
                               and (r.get("to") is None
                                    or isinstance(r.get("to"), str))
                               and (r.get("action") is None
                                    or isinstance(r.get("action"), str))]
        if "vitals" in d:
            v = d["vitals"]
            if not isinstance(v, dict):
                return None
            if any(k in v and type(v[k]) not in (int, float)
                   for k in _VITAL_KEYS):
                return None
            vit = {k: float(v[k]) for k in _VITAL_KEYS
                   if k in v and type(v[k]) in (int, float)
                   and math.isfinite(v[k])}
            if vit:
                out["vitals"] = vit
        for k in ("summary", "urgency"):
            if k in d:
                if k == "urgency" and d[k] not in ("high", "routine"):
                    continue
                if k == "summary" and not isinstance(d[k], str):
                    return None
                out[k] = d[k]
        if "points" in d:
            if not isinstance(d["points"], list):
                return None
            out["points"] = [str(p)[:40] for p in d["points"]
                             if isinstance(p, str) and p.strip()][:3]
    except (TypeError, ValueError, OverflowError):
        return None
    return out


def llm_extract(body: str) -> dict | None:
    """One message -> validated structured dict, or None on failure."""
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": _PROMPT % body[:3000]}],
            "max_tokens": 900,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            out = json.load(r)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    if not isinstance(out, dict) or not isinstance(out.get("choices"), list) \
            or not out["choices"] or not isinstance(out["choices"][0], dict):
        return None
    message = out["choices"][0].get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("content") or ""
    if not isinstance(text, str):
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return _validate(d) if isinstance(d, dict) else None


def _llm_up() -> bool:
    try:
        with _OPENER.open("http://127.0.0.1:8080/v1/models", timeout=3):
            pass
        return True
    except OSError:
        return False


def _fail(ledger, r, attempts: int):
    """Record an error artifact with retry metadata — a transient failure
    must retry later, a permanent one must stop (Oracle B21)."""
    ledger.artifact_add(
        KIND, json.dumps({"_model": MODEL, "_error": True},
                         ensure_ascii=False),
        project_id=r["project_id"], message_id=r["message_id"],
        model=MODEL,
        meta={"hash": r["content_hash"], "error": True,
              "attempts": attempts + 1,
              "next_try": time.time() + min(3600, 300 * (attempts + 1))})


def _clear_error(ledger, mid: int):
    ledger.db.execute(
        "DELETE FROM artifacts WHERE kind=? AND message_id=? "
        "AND json_valid(meta) AND json_extract(meta,'$.error')=1",
        (KIND, mid))
    ledger.db.commit()


def run_pending(ledger, limit: int = 20, budget_s: float = 180) -> dict:
    """Extract up to `limit` pending/stale messages within budget_s.
    Returns {'done': n, 'left': n, 'failed': n}."""
    deadline = time.monotonic() + budget_s
    # Any artifact for an older body is stale, including retry state.
    ledger.db.execute("""
      DELETE FROM artifacts
      WHERE kind=? AND message_id IN (SELECT message_id FROM messages)
        AND CASE WHEN json_valid(meta) THEN
          json_extract(meta,'$.hash') IS NULL
          OR json_extract(meta,'$.hash') !=
             (SELECT m.content_hash FROM messages m
              WHERE m.message_id=artifacts.message_id)
        ELSE 0 END
    """, (KIND,))
    ledger.db.commit()
    # pending = no artifact, plus retriable error artifacts whose
    # backoff expired (attempts < 5, next_try <= now)
    rows = ledger.db.execute("""
      SELECT m.message_id, m.project_id, m.body_text, m.content_hash,
             MAX(COALESCE(json_extract(e.meta,'$.attempts'),0)) AS attempts
      FROM messages m
      LEFT JOIN artifacts e ON e.message_id=m.message_id AND e.kind=?
        AND CASE WHEN json_valid(e.meta)
                 THEN json_extract(e.meta,'$.error')=1 ELSE 0 END
      WHERE m.body_text IS NOT NULL AND m.body_text != ''
        AND NOT EXISTS (SELECT 1 FROM artifacts bad
                        WHERE bad.kind=? AND bad.message_id=m.message_id
                          AND NOT json_valid(bad.meta))
        AND NOT EXISTS (SELECT 1 FROM artifacts a
                        WHERE a.kind=? AND a.message_id=m.message_id
                          AND CASE WHEN json_valid(a.meta) THEN
                            json_extract(a.meta,'$.error') IS NOT 1
                            AND json_extract(a.meta,'$.hash')=m.content_hash
                          ELSE 0 END)
      GROUP BY m.message_id
      HAVING attempts < 5
         AND COALESCE(MAX(json_extract(e.meta,'$.next_try')),0)
             <= unixepoch()
      ORDER BY m.posted_at_ts DESC
    """, (KIND, KIND, KIND)).fetchall()
    done = failed = 0
    done_pids = set()
    for r in rows[:limit]:
        if time.monotonic() > deadline:
            break
        d = llm_extract(r["body_text"])
        if d is None:
            failed += 1
            if not _llm_up():
                break  # endpoint down — don't burn budget retrying
            _clear_error(ledger, r["message_id"])
            _fail(ledger, r, r["attempts"])
            continue
        _clear_error(ledger, r["message_id"])
        d["_model"] = MODEL
        ledger.artifact_add(KIND, json.dumps(d, ensure_ascii=False),
                            project_id=r["project_id"],
                            message_id=r["message_id"], model=MODEL,
                            meta={"hash": r["content_hash"]})
        done += 1
        done_pids.add(r["project_id"])
    left = ledger.db.execute("""
      SELECT COUNT(*) FROM messages m
      WHERE m.body_text IS NOT NULL AND m.body_text != ''
        AND NOT EXISTS (SELECT 1 FROM artifacts a
                        WHERE a.kind=? AND a.message_id=m.message_id
                          AND CASE WHEN json_valid(a.meta) THEN
                            json_extract(a.meta,'$.error') IS NOT 1
                            AND json_extract(a.meta,'$.hash')=m.content_hash
                          ELSE 0 END)
    """, (KIND,)).fetchone()[0]
    return {"done": done, "failed": failed, "left": left,
            "pids": sorted(done_pids)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--budget", type=float, default=180)
    ap.add_argument("--all", action="store_true",
                    help="drain the whole backlog (ignores budget pacing)")
    args = ap.parse_args()
    # same single-writer lock as the scheduled tick — run_pending() writes
    # artifacts and a concurrent tick must not double-process the backlog
    lock_fd = acquire_run_lock()
    if lock_fd is None:
        print(json.dumps({"ok": False, "error": "lock_held"}))
        return 3
    try:
        l = Ledger(DB)
    except Exception:
        os.close(lock_fd)
        raise
    if args.all:
        total = {"done": 0, "failed": 0, "left": 0}
        while True:
            r = run_pending(l, limit=50, budget_s=3600)
            total["done"] += r["done"]; total["failed"] += r["failed"]
            total["left"] = r["left"]
            print(json.dumps(r, ensure_ascii=False), flush=True)
            if r["done"] == 0 or r["left"] == 0:
                break
        print(json.dumps(total, ensure_ascii=False))
    else:
        print(json.dumps(run_pending(l, args.limit, args.budget),
                         ensure_ascii=False))
    l.close()
    os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
