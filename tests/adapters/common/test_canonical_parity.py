"""The adapter's import-isolated copies of core hashing/version tables stay identical (synthetic)."""
import pytest

import mcs_util
import notify_scope
from adapters.common import envelopes

VALUES = [
    {"b": 1, "a": {"z": [3, 2, 1], "y": None}},
    {"本文": "合成テスト 患者なし", "数値": 36.5, "flag": True},
    [1, 2.25, "x", None, {"k": []}],
    "plain",
    0,
]


def test_envelope_versions_mirror_notify_scope():
    assert envelopes._VERSION == notify_scope.TRANSPORT_VERSIONS


@pytest.mark.parametrize("value", VALUES)
def test_canonical_and_payload_hash_match_core(value):
    assert envelopes.canonical(value) == mcs_util.canonical(value)
    assert envelopes.payload_hash(value) == mcs_util.payload_hash(value)


def test_nan_rejected_by_both():
    for fn in (envelopes.canonical, mcs_util.canonical):
        with pytest.raises(ValueError):
            fn({"v": float("nan")})
