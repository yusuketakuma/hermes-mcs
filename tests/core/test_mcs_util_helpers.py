"""Synthetic checks for the shared error-code, strict-JSON and text-hash helpers."""
import hashlib
import json

import pytest
import mcs_util as util


def test_safe_error_code_keeps_code_shaped_value_error():
    assert util.safe_error_code(ValueError("sample_code_1")) == "sample_code_1"


def test_safe_error_code_hides_free_text_value_error():
    assert util.safe_error_code(ValueError("free text /tmp/x")) == "ValueError"


def test_safe_error_code_uses_type_name_for_other_errors():
    assert util.safe_error_code(OSError("sample_code")) == "OSError"


def test_unique_pairs_rejects_duplicate_key():
    assert json.loads('{"a": 1, "b": 2}', object_pairs_hook=util.unique_pairs) == {"a": 1, "b": 2}
    with pytest.raises(ValueError, match="^duplicate_key$"):
        json.loads('{"a": 1, "a": 2}', object_pairs_hook=util.unique_pairs)


@pytest.mark.parametrize("raw", ['{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'])
def test_reject_constant_rejects_nonfinite(raw):
    with pytest.raises(ValueError, match="^invalid_number$"):
        json.loads(raw, parse_constant=util.reject_constant)


def test_text_sha256_matches_hashlib():
    text = "合成テキスト sample"
    assert util.text_sha256(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_strict_json_accepts_finite_object():
    assert util.strict_json('{"a": 1.5, "b": [2]}') == {"a": 1.5, "b": [2]}


@pytest.mark.parametrize("raw, code", [
    ('{"a": 1, "a": 2}', "duplicate_json_key"),
    ('{"x": NaN}', "nonfinite_json_number"),
    ('{"x": -Infinity}', "nonfinite_json_number"),
    ('{"x": 1e999}', "nonfinite_json_number"),
])
def test_strict_json_rejects_with_distinct_codes(raw, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        util.strict_json(raw)
