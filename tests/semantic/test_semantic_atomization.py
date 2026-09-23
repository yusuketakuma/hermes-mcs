"""Source atomization and chunk-ownership contract tests.

The manifest must cover every codepoint exactly once, keep stable source
order, disambiguate duplicate quotes by owning chunk, preserve heading
dependencies, and support durable prefix reuse after interruption.
"""
import json

import pytest

import semantic_extraction as extraction
import semantic_facts as sf


FP = "sf_test"


def _member(body, **kw):
    member = {"project_id": 1, "message_id": "m1", "revision": "r1",
              "body_original": body, "sender_id": "s1",
              "sender_name": "合成スタッフ", "thread_id": "t1"}
    member.update(kw)
    return member


def test_atoms_cover_source_exactly_once_in_order():
    body = ("【バイタル】\n体温 38.2度\nSpO2 93%\n\n"
            "・アムロジピン5mg朝食後\n・メトホルミン中止\n\n"
            "膝の痛みが継続。湿布を希望。\n")
    manifest = extraction.build_manifest(body, FP, chunk_size=60)
    atoms = manifest["atoms"]
    assert atoms
    cursor = 0
    for atom in atoms:
        assert atom["start"] == cursor
        assert atom["end"] > atom["start"]
        cursor = atom["end"]
    assert cursor == len(body)
    kinds = [a["kind"] for a in atoms]
    assert "heading" in kinds and "list_item" in kinds
    assert kinds[0] == "heading"


def test_leading_whitespace_is_owned_by_first_atom():
    # FIX-SE3: a source starting with blank lines previously orphaned
    # the whitespace range and crashed atom_coverage_incomplete.
    for body in ("\n先頭に空行", "\n\n連続する空行の後に本文",
                 " \n \n 空白混在", "\t\n　\nテキスト"):
        manifest = extraction.build_manifest(body, FP, chunk_size=30)
        atoms = manifest["atoms"]
        assert atoms[0]["start"] == 0
        cursor = 0
        for atom in atoms:
            assert atom["start"] == cursor
            cursor = atom["end"]
        assert cursor == len(body)


def test_core_chunks_own_each_atom_exactly_once():
    body = "行" * 20 + "\n" + ("・項目あ\n・項目い\n・項目う\n" * 30)
    manifest = extraction.build_manifest(body, FP, chunk_size=50)
    owned = [aid for c in manifest["chunks"] for aid in c["core_atom_ids"]]
    assert sorted(owned) == sorted(a["atom_id"] for a in manifest["atoms"])
    assert len(owned) == len(set(owned))
    # Core ranges are contiguous per chunk and cover [0, len).
    by_id = {a["atom_id"]: a for a in manifest["atoms"]}
    cursor = 0
    for chunk in manifest["chunks"]:
        core = [by_id[a] for a in chunk["core_atom_ids"]]
        assert core[0]["start"] == cursor
        for left, right in zip(core, core[1:]):
            assert left["end"] == right["start"]
        cursor = core[-1]["end"]
        assert chunk["text"] == body[chunk["start"]:chunk["end"]]
    assert cursor == len(body)


def test_heading_dependency_and_context_atoms():
    body = "【処方】\n・アムロジピン開始\n【症状】\n・咳悪化\n"
    manifest = extraction.build_manifest(body, FP, chunk_size=200)
    by_id = {a["atom_id"]: a for a in manifest["atoms"]}
    start_item = next(a for a in manifest["atoms"]
                      if "アムロジピン" in body[a["start"]:a["end"]])
    heading = manifest["atoms"][0]
    assert start_item["dependency_atom_ids"] == [heading["atom_id"]]
    assert start_item["section_path"] == ["【処方】"]
    # Small chunk boundary exposes neighbour atoms as context only.
    small = extraction.build_manifest(body, FP, chunk_size=24)
    for chunk in small["chunks"]:
        for ref in chunk["context_atom_ids"]:
            assert ref not in chunk["core_atom_ids"]
            assert ref in by_id


def test_oversized_atom_splits_safely():
    sentence = "これは文です。" * 200      # ~1400 codepoints, no newline
    manifest = extraction.build_manifest(sentence, FP, chunk_size=100)
    atoms = manifest["atoms"]
    assert len(atoms) > 1
    assert all(a["end"] - a["start"] <= 100 for a in atoms)
    assert "".join(sentence[a["start"]:a["end"]] for a in atoms) == sentence
    # First split ends at a sentence boundary.
    assert sentence[atoms[0]["end"] - 1] == "。"


def test_evidence_atom_resolves_inside_owning_chunk():
    body = "A" * 30 + "クオートX" + "B" * 30 + "\n" + "クオートX" + "C" * 30
    manifest = extraction.build_manifest(body, FP, chunk_size=40)
    target = body.index("クオートX")
    atom = extraction._evidence_atom(manifest, target, target + 5)
    assert atom is not None
    by_id = {a["atom_id"]: a for a in manifest["atoms"]}
    assert by_id[atom]["start"] <= target < by_id[atom]["end"]
    # A duplicate quote in a later chunk resolves to a different atom.
    dup = body.index("クオートX", target + 1)
    assert extraction._evidence_atom(manifest, dup, dup + 5) != atom


def _llm_echo(facts):
    def llm(_prompt):
        return json.dumps({"facts": facts}, ensure_ascii=False)
    return llm


def test_extraction_uses_manifest_chunks_and_maps_atom():
    body = "アムロジピンを開始。" + "あ" * 80 + "\n" + "アムロジピンを開始。"

    def llm(_prompt):
        return json.dumps(
            {"facts": [{"statement": "s", "kind": "medication",
                        "evidence_quote": "アムロジピンを開始"}]},
            ensure_ascii=False)

    member = _member(body)
    result = extraction.extract_facts_resumable(
        llm, member, chunk_size=60)
    assert result["complete"]
    assert result["chunks_total"] >= 2
    evidenced = [f["_evidence"] for f in result["facts"]
                 if f["_evidence"]]
    # The same quote text appears twice; whole-source locate is
    # ambiguous, but each owning chunk resolves its own occurrence —
    # two distinct atoms at distinct absolute offsets.
    assert len(evidenced) == 2
    spans = sorted((e["start_codepoint"], e["end_codepoint"])
                   for e in evidenced)
    assert spans == [(0, 9), (91, 100)]
    atom_ids = {e["atom_id"] for e in evidenced}
    assert len(atom_ids) == 2 and None not in atom_ids
    # A chunk whose text lacks the quote keeps the fact unverified —
    # out-of-chunk text is never borrowed as evidence.
    unverified = [f for f in result["facts"] if f["_evidence"] is None]
    assert all(f["validation_status"] == "unverified"
               for f in unverified)


def test_durable_prefix_reuse_after_interruption():
    body = "一段落目です。" + "あ" * 50 + "\n" + "二段落目です。" + "い" * 50
    completed = []

    def llm_first(prompt):
        completed.append(len(completed))
        if len(completed) == 2:
            raise RuntimeError("model down")
        return json.dumps({"facts": []}, ensure_ascii=False)

    class FakeLedger:
        def __init__(self):
            self.rows = []

        def artifacts(self, kind, project_id=None, message_id=None):
            return [r for r in self.rows if r["kind"] == kind]

        def artifact_add(self, kind, content, project_id=None,
                         message_id=None, model=None, meta=None):
            self.rows.append({"kind": kind, "content": content,
                              "meta": json.dumps(meta or {})})

    ledger = FakeLedger()
    member = _member(body)
    first = extraction.extract_facts_resumable(
        llm_first, member, ledger=ledger,
        source_fingerprint=FP, chunk_size=40)
    assert not first["complete"]
    assert first["chunks_completed"] >= 1

    second_calls = []

    def llm_second(prompt):
        second_calls.append(prompt)
        return json.dumps({"facts": []}, ensure_ascii=False)

    second = extraction.extract_facts_resumable(
        llm_second, member, ledger=ledger,
        source_fingerprint=FP, chunk_size=40)
    assert second["complete"]
    assert second["reused_chunks"] == first["completed_chunks"]
    # Cached prefix was not re-requested.
    assert len(second_calls) < first["chunks_total"]
    # The manifest artifact was persisted exactly once.
    manifest_rows = [r for r in ledger.rows
                     if r["kind"] == "semantic_source_manifest"]
    assert len(manifest_rows) == 1
    doc = json.loads(manifest_rows[0]["content"])
    assert doc["version"] == "semantic-source-manifest/v1"
    expected = 0
    for atom in doc["atoms"]:
        assert atom["start"] == expected
        expected = atom["end"]
    assert expected == len(body)


def test_malformed_chunker_or_unresolved_dependency_fails_closed():
    body = "本文です。"
    with pytest.raises(ValueError):
        extraction.build_manifest(body, FP, chunk_size=0)
    # A manifest with a gap must not validate as a facts doc.
    manifest = extraction.build_manifest(body, FP, chunk_size=10)
    doc = {
        "version": sf.CONTRACT_VERSION,
        "source": {
            "message_id": "m1", "revision": "r1", "content_hash": "h",
            "body_codepoints": len(body), "content_quality": "full",
            "attachments_complete": True,
            "source_fingerprint": FP,
        },
        "atoms": manifest["atoms"][:-1],   # drop last atom -> gap
        "chunks": manifest["chunks"],
        "obligations": [], "evidence": [], "facts": [], "relations": [],
        "coverage": {"category_counts": {}, "open_obligation_ids": [],
                     "limitations": [], "status": "complete"},
    }
    with pytest.raises(sf.ContractError):
        sf.validate_facts_doc(doc)
