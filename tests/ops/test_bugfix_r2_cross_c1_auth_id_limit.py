"""Regression: /2 envelopes must refuse an auth_id that withdraw/1 cannot carry."""
import pytest

import c1_envelopes as envs
from c1_contract import C1ContractError
from test_c1_envelopes import _build, _records, _resign


def test_builder_and_validator_share_the_withdraw_auth_id_limit():
    assert len(_build(_records(), auth_id="a" * envs.AUTH_ID_MAX)) == 1
    with pytest.raises(C1ContractError, match="envelope_metadata_invalid"):
        _build(_records(), auth_id="a" * (envs.AUTH_ID_MAX + 1))
    envelope = _build(_records())[0]
    envelope["auth_id"] = "a" * (envs.AUTH_ID_MAX + 1)
    _resign(envelope)
    with pytest.raises(C1ContractError, match="envelope_metadata_invalid"):
        envs.validate_envelope(envelope)
