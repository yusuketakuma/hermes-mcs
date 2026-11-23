"""Synthetic contracts for the shared medicine catalog entry point."""
import hashlib
import json
from copy import deepcopy

import pytest

import drug_catalog
from medications_testkit import DOCUMENT, _source_document


def _cfg(tmp_path, document, approved=True):
    document = deepcopy(document)
    if not approved:
        document["source"].pop("approved_by")
    raw = json.dumps(document, ensure_ascii=False).encode()
    path = tmp_path / "catalog.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return {"drug_map": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}}


def _with_products():
    document = _source_document()
    document["entries"][0]["products"] = [
        {"medicine": "900000001", "name": "架空製品錠5mg", "unit": "錠", "form_code": "1"}]
    document["entries"][1]["products"] = [
        {"medicine": "900000002", "name": "架空後発錠5mg", "unit": "錠", "form_code": "1"}]
    return document


def test_search_returns_official_codes_with_edition(tmp_path):
    cfg = _cfg(tmp_path, _with_products())
    items = drug_catalog.search(cfg, "架空")
    assert [item["code"] for item in items] == ["900000002", "900000001"]
    first = items[0]
    assert first["code_system"] == "mhlw_medicine" and first["unit"] == "錠"
    assert first["generic_name"] == "【般】架空5mg錠"
    assert first["source_edition"]["sha256"] == cfg["drug_map"]["sha256"]
    assert drug_catalog.search(cfg, "900000001")[0]["name"] == "架空製品錠5mg"
    assert drug_catalog.search(cfg, "架空", limit=1) == items[:1]
    assert drug_catalog.status(cfg) == "active"


def test_alias_matches_beyond_the_reference_search_cap_are_returned(tmp_path):
    document = _source_document()
    document["entries"] = []
    for n in range(205):
        code = f"9{n:08d}"
        # the last identity sorts after the old 200-item cap but its product name sorts first
        name = "あ架空製品" if n == 204 else f"ん架空製品{n:03d}"
        document["entries"].append({
            "id": f"mhlw:medicine:{code}", "kind": "product", "display": name,
            "aliases": [f"共通別名{n:03d}"], "codes": {"medicine": code}, "forms": [],
            "source_codes": [{"medicine": code, "drug_price": f"DRG{n:09d}", "general_name": None}],
            "products": [{"medicine": code, "name": name, "unit": "錠", "form_code": "1"}]})
    cfg = _cfg(tmp_path, document)
    items = drug_catalog.search(cfg, "共通別名", limit=200)
    assert items[0]["name"] == "あ架空製品" and len(items) == 200


def test_lookup_by_code_and_unknown(tmp_path):
    cfg = _cfg(tmp_path, _with_products())
    assert drug_catalog.lookup(cfg, "900000001")["name"] == "架空製品錠5mg"
    assert drug_catalog.lookup(cfg, "999999999") is None


def test_product_without_metadata_keeps_its_official_code(tmp_path):
    cfg = _cfg(tmp_path, _source_document())
    assert [item["code"] for item in drug_catalog.search(cfg, "架空製品")] == ["900000001"]
    assert drug_catalog.lookup(cfg, "900000001")["unit"] == ""


@pytest.mark.parametrize("case", ["unconfigured", "unapproved", "invalid", "legacy"])
def test_no_approved_official_dictionary_returns_nothing(tmp_path, case):
    cfg = ({} if case == "unconfigured" else _cfg(tmp_path, _with_products(), approved=False)
           if case == "unapproved" else _cfg(tmp_path, DOCUMENT) if case == "legacy"
           else {"drug_map": {"path": str(tmp_path / "absent.json"), "sha256": "f" * 64}})
    assert drug_catalog.search(cfg, "架空") == []
    assert drug_catalog.lookup(cfg, "900000001") is None
    assert drug_catalog.status(cfg) == ("active" if case == "legacy" else case)


@pytest.mark.parametrize("args", [("",), ("架空", 0), ("架空", 201)])
def test_arguments_are_strict(tmp_path, args):
    with pytest.raises(ValueError):
        drug_catalog.search({}, args[0], **({"limit": args[1]} if len(args) > 1 else {}))
