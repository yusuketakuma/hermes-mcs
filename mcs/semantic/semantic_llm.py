"""Local-LLM extraction and summarization (spec §13.1, §15):
prompts, response parsing, fact extraction wrapper, and the
summary/repair pass. llm_fn is injected — transport stays on the
facade (semantic.llm_chat) so endpoint/timeout patch points hold."""
from __future__ import annotations

import json
import re
import time

import semantic_jev as jev
import semantic_runtime as runtime
from mcs_util import json_object as _json_block
# generic text utilities live in core/mcs_util.py; the private aliases
# keep the semantic_llm/semantic facade patch surface stable
from mcs_util import locate_quote_span as _locate_quote  # noqa: F401
from mcs_util import text_chunks as _chunks  # noqa: F401
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


# Canonical semantic-facts/v2 extraction prompt.  Unlike _FACT_PROMPT the
# response must adjudicate every mandatory pharmacist-rubric category, so
# an empty category is an explicit decision ("none"), never an omission.
_FACT_V2_PROMPT = """あなたは在宅医療チャット記録の原子事実抽出器です。以下の投稿本文から、
記載されている全ての臨床事実をJSONのみで列挙してください。推測・外部知識・
要約は禁止です。記載がない項目は作らず、引用は本文から一字一句そのまま
コピーしてください。別薬剤・別時点の事実を1つに混ぜないでください。

各事実のキー:
- "statement": 対象・値・否定・時制を省略せず簡潔に（報告としての表現）
- "kind": medication_event|medication_exposure|allergy_intolerance|adverse_drug_event|adherence_administration|symptom_state|vital_lab|care_event|request_pending|preference|other_observation
- "subject_role": patient|family|staff|other|null（不明ならnull）
- "subject_name": 原文の呼称そのまま、なければnull
- "polarity": affirmed|negated|uncertain|unknown
- "epistemic": asserted|reported|speculated|unknown
- "workflow_status": reported|ordered|planned|considering|in_progress|performed|done|cancelled|on_hold|pending|unknown
- "action": medication_eventのみ start|stop|increase|decrease|change|hold|restart|continue|unchanged|consider|planned|ordered|administered|cancelled|unknown
- "event_time": ISO形式の日付またはunknown
- "valid_time": 有効期間表現またはunknown
- "quantity": 数量表現またはnull
- "importance": T0（緊急）|T1（重要）|T2（通常）|T3（参考）|unknown
- "evidence_quote": 根拠となる本文の完全一致引用（必要な範囲を省略しない）

"category_presence": 本文に各カテゴリの事実が存在するかの判定。
medication|allergy_intolerance|adverse_drug_event|adherence_administration|
symptom_state|vital_lab|care_event|request_pending|preference|other_observation
の全てに none|one|multiple|ambiguous のいずれかを必ず付けること。

{"facts": [ ... ], "category_presence": { ... }} の形のみ出力。

本文:
<<<
%s
>>>
JSON:"""

_FACT_V2_REQUEST_FOLLOWING_PROMPT = _FACT_V2_PROMPT.rsplit("JSON:", 1)[0] + """
request_pendingのみ、次の任意項目も保持してください:
- "request_to"・"request_from": 原文の宛先・依頼者の呼称。根拠がなければunknown
- "due_text"・"condition": 期限原文・条件原文。根拠がなければunknown
- "request_kind": request|question|self_plan|unknown
追加項目の原文も同じ事実のevidence_quoteに含めてください。
相対期限を推測した日付へ変換せず、依頼者や宛先を文脈から補完しないでください。
返信・回答・受領・了承を新しい依頼や臨床対応の完了に変換しないでください。
JSON:"""

_FACT_V2_REPAIR_SUFFIX = """
前回この本文から抽出した以下の事実が監査で不合格でした:
%s
同じ本文で全事実を再抽出してください。不合格の事実は、支持されない
主張を削るか、evidenceと一致する形に修正してください。新たな事実の
推測追加は禁止。引用は本文の完全一致のみ。
{"facts": [ ... ], "category_presence": { ... }} の形のみ出力。
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
- reported_fact の根拠となる事実候補が一覧に無い場合、そのclaimは出力しない。
  代わりに limitations に「〜は事実候補なし」と記述する
- claimの内容は fact_refs が指す候補の statement の言い換えに留める。
  候補に無い数値・主体・時制・推測をclaimに追加しない
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
# Sized for the deployed -c 49152 -np 3 layout (16384 tokens/slot):
# CJK-dense text can approach 1 token/char, so 12000 chars keeps the
# prompt under the slot window with headroom for the output budget.
PROMPT_CHAR_LIMIT = 12000


def _oversize_stub(bundle: dict, target_id: int) -> dict:
    """Terminal no-claims summary for a prompt the slot context cannot
    hold — audit flags _input_oversize -> NEEDS_REVIEW, never a silent
    partial PASS."""
    return {"summary_id": f"sum_{bundle['bundle_id']}",
            "input_bundle_id": bundle["bundle_id"],
            "schema_version": SCHEMA_VERSION,
            "target_message_id": target_id,
            "claims": [],
            "limitations": ["対象投稿または文脈が大きすぎるため"
                            "要約を生成できませんでした"],
            "audit_status": "pending", "_input_oversize": True}


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
        return result(_oversize_stub(bundle, target_id))
    try:
        response = llm_fn(prompt)
    except runtime.LLMRejected:
        # the backend refused the prompt (context window) — same
        # terminal oversize outcome as the local char gate, reached
        # without burning a retry on an input that can never pass
        return result(_oversize_stub(bundle, target_id))
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
            return result(None, "model")
        text, section = c.get("text"), c.get("section")
        kind = c.get("claim_kind", "reported_fact")
        refs = c.get("fact_refs")
        if not isinstance(text, str) or not text.strip():
            return result(None, "model")
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
    limitations = raw.get("limitations", [])
    if limitations is None:
        limitations = []
    if not isinstance(limitations, list) \
            or any(not isinstance(x, str) for x in limitations):
        return result(None, "model")
    attachment_count = sum(len(m.get("attachments", [])) for m in bundle["members"])
    if attachment_count:
        limitations.append(f"対象投稿・文脈に添付{attachment_count}件があります。添付内容は解析していません。")
    return result({"summary_id": f"sum_{bundle['bundle_id']}",
                   "input_bundle_id": bundle["bundle_id"],
                   "schema_version": SCHEMA_VERSION,
                   "target_message_id": target_id,
                   "claims": claims, "limitations": limitations,
                   "audit_status": "pending"})
