"""Map a cloud provider name to its single definition module.

Everything provider-specific lives in that provider's one file
(``anthropic_client`` or ``openai_client``), under the same names:
``MODEL``, ``KEY_ENV``, ``DISPLAY_NAME``, ``MAX_TOKENS_FIELD``, ``CONFIG_MODELS``, ``HIGH_INPUT_PRICE``,
``ORIGINAL_CONTRACT``, ``CURRENT_PRICE_VERSIONS``, ``PRICE_SNAPSHOTS``,
``PROFILE_DEFINITIONS``, ``PROFILE_PURPOSES``, ``DEFAULT_PROFILES``,
``VALID_MODEL_EFFORTS``, ``ATTEMPT_TIMEOUTS``, ``provider_contract``, ``reservation_cost``, ``usage_cost``,
``original_contract_valid``, ``original_selection_valid``, ``select_profile``,
``request_contract_error`` and ``client_class``.
Shared code looks the provider up here instead of branching on its name. A provider
name stays only where it is data: stored record fields, configuration keys and CLI flags.
"""
from __future__ import annotations

from types import MappingProxyType

import anthropic_client
import openai_client

PROVIDER_MODULES = MappingProxyType({"anthropic": anthropic_client, "openai": openai_client})
# Every cloud provider, in the default priority order.
CLOUD_PROVIDERS = tuple(PROVIDER_MODULES)
# Every inference producer a stored proof may name: the local model, then each cloud provider.
INFERENCE_PROVIDERS = ("local", *CLOUD_PROVIDERS)
# The provider an invalid or "auto" setting falls back to.
DEFAULT_PROVIDER = CLOUD_PROVIDERS[0]
# The provider whose profiles the older ``cloud_llm.profiles`` key holds
# (``cloud_llm.provider_profiles`` replaced it).
LEGACY_PROFILES_PROVIDER = "openai"


def provider_module(provider):
    """The provider's definition module, or None for an unknown name."""
    return PROVIDER_MODULES.get(provider) if isinstance(provider, str) else None


def display_name(provider, default=None):
    """The provider's name for people, or ``default`` for an unknown name."""
    module = provider_module(provider)
    return module.DISPLAY_NAME if module is not None else default


def provider_for_model(model):
    """The provider whose default model is ``model``, or None."""
    return next((name for name, module in PROVIDER_MODULES.items() if module.MODEL == model), None)


def original_effort(provider):
    """The effort of the provider's original contract, which older records leave out (None if unknown)."""
    module = provider_module(provider)
    return module.ORIGINAL_CONTRACT[1] if module is not None else None
