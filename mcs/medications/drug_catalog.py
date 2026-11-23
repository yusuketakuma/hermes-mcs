"""在宅記録などから使う医薬品の共通入口（承認済み辞書の公式コードで検索・照会する）。"""
from __future__ import annotations

import drug_map

CODE_SYSTEM = "mhlw_medicine"


def _dictionary(cfg):
    """The configured, approved dictionary (drug_map.load keeps the last one loaded); else (None, state)."""
    dictionary, error = drug_map.configured(cfg)
    if error:
        return None, "invalid"
    if dictionary is None:
        return None, "unconfigured"
    if not dictionary.approved:
        return None, "unapproved"
    return dictionary, "active"


def status(cfg) -> str:
    """active / unconfigured / unapproved / invalid — why a search may return nothing."""
    return _dictionary(cfg)[1]


def _generic_names(dictionary) -> dict:
    names = {}
    for entry in dictionary.entries.values():
        if entry.kind == "general_name":
            for code in (*entry.source_codes, *(p.medicine for p in entry.products)):
                names.setdefault(code, entry.display)
    return names


def _item(dictionary, product, generic) -> dict:
    return {"code": product.medicine, "code_system": CODE_SYSTEM, "name": product.name,
            "generic_name": generic, "unit": product.unit, "form_code": product.form_code,
            "source_edition": {"dict_id": dictionary.dict_id, "sha256": dictionary.sha256,
                               "as_of": dictionary.source.as_of}}


def _entry_products(entry):
    """Product metadata, else the bare official code of a product identity without it."""
    if entry.kind not in ("product", "general_name"):
        return ()
    if entry.products:
        return entry.products
    return tuple(drug_map.Product(code, entry.display, "", "") for code in entry.source_codes
                 if entry.kind == "product")


def _products(dictionary):
    for entry in dictionary.entries.values():
        yield from _entry_products(entry)


def search(cfg, query, *, limit=20) -> list[dict]:
    """Products whose name, alias or code contains the query; nothing without an approved dictionary."""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("drug_catalog_limit")
    if not drug_map.valid_text(query) or not drug_map.fold(query):
        raise ValueError("drug_catalog_query")
    dictionary, _ = _dictionary(cfg)
    if dictionary is None:
        return []
    needle = drug_map.fold(query)
    ids = set(dictionary.matching(needle)[0])
    products = {}
    for entry in dictionary.entries.values():
        hit = entry.id in ids
        for product in _entry_products(entry):
            if hit or needle in drug_map.fold(product.name) or needle in product.medicine:
                products.setdefault(product.medicine, product)
    generic = _generic_names(dictionary)
    ordered = sorted(products.values(), key=lambda p: (drug_map.fold(p.name), p.medicine))
    return [_item(dictionary, p, generic.get(p.medicine)) for p in ordered[:limit]]


def lookup(cfg, code) -> dict | None:
    """One product by its official medicine code; None when unknown or no approved dictionary."""
    if not isinstance(code, str) or not code or len(code) > 64:
        raise ValueError("drug_catalog_code")
    dictionary, _ = _dictionary(cfg)
    if dictionary is None:
        return None
    for product in _products(dictionary):
        if product.medicine == code:
            return _item(dictionary, product, _generic_names(dictionary).get(code))
    return None
