"""Pure request scalars and release policy must not initialize queue ownership."""
import pytest

import mcs_util
import update_policy
from ops_testkit import bounded_python


@pytest.mark.parametrize("value,expected", [
    ("a" * 64, True), ("0" * 64, True), ("A" * 64, False),
    ("a" * 63, False), ("a" * 65, False), ("a" * 64 + "\n", False),
    ("a" * 63 + "\x00", False), (None, False), (True, False), (64, False),
])
def test_hash_shape_independent_table(value, expected):
    assert mcs_util.valid_hash(value) is expected


@pytest.mark.parametrize("value,expected", [
    ("v1.2.3", True), ("1.2.3", True), ("v01.02.03", True),
    ("v1.2.3-rc.1", True), ("v1.2.3-0.alpha-x", True),
    ("v1.2", False), ("V1.2.3", False), ("v1.2.3\n", False),
    ("v1.2.3-rc..1", False), ("v1.2.3-", False), ("v1.2.3+build", False),
    ("v1.0.0-" + "a" * 250, False), (None, False), (123, False),
])
def test_release_grammar_independent_table(value, expected):
    assert update_policy.release_tag(value) is expected


@pytest.mark.parametrize("module", ["operation_contract", "update_policy"])
def test_pure_operation_contract_import_has_no_queue_backedge(tmp_path, module):
    bounded_python(tmp_path, """
import builtins
import importlib
assert sys.argv[1] not in sys.modules
assert 'mcs_requests' not in sys.modules
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'mcs_requests':
        raise AssertionError('pure contract attempted to import queue owner')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
importlib.import_module(sys.argv[1])
assert 'mcs_requests' not in sys.modules
""", module)
