"""Synthetic next-contract builders stay inert until the explicit generation switch."""
from copy import deepcopy
import json

import pytest

import clinical_allergies
import extract_llm_prompts as prompts
import extract_schema
import extract_validation
import patient_context
import pattern_cards


def _defaults():
    return (
        json.dumps(extract_schema.SCHEMA, sort_keys=True, ensure_ascii=False),
        json.dumps(extract_schema.SCHEMA_BATCH, sort_keys=True, ensure_ascii=False),
        prompts._PROMPT_SPEC, prompts._PROMPT_HEAD, prompts._BATCH_HEAD,
        prompts._REPAIR_HEAD, pattern_cards.FIELD_PATHS, pattern_cards.ENUMS,
    )


def test_builders_leave_runtime_schema_prompt_cache_heads_and_cli_unchanged():
    before = _defaults()
    single = extract_schema.gen12_schema()
    batch = extract_schema.batch_schema(single)
    spec, head, batch_head = prompts.gen12_prompt_heads()
    fields, enums = pattern_cards.schema_vocabulary(single)

    assert _defaults() == before
    assert clinical_allergies.FIELD not in extract_schema.SCHEMA["schema"]["properties"]
    assert clinical_allergies.FIELD in single["schema"]["properties"]
    assert "allergies.allergen" in fields - pattern_cards.FIELD_PATHS
    assert set(clinical_allergies.KINDS) <= enums
    assert head == spec + prompts._PROMPT_EXAMPLES
    assert batch_head == spec + prompts._BATCH_FMT
    assert prompts._PROMPT_HEAD == prompts._PROMPT_SPEC + prompts._PROMPT_EXAMPLES
    assert prompts._BATCH_HEAD == prompts._PROMPT_SPEC + prompts._BATCH_FMT
    assert batch["schema"]["properties"]["items"]["items"] is not extract_schema.BATCH_ITEM

    # Returned shapes share no mutable children with either default or a later build.
    single["schema"]["properties"]["symptoms"]["items"]["properties"].clear()
    assert extract_schema.gen12_schema()["schema"]["properties"]["symptoms"]["items"]["properties"]
    assert _defaults() == before


@pytest.mark.parametrize("single", [extract_schema.SCHEMA, None], ids=["current", "next"])
def test_batch_item_preserves_the_individual_shape_and_requires_only_its_index(single):
    single = extract_schema.gen12_schema() if single is None else single
    before = deepcopy(single)
    batch = extract_schema.batch_schema(single)
    item = deepcopy(batch["schema"]["properties"]["items"]["items"])
    assert item["properties"].pop("i") == {"type": "integer"}
    assert item["required"].pop() == "i"
    assert item == single["schema"]
    assert single == before
    assert extract_schema.BATCH_ITEM is extract_schema.SCHEMA_BATCH["schema"]["properties"]["items"]["items"]
    assert extract_schema.SCHEMA_BATCH == extract_schema.batch_schema(extract_schema.SCHEMA)


@pytest.mark.parametrize("owner,name,extra", [
    (patient_context, "CATEGORIES", "synthetic_category"),
    (patient_context, "DETAIL_KEYS", "synthetic_detail"),
    (clinical_allergies, "KINDS", "synthetic_kind"),
    (clinical_allergies, "CERTAINTIES", "synthetic_certainty"),
    (clinical_allergies, "SUBJECTS", "synthetic_subject"),
    (clinical_allergies, "STATUSES", "synthetic_status"),
])
def test_one_canonical_addition_reaches_schema_prompt_and_pattern_cards(monkeypatch, owner, name, extra):
    before = _defaults()
    monkeypatch.setattr(owner, name, (*getattr(owner, name), extra))
    single = extract_schema.gen12_schema()
    fields, enums = pattern_cards.schema_vocabulary(single)
    spec, head, batch_head = prompts.gen12_prompt_heads()
    assert extra in enums
    assert extra in spec and extra in head and extra in batch_head
    if name == "CATEGORIES":
        assert f"patient_context.{extra}" in fields
    if name == "DETAIL_KEYS":
        assert all(f"patient_context.{category}.{extra}" in fields
                   for category in patient_context.CATEGORIES)
    assert _defaults() == before
    item = extract_schema.batch_schema(single)["schema"]["properties"]["items"]["items"]
    assert item["properties"] == {**single["schema"]["properties"], "i": {"type": "integer"}}


def test_allergy_schema_matches_the_eight_key_candidate_contract():
    item = extract_schema.gen12_schema()["schema"]["properties"][clinical_allergies.FIELD]["items"]
    props = item["properties"]
    assert set(props) == {"allergen", "kind", "reaction", "subject", "status", "negated", "certainty", "evidence"}
    assert props["kind"]["enum"] == list(clinical_allergies.KINDS)
    assert props["subject"]["enum"] == list(clinical_allergies.SUBJECTS)
    assert props["status"]["enum"] == list(clinical_allergies.STATUSES)
    assert props["certainty"]["enum"] == list(clinical_allergies.CERTAINTIES)
    assert props["negated"] == {"type": "boolean"}
    assert all(props[key] == {"type": ["string", "null"]}
               for key in ("allergen", "reaction", "evidence"))
    assert set(item["required"]) == {"kind", "subject", "status", "negated", "certainty"}
    assert item["additionalProperties"] is False
    assert "unverified" not in props
    assert "未確認候補" in prompts.gen12_prompt_heads()[0]


@pytest.mark.parametrize("axes", [
    {}, {"subject": "family"}, {"status": "planned"}, {"negated": True},
])
def test_current_and_next_stub_responses_validate_without_confirming_allergies(axes):
    body = "合成薬Aのアレルギーについて確認しました。"
    old_response = {"symptoms": []}
    claim = {"allergen": "合成薬A", "kind": "allergy", "reaction": None,
             "subject": "patient", "status": "current", "negated": False,
             "certainty": "confirmed", "evidence": body, **axes}
    next_response = {**old_response, clinical_allergies.FIELD: [claim]}
    assert extract_validation.validate(old_response, body) == old_response
    assert extract_validation.validate(next_response, body) == {
        **old_response, clinical_allergies.FIELD: [{**claim, "unverified": True}]}


def test_empty_and_absent_background_use_identical_prompt_strings(monkeypatch):
    current = prompts._extract_prompt_context(None, False, [])[0]
    assert prompts._extract_prompt_context("", False, [])[0] == current
    _spec, head, _batch_head = prompts.gen12_prompt_heads()
    monkeypatch.setattr(prompts, "_PROMPT_HEAD", head)
    assert prompts._extract_prompt_context(None, False, [])[0] == head
    assert prompts._extract_prompt_context("", False, [])[0] == head
    assert prompts._CTX_HEAD not in head


def test_reply_and_existing_response_fields_survive_the_next_schema():
    before = extract_schema.SCHEMA["schema"]["properties"]
    after = extract_schema.gen12_schema()["schema"]["properties"]
    assert all(after[key] == value for key, value in before.items())
    assert {"summary", "points", "urgency", "urgency_evidence", "reply"} <= after.keys()
    assert set(after["event_records"]["items"]["required"]) == {
        "kind", "subject", "status", "negated", "evidence"}
    assert after["reply"] == before["reply"]
    assert '"reply"' in prompts._CTX_HEAD


def test_pattern_cards_next_vocabulary_requires_an_explicit_switch(monkeypatch):
    card = {"class": "adverse", "field_paths": ["allergies.allergen"],
            "attributes": {"negation": False, "subject": "patient", "tense": "present",
                           "condition": "unconditional"},
            "counts": {"posts": 30, "patients": 5, "professions": 2},
            "current_shape": "missing", "expected_shape": ["allergies.allergen", "allergy"]}
    assert "vocabulary:$.field_paths" in pattern_cards.lint(card, set())
    fields, enums = pattern_cards.schema_vocabulary(extract_schema.gen12_schema())
    monkeypatch.setattr(pattern_cards, "FIELD_PATHS", fields)
    monkeypatch.setattr(pattern_cards, "ENUMS", enums)
    assert pattern_cards.lint(card, set()) == []
