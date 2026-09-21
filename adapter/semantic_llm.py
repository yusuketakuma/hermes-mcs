#!/usr/bin/env python3
"""Phase J content production — local-LLM fact extraction and summary
drafting.

The model gets no tools and no send capability; the caller is expected
to clip timeouts to the job's remaining budget. Everything produced
here is a *candidate* — semantic_audit decides what may be published.
"""
import http.client
import json
import re
import time
import urllib.error
import urllib.request

from mcs_util import NoRedirect, no_proxy_opener
import semantic_jev as jev
from semantic_model import (CLAIM_KINDS, CLAIM_SECTIONS, FACT_KINDS,
                            FACT_STATUSES, POLARITIES,
                            PROMPT_CHAR_LIMIT, SCHEMA_VERSION)

LLM_ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
LLM_MODEL = "Qwen3.5-9B"
LLM_TIMEOUT = 90
LLM_OPENER = no_proxy_opener(NoRedirect)


def llm_chat(prompt: str, timeout: float = LLM_TIMEOUT) -> str | None:
    """One local-llama.cpp chat call. The model gets no tools and no
    send capability; loopback-only opener, no proxy, no redirect
    (INV-13, §15.3). Returns raw text or None."""
    req = urllib.request.Request(
        LLM_ENDPOINT,
        data=json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1400, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with LLM_OPENER.open(req, timeout=timeout) as r:
            out = json.load(r)
    except (OSError, urllib.error.URLError, json.JSONDecodeError,
            http.client.HTTPException):
        return None
    try:
        text = out["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return text if isinstance(text, str) else None


def _json_block(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


_FACT_PROMPT = """あなたは在宅医療チャット記録の事実候補抽出器です。以下の投稿本文から、
記載されている事実候補をJSONのみで列挙してください。推測や外部知識は禁止です。
引用は本文から一字一句そのままコピーしてください。

各候補のキー:
- "statement": 事実内容を15〜60字で（「〜の記載がある」等、報告としての表現）
- "kind": medication_event|symptom|explicit_request|pending_item|schedule|preference|observation|other
- "status": considered|planned|order_reported|execution_reported|cancelled|not_stated|conflicting
- "polarity": affirmed|negated|uncertain
- "time_text": 時間表現またはnull
- "quantity": 数量表現またはnull
- "evidence_quote": 根拠となる本文の完全一致引用（30字以内）

{"facts": [ ... ]} の形のみ出力。該当なしなら {"facts": []}。

本文:
<<<
%s
>>>
JSON:"""


def _iso_date(text: str) -> str | None:
    """occurred_at only when the original expression already carries an
    absolute date — relative/ambiguous text stays null and the raw
    time_text preserves it (spec §15.2)."""
    if not isinstance(text, str):
        return None
    m = re.match(r"^\s*(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    if not (2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _locate_quote(body: str, quote: str) -> tuple[int, int] | None:
    """Find quote's UNIQUE codepoint span in body. Ambiguous or absent
    quotes get no span — never a guessed one (INV-07, AT-029)."""
    if not body or not quote:
        return None
    first = body.find(quote)
    if first < 0 or body.find(quote, first + 1) >= 0:
        return None
    return (first, first + len(quote))


def _chunks(text: str, size: int = 3000) -> list:
    """Split into <=size chunks at line/sentence boundaries, hard-
    splitting only as a last resort. The concatenation of all chunks is
    the original text — full coverage, never head-only processing
    (§12.3, AT-017)."""
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out, buf = [], ""
    for seg in re.split(r"(?<=\n)", text):
        if len(buf) + len(seg) <= size:
            buf += seg
            continue
        if buf:
            out.append(buf)
            buf = ""
        while len(seg) > size:
            cut = max(seg.rfind("。", 0, size), seg.rfind("\n", 0, size))
            if cut <= 0:
                cut = size
            out.append(seg[:cut])
            seg = seg[cut:]
        buf = seg
    if buf:
        out.append(buf)
    return out


def extract_facts(llm_fn, member: dict,
                  deadline: float | None = None) -> tuple[list, bool, int]:
    """Fact candidates with verified evidence spans, extracted across
    the WHOLE body in bounded chunks — a long tail is never silently
    dropped (§12.3, AT-017). Returns (facts, complete, dropped):
    complete=False means the deadline hit mid-extraction OR a chunk's
    response was missing/unparseable — the caller must defer; the
    member is NOT 'processed' and no partial artifact is recorded, so a
    failed chunk can never silently narrow the extraction for this
    generation (§16.2 chunk ledger). dropped counts valid items lost to
    the fact cap so the artifact meta records the truncation instead of
    hiding it (§16.2). Unlocatable or ambiguous quotes stay
    'unverified' — never silently promoted (INV-07/08)."""
    body = member["body_original"]
    facts = []
    dropped = 0
    for ch in _chunks(body):
        if deadline is not None and time.monotonic() > deadline:
            return facts, False, dropped
        raw = _json_block(llm_fn(_FACT_PROMPT % ch) or "")
        items = raw.get("facts") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return facts, False, dropped
        for it in items:
            if not isinstance(it, dict):
                continue
            if len(facts) >= 40:
                dropped += 1
                continue
            stmt = it.get("statement")
            if not isinstance(stmt, str) or not stmt.strip():
                continue
            quote = it.get("evidence_quote")
            span = _locate_quote(body, quote) \
                if isinstance(quote, str) else None
            i = len(facts)
            ev_id = f"ev_{member['message_id']}_{i}"
            status = it.get("status")
            polarity = it.get("polarity")
            facts.append({
                "fact_id": f"fact_{member['message_id']}_{i}",
                "kind": it.get("kind") if it.get("kind") in FACT_KINDS
                        else "other",
                "statement": stmt.strip()[:200],
                "subject_ref": f"m{member['message_id']}",
                "drug_ref": None,
                "status": status if status in FACT_STATUSES
                          else "not_stated",
                "polarity": polarity if polarity in POLARITIES
                            else "uncertain",
                "occurred_at": _iso_date(it.get("time_text")),
                "time_text": it.get("time_text")
                             if isinstance(it.get("time_text"), str)
                             else None,
                "quantity": it.get("quantity")
                            if isinstance(it.get("quantity"), str)
                            else None,
                "evidence_refs": [ev_id] if span else [],
                "validation_status": "candidate" if span
                                     else "unverified",
                "_evidence": {"evidence_id": ev_id,
                              "message_id": member["message_id"],
                              "revision_id": member["revision"],
                              "start_codepoint": span[0],
                              "end_codepoint": span[1],
                              "quote": quote} if span else None,
            })
    return facts, True, dropped


_SUMMARY_PROMPT = """あなたは在宅医療チャット記録の要約器です。対象投稿と同一スレッド文脈、
検証済み事実候補から、全セクション共通のclaim構造を持つJSONのみ出力してください。

ルール:
- 各claimは {"section","text","claim_kind","fact_refs","status","polarity"}
- section: medication|status|pharmacy|followup|progress|flow|other
- claim_kind: reported_fact|inference|limitation
- fact_refs: 根拠となる候補の番号(0起き)の配列 — reported_factは必須、空は不可
- 対象時点の情報として書く（現在の診察結果と断定しない）
- 「対応不要」を既定にしない。未記載は limitations に書く
- 依頼の確認提案は原文の依頼と分け、claim_kind="inference" とする
- 命題評価は別系統の参考信号 — そのまま断定に使わず、claimの真偽は
  必ず本文と事実候補から判断する

対象投稿:
<<<
%s
>>>
同一スレッド文脈:
<<<
%s
>>>
事実候補(番号付き):
%s
命題評価(参考):
%s

{"claims":[...], "limitations":[...]} のみ出力。
JSON:"""

_REPAIR_SUFFIX = """
前回の出力は監査で不合格でした。修正点:
%s
同じ入力で全claimを再生成してください。原文を書き換えず、支持されない
表現を削り、落とした候補を拾ってください。
JSON:"""


def _facts_brief(facts: list) -> str:
    lines = []
    for i, f in enumerate(facts):
        lines.append(f"{i}. [{f['kind']}/{f['status']}/{f['polarity']}] "
                     f"{f['statement']}"
                     + (f"（{f['time_text']}）" if f.get("time_text") else ""))
    return "\n".join(lines) or "(なし)"


def _verdicts_brief(verdicts: dict) -> str:
    """Jev proposition verdicts rendered as a reference signal for the
    writer — advisory context, never forced truth (§15.3)."""
    lines = []
    for pid, v in (verdicts or {}).items():
        label = jev.PROPOSITIONS.get(pid, {}).get("label_ja", pid)
        lines.append(f"{pid} {label}: {v.get('verdict', '?')}")
    return "\n".join(lines) or "(なし)"


def summarize(llm_fn, bundle: dict, target_id: int, facts: list,
              verdicts: dict, feedback: list | None = None) -> dict | None:
    """Local-LLM summary in the common Claim schema. Jev verdicts are
    context for the writer, never forced truth (§15.3). The FULL target
    body and thread context are passed untruncated; if the prompt would
    exceed PROMPT_CHAR_LIMIT no model call is made and the result is a
    stub flagged _input_oversize (audit forces NEEDS_REVIEW — never a
    silent partial PASS)."""
    target = next((m for m in bundle["members"]
                   if m["message_id"] == target_id), None)
    if target is None:
        return None
    ctx = "\n".join(m["body_original"] for m in bundle["members"]
                    if m["message_id"] != target_id
                    and m["body_original"])
    prompt = _SUMMARY_PROMPT % (target["body_original"], ctx,
                                _facts_brief(facts),
                                _verdicts_brief(verdicts))
    if feedback:
        prompt += _REPAIR_SUFFIX % "\n".join(feedback[:10])
    if len(prompt) > PROMPT_CHAR_LIMIT:
        return {"summary_id": f"sum_{bundle['bundle_id']}",
                "input_bundle_id": bundle["bundle_id"],
                "schema_version": SCHEMA_VERSION,
                "target_message_id": target_id,
                "claims": [],
                "limitations": ["対象投稿または文脈が大きすぎるため"
                                "要約を生成できませんでした"],
                "audit_status": "pending", "_input_oversize": True}
    raw = _json_block(llm_fn(prompt) or "")
    if raw is None:
        return None
    claims_in = raw.get("claims")
    if not isinstance(claims_in, list):
        return None
    claims = []
    for i, c in enumerate(claims_in[:30]):
        if not isinstance(c, dict):
            continue
        text, section = c.get("text"), c.get("section")
        kind = c.get("claim_kind", "reported_fact")
        refs = c.get("fact_refs")
        if not isinstance(text, str) or not text.strip():
            continue
        if section not in CLAIM_SECTIONS:
            section = "other"
        if kind not in CLAIM_KINDS:
            kind = "reported_fact"
        if not isinstance(refs, list):
            refs = []
        refs = [r for r in refs
                if type(r) is int and 0 <= r < len(facts)]
        # a reported_fact without evidence is INV-07's exact failure —
        # keep it flagged so the audit can fail it, never drop silently
        ev_refs = sorted({e for r in refs
                          for e in facts[r]["evidence_refs"]})
        claims.append({
            "claim_id": f"claim_{target_id}_{i}",
            "section": section, "text": text.strip()[:400],
            "claim_kind": kind, "fact_refs": refs,
            "evidence_refs": ev_refs,
            "status": c.get("status") if c.get("status")
                      in FACT_STATUSES else "not_stated",
            "polarity": c.get("polarity") if c.get("polarity")
                        in POLARITIES else "uncertain"})
    limitations = [str(x)[:200] for x in
                   (raw.get("limitations") or []) if isinstance(x, str)]
    # cap truncation is a finding, not a silent drop — the auditor and
    # the notice reader must see that claims were left out (§16.2)
    if len(claims_in) > 30:
        limitations = [f"claim数が上限を超えたため"
                       f"{len(claims_in) - 30}件省略"] + limitations
    return {"summary_id": f"sum_{bundle['bundle_id']}",
            "input_bundle_id": bundle["bundle_id"],
            "schema_version": SCHEMA_VERSION,
            "target_message_id": target_id,
            "claims": claims, "limitations": limitations[:10],
            "audit_status": "pending"}
