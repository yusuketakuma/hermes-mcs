"""Every time field used by relation ordering must reach its independent verifier."""
from copy import deepcopy

import semantic_audit
import semantic_relations
from semantic_testkit import v2_fact


def test_relation_ordering_valid_time_is_visible_to_the_fact_auditor():
    source = v2_fact("synthetic-before", statement="合成薬剤Aの開始", action="start",
                     event_time="2026-10-21", valid_time="2026-10-20")
    newer = v2_fact("synthetic-after", statement="合成薬剤Aの中止", action="stop",
                    event_time="2026-10-21T09:00:00+09:00", valid_time="2026-10-22")
    # The newly enabled fallback axis affects a directed clinical relation.
    result = semantic_relations.classify_pair(source, newer)
    assert result["type"] == "EXPLICIT_SUPERSESSION"
    before = deepcopy(newer)
    before["valid_time"] = "2026-10-01"
    assert semantic_relations.classify_pair(source, before)["left_fact_id"] == before["fact_id"]
    assert semantic_audit._fact_audit_target(newer) != semantic_audit._fact_audit_target(before)
    assert "valid_time:2026-10-22" in semantic_audit._fact_audit_target(newer)
