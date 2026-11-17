"""Rule 30: the subject a labelled heading names, as pure functions over extract_context items.

The heading rules emit ``unspecified``, which downstream reads as the patient's
own item. Rule 30 names another subject only on an allow-list, because a wrong
family or other label hides the patient's own allergy, adverse reaction or wish:
家族の意向／家族の希望 is the family's, and a value of an allergy, adverse-event or
preference heading is a family member's or another patient's only when the whole
value is exactly a plain kin word or another patient, は (never も, which says the
patient shares the fact, and never が, which marks an actor or a reporter; everyone but
the patient (本人以外・他者・他人・第三者) holds nothing, since it implies the patient has
something), and one fact of a closed shape for the heading: an
allergy (卵アレルギー・合成薬Aで発疹・花粉症・なし), an adverse reaction (合成薬Bで眠気・
なし) or a stance on one thing (施設入所を希望・延命を望まない・反対). No space, symbol,
bracket, second item, clause, request (〜してほしい) or permission (同席可) may
follow, the fact names no person, group or pointer back to the patient's record
(皆・全員・本人・入居者・対象・同じ・同薬; nor opens with a determiner that refers back:
当該・前述・左記・本件・当薬・以上), and an
honorific kin word that can be the patient in a note to the family (ご主人・お母様)
holds nothing. A staff member holds only a proposal under a policy (方針) heading.
本人の意向／本人の希望 is the patient's and is never relabelled; a person heading its
value only withdraws the assertion. 家族の意向／家族の希望 is the family's only when its
value names no patient word (本人・患者・利用者・対象者・Pt・自身・私・彼), even after a
holder (母本人は). Everything else stays ``unspecified``, the
former behaviour and the safe direction. Fully synthetic text only.
"""
import copy
import json
from pathlib import Path

import pytest

import extract_bench
from patient_context import annotate_heading_subjects, extract_context, heading_subject

ROOT = Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "evaluation" / "extract_cases_safety_history.json").read_bytes())["cases"]


@pytest.mark.parametrize(("label", "value", "subject"), [
    # The heading itself names the person.
    ("家族の意向", "自宅での看取りを希望されています", "family"),
    ("家族の希望", "週2回の訪問を続けたい", "family"),
    ("本人の意向", "自宅で最期を迎えたい", "patient"),
    ("本人の希望", "延命治療は望まない", "patient"),
    # The head of the value names a family member or another person.
    ("アレルギー", "長女は架空果実Mで口がかゆくなる", "family"),
    ("副作用", "ご家族は合成薬Cで眠気が出た", "family"),
    ("意向", "長男さんは胃ろうを望んでいる", "family"),
    ("希望", "他の利用者は個室を希望", "other"),
    ("方針", "医師は在宅継続を提案", "other"),
    ("アレルギー", "・長女は架空果実Nアレルギー", "family"),
    ("アレルギー", "長女自身は卵アレルギー", "family"),
    ("アレルギー", "長女さんは卵アレルギー", "family"),
    ("延命", "家族は希望しない", "family"),
    ("副作用歴", "娘は合成薬Aで発疹", "family"),
    # A person in the value never relabels a patient heading; it only withdraws the assertion.
    ("本人の意向", "長女は胃ろうを希望", "unspecified"),
    # が marks an actor or a reporter, never the holder.
    ("薬剤アレルギー", "娘さんが合成薬Aで発疹", "unspecified"),
    ("被疑薬", "息子さん本人が飲んでいた合成薬D", "unspecified"),
    ("希望", "他の利用者が同じ部屋を希望", "unspecified"),
    # の is no holder form: 奥様の薬 may be the drug the patient took.
    ("副作用歴", "奥様の薬でめまい", "unspecified"),
    ("アレルギー", "長女のアレルギーは卵", "unspecified"),
    ("副作用歴", "別の利用者の発疹は合成薬Eによる", "unspecified"),
    # Staff report a clinical fact; they hold only a policy heading.
    ("アレルギー", "ヘルパーさんは卵アレルギー", "unspecified"),
    ("希望", "医師は入院を勧めている", "unspecified"),
    # The value is not about that person alone: unchanged, and the heading decides.
    # A second-hand report or a clinician's judgement or action.
    ("アレルギー", "家族の話では卵アレルギーあり", "unspecified"),
    ("副作用歴", "医師は合成薬Aによる薬疹と判断", "unspecified"),
    ("アレルギー", "薬剤師が確認、合成薬Aで発疹歴あり", "unspecified"),
    ("副作用", "ご家族は合成薬Cで眠気が出たとのこと", "unspecified"),
    ("副作用", "長女が報告、合成薬Hで発疹", "unspecified"),
    ("副作用", "長女が報告した合成薬Hでの発疹", "unspecified"),
    ("アレルギー", "看護師が確認した卵アレルギー", "unspecified"),
    ("DNAR", "医師が説明し本人が同意", "unspecified"),
    # The patient is named later in the same value.
    ("アレルギー", "家族は特になし、本人は卵", "unspecified"),
    ("アレルギー", "妻も本人も卵アレルギー", "unspecified"),
    ("延命", "家族は希望するが本人は望まない", "unspecified"),
    ("アレルギー", "長女は利用者と同じく卵", "unspecified"),
    # More than one line, sentence or clause.
    ("アレルギー", "・長女は花粉症\n・合成薬Aで発疹", "unspecified"),
    ("アレルギー", "長女は花粉症。合成薬Aで発疹", "unspecified"),
    ("アレルギー", "長女は花粉症、合成薬Aで発疹", "unspecified"),
    # の that is not the person's clinical fact.
    ("本人の意向", "家族の負担になりたくない", "patient"),
    ("本人の希望", "医師の往診を続けてほしい", "patient"),
    ("本人の希望", "他者の意見で施設入所を希望", "patient"),
    ("アレルギー", "父の日に食べたエビで蕁麻疹", "unspecified"),
    ("副作用歴", "奥様の合成薬Bでめまい", "unspecified"),
    ("副作用歴", "別の利用者の記録で合成薬E", "unspecified"),
    # 家族の意向 whose value names the patient: the heading no longer decides (was family).
    ("家族の意向", "本人は自宅を望んでいると話す", "unspecified"),
    # も says the patient shares the fact (was family / other): the safe direction.
    ("アレルギー歴", "同居者も卵アレルギーがある", "unspecified"),
    ("副作用歴", "別の利用者も合成薬Eで発疹", "unspecified"),
    # 同じ points back to the patient's own (was other).
    ("希望", "他の利用者は同じ部屋を希望", "unspecified"),
    # No person is named: unchanged.
    ("アレルギー", "合成薬Aで発疹", "unspecified"),
    ("アレルギー", "なし", "unspecified"),
    ("副作用歴", "特になし", "unspecified"),
    ("意向", "自宅で過ごしたい", "unspecified"),
    ("ACP", "10月に実施済み", "unspecified"),
    ("療養方針", "在宅での緩和ケア", "unspecified"),
    # A person word without its particle, or as part of another word, is not a subject.
    ("アレルギー", "家族歴なし", "unspecified"),
    ("アレルギー", "長女", "unspecified"),
    ("アレルギー", "長女は", "unspecified"),
    ("アレルギー", "夫婦とも特になし", "unspecified"),
    ("アレルギー", "合成薬Aで発疹。長女は架空果実M", "unspecified"),
    # Self and named persons are not resolved here: no guess either way.
    ("アレルギー", "ご本人は合成薬Fで発疹", "unspecified"),
    ("アレルギー", "架空さんは卵アレルギー", "unspecified"),
    # Other categories keep the former behaviour.
    ("家族構成", "長女は同居", "unspecified"),
    ("主介護者", "長女が担当", "unspecified"),
    ("既往歴", "母が糖尿病", "unspecified"),
    ("キーパーソン", "長男の架空 一郎さん", "unspecified"),
])
def test_heading_subject_follows_the_rule_table(label, value, subject):
    assert heading_subject(label, value) == subject


def _line_subjects(line):
    return [item["subject"] for item in annotate_heading_subjects(extract_context(line))]


# The patient's own fact under a person word at the head of the value: an observer,
# a reporter, an actor or a condition. Relabelling any of these hides the patient's fact.
@pytest.mark.parametrize(("line", "subject"), [
    ("アレルギー：看護師が合成薬Aで発疹を確認", "unspecified"),
    ("アレルギー：医師は合成薬Aの副作用歴を指摘", "unspecified"),
    ("アレルギー：看護師は薬疹の既往ありと記載", "unspecified"),
    ("アレルギー：医師が処方した合成薬Aで発疹", "unspecified"),
    ("アレルギー：ケアマネさんが訪問時に発疹に気づいた", "unspecified"),
    ("副作用：妻が気づいた時にはふらつきが出ていた", "unspecified"),
    ("副作用：家族が薬を飲ませたところ眠気が強く出た", "unspecified"),
    ("アレルギー：妻が薬局で聞いた話では合成薬Aで発疹", "unspecified"),
    ("アレルギー：娘が言うには卵アレルギー", "unspecified"),
    ("アレルギー：娘さんが言うには卵で蕁麻疹", "unspecified"),
    ("アレルギー：娘が毎回食後に発疹が出ると訴えている", "unspecified"),
    ("本人の希望：家族が望むなら入院してもよい", "patient"),
    ("本人の意向：夫が元気なうちは在宅を続けたい", "patient"),
    ("希望：主人は入院を希望しているが私は在宅がよい", "unspecified"),
    # More of the same, by form.
    ("アレルギー：長女が合成薬Aで発疹を見つけた", "unspecified"),
    ("アレルギー：妻は本人が卵で蕁麻疹が出たと話す", "unspecified"),
    ("副作用：長男は飲み始めてからふらつくと心配している", "unspecified"),
    ("アレルギー：薬剤師は合成薬Aでの発疹歴を確認", "unspecified"),
    ("副作用：ヘルパーは服用後のふらつきを報告", "unspecified"),
    ("アレルギー：スタッフは卵アレルギーと聞いている", "unspecified"),
    ("副作用：娘は薬を飲ませた後に眠気が出たと言う", "unspecified"),
    ("アレルギー：夫は知らなかったが合成薬Aで発疹歴あり", "unspecified"),
    ("アレルギー：長女は卵、本人はなし", "unspecified"),
    ("希望：家族は本人が望むなら在宅を続けたい", "unspecified"),
    ("本人の希望：長女は在宅を希望", "unspecified"),
    ("本人の意向：他の利用者は関係なく個室を希望", "unspecified"),
    ("副作用：妻は私が合成薬Bを飲むと眠くなると言う", "unspecified"),
    ("アレルギー：息子は母と同じく卵アレルギー", "unspecified"),
    ("アレルギー：妻によれば卵アレルギー", "unspecified"),
    ("アレルギー：長女さんの話では卵で発疹", "unspecified"),
    ("副作用：家族は眠気が出ているので減量を希望", "unspecified"),
    ("副作用：長女は合成薬Aを飲ませると眠気が強いと訴える", "unspecified"),
    ("アレルギー：長女は卵アレルギーだが本人は問題なし", "unspecified"),
    ("希望：夫は自分が元気なうちは在宅を続けたい", "unspecified"),
    ("副作用：長女は服薬させたら眠気が出たと連絡", "unspecified"),
    ("アレルギー：同居者は合成薬Aを処方されたら発疹が出た", "unspecified"),
    ("アレルギー：長女は合成薬Aで発疹が出たのを見た", "unspecified"),
    ("副作用：夫は薬を管理しており眠気あり", "unspecified"),
    ("アレルギー：家族は卵アレルギーと把握している", "unspecified"),
    ("副作用：娘は服薬介助後に眠気を目撃", "unspecified"),
    ("副作用：家族は眠気が強いと感じている", "unspecified"),
    ("アレルギー：長女は合成薬Aで発疹があった様子", "unspecified"),
    ("アレルギー：母も同様", "unspecified"),
    ("アレルギー：妻は付き添い時に発疹を目にした", "unspecified"),
    ("副作用：長女は眠気が出たため中止", "unspecified"),
    ("アレルギー：妻は気にしている", "unspecified"),
    ("アレルギー：長女は薬疹に注意している", "unspecified"),
    ("本人の希望：家族は反対だが本人は在宅", "unspecified"),
    ("本人の希望：医師の往診を続けてほしい", "patient"),
    # Kept from the former rule.
    ("アレルギー：家族によると本人は卵アレルギー", "unspecified"),
    ("アレルギー：本人は卵、家族は花粉症", "unspecified"),
    ("アレルギー：母は花粉症（本人は特になし）", "unspecified"),
    ("アレルギー：妻は特になし。本人は合成薬Aで発疹", "unspecified"),
    ("アレルギー：主治医によると合成薬Aで発疹", "unspecified"),
    ("副作用：スタッフ間で共有済み", "unspecified"),
])
def test_a_person_who_observes_reports_or_acts_never_relabels_the_patients_fact(line, subject):
    assert _line_subjects(line) == [subject]


# A plain fact about one family member or another patient keeps its holder.
@pytest.mark.parametrize(("line", "subject"), [
    ("延命：家族は希望しない", "family"),
    ("副作用歴：娘は合成薬Aで発疹", "family"),
    ("アレルギー：長女さんは卵アレルギー", "family"),
    ("方針：医師は在宅継続を提案", "other"),
    ("アレルギー：母は花粉症", "family"),
    ("アレルギー：長男は蕎麦アレルギー", "family"),
    ("副作用：夫は合成薬Bで眠気", "family"),
    ("薬剤アレルギー：次女さんは合成薬Dで蕁麻疹", "family"),
    ("希望：長女は施設入所を希望している", "family"),
    ("延命：ご家族は延命治療を望まない", "family"),
    ("家族の意向：自宅で看取りたい", "family"),
    ("副作用：孫さんは合成薬Eでかゆみ", "family"),
    ("アレルギー：父自身は甲殻類で蕁麻疹", "family"),
    ("希望：他の利用者は個室を希望", "other"),
    ("治療方針：医師は検査結果を確認して決める", "unspecified"),
    ("治療方針：医師は外来での経過観察を提案", "other"),
    ("療養方針：看護師は週3回の訪問を提案", "other"),
    ("副作用：夫は合成薬Bで眠気あり", "family"),
    ("希望：長男は在宅介護を希望", "family"),
    ("・アレルギー：■長女は架空果実Nアレルギー", "family"),
    # More of the allowed shapes, by heading category.
    ("アレルギー：夫は卵アレルギー", "family"),
    ("アレルギー：娘は蕎麦アレルギーあり", "family"),
    ("アレルギー：義母は花粉症", "family"),
    ("アレルギー：息子さんはアレルギーなし", "family"),
    ("アレルギー：兄は特になし", "family"),
    ("アレルギー：家族は花粉症", "family"),
    ("アレルギー：妻はそばで蕁麻疹", "family"),
    ("アレルギー：長女はピーナッツアレルギーです", "family"),
    ("アレルギー：同居者は合成薬Ａで発疹あり", "family"),
    ("副作用：妻は合成薬Cで発疹", "family"),
    ("副作用：長男は合成薬Aで頭痛", "family"),
    ("副作用歴：次男は合成薬Eでふらつき", "family"),
    ("副作用：父は睡眠薬で眠気あり", "family"),
    ("副作用：娘は副作用なし", "family"),
    ("被疑薬：孫は合成薬Fで嘔気", "family"),
    ("希望：妻は施設入所を希望", "family"),
    ("希望：息子は延命を希望しない", "family"),
    ("希望：娘は同居を希望しています", "family"),
    ("希望：夫は反対", "family"),
    ("意向：妻は看取りを望む", "family"),
    ("意向：長女は自宅での看取りを希望", "family"),
    ("意向：家族は同意", "family"),
    ("延命：夫は希望しない", "family"),
    ("延命：妻は胃ろうを拒否", "family"),
    ("DNAR：長男は了承", "family"),
    ("希望：家族は話し合いを希望", "family"),
    ("方針：家族は在宅継続を希望", "family"),
    ("意向：別の利用者は個室を希望", "other"),
    ("アレルギー：他の利用者は卵アレルギー", "other"),
    ("副作用：他の利用者は合成薬Bで眠気", "other"),
    ("方針：看護師は週3回訪問を提案", "other"),
    ("療養方針：ケアマネは通所の継続を推奨", "other"),
    ("本人の意向：自宅で最期を迎えたい", "patient"),
])
def test_a_plain_fact_about_one_family_member_or_other_patient_keeps_its_holder(line, subject):
    assert _line_subjects(line) == [subject]


def _relabelled(line):
    subjects = _line_subjects(line)
    assert subjects, line
    return [subject for subject in subjects if subject in ("family", "other")]


# The patient's own fact that a head word for a family member could take over.
# Each must stay unspecified (or patient under a patient heading); a family or
# other label would hide it.
PATIENT_FACTS = [
    # M1: a request addressed to the person is the patient's wish about them.
    "希望：家族も同席してほしい", "希望：家族は同席してほしい", "希望：家族は同席してください",
    "希望：家族は立ち会ってほしい", "希望：家族は手伝ってほしい", "希望：家族は理解してほしい",
    "希望：家族は反対しないでほしい", "希望：家族は面会にきてほしい", "希望：娘は毎日きてほしい",
    "希望：娘はそばにいてほしい", "希望：夫は無理しないでほしい", "希望：夫は家で待っていてほしい",
    "希望：妻は長生きしてほしい", "希望：孫は健康でいてほしい", "希望：長女は結婚してほしい",
    "希望：長男は同居してほしい",
    # M2: a second item after a space or a symbol may be the patient's.
    "希望：長女は反対 自宅", "意向：夫は了承 在宅希望", "延命：家族は了承 DNAR", "延命：妻は同意 希望なし",
    "アレルギー：夫は卵アレルギー　合成薬Aで発疹", "アレルギー：夫は卵 蕎麦", "アレルギー：夫は卵／蕎麦",
    "アレルギー：夫は卵/蕎麦", "アレルギー：夫は卵・蕎麦", "アレルギー：夫は卵+蕎麦", "アレルギー：夫は卵→蕎麦",
    "アレルギー：夫は卵：蕎麦", "アレルギー：夫は卵【蕎麦】", "アレルギー：夫は卵[蕎麦]", "アレルギー：夫は卵…蕎麦",
    "アレルギー：夫は卵〜蕎麦", "アレルギー：夫は卵ー蕎麦アレルギー", "アレルギー：妻は「卵がだめ」",
    "意向：夫は了承在宅を希望",
    # M3: the family as a whole includes the patient.
    "アレルギー：家族は皆卵アレルギー", "アレルギー：家族は全員卵アレルギー", "アレルギー：家族はみな卵アレルギー",
    "アレルギー：家族は一同花粉症", "希望：家族はみんな在宅を希望", "アレルギー：家族は全員なし",
    # M4: the patient named by a synonym after the family fact.
    "アレルギー：妻は卵アレルギー 対象者は蕎麦", "アレルギー：妻は卵アレルギー 入居者は蕎麦",
    "副作用：長女は眠気 当人なし", "副作用：長女は眠気 当方なし", "副作用：長女は眠気 要介護者なし",
    "副作用：長女は眠気 被介護者なし", "アレルギー：夫は入居者同様卵アレルギー",
    # S1: in a note to the family an honorific kin word can be the patient.
    "アレルギー：ご主人は卵アレルギー", "副作用：お母様は合成薬Aで眠気", "希望：お母様は在宅を希望",
    "延命：お母さんは望まない", "アレルギー：奥様は卵アレルギー", "副作用：お父さんは合成薬Bで発疹",
    "希望：旦那さんは在宅を希望", "アレルギー：主人は卵アレルギー", "副作用歴：お母さんは合成薬Cで発疹",
    "アレルギー：お父様は花粉症", "希望：奥さんは施設を希望",
    # S2: a permission or need is the patient's wish about the person.
    "希望：夫は同席可", "希望：夫は同席不要", "希望：夫は同席不可", "希望：家族は面会禁止",
    "希望：家族は面会制限なし", "希望：妻は面会可", "希望：家族は同席のみ",
    # A self word or the patient in the fact.
    "アレルギー：夫は私同様卵アレルギー", "副作用：妻は自分と同じ合成薬Bで眠気", "希望：妻は私の在宅を希望",
    "アレルギー：夫は本人同様卵アレルギー",
    # A pointer back to the patient's record.
    "副作用：妻は同上で眠気", "副作用：妻は上記で眠気", "副作用：妻はそれで眠気",
    # A second person word in the fact.
    "アレルギー：妻は長女と卵アレルギー", "希望：夫は長女の同居を希望", "アレルギー：夫は長男同様花粉症",
    # A report, observation or perception word.
    "アレルギー：娘は卵アレルギーと報告", "副作用：妻は合成薬Bで眠気と記載", "副作用：妻は眠気を心配",
    "アレルギー：長女は卵アレルギーとのこと",
    # A time or event clause.
    "副作用：長女は昨日から眠気", "アレルギー：妻は訪問時に発疹", "副作用：夫は服用後に眠気",
    # A condition or reason clause.
    "希望：夫は元気なら在宅を希望", "希望：家族は入院したら延命を希望しない", "延命：妻は本人が望むなら希望",
    "希望：夫は元気な時在宅を希望", "副作用：長女は眠気のため中止",
    # を in an allergy or adverse reaction is an act on a drug.
    "アレルギー：長女は合成薬Aを飲ませて発疹", "副作用：娘は薬を管理し眠気", "副作用：妻は合成薬Bを中止",
    # A clinical fact outside the closed shapes.
    "アレルギー：夫は知らなかった卵アレルギー", "アレルギー：妻は上記の薬で発疹", "アレルギー：弟は卵",
    "副作用：妻は 夜間ふらつき", "アレルギー：長女は、卵アレルギー",
]


@pytest.mark.parametrize("line", PATIENT_FACTS)
def test_a_patient_fact_never_becomes_a_family_or_other_fact(line):
    assert _relabelled(line) == []


# A fact at least partly the patient's, by form; none may become family or other.
SHARED_OR_POINTED_FACTS = [
    # も: the patient shares the fact.
    "アレルギー：妻も卵アレルギー", "副作用：妻も合成薬Aで眠気", "希望：妻も在宅を希望", "延命：家族も拒否",
    "アレルギー：家族もなし", "アレルギー：他の利用者もなし", "副作用：夫もなし", "意向：家族も同意",
    # A pointer back to the patient's record inside the fact.
    "アレルギー：妻は同じ卵アレルギー", "副作用：妻は同薬で眠気", "副作用：夫は同剤で眠気",
    "副作用：長男は同種薬で眠気", "副作用：夫は同系薬で眠気", "副作用：娘は同一薬で眠気",
    "アレルギー：妻は同アレルギー", "意向：長男は同内容を希望", "希望：夫は同施設への入所を希望",
    "方針：医師は同方針を提案", "アレルギー：妻はおなじアレルギー", "アレルギー：夫は当該アレルギー",
    "副作用：娘は該当薬で眠気", "副作用：娘は前述薬で眠気", "副作用：娘は上述薬で眠気",
    "副作用：娘は先述薬で眠気", "アレルギー：妻は前出アレルギー", "副作用：妻は前記薬で眠気",
    "アレルギー：妻は右記アレルギー", "アレルギー：妻は下記アレルギー", "アレルギー：妻は以上アレルギー",
    "希望：妻は同じ施設を希望", "方針：医師は同じ内容を提案", "副作用：妻はこれで眠気", "副作用：妻はあれで眠気",
    # A determiner that refers back opens the fact, one per determiner and per head.
    "副作用：妻は当薬で眠気", "アレルギー：妻は当アレルギー", "副作用：妻は本剤で眠気", "希望：妻は本件を希望",
    "アレルギー：妻は本品アレルギー", "副作用：妻は該薬で眠気", "アレルギー：妻は前項アレルギー",
    "アレルギー：妻は前回アレルギー", "アレルギー：妻は前掲アレルギー", "アレルギー：妻は前例アレルギー",
    "副作用：妻は上薬で眠気", "アレルギー：妻は上欄アレルギー", "アレルギー：妻は左記アレルギー",
    "希望：妻は左記を希望", "アレルギー：妻は既知アレルギー", "アレルギー：妻は既往アレルギー",
    "副作用：妻は先薬で眠気", "アレルギー：妻は件アレルギー", "アレルギー：妻は例アレルギー",
    "希望：妻は以下を希望", "副作用：妻は左記薬で眠気", "希望：長男は在宅での前述内容を希望",
    # A together-word of 同 that names the item itself is the patient's item.
    "副作用：妻は同時薬で眠気", "副作用：妻は同日剤で眠気",
    # Everyone but the patient implies the patient has something.
    "アレルギー：本人以外は特になし", "アレルギー：本人以外はなし", "副作用：本人以外はなし",
    "アレルギー：他者は特になし", "アレルギー：他人は特になし", "アレルギー：第三者は特になし",
    "副作用：他者は合成薬Bで眠気", "希望：他人は拒否", "希望：第三者は希望なし",
    # A stance as the object of a proposal is someone's wish, not a thing.
    "方針：医師は意向を提案",
    # 家族の意向 whose value names the patient, on any line.
    "家族の意向：本人は自宅を希望", "家族の希望：患者は延命を拒否", "家族の意向：在宅を希望（本人の意向）",
    "家族の意向：在宅を希望\n本人は施設を希望", "家族の意向：自宅で看取りたい\n本人もそれを望んでいる",
    "家族の意向：ご本人様は自宅を希望", "家族の希望：御本人の希望通りに", "家族の意向：入居者様は施設継続を希望",
    "家族の希望：利用者さんは自宅を希望", "家族の意向：当人は在宅を希望", "家族の意向：私は在宅を希望",
    "家族の意向：自分は在宅を希望",
    # One per patient word of the shared list, and after a holder (母本人は).
    "家族の意向：ほんにんは在宅を希望", "家族の意向：当事者は在宅を希望", "家族の意向：対象者は在宅を希望",
    "家族の希望：対象者の希望通り", "家族の意向：被保険者は在宅を希望", "家族の意向：要介護者は在宅を希望",
    "家族の意向：被介護者は在宅を希望", "家族の希望：クライアントは在宅を希望", "家族の希望：お客様は在宅を希望",
    "家族の意向：自身は在宅を希望", "家族の希望：ご自身の意思を尊重", "家族の意向：じぶんは在宅を希望",
    "家族の希望：わたしは在宅を希望", "家族の希望：わたくしは在宅を希望", "家族の意向：当方は在宅を希望",
    "家族の希望：僕は在宅を希望", "家族の希望：俺は在宅を希望", "家族の意向：彼は在宅を希望",
    "家族の意向：彼女は在宅を希望", "家族の意向：あの方は在宅を希望", "家族の意向：この方は在宅を希望",
    "家族の意向：その方は在宅を希望", "家族の意向：Ptは在宅を希望", "家族の意向：ptは在宅を希望",
    "家族の意向：PTは在宅を希望", "家族の希望：Ptの意向を尊重",
    "家族の意向：母本人は在宅を希望", "家族の意向：父自身は在宅を希望", "家族の希望：お母さん自身は在宅を希望",
    "家族の意向：長男本人は在宅を希望", "家族の意向：長男は本人と在宅を希望",
    # Cheap patient-denoting tokens.
    "アレルギー：夫は対象アレルギー", "副作用：夫は対象で眠気", "アレルギー：夫はptアレルギー",
    "アレルギー：夫はPtアレルギー", "アレルギー：夫はPT卵アレルギー", "副作用：夫はA氏で眠気",
    "アレルギー：夫はほんにんアレルギー", "アレルギー：夫はじぶんアレルギー",
    # A person, self or group word in the fact, one per excluded word.
    "アレルギー：妻はたろうさんアレルギー", "副作用：妻は架空様処方薬で眠気", "副作用：妻はゆきちゃんで発疹",
    "副作用：妻は架空君処方薬で眠気", "副作用：妻は入居者処方薬で眠気", "副作用：妻は本人処方薬で眠気", "副作用：妻は当人処方薬で眠気",
    "副作用：妻は自身処方薬で眠気", "副作用：妻は自分処方薬で眠気", "アレルギー：妻はわたしアレルギー",
    "副作用：妻は当方処方薬で眠気", "副作用：妻は僕処方薬で眠気", "副作用：妻は俺処方薬で眠気",
    "アレルギー：家族はみなアレルギー", "アレルギー：家族はみんなアレルギー", "アレルギー：妻は両方卵アレルギー",
    "アレルギー：家族は双方卵アレルギー", "アレルギー：家族は一同卵アレルギー", "副作用：妻は共通薬で眠気",
    "希望：妻は一緒の施設入所を希望", "アレルギー：家族は揃い卵アレルギー", "副作用：家族はそろいで眠気",
    "希望：夫は二人部屋を希望", "希望：夫はふたりでの外出を希望", "希望：妻は親子入所を希望",
    # Redundant: a family or other word, or 同, already excludes these.
    "副作用：妻は他人処方薬で眠気", "副作用：妻は主人処方薬で眠気", "アレルギー：家族は夫婦卵アレルギー",
    # The closed shapes: no space after the holder, no は・が・も・と・を・の inside a
    # hiragana thing, at most 20 characters, and a staff proposal only under a policy heading.
    "アレルギー：妻は 卵アレルギー", "アレルギー：妻は　卵アレルギー", "アレルギー：妻はたまごがアレルギー",
    "副作用：妻は合成薬A12345678901234567で眠気", "希望：医師は在宅を提案",
]


@pytest.mark.parametrize("line", SHARED_OR_POINTED_FACTS)
def test_a_shared_or_pointed_fact_never_becomes_a_family_or_other_fact(line):
    assert _relabelled(line) == []


# The same shapes without the excluded word keep their holder, so each exclusion above
# is what decides. 家族は（特に）なし says nothing about the patient: unspecified would
# show it as the patient's own none.
@pytest.mark.parametrize(("line", "subject"), [
    ("アレルギー：家族はなし", "family"), ("アレルギー：家族は特になし", "family"),
    ("副作用：妻はなし", "family"), ("アレルギー：他の利用者はなし", "other"),
    ("副作用：妻は合成薬A1234567890123456で眠気", "family"),
    ("アレルギー：妻はたまごアレルギー", "family"), ("副作用：妻は架空処方薬で眠気", "family"),
    ("アレルギー：妻は卵アレルギー", "family"), ("希望：妻は施設入所を希望", "family"),
    ("希望：娘は同居を希望しています", "family"), ("希望：夫は受診への同行を希望", "family"),
    ("方針：医師は在宅を提案", "other"), ("家族の希望：ご主人は在宅を希望", "family"),
    ("家族の意向：在宅を希望", "family"), ("家族の意向：長男は在宅を希望", "family"),
    ("アレルギー：夫はOPTアレルギー", "family"), ("アレルギー：夫はPTFEアレルギー", "family"),
    # A holder with 本人 or 自身 under a heading that is not 家族の意向.
    ("アレルギー：母本人は卵アレルギー", "family"),
    # Not everyone but the patient: one other patient keeps the holder.
    ("副作用：他の利用者は合成薬Bで眠気", "other"),
    # A determiner kanji that names a thing, not a reference back.
    ("希望：妻は当院への入院を希望", "family"), ("希望：長男は本日の面会を希望", "family"),
    ("希望：長男は前日の面会を希望", "family"), ("希望：妻は上肢リハビリを希望", "family"),
    ("副作用：妻は眠前薬で眠気", "family"), ("副作用：妻は先発品で眠気", "family"),
    ("希望：妻は左膝の手術を希望", "family"), ("アレルギー：妻は既製品アレルギー", "family"),
    # 同 that means together points at nothing.
    ("希望：長男は同室を希望", "family"), ("希望：長男は同日退院を希望", "family"),
    ("希望：妻は同時面会を希望", "family"), ("副作用：妻は同時服用で眠気", "family"),
])
def test_the_same_shape_without_the_excluded_word_keeps_its_holder(line, subject):
    assert _line_subjects(line) == [subject]


# A true family fact outside the allowed shapes is left unspecified: the safe
# direction, kept here so a widened shape is a visible decision.
@pytest.mark.parametrize("line", [
    "アレルギー：・長女は架空果実N", "副作用症状：長女は発疹", "アレルギー：妻は合成薬Aにかぶれやすい",
    "アレルギー：長女は合成薬Aが合わない", "希望：妻は自宅で一緒に過ごしたい", "延命：家族は胃ろうは希望しない",
    "アレルギー：お孫さんは卵アレルギー",
    # も: the patient may share it; formerly family / other.
    "アレルギー歴：妻も卵アレルギーあり", "副作用歴：別の利用者も合成薬Fで発疹",
    # A group, patient or pointer word, or a referring determiner, that is a thing here.
    "希望：長女は全額負担を希望", "希望：妻は全面介助を希望", "アレルギー：妻は全身麻酔で発疹",
    "希望：長男は対象施設を希望", "希望：長女は以上を希望", "希望：長男は氏名公表を拒否",
    "副作用：妻は同系統薬で眠気", "アレルギー：妻は同種果実アレルギー", "副作用：妻は下剤で下痢",
    "アレルギー：他人は卵アレルギー", "意向：妻は意向を了承",
])
def test_a_family_fact_outside_the_allowed_shapes_stays_unspecified(line):
    assert _line_subjects(line) == ["unspecified"]


@pytest.mark.parametrize(("line", "subject"), [
    # Under the patient's own heading a person at the head withdraws, never relabels.
    ("本人の希望：お母様は在宅を希望", "unspecified"),
    ("本人の意向：ご主人は反対", "unspecified"),
    ("本人の希望：家族は同席してほしい", "unspecified"),
    ("本人の意向：家族は皆在宅を希望", "unspecified"),
    # 家族の意向 is the family's unless its value names the patient (was family).
    ("家族の意向：本人は同席してほしい", "unspecified"),
    ("家族の希望：ご主人は在宅を希望", "family"),
])
def test_a_subject_heading_decides_when_the_value_does_not(line, subject):
    assert _line_subjects(line) == [subject]


@pytest.mark.parametrize(("label", "value"), [(None, "長女は卵"), ("アレルギー", None), (3, 4), ("", "")])
def test_heading_subject_of_malformed_input_is_unspecified(label, value):
    assert heading_subject(label, value) == "unspecified"


def test_annotate_changes_only_the_subject_of_heading_items_and_does_not_mutate():
    body = ("本人の意向：自宅で最期を迎えたい\n家族の希望：施設入所も考えたい\n"
            "アレルギー：長女は架空果実Mで口がかゆくなる\n既往歴：母が糖尿病\n副作用歴：合成薬Gで眠気")
    items = extract_context(body)
    before = copy.deepcopy(items)
    llm = {"category": "allergies", "text": "長女は架空果実M", "evidence": "長女は架空果実Mで口がかゆくなる",
           "subject": "unspecified"}
    out = annotate_heading_subjects(items + [llm, "not an item"])
    assert items == before
    assert out[-2:] == [llm, "not an item"]
    assert [item["subject"] for item in out[:-2]] == [
        "patient", "family", "family", "unspecified", "unspecified"]
    assert [{key: value for key, value in item.items() if key != "subject"} for item in out[:-2]] == [
        {key: value for key, value in item.items() if key != "subject"} for item in before]
    assert out[0] is not items[0]


@pytest.mark.parametrize("item", [
    # Not from the heading rules: an evidence that opens with a heading line is not enough.
    {"category": "allergies", "text": "長女は架空果実M", "evidence": "アレルギー：長女は架空果実M",
     "subject": "unspecified"},
    # A malformed heading item.
    {"category": "allergies", "text": "長女は架空果実M", "evidence": None, "subject": "unspecified",
     "origin": "explicit_heading"},
    {"category": "allergies", "text": None, "evidence": "アレルギー：長女は架空果実M", "subject": "unspecified",
     "origin": "explicit_heading"},
    # Only the first evidence line is the heading; a heading on a later line is not read.
    {"category": "allergies", "text": "長女は架空果実M", "evidence": "申し送り\nアレルギー：長女は架空果実M",
     "subject": "unspecified", "origin": "explicit_heading"},
])
def test_annotate_passes_other_items_through_unchanged(item):
    assert annotate_heading_subjects([item]) == [item]


def test_annotate_leaves_a_subject_already_named_and_is_idempotent():
    body = "アレルギー：長女は架空果実Mで口がかゆくなる\n本人の意向：自宅で最期を迎えたい"
    named = [{**item, "subject": "other"} for item in extract_context(body)]
    assert annotate_heading_subjects(named) == named
    once = annotate_heading_subjects(extract_context(body))
    assert annotate_heading_subjects(once) == once
    assert annotate_heading_subjects(None) == [] and annotate_heading_subjects([]) == []


def test_annotate_keeps_the_details_of_a_heading_item():
    items = extract_context("副作用症状：長女は合成薬Aで発疹")
    assert items[0]["details"]
    out = annotate_heading_subjects(items)
    assert out[0]["subject"] == "family" and out[0]["details"] == items[0]["details"]


def _score(annotate):
    scores = [extract_bench._score_case(case, {"patient_context": annotate(extract_context(case["body"]))})
              for case in CASES]
    return scores, extract_bench._aggregate(scores)["patient_context"], extract_bench._false_assertions(scores)


def _unannotated(items):
    # The generation-11 rule output, whether or not rule 30 is wired into extract_context.
    return [{**item, "subject": "unspecified"} if item.get("origin") == "explicit_heading" else item
            for item in items]


def test_rule30_removes_the_corpus_wrong_assertion_and_keeps_recall():
    scores, row, wrong = _score(lambda items: annotate_heading_subjects(_unannotated(items)))
    assert wrong["total"] == 0, [(s["id"], s["forbid_violations"]) for s in scores if s["forbid_violations"]]
    # extract_bench reports recall rounded to 3 places; 8 of 12 reads as 0.667.
    assert row["recall"] >= 0.667 and row["fp"] == 0
    assert (row["tp"], row["fn"]) == (8, 4)
    # Without rule 30 the same corpus asserts the family's allergy as the patient's.
    scores, before, wrong = _score(_unannotated)
    assert wrong["total"] == 1 and (before["tp"], before["fn"]) == (8, 4)
    assert [s["id"] for s in scores if s["forbid_violations"]] == ["alg-family-heading"]


def test_corpus_items_outside_the_rule_stay_unspecified():
    named = {}
    for case in CASES:
        for item in annotate_heading_subjects(_unannotated(extract_context(case["body"]))):
            if item["subject"] != "unspecified":
                named[case["id"]] = (item["category"], item["subject"])
    # 本人の意向 is the patient's; 長女は under an allergy heading is the family's.
    assert named == {"acp-done-heading": ("preferences", "patient"),
                     "alg-family-heading": ("allergies", "family")}
