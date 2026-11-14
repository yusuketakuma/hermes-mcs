"""Map a cloud provider name to its single definition module.

Everything provider-specific lives in that provider's one file
(``anthropic_client`` or ``openai_client``), under the same names:
``MODEL``, ``ORIGINAL_CONTRACT``, ``CURRENT_PRICE_VERSIONS``, ``PRICE_SNAPSHOTS``,
``PROFILE_DEFINITIONS``, ``PROFILE_PURPOSES``, ``DEFAULT_PROFILES``,
``VALID_MODEL_EFFORTS``, ``reservation_cost``, ``usage_cost`` and ``CLIENT``.
Shared code looks the provider up here instead of branching on its name.
"""
from __future__ import annotations

from types import MappingProxyType

import anthropic_client
import openai_client

PROVIDER_MODULES = MappingProxyType({"anthropic": anthropic_client, "openai": openai_client})


def provider_module(provider):
    """The provider's definition module, or None for an unknown name."""
    return PROVIDER_MODULES.get(provider) if isinstance(provider, str) else None
