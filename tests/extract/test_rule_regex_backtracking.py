"""Crafted post bodies must not make rule regexes backtrack super-linearly.

Fully synthetic strings only. The bounds are generous: the old patterns took
minutes on these inputs, the bounded ones take milliseconds.
"""
import time

import clinical_chunking
import extract
import extract_llm


def _fast(fn, budget=1.0):
    start = time.perf_counter()
    fn()
    assert time.perf_counter() - start < budget


def test_med_token_is_linear_on_name_then_digits_without_unit():
    _fast(lambda: extract._MED_TOKEN.findall("処方" * 750 + "1" * 3000))


def test_dose_and_inquiry_cues_are_bounded_on_long_runs():
    _fast(lambda: clinical_chunking._DOSE.search("1" * 20000))
    _fast(lambda: extract_llm._EVENT_CUES["prescription_inquiry"].search("処方" * 5000))


def test_bounded_patterns_keep_realistic_matches():
    assert extract._MED_TOKEN.findall("アムロジピン錠5mg 1錠") == [("アムロジピン錠", "5", "mg")]
    assert extract._MED_TOKEN.findall("マグミット錠330mg") == [("マグミット錠", "330", "mg")]
    assert [m.group(0) for m in clinical_chunking._DOSE.finditer("朝0.5mg 夕1.25 mg 10錠")] \
        == ["0.5mg", "1.25 mg", "10錠"]
    # over-long numbers are not split into a smaller "dose"
    assert clinical_chunking._DOSE.search("0.1234567mg") is None
    assert extract._MED_TOKEN.findall("ロキソニン1234567mg") == []
    assert extract_llm._EVENT_CUES["prescription_inquiry"].search("処方について先生へ照会しました")
