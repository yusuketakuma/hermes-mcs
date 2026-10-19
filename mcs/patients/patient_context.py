"""Extract and read source-bound patient context without inferring current clinical state."""
import clinical_scope
import math
import re


from mcs_util import locate_quote_span


LABELS = {
    "demographics": ("基本情報", "患者情報", "生年月日", "年齢", "性別", "住所", "訪問先", "訪問先住所"),
    "diagnoses": ("主病名", "診断名", "傷病名", "病名", "疾患", "現病名"),
    "history": ("既往歴", "既往", "手術歴", "病歴"),
    "course": ("現病歴", "経過", "紹介経緯", "経緯", "入退院歴", "様子", "状態", "全身状態", "体調", "状態変化", "急変",
               "入院", "入院日", "退院", "退院日", "退院予定", "転院", "入所", "退所", "救急搬送", "受診", "通院", "往診",
               "看取り期", "逝去", "死亡"),
    "services_provided": ("実施内容", "訪問内容", "看護内容", "介護内容", "ケア内容", "提供内容", "訪問実績", "訪問日",
                          "訪問時間", "訪問診療日", "緊急訪問", "臨時訪問", "訪問中止", "キャンセル", "不在", "身体介護",
                          "生活援助", "入浴介助", "清拭", "洗髪", "排泄介助", "食事介助", "移乗介助", "服薬介助", "調理",
                          "掃除", "洗濯", "買い物", "通院介助", "見守り"),
    "incidents": ("事故・ヒヤリハット", "事故", "事故報告", "ヒヤリハット", "インシデント", "転倒", "転落", "誤嚥", "誤薬", "飲み間違い", "離設",
                  "行方不明", "外傷", "骨折", "熱傷", "窒息"),
    "adl": ("ADL", "日常生活動作", "移動", "歩行", "入浴", "寝返り", "起き上がり", "起居", "座位", "座位保持", "立位",
            "移乗", "車椅子", "階段", "食事動作", "整容", "更衣", "着替え", "トイレ動作", "排泄動作", "寝たきり度",
            "日常生活自立度", "IADL", "家事", "金銭管理", "外出", "リハビリ", "リハビリテーション", "機能訓練", "訓練",
            "訓練内容", "関節可動域", "ROM", "筋力", "麻痺", "拘縮", "バランス", "歩行能力", "補助具", "杖", "歩行器",
            "興味関心", "活動と参加", "参加"),
    "living": ("生活環境", "家族構成", "生活歴", "同居", "独居", "住環境", "居住環境", "駐車場", "駐車", "訪問時の注意", "睡眠", "睡眠状況", "生活リズム",
               "昼夜逆転", "中途覚醒", "日課", "活動", "活動量", "日中の活動", "家族等の状況", "家族の状況", "主介護者",
               "介護者", "介護負担", "介護力", "家族の健康", "就労", "面会", "社会との関わり", "社会参加", "経済状況",
               "生活保護", "年金", "費用負担", "障害者手帳", "難病", "公費", "医療費助成", "成年後見", "後見", "権利擁護",
               "虐待", "身寄り", "外国人"),
    "care_level": ("介護度", "要介護", "要支援", "介護保険", "要介護度", "要介護状態区分", "認定", "認定情報", "有効期間",
                   "区分変更", "更新申請", "申請中", "支給限度", "認定調査", "主治医意見書"),
    "care_services": ("利用サービス", "サービス", "訪問看護事業所", "訪問介護事業所", "訪問看護", "訪問介護", "デイサービス",
                      "通所", "デイケア", "ショートステイ", "短期入所", "訪問リハ", "訪問診療", "訪問入浴", "訪問歯科",
                      "福祉用具", "福祉用具貸与", "住宅改修", "配食", "居宅療養管理指導", "定期巡回", "小規模多機能",
                      "看多機", "利用票", "週間サービス計画", "サービス種別", "事業所", "事業所名", "頻度", "曜日"),
    "care_team": ("担当", "担当者", "ケアマネ", "医療機関", "主治医", "事業者名", "事業者", "関係事業所", "関係機関", "歯科医", "歯科衛生士", "管理栄養士", "理学療法士",
                  "作業療法士", "言語聴覚士", "PT", "OT", "ST", "ヘルパー", "サービス提供責任者", "相談員", "MSW"),
    "contacts": ("キーパーソン", "緊急連絡先", "連絡先", "電話番号", "電話", "TEL"),
    "allergies": ("アレルギー", "アレルギー歴", "薬剤アレルギー"),
    "devices": ("医療処置", "医療機器", "在宅酸素", "カテーテル", "胃ろう", "ストーマ", "処置", "吸引", "気管切開",
                "気管カニューレ", "経管栄養", "胃瘻", "経鼻", "中心静脈栄養", "CV", "ポート", "点滴", "輸液", "導尿",
                "膀胱留置カテーテル", "バルーン", "人工肛門", "人工膀胱", "HOT", "人工呼吸器", "NPPV", "CPAP", "インスリン",
                "自己注射", "血糖測定", "衛生材料"),
    "preferences": ("療養方針", "治療方針", "本人の意向", "家族の意向", "意向", "希望", "方針", "ACP", "主訴", "本人の希望",
                    "家族の希望", "看取り", "DNAR", "延命", "蘇生", "事前指示", "人生会議", "最期の場所", "療養場所"),
    "medication_management": ("服薬管理", "薬剤管理", "内服薬", "処方薬", "処方", "服薬",
                              "他院処方", "市販薬", "OTC", "サプリメント", "サプリ", "残薬", "飲み忘れ", "処方元",
                              "実服用量", "実服用頻度", "服薬自己管理", "服薬支援者", "服薬支援", "一包化", "処方変更",
                              "麻薬", "医療用麻薬", "頓服", "点眼", "貼付", "吸入", "与薬", "服薬カレンダー", "お薬カレンダー",
                              "配薬", "薬剤数", "ポリファーマシー", "持参薬", "トレーシングレポート", "服薬情報提供"),
    "nutrition": ("食事", "栄養", "嚥下", "水分摂取", "食事量", "摂取量", "食事摂取", "水分", "水分量", "食形態",
                  "主食形態", "副食形態", "とろみ", "栄養補給", "栄養状態", "低栄養", "低栄養リスク", "体重減少", "食欲",
                  "食事制限", "塩分制限", "嚥下調整食", "むせ"),
    "excretion": ("排泄", "排泄状況", "排尿", "排便", "失禁", "おむつ", "パッド", "最終排便", "便秘対策", "摘便", "浣腸",
                  "尿量", "排便回数", "排尿回数", "頻尿", "夜間排尿"),
    "skin_wound": ("皮膚・褥瘡", "皮膚", "皮膚状態", "褥瘡", "褥瘡処置", "発赤", "表皮剥離", "スキンテア", "創部", "創処置", "潰瘍",
                   "水疱", "掻痒", "爪", "白癬", "部位", "発生部位"),
    "oral_health": ("口腔", "口腔内", "口腔ケア", "口腔衛生", "口腔状態", "義歯", "入れ歯", "歯", "咀嚼", "口臭", "口腔乾燥",
                    "舌苔", "歯科受診", "歯科"),
    "cognition": ("認知機能", "認知", "意思疎通", "認知症", "判断能力", "意思決定", "BPSD", "行動・心理症状", "周辺症状",
                  "行動", "精神状態", "不穏", "興奮", "暴言", "徘徊", "帰宅願望", "介護抵抗", "幻覚", "妄想", "抑うつ", "不安",
                  "意欲", "拒否", "拒薬", "拒食", "視力", "聴力", "失語", "コミュニケーション"),
    "adverse_events": ("薬物有害事象", "副作用", "被疑薬", "副作用症状", "副作用発生日", "副作用への対応", "副作用転帰",
                       "副作用歴"),
    "observations": ("検査・観測値", "検査値", "観測値", "体重", "身長", "Cr", "eGFR", "AST", "ALT", "血糖", "バイタル",
                     "バイタルサイン", "血圧", "脈拍", "体温", "呼吸数", "SpO2", "酸素飽和度", "BMI", "NRS", "疼痛スケール",
                     "Barthel", "BI", "FIM", "MMSE", "HDS-R", "DESIGN-R", "MNA", "握力", "歩行速度"),
    "plan_goals": ("計画・目標", "ケアプラン", "居宅サービス計画", "計画書", "看護計画", "リハビリ計画", "リハ計画", "栄養ケア計画",
                   "訪問介護計画", "施設サービス計画", "ニーズ", "課題", "長期目標", "短期目標", "目標", "援助方針",
                   "総合的な援助の方針", "モニタリング", "評価", "達成状況", "サービス担当者会議", "担当者会議",
                   "カンファレンス", "退院前カンファレンス", "会議", "検討内容", "結論", "残された課題", "計画期間"),
    "orders_documents": ("指示・連携文書", "指示", "医師の指示", "指示書", "訪問看護指示書", "特別指示", "特別訪問看護指示書", "点滴指示",
                         "指示期間", "指示内容", "診療情報提供書", "情報提供書", "入院時情報提供書", "紹介状", "看護サマリー",
                         "退院時サマリー", "薬剤管理サマリー", "サマリー", "報告書", "報告", "主治医報告", "連絡", "連携",
                         "提供済み", "報告済み"),
    "followup": ("変更後の経過", "退院後の経過", "退院時処方", "支援後の変化", "次回確認事項", "次回確認", "確認期限",
                 "次回訪問", "訪問予定", "受診予定", "次回受診", "指示待ち", "要報告"),
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
    # bare 保管状況/保管場所 also head documents (同意書, 紹介状); only drug-specific headings
    "storage_status": ("薬剤保管状況", "薬の保管状況"),
    "storage_place": ("薬剤保管場所", "薬の保管場所", "薬の置き場所"),
    "medication_manager": ("服薬管理者", "薬の管理者"),
    # a bare 照会結果 is often not about a drug (担当者会議, 営業時間), so no generic alias
    "inquiry_result": ("疑義照会結果", "処方提案結果"),
    "interaction_check": ("重複・相互作用確認", "相互作用確認", "重複投薬確認"),
    # period of an order/certification/plan is a span, never a due date (due_text) or an event date
    "valid_period": ("有効期間", "指示期間", "計画期間"),
    "body_site": ("部位", "発生部位"),
    "food_texture": ("食形態", "主食形態", "副食形態", "とろみ"),
}
DETAIL_KEYS = frozenset(DETAIL_LABELS)
_DETAIL_ALIAS = {label: key for key, labels in DETAIL_LABELS.items() for label in labels}
CATEGORIES = frozenset(LABELS)
_CATEGORY = {label: key for key, labels in LABELS.items() for label in labels}
for label, key in _DETAIL_ALIAS.items():
    _CATEGORY.setdefault(label, "observations" if key in (
        "observation_name", "value", "unit", "measured_on", "condition", "observed_on") else
        "adverse_events" if key == "symptom" else "course" if key in ("onset", "response", "outcome") else
        "followup" if key in ("followup", "assignee", "due_text", "last_confirmed_on", "confirmed_by", "certainty") else
        "care_level" if key == "valid_period" else "skin_wound" if key == "body_site" else
        "nutrition" if key == "food_texture" else "medication_management")
_HEADING = re.compile(
    r"^[ \t　]*(?:[■●◆#・＊*]+[ \t　]*)?(?:[【［\[（(][ \t　]*)?"
    r"(?P<label>" + "|".join(map(re.escape, sorted(_CATEGORY, key=len, reverse=True))) + r")"
    r"(?:[ \t　]*[】］\]）)][ \t　]*[:：]?[ \t　]*|[ \t　]*[:：][ \t　]*|[ \t　]*$)",
    re.IGNORECASE)
_BOUNDARY = re.compile(
    r"^[ \t　]*(?:[■●◆#]+|[【［\[][^】］\]\n]+[】］\]]|[^\s:：]{1,24}[:：])")
_MEASUREMENT = re.compile(r"^[ \t　]*(\d+(?:[.．]\d+)?)(?![\d.．/／年月日-])[ \t　]*(kg|cm|mg/dL|g/dL|mL/min/1\.73m2|U/L|mmol/L|%|％)?", re.I)
_REPORTED_DATE = re.compile(r"(?<!\d)\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?(?!\d)")
# Drug storage/management/inquiry attributes only describe medication_management items.
_DRUG_ONLY_KEYS = frozenset({"storage_status", "storage_place", "medication_manager", "inquiry_result",
                             "interaction_check"})
_PATIENT_OWNERS = ("ご本人", "本人", "患者", "利用者")
_DRUG_OWNER = re.compile(
    rf"(?P<who>ご本人|本人|患者|利用者|{clinical_scope.FAMILY_PERSON})"
    r"(?:さん|様)?の(?:お)?(?:内服薬|処方薬|残薬|薬)")


def reference_item(value):
    """Read a resolved reference identity without fetching or substituting its value."""
    fields = {"token", "key", "value", "drug_names", "project_id", "message_id", "content_hash",
              "fingerprint", "parent_fingerprint", "posted_at_ts"}
    if (not isinstance(value, dict) or set(value) != fields
            or not isinstance(value.get("token"), str) or not re.fullmatch(r"S[1-9][0-9]*", value["token"])
            or not isinstance(value.get("key"), str) or value["key"] not in DETAIL_KEYS
            or not isinstance(value.get("value"), str) or not value["value"].strip()
            or not isinstance(value.get("drug_names"), list) or len(value["drug_names"]) > 1
            or any(not isinstance(name, str) or not name.strip() for name in value["drug_names"])
            or any(type(value.get(key)) is not int or value[key] <= 0 for key in ("project_id", "message_id"))
            or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
                   for key in ("content_hash", "fingerprint", "parent_fingerprint"))
            or type(value.get("posted_at_ts")) not in (int, float)):
        return None
    try:
        if not math.isfinite(value["posted_at_ts"]):
            return None
    except OverflowError:
        return None
    return {**value, "drug_names": list(value["drug_names"])}


def _other_persons_drug(body: str, span, value: str) -> bool:
    """Whether every quoted occurrence of value belongs to a non-patient's drug in its sentence."""
    owned = []
    at = body.find(value, span[0], span[1])
    while at >= 0:
        start = max(body.rfind(mark, 0, at) for mark in "。！？!?\n") + 1
        end = min((i for i in (body.find(mark, at) for mark in "。！？!?\n") if i >= 0), default=len(body))
        owners = list(_DRUG_OWNER.finditer(body, start, end))
        # the nearest preceding owner, else the sentence's next one; the owner phrase itself is no value
        if not any(o.start() <= at < o.end() for o in owners):
            before = [o for o in owners if o.end() <= at]
            owner = before[-1] if before else next(iter(owners), None)
            owned.append(owner is not None and owner["who"] not in _PATIENT_OWNERS)
        at = body.find(value, at + 1, span[1])
    return bool(owned) and all(owned)


def detail_items(items, body: str, parent_span, category=None, subject=None) -> list[dict]:
    """Keep each raw attribute only inside its parent's unique source quote, category and drug owner."""
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
        if category is not None and item["key"] in _DRUG_ONLY_KEYS and category != "medication_management":
            continue
        if subject in ("patient", "unspecified") and _other_persons_drug(body, span, item["value"]):
            continue
        clean = {"key": item["key"], "value": item["value"], "evidence": body[span[0]:span[1]]}
        reference = reference_item(item.get("refers_to"))
        if reference is not None and reference["key"] == clean["key"]:
            clean["refers_to"] = reference
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
            clean["details"] = detail_items(item["details"], body, span, item["category"], item["subject"])
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
