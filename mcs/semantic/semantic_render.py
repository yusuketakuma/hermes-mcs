"""Render semantic notices and borrowed-connection summary sections.

Text formatting never publishes intents or owns a database connection.
"""
from __future__ import annotations
import semantic_policy
import semantic_store

from collections import Counter
from datetime import datetime
import re

from mcs_queries import JST
import semantic_facts as sf
import semantic_send_gate
from semantic_send_gate import enforce_block_reason

_SECTION_LABEL = {"medication": "薬剤・処方に関する情報",
                  "status": "現在の状況（対象投稿時点）",
                  "pharmacy": "薬局への影響", "followup": "未決事項／フォローアップ",
                  "progress": "前回からの進展", "flow": "投稿の流れ",
                  "other": "その他"}

_CATEGORY_LABEL = {
    "medication": "薬剤", "allergy_intolerance": "アレルギー・不耐",
    "adverse_drug_event": "有害事象", "adherence_administration": "服薬・投与",
    "symptom_state": "症状・状態", "vital_lab": "バイタル・検査",
    "care_event": "診療・ケア", "request_pending": "依頼・未決",
    "preference": "希望", "other_observation": "その他の所見",
}


_LIMITATION_NOTICE = "要約対象外の情報があります。MCS原文を確認してください。"


def summary_limitations(summary: dict) -> list[str]:
    """Render only fixed code vocabulary from the mixed limitations channel."""
    allowed = {f"{label}の判定が未確定のため要約対象外（原文確認）"
               for label in _CATEGORY_LABEL.values()}
    allowed.add("対象投稿または文脈が大きすぎるため要約を生成できませんでした")
    lines = []
    for item in summary.get("code_limitations") or []:
        if isinstance(item, str) and (item in allowed or re.fullmatch(
                r"対象投稿・文脈に添付[0-9]+件があります。添付内容は解析していません。", item)):
            if item not in lines:
                lines.append(item)
    # Matching a template does not establish provenance for model/old mixed text.
    if any(item not in lines for item in summary.get("limitations") or []):
        lines.insert(0, _LIMITATION_NOTICE)
    return lines


MANDATORY_PAGE_BUDGET = 1900    # chars/page — the send-path chunk bound

# Stored contract attributes a reader needs to interpret a line (dose,
# negation, certainty, workflow state). Rendered verbatim — codes, not
# paraphrase — and only when stated; "unknown" adds nothing.
FACT_ATTRIBUTE_LABELS = (("quantity", "量"), ("polarity", "極性"),
               ("epistemic", "確度"), ("workflow_status", "状態"),
               ("action", "行為"), ("valid_time", "有効時点"),
               ("actor", "記録者"))


def fact_binding(fid: str) -> str:
    """Page-line marker binding a fact ID to its evidence."""
    return f"、ID:{fid}、証拠:"


def _attr_desc(fact: dict) -> str:
    return "".join(f"、{label}:{fact[key]}" for key, label in FACT_ATTRIBUTE_LABELS
                   if isinstance(fact.get(key), str)
                   and fact[key] not in ("", "unknown"))


def _evidence_desc(fact: dict) -> str:
    ids = [e for e in (fact.get("evidence_ids") or []) if isinstance(e, str)]
    if not ids:
        return "なし"
    return ids[0] + (f"他{len(ids) - 1}件" if len(ids) > 1 else "")


def _fact_pages(entries: list, budget: int) -> list:
    """Pack (line, fact_id) pairs into immutable pages within the char
    budget. A line alone over budget still takes its own page — the
    completeness check then reports it oversized, never truncated."""
    pages, cur_lines, cur_ids, used = [], [], [], 0
    for line, fid in entries:
        need = len(line) + (1 if cur_lines else 0)
        if cur_lines and used + need > budget:
            pages.append({"index": len(pages), "fact_ids": cur_ids,
                          "text": "\n".join(cur_lines)})
            cur_lines, cur_ids, used = [], [], 0
            need = len(line)
        cur_lines.append(line)
        cur_ids.append(fid)
        used += need
    if cur_lines:
        pages.append({"index": len(pages), "fact_ids": cur_ids,
                      "text": "\n".join(cur_lines)})
    return pages


def verify_mandatory_pages(rendered: dict,
                           budget: int = MANDATORY_PAGE_BUDGET) -> dict:
    """Completeness oracle for a mandatory_render result — the gate
    between 'published' and 'still missing'. A missing/duplicated/
    unbound fact_id or an oversized page means publication is
    INCOMPLETE, never PASS."""
    fact_ids = list(rendered.get("fact_ids") or [])
    pages = list(rendered.get("pages") or [])
    declared = [fid for p in pages for fid in p.get("fact_ids") or []]
    declared_set = set(declared)
    fact_set = set(fact_ids)
    missing = [f for f in fact_ids if f not in declared_set]
    extra = [f for f in declared if f not in fact_set]
    duplicated = sorted(f for f, count in Counter(declared).items() if count > 1)
    oversized = [p.get("index") for p in pages
                 if len(p.get("text") or "") > budget]
    unbound = [{"page": p.get("index"), "fact_id": fid}
               for p in pages for fid in p.get("fact_ids") or []
               if fact_binding(fid) not in (p.get("text") or "")]
    return {"complete": not (missing or extra or duplicated
                             or oversized or unbound),
            "missing": missing, "extra": extra,
            "duplicated": duplicated, "oversized_pages": oversized,
            "unbound": unbound}


def mandatory_render(doc: dict,
                     page_budget: int = MANDATORY_PAGE_BUDGET) -> dict:
    """Deterministic mandatory layer for a semantic-facts/v2 document
    (T11): every verified fact and every non-terminal obligation must
    be visible in the rendered output even when the model summary
    omits them.  Lines are code-generated from the stored contract —
    never model text invented here. No count cap: each rendered line
    carries subject/time/ID/evidence identity plus every stated
    quantity/polarity/epistemic/workflow/action/valid-time/actor
    attribute, and the full list is
    split into source-bound pages within the platform char budget —
    'N omitted' is publication-incomplete, never a limitation."""
    facts = [f for f in doc.get("facts", [])
             if isinstance(f, dict)
             and f.get("validation_status") == "verified"]
    lines, fact_ids, seen = [], [], set()
    category_counts = Counter()
    entries = []
    for fact in facts:
        text = fact.get("statement")
        fid = fact.get("fact_id")
        if not isinstance(text, str) or not text \
                or not isinstance(fid, str) or not fid or fid in seen:
            continue
        seen.add(fid)
        label = _CATEGORY_LABEL.get(
            sf.KIND_CATEGORY.get(fact.get("kind"), ""), "その他の所見")
        category_counts[label] += 1
        line = (f"{label}｜{text}"
                f"（対象:{fact.get('subject') or '不明'}"
                f"、時点:{fact.get('event_time') or '不明'}"
                f"{_attr_desc(fact)}"
                f"{fact_binding(fid)}{_evidence_desc(fact)}）")
        line = semantic_text_line(line)
        lines.append(line)
        fact_ids.append(fid)
        entries.append((line, fid))
    lims, lim_seen = [], set()
    for ob in doc.get("obligations", []):
        if not isinstance(ob, dict) \
                or ob.get("status") not in ("open", "ambiguous",
                                            "failed"):
            continue
        label = _CATEGORY_LABEL.get(ob.get("category"), "その他の所見")
        line = f"{label}の判定が未確定のため要約対象外（原文確認）"
        if line not in lim_seen:
            lim_seen.add(line)
            lims.append(line)
    breakdown = "、".join(f"{label}{n}"
                          for label, n in category_counts.items())
    overview = (f"確認済み事実{len(lines)}件（{breakdown}）"
                if lines else "確認済み事実0件")
    src = doc.get("source") or {}
    out = {"facts": lines, "fact_ids": fact_ids, "overview": overview,
           "pages": _fact_pages(entries, page_budget),
           "limitations": lims,
           "source_fingerprint": src.get("source_fingerprint")}
    out["complete"] = verify_mandatory_pages(out, page_budget)["complete"]
    return out


def render_notice(db, project_id: int, root_id: int,
                  summary: dict, audit_status: str,
                  targets: list | None = None,
                  quality: str | None = None,
                  focus_mid: int | None = None) -> str:
    """The §20.1 block. Patient label + coverage/audit status lines are
    code-generated; claim text comes from the audited summary. The MCS
    link is the stored patient URL — never a guessed permalink.
    対象新着 counts only THIS generation's target posts — the rest of
    the thread is context, not arrivals. A multi-target generation
    emits one notice per target; focus_mid names WHICH covered post
    this notice's claims belong to."""
    def instant(value):
        try:
            parsed = datetime.fromisoformat(value or "")
            return parsed if parsed.tzinfo is not None else None
        except ValueError:
            return None

    def stamp(value):
        return value.astimezone(JST).strftime(
            "%Y/%m/%d %H:%M JST") if value else "時刻不明"

    pat = db.execute(
        "SELECT patient_name,url FROM patients WHERE project_id=?",
        (project_id,)).fetchone()
    name = (pat["patient_name"] if pat else None) or str(project_id)
    url = (pat["url"] if pat else None) or ""
    ids = [t for t in (targets or [root_id]) if type(t) is int] \
        or [root_id]
    marks = ",".join("?" * len(ids))
    members = db.execute(
        f"SELECT message_id,posted_at FROM messages "
        f"WHERE project_id=? AND message_id IN ({marks})",
        (project_id, *ids)).fetchall()
    times = [instant(row["posted_at"]) for row in members]
    latest = stamp(max((value for value in times if value is not None),
                       default=None))
    lines = [f"【{name}】",
             f"対象新着：{len(members)}投稿"
             f"｜対象投稿の最終時刻：{latest}",
             f"取得：{'完全' if quality == 'full' else '一部未取得'}",
             f"要約：{'自動検査完了' if audit_status == 'PASS' else '要確認'}"]
    if focus_mid is not None and len(ids) > 1:
        trow = db.execute(
            "SELECT posted_at FROM messages WHERE project_id=? "
            "AND message_id=?", (project_id, focus_mid)).fetchone()
        focus_stamp = stamp(instant(trow["posted_at"])) if trow else "時刻不明"
        lines.append(f"要約対象：{focus_stamp} の投稿#{focus_mid}")
    by_sec: dict[str, list] = {}
    for c in summary.get("claims", []):
        by_sec.setdefault(c["section"], []).append(c)
    if by_sec:
        lines.append("\n■ 今回の重要情報")
        for c in by_sec.get("medication", []) + by_sec.get("status", []):
            prefix = "【提案】" if c["claim_kind"] == "inference" else ""
            lines.append(f"・{prefix}{semantic_text_line(c['text'])}")
    for sec in ("pharmacy", "followup", "progress", "flow", "other"):
        if by_sec.get(sec):
            lines.append(f"\n■ {_SECTION_LABEL[sec]}")
            for c in by_sec[sec]:
                prefix = "【提案】" if c["claim_kind"] == "inference" else ""
                lines.append(f"・{prefix}{semantic_text_line(c['text'])}")
    mfacts = summary.get("mandatory_facts") or []
    if mfacts:
        lines.append("\n■ 抽出済み事実（監査済み）")
        m_overview = summary.get("mandatory_overview")
        if m_overview:
            lines.append(f"・{semantic_text_line(m_overview)}")
        lines.extend(f"・{semantic_text_line(x)}" for x in mfacts)
    lims = summary_limitations(summary)
    if lims:
        lines.append("\n■ 原文・制約")
        lines.extend(f"・{x}" for x in lims)
    if url.startswith("https://"):
        lines.append("\n▶ MCSで確認\n" + url)
    return "\n".join(lines)


def render_degraded(db, project_id: int) -> str:
    """Minimal code-generated notice for enforce-mode overruns — carries
    no unverified clinical claims (§19.3)."""
    pat = db.execute(
        "SELECT patient_name,url FROM patients WHERE project_id=?",
        (project_id,)).fetchone()
    name = (pat["patient_name"] if pat else None) or str(project_id)
    url = (pat["url"] if pat else None) or ""
    text = (f"【{name}】新着を取り込みました\n"
            "取得：保存済み\n"
            "要約：意味検査が完了していないため保留\n"
            "確認方法：MCS原文を確認してください")
    if url.startswith("https://"):
        text += "\n\n▶ MCSで確認\n" + url
    return text










def apply_mandatory_render(summary: dict, findings: list, status: str,
                            v2_doc: dict) -> str:
    """Merge the adjudicated mandatory render layer into ``summary`` and
    ``findings`` in place; returns the (possibly downgraded) status."""
    mandatory = mandatory_render(v2_doc)
    if mandatory["facts"]:
        summary["mandatory_facts"] = mandatory["facts"]
        summary["mandatory_fact_ids"] = mandatory["fact_ids"]
        summary["mandatory_pages"] = mandatory["pages"]
        summary["mandatory_overview"] = mandatory["overview"]
    if mandatory["limitations"]:
        code_lims = summary.setdefault("code_limitations", [])
        code_lims.extend(x for x in mandatory["limitations"] if x not in code_lims)
        existing_lims = summary.get("limitations") or []
        summary["limitations"] = existing_lims + [
            x for x in mandatory["limitations"]
            if x not in existing_lims]
    # A verified fact that cannot reach any page (oversized
    # single line, or a packing gap) is publication-incomplete —
    # flag it instead of letting a partial list pass as done.
    if not mandatory["complete"]:
        findings.append({"code": "mandatory_render_incomplete"})
        status = "NEEDS_REVIEW"
    return status


def semantic_text_line(text: str) -> str:
    """Keep summary values on one content line, without structural markers."""
    return " ".join(text.split()).replace("■", "□").replace(
        "▶", "▷").replace("─────", "—")


def semantic_chunks(content: str, max_len: int) -> list[str]:
    """Split a frozen semantic notice while repeating its provenance."""
    return semantic_chunk_parts(content, max_len)[0]


def semantic_chunk_parts(content: str,
                         max_len: int) -> tuple[list[str], list[str]]:
    """``(parts, bodies)``: the sent parts plus the body slice each part
    carries between its repeated header/marker and footer — the bodies
    concatenate back to the notice body, so readers attribute lines to
    parts without re-parsing the rendered text.

    ``semantic_render.render_notice`` already puts the patient, coverage/audit
    labels, and stored MCS URL in the frozen payload. A raw character slice
    would leave those lines only in part 1, so each part gets the same frozen
    header and link. The payload is never rebuilt from the ledger here.
    """
    lines = content.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    # Only the renderer's terminal footer is structural. An evidence quote
    # or model claim may contain the same marker/link inside the body.
    if len(lines) < 2 or lines[-2] != "▶ MCSで確認":
        raise ValueError("semantic_provenance_missing")
    footer_index = len(lines) - 2
    link = lines[-1].strip()
    if not link.startswith("https://"):
        raise ValueError("semantic_provenance_missing")

    # The renderer's leading block is stable and ends at the first blank line
    # or section. Keep every line in that block so target time and any
    # degraded-notice explanation remain visible on every part.
    header_end = len(lines)
    for index, line in enumerate(lines[:footer_index]):
        if not line.strip() or line.startswith("■"):
            header_end = index
            break
    header = [line for line in lines[:header_end] if line.strip()]
    if not any(line.startswith("取得：") for line in header):
        raise ValueError("semantic_coverage_missing")
    if not any(line.startswith("要約：") for line in header):
        raise ValueError("semantic_audit_missing")

    # Remove the original header/footer before reattaching them to every
    # chunk. This keeps the frozen claims intact without duplicating the
    # first block inside part 1.
    body_start = header_end
    while body_start < footer_index and not lines[body_start].strip():
        body_start += 1
    body_lines = lines[body_start:footer_index]
    body = "\n".join(body_lines).strip()

    footer = ["▶ MCSで確認", link]
    static = "\n".join(header + footer)
    count = 1
    chunks = []
    for _ in range(20):
        marker = f"part {count}/{count}"
        overhead = len(static) + len(marker) + 3
        capacity = max_len - overhead
        if capacity < 1:
            raise ValueError("semantic_provenance_too_long")
        chunks = [body[i:i + capacity] for i in range(0, len(body), capacity)]
        if not chunks:
            chunks = [""]
        next_count = len(chunks)
        if next_count == count:
            break
        count = next_count
    else:
        raise ValueError("semantic_chunking_failed")

    out = []
    for index, chunk in enumerate(chunks, 1):
        marker = f"part {index}/{count}"
        part = "\n".join(header + [marker, chunk, *footer])
        if len(part) > max_len:
            raise ValueError("semantic_chunk_limit")
        out.append(part)
    return out, chunks


def semantic_summary_block(db, r, cfg: dict) -> str:
    """Enforce-mode: audited summary section appended to this post.
    Only the newest semantic_summary with audit_status PASS on the
    CURRENT generation qualifies — stale/failed/unaudited ones are
    silently absent, and in shadow/assist/off the notification stays
    byte-identical to pre-Phase-J (INV-16, spec §20.2)."""
    try:
        scfg, _ = semantic_policy.semantic_config(cfg)
        if enforce_block_reason(db, scfg, r["project_id"]) is not None:
            return ""
        root = r["parent_id"] or r["message_id"]
        # The audited summary must describe the current whole-thread
        # generation, not only the target's body revision (INV-15).
        cur = semantic_send_gate.current_summary(
            db, r["project_id"], r["message_id"],
            lambda: semantic_store.thread_bundle(db, r["project_id"], root,
                                       [r["message_id"]]),
            semantic_policy.policy_fingerprint(scfg))
        if cur is None:
            return ""
        summ = cur["content"]
        # Keep every audited claim; the send path's chunker is the one
        # place that splits a notification.
        lines = [semantic_text_line(str(c.get("text", "")))
                 for c in summ.get("claims", [])
                 if isinstance(c, dict) and c.get("text")]
        mfacts = [semantic_text_line(x) for x in (summ.get("mandatory_facts") or [])
                  if isinstance(x, str) and x]
        if not lines and not mfacts:
            return ""
        body = "\n".join(f"・{x}" for x in lines)
        if mfacts:
            if body:
                body += "\n"
            body += "■ 抽出済み事実（監査済み）\n" + "\n".join(f"・{x}" for x in mfacts)
        lims = summary_limitations(summ)
        if lims:
            body += "\n・（原文確認）" + "；".join(lims)
        return f"\n───── 要約（自動検査済） ─────\n{body}"
    except Exception:
        return ""
