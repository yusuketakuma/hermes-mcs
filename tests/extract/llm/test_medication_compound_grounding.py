"""Source-bound medication names and narrowly supported compound changes."""

import pytest

import extract_validation


def _item(name, action, status, evidence, *, dose=None):
    return {"name": name, "action": action, "status": status,
            "subject": "patient", "negated": False, "dose": dose,
            "evidence": evidence}


def test_medication_name_boundaries_keep_source_offsets():
    body = "本人は架空薬Ａ5mgを開始し、架空薬ＡＡを開始しました。"
    assert [body[a:b] for a, b in extract_validation._med_name_spans(body, "架空薬A")] == ["架空薬Ａ"]
    assert [body[a:b] for a, b in extract_validation._med_name_spans(body, "架空薬AA")] == ["架空薬ＡＡ"]
    assert not extract_validation._med_name_spans("本人は架空薬AAを開始しました", "架空薬A")
    assert not extract_validation._med_name_spans("本人は架空薬A12Xを開始しました", "架空薬A")
    assert not extract_validation._med_name_spans("本人は架空薬A5mgAAを開始しました", "架空薬A")
    assert not extract_validation._med_name_spans("本人は別架空薬Aを開始しました", "架空薬A")
    assert extract_validation._med_name_spans("本人は架空薬Aを開始しました", "架空薬A")
    assert extract_validation._med_name_spans("本人は本日架空薬Aを開始しました", "架空薬A")
    assert extract_validation._med_name_spans("本人は本日 架空薬Aを開始しました", "架空薬A")
    assert extract_validation._med_name_spans("本人は明日から架空薬Aを開始します", "架空薬A")
    assert extract_validation._med_name_spans("本人は降圧薬アムロジピンを中止しました", "アムロジピン")
    assert extract_validation._med_name_spans("本人は架空薬Aを開始し架空薬Bを開始しました", "架空薬B")
    assert not extract_validation._med_name_spans("本人は開始し別架空薬Aを開始しました", "架空薬A")


@pytest.mark.parametrize("suffix", ["配合錠", "徐放錠", "別名", "5mg配合錠",
                                   "こども用", "5mgこども用", "のど飴", "のしろっぷ", "はちみつ味"])
def test_drug_name_with_unproved_suffix_remains_unverified(suffix):
    body = f"本人は架空薬A{suffix}を開始しました。"
    item = _item("架空薬A", "start", "current", body)
    result = extract_validation.validate({"meds": [item]}, body)
    assert result["meds"][0].get("unverified") is True
    assert result["meds"][0]["evidence"] == body


@pytest.mark.parametrize("tail", ["を中止", "は中止", "が開始", "も中止", "の中止", " 5mgを開始", "5mg錠を開始"])
def test_drug_name_with_particle_or_strength_is_whole(tail):
    body = f"本人は架空薬A{tail}しました。"
    assert len(extract_validation._med_name_spans(body, "架空薬A")) == 1


@pytest.mark.parametrize("verb", ["やめました", "やめた", "やめています", "のみました", "のんでいます"])
def test_hiragana_medication_verbs_keep_the_source_name_boundary(verb):
    body = f"本人は架空薬Aを{verb}。"
    item = _item("架空薬A", "none", "current", body)
    assertion = extract_validation.Validator(body, None).assertion(item, ("架空薬A",), medication=True)
    assert assertion is not None
    assert body[assertion[1]:assertion[2]] == "架空薬A"
    result = extract_validation.validate({"meds": [item]}, body)
    assert result["meds"][0]["evidence"] == body


def test_named_drug_inside_longer_kanji_name_remains_unverified():
    body = "本人は別架空薬Aを開始しました。"
    item = _item("架空薬A", "start", "current", body)
    result = extract_validation.validate({"meds": [item]}, body)
    assert result["meds"][0].get("unverified") is True


@pytest.mark.parametrize("action,status,verb", [("stop", "past", "中止"), ("start", "current", "開始")])
def test_compound_action_keeps_first_drug_verified(action, status, verb):
    body = f"本人は架空薬A5mgを{verb}し、架空薬AA10mgを開始しました。"
    item = _item("架空薬A", action, status, body, dose="5mg")
    result = extract_validation.validate({"meds": [item]}, body)
    assert result["meds"][0].get("unverified") is not True
    assert result["meds"][0]["status"] == status
    assert result["meds"][0]["evidence"] == body


def test_compound_conflicting_action_is_not_verified_or_lost():
    body = "本人の架空薬Aを中止し、架空薬Bを開始しました。"
    item = _item("架空薬A", "start", "current", body)
    result = extract_validation.validate({"meds": [item]}, body)
    assert result["meds"][0]["action"] == "start"
    assert result["meds"][0]["negated"] is False
    assert result["meds"][0].get("unverified") is True


@pytest.mark.parametrize("body", [
    "本人は架空薬Aを中止し、架空薬Bを開始しませんでした。",
    "本人は架空薬Aを中止し、架空薬Bを開始する予定です。",
    "本人は架空薬Aを中止し、家族は架空薬Bを開始しました。",
    "家族は架空薬Aを中止し、架空薬Bを開始しました。",
    "もし本人が架空薬Aを中止し、架空薬Bを開始した場合。",
    "本人は架空薬Aを中止し、架空薬Bを開始しましたか？",
    "本人は架空薬Aを中止し、架空薬Bを開始しましたが中止は誤りです。",
    "本人は架空薬Aを中止し、架空薬Bを開始しました。訂正: 中止していません。",
    "本人は架空薬Aを中止し、架空薬Bを開始しました。架空薬Aの中止を取り消しました。",
    "本人は架空薬Aを中止し、架空薬Bを開始しました。この薬は中止していません。",
    "本人は架空薬Aを中止し、架空薬Bを開始しました。該当薬は中止していません。",
    "本人は架空薬Aを中止し、架空薬Bを開始しました。前の薬は中止していません。",
    "本人は架空薬Aを中止し、架空薬Bを開始しました。家族のこの薬は中止していません。",
    "本人は架空薬Aを中止し、架空薬Aを開始しました。",
    "本人は架空薬Aを中止し、架空薬aを開始しました。",
    "本人は架空薬Ａを中止し、架空薬aを開始しました。",
    "本人は架空薬Aを中止し、運動を開始しました。",
])
def test_compound_shortcut_does_not_infer_ambiguous_source(body):
    name = "架空薬A"
    start, end = extract_validation._med_name_spans(body, name)[0]
    item = _item(name, "stop", "past", body)
    assert not extract_validation._compound_medication_done(body, item, start, end)
