"""Extract and read source-bound patient context without inferring current clinical state."""
import clinical_scope
import clinical_values
import hashlib
import json
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
# casefold -> value with the FIRST label in table order winning, as the former next() scans did
_CATEGORY_FOLD: dict = {}
for label, key in _CATEGORY.items():
    _CATEGORY_FOLD.setdefault(label.casefold(), key)
_DETAIL_FOLD: dict = {}
for label, key in _DETAIL_ALIAS.items():
    _DETAIL_FOLD.setdefault(label.casefold(), key)
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
_DRUG_NOUN = r"(?:お)?(?:内服薬|処方薬|残薬|薬)"
_DRUG_OWNER = re.compile(
    rf"(?P<who>ご本人|本人|患者|利用者|{clinical_scope.FAMILY_PERSON})"
    rf"(?:さん|様)?(?:が|は)?(?:自分|自身)?の{_DRUG_NOUN}")


PHARMACY_STATE_KEYS = frozenset({"inquiry_result", "storage_status", "storage_place",
    "medication_manager", "interaction_check", "support_method", "residual_quantity",
    "missed_doses", "followup"})


def valid_reference_hash(value):
    """Validate a source or reference identity as a lowercase SHA-256 digest."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def pharmacy_state_items(artifact, message) -> list[dict]:
    """Source-grounded pharmacy reports from one decoded current extraction, preserving each parent."""
    message = dict(message)
    external = message.get("source") == "attachment_ocr"
    if (message.get("body_state") not in (None, "full")
            or type(message.get("project_id")) is not int or message["project_id"] <= 0
            or type(message.get("message_id")) is not int
            or (message["message_id"] != 0 if external else message["message_id"] <= 0)
            or not valid_reference_hash(message.get("content_hash"))):
        return []
    source_fields = {}
    if external:
        import original_source_keys
        key = message.get("source_key")
        if not original_source_keys.valid(key):
            return []
        kind, raw_artifact, page = original_source_keys.parse(key)
        if (kind != "attachment_ocr" or message.get("ready") is not True
                or not valid_reference_hash(message.get("_source_binding"))
                or not valid_reference_hash(message.get("result_hash"))
                or any(type(message.get(field)) is not int or message[field] <= 0
                       for field in ("parent_message_id", "attachment_id", "extraction_artifact_id"))
                or type(message.get("source_artifact_id")) is not int or message["source_artifact_id"] != raw_artifact
                or type(message.get("page")) is not int or message["page"] != page
                or any(message.get(field) is not None for field in ("posted_at", "posted_at_ts", "sender_id", "sender_name"))
                or not isinstance(message.get("body_text"), str)):
            return []
        try:
            if hashlib.sha256(message["body_text"].encode("utf-8")).hexdigest() != message["content_hash"]:
                return []
        except UnicodeError:
            return []
        source_fields = {field: message[field] for field in ("source_key", "parent_message_id", "attachment_id", "page",
                         "source_artifact_id", "extraction_artifact_id", "result_hash")}
        source_fields["source_binding"] = message["_source_binding"]
    result = []
    for item in context_items(artifact, message.get("body_text")):
        parent = {key: item[key] for key in ("category", "text", "evidence", "subject")}
        parent["details"] = item.get("details", [])
        identity = {key: parent[key] for key in ("category", "text", "evidence", "subject")}
        if external:
            identity["source"] = [message["project_id"], message["source_key"], message["content_hash"], message["_source_binding"]]
        parent_fp = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        names = sorted({detail["value"] for detail in parent["details"] if detail["key"] == "drug_name"})
        for detail in parent["details"]:
            if detail["key"] not in PHARMACY_STATE_KEYS:
                continue
            own_detail = {key: detail[key] for key in ("key", "value", "evidence")}
            fingerprint = hashlib.sha256(json.dumps([parent_fp, own_detail, names],
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            stamp = message.get("posted_at_ts")
            result.append({**detail, "parent": parent, "parent_fingerprint": parent_fp,
                "fingerprint": fingerprint, "drug_names": names, "subject": parent["subject"],
                "project_id": message["project_id"], "message_id": message["message_id"],
                "content_hash": message["content_hash"], "posted_at": message.get("posted_at"),
                "posted_at_ts": stamp if type(stamp) in (int, float) and math.isfinite(stamp) else None,
                "source": "attachment_ocr" if external else "chat", **source_fields})
    return result


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
            or any(not valid_reference_hash(value.get(key))
                   for key in ("content_hash", "fingerprint", "parent_fingerprint"))
            or type(value.get("posted_at_ts")) not in (int, float)):
        return None
    try:
        if not math.isfinite(value["posted_at_ts"]):
            return None
    except OverflowError:
        return None
    return {**value, "drug_names": list(value["drug_names"])}


def _quoted_value_positions(body: str, span: tuple[int, int], value: str):
    """Yield the sentence start, value position and sentence end for each quoted match."""
    at = body.find(value, span[0], span[1])
    while at >= 0:
        start = max(body.rfind(mark, 0, at) for mark in "。！？!?\n") + 1
        end = min((i for i in (body.find(mark, at) for mark in "。！？!?\n") if i >= 0), default=len(body))
        yield start, at, end
        at = body.find(value, at + 1, span[1])


def _drug_owner_at(body: str, start: int, at: int, end: int):
    """The nearest preceding drug owner, otherwise the first owner following the value."""
    owner = None
    for candidate in _DRUG_OWNER.finditer(body, start, end):
        if candidate.start() > at and owner is not None:
            break
        owner = candidate
        if owner.end() > at:
            break
    return owner


def _other_persons_drug(body: str, span, value: str) -> bool:
    """Whether every quoted occurrence of value belongs to a non-patient's drug in its sentence."""
    owned = []
    for start, at, end in _quoted_value_positions(body, span, value):
        owner = _drug_owner_at(body, start, at, end)
        if owner is None or not owner.start() <= at < owner.end():
            owned.append(owner is not None and owner["who"] not in _PATIENT_OWNERS)
    return bool(owned) and all(owned)

_DETAIL_NEGATIVE = re.compile(r"して(?:いない|いません|おりません|おらず)|しない|しません|なかった|ありません|ではない|なし")
_DETAIL_UNCERTAIN = re.compile(r"未確認|未確定|不明|疑い|可能性|かもしれ|予定|検討")
_DETAIL_CLAUSE_BREAK = re.compile(r"[、，,]")
_STORAGE_OBSERVATION = re.compile(
    rf"(?:(?P<past>(?:{clinical_scope.HISTORICAL_MARK}|{clinical_scope.PAST_MARK}|前は)(?!から|より))"
    rf"|(?P<planned>明日|翌日|来週|来月|予定|計画)|今(?=[はも])|{clinical_scope.NOW_MARK}|今の)"
    r"(?P<occasion>(?:の[一-龥々]*)?(?:時点|時|際))?"
    r"(?:(?P<topic>(?:に|で)?[はも])|に)?")
_STORAGE_ONSET = r"(?:以前|昨[日年]|先[日週月])(?:から|より)"
_STORAGE_ARGUMENT = re.compile(
    rf"(?:{_STORAGE_ONSET})?(?:(?:{'|'.join(_PATIENT_OWNERS)})[がは])?(?:(?:{_DRUG_OWNER.pattern}|{_DRUG_NOUN})[はをがも])?")
_ATTRIBUTIVE_ARGUMENT = re.compile(r"[^、，,。！？!?]*た[一-龥々ァ-ヶーA-Za-z0-9]+[はをがも]")
_STORAGE_LABEL = re.compile(r"(?:保管|保存)(?:場所|先|状況)は\s*$")
_PLACE_NOUN = r"[一-龥々ァ-ヶーA-Za-z0-9]{0,20}(?:庫|棚|箱|室|台所|居間|引き出し|ボックス|カレンダー)"
_HEADING_PLACE = re.compile(rf"{_PLACE_NOUN}(?:の{_PLACE_NOUN})*")
_ADVERSE_SYMPTOM = (r"眠気|ふらつき|めまい|発疹|薬疹|蕁麻疹|湿疹|かゆみ|嘔気|吐き気|嘔吐|下痢|便秘|頭痛|胃痛"
                    r"|腹痛|口渇|倦怠感|だるさ|むくみ|浮腫|動悸|低血圧|低血糖|食欲低下|食欲不振|肝障害|腎障害"
                    r"|せん妄|不眠|振戦|発熱")
_NEXT_REPORT = re.compile(r"\s*(?:(?:が|けれど(?:も)?|ものの)\s*[、，,;；]?|[、，,;；])\s*")
_STORAGE_ACTION = re.compile(
    r"(?:(?:内|の中)?[でに](?:の)?)?(?:冷蔵|冷凍|常温)?(?:保管|保存)"
    rf"(?:(?P<former>して(?:いました|おりました|いた))(?=$|{_NEXT_REPORT.pattern})"
    rf"|(?P<stopped>[はをも]?(?:中止|終了)(?:しました|した(?!い|ら)|済み|して?(?=$|[一-龥々]|{_NEXT_REPORT.pattern})))"
    rf"|(?P<negative>{_DETAIL_NEGATIVE.pattern})|(?P<active>して(?:います|おります|いる|いて|おり)|です)?)")
_STORAGE_RESTART = re.compile(
    r"(?:が|後に?)?[、，, ]*(?:その後|(?P<now>現在は|今は))?[、，, ]*(?:(?:保管|保存)を)?再開(?:しました|しています|した|済み)(?:です)?")
_OTHER_PHARMACY_REPORT = re.compile(
    r"(?:残薬量?(?:について)?[はがも]\d+(?:[.．]\d+)?\s*(?:錠|包|枚|本|カプセル)"
    rf"|(?:残薬量?|副作用(?:症状)?|飲み忘れ|{_ADVERSE_SYMPTOM})(?:の訴え)?(?:なし|(?:について)?[はがも]"
    rf"(?:特に)?(?:確認)?(?:{_DETAIL_NEGATIVE.pattern}|{_DETAIL_UNCERTAIN.pattern}|あり(?:ます|ました))))"
    r"(?:でした|です)?(?:とのこと(?:でした|です)?)?")
_STORAGE_REPORT = re.compile(r"(?:とのこと(?:です)?|ことを確認(?:しました|しています)|と(?:聞き|伺い)ました)")


def _complete_storage_report(source: str, end: int, *, conjunctive=False) -> bool:
    """A grounded assertion can be attributed or confirmed, then followed by independent reports."""
    attribution = _STORAGE_REPORT.match(source, end)
    if attribution:
        end = attribution.end()
    while end < len(source):
        boundary = _NEXT_REPORT.match(source, end)
        if not boundary and not conjunctive:
            return False
        report = _OTHER_PHARMACY_REPORT.match(source, boundary.end() if boundary else end)
        if not report:
            return False
        end = report.end()
        conjunctive = False
    return True


def _storage_observation_period(before: str) -> str:
    """Date an affirmative storage assertion at its observation, allowing a reporting attribution."""
    chunks = _DETAIL_CLAUSE_BREAK.split(before)
    local = chunks[-1].strip()
    frame = None
    ambiguous = False
    for mark in _STORAGE_OBSERVATION.finditer(local):
        # An explicit visit-time topic applies even when the object is an unrecognized medicine.
        explicit_topic = mark['topic'] and (mark['occasion'] or not mark['past'])
        if explicit_topic or _STORAGE_ARGUMENT.fullmatch(local, mark.end()):
            frame, ambiguous = mark, False
        elif (mark['past'] or mark['planned']) and not _ATTRIBUTIVE_ARGUMENT.fullmatch(local, mark.end()):
            frame, ambiguous = None, True
    if frame is None and not ambiguous:
        for chunk in reversed(chunks[:-1]):
            preceding = chunk.strip()
            if not preceding.endswith(('によると', 'によれば')):
                frame = _STORAGE_OBSERVATION.fullmatch(preceding)
                if not frame and any(mark['past'] or mark['planned'] for mark in _STORAGE_OBSERVATION.finditer(preceding)):
                    other = _OTHER_PHARMACY_REPORT.search(preceding)
                    if not other or (other.end() != len(preceding) and not _NEXT_REPORT.fullmatch(preceding, other.end())):
                        ambiguous = True
                break
    if ambiguous:
        return 'unknown'
    if frame and frame['planned']:
        return 'planned'
    return 'historical' if frame and frame['past'] else 'current'


def _storage_assertion(before: str, value: str, tail: str, heading_key=None) -> str:
    """Classify one source occurrence, requiring a complete affirmative assertion for a shared card."""
    if _DETAIL_NEGATIVE.search(value) or _DETAIL_UNCERTAIN.search(value) or _DETAIL_CLAUSE_BREAK.search(value):
        return 'unknown'
    source = value + tail
    label = _STORAGE_LABEL.search(before)
    if heading_key == 'storage_place' and (not _HEADING_PLACE.fullmatch(value.removesuffix('です'))
                                           or _STORAGE_OBSERVATION.search(value)):
        return 'unknown'
    if label or heading_key == 'storage_place':
        end = len(value) + (2 if tail.startswith('です') else 0)
        if _complete_storage_report(source, end):
            return _storage_observation_period(before[:label.start()] if label else before)
        return 'unknown'
    action = next((action for action in _STORAGE_ACTION.finditer(source)
                   if action.start() <= len(value) <= action.end()), None)
    if action is None or (heading_key == 'storage_status' and action.start() != 0):
        return 'unrelated'
    period = _storage_observation_period(before)
    if action['former'] is not None or action['stopped'] is not None:
        restart = _STORAGE_RESTART.match(source, action.end())
        if restart and _complete_storage_report(source, restart.end()):
            return 'current' if restart['now'] else period
        if action['former']:
            return 'historical' if _complete_storage_report(source, action.end()) else 'unknown'
    if action['negative'] is not None or action['stopped'] is not None:
        return 'inactive' if period == 'current' else period
    if _complete_storage_report(source, action.end(), conjunctive=action['active'] in ('していて', 'しており')):
        return period
    return 'planned' if period == 'planned' else 'unknown'


def _storage_drug_argument(before: str) -> str | None:
    """A specific written medicine can distinguish a withdrawal; a generic or ambiguous argument cannot."""
    clause = _DETAIL_CLAUSE_BREAK.split(_STORAGE_LABEL.sub('', before).rstrip('、，, '))[-1].strip()
    frame = _STORAGE_OBSERVATION.match(clause)
    if frame:
        clause = clause[frame.end():]
    for match in clinical_values.written_medication_mentions(clause):
        name = match[0]
        if (match.end() == len(clause) - 1 and clause[:match.start()] in ('', *(who + 'の' for who in _PATIENT_OWNERS))
                and re.fullmatch(_THING, name) and not clinical_values.GENERIC_MEDICATION_FORM.fullmatch(name)):
            return clinical_values.medication_surface(name)['base']
    return None


def _storage_heading_key(body: str, at: int):
    """Use the same source-line heading and aliases as the rule extractor, including next-line values."""
    end = at
    while end and body[end - 1].isspace():
        end -= 1
    start = body.rfind('\n', 0, end) + 1
    newline = body.find('\n', end)
    header = _HEADING.match(body[start:newline if newline >= 0 else len(body)].rstrip('\r'))
    if header and start + header.end() <= at and not body[start + header.end():at].strip():
        return _DETAIL_FOLD.get(header['label'].casefold())
    return None


def _current_storage_in_quote(body: str, span, value: str, subject, key) -> bool:
    """Only quoted evidence establishes storage; a later assertion about the same subject can withdraw it."""
    if not isinstance(value, str) or not value or value not in body[span[0]:span[1]]:
        return False
    value = value.removesuffix('。')
    if not value:
        return False
    active = set()
    for start, at, end in _quoted_value_positions(body, (span[0], len(body)), value):
        before = body[start:at]
        heading_key = _storage_heading_key(body, at)
        state = _storage_assertion(before, value, body[at + len(value):end],
                                   heading_key if heading_key == key else None)
        if state in ('unrelated', 'historical', 'planned'):
            continue
        if body[end:end + 1] in ('?', '？'):
            state = 'unknown'
        owner = _drug_owner_at(body, start, at, end)
        who = owner['who'] if owner else subject
        if who in (*_PATIENT_OWNERS, None, 'unspecified'):
            who = 'patient'
        if subject in ('patient', 'unspecified') and who != 'patient':
            continue
        drug = _storage_drug_argument(before)
        if state == 'current' and at + len(value) <= span[1]:
            active.add((who, drug))
        elif state != 'current':
            active = {(holder, name) for holder, name in active
                      if holder != who or (drug is not None and name is not None and drug != name)}
    return bool(active)


def noncurrent_detail(body: str, evidence: str, value: str, *, key=None, subject=None) -> bool:
    """Whether a quoted detail should be withheld from a shared card."""
    if not isinstance(body, str) or not isinstance(evidence, str):
        return True
    span = locate_quote_span(body, evidence)
    if span is None:
        return True
    if key in ('storage_place', 'storage_status') or (key is None and ('保管' in body or '保存' in body)):
        return not _current_storage_in_quote(body, span, value, subject, key)
    verdicts = []
    for start, at, end in _quoted_value_positions(body, span, value):
        before, tail = body[start:at], body[at + len(value):end]
        clause = _DETAIL_CLAUSE_BREAK.split(tail, maxsplit=1)[0]
        past_only = (re.search(r"以前|過去|前は", before) is not None
                     and not any(mark in before for mark in ("現在", "今は", "今の")))
        verdicts.append(bool(_DETAIL_NEGATIVE.search(clause) or _DETAIL_UNCERTAIN.search(clause) or past_only))
    return bool(verdicts) and all(verdicts)


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
            key = _DETAIL_FOLD.get(alias.casefold())
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
                value = _CATEGORY_FOLD.get(label.casefold())
                if value is None:   # IGNORECASE matched a label casefold does not: fail as before
                    value = _CATEGORY[next(key for key in _CATEGORY if key.casefold() == label.casefold())]
                active = (offset, offset + match.end(), value)
        offset += len(line)
    finish(len(body))
    return result


# A heading can name a subject only through a closed, source-written role.
# Bare kin words are relative to the writer: 母, 妻 and 長女 may all name the
# patient. Their spelling alone never establishes a different person.
_SUBJECT_HEADINGS = {"家族の意向": "family", "家族の希望": "family",
                     "本人の意向": "patient", "本人の希望": "patient"}
_SUBJECT_CATEGORIES = frozenset({"preferences", "allergies", "adverse_events"})
# A staff member holds only a policy; for a clinical fact or a wish they are a reporter.
_STAFF_HELD_HEADINGS = frozenset({"方針", "療養方針", "治療方針"})
_PATIENT_REF = (r"本人|ほんにん|当人|当事者|患者|利用者|入居者|対象|被保険者|要介護者|被介護者"
                r"|クライアント|お客様|自身|自分|じぶん|私|わたし|わたくし|当方|僕|俺|彼"
                r"|(?:あの|この|その)方|(?<![A-Za-z])(?:pt|Pt|PT)(?![A-Za-z])")
_LEAD = r"[ \t　]*(?:[■●◆#・＊*]+[ \t　]*)?"
# Only a written relationship to the patient binds a kin holder. Other holders
# are an explicitly different patient or a staff role, never 本人以外 as a group.
_VALUE_HOLDER = re.compile(
    rf"{_LEAD}(?:(?:本人|患者|利用者)(?:さん|様)?の(?P<family>{clinical_scope.FAMILY_PERSON})"
    rf"(?:ご本人|本人)?(?:自身)?|(?P<other>(?:他の|別の)(?:患者|利用者)|他患者|別患者|{clinical_scope.STAFF_PERSON}))は")
# A conflicting holder under an explicitly patient-owned heading withdraws
# only that heading's patient assertion. A bare kin may do so without being
# affirmative evidence for a family holder. が and も are not this holder form.
_PATIENT_HEADING_CONFLICT = re.compile(
    rf"{_LEAD}(?:{clinical_scope.FAMILY_PERSON})(?:ご本人|本人)?(?:自身)?は")
# A fact names one thing, never a person, a group that may include the patient or a
# pointer back to the patient's record (皆・全員・入居者・本人同様・同じ・それ); the
# closed shapes below guarantee everything else. A family or other word already covers
# 他人・主人・夫婦. 同 before a noun is "the same as the patient's" (同薬・同施設・同内容)
# except where it means "together" (同居・同席・同行・同伴・同室・同時・同日), which
# points at nothing; 同意 is a stance, not a thing.
_NOT_A_THING = (rf"{clinical_scope.FAMILY_PERSON}|{clinical_scope.OTHER_PERSON}|{_PATIENT_REF}"
                r"|者|さん|様|ちゃん|君|氏"
                r"|皆|みな|みんな|全|両|双方|共|一緒|揃|そろ|二人|ふたり|親子"
                r"|同(?![居席行伴室時日])|おなじ|それ|これ|あれ|その|この|あの")
# A thing is self-contained unless it opens with a determiner kanji (当・本・該・前・上・
# 下・左・右・既・先・件・例・以) that refers back: one standing alone (当アレルギー・同じ)
# or one before a head that completes a reference — a place in the record (左記・前述・
# 前項・前出・前掲), an occurrence or case (前回・本件・前例), a position (以上・以下) or a
# known history (既知・既往), or another determiner (当該・該当). 当院・本日・前日・上肢
# name a thing and stay.
_POINTER = r"[当本該前上下左右既先件例以](?:[記述項出掲欄回件例上下知往該当]|(?![一-鿿]))"
# A stance word, or a proxy marker that makes the stance someone else's (代理・代弁・代り・
# 代筆・代行: the son voices the patient's wish); the object of a stance holds neither.
# 代わり never fits the object (わ ends it).
_STANCE = r"希望|望|意向|拒否|反対|賛成|了承|同意|提案|推奨|指示|予定|代理|代弁|代り|代筆|代行"


def _thing(excluded):
    """Return the pattern of one short noun that contains none of the excluded words."""
    # At most 20 characters: kanji with at most two noun-forming kana each (看取り・
    # 話し合い・胃ろう), katakana, alphanumerics, or hiragana alone without は・が・も・と・
    # を・の. No space or symbol fits, so neither a verb, a clause nor a second item
    # does (知らなかった・元気なら・卵 蕎麦).
    kana = "[々りいしみきけえちびれじろう]"
    word = rf"[一-鿿]|(?<=[一-鿿]){kana}|(?<=[一-鿿]{kana}){kana}"
    name = r"[ァ-ヴ]|(?<=[ァ-ヴー])ー|[A-Za-z0-9Ａ-Ｚａ-ｚ０-９]"
    char = f"{word}|{name}"
    # A drug, prescription or product names one only with a name part (合成薬A・ペニシリン系
    # 薬); written in kanji alone it is a regimen or a class that may be the patient's
    # (処方薬・持参薬・本処方・当内服薬・同時薬・睡眠薬・下剤), never a thing.
    # Possessive runs keep the check linear in the length of the value.
    whole = rf"(?!(?!アレルギー)(?:{char}))"
    nameless_drug = (rf"(?:{word})++(?<=[薬剤品]){whole}"
                     rf"|(?=(?:(?!処方|内服|服用)(?:{word}))*+(?:処方|内服|服用))(?:{word})++{whole}")
    return rf"(?:(?!{_POINTER}|{nameless_drug})(?:(?!{excluded})(?:{char})){{1,20}}|(?:(?!{excluded})[ぁ-ゖ](?<![はがもとをの])){{1,20}})"


_THING = _thing(_NOT_A_THING)
# What a stance or a proposal is about may join nouns with の・で・に・へ (自宅での看取り),
# but holds no second stance (了承在宅を希望) and no proxy (代理で在宅を希望).
_OBJECT = _thing(f"{_NOT_A_THING}|{_STANCE}")
_OBJECT = rf"{_OBJECT}(?:(?:での|への|の|で|に|へ){_OBJECT}){{0,2}}"
_ALLERGY_SYMPTOM = (r"発疹|皮疹|薬疹|蕁麻疹|じんましん|湿疹|かぶれ|発赤|かゆみ|痒み|口がかゆくなる|かゆくなる"
                    r"|腫れ|むくみ|喘息|呼吸困難|アナフィラキシー|嘔吐|下痢")
_DOING = r"(?:する|しない|している|していない|しています|されている|されています|です)?"
_WISH = (rf"(?:希望|拒否|反対|賛成|了承|同意){_DOING}"
         r"|望む|望まない|望んでいる|望んでいない|望んでいます|望みます")
# The whole value after the holder, per heading category; a stance needs its
# own self-contained object. A pointer or an objectless 同意 cannot supply it.
_FACT = {
    "allergies": re.compile(
        rf"{_THING}アレルギー(?:あり|有り|がある|です)?|{_THING}で(?:{_ALLERGY_SYMPTOM})(?:あり|有り|が出た)?"
        r"|花粉症|(?:アレルギー)?(?:なし|無し|特になし)"),
    "adverse_events": re.compile(
        rf"{_THING}で(?:{_ADVERSE_SYMPTOM})(?:あり|有り|が出た)?|(?:副作用)?(?:なし|無し|特になし)"),
    "preferences": re.compile(rf"{_OBJECT}を?(?:{_WISH})"),
}
_PROPOSAL = re.compile(rf"{_OBJECT}を?(?:提案|推奨|指示|予定){_DOING}")


def heading_subject(label, value) -> str:
    """Name a written heading or closed holder subject, leaving unresolved relative holders unspecified."""
    if not isinstance(label, str) or not isinstance(value, str):
        return "unspecified"
    category = _CATEGORY_FOLD.get(label.casefold())
    if category not in _SUBJECT_CATEGORIES:
        return "unspecified"
    heading = _SUBJECT_HEADINGS.get(label)
    if heading == "patient":
        conflict = _VALUE_HOLDER.match(value) or _PATIENT_HEADING_CONFLICT.match(value)
        return "unspecified" if conflict else "patient"
    person = _VALUE_HOLDER.match(value)
    if person is None:
        return "unspecified"
    holder = "family" if person["family"] else "other"
    if heading == "family" and holder != "family":
        return "unspecified"
    shape = _FACT[category]
    if person["other"] and re.fullmatch(clinical_scope.STAFF_PERSON, person["other"]):
        shape = _PROPOSAL if label in _STAFF_HELD_HEADINGS else None
    return holder if shape and shape.fullmatch(value[person.end():].rstrip()) else "unspecified"


def annotate_heading_subjects(items) -> list:
    """Annotate only undecided heading items in a fresh list, leaving named subjects and inputs untouched."""
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if (isinstance(item, dict) and item.get("origin") == "explicit_heading"
                and item.get("subject") == "unspecified"
                and isinstance(item.get("evidence"), str) and isinstance(item.get("text"), str)):
            lines = item["evidence"].splitlines()
            header = _HEADING.match(lines[0]) if lines else None
            item = {**item, "subject": heading_subject(header["label"], item["text"]) if header else "unspecified"}
        result.append(item)
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


def project_context_items(items, scope, source):
    """Preserve grounded attribute values in detail and count only their keys in aggregate."""
    if scope == "detail":
        return [{**{key: item[key] for key in ("category", "text", "evidence", "subject")},
                 **({"origin": item["origin"]} if item.get("origin") == "explicit_heading" else {}),
                 **({"details": item["details"]} if item.get("details") else {}),
                 "source": dict(source)} for item in items]
    counts = {}
    attributes = {}
    for item in items:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
        for detail in item.get("details") or []:
            category = attributes.setdefault(item["category"], {})
            category[detail["key"]] = category.get(detail["key"], 0) + 1
    return [{"category": key, "count": value,
             **({"details": [{"key": name, "count": count} for name, count in sorted(attributes[key].items())]}
                if key in attributes else {})} for key, value in sorted(counts.items())]
