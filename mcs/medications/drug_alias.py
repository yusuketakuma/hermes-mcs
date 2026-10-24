"""薬剤別名の影響プレビューとreceiptに束縛した私有辞書切替を行う。"""
from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import replace
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import drug_map
import safe_io
from mcs_requests import canonical, payload_hash, valid_uuid
from mcs_util import valid_hash
from safe_io import atomic_write, fsync_dir


def private_json(path, limit=drug_map.MAX_BYTES):
    """Read bounded, owner-only regular JSON without following symlinks."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with safe_io.fdopen_owned(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError("alias_private_file")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("alias_file_size")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("alias_file_shape")
    return doc, raw


def safe_alias(alias):
    # Restrict proposals to drug-like katakana surfaces; prose, kanji names,
    # Latin identifiers and long numeric sequences never enter a receipt.
    if (not isinstance(alias, str) or not 2 <= len(alias) <= 48
            or alias.endswith(("サン", "サマ", "センセイ", "タロウ", "ハナコ"))
            or not re.fullmatch(r"[ァ-ヶー・]+(?:[0-9]{1,3}(?:mg|mL|μg))?", alias)):
        raise ValueError("alias_surface_rejected")
    return drug_map.fold(alias)


def impact_snapshot(db, before, alias, target):
    """Bind aggregate effects to source generations without exporting post IDs/text."""
    if target not in before.entries:
        raise ValueError("alias_target_or_collision")
    after = replace(before, aliases={**before.aliases, alias: (before.entries[target],)})
    digest = hashlib.sha256()
    counts = {"posts": 0, "unextracted": 0, "mentions_changed": 0}
    for row in db.execute("SELECT message_id,content_hash,body_state FROM messages ORDER BY message_id"):
        source = drug_map._source(db, row[0])
        meta = db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (source[0],)).fetchone()[0] if source else None
        digest.update(canonical([row[0], row[1], row[2], list(source) if source else None, meta]))
        counts["posts"] += 1
        if source is None:
            counts["unextracted"] += 1
            continue
        try:
            meds = drug_map._source_meds(source)
        except ValueError:
            counts["unextracted"] += 1
            continue
        counts["mentions_changed"] += sum(before.resolve(name) != after.resolve(name)
                                           for _, name in meds)
    return {"hash": digest.hexdigest(), "counts": counts}


def _candidate_payload(pin, alias, target):
    document, original = private_json(pin["path"])
    if hashlib.sha256(original).hexdigest() != pin["sha256"]:
        raise ValueError("alias_stale_dictionary")
    for entry in document["entries"]:
        if entry["id"] == target:
            if len(entry["aliases"]) >= drug_map.MAX_ALIASES:
                raise ValueError("alias_capacity")
            entry["aliases"].append(alias)
    payload = canonical(document)
    if len(payload) > drug_map.MAX_BYTES:
        raise ValueError("alias_dictionary_size")
    return payload


def proposal(db, cfg, alias, target):
    folded = safe_alias(alias)
    before, error = drug_map.configured(cfg)
    if error or before is None or not before.approved:
        raise ValueError("alias_dictionary_unavailable")
    if target not in before.entries or before.aliases.get(folded):
        raise ValueError("alias_target_or_collision")
    # The loader rejects duplicate ids, so every alias hit for target is this one Entry.
    spellings = before.entries[target].aliases
    if not any(SequenceMatcher(None, folded, drug_map.fold(a)).ratio() >= 0.6 for a in spellings):
        raise ValueError("alias_not_drug_like")
    impact = impact_snapshot(db, before, folded, target)
    result = {"schema": "drug-alias-proposal/1", "before_dict_sha": before.sha256,
              "after_dict_sha": hashlib.sha256(_candidate_payload(cfg["drug_map"], alias, target)).hexdigest(),
              "resolver_version": drug_map.RESOLVER_VERSION, "alias": alias,
              "folded_alias": folded, "target_entry": target,
              "impact_snapshot_hash": impact["hash"], "impact_counts": impact["counts"]}
    return {**result, "proposal_hash": payload_hash(result)}


def validate_proposal(value):
    if not isinstance(value, dict) or set(value) != {
            "schema", "before_dict_sha", "after_dict_sha", "resolver_version", "alias", "folded_alias",
            "target_entry", "impact_snapshot_hash", "impact_counts", "proposal_hash"}:
        return "bad_alias_proposal"
    try:
        if (value["schema"] != "drug-alias-proposal/1"
                or safe_alias(value["alias"]) != value["folded_alias"]
                or value["resolver_version"] != drug_map.RESOLVER_VERSION
                or not valid_hash(value["before_dict_sha"])
                or not valid_hash(value["after_dict_sha"])
                or not valid_hash(value["impact_snapshot_hash"])
                or not isinstance(value["target_entry"], str)
                or not 1 <= len(value["target_entry"]) <= 120
                or not isinstance(value["impact_counts"], dict)
                or set(value["impact_counts"]) != {"posts", "unextracted", "mentions_changed"}
                or any(type(v) is not int or v < 0 for v in value["impact_counts"].values())
                or payload_hash({k: v for k, v in value.items() if k != "proposal_hash"})
                != value["proposal_hash"]):
            return "bad_alias_proposal"
    except (ValueError, TypeError):
        return "bad_alias_proposal"
    return None


def validate_operation(req, common):
    fields = {"proposal"} if req["cmd"] == "ops.drug_alias_apply" else {"approval_id"}
    if "project_id" in req or req.keys() - ((common - {"project_id"}) | fields | {"reason"}):
        return "unknown_field"
    # Fixed reason codes avoid patient text in dictionary-wide receipts.
    if req.get("reason") not in ("dictionary_alias_reviewed", "dictionary_alias_rollback"):
        return "bad_alias_reason"
    if req["cmd"] == "ops.drug_alias_apply":
        return validate_proposal(req.get("proposal"))
    return None if valid_uuid(req.get("approval_id")) else "bad_alias_approval_id"




@contextmanager
def _switch(config_path, cfg, raw, before, after, journal, record, *, recovery=False):
    """Keep compensation armed until the surrounding receipt COMMIT completes."""
    if not recovery:
        safe_io.publish_new_bytes(journal, canonical(record))
    switched = False
    try:
        if private_json(config_path)[1] != raw:
            raise ValueError("alias_config_changed")
        switched = True  # atomic_write may rename and then fail its directory sync.
        atomic_write(str(config_path), lambda f: f.write(canonical({**cfg, "drug_map": after}).decode()), 0o600)
        yield
    except BaseException:
        if switched:
            current, _ = private_json(config_path)
            if current not in (cfg, {**cfg, "drug_map": after}):
                raise RuntimeError("alias_recovery_required") from None
            atomic_write(str(config_path), lambda f: f.write(raw.decode()), 0o600)
        if not recovery:
            journal.unlink()
            fsync_dir(journal.parent)
        raise
    else:
        # A crash after COMMIT leaves the journal for explicit rollback, never retry.
        with suppress(OSError):
            journal.unlink()
            fsync_dir(journal.parent)


def apply_tx(db, req, filesystem_changes):
    """Apply/rollback only inside the existing human command receipt transaction."""
    if filesystem_changes is None:
        raise RuntimeError("alias_receipt_transaction_required")
    error = validate_operation(req, {"version", "cmd", "command_id", "actor", "human_confirmed"})
    if error or req.get("human_confirmed") is not True:
        return error or "human_confirmation_required", {}
    main = db.execute("PRAGMA database_list").fetchone()["file"]
    if not main:
        return "alias_no_data_dir", {}
    root = Path(main).parent.parent
    config_path = root / "config.json"
    folder = root / "data" / "drug-map"
    journal = folder / "alias-switch.pending.json"
    try:
        cfg, raw = private_json(config_path)
        before = cfg.get("drug_map")
        if drug_map.config_error(before) is not None:
            return "alias_dictionary_unavailable", {}
        if req["cmd"] == "ops.drug_alias_apply":
            dictionary, error = drug_map.configured(cfg)
            if error or dictionary is None:
                return "alias_dictionary_unavailable", {}
        folder.mkdir(mode=0o700, exist_ok=True)
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return "alias_private_directory", {}
        recovery = journal.exists()
        if req["cmd"] == "ops.drug_alias_apply":
            if recovery:
                return "alias_recovery_required", {}
            wanted = req["proposal"]
            fresh = proposal(db, cfg, wanted["alias"], wanted["target_entry"])
            if fresh != wanted:
                return "alias_stale_proposal", {}
            payload = _candidate_payload(before, wanted["alias"], wanted["target_entry"])
            sha = hashlib.sha256(payload).hexdigest()
            if sha != wanted["after_dict_sha"]:
                return "alias_stale_dictionary", {}
            destination = folder / ("alias-" + sha + ".json")
            if not destination.exists():
                safe_io.publish_new_bytes(destination, payload)
            if drug_map.load(destination, expected_sha256=sha) is None:
                return "alias_dictionary_unavailable", {}
            after = {"path": str(destination), "sha256": sha}
            record = {"cmd": req["cmd"], "approval_id": req["command_id"], "before_pin": before,
                      "after_pin": after, "proposal_hash": wanted["proposal_hash"],
                      "alias": wanted["alias"], "target_entry": wanted["target_entry"],
                      "resolver_version": wanted["resolver_version"],
                      "impact_snapshot_hash": wanted["impact_snapshot_hash"], "reason": req["reason"]}
        else:
            if recovery:
                record, _ = private_json(journal, 16384)
            else:
                row = db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=? AND outcome='applied'",
                                 (req["approval_id"],)).fetchone()
                record = json.loads(row[0]) if row else {}
            if (record.get("cmd") != "ops.drug_alias_apply"
                    or record.get("approval_id") != req["approval_id"]
                    or before not in (record.get("after_pin"), record.get("before_pin"))
                    or (not recovery and before != record.get("after_pin"))):
                return "alias_rollback_stale", {}
            after = record["before_pin"]
            if drug_map.load(after["path"], expected_sha256=after["sha256"]) is None:
                return "alias_previous_dictionary_missing", {}
            rollback_origin = record
            record = {"cmd": req["cmd"], "approval_id": req["approval_id"],
                      "before_pin": before, "after_pin": after, "reason": req["reason"]}
        filesystem_changes.enter_context(_switch(config_path, cfg, raw, before, after,
                                                 journal, rollback_origin if req["cmd"] == "ops.drug_alias_rollback" else record,
                                                 recovery=recovery))
        return None, record
    except (OSError, ValueError, KeyError, TypeError):
        return "alias_switch_failed", {}
