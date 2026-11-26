"""Restore extract/llm module state after every test.

The extraction suite drives ``run_pending`` in-process, so every module-level
mutable the workers consult — slot overrides, threading.local call notes,
format-probe caches, lane estimates, admission memory — is shared across
tests in pytest's fixed collection order.  Several tests mutate that state
directly (``extract_llm._CALL_NOTES.defer_reason = ...``, writes behind the
``--lane`` wiring, ``extract_llm_provider._FMT_MODE = ...``) which monkeypatch
never restores, and a polluted slot override or defer reason makes later
workers process zero rows within their deadline.  This fixture snapshots the
mutable private state of the extraction/llm modules before each test and
restores it afterwards, so no test can observe another test's residue
regardless of how the mutation happened.
"""
import copy
import threading

import pytest

_MODULES = (
    "extract_llm extract_llm_admission extract_llm_call_context extract_llm_carry "
    "extract_llm_context extract_llm_engine extract_llm_errors extract_llm_historical "
    "extract_llm_local_rebuild extract_llm_prepare extract_llm_progress extract_llm_prompts "
    "extract_llm_proofs extract_llm_provider extract_llm_qc extract_llm_resident "
    "extract_llm_runner extract_llm_selection extract_llm_source_contract extract_llm_store "
    "extract_quality extract_schema extract_validation extraction_context "
    "extraction_medications extraction_provenance cloud_backlog cloud_history_scope "
    "cloud_inference cloud_policy cloud_usage_accounting cloud_send_admission "
    "llm_admission llm_availability llm_slot_schedule cloud_client llm_retry local_llm"
).split()


def _state_modules():
    import importlib
    mods = []
    for name in _MODULES:
        try:
            mods.append(importlib.import_module(name))
        except Exception:
            continue
    return mods


def _snap_value(value):
    if isinstance(value, threading.local):
        return ("threadlocal", (value, {k: _snap_value(v) for k, v in vars(value).items()}))
    if isinstance(value, (dict, list, set)):
        # In-place mutation vectors need a real copy; everything else —
        # immutable scalars, identity sentinels like object(), locks,
        # compiled patterns — must come back as the same reference (a copied
        # sentinel would break ``is`` checks).
        try:
            return ("copy", (value, copy.deepcopy(value)))
        except Exception:
            pass
    return ("ref", value)


def _restore_value(obj, key, entry):
    kind, value = entry
    if kind == "threadlocal":
        original, fields = value
        for field in vars(original).keys() - fields.keys():
            delattr(original, field)
        for field, entry in fields.items():
            _restore_value(original, field, entry)
        setattr(obj, key, original)
    elif kind == "copy":
        # Importers share these objects. Restore their contents and original binding.
        original, contents = value
        if isinstance(original, list):
            original[:] = contents
        else:
            original.clear()
            original.update(contents)
        setattr(obj, key, original)
    else:
        try:
            setattr(obj, key, value)
        except Exception:
            pass


def _snapshot(mods):
    import types as _types
    snap = {}
    present = set()
    for mod in mods:
        for key, value in vars(mod).items():
            if not key.startswith("_") or key.startswith("__"):
                continue
            present.add((mod, key))
            if isinstance(value, threading.local):
                snap[(mod, key)] = _snap_value(value)
            elif isinstance(value, (_types.ModuleType, _types.FunctionType,
                                    _types.BuiltinFunctionType, type)) or callable(value):
                # Functions/classes/modules are restored by reference: what
                # matters is undoing direct rebinding, not copying them.
                snap[(mod, key)] = ("ref", value)
            else:
                snap[(mod, key)] = _snap_value(value)
    return snap, present


@pytest.fixture(autouse=True)
def _restore_extract_llm_module_state():
    mods = _state_modules()
    snap, present = _snapshot(mods)
    yield
    for (mod, key), entry in snap.items():
        _restore_value(mod, key, entry)
    # Drop private attributes a test added that were absent at snapshot time.
    for mod in mods:
        for key in list(vars(mod)):
            if key.startswith("_") and not key.startswith("__") and (mod, key) not in present:
                try:
                    delattr(mod, key)
                except Exception:
                    pass
