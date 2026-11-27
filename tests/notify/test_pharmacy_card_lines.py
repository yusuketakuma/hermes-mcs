"""Shared cards show only the five allowed pharmacy details, on wholly synthetic posts."""
import json

import pytest

import structured_view
from inference_version import CURRENT_VERSION, VERSION_KEY
from mcs_queries import current_projection_id
from semantic_projection import PROJECTION_VERSION
from notify_lineworks_testkit import LINEWORKS
from notify_testkit import CFG, NOW, _dispatch, _intent, _latest_render, _msg, _patient, led as led
from slack_testkit import SLACK

BODY = "本人の薬は冷蔵保管です。キーパーソンは合成長女です。一包化で配薬しています。"
STORAGE = "本人の薬は冷蔵保管です。"


def _extract(led, mid, context, *, events=None, kind="extract_llm"):
    content_hash = led.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=?", (mid,)).fetchone()[0]
    document = {"summary": "合成の要約", "patient_context": context, **({"events": events} if events else {})}
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) VALUES(?,1,?,?,?,?)",
        (kind, mid, json.dumps(document, ensure_ascii=False),
         json.dumps({VERSION_KEY: CURRENT_VERSION, "hash": content_hash, "extract_version": 7}), NOW))
    led.db.commit()


def _item(category, evidence, details=(), subject="patient"):
    return {"category": category, "text": evidence, "evidence": evidence, "subject": subject,
            "details": list(details)}


def _storage():
    return _item("medication_management", STORAGE,
                 [{"key": "storage_status", "value": "冷蔵保管", "evidence": STORAGE}])


def _seed(led, context, body=BODY, **kw):
    _patient(led)
    _msg(led, 100, body=body)
    _msg(led, 101, parent=100, body="合成の返信です。")
    _extract(led, 100, context, **kw)


def _card_text(led, cfg):
    assert _dispatch(led, _intent(led), cfg)["dispatched"]
    spec = json.loads(_latest_render(led)["spec_json"])
    return "\n".join(c.get("text", "") for c in spec["parts"]["containers"])


@pytest.mark.parametrize("cfg", [CFG, SLACK, LINEWORKS], ids=["discord", "slack", "lineworks"])
def test_a_storage_detail_shows_one_line_on_every_platform(led, cfg):
    _seed(led, [_storage()])
    text = _card_text(led, cfg)
    assert text.count("服薬管理（保管状況）: 「冷蔵保管」") == 1


def test_other_details_and_identity_categories_in_the_same_parent_never_leak(led):
    mixed = _item("medication_management", BODY, [
        {"key": "storage_status", "value": "冷蔵保管", "evidence": STORAGE},
        {"key": "support_method", "value": "一包化", "evidence": "一包化で配薬しています。"},
        {"key": "supporter", "value": "合成長女", "evidence": "キーパーソンは合成長女です。"}])
    _seed(led, [mixed, _item("contacts", "キーパーソンは合成長女です。"),
                _item("demographics", "本人の薬は冷蔵保管です。")])
    lines = structured_view.structured_lines(led.db, 100, plain=True)
    pharmacy = [line for line in lines if line.startswith("服薬管理（")]
    assert pharmacy == ["服薬管理（保管状況）: 「冷蔵保管」"]
    assert not any("合成長女" in line or "一包化" in line for line in pharmacy)


def test_a_post_without_the_allowed_details_renders_as_before(led, monkeypatch):
    _seed(led, [_item("living", "本人の薬は冷蔵保管です。")])
    after = structured_view.structured_lines(led.db, 100, plain=True)
    monkeypatch.setattr(structured_view, "_pharmacy_detail_lines", lambda db, mid: [])
    assert structured_view.structured_lines(led.db, 100, plain=True) == after


@pytest.mark.parametrize("subject,suffix", [("family", "(対象:家族)"), ("other", "(対象:本人以外)")])
def test_a_non_patient_detail_keeps_its_subject(led, subject, suffix):
    item = _storage()
    _seed(led, [{**item, "subject": subject}])
    assert f"服薬管理（保管状況）: 「冷蔵保管」{suffix}" in structured_view.structured_lines(led.db, 100)


def test_the_inquiry_event_label_does_not_claim_completion(led):
    body = "合成薬Aの用量を処方医に疑義照会しました。"
    _seed(led, [], body=body, events=["prescription_inquiry"])
    lines = structured_view.structured_lines(led.db, 100, plain=True)
    assert "区分: 処方の照会・提案の記載" in lines


def test_a_canonical_projection_still_shows_the_extraction_detail(led):
    _seed(led, [_storage()])
    content_hash = led.db.execute("SELECT content_hash FROM messages WHERE message_id=100").fetchone()[0]
    inserted = led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) VALUES(?,1,100,?,?,?)",
        ("canonical_projection", json.dumps({"summary": "合成の投影", "facts": []}),
         json.dumps({VERSION_KEY: CURRENT_VERSION, "hash": content_hash,
                     "projection_version": PROJECTION_VERSION}), NOW)).lastrowid
    led.db.commit()
    selected = led.db.execute(f"SELECT {current_projection_id()} FROM messages m WHERE m.message_id=100").fetchone()[0]
    assert selected == inserted
    assert "服薬管理（保管状況）: 「冷蔵保管」" in structured_view.structured_lines(led.db, 100)


@pytest.mark.parametrize("change", ["edited", "deleted"])
def test_an_edited_or_deleted_source_shows_no_detail(led, change):
    _seed(led, [_storage()])
    if change == "edited":
        led.db.execute("UPDATE messages SET body_text=?, content_hash=? WHERE message_id=100",
                       ("合成の別本文です。", f"{999:064x}"))
    else:
        led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=100")
    led.db.commit()
    assert not any(line.startswith("服薬管理（")
                   for line in structured_view.structured_lines(led.db, 100))


@pytest.mark.parametrize("body,key,value", [
    ("本人の薬は冷蔵保管していません。", "storage_status", "冷蔵保管"),
    ("以前は冷蔵庫、現在は常温で保管しています。", "storage_place", "冷蔵庫"),
    ("母が自分の薬を冷蔵庫に保管しています。", "storage_place", "冷蔵庫"),
    ("本人の薬は冷蔵庫で保管しているかは未確認です。", "storage_place", "冷蔵庫"),
])
def test_negated_past_or_another_persons_storage_value_is_not_a_patient_current_fact(led, body, key, value):
    item = _item("medication_management", body, [{"key": key, "value": value, "evidence": body}])
    _seed(led, [item], body=body)
    assert not any(line.startswith("服薬管理（") for line in structured_view.structured_lines(led.db, 100))


@pytest.mark.parametrize("body,evidence,shown", [
    ("本人の薬は冷蔵庫で保管、副作用なし。", None, True),
    ("本人の薬は冷蔵庫で保管しており副作用はありません。", None, True),
    ("本人の薬は冷蔵庫で保管しており副作用はありませんとのことです。", "冷蔵庫", True),
    ("本人の薬は冷蔵庫で保管していて残薬はありません。", "冷蔵庫", True),
    ("薬の冷蔵庫での保管は中止し再開はしていません。", "冷蔵庫", False),
    ("薬の冷蔵庫での保管は中止し常温に変更しました。", "冷蔵庫", False),
    ("薬の冷蔵庫での保管を中止した後に再開しました。", "冷蔵庫", True),
    ("薬の冷蔵庫での保管を中止した後に再開しましたが、副作用はありません。", "冷蔵庫", True),
    ("薬の冷蔵庫での保管を中止した後に再開しましたが残薬については未確認です。", "冷蔵庫", True),
    ("薬の冷蔵庫での保管を中止した後に再開しました、頭痛の訴えはありませんとのことです、残薬は未確認です。", "冷蔵庫", True),
    ("薬の冷蔵庫での保管を中止した後に再開しましたが、実際には再開していません。", "冷蔵庫", False),
    ("薬の冷蔵庫での保管を中止した後に再開しましたが、再び中止しました。", "冷蔵庫", False),
    ("薬の冷蔵庫での保管を中止した後に再開しましたが、副作用はありませんが今は再び中止しました。", "冷蔵庫", False),
    ("薬の冷蔵庫での保管を中止した後に再開しましたが、今は常温に変更しました。", "冷蔵庫", False),
    ("薬の冷蔵庫での保管を中止した後に常温で再開しましたが、副作用はありません。", "冷蔵庫", False),
    ("薬の冷蔵庫での保管を中止した後に再開したいのですが、副作用はありません。", "冷蔵庫", False),
    ("薬の冷蔵庫での保管を中止し、その後保管を再開しました。", "冷蔵庫", True),
    ("薬の冷蔵庫での保管を中止し通院を再開しました。", "冷蔵庫", False),
    ("薬の冷蔵庫での保管を中止し再開を検討しています。", "冷蔵庫", False),
    ("薬の冷蔵庫での保管を中止する予定です。", "冷蔵庫", False),
    ("現在は冷蔵庫で保管しています。来月から中止する予定です。", "冷蔵庫", True),
    ("本人の薬は冷蔵庫で保管しており残薬については確認しておりません。", "冷蔵庫", True),
    ("本人の薬は冷蔵庫で保管しており保管場所については確認しておりません。", "冷蔵庫", False),
    ("本人の薬は以前から冷蔵庫で保管しています。", "冷蔵庫", True),
    ("本人の薬は冷蔵庫で保管していました。", "冷蔵庫", False),
    ("本人の薬は冷蔵庫での保管を中止しました。", "冷蔵庫", False),
    ("本人の薬は以前から冷蔵庫で保管していました。", "冷蔵庫", False),
    ("本人の薬は以前から冷蔵庫で保管していましたが今は中止しました。", "冷蔵庫", False),
    ("本人の薬は冷蔵庫で保管しており頭痛の訴えはありません。", "冷蔵庫", True),
    ("本人の薬は冷蔵庫で保管しておりません。", "冷蔵庫", False),
    ("本人の薬は冷蔵庫で保管していません。副作用はありません。", "冷蔵庫", False),
    ("本人の薬は冷蔵庫で保管していると聞きましたが実際には保管していません。", "冷蔵庫", False),
    ("本人の薬は冷蔵庫に移しており本人は冷蔵庫では保管していません。", "冷蔵庫に移し", False),
    ("本人の薬は冷蔵庫で保管しており確認はしていません。", "冷蔵庫", False),
    ("本人の薬は冷蔵庫で保管しており副作用はありませんが実際には保管していません。", "冷蔵庫", False),
    ("本人の薬は以前は冷蔵庫で保管しており副作用はありません。", "冷蔵庫", False),
    ("本人の薬は冷蔵庫で保管する予定で副作用はありません。", "冷蔵庫", False),
    ("本人の薬は冷蔵庫で保管しているかは未確認です。", "冷蔵庫", False),
])
def test_storage_card_uses_the_storage_predicate_in_the_source(led, body, evidence, shown):
    item = _item("medication_management", body, [{"key": "storage_place", "value": "冷蔵庫", "evidence": evidence or body}])
    _seed(led, [item], body=body)
    assert ("服薬管理（保管場所）: 「冷蔵庫」" in _card_text(led, SLACK)) is shown


def test_storage_scope_without_a_detail_key_requires_the_same_source_predicate():
    from patient_context import noncurrent_detail

    body = "本人の薬は冷蔵庫で保管しており残薬については確認しておりません。"
    assert not noncurrent_detail(body, "冷蔵庫", "冷蔵庫")
    assert noncurrent_detail(body, "冷蔵庫", "冷蔵庫", key="inquiry_result")


@pytest.mark.parametrize("body,value", [
    ("本人の薬は常温で保管しています。", "常温で保管"),
    ("本人の薬は冷蔵保管しており残薬については確認しておりません。", "冷蔵保管"),
])
def test_a_current_patient_storage_value_still_reaches_the_shared_card(led, body, value):
    item = _item("medication_management", body, [{"key": "storage_status", "value": value, "evidence": body}])
    _seed(led, [item], body=body)
    assert f"服薬管理（保管状況）: 「{value}」" in _card_text(led, SLACK)
