"""v4 vitals guard accepts a full-width decimal point (synthetic)."""
import pytest

import extract_llm


@pytest.mark.parametrize('body', ['体温３６．５℃', '体温36.5℃'])
def test_full_width_decimal_vital_kept(body):
    out = extract_llm._validate({'vitals': {'bt': 36.5}, 'summary': 's'}, body)
    assert out.get('vitals') == {'bt': 36.5}
