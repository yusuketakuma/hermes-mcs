"""Synthetic boundary cases for shared request scalar validation."""
import pytest

from mcs_util import valid_text


@pytest.mark.parametrize("value,size,nullable,expected", [
    (None, 1, False, False),
    (None, 1, True, True),
    ("", 1, False, False),
    (" ", 1, False, False),
    ("\t\n", 2, True, False),
    ("a", 1, False, True),
    ("ab", 1, False, False),
    ("a", 0, False, False),
    (" a ", 3, False, True),
    (" a ", 2, False, False),
    ("a\x00", 2, True, False),
    ("あ", 1, False, True),
    (False, 1, True, False),
    (1, 1, True, False),
    (b"a", 1, True, False),
])
def test_valid_text_preserves_scalar_contract(value, size, nullable, expected):
    assert valid_text(value, size, nullable) is expected
