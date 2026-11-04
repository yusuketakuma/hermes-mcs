"""Preserve OCR medical sections and literal pump candidates with exact page evidence."""
import hashlib
import re


SCHEMA = "medical-document/v1"
_HEADINGS = {
    "diagnosis": ("傷病名", "診断名", "主病名"),
    "past_history": ("既往歴", "既往症", "手術歴"),
    "family_history": ("家族歴",),
    "current_illness": ("現病歴", "病状経過", "症状経過", "現在の病状", "病状の経過"),
    "medications": ("現在の処方", "現在の処方薬", "処方薬", "処方内容", "投薬内容", "内服薬", "服薬歴", "服薬等の状況"),
    "allergies": ("アレルギー", "薬剤アレルギー", "不耐症"),
    "labs_vitals": ("検査結果", "検査所見", "検査データ", "バイタルサイン", "身体所見"),
    "patient_background": ("生活背景", "社会的背景", "身体的背景", "患者背景", "家庭での介護の状況", "家族等との関係", "主な介護者"),
    "adl_cognition": ("ADL", "認知機能", "認知症", "要介護度", "日常生活自立度"),
    "nutrition": ("栄養", "栄養状態", "食事", "摂食嚥下"),
    "care_plan": ("治療経過", "治療内容", "看護指示", "療養上の注意", "留意事項", "緊急時の連絡先", "療養上の課題・支援内容", "評価"),
    # MHLW 2026-03-27 report/record fields: https://www.mhlw.go.jp/content/12400000/001681763.pdf
    "care_provided": ("看護・リハビリテーションの内容", "看護の内容", "実施した看護内容", "実施した看護・リハビリテーションの内容"),
    "report_period": ("対象期間", "報告対象期間", "訪問日", "実際の訪問日時", "訪問年月日"),
    "supplies": ("衛生材料等の使用量および使用状況", "衛生材料等の種類・量の変更"),
    "mental_status": ("GAF", "ＧＡＦ", "Ｇ Ａ Ｆ", "精神状態"),
    "devices": ("医療機器", "使用医療機器", "カテーテル", "処置", "PCAポンプ", "PCA ポンプ"),
    "referral_purpose": ("紹介目的", "依頼目的"),
    "goals_of_care": ("療養方針", "治療方針", "本人の意向", "ACP", "意思決定支援"),
    "document_metadata": ("文書名", "作成日", "発行日", "診療情報提供書", "訪問看護指示書", "退院時サマリー",
                          "訪問看護報告書", "精神科訪問看護報告書", "訪問看護計画書", "精神科訪問看護計画書",
                          "訪問看護記録書", "訪問看護記録書Ⅰ", "訪問看護記録書Ⅱ", "訪問看護記録書Ⅲ",
                          "精神科訪問看護記録書Ⅰ", "精神科訪問看護記録書Ⅱ",
                          "訪問薬剤管理指導報告書", "居宅療養管理指導報告書", "リハビリテーション報告書"),
    "past_and_family_history": ("既往歴及び家族歴",),
    "course_and_labs": ("症状経過及び検査結果",),
    "other": ("備考",),
}
_CATEGORY = {label: category for category, labels in _HEADINGS.items() for label in labels}
_COMPACT_HEADINGS = {re.sub(r"\s+", "", label): label for label in _CATEGORY}
_HEADING_LABELS = "|".join(map(re.escape, sorted(_CATEGORY, key=len, reverse=True)))
_HEADING = re.compile(
    r"^[ \t]*(?:[【\[（(](?P<bracket>" + _HEADING_LABELS
    + r")[】\]）)]|(?P<plain>" + _HEADING_LABELS
    + r")(?=$|[：:\t ]))")
_COLUMN_HEADING = re.compile(r"(?:^|[\t |｜])[【\[（(]?(?:" + _HEADING_LABELS + r")[：:】\]）)]")
_PUMP_LABELS = {
    "remaining_volume": ("PCA残量", "残液量", "残量"),
    "continuous_rate": ("持続注入速度", "持続投与速度", "持続流量", "ベース流量", "持続量"),
    "bolus_dose": ("ボーラス投与量", "ボーラス量", "ボーラス", "PCAドーズ量", "PCA投与量"),
    "lockout_interval": ("ロックアウト時間", "ロックアウト間隔", "ロックアウト"),
    "total_infused": ("総注入量", "総投与量", "積算量"),
    "pca_requested": ("PCA要求回数", "PCAリクエスト回数", "PCA試行回数"),
    "pca_delivered": ("PCA投与回数", "PCA実施回数", "PCA有効回数"),
    "drug_concentration": ("薬剤濃度", "薬液濃度"),
}
_PUMP_FIELD = {label: field for field, labels in _PUMP_LABELS.items() for label in labels}
_PUMP_LABEL = re.compile("|".join(map(re.escape, sorted(_PUMP_FIELD, key=len, reverse=True))))
_MASS = r"(?:mg|μg|µg|mcg|g)"
_VOLUME = r"(?:mL|ml|ML)"
_UNITS = {
    "remaining_volume": _VOLUME,
    "continuous_rate": rf"(?:{_VOLUME}|{_MASS})\s*/\s*(?:h|hr|時間|min|分)",
    "bolus_dose": rf"(?:{_VOLUME}|{_MASS})",
    "lockout_interval": r"(?:分|min|時間|h|hr|秒|s)",
    "total_infused": rf"(?:{_VOLUME}|{_MASS})",
    "pca_requested": r"回",
    "pca_delivered": r"回",
    "drug_concentration": rf"(?:{_MASS}\s*/\s*{_VOLUME}|%)",
}
_QUANTITIES = {field: re.compile(r"(?P<number>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>" + unit + ")")
               for field, unit in _UNITS.items()}


def extract(pages):
    """Return source-bound sections and unverified same-line pump quantities without clinical inference."""
    sections, candidates = [], []
    for page in pages:
        text = page["text"]
        source = {"source_kind": "attachment_ocr", "page": page["page"],
                  "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        start, offset, category, heading, section_status = 0, 0, "other", None, "literal_candidate"
        pump_context = re.search(r"PCA|ポンプ", text, re.IGNORECASE) is not None
        lines = text.splitlines(keepends=True)
        continuation_until = 0
        for index, line in enumerate(lines):
            match = _HEADING.match(line.rstrip("\r\n")) if index >= continuation_until else None
            next_heading = (match["bracket"] or match["plain"]) if match else None
            if next_heading is None and index >= continuation_until:
                candidate = ""
                for end in range(index, min(index + 3, len(lines))):
                    if not lines[end].strip() or len(candidate) + len(lines[end]) > 100:
                        break
                    candidate += lines[end]
                    compact = re.sub(r"\s+", "", candidate)
                    if compact in _COMPACT_HEADINGS:
                        next_heading, continuation_until = _COMPACT_HEADINGS[compact], end + 1
                        break
                    if not any(label.startswith(compact) for label in _COMPACT_HEADINGS):
                        break
            if next_heading is not None:
                if offset > start:
                    sections.append({**source, "category": category, "heading": heading,
                                     "text": text[start:offset], "evidence_span": [start, offset],
                                     **({"date_state": "unknown"} if category == "report_period" else {}),
                                     "status": section_status})
                start = offset
                heading = next_heading
                category = _CATEGORY[heading]
                section_status = "literal_candidate"
                if len(_COLUMN_HEADING.findall(line)) > 1:
                    category, section_status = "other", "needs_review"
            labels = list(_PUMP_LABEL.finditer(line))
            for label in labels:
                field = _PUMP_FIELD[label[0]]
                raw = line[label.end():].strip().lstrip("：:").strip()
                quantity = _QUANTITIES[field].fullmatch(raw) if len(labels) == 1 else None
                # A page-level pump heading permits labels on following lines; it never supplies their values.
                literal = quantity is not None and pump_context
                candidates.append({**source, "field": field, "label": label[0], "raw_value": raw,
                                   "evidence": line, "evidence_span": [offset, offset + len(line)],
                                   "status": "literal_candidate" if literal else "needs_review",
                                   **({"number": quantity["number"], "unit": quantity["unit"]}
                                      if literal else {})})
            offset += len(line)
        if start < len(text):
            sections.append({**source, "category": category, "heading": heading,
                             "text": text[start:], "evidence_span": [start, len(text)],
                             **({"date_state": "unknown"} if category == "report_period" else {}),
                             "status": section_status})
    return {"schema": SCHEMA, "source_kind": "attachment_ocr", "sections": sections,
            "pump_candidates": candidates}
