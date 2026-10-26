"""Missing checks and unfinished leaves never prove inference-v1 completeness."""
import pytest

import extract_quality as quality
import inference_version as inference


OK={"state":"hint_gaps_resolved","remaining_hint_candidates":0}


@pytest.mark.parametrize("integrity,expected", [
    (None,False), ({},False), ({"completeness":{}},False),
    ({"completeness":{"0":OK}},False),
    ({"completeness_chunks":True,"completeness":{"0":OK}},False),
    ({"completeness_chunks":1,"completeness":{"0":OK}},True),
    ({"completeness_chunks":2,"completeness":{"0":OK}},False),
    ({"completeness_chunks":2,"completeness":{"0":OK,"1":OK}},True),
    ({"completeness_chunks":2,"completeness":{"0":OK,"1":{"state":"pending","remaining_hint_candidates":1}}},False),
    ({"completeness_chunks":1,"completeness":{"0":{"state":"hint_gaps_resolved","remaining_hint_candidates":False}}},False),
])
def test_current_inference_requires_every_leaf_check(integrity,expected):
    assert quality.completeness_resolved({inference.VERSION_KEY:inference.CURRENT_VERSION,
                                          "integrity":integrity}) is expected


@pytest.mark.parametrize("meta,expected", [({},True),
    ({inference.VERSION_KEY:inference.LEGACY_VERSION},True),
    ({inference.VERSION_KEY:None},False),
    ({inference.VERSION_KEY:"99.0"},False),
    ({inference.VERSION_KEY:inference.LEGACY_VERSION,"integrity":{"completeness":{"0":{"state":"pending"}}}},False),
])
def test_legacy_absence_is_not_relabelled_as_a_current_completed_check(meta,expected):
    assert quality.completeness_resolved(meta) is expected


def test_structured_event_and_legacy_projection_are_one_fact():
    doc={"events":["discharge"],"event_records":[{"kind":"discharge","subject":"patient",
         "status":"current","negated":False,"evidence":"本人は退院しました。","span":{"start":0,"end":10}}]}
    facts=quality.facts(doc)
    assert len(facts)==1 and next(iter(facts))[0]=='event_records'
