"""The optional pharmacy reference flag must be an explicit boolean."""
import pytest

import mcs_setup

BASE = {"mcs_login_id": "synthetic", "notify_target": "slack:#synthetic"}


@pytest.mark.parametrize("block", [{}, {"patient_state_context": False}, {"patient_state_context": True}])
def test_optional_extract_config_is_recognized(block):
    assert mcs_setup.validate_config({**BASE, "extract": block}) == ([], [])


@pytest.mark.parametrize("value", [1, "true", None, [], {}])
def test_reference_flag_rejects_implicit_boolean_conversion(value):
    errors, _ = mcs_setup.validate_config({**BASE, "extract": {"patient_state_context": value}})
    assert "extract.patient_state_context: true（有効）かfalse（無効）を指定してください" in errors
