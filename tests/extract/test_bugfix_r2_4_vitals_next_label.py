"""v4 vitals guard: a label after the number names the NEXT reading (synthetic)."""
import pytest

import extract_llm


@pytest.mark.parametrize('body, vit', [
    ('P72 SpO2測定不可', {'hr': 72}),
    ('P88 BS測定せず', {'hr': 88}),
    ('P72 SpO2 97%', {'hr': 72, 'spo2': 97}),
    ('KT36.5 SpO2 97', {'bt': 36.5}),
    ('KT36.5 BP120/80', {'bt': 36.5}),
    ('体温36.5℃', {'bt': 36.5}),
    ('36.5℃', {'bt': 36.5}),
    ('120/80mmHg', {'sbp': 120, 'dbp': 80}),
])
def test_following_label_does_not_relabel(body, vit):
    assert extract_llm._vitals_guard(body, dict(vit)) == vit


def test_unit_after_number_still_remaps():
    assert extract_llm._vitals_guard('36.5℃', {'hr': 36.5}) == {'bt': 36.5}
