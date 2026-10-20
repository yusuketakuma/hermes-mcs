"""Shared stored-JSON parsing preserves metadata observation states."""
import json

import pytest

from message_observations import decode_metadata


@pytest.mark.parametrize("raw", [None, 17, "{", "null", "[]", '"text"',
                                "[" * 1500 + "]" * 1500])
def test_invalid_metadata_object_keeps_fields_invalid(raw):
    result = decode_metadata((raw, 10, None))
    assert [result[key] for key in (
        "reactions_status", "mentions_status", "bookmark_status", "pin_status",
    )] == ["invalid"] * 4
    assert result["checked_at"] == 10
    assert result["reactions_observed_at"] is None
    assert result["last_error"] is None


def test_empty_metadata_object_keeps_fields_unfetched():
    result = decode_metadata(("{}", 10, None))
    assert [result[key] for key in (
        "reactions_status", "mentions_status", "bookmark_status", "pin_status",
    )] == ["not_fetched"] * 4


def test_valid_empty_reactions_and_invalid_optional_field_stay_distinct():
    raw = json.dumps({"reactions": {"value": [], "observed_at": 9},
                      "is_bookmarked": {"value": True, "observed_at": 9}})
    result = decode_metadata((raw, 10, "is_bookmarked_invalid"))
    assert result["reactions_status"] == "observed"
    assert result["reactions"] == []
    assert result["reactions_observed_at"] == 9
    assert result["bookmark_status"] == "invalid"
    assert result["is_bookmarked"] is None
    assert result["last_error"] is None
