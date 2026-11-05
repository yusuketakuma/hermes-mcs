"""Structured display view over extraction artifacts.

Shared formatter for the extract_v1/extract_llm artifacts — used by the
text notify_flush (_fmt_row's 📋 要約 section) and the interactive card
renderer (notify_render.structured_block). Keeping one implementation
means the suppression rules (negated/resolved symptoms, other-person
meds, unverified-medication labelling) and the freshness gate can never
diverge between surfaces.

Freshness/safety gate lives in mcs_queries.current_message_extract SQL: the artifact must
be bound to the message's CURRENT content_hash and the message must not
be deleted — a tombstoned message never contributes extracted data.

Merge contract (v1 rules ∪ llm ∪ v4 canonical facts), per field:
  events   — llm authoritative when present; rules add media references only.
  vitals   — preserve one source; no cross-subject or cross-time key filling.
  symptoms — llm entries carry polarity/subject; v1 tokens merge in
             unless they overlap an llm symptom (typed negation must not
             resurface as a bare symptom token).
  meds     — llm ONLY when it emitted meds (even all-filtered): falling
             back to v1 would resurrect filtered negated/family items.
             v1 feeds "薬剤候補（未確認）" only when llm saw nothing.
  requests — llm preferred; v1 formulaic 確認/連絡 fill only when llm
             emitted none.
  canonical facts — appended last as the highest-authority layer.
"""
from __future__ import annotations

import hashlib
from itertools import islice
import json
import re
import sqlite3

from drug_map import KIND as REF_KIND, candidate_note, current_refs, generation_signature
import drug_map
from mcs_queries import (FACT_KINDS_SQL, current_extract_pred, current_fact_pred,
                         med_is_patient_current, item_unverified)
from mcs_util import loads_dict, search_fold
from clinical_display import item_scope, scoped_vitals, vital_line
from semantic_render import FACT_ATTRIBUTE_LABELS
import clinical_urgency
import clinical_values
import mcs_queries


def _drug_refs(db, mid: int) -> list:
    """Annotation failure never suppresses the post's raw medication facts."""
    if db is None:
        return []
    try:
        return current_refs(db, mid)
    except (sqlite3.DatabaseError, ValueError, TypeError, RecursionError):
        return []


def _drug_note(ref) -> str:
    """Only existing unconfirmed-candidate wording; never infer clinical equivalence."""
    try:
        note = candidate_note(ref)
        note.encode("utf-8")
        return note
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return ""


def fact_generations(db, mids: list) -> dict:
    """Current selected fact and rule generations, fetched in bounded batches."""
    ids = list(dict.fromkeys(int(m) for m in mids if isinstance(m, int) or
                            (isinstance(m, str) and m.isdigit())))
    result = {}
    dictionary_generation = None
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        marks = ",".join("?" * len(batch))
        for r in db.execute(
                "SELECT a.message_id,a.kind,MAX(a.artifact_id) generation "
                "FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
                f"WHERE a.message_id IN ({marks}) AND ("
                f"(a.kind='extract_v1' {current_extract_pred()}) OR "
                f"(a.kind IN ({FACT_KINDS_SQL}) "
                f"{current_fact_pred()})) GROUP BY a.message_id,a.kind", batch):
            if r['kind'] == 'extract_llm' and not mcs_queries.selected_artifact_proof_valid(db, r['generation']):
                continue
            result.setdefault(r["message_id"], {})[r["kind"]] = r["generation"]
        ref_mids = db.execute(
            f"SELECT DISTINCT message_id FROM artifacts WHERE kind=? "
            f"AND message_id IN ({marks})", (REF_KIND, *batch)).fetchall()
        for row in ref_mids:
            mid = row[0]
            if mid not in result:
                continue  # an annotation alone is not a ready extraction
            refs = _drug_refs(db, mid)
            if not refs:
                continue  # disabled/corrupt/removed annotations share the same absent state
            if dictionary_generation is None:
                dictionary_generation = generation_signature(db)
            # Hash the usable annotations, not rewrite IDs/cursor movement.
            # Direct corruption changes this too, even without a new artifact ID.
            result[mid]["med_ref"] = hashlib.sha256(json.dumps(
                [dictionary_generation, refs], sort_keys=True
            ).encode()).hexdigest()
    return result




RX_LABEL = {"start": "開始", "stop": "中止", "change": "変更",
            "none": "変更なし", "no_change": "変更なし",
            "increase": "増量", "decrease": "減量"}
REQ_LABEL = {"confirm": "確認", "contact": "連絡", "share": "共有",
             "ask": "質問", "request": "依頼", "report": "報告"}
EVT_LABEL = {"admission": "入院", "discharge": "退院", "exam": "受診/検査",
             "visit": "訪問", "medication": "投薬", "adherence": "服薬",
             "care": "介護", "eol": "看取り", "media_ref": "添付",
             "transfer": "転院/移動", "fall": "転倒",
             "family_contact": "家族連絡",
             # the event covers both 疑義照会 and 処方提案, and never says it was done
             "prescription_inquiry": "処方の照会・提案の記載", "other": "その他"}
# schema v4 detail labels
_SEVERITY_JP = {"mild": "軽度", "moderate": "中等度", "severe": "重度"}
_ROUTE_JP = {"oral": "内服", "topical": "外用", "injection": "注射",
             "infusion": "点滴", "inhalation": "吸入", "tube": "経管",
             "other": "その他"}
_LAB_FLAG_JP = {"high": "高", "low": "低"}
# source-scope labels shared by the finding and lab renderers
_SUBJECT_JP = {"family": "家族", "other": "本人以外", "unknown": "未確認"}
_TIME_SCOPE_JP = {"past": "過去の報告", "planned": "予定", "conditional": "条件・可能性の記載"}


# Canonical-only categories: these v2 kinds have no legacy slot, so
# they render straight from ``canonical_facts`` — fact_id and evidence
# stay attached and the statement is never squeezed into a wrong field.
_FINDING_LABEL = {"allergy_intolerance": "アレルギー・不耐",
                  "adverse_drug_event": "有害事象",
                  "vital_lab": "バイタル・検査",
                  "preference": "希望",
                  "other_observation": "所見"}


def _items(doc: dict, key: str) -> list:
    value = doc.get(key)
    return value if isinstance(value, list) else []


def _label(labels: dict, value):
    return labels.get(value) if isinstance(value, str) else None


def _empty_field(doc: dict, key: str) -> bool:
    return key not in doc or (isinstance(doc[key], list) and not doc[key])


def _source_context_columns(db, message="m"):
    columns = {r[1] for r in db.execute("PRAGMA table_info(patients)")}
    name = "p.patient_name" if "patient_name" in columns else "NULL"
    kind = "p.project_type" if "project_type" in columns else "NULL"
    join = f"LEFT JOIN patients p ON p.project_id={message}.project_id" if columns else ""
    return (f"{message}.body_text,{message}.body_state,{name} patient_name,{kind} project_type",
            join)


def _source_context_row(row):
    if row is None:
        return "", None, "patient"
    body = row[0] if row[1] in (None, "full") else ""
    return body, row[2], "unknown" if row[3] == "group" else "patient"


def _source_row(db, mid):
    """(row, patients joined) for one message, with the columns and names
    message_urgency_source reads, so both readers can share it."""
    fields, join = _source_context_columns(db)
    row = db.execute(f"SELECT {fields} FROM messages m {join} WHERE m.message_id=?",
                     (mid,)).fetchone()
    return row, bool(join)


def _source_context(db, mid):
    if db is None:
        return None, None, "patient"
    return _source_context_row(_source_row(db, mid)[0])


def _canonical_finding_lines(llm: dict, *, context=(None, None, "patient")) -> list[str]:
    """Structured lines for verified facts that no legacy slot can
    carry. Interpretation qualifiers precede the shortened statement."""
    out = []
    facts = llm.get("canonical_facts")
    if not isinstance(facts, list):
        return out
    seen = set()
    for f in facts:
        if not isinstance(f, dict):
            continue
        label = _label(_FINDING_LABEL, f.get("kind"))
        statement = f.get("statement")
        fid = f.get("fact_id")
        if label is None or not isinstance(statement, str) \
                or not statement.strip() or not isinstance(fid, str) \
                or not fid.strip() or fid in seen:
            continue
        seen.add(fid)
        scope = item_scope({"evidence": f.get("evidence_quote")}, context, statement)
        if scope in ("family", "other", "unknown"):
            f = {**f, "subject": _SUBJECT_JP[scope]}
        attrs = [f"{name}:{f[key]}" for key, name in
                 (("subject", "対象"), *FACT_ATTRIBUTE_LABELS, ("event_time", "時点"))
                 if isinstance(f.get(key), str) and f[key].strip()]
        if scope in ("past", "planned", "conditional"):
            attrs.append("原文:" + _TIME_SCOPE_JP[scope])
        qualifier = f"（{'、'.join(attrs)}）" if attrs else ""
        line = f"{label}｜{qualifier}{statement.strip()}"
        quote = f.get("evidence_quote")
        if isinstance(quote, str) and quote.strip():
            line += f"（根拠:{quote.strip()}）"
        out.append(line)
    return out


# Where high urgency came from: a lexical rule match is only the 🚨 icon —
# not a clinical assessment — while the AI verdict adds its wording.
URGENCY_LABEL_PLAIN = {"llm": "🚨 緊急度高", "rule": "🚨"}   # one spelling on every surface










_QC_URGENCY_SUFFIX = {"routine": "（監査では通常判定）",
                      "unclear": "（監査では判断保留）"}


def urgency_qc_suffix(db, mid: int) -> str:
    """Display suffix marking a displayed 'llm' high badge whose newest
    current QC disagreed — empty string when QC agrees or never ran."""
    qc = clinical_urgency.urgency_qc_disagreement(db, mid)
    if qc and qc.get("jev") in _QC_URGENCY_SUFFIX:
        return _QC_URGENCY_SUFFIX[qc["jev"]]
    return ""


_VFLAG_LABEL = {"spo2": "SpO2", "sbp": "収縮期BP", "bs": "BS"}


def _head_lines(llm: dict | None, v1: dict, urgency: str | None = None,
                urgency_suffix: str = "") -> list[str]:
    return [line for _, line in _head_ranked(llm, v1, urgency, urgency_suffix)]


def _head_ranked(llm: dict | None, v1: dict, urgency: str | None = None,
                 urgency_suffix: str = "") -> list[tuple[int, str]]:
    """Head lines with their plain-surface rank, fixed where each line is built.

    The free-text summary and 要点 never go through prefix matching, so a
    summary that begins with a label word (薬剤…, 依頼…) keeps its slot."""
    selected = llm is not None
    llm = llm or {}
    lines: list[tuple[int, str]] = []
    if urgency in URGENCY_LABEL_PLAIN:
        lines.append((0, URGENCY_LABEL_PLAIN[urgency] + urgency_suffix))
    summary = llm.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append((1, summary.strip()))
    pts = [p.strip() for p in _items(llm, "points")
           if isinstance(p, str) and p.strip()]
    if pts:
        lines.append((1, "要点: " + " / ".join(pts)))
    # LLM omissions can encode negation, subject or temporal exclusions.
    rule_events = _items(v1, "events")
    if selected:
        rule_events = [event for event in rule_events if event == "media_ref"]
    events = _items(llm, "events") + rule_events
    evs = list(dict.fromkeys(event for event in events
                             if _label(EVT_LABEL, event)))
    if evs:
        lines.append((7, "区分: " + "・".join(EVT_LABEL[e] for e in evs)))
    return lines


def _lab_lines(llm: dict, body: str | None = None, *, patient_name=None, default="patient",
               multiline: bool = False) -> list[str]:
    """Reported lab values (v4) — name+value+unit plus the body's own
    out-of-range marker; the view never invents reference ranges."""
    from clinical_values import lab_candidate
    from mcs_util import locate_quote_span

    confirmed, candidates = [], []
    for lb in _items(llm, "labs"):
        if not isinstance(lb, dict) or not isinstance(lb.get("name"), str) \
                or not lb["name"]:
            continue
        if type(lb.get("value")) not in (int, float, str):
            continue
        evidence = lb.get("evidence")
        located = (isinstance(evidence, str) and body is not None
                   and locate_quote_span(body, evidence) is not None)
        contextual = (lb.get("subject") in ("family", "other")
                      or lb.get("status") == "planned" or bool(lb.get("condition")))
        scope = item_scope(lb, (body, patient_name, default), lb["name"])
        contextual = contextual or scope not in ("patient", "past")
        normalized = lab_candidate(
            lb["name"], lb["value"],
            lb.get("unit") if isinstance(lb.get("unit"), str) else None,
            evidence if located else None,
            unverified=item_unverified(lb) or contextual,
            flag=lb.get("flag") if lb.get("flag") in ("high", "low") else None,
            patient_name=patient_name)
        d = f"{lb['name']} {lb.get('value')}"
        if isinstance(lb.get("unit"), str) and lb["unit"]:
            d += lb["unit"]
        if (flag := _label(_LAB_FLAG_JP, lb.get("flag"))):
            d += f"({flag})"
        if normalized["measured_on"]:
            d += f"(測定日:{normalized['measured_on']})"
        elif (located and isinstance(lb.get("measured_on"), str)
              and lb["measured_on"] and lb["measured_on"] in evidence):
            d += f"(測定時期:{lb['measured_on']})"
        if lb.get("subject") in ("family", "other"):
            d += "(対象:" + _SUBJECT_JP[lb["subject"]] + ")"
        elif scope in ("family", "other", "unknown"):
            d += "(対象:" + _SUBJECT_JP[scope] + ")"
        if lb.get("status") in ("past", "planned"):
            d += "(" + _TIME_SCOPE_JP[lb["status"]] + ")"
        elif scope in ("past", "planned", "conditional"):
            d += "(" + _TIME_SCOPE_JP[scope] + ")"
        if isinstance(lb.get("condition"), str) and lb["condition"]:
            d += f"(条件:{lb['condition']})"
        (candidates if normalized["confirmation"] == "unverified"
         else confirmed).append(d)
        if len(confirmed) + len(candidates) == 6:
            break
    lines = []
    if confirmed:
        lines.append(_item_line("検査", confirmed, "・", multiline))
    if candidates:
        lines.append(_item_line("検査候補（未確認）", candidates, "・", multiline))
    return lines


def _symptom_line(llm: dict, v1: dict, *, context=(None, None, "patient")):
    syms, neg, seen, neg_seen = [], [], set(), set()
    llm_symptoms = [s for s in _items(llm, "symptoms")
                    if isinstance(s, dict) and isinstance(s.get("text"), str)
                    and s["text"]]
    for s in llm_symptoms:
        if s.get("subject") in ("family", "other") \
                or item_unverified(s):
            continue
        if item_scope(s, context, s["text"]) != "patient":
            continue
        if s.get("negated") or s.get("status") in ("resolved", "past"):
            if s["text"] not in neg_seen:
                neg_seen.add(s["text"])
                if s.get("negated"):
                    neg.append(s["text"])
        elif s["text"] not in seen:
            seen.add(s["text"])
            parts = []
            sev = _label(_SEVERITY_JP, s.get("severity"))
            if sev:
                parts.append(sev)
            parts += [x.strip() for x in (s.get("onset"), s.get("duration"))
                      if isinstance(x, str) and x.strip()]
            syms.append(s["text"]
                        + (f"({'・'.join(parts)})" if parts else ""))
    # An unreadable selected field can carry exclusions; do not revive rules.
    raw_symptoms = llm.get("symptoms", [])
    rule_symptoms = _items(v1, "symptoms") if isinstance(raw_symptoms, list) \
        and len(llm_symptoms) == len(raw_symptoms) else []
    for s in rule_symptoms:
        if not isinstance(s, str):
            continue
        if item_scope({}, context, s) != "patient":
            continue
        if any(x["text"] in s or s in x["text"] for x in llm_symptoms):
            continue  # Typed polarity/subject must not reappear through rules.
        if s and s not in seen:
            syms.append(s)
    if not syms and not neg:
        return None
    line = "症状: " + "、".join(syms)
    if neg:
        line += ("　" if syms else "") + "、".join(
            f"{n}なし" for n in neg)
    return line


def _med_display_name(item, *, body=None):
    name, dose = str(item["name"]), item.get("dose")
    if not isinstance(dose, str) or not dose.strip():
        return name + (f" {dose}" if dose else "")
    span = item.get("evidence_span")
    if isinstance(item.get("span"), dict):
        span = [item["span"].get("start"), item["span"].get("end")]
    if clinical_values.medication_source_dose_unrecorded(
            body, name, evidence=item.get("evidence"), evidence_span=span):
        return f"{name}（用量要確認: {dose}）"
    folded_name, folded_dose = search_fold(name), search_fold(dose)
    if not re.search(r"(?<![0-9.])" + re.escape(folded_dose) + r"(?![0-9.a-z])", folded_name):
        return f"{name} {dose}"

    # Product strength does not prove dose; a short quote also cannot
    # disprove a separate dose elsewhere in the original message.
    return f"{name}（用量要確認: {dose}）"


def _med_entries(llm: dict, v1: dict, refs=(), *, context=(None, None, "patient")) -> tuple[list, list]:
    """(confirmed, unverified) medication entries as ``(text, ref)`` —
    ``ref`` is the current dictionary annotation of exactly that mention
    (same source list index and surface name), else None. The card's
    薬剤 lines and the 💊 薬剤を確認 view share this one selection."""
    annotations = {(r["source_kind"] == "extract_v1", r["i"]): r for r in refs}

    def ref_of(rule, i, item):
        ref = annotations.get((rule, i))
        return ref if ref and ref.get("name") == item.get("name") else None

    meds = []
    for i, m in enumerate(_items(llm, "meds")):
        if not isinstance(m, dict) or not m.get("name"):
            continue
        # negated / other-person / historical meds must not read as the
        # patient's own medication (planned survives — shown as [予定])
        if not med_is_patient_current(m):
            continue
        scope = item_scope(m, context, str(m["name"]))
        if scope != "patient" and not (scope == "planned" and m.get("status") == "planned"):
            continue
        d = _med_display_name(m, body=context[0])
        if (action := _label(RX_LABEL, m.get("action"))):
            d += f"[{action}]"
        if m.get("status") == "planned":
            d += "[予定]"
        tail = []
        if (route := _label(_ROUTE_JP, m.get("route"))):
            tail.append(route)
        if isinstance(m.get("freq"), str) and m["freq"]:
            tail.append(m["freq"])
        if m.get("prn") is True:
            tail.append("頓服")
        if tail:
            d += f"({'・'.join(tail)})"
        meds.append((d, ref_of(False, i, m)))
    unverified = []
    if _empty_field(llm, "meds"):
        # v1 fallback only when the LLM saw NO meds — if it saw meds
        # but all were filtered (negated/family/past), falling back to
        # v1 would re-display the very mentions that were filtered out
        unverified.extend(
            (_med_display_name(m, body=context[0]),
             ref_of(True, i, m))
            for i, m in enumerate(_items(v1, "medications"))
            if isinstance(m, dict) and m.get("name")
            and item_scope(m, context, str(m["name"])) == "patient")
        unverified.extend(
            (f"{RX_LABEL[a['action']]}:{a['ctx']}", None)
            for a in _items(v1, "rx_actions")
            if isinstance(a, dict) and _label(RX_LABEL, a.get("action"))
            and isinstance(a.get("ctx"), str) and a["ctx"]
            and item_scope({}, context, a["ctx"]) == "patient")
    return meds, unverified


def _item_line(label: str, items: list[str], sep: str, multiline: bool) -> str:
    """``label: a、b`` — or, on the plain (layout 2 / notice) surfaces, one
    indented row per item once there are two or more, so a long list of
    drugs, requests or lab values is read down, not across. Every word
    still shows (owner rule 2026-10-05); only the line breaks change."""
    if multiline and len(items) > 1:
        return label + ":\n" + "\n".join("　・" + item for item in items)
    return f"{label}: " + sep.join(items)


def _med_lines(llm: dict, v1: dict, refs=(), *, context=(None, None, "patient"),
               multiline: bool = False) -> list[str]:
    meds, unverified = _med_entries(llm, v1, refs, context=context)

    def shown(entries):
        return [text + (f"（{note}）" if (note := _drug_note(ref)) else "")
                for text, ref in entries]

    lines = []
    if meds:
        lines.append(_item_line("薬剤", shown(meds), "、", multiline))
    if unverified:
        lines.append(_item_line("薬剤候補（未確認）", shown(unverified), "、", multiline))
    return lines


def attachment_medication_entries(db, mid: int, *, cfg=None, annotate=True, report_refs=None) -> tuple[list, list]:
    """Select ready attachment medications with the same clinical filters as post medications."""
    import attachment_extraction
    from mcs_util import load_config
    cfg = load_config() if cfg is None else cfg
    if not isinstance(cfg.get('attachment_ocr'), dict) or cfg['attachment_ocr'].get('enabled') is not True:
        return [], []
    row = db.execute("SELECT project_id FROM messages WHERE message_id=? AND body_state IS NOT 'deleted'", (mid,)).fetchone()
    if row is None:
        return [], []
    dictionary, _ = drug_map.active_dictionary(db, cfg) if annotate else (None, None)
    if dictionary is not None and not dictionary.approved:
        dictionary = None
    confirmed, uncertain = [], []
    for source in attachment_extraction.iter_sources(db, row['project_id'], mid=mid, cfg=cfg):
        refs = [{**dictionary.resolve(item['name'], i), 'source_kind': 'attachment_ocr'}
                for i, item in enumerate(_items(source['document'], 'meds'))
                if dictionary is not None and isinstance(item, dict) and item.get('name')]
        meds, unverified = _med_entries(source['document'], {}, refs,
                                      context=(source['body_text'], None, 'patient'))
        where = f"（添付#{source['attachment_id']}・{source['page']}ページ）"
        if report_refs is not None and (meds or unverified):
            report_refs.append({"message_id": mid, "artifact_id": source["extraction_artifact_id"],
                "source_key": source["source_key"], "attachment_id": source["attachment_id"],
                "page": source["page"], "mentions": [text + where for text, _ in meds + unverified]})
        confirmed.extend((text + where, ref) for text, ref in meds)
        uncertain.extend((text + where, ref) for text, ref in unverified)
    return confirmed, uncertain


def medication_entries(db, mid: int, *, cfg=None, report_refs=None) -> tuple[list, list]:
    """The post's current (confirmed, unverified) medication entries with
    their dictionary annotations — the 💊 薬剤を確認 view's input."""
    v1 = mcs_queries.current_message_extract(db, "extract_v1", mid) or {}
    meds, unverified = _med_entries(mcs_queries.current_message_facts(db, mid) or {}, v1, _drug_refs(db, mid),
                                   context=_source_context(db, mid))
    attachment_meds, attachment_unverified = attachment_medication_entries(db, mid, cfg=cfg, report_refs=report_refs)
    return meds + attachment_meds, unverified + attachment_unverified


def medication_post_ids(db, mids):
    """Yield medication post IDs in input order without dictionary annotation reads."""
    ids = iter(mids)
    batch = list(islice(ids, 1))
    if not batch:
        return
    from mcs_util import load_config
    cfg = load_config()
    fields, join = _source_context_columns(db, "source")
    rule = mcs_queries.current_message_artifact_query(mid="source.message_id")
    fact = mcs_queries.current_message_artifact_query(facts=True, mid="source.message_id", field='artifact_id')
    # Sort only the bounded ID driver, not the original body/artifact blobs.
    query = (f"SELECT requested.mid,({rule}),({fact}),{fields} "
             "FROM (SELECT value mid FROM json_each(?) ORDER BY key LIMIT -1) requested "
             "CROSS JOIN messages source ON source.message_id=requested.mid "
             f"{join}")
    while batch:
        cursor = db.execute(query, ("extract_v1", json.dumps(batch)))
        try:
            for row in cursor:
                v1 = loads_dict(row[1]) or {}
                facts = {}
                if row[2] is not None and mcs_queries.selected_artifact_proof_valid(db, row[2]):
                    artifact = db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (row[2],)).fetchone()
                    facts = loads_dict(artifact['content']) if artifact else None
                    facts = facts or {}
                    facts.pop('__mcs_internal_proof_v1', None)
                if (any(_med_entries(facts, v1, context=_source_context_row(row[3:])))
                        or any(attachment_medication_entries(db, row[0], cfg=cfg, annotate=False))):
                    yield row[0]
        finally:
            cursor.close()
        batch = list(islice(ids, 400))


# per-item prefix inside the 依頼: line — a plan of the poster or a
# question is not an order to somebody (#20 order 3)
_REQ_KIND_PREFIX = {"self_plan": "予定:", "question": "確認依頼:"}


# owner 2026-10-14: only these pharmacy details reach shared cards; every other
# detail, 基本情報 and 連絡先 stay in the private summary and detail reads.
_CARD_DETAIL_LABEL = {"storage_status": "保管状況", "storage_place": "保管場所",
                      "medication_manager": "管理者", "inquiry_result": "照会・提案の結果",
                      "interaction_check": "重複・相互作用の確認"}
_CONTEXT_SUBJECT_JP = {"family": "家族", "other": "本人以外", "unspecified": "対象者の記載なし"}


def _pharmacy_detail_lines(db, mid: int) -> list[str]:
    """One short line per allowed detail, quoting only its own source value."""
    from read_model import message_patient_context, reference_text
    row = db.execute("SELECT project_id,message_id,content_hash,body_state,body_text,posted_at_ts "
                     "FROM messages WHERE message_id=?", (mid,)).fetchone()
    if row is None:
        return []
    lines = []
    for item in message_patient_context(db, dict(row)):
        scope = _CONTEXT_SUBJECT_JP.get(item.get("subject"))
        for detail in item.get("details") or []:
            label = _CARD_DETAIL_LABEL.get(detail.get("key"))
            if label is None:
                continue
            line = (f"服薬管理（{label}）: 「{str(detail['value'])[:40]}」"
                    + (f"(対象:{scope})" if scope else "") + reference_text(detail.get("refers_to")))
            if line not in lines:
                lines.append(line)
    return lines


def _request_lines(llm: dict, v1: dict, *, multiline: bool = False) -> list[str]:
    reqs, cands = [], []
    for r in _items(llm, "requests"):
        if isinstance(r, dict) and (r.get("to") or r.get("action")):
            to = str(r.get("to") or "")
            to = "" if to in ("", "不明", "unknown", "-") else f"{to}へ"
            frm = str(r.get("from") or "")
            prefix = "" if frm in ("", "不明", "unknown", "-") \
                else f"{frm}→"
            kind = r.get("kind")
            if isinstance(kind, str):
                prefix = _REQ_KIND_PREFIX.get(kind, "") + prefix
            # a relative deadline (due null, due_text kept verbatim) is
            # still a deadline to the reader
            due = r.get("due")
            due = due if isinstance(due, str) and due else r.get("due_text")
            suffix = f"(期限:{due})" if isinstance(due, str) and due \
                else ""
            # owner rule 2026-10-05: the 要約 shows every word — no caps
            cond = r.get("condition")
            cond = cond if isinstance(cond, str) else ""
            if cond:
                suffix += f"(条件:{cond})"
            action = str(r.get("action") or "")
            # negated/speculative/ungrounded requests must not read as
            # confirmed; any flag other than a literal False fails closed
            (cands if item_unverified(r) else reqs).append(
                prefix + to + action + suffix)
    # rule fallback only when the selected facts carry no request at all
    if _empty_field(llm, "requests"):
        reqs.extend(("" if (_label(REQ_LABEL, r.get("kind")) or "依頼") == "依頼"
                     else f"{_label(REQ_LABEL, r.get('kind'))}:") + r["ctx"]
                    for r in _items(v1, "requests")
                    if isinstance(r, dict) and isinstance(r.get("ctx"), str)
                    and r["ctx"])
    lines = [_item_line("依頼", reqs, " / ", multiline)] if reqs else []
    if cands:
        lines.append(_item_line("依頼候補（未確認）", cands, " / ", multiline))
    return lines


def structured_lines(db, mid: int, *, drug_candidates: bool = True,
                     plain: bool = False) -> list[str]:
    """Compact structured summary from extract_v1 + the current fact
    artifact (canonical_projection shadows extract_llm).  Returns []
    when nothing usable exists (caller falls back to raw only)."""
    v1 = mcs_queries.current_message_extract(db, "extract_v1", mid) or {}
    selected = mcs_queries.current_message_facts(db, mid)
    llm = selected or {}
    if not v1 and not llm:
        return []
    source, joined = (None, False) if db is None else _source_row(db, mid)
    # without a patients table the urgency read keeps its own source query (and its error)
    details = clinical_urgency.message_urgency_details(
        db, mid, facts=selected, rule=v1, **({"source": source} if joined else {}))
    urgency = details["source"]
    # Fact artifacts (canonical projections) carry no urgency fields —
    # the extract_llm row behind them holds evidence and vital_flags.
    ext = (mcs_queries.current_message_extract(db, "extract_llm", mid) or {}) \
        if urgency or llm else {}
    suffix = ""
    if urgency == "llm":
        # quote only the patient-scoped reason; raw evidence[0] may be a family member's
        if details["reasons"]:
            suffix = f" — 根拠:「{str(details['reasons'][0])[:40]}」"
        suffix += urgency_qc_suffix(db, mid)
    head = _head_ranked(selected, v1, urgency, suffix)
    lines: list[str] = [line for _, line in head]
    if details["held"]:
        lines.append("急ぎの確認依頼（本人の緊急状態とは別）" if details["kind"] == "request" else
                     "緊急度: 要確認（対象人物・時点の根拠を確認）" if plain else
                     "緊急度: 要確認（対象人物・時点の根拠を確認。元の判定は高）")
    elif details["kind"] == "request":
        lines.append("急ぎの確認依頼（本人の緊急状態とは別）")
    context = (None, None, "patient") if db is None else _source_context_row(source)
    body, patient_name, default = context
    # Public source-less views cannot prove a measurement. Direct helpers and
    # nullable-body legacy rows retain their parsed-only compatibility.
    vital_context = ("", patient_name, default) if db is None else context
    if (line := vital_line(selected, v1, vital_context[0], patient_name=patient_name, default=default)) is not None:
        lines.append(line)
    vital_flags = llm.get("vital_flags") or ext.get("vital_flags") or []
    flags = [_VFLAG_LABEL.get(f["key"], f["key"]) + f" {f['value']:g}"
             for f in (vital_flags if isinstance(vital_flags, list) else [])
             if isinstance(f, dict) and _label(_VFLAG_LABEL, f.get("key"))
             and type(f.get("value")) in (int, float)
             and scoped_vitals({f["key"]: f["value"]}, vital_context).get(f["key"]) == f["value"]]
    if flags and body:
        lines.append("閾値超過の測定値: " + "、".join(flags))
    if _items(llm, "labs"):
        lines.extend(_lab_lines(llm, body, patient_name=patient_name, default=default,
                                multiline=plain))
    if (line := _symptom_line(llm, v1, context=context)) is not None:
        lines.append(line)
    lines.extend(_med_lines(llm, v1, _drug_refs(db, mid) if drug_candidates else (),
                            context=context, multiline=plain))
    if db is not None:
        lines.extend(_pharmacy_detail_lines(db, mid))
    lines.extend(_request_lines(llm, v1, multiline=plain))
    for mp in _items(v1, "med_periods"):
        if isinstance(mp, dict) and mp.get("start") and item_scope(mp, context, mp.get("raw")) == "patient":
            lines.append(f"服薬期間: {mp['start']}〜{mp.get('end') or '?'}")
            break
    if v1.get("next_planned"):
        lines.append(f"次回予定: {v1['next_planned']}")
    lines.extend(_canonical_finding_lines(llm, context=context))
    if plain:
        # owner 2026-10-08: one fixed reading order on new cards and notices
        # (missing items are simply absent); layout-1 cards keep the old order
        ranks = [rank for rank, _ in head] + [_summary_rank(line) for line in lines[len(head):]]
        lines = [line for _, line in sorted(zip(ranks, lines), key=lambda pair: pair[0])]
    return lines


# 緊急度 → 概要・要点 → 依頼 → 薬剤 → 臨床所見（バイタル・検査・症状） → 予定 → 区分
_SUMMARY_ORDER = (
    (0, ("🚨", "緊急度", "急ぎの確認依頼")),
    (2, ("依頼",)),
    (3, ("薬剤", "服薬期間", "服薬管理（")),
    (4, ("バイタル", "閾値超過", "検査", "症状")),
    (5, ("次回予定",)),
    (7, ("区分",)),
)


def _summary_rank(line: str) -> int:
    # generated label lines only: head lines carry their rank from _head_ranked
    for rank, prefixes in _SUMMARY_ORDER:
        if line.startswith(prefixes):
            return rank
    # canonical findings read 「ラベル｜…」 next to the other findings
    return 4 if "｜" in line.split(" ", 1)[0] else 1
