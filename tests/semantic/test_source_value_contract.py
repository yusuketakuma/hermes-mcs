"""Absolute written dates and entity tokens preserve the original source contract."""
import pytest

import clinical_values
import semantic_facts
import semantic_projection


@pytest.mark.parametrize("text,expected", [
    ("1999-12-31", None), ("2000/1/1予定", "2000-01-01"),
    ("2100年12月31日", "2100-12-31"), ("2101-01-01", None),
    ("2024-02-29", "2024-02-29"), ("2026-02-29", None),
    ("2026-02-31", None), ("次回1月", None), ("明日", None),
    ("予定 2026-10-01", None), (None, None), (True, None),
])
def test_original_absolute_date_contract_is_conservative(text, expected):
    assert clinical_values.absolute_date(text) == expected


@pytest.mark.parametrize("statement,expected", [
    (None, set()), ("", set()), ("アムロジピン 5mg", {"アムロジピン", "5mg"}),
    ("DrugＡ 投与", {"drug", "投与"}), ("同一対象を中止", {"同一対象", "中止"}),
])
def test_entity_token_identity_is_not_the_generic_word_filter(statement, expected):
    assert semantic_facts.entity_tokens(statement) == expected


def test_projection_longest_entity_and_tie_break_are_unchanged():
    assert semantic_projection._v2_drug_ref("DrugB DrugA") == "druga"
    assert semantic_projection._v2_drug_ref("AB LONGER") == "longer"
