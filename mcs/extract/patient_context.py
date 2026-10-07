"""Extract and read source-bound patient context without inferring current clinical state."""
import re

from mcs_util import locate_quote_span


LABELS = {
    "demographics": ("基本情報", "患者情報", "生年月日", "年齢", "性別", "住所"),
    "diagnoses": ("主病名", "診断名", "傷病名", "病名", "疾患"),
    "history": ("既往歴", "既往", "手術歴", "病歴"),
    "course": ("現病歴", "経過", "紹介経緯", "経緯", "入退院歴"),
    "adl": ("ADL", "日常生活動作", "移動", "歩行", "排泄", "入浴"),
    "living": ("生活環境", "家族構成", "生活歴", "同居", "独居"),
    "care_level": ("介護度", "要介護", "要支援", "介護保険"),
    "care_services": ("利用サービス", "サービス", "訪問看護事業所", "訪問介護事業所", "訪問看護", "訪問介護", "デイサービス"),
    "care_team": ("担当", "担当者", "ケアマネ", "医療機関", "主治医"),
    "contacts": ("キーパーソン", "緊急連絡先", "連絡先"),
    "allergies": ("アレルギー", "アレルギー歴", "副作用歴", "薬剤アレルギー"),
    "devices": ("医療処置", "医療機器", "在宅酸素", "カテーテル", "胃ろう", "ストーマ"),
    "preferences": ("療養方針", "治療方針", "本人の意向", "家族の意向", "意向", "希望", "方針", "ACP"),
    "medication_management": ("服薬管理", "薬剤管理", "内服薬", "処方薬", "処方", "服薬"),
    "nutrition": ("食事", "栄養", "嚥下", "水分摂取"),
    "cognition": ("認知機能", "認知", "意思疎通"),
    "adverse_events": ("薬物有害事象", "副作用", "被疑薬", "副作用症状", "副作用発生日", "副作用への対応", "副作用転帰"),
    "observations": ("検査・観測値", "検査値", "観測値", "体重", "身長", "Cr", "eGFR", "AST", "ALT", "血糖"),
    "followup": ("変更後の経過", "退院後の経過", "退院時処方", "支援後の変化", "次回確認事項", "次回確認", "確認期限"),
}
DETAIL_LABELS = {
    "drug_name": ("対象薬", "薬剤名", "被疑薬"),
    "medication_kind": ("薬の区分", "薬剤区分"),
    "prescriber": ("処方元", "処方医療機関"),
    "actual_dose": ("実際の服用量", "実服用量"),
    "actual_frequency": ("実際の飲み方", "実服用頻度"),
    "residual_quantity": ("残薬", "残薬量"),
    "missed_doses": ("飲み忘れ", "飲み忘れ状況"),
    "symptom": ("副作用症状", "有害事象の症状"),
    "onset": ("副作用発生日", "発生日", "発症時期"),
    "response": ("副作用への対応", "対応内容"),
    "outcome": ("副作用転帰", "転帰", "支援後の変化"),
    "certainty": ("確認状態", "確定状態"),
    "observation_name": ("検査項目", "観測項目"),
    "value": ("測定値", "検査結果"),
    "unit": ("測定単位", "単位"),
    "measured_on": ("測定日", "採血日", "検査日"),
    "condition": ("測定条件", "採血条件", "検体"),
    "swallowing": ("嚥下", "嚥下状況"),
    "self_management": ("服薬自己管理", "自己管理能力"),
    "supporter": ("服薬支援者", "支援者"),
    "support_method": ("服薬支援", "支援方法", "一包化", "剤形の扱いやすさ"),
    "followup": ("次回確認事項", "次回確認"),
    "assignee": ("確認担当", "担当者"),
    "due_text": ("確認期限", "期限"),
    "observed_on": ("観測日", "観察日"),
    "last_confirmed_on": ("最終確認日", "確認日"),
    "confirmed_by": ("確認者",),
}
DETAIL_KEYS = frozenset(DETAIL_LABELS)
_DETAIL_ALIAS = {label: key for key, labels in DETAIL_LABELS.items() for label in labels}
LABELS["medication_management"] += ("他院処方", "市販薬", "OTC", "サプリメント", "サプリ", "残薬", "飲み忘れ", "処方元", "実服用量", "実服用頻度", "服薬自己管理", "服薬支援者", "服薬支援", "一包化")
CATEGORIES = frozenset(LABELS)
_CATEGORY = {label: key for key, labels in LABELS.items() for label in labels}
_CATEGORY["副作用歴"] = "adverse_events"
for label, key in _DETAIL_ALIAS.items():
    _CATEGORY.setdefault(label, "observations" if key in (
        "observation_name", "value", "unit", "measured_on", "condition", "observed_on") else
        "adverse_events" if key == "symptom" else "course" if key in ("onset", "response", "outcome") else
        "followup" if key in ("followup", "assignee", "due_text", "last_confirmed_on", "confirmed_by", "certainty") else
        "medication_management")
_HEADING = re.compile(
    r"^[ \t　]*(?:[■●◆#・＊*]+[ \t　]*)?(?:[【［\[（(][ \t　]*)?"
    r"(?P<label>" + "|".join(map(re.escape, sorted(_CATEGORY, key=len, reverse=True))) + r")"
    r"(?:[ \t　]*[】］\]）)][ \t　]*[:：]?[ \t　]*|[ \t　]*[:：][ \t　]*|[ \t　]*$)",
    re.IGNORECASE)
_BOUNDARY = re.compile(
    r"^[ \t　]*(?:[■●◆#]+|[【［\[][^】］\]\n]+[】］\]]|[^\s:：]{1,24}[:：])")
_MEASUREMENT = re.compile(r"^[ \t　]*(\d+(?:[.．]\d+)?)(?![\d.．/／年月日-])[ \t　]*(kg|cm|mg/dL|g/dL|mL/min/1\.73m2|U/L|mmol/L|%|％)?", re.I)
_REPORTED_DATE = re.compile(r"(?<!\d)\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(?!\d)")


def detail_items(items, body: str, parent_span) -> list[dict]:
    """Keep each raw attribute only inside its parent's unique source quote."""
    if not isinstance(items, list) or not isinstance(body, str) or parent_span is None:
        return []
    result = []
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("key"), str)
                or item["key"] not in DETAIL_KEYS or not isinstance(item.get("value"), str)
                or not item["value"].strip() or not isinstance(item.get("evidence"), str)):
            continue
        span = locate_quote_span(body, item["evidence"])
        if (span is None or span[0] < parent_span[0] or span[1] > parent_span[1]
                or item["value"] not in body[span[0]:span[1]]):
            continue
        clean = {"key": item["key"], "value": item["value"], "evidence": body[span[0]:span[1]]}
        if clean not in result:
            result.append(clean)
    return result


def extract_context(body: str) -> list[dict]:
    """Preserve explicit labelled sections in full, including negation and historical wording."""
    if not isinstance(body, str):
        return []
    result = []
    active = None
    offset = 0

    def finish(end):
        if active is None:
            return
        start, value_start, category = active
        text = body[value_start:end].strip()
        if text:
            item = {"category": category, "text": text,
                           "evidence": body[start:end].strip(),
                           "origin": "explicit_heading", "subject": "unspecified"}
            # Labelled attributes retain raw values; no clinical status or dates are inferred.
            header = _HEADING.match(body[start:end].splitlines()[0])
            alias = header["label"] if header else ""
            key = next((value for label, value in _DETAIL_ALIAS.items()
                        if label.casefold() == alias.casefold()), None)
            if key:
                item["details"] = [{"key": key, "value": text, "evidence": item["evidence"]}]
            elif category == "observations" and alias.casefold() in ("体重", "身長", "cr", "egfr", "ast", "alt", "血糖"):
                details = [{"key": "observation_name", "value": alias, "evidence": item["evidence"]}]
                measure = _MEASUREMENT.match(text)
                if measure:
                    details.append({"key": "value", "value": measure[1], "evidence": item["evidence"]})
                    if measure[2]:
                        details.append({"key": "unit", "value": measure[2], "evidence": item["evidence"]})
                dates = list(_REPORTED_DATE.finditer(text))
                if len(dates) == 1 and (re.search(r"(?:測定|計測|採血|検査)(?:日|日時)?\s*[:：]?\s*$", text[:dates[0].start()])
                                        or re.match(r"\s*(?:に|の)?(?:測定|計測|採血|検査)", text[dates[0].end():])):
                    details.append({"key": "measured_on", "value": dates[0][0], "evidence": item["evidence"]})
                item["details"] = details
            result.append(item)

    for line in body.splitlines(keepends=True):
        match = _HEADING.match(line.rstrip("\r\n"))
        if match or _BOUNDARY.match(line):
            finish(offset)
            active = None
            if match:
                label = match["label"]
                category = next(key for key in _CATEGORY if key.casefold() == label.casefold())
                active = (offset, offset + match.end(), _CATEGORY[category])
        offset += len(line)
    finish(len(body))
    return result


def context_items(doc, body: str) -> list[dict]:
    """Read only well-shaped context with a unique verbatim source and contained value."""
    if not isinstance(body, str):
        return []
    items = doc.get("patient_context") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("category"), str)
                or item["category"] not in CATEGORIES):
            continue
        text, quote = item.get("text"), item.get("evidence")
        if not isinstance(text, str) or not isinstance(quote, str) or not text.strip():
            continue
        span = locate_quote_span(body, quote)
        if span is None or text not in body[span[0]:span[1]]:
            continue
        if item.get("subject") not in ("patient", "family", "other", "unspecified"):
            continue
        clean = {**item, "evidence": body[span[0]:span[1]]}
        if "details" in item:
            clean["details"] = detail_items(item["details"], body, span)
        result.append(clean)
    return result


def merged_context(v1, llm, body: str) -> list[dict]:
    """Keep distinct reports rather than overwrite conflicting or historical information."""
    result, seen = [], {}
    for doc in (v1, llm):
        for item in context_items(doc, body):
            key = (item["category"], item["text"], item["evidence"], item["subject"])
            if key not in seen:
                result.append(item)
                seen[key] = item
            elif item.get("details"):
                prior = seen[key]
                for detail in item["details"]:
                    if detail not in prior.setdefault("details", []):
                        prior["details"].append(detail)
    return result
