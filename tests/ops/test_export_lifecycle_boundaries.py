"""Manual export owners remain independent of CLI and receiver initialization."""
import pytest

from ops_testkit import bounded_python


@pytest.mark.parametrize("module,blocked", [
    ("export_contract", "ext_contract,export_delivery"),
    ("export_delivery", "ext_contract,c1_receiver"),
])
def test_export_owner_import_does_not_initialize_cli_or_receiver(tmp_path, module, blocked):
    bounded_python(tmp_path, """
import builtins
import importlib
original = builtins.__import__
blocked = set(sys.argv[2].split(','))
def guarded(name, *args, **kwargs):
    if name in blocked:
        raise AssertionError('unexpected export ownership backedge: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
importlib.import_module(sys.argv[1])
""", module, blocked)
