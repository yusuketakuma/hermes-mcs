"""Fully fictional drug dictionaries and seed helpers shared by drug tests.

Not a test module (no ``test_`` prefix); the tests/ conftest puts this
directory on sys.path, so tests import it as ``medications_testkit``."""
from copy import deepcopy
import errno
import hashlib
import json
import os
from typing import TypedDict

import drug_map
from semantic_projection import PROJECTION_VERSION
from extract_testkit import _hash, _message
from common_testkit import ledger_fixture


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
        meta={"hash": _hash(store, mid), "engine_version": 4,
              **({"projection_version": PROJECTION_VERSION}
                 if kind in ("canonical_projection", "semantic_facts_v4") else {})})


store = ledger_fixture(name="store", patients=(1,))


def _fail_dictionary_io(monkeypatch, path, failure):
    """Inject a synthetic failure only into this dictionary's acquired FD."""
    opened = []
    real_open, real_fdopen, real_fstat = os.open, os.fdopen, os.fstat

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "synthetic dictionary I/O failure")

    def tracked_open(name, *args, **kwargs):
        fd = real_open(name, *args, **kwargs)
        if os.fspath(name) == os.fspath(path):
            opened.append(fd)
        return fd

    def wrapped_fdopen(fd, *args, **kwargs):
        if fd in opened and failure == "fdopen":
            fail()
        stream = real_fdopen(fd, *args, **kwargs)
        if fd in opened and failure == "read":
            monkeypatch.setattr(stream, "read", fail)
        return stream

    def checked_fstat(fd):
        if fd in opened and failure == "fstat":
            fail()
        return real_fstat(fd)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fdopen", wrapped_fdopen)
    monkeypatch.setattr(os, "fstat", checked_fstat)
    return opened, real_fstat


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
