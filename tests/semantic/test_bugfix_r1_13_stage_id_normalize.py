import semantic_evaluation as evaluation


def test_stage_ids_and_unresolved_refs_normalized_like_fact_ids():
    record = {
        "label": {"facts": [{"fact_id": 1, "mandatory": True},
                            {"fact_id": "f2", "mandatory": True}]},
        "candidate": {
            "facts": [{"fact_id": "f2"}],
            "unresolved": [{"fact_ref": 1}],
            "verified_fact_ids": ["f2"],
            "rendered_fact_ids": ["f2 "],
            "delivered_fact_ids": ["f2 "],
        },
    }
    counts = evaluation._case_counts(record)
    assert counts["rendered"] == [1, 2]
    assert counts["delivered"] == [1, 2]
    assert counts["silent_drop"] == [0, 2]
    assert counts["lifecycle"]["complete"] == 1
