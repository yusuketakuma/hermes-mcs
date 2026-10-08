"""Synthetic clinical meaning and source coverage agree across extraction engines."""
import json
from pathlib import Path

import pytest

import clinical_chunking
import extract_llm
import semantic_extraction


@pytest.mark.parametrize("case_id", ["dense-multi-drug-16plus", "family-medication-no-link",
                                     "negation-uncertainty-preserved"])
def test_source_plan_and_fact_quotes_agree_across_engines(case_id):
    corpus = json.loads((Path(__file__).resolve().parents[2] / "evaluation" /
                         "semantic_completeness_cases.json").read_text())
    case = next(case for case in corpus["cases"] if case["id"] == case_id)
    for message in case["messages"]:
        body = message["body"]
        pieces = clinical_chunking.plan_chunks(body)
        legacy = [extract_llm._chunk_piece(body, 3000, index, {})
                  for index in range(len(pieces))]
        manifest = semantic_extraction.build_manifest(body, "synthetic-source")
        assert legacy == pieces == [chunk["text"] for chunk in manifest["chunks"]]
        assert "".join(pieces) == body
        for fact in case["expect"]["facts"]:
            quote = fact.get("evidence_quote")
            if fact.get("message_id") == message["message_id"] and quote in body:
                assert any(quote in piece for piece in pieces), fact["id"]
        if case_id == "dense-multi-drug-16plus":
            assert len(pieces) > 1
