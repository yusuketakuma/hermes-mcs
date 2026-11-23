"""Each cloud provider answers the shared questions from its own definition module."""
import pytest

import anthropic_client
import cloud_policy
import openai_client
import provider_registry
import provider_settings


def test_every_provider_module_defines_the_shared_names():
    names = ("MODEL", "KEY_ENV", "DISPLAY_NAME", "MAX_TOKENS_FIELD", "CONFIG_MODELS", "HIGH_INPUT_PRICE",
             "ORIGINAL_CONTRACT", "CURRENT_PRICE_VERSIONS", "PRICE_SNAPSHOTS", "PROFILE_DEFINITIONS",
             "PROFILE_PURPOSES", "DEFAULT_PROFILES", "VALID_MODEL_EFFORTS", "ATTEMPT_TIMEOUTS",
             "provider_contract", "reservation_cost", "usage_cost", "original_contract_valid",
             "original_selection_valid", "select_profile", "request_contract_error", "legacy_request_effort",
             "client_class")
    for module in provider_registry.PROVIDER_MODULES.values():
        assert [name for name in names if not hasattr(module, name)] == []


def test_settings_files_and_registry_name_the_same_providers():
    assert provider_settings.CLOUD_PROVIDERS == provider_registry.CLOUD_PROVIDERS
    assert provider_settings.LEGACY_PROFILES_PROVIDER == provider_registry.LEGACY_PROFILES_PROVIDER
    assert provider_registry.INFERENCE_PROVIDERS == ("local", "anthropic", "openai")
    assert provider_registry.DEFAULT_PROVIDER == "anthropic"


def test_names_and_lookups():
    assert provider_registry.display_name("openai") == "OpenAI"
    assert provider_registry.display_name("jev", "外部AI") == "外部AI"
    assert provider_registry.provider_for_model(openai_client.MODEL) == "openai"
    assert provider_registry.provider_for_model(anthropic_client.MODEL) == "anthropic"
    assert provider_registry.provider_for_model("synthetic-model") is None
    assert provider_registry.original_effort("openai") == "low"
    assert provider_registry.original_effort("anthropic") is None
    assert provider_registry.original_effort("local") is None


@pytest.mark.parametrize(("configured", "chosen", "expected"), [
    (openai_client.MODEL, {}, (None, openai_client.MODEL, "low")),
    (openai_client.SOL_MODEL, {}, ("sol-medium", openai_client.SOL_MODEL, "medium")),
    (None, {}, ("luna-medium", openai_client.MODEL, "medium")),
    (openai_client.MODEL, {"backlog": "sol-high"}, ("sol-high", openai_client.SOL_MODEL, "high")),
])
def test_openai_profile_choice(configured, chosen, expected):
    assert openai_client.select_profile("backlog", chosen, configured) == expected


def test_anthropic_profile_choice_ignores_the_configured_model():
    assert anthropic_client.select_profile("backlog", {}, openai_client.MODEL) == (
        "haiku-medium", anthropic_client.MODEL, "medium")
    assert anthropic_client.select_profile("backlog", {"backlog": "sonnet-high"}, None) == (
        "sonnet-high", anthropic_client.SONNET_MODEL, "high")


def test_request_contracts():
    selection = {"effort": "medium"}
    openai_body = {"reasoning": {"effort": "medium"}, "service_tier": "default", "store": False,
                   "prompt_cache_options": {"ttl": "30m"}}
    assert openai_client.request_contract_error(openai_body, selection) is None
    assert openai_client.request_contract_error({**openai_body, "store": True}, None) == "openai_price_contract_invalid"
    assert openai_client.request_contract_error(openai_body, {"effort": "high"}) == "cloud_profile_invalid"
    assert anthropic_client.request_contract_error({}, None) is None
    assert anthropic_client.request_contract_error({"output_config": {"effort": "low"}}, {"effort": None}) is None
    assert anthropic_client.request_contract_error({"output_config": {"effort": "low"}}, selection) == "cloud_profile_invalid"


def test_original_contracts():
    assert anthropic_client.original_contract_valid(anthropic_client.MODEL, None)
    assert not anthropic_client.original_contract_valid(anthropic_client.SONNET_MODEL, None)
    assert openai_client.original_contract_valid(*openai_client.ORIGINAL_CONTRACT)
    assert not openai_client.original_contract_valid(openai_client.SOL_MODEL, "medium")
    limits = cloud_policy.PROFILE_MAX_TOKENS
    assert openai_client.original_selection_valid(openai_client.MODEL, 8192, default_model=None, purpose_limits=limits)
    assert not openai_client.original_selection_valid(openai_client.MODEL, 100, default_model=None, purpose_limits=limits)
    assert anthropic_client.original_selection_valid("m", None, default_model="m", purpose_limits=limits)
    assert not anthropic_client.original_selection_valid("m", None, default_model="n", purpose_limits=limits)


def test_purpose_limits_are_the_profile_limits():
    assert cloud_policy.PROFILE_MAX_TOKENS == tuple(cloud_policy.PURPOSE_MAX_TOKENS.values()) == (8192, 16384, 32768)
