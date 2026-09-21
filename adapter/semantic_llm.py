"""Local-LLM extraction and summarization (spec §13.1, §15):
prompts, response parsing, fact extraction wrapper, and the
summary/repair pass. llm_fn is injected — transport stays on the
facade (semantic.llm_chat) so endpoint/timeout patch points hold."""
from __future__ import annotations

import json
import re
import time

import semantic_jev as jev
from mcs_util import json_object as _json_block
from semantic_policy import SCHEMA_VERSION

FACT_KINDS = ("medication_event", "symptom", "explicit_request",
              "pending_item", "schedule", "preference", "observation",
              "other")
FACT_STATUSES = ("considered", "planned", "order_reported",
                 "execution_reported", "cancelled", "not_stated",
                 "conflicting")
POLARITIES = ("affirmed", "negated", "uncertain")
CLAIM_KINDS = ("reported_fact", "inference", "limitation")
CLAIM_SECTIONS = ("medication", "status", "pharmacy", "followup",
                  "progress", "flow", "other")

_FACT_PROMPT = """あなたは在宅医療チャット記録の事実候補抽出器です。以下の投稿本文から、
記載されている事実候補をJSONのみで列挙してください。推測や外部知識は禁止です。
引用は本文から一字一句そのままコピーしてください。
薬剤・対象・事象・時点ごとに候補を分け、別薬剤の変更なしと増量を混ぜないでください。

各候補のキー:
- "statement": 対象・値・否定・時制を省略せず簡潔に（報告としての表現）
- "drug_ref": 原文に明示された対象薬剤名そのまま、特定不能ならnull
- "kind": medication_event|symptom|explicit_request|pending_item|schedule|preference|observation|other
- "status": considered|planned|order_reported|execution_reported|cancelled|not_stated|conflicting
- "polarity": affirmed|negated|uncertain
- "time_text": 時間表現またはnull
- "quantity": 数量表現またはnull
- "evidence_quote": 根拠となる本文の完全一致引用（必要な範囲を省略しない）

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
    from datetime import date
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


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
                  deadline: float | None = None,
                  return_reason: bool = False, *, ledger=None,
                  source_fingerprint=None, project_id=None) -> tuple:
    """Whole-source extraction; durable chunks when called by the worker."""
    from semantic_extraction import extract_facts_resumable
    result = extract_facts_resumable(
        llm_fn, member, deadline, ledger=ledger,
        source_fingerprint=source_fingerprint, project_id=project_id)
    values = (result["facts"], result["complete"], result["dropped"])
    return (*values, result["failure_reason"]) if return_reason else values


_SUMMARY_PROMPT = """あなたは在宅医療チャット記録の要約器です。対象投稿と同一スレッド文脈、
検証済み事実候補から、全セクション共通のclaim構造を持つJSONのみ出力してください。

ルール:
- 各claimは {"section","text","claim_kind","fact_refs","status","polarity"}
- 一つのclaimは一つの対象・事象・時点。複数薬剤や複数時点は別claimに分割する
- section: medication|status|pharmacy|followup|progress|flow|other
- claim_kind: reported_fact|inference|limitation
- fact_refs: 根拠となる候補の番号(0起き)の配列 — reported_factは必須、空は不可
- 数値・単位・1回量/1日量は参照する原文の表記と対応を保つ。換算や合算を新たに推測しない
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


# Safety ceiling for one local-LLM prompt. Above it the model's context
# window could silently drop input — an oversize target is flagged
# input_oversize -> NEEDS_REVIEW instead of being chopped (§12.3).
PROMPT_CHAR_LIMIT = 28000


def summarize(llm_fn, bundle: dict, target_id: int, facts: list,
              verdicts: dict, feedback: list | None = None,
              deadline: float | None = None,
              return_reason: bool = False):
    """Local-LLM summary in the common Claim schema. Jev verdicts are
    context for the writer, never forced truth (§15.3). The FULL target
    body and thread context are passed untruncated; if the prompt would
    exceed PROMPT_CHAR_LIMIT no model call is made and the result is a
    stub flagged _input_oversize (audit forces NEEDS_REVIEW — never a
    silent partial PASS).

    ``return_reason=True`` preserves the normal result while identifying a
    deadline/resource wait versus an unsuccessful model response.  Existing
    callers keep the original ``dict | None`` return shape by default."""

    def result(value, reason: str | None = None):
        return (value, reason) if return_reason else value

    target = next((m for m in bundle["members"]
                   if m["message_id"] == target_id), None)
    if target is None:
        return result(None, "invalid")
    inputs = [{"message_id": m["message_id"], "text": m["body_original"],
               "posted_at": m["posted_at"], "sender": m["sender"],
               "unparsed_attachment_count": len(m.get("attachments", []))}
              for m in bundle["members"]]
    target_input = next(m for m in inputs if m["message_id"] == target_id)
    ctx = json.dumps([m for m in inputs if m["message_id"] != target_id],
                     ensure_ascii=False)
    prompt = _SUMMARY_PROMPT % (json.dumps(target_input, ensure_ascii=False),
                                ctx, _facts_brief(facts),
                                _verdicts_brief(verdicts))
    if feedback:
        prompt += _REPAIR_SUFFIX % "\n".join(feedback[:10])
    if deadline is not None and time.monotonic() > deadline:
        return result(None, "deadline")
    if len(prompt) > PROMPT_CHAR_LIMIT:
        return result({"summary_id": f"sum_{bundle['bundle_id']}",
                       "input_bundle_id": bundle["bundle_id"],
                       "schema_version": SCHEMA_VERSION,
                       "target_message_id": target_id,
                       "claims": [],
                       "limitations": ["対象投稿または文脈が大きすぎるため"
                                       "要約を生成できませんでした"],
                       "audit_status": "pending", "_input_oversize": True})
    response = llm_fn(prompt)
    if deadline is not None and time.monotonic() > deadline:
        return result(None, "deadline")
    raw = _json_block(response or "")
    if raw is None:
        return result(None, "model")
    claims_in = raw.get("claims")
    if not isinstance(claims_in, list):
        return result(None, "model")
    claims = []
    for i, c in enumerate(claims_in):
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
            "section": section, "text": text.strip(),
            "claim_kind": kind, "fact_refs": refs,
            "evidence_refs": ev_refs,
            "status": c.get("status") if c.get("status")
                      in FACT_STATUSES else "not_stated",
            "polarity": c.get("polarity") if c.get("polarity")
                        in POLARITIES else "uncertain"})
    limitations = [x for x in
                   (raw.get("limitations") or []) if isinstance(x, str)]
    attachment_count = sum(len(m.get("attachments", [])) for m in bundle["members"])
    if attachment_count:
        limitations.append(f"対象投稿・文脈に添付{attachment_count}件があります。添付内容は解析していません。")
    return result({"summary_id": f"sum_{bundle['bundle_id']}",
                   "input_bundle_id": bundle["bundle_id"],
                   "schema_version": SCHEMA_VERSION,
                   "target_message_id": target_id,
                   "claims": claims, "limitations": limitations,
                   "audit_status": "pending"})
