"""Shared finite-number checks retain each field boundary's own result and error."""
import pytest

import operational_status
import project_metadata_contract as metadata
from mcs_contract import SchemaError


@pytest.mark.parametrize("value,observed", [(None, None), (-1, None), (0, 0), (1.5, 1.5)])
def test_numeric_domains_stay_distinct(value, observed):
    assert operational_status._number(value) == observed
    assert metadata._number(value) == value


@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf"), 10 ** 1000], ids=["bool", "text", "nan", "infinity", "huge_int"])
def test_invalid_numeric_types_keep_none_vs_schema_error(value):
    assert operational_status._number(value) is None
    with pytest.raises(SchemaError, match="project metadata: number invalid"):
        metadata._number(value)
