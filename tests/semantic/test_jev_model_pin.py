"""A Jev model pin an earlier release fixed keeps naming the current fixed model."""
import pytest

import provider_settings
import semantic_candidate as candidate
import semantic_jev as jev
from semantic_policy import semantic_config

OWNERS = {"synthetic-anthropic": "anthropic", "synthetic-openai": "openai"}


def test_current_model_is_not_retired():
    assert jev.JEV_MODEL not in provider_settings.JEV_RETIRED_PINS


@pytest.mark.parametrize("pin", sorted(provider_settings.JEV_RETIRED_PINS) + [jev.JEV_MODEL])
def test_retired_or_current_pin_is_accepted(pin):
    common, providers = provider_settings.split_legacy(
        {"semantic": {"model": pin, "until": None}}, OWNERS, jev_model=jev.JEV_MODEL)
    assert providers["jev"]["model"] == pin
    out, errors = semantic_config({"semantic": {"model": pin}})
    assert "config: semantic_model_invalid" not in errors
    assert out["model"] == jev.JEV_MODEL


@pytest.mark.parametrize("pin", ["synthetic-other", [], {}, None, 1])
def test_other_pin_is_rejected(pin):
    with pytest.raises(ValueError, match="provider_model_invalid"):
        provider_settings.split_legacy({"semantic": {"model": pin}}, OWNERS,
                                       jev_model=jev.JEV_MODEL)
    out, errors = semantic_config({"semantic": {"model": pin}})
    assert "config: semantic_model_invalid" in errors and out["mode"] == "off"


def test_model_pin_does_not_change_candidate_identity():
    base = candidate.candidate_fingerprint({})
    for pin in sorted(provider_settings.JEV_RETIRED_PINS) + [jev.JEV_MODEL]:
        assert candidate.candidate_fingerprint({"semantic": {"model": pin}}) == base
