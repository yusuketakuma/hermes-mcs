"""Pinned private dictionaries and source-bound medication candidate annotations."""

from dataclasses import asdict, dataclass
from collections.abc import Mapping
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from types import MappingProxyType
from typing import Literal, TypedDict

from clinical_values import MedicationSurface, fold_surface, medication_surface
from mcs_queries import current_extract_pred, current_fact_pred
from mcs_requests import parse_command

SCHEMA = "mcs-drug-map/1"
SOURCE_SCHEMA = "mcs-drug-map/2"
RESOLVER_VERSION = "mcs-drug-ref/1"
KIND = "med_ref"
# 20260930 whole master: 19,272 rows, 12,792 identities, ~5.43 MiB JSON.
MAX_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 20000
PROGRESS_KIND = "med_ref_progress"
SCAN_BATCH = 100
MAX_ALIASES = 100
_GENERIC = {fold_surface(s) for s in ("薬", "処方薬", "内服薬", "降圧薬")}

@dataclass(frozen=True)
class Source:
    name: str
    url: str
    terms_checked_on: str
    approved_by: str


@dataclass(frozen=True)
class Entry:
    id: str
    kind: Literal["ingredient", "class", "general_name", "product"]
    display: str
    aliases: tuple[str, ...] = ()


class Candidate(TypedDict):
    system: str
    code: str
    display: str
    kind: Literal["ingredient", "class", "general_name", "product"]
    method: str
    candidate: bool


class Ref(TypedDict):
    i: int
    name: str
    status: str
    cands: list[Candidate]


class Annotation(Ref):
    dict_id: str
    dict_sha256: str
    resolver_version: str
    source_kind: str


class Binding(TypedDict):
    source_artifact_id: int
    source_kind: str
    hash: str
    source_sha256: str


class DeriveResult(TypedDict):
    status: str
    done: int
    pids: list[int]


def fold(text: str) -> str:
    """Reuse clinical surface folding, then fold dictionary spelling marks."""
    return re.sub(r"[・()\s]", "", fold_surface(text).translate(
        str.maketrans({c: "ー" for c in "‐‑‒–—―−-"})))


def split_surface(name: str) -> MedicationSurface:
    """Separate a trailing maker label, form and strength without fuzzy matching."""
    bare = re.sub(r"[「『][^「」『』]*[」』]$", "", name.strip())
    surface: MedicationSurface = medication_surface(fold(bare))
    if surface["form"] is None:
        extended = re.search(
            r"(ds|点眼|注|液)([0-9]+(?:\.[0-9]+)?(?:mg|μg|mcg|g|ml|%))?$",
            surface["surface"])
        if extended and extended.start() > 0:
            surface["base"] = surface["surface"][:extended.start()]
            surface["form"] = extended[1]
            surface["strength"] = extended[2]
    surface["base"] = fold(surface["base"])
    return surface


@dataclass(frozen=True)
class DrugMap:
    dict_id: str
    sha256: str
    source: Source
    aliases: Mapping[str, tuple[Entry, ...]]

    @property
    def approved(self) -> bool:
        return bool(self.source.approved_by)

    def lookup(self, name: str) -> dict:
        """Explain an offline candidate lookup; approval never confirms a medication."""
        if not _text(name):
            raise ValueError("drug_map_lookup_name")
        ref = self.resolve(name)
        codes = {c["code"] for c in ref["cands"]}
        hits = self.aliases.get(fold(name), ())
        if not hits:
            hits = self.aliases.get(split_surface(name)["base"], ())
        return {**ref, "dictionary": {"id": self.dict_id, "sha256": self.sha256,
                    "resolver_version": RESOLVER_VERSION, "approved": self.approved,
                    "source": {"name": self.source.name, "url": self.source.url,
                               "terms_checked_on": self.source.terms_checked_on}},
                "surface": split_surface(name),
                "matched_aliases": sorted({alias for entry in hits if entry.id in codes
                                           for alias in entry.aliases}),
                "explanation": {"resolved": "辞書の一致候補。処方・成分の確定ではありません。",
                    "ambiguous": "複数の一致候補。用量・剤形等を確認してください。",
                    "generic": "総称のため個別薬剤を確定できません。",
                    "unresolved": "一致する候補はありません。類似名から推定しません。"}[ref["status"]]}

    def resolve(self, name: str, i: int = 0) -> Ref:
        """Return identity candidates; official product/general names are exact-only."""
        hits = self.aliases.get(fold(name), ())
        method = "alias"
        if not hits:
            hits = tuple(entry for entry in self.aliases.get(split_surface(name)["base"], ())
                         if entry.kind in ("ingredient", "class"))
            method = "stem"
        generic = fold(name) in _GENERIC
        status = ("generic" if generic else "ambiguous" if len(hits) > 1
                  else "generic" if hits and hits[0].kind == "class"
                  else "resolved" if hits else "unresolved")
        cands: list[Candidate] = [{"system": "local", "code": entry.id,
                  "display": entry.display, "kind": entry.kind,
                  "method": method, "candidate": True} for entry in hits
                 if not generic or entry.kind == "class"]
        return {"i": i, "name": name, "status": status, "cands": cands}


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512


def _source_identity(entry) -> bool:
    """Keep explicit official identities opaque; no code prefix implies an ingredient."""
    codes, rows = entry.get("codes"), entry.get("source_codes")
    if (not isinstance(codes, dict) or not isinstance(rows, list)
            or not 1 <= len(rows) <= MAX_ENTRIES
            or any(not isinstance(row, dict) or set(row) != {
                "medicine", "drug_price", "general_name"} for row in rows)):
        return False
    for row in rows:
        if (not isinstance(row["medicine"], str)
                or not re.fullmatch(r"[0-9]{1,9}", row["medicine"])
                or int(row["medicine"]) == 0
                or any(value is not None and (not isinstance(value, str)
                    or not re.fullmatch(r"[A-Za-z0-9]{1,12}", value))
                    for value in (row["drug_price"], row["general_name"]))):
            return False
    if entry["kind"] == "general_name":
        code = codes.get("general_name")
        return (set(codes) == {"general_name"} and isinstance(code, str)
                and re.fullmatch(r"[A-Za-z0-9]{12}", code) is not None
                and any(c != "0" for c in code)
                and entry["id"] == f"mhlw:general:{code}"
                and all(row["general_name"] == code for row in rows))
    code = codes.get("medicine")
    return (entry["kind"] == "product" and set(codes) == {"medicine"}
            and isinstance(code, str) and entry["id"] == f"mhlw:medicine:{code}"
            and len(rows) == 1 and rows[0]["medicine"] == code)


def load(path: str | Path, *, expected_sha256: str) -> DrugMap | None:
    """Load only an explicit absolute, owner-private regular file with a SHA pin.

    Missing files are unavailable. Invalid, oversized, shared or unpinned files
    raise ValueError. No default path, environment lookup or service is used.
    An unapproved dictionary may be resolved in isolation but derive refuses
    production application unless its caller explicitly selects synthetic mode.
    """
    path = Path(path)
    if not path.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("drug_map_path_or_pin")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("drug_map_file") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
            raise ValueError("drug_map_private_or_size")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("drug_map_size")
    sha = hashlib.sha256(raw).hexdigest()
    if sha != expected_sha256:
        raise ValueError("drug_map_sha256")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("drug_map_json") from exc
    if not isinstance(document, dict) or document.get("schema") not in (SCHEMA, SOURCE_SCHEMA):
        raise ValueError("drug_map_schema")
    source = document.get("source")
    entries = document.get("entries")
    if (not _text(document.get("dict_id")) or not isinstance(source, dict)
            or not all(_text(source.get(k)) for k in
                       ("name", "url", "terms_checked_on"))
            or ("approved_by" in source and not _text(source["approved_by"]))
            or not isinstance(entries, list) or len(entries) > MAX_ENTRIES):
        raise ValueError("drug_map_provenance_or_entries")
    try:
        date.fromisoformat(source["terms_checked_on"])
    except ValueError as exc:
        raise ValueError("drug_map_terms_date") from exc
    aliases = {}
    ids = set()
    for entry in entries:
        if (not isinstance(entry, dict)
                or not _text(entry.get("id")) or entry["id"] in ids
                or entry.get("kind") not in (("ingredient", "class") if document["schema"] == SCHEMA
                                            else ("general_name", "product"))
                or not _text(entry.get("display"))
                or not isinstance(entry.get("aliases"), list)
                or len(entry["aliases"]) > MAX_ALIASES
                or not all(_text(a) for a in entry["aliases"])
                or not isinstance(entry.get("codes"), dict)
                or not all(k in (("yj", "ssk") if document["schema"] == SCHEMA
                                 else ("medicine", "general_name")) and _text(v)
                           for k, v in entry["codes"].items())
                or not isinstance(entry.get("forms"), list)
                or not all(_text(f) for f in entry["forms"])
                or len(entry["forms"]) > MAX_ALIASES):
            raise ValueError("drug_map_entry")
        if document["schema"] == SOURCE_SCHEMA and not _source_identity(entry):
            raise ValueError("drug_map_source_identity")
        ids.add(entry["id"])
        # /1 ingredient/class identities remain unchanged. /2 uses explicit
        # official product/general-name identity, never inferred ingredients.
        candidate = Entry(entry["id"], entry["kind"], entry["display"],
                          tuple(sorted(set([entry["display"], *entry["aliases"]]))))
        for alias in {fold(a) for a in [entry["display"], *entry["aliases"]]}:
            if not alias:
                raise ValueError("drug_map_empty_alias")
            aliases.setdefault(alias, []).append(candidate)
    provenance = Source(source["name"], source["url"], source["terms_checked_on"],
                        source.get("approved_by", ""))
    return DrugMap(document["dict_id"], sha, provenance,
                   MappingProxyType({key: tuple(values)
                                     for key, values in aliases.items()}))


def _source(db: sqlite3.Connection, mid: int):
    """Select the current published fact generation, with rule-only fallback."""
    return db.execute(f"""
        SELECT a.artifact_id, a.kind, a.content, m.content_hash, m.project_id
        FROM artifacts a JOIN messages m ON m.message_id=a.message_id
        WHERE a.message_id=? AND a.kind IN
          ('semantic_facts_v4','canonical_projection','extract_llm','extract_v1')
          {current_extract_pred()}
          AND (a.kind='extract_v1' OR (1=1 {current_fact_pred()}))
          AND COALESCE(json_extract(a.content,'$._error'),0)=0
        ORDER BY CASE a.kind WHEN 'semantic_facts_v4' THEN 0
          WHEN 'canonical_projection' THEN 1 WHEN 'extract_llm' THEN 2
          ELSE 3 END, a.artifact_id DESC LIMIT 1
    """, (mid,)).fetchone()


def config_error(value) -> str | None:
    """Validate config ``drug_map`` ({path, sha256}); None when well-formed."""
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        return "must contain only path and sha256"
    if not isinstance(value["path"], str) or not os.path.isabs(value["path"]):
        return "path must be absolute"
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        return "sha256 must be a pinned lowercase SHA256"
    return None


def configured(cfg) -> tuple["DrugMap | None", str | None]:
    """The config-pinned dictionary: (map, None); (None, None) when unset
    or the file is absent; (None, "invalid_config"|"invalid_dictionary")."""
    setting = (cfg or {}).get("drug_map") if isinstance(cfg, dict) else None
    if setting is None:
        return None, None
    if config_error(setting) is not None:
        return None, "invalid_config"
    try:
        return load(setting["path"], expected_sha256=setting["sha256"]), None
    except ValueError:
        return None, "invalid_dictionary"


def active_dictionary(db: sqlite3.Connection, cfg) -> tuple["DrugMap | None", str]:
    """The configured dictionary only while it is the DB's active derive
    generation — so a chat search never answers from a dictionary whose
    candidates the posts do not carry. State: active/unconfigured/
    inactive/invalid."""
    dictionary, error = configured(cfg)
    if error:
        return None, "invalid"
    if dictionary is None:
        return None, "unconfigured"
    progress = _progress(db)
    if (not progress or progress.get("invalid") or progress.get("dictionary")
            != [dictionary.dict_id, dictionary.sha256, False, RESOLVER_VERSION]):
        return None, "inactive"
    return dictionary, "active"


def _source_meta(source) -> Binding:
    return {"source_artifact_id": source[0], "source_kind": source[1],
            "hash": source[3],
            "source_sha256": hashlib.sha256(source[2].encode()).hexdigest()}


def _source_meds(source) -> list[tuple[int, str]]:
    """Index and name of each named medication in a current source; ValueError on bad shape."""
    try:
        document = json.loads(source[2])
    except (TypeError, RecursionError) as error:
        raise ValueError("source_json") from error
    if not isinstance(document, dict):
        raise ValueError("source_shape")
    meds = document.get("medications" if source[1] == "extract_v1" else "meds", [])
    if not isinstance(meds, list):
        raise ValueError("medications_shape")
    return [(i, med["name"]) for i, med in enumerate(meds)
            if isinstance(med, dict) and _text(med.get("name"))]


def current_refs(db: sqlite3.Connection, mid: int) -> list[Annotation]:
    """Read only exact current-source annotations from the DB, never a dictionary."""
    progress = _progress(db)
    if progress is not None and progress.get("dictionary") is None:
        return []
    source = _source(db, mid)
    if source is None:
        return []
    binding = _source_meta(source)
    row = db.execute(
        "SELECT content,meta FROM artifacts WHERE kind=? AND message_id=? "
        "AND (project_id IS NULL OR project_id=?) "
        "ORDER BY artifact_id DESC LIMIT 1", (KIND, mid, source[4])).fetchone()
    if row is None:
        return []
    try:
        content, meta = json.loads(row[0]), json.loads(row[1])
    except (ValueError, TypeError, RecursionError):
        return []
    if (not isinstance(content, dict) or not isinstance(meta, dict)
            or any(meta.get(k) != v for k, v in binding.items())
            or meta.get("resolver_version") != RESOLVER_VERSION
            or not _text(meta.get("dict_id"))
            or not isinstance(meta.get("dict_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", meta["dict_sha256"])
            or (progress is not None and progress.get("dictionary") !=
                [meta.get("dict_id"), meta.get("dict_sha256"), False, RESOLVER_VERSION])
            or meta.get("synthetic") is not False
            or not isinstance(meta.get("source"), dict)
            or not _text(meta["source"].get("approved_by"))
            or not isinstance(content.get("refs"), list)):
        return []
    try:
        meds = json.loads(source[2]).get(
            "medications" if source[1] == "extract_v1" else "meds", [])
    except (ValueError, TypeError, RecursionError):
        return []
    if not isinstance(meds, list):
        return []
    refs = []
    for ref in content["refs"]:
        if (not isinstance(ref, dict) or type(ref.get("i")) is not int
                or not 0 <= ref["i"] < len(meds)
                or not isinstance(meds[ref["i"]], dict)
                or ref.get("name") != meds[ref["i"]].get("name")
                or ref.get("status") not in
                ("resolved", "ambiguous", "unresolved", "generic")
                or not isinstance(ref.get("cands"), list)):
            continue
        cands = ref["cands"]
        if not all(isinstance(c, dict) and c.get("system") == "local"
                   and _text(c.get("code")) and _text(c.get("display"))
                   and c.get("kind") in ("ingredient", "class", "general_name", "product")
                   and c.get("method") in ("alias", "stem")
                   and c.get("candidate") is True for c in cands):
            continue
        status = ref["status"]
        if ((status == "resolved" and
             (len(cands) != 1 or cands[0]["kind"] not in ("ingredient", "general_name", "product")))
                or (status == "ambiguous" and len(cands) < 2)
                or (status == "unresolved" and cands)
                or (status == "generic" and
                    any(c["kind"] != "class" for c in cands))):
            continue
        refs.append({**ref, "dict_id": meta["dict_id"],
                     "dict_sha256": meta["dict_sha256"],
                     "resolver_version": meta["resolver_version"],
                     "source_kind": source[1]})
    return refs


def _progress(db: sqlite3.Connection) -> dict | None:
    row = db.execute("SELECT content FROM artifacts WHERE kind=? "
                     "ORDER BY artifact_id DESC LIMIT 1", (PROGRESS_KIND,)).fetchone()
    if row is None:
        return None
    try:
        progress = parse_command(row[0])
    except (ValueError, TypeError, RecursionError):
        return {"invalid": True}
    if not isinstance(progress, dict) or set(progress) != {"dictionary", "cursor"}:
        return {"invalid": True}
    cursor, generation = progress["cursor"], progress["dictionary"]
    if type(cursor) is not int or not 0 <= cursor <= 2**63 - 1:
        return {"invalid": True}
    if generation is not None and (
            not isinstance(generation, list) or len(generation) != 4
            or not _text(generation[0]) or not isinstance(generation[1], str)
            or not re.fullmatch(r"[0-9a-f]{64}", generation[1])
            or type(generation[2]) is not bool or generation[3] != RESOLVER_VERSION):
        return {"invalid": True}
    return progress


def generation_signature(db: sqlite3.Connection) -> str:
    """Stable active dictionary currency for rollup caches; cursor is excluded."""
    progress = _progress(db)
    generation = ("legacy" if progress is None else "invalid" if progress.get("invalid")
                  else progress["dictionary"])
    return hashlib.sha256(json.dumps(generation, sort_keys=True).encode()).hexdigest()


def _save_progress(db: sqlite3.Connection, progress: dict) -> None:
    # One metadata-only artifact uses the existing durable storage mechanism.
    row = db.execute("SELECT artifact_id FROM artifacts WHERE kind=? "
                     "ORDER BY artifact_id DESC LIMIT 1", (PROGRESS_KIND,)).fetchone()
    payload = json.dumps(progress, sort_keys=True)
    if row:
        db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                   (payload, row[0]))
    else:
        db.execute("INSERT INTO artifacts(kind,content,meta,created_at) VALUES(?,?,'{}',?)",
                   (PROGRESS_KIND, payload, time.time()))


def derive(ledger, dictionary: DrugMap | None, *,
           deadline: float | None = None, synthetic: bool = False) -> DeriveResult:
    """Resume a bounded source walk; commit each annotation with its cursor.

    Completed cycles start again to catch changes at earlier message IDs.
    Source bindings hide changed originals immediately; dictionary generation
    changes hide old references even while their bounded cleanup is unfinished.
    """
    usable = dictionary is not None and (dictionary.approved or synthetic)
    generation = [dictionary.dict_id, dictionary.sha256, synthetic, RESOLVER_VERSION] if usable else None
    progress = _progress(ledger.db)
    if progress is None and not usable and not ledger.db.execute(
            "SELECT 1 FROM artifacts WHERE kind=? LIMIT 1", (KIND,)).fetchone():
        return {"status": "unavailable", "done": 0, "pids": []}
    progress = progress or {}
    done, pids, cut = 0, set(), False
    if progress.get("dictionary") != generation:
        pids.update(r[0] for r in ledger.db.execute(
            "SELECT DISTINCT project_id FROM artifacts WHERE kind=? "
            "AND project_id IS NOT NULL", (KIND,)))
        progress = {}
    cursor = progress.get("cursor", 0)
    if type(cursor) is not int or cursor < 0:
        cursor = 0
    progress = {"dictionary": generation, "cursor": cursor}
    with ledger.db:
        _save_progress(ledger.db, progress)
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            cut = True
            break
        rows = ledger.db.execute(("""
            SELECT message_id,project_id FROM messages WHERE message_id>?
            UNION """ if usable else "") + """SELECT message_id,project_id
            FROM artifacts WHERE kind=? AND message_id>?
            ORDER BY message_id LIMIT ?
        """, ((cursor, KIND, cursor, SCAN_BATCH) if usable else
              (KIND, cursor, SCAN_BATCH))).fetchall()
        if not rows:
            progress["cursor"] = 0
            with ledger.db:
                _save_progress(ledger.db, progress)
            break
        for mid, pid in rows:
            if deadline is not None and time.monotonic() >= deadline:
                cut = True
                break
            source = _source(ledger.db, mid) if usable else None
            content = meta = None
            if source is not None and dictionary is not None:
                try:
                    content = {"refs": [dictionary.resolve(name, i)
                                        for i, name in _source_meds(source)]}
                    meta = {**_source_meta(source), "dict_id": dictionary.dict_id,
                        "dict_sha256": dictionary.sha256,
                        "resolver_version": RESOLVER_VERSION,
                        "source": asdict(dictionary.source), "synthetic": synthetic}
                    pid = source[4]
                except (ValueError, TypeError, AttributeError, RecursionError):
                    content = meta = None
            old = ledger.db.execute(
                "SELECT content,meta FROM artifacts WHERE kind=? AND message_id=?",
                (KIND, mid)).fetchall()
            encoded = json.dumps(content, ensure_ascii=False) if content else None
            encoded_meta = json.dumps(meta, ensure_ascii=False) if meta else None
            changed = not ((not old and content is None) or
                           (len(old) == 1 and tuple(old[0]) == (encoded, encoded_meta)))
            progress["cursor"] = mid
            with ledger.db:
                if changed:
                    ledger.db.execute("DELETE FROM artifacts WHERE kind=? AND message_id=?",
                                      (KIND, mid))
                    if content is not None:
                        ledger.artifact_add_tx(KIND, encoded, project_id=pid,
                                               message_id=mid, meta=meta)
                _save_progress(ledger.db, progress)
            cursor = mid
            if changed:
                done += 1
                if pid is not None:
                    pids.add(pid)
        if cut:
            break
    return {"status": "unavailable" if not usable else "partial" if cut else "ok",
            "done": done, "pids": sorted(pids)}


def candidate_note(ref: Annotation | None) -> str:
    """Human candidate wording: ambiguous references never expose ingredient names."""
    if not ref:
        return ""
    status = ref.get("status")
    label = {"ambiguous": "複数候補", "generic": "総称",
             "unresolved": "不明"}.get(status)
    if status == "resolved":
        cands = ref.get("cands", [])
        if (len(cands) != 1 or cands[0].get("kind") not in ("ingredient", "general_name", "product")
                or cands[0].get("candidate") is not True):
            return ""
        label = cands[0]["display"]
    if not label:
        return ""
    kinds = {c.get("kind") for c in ref.get("cands", [])}
    prefix = ("製品候補" if kinds == {"product"} else "一般名処方候補" if kinds == {"general_name"}
              else "薬剤候補" if kinds & {"product", "general_name"} else "成分候補")
    return (f"{prefix}: {label}（辞書 {ref['dict_id']}@"
            f"{ref['dict_sha256'][:8]}・未確認）")
