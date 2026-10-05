"""Fully fictional dictionaries and isolated source-bound derivation contracts."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from difflib import SequenceMatcher
import hashlib
import json
from operator import setitem
from typing import TypedDict

import pytest

import drug_map
import rollup
from extract_testkit import _hash, _ledger, _message


class _FictionalEntryRequired(TypedDict):
    id: str
    kind: str
    display: str
    aliases: list[str]
    codes: dict[str, str]
    forms: list[str]


class FictionalEntry(_FictionalEntryRequired, total=False):
    source_codes: list[dict[str, str | None]]


class FictionalDocument(TypedDict):
    schema: str
    dict_id: str
    source: dict[str, str]
    entries: list[FictionalEntry]


DOCUMENT: FictionalDocument = {
    "schema": "mcs-drug-map/1", "dict_id": "fictional-v1",
    "source": {"name": "fictional test dictionary",
               "url": "https://example.invalid/fictional",
               "terms_checked_on": "2026-10-04", "approved_by": "fictional-test"},
    "entries": [
        {"id": "fiction-i1", "kind": "ingredient", "display": "架空成分甲",
         "aliases": ["キラナ", "キラーナ", "別キラナ", "共通架空"],
         "codes": {"yj": "fictional-product", "ssk": "fictional-package"},
         "forms": ["錠", "液"]},
        {"id": "fiction-i2", "kind": "ingredient", "display": "架空成分乙",
         "aliases": ["共通架空", "キラナタール"], "codes": {}, "forms": []},
        {"id": "fiction-class", "kind": "class", "display": "架空薬群",
         "aliases": ["架空群薬"], "codes": {}, "forms": []},
    ],
}


def test_fictional_entry_keeps_required_and_optional_keys():
    assert FictionalEntry.__required_keys__ == {"id", "kind", "display", "aliases", "codes", "forms"}
    assert FictionalEntry.__optional_keys__ == {"source_codes"}


def _dictionary(tmp_path, document=None):
    raw = json.dumps(document or DOCUMENT, ensure_ascii=False).encode()
    path = tmp_path / "fictional-drug-map.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    dictionary = drug_map.load(path, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert dictionary is not None
    return dictionary


def _seed(store, mid=1, names=("キラナ",), *, kind="extract_llm",
          action="start", posted="2026-09-19T00:00:00+09:00"):
    store.save_messages([_message(mid=mid, body="entirely fictional source",
                                  posted_at=posted)])
    key = "medications" if kind == "extract_v1" else "meds"
    meds = [{"name": n, "action": action, "subject": "patient",
             "status": "current", "negated": False, "unverified": False,
             "evidence": "fictional quotation only"} for n in names]
    return store.artifact_add(
        kind, json.dumps({key: meds}), project_id=1, message_id=mid,
        meta={"hash": _hash(store, mid), "engine_version": 4})


@pytest.fixture
def store(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    yield ledger
    ledger.close()


@pytest.mark.parametrize("name,method", [
    ("キラナ", "alias"), ("ｷﾗﾅ", "alias"), ("きらな", "alias"),
    (" キ・ラ ナ ", "alias"), ("キラｰナ", "alias"), ("キラ-ナ", "alias"),
    ("キラナ錠5mg", "stem"), ("きらなＯＤ錠５ｍｇ「架空社」", "stem"),
    ("キラナ(錠)5mg", "stem"), ("キラナカプセル10mg", "stem"),
    ("キラナDS5mg", "stem"), ("キラナ注5ml", "stem"),
])
def test_exact_alias_and_stem_table(tmp_path, name, method):
    dictionary = _dictionary(tmp_path)
    ref = dictionary.resolve(name)
    assert ref["status"] == "resolved"
    assert ref["cands"] == [{
        "system": "local", "code": "fiction-i1", "display": "架空成分甲",
        "kind": "ingredient", "method": method, "candidate": True}]


def test_collisions_classes_generics_and_lasa_are_not_ingredients(tmp_path):
    dictionary = _dictionary(tmp_path)
    assert dictionary.resolve("共通架空")["status"] == "ambiguous"
    assert len(dictionary.resolve("共通架空")["cands"]) == 2
    assert dictionary.resolve("架空群薬")["status"] == "generic"
    assert dictionary.resolve("処方薬") == {
        "i": 0, "name": "処方薬", "status": "generic", "cands": []}
    near = "キラナタールル"
    assert SequenceMatcher(None, near, "キラナタール").ratio() > 0.9
    assert dictionary.resolve(near)["cands"] == []
    assert dictionary.resolve(near)["status"] == "unresolved"
    assert dictionary.resolve("キラナ架空")["status"] == "unresolved"


def test_generic_word_cannot_be_resolved_by_ingredient_alias(tmp_path):
    document = deepcopy(DOCUMENT)
    document["entries"][0]["aliases"].append("降圧薬")
    assert _dictionary(tmp_path, document).resolve("降圧薬")["status"] == "generic"


def test_load_missing_pin_private_path_and_symlink(tmp_path):
    path = tmp_path / "fictional-drug-map.json"
    assert drug_map.load(path, expected_sha256="0" * 64) is None
    with pytest.raises(ValueError, match="path_or_pin"):
        drug_map.load("relative.json", expected_sha256="0" * 64)
    dictionary = _dictionary(tmp_path)
    with pytest.raises(ValueError, match="sha256"):
        drug_map.load(path, expected_sha256="0" * 64)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private_or_size"):
        drug_map.load(path, expected_sha256=dictionary.sha256)
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="drug_map_file"):
        drug_map.load(link, expected_sha256=dictionary.sha256)


def test_deep_dictionary_json_uses_safe_validation_error(tmp_path):
    raw = b"[" * 10000 + b"0" + b"]" * 10000
    assert len(raw) == 20001 and len(raw) < drug_map.MAX_BYTES
    with pytest.raises(RecursionError):
        json.loads(raw)
    path = tmp_path / "synthetic-deep-dictionary.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="^drug_map_json$"):
        drug_map.load(path, expected_sha256=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("field", ["schema", "provenance", "date", "id",
                                  "kind", "aliases", "forms", "codes"])
def test_invalid_dictionary_boundary(tmp_path, field):
    # Malformed JSON is intentionally outside the valid fixture's typed shape.
    document = json.loads(json.dumps(DOCUMENT))
    if field == "schema":
        document["schema"] = "unknown/1"
    elif field == "provenance":
        del document["source"]["url"]
    elif field == "date":
        document["source"]["terms_checked_on"] = "not-a-date"
    elif field == "id":
        document["entries"][1]["id"] = document["entries"][0]["id"]
    elif field == "kind":
        document["entries"][0]["kind"] = "product"
    else:
        document["entries"][0][field] = None
    with pytest.raises(ValueError, match="drug_map"):
        _dictionary(tmp_path, document)


def test_loader_has_byte_entry_and_alias_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(drug_map, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="size"):
        _dictionary(tmp_path)
    monkeypatch.setattr(drug_map, "MAX_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(drug_map, "MAX_ENTRIES", 1)
    with pytest.raises(ValueError, match="entries"):
        _dictionary(tmp_path)
    monkeypatch.setattr(drug_map, "MAX_ENTRIES", 10000)
    monkeypatch.setattr(drug_map, "MAX_ALIASES", 1)
    with pytest.raises(ValueError, match="entry"):
        _dictionary(tmp_path)


def test_loaded_approval_and_index_are_immutable(tmp_path):
    dictionary = _dictionary(tmp_path)
    with pytest.raises(FrozenInstanceError):
        setattr(dictionary.source, "approved_by", "")
    with pytest.raises(TypeError):
        setitem(dictionary.aliases, "tampered", ())
    with pytest.raises(FrozenInstanceError):
        setattr(dictionary.aliases[drug_map.fold("キラナ")][0], "id", "tampered")


def test_derive_is_pinned_idempotent_and_body_free(store, tmp_path):
    source_id = _seed(store)
    dictionary = _dictionary(tmp_path)
    assert drug_map.derive(store, dictionary) == {
        "status": "ok", "done": 1, "pids": [1]}
    row = store.artifacts(drug_map.KIND)[0]
    meta = json.loads(row["meta"])
    assert meta["source_artifact_id"] == source_id
    assert meta["source_kind"] == "extract_llm"
    assert meta["hash"] == _hash(store)
    assert meta["dict_sha256"] == dictionary.sha256
    assert meta["source"] == DOCUMENT["source"]
    assert meta["resolver_version"] == drug_map.RESOLVER_VERSION
    assert "quotation" not in row["content"] and "fictional source" not in row["content"]
    assert drug_map.derive(store, dictionary)["done"] == 0
    assert store.artifacts(drug_map.KIND)[0]["artifact_id"] == row["artifact_id"]
    assert drug_map.current_refs(store.db, 1)[0]["cands"][0]["code"] == "fiction-i1"


@pytest.mark.parametrize("field", ["content", "meta"])
def test_deep_annotation_json_is_unavailable_without_writes(store, tmp_path, field):
    _seed(store)
    drug_map.derive(store, _dictionary(tmp_path))
    assert drug_map.current_refs(store.db, 1)[0]["cands"][0]["code"] == "fiction-i1"
    deep = "[" * 10000 + "0" + "]" * 10000
    with pytest.raises(RecursionError):
        json.loads(deep)
    # The column is selected from this fixed parameter table, never an input.
    store.db.execute(f"UPDATE artifacts SET {field}=? WHERE kind=?",
                     (deep, drug_map.KIND))
    store.db.commit()
    before = tuple(store.db.iterdump())
    changes = store.db.total_changes
    assert drug_map.current_refs(store.db, 1) == []
    assert tuple(store.db.iterdump()) == before
    assert store.db.total_changes == changes


def test_deep_source_json_is_excluded_before_python_medication_decode(store, tmp_path):
    source_id = _seed(store)
    drug_map.derive(store, _dictionary(tmp_path))
    deep = '{"meds":[],"synthetic_unused":' + "[" * 10000 + "0" + "]" * 10000 + "}"
    store.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (deep, source_id))
    store.db.commit()
    assert store.db.execute("SELECT json_valid(?)", (deep,)).fetchone()[0] == 0
    before = tuple(store.db.iterdump())
    assert drug_map.current_refs(store.db, 1) == []
    assert tuple(store.db.iterdump()) == before


def test_sql_valid_source_at_python_depth_limit_keeps_read_only_refs_safe(store, tmp_path):
    source_id = _seed(store)
    drug_map.derive(store, _dictionary(tmp_path))
    original = store.db.execute("SELECT content FROM artifacts WHERE artifact_id=?",
                                (source_id,)).fetchone()[0]
    deep = original[:-1] + ',"synthetic_unused":' + "[" * 999 + "0" + "]" * 999 + "}"
    assert store.db.execute("SELECT json_valid(?)", (deep,)).fetchone()[0] == 1
    try:
        json.loads(deep)
    except RecursionError:
        readable = False
    else:
        readable = True
    meta = json.loads(store.artifacts(drug_map.KIND)[0]["meta"])
    meta["source_sha256"] = hashlib.sha256(deep.encode()).hexdigest()
    store.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (deep, source_id))
    store.db.execute("UPDATE artifacts SET meta=? WHERE kind=?",
                     (json.dumps(meta), drug_map.KIND))
    store.db.commit()
    before = tuple(store.db.iterdump())
    changes = store.db.total_changes
    refs = drug_map.current_refs(store.db, 1)
    if readable:
        assert refs[0]["cands"][0]["code"] == "fiction-i1"
    else:
        assert refs == []
    assert tuple(store.db.iterdump()) == before
    assert store.db.total_changes == changes


def test_sql_valid_source_at_python_depth_limit_retires_only_unreadable_refs(store, tmp_path):
    source_id = _seed(store)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    original = store.db.execute("SELECT content FROM artifacts WHERE artifact_id=?",
                                (source_id,)).fetchone()[0]
    deep = original[:-1] + ',"synthetic_unused":' + "[" * 999 + "0" + "]" * 999 + "}"
    assert store.db.execute("SELECT json_valid(?)", (deep,)).fetchone()[0] == 1
    try:
        json.loads(deep)
    except RecursionError:
        readable = False
    else:
        readable = True
    store.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (deep, source_id))
    store.db.commit()
    source_before = tuple(store.db.execute("SELECT * FROM artifacts WHERE kind<>?",
                                           (drug_map.KIND,)))
    messages_before = tuple(store.db.execute("SELECT * FROM messages"))
    assert drug_map.derive(store, dictionary) == {"status": "ok", "done": 1, "pids": [1]}
    refs = drug_map.current_refs(store.db, 1)
    if readable:
        assert refs[0]["cands"][0]["code"] == "fiction-i1"
    else:
        assert refs == [] and store.artifacts(drug_map.KIND) == []
    assert drug_map.derive(store, dictionary) == {"status": "ok", "done": 0, "pids": []}
    assert tuple(store.db.execute("SELECT * FROM artifacts WHERE kind<>?",
                                  (drug_map.KIND,))) == source_before
    assert tuple(store.db.execute("SELECT * FROM messages")) == messages_before


def test_unapproved_and_missing_dictionary_remove_production_refs(store, tmp_path):
    _seed(store)
    drug_map.derive(store, _dictionary(tmp_path))
    document = deepcopy(DOCUMENT)
    del document["source"]["approved_by"]
    dictionary = _dictionary(tmp_path, document)
    assert not dictionary.approved
    assert drug_map.derive(store, dictionary) == {
        "status": "unavailable", "done": 1, "pids": [1]}
    assert not store.artifacts(drug_map.KIND)
    assert drug_map.derive(store, dictionary, synthetic=True)["done"] == 1
    assert drug_map.current_refs(store.db, 1) == []
    assert drug_map.derive(store, None)["status"] == "unavailable"
    assert not store.artifacts(drug_map.KIND)


def test_dictionary_and_resolver_changes_replace_not_accumulate(store, tmp_path, monkeypatch):
    _seed(store)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    old_id = store.artifacts(drug_map.KIND)[0]["artifact_id"]
    document = deepcopy(DOCUMENT)
    document["dict_id"] = "fictional-v2"
    document["entries"][0]["id"] = "fiction-new"
    dictionary = _dictionary(tmp_path, document)
    assert drug_map.derive(store, dictionary)["done"] == 1
    assert drug_map.current_refs(store.db, 1)[0]["cands"][0]["code"] == "fiction-new"
    monkeypatch.setattr(drug_map, "RESOLVER_VERSION", "mcs-drug-ref/test-next")
    assert drug_map.current_refs(store.db, 1) == []
    assert drug_map.derive(store, dictionary)["done"] == 1
    rows = store.artifacts(drug_map.KIND)
    assert len(rows) == 1 and rows[0]["artifact_id"] != old_id
    assert json.loads(rows[0]["meta"])["dict_sha256"] == dictionary.sha256


@pytest.mark.parametrize("change", ["replace", "same_id_content", "hash", "deleted",
                                   "invalidated", "projection"])
def test_source_changes_hide_old_refs_before_rederive(store, tmp_path, change):
    kind = "canonical_projection" if change == "invalidated" else "extract_llm"
    sid = _seed(store, kind=kind)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    if change == "replace":
        _seed(store, names=("別キラナ",))
    elif change == "same_id_content":
        store.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                         (json.dumps({"meds": [{"name": "別キラナ"}]}), sid))
    elif change == "hash":
        store.save_messages([_message(body="changed fictional source")])
    elif change == "deleted":
        store.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
    elif change == "invalidated":
        store.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                         (json.dumps({"hash": _hash(store), "invalidated": True}), sid))
    else:
        _seed(store, names=("別キラナ",), kind="semantic_facts_v4")
    assert drug_map.current_refs(store.db, 1) == []
    assert drug_map.derive(store, dictionary)["done"] == 1
    refs = drug_map.current_refs(store.db, 1)
    if change in ("hash", "deleted", "invalidated"):
        assert refs == [] and not store.artifacts(drug_map.KIND)
    else:
        assert refs[0]["name"] == "別キラナ"
        assert len(store.artifacts(drug_map.KIND)) == 1


def test_rule_only_source_is_annotated_as_unverified_rollup(store, tmp_path):
    _seed(store, kind="extract_v1")
    drug_map.derive(store, _dictionary(tmp_path))
    assert drug_map.current_refs(store.db, 1)[0]["source_kind"] == "extract_v1"
    assert rollup.build_rollup(store, 1)["unverified_medications"][0]["ref"]["status"] == "resolved"


def test_aliases_stay_separate_and_stop_never_suppresses_sibling(store, tmp_path):
    _seed(store, names=("キラナ", "別キラナ"))
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    rows = rollup.build_rollup(store, 1)["medications"]
    assert [row["name"] for row in rows] == ["別キラナ", "キラナ"]
    assert all(row["ref"]["cands"][0]["code"] == "fiction-i1" for row in rows)
    _seed(store, mid=2, names=("キラナ",), action="stop",
          posted="2026-09-20T00:00:00+09:00")
    drug_map.derive(store, dictionary)
    assert [row["name"] for row in rollup.build_rollup(store, 1)["medications"]] == ["別キラナ"]


def test_deadline_commits_prefix_and_resumes_without_sleep(store, tmp_path, monkeypatch):
    _seed(store, mid=1)
    _seed(store, mid=2)
    dictionary = _dictionary(tmp_path)
    clock = {"now": 1.0}
    monkeypatch.setattr(drug_map.time, "monotonic", lambda: clock["now"])
    original = store.artifact_add_tx

    def add(*args, **kwargs):
        result = original(*args, **kwargs)
        clock["now"] = 10.0
        return result

    monkeypatch.setattr(store, "artifact_add_tx", add)
    assert drug_map.derive(store, dictionary, deadline=5.0)["done"] == 1
    assert not drug_map.current_refs(store.db, 2)
    monkeypatch.setattr(store, "artifact_add_tx", original)
    assert drug_map.derive(store, dictionary, deadline=20.0)["done"] == 1
    assert drug_map.derive(store, dictionary, deadline=20.0)["done"] == 0


def _source_document():
    document = deepcopy(DOCUMENT)
    document["schema"] = drug_map.SOURCE_SCHEMA
    document["entries"] = [
        {"id": "mhlw:medicine:900000001", "kind": "product", "display": "架空製品錠5mg",
         "aliases": ["架空製品"], "codes": {"medicine": "900000001"}, "forms": [],
         "source_codes": [{"medicine": "900000001", "drug_price": "DRG000000001",
                           "general_name": None}]},
        {"id": "mhlw:general:GEN000000001", "kind": "general_name", "display": "【般】架空5mg錠",
         "aliases": ["架空ブランド", "共通source"], "codes": {"general_name": "GEN000000001"},
         "forms": [], "source_codes": [{"medicine": "900000002", "drug_price": "DRG000000002",
                                       "general_name": "GEN000000001"}]},
    ]
    return document


def test_explicit_source_identity_is_exact_only_and_never_an_ingredient(tmp_path):
    # Given / When
    dictionary = _dictionary(tmp_path, _source_document())
    # Then
    product = dictionary.resolve("架空製品")
    assert product["cands"][0]["kind"] == "product"
    assert product["cands"][0]["code"] == "mhlw:medicine:900000001"
    assert dictionary.resolve("架空製品錠10mg")["status"] == "unresolved"
    assert dictionary.resolve("架空ブランド")["cands"][0]["kind"] == "general_name"
    assert dictionary.resolve("架空ブランド錠5mg")["status"] == "unresolved"


@pytest.mark.parametrize("damage", ["identity", "codes", "rows", "association", "legacy_schema"])
def test_source_identity_provenance_must_match_namespace_and_kind(tmp_path, damage):
    # Given
    document = _source_document()
    entry = document["entries"][1]
    if damage == "identity":
        entry["id"] = "invented-ingredient"
    elif damage == "codes":
        entry["codes"] = {"yj": "GEN000000001"}
    elif damage == "rows":
        entry["source_codes"] = []
    elif damage == "association":
        assert "source_codes" in entry
        entry["source_codes"][0]["general_name"] = "GEN000000002"
    else:
        document["schema"] = drug_map.SCHEMA
    # When / Then
    with pytest.raises(ValueError, match="drug_map"):
        _dictionary(tmp_path, document)


def test_source_identity_survives_actual_annotation_reader_without_reclassifying(store, tmp_path):
    # Given
    _seed(store, names=("架空製品", "架空ブランド"))
    dictionary = _dictionary(tmp_path, _source_document())
    # When
    drug_map.derive(store, dictionary)
    # Then
    refs = drug_map.current_refs(store.db, 1)
    assert [ref["cands"][0]["kind"] for ref in refs] == ["product", "general_name"]
    assert all(ref["cands"][0]["candidate"] is True for ref in refs)
    assert [ref["cands"][0]["code"] for ref in refs] == [
        "mhlw:medicine:900000001", "mhlw:general:GEN000000001"]


def test_ref_replace_or_delete_marks_rollup_dirty_without_derive_pids(store, tmp_path):
    """A tick that rewrites med_ref but runs out of rollup budget must not
    leave stale candidates once derive stops reporting the pid (M5)."""
    _seed(store)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    rollup.rebuild(store, 1)
    assert rollup.dirty_projects(store) == []
    document = deepcopy(DOCUMENT)
    document["dict_id"] = "fictional-v2"
    assert drug_map.derive(store, _dictionary(tmp_path, document))["pids"] == [1]
    assert rollup.dirty_projects(store) == [1]
    rollup.rebuild(store, 1)
    assert rollup.dirty_projects(store) == []
    assert drug_map.derive(store, None)["pids"] == [1]
    assert rollup.dirty_projects(store) == [1]
    rollup.rebuild(store, 1)
    assert rollup.dirty_projects(store) == []


def test_dictionary_replace_without_visible_refs_settles_after_one_rebuild(
        store, tmp_path):
    """A replace rewrites empty med_ref rows; the unchanged rollup must not
    stay dirty on every later tick (M5 follow-up)."""
    _seed(store, names=())
    drug_map.derive(store, _dictionary(tmp_path))
    rollup.rebuild(store, 1)
    assert rollup.dirty_projects(store) == []
    document = deepcopy(DOCUMENT)
    document["dict_id"] = "fictional-v2"
    drug_map.derive(store, _dictionary(tmp_path, document))
    assert rollup.dirty_projects(store) == [1]
    rollup.rebuild(store, 1)
    assert rollup.dirty_projects(store) == []


def test_deadline_cut_without_dictionary_stays_unavailable(store, monkeypatch):
    _seed(store)
    monkeypatch.setattr(drug_map.time, "monotonic", lambda: 10.0)
    assert drug_map.derive(store, None, deadline=5.0)["status"] == "unavailable"


def test_deadline_cut_scan_reports_partial(store, tmp_path, monkeypatch):
    _seed(store)
    monkeypatch.setattr(drug_map.time, "monotonic", lambda: 10.0)
    assert drug_map.derive(store, _dictionary(tmp_path), deadline=5.0) == {
        "status": "partial", "done": 0, "pids": []}
