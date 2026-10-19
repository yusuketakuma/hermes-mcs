#!/usr/bin/env python3
"""Governed external export contract — disabled by default (T16).

A provider-neutral authorization + envelope + journal layer around the
T15 machine read model. There is deliberately NO network code here: the
only demonstrable destination is :class:`LocalSink`, an in-process fake
consumer backed by a directory. Wiring a real connector requires a
named provider, an access review and a separate permission — see
``docs/specs/external-export-contract.md``.

Guarantees enforced in code:

- every export attempt is bound to an ``mcs-ext-auth/1`` authorization
  file (purpose/actor/destination/scope/per-patient eligibility/expiry/
  retention) — revoked, expired, malformed or detail-scope
  authorizations are refused before any bytes move;
- every record leaving the process is aggregate-scope only and passes a
  field whitelist — the default /1 path rejects raw content at both the
  producer and the sink; explicit C1 validation permits only the paired
  ``message_body.body_text`` field, not other raw-content keys;
- sends are idempotent by ``envelope_id`` — an acknowledged envelope is
  never re-sent, and an unacknowledged one is HELD, never retried
  blindly (a lost acknowledgement leaves the outcome ``unknown`` until
  reconciled or explicitly re-authorized);
- withdrawal propagates a delete directive whose acknowledgement is
  independently journaled — an unacknowledged delete is held, not
  claimed;
- every action appends to an audit log — refusals included.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401
import export_contract  # noqa: E402
import export_delivery  # noqa: E402
from c1_contract import C1ContractError  # noqa: E402
from c1_envelopes import (  # noqa: E402
    CONTRACT as C1_CONTRACT, MAX_WIRE_BYTES, RECEIPT_CONTRACT,
    WITHDRAW_CONTRACT, WITHDRAW_REASONS, build_envelopes, encode_envelope)
from stats_math import finite_number  # noqa: E402
from safe_io import fdopen_owned, fsync_dir  # noqa: E402


_RECEIVER_FAULTS = ("receiver_capacity", "receiver_state", "receiver_directory",
                    "receiver_lock", "receiver_source", "receiver_limits", "receiver_time")


def _write_new_receipts(path: str, bundle: str) -> None:
    # Publish a complete private file without replacing any competing file
    # or symlink. The initial --receipts-out check alone cannot protect it.
    parent = str(Path(path).parent)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".r-", suffix=".tmp")
    try:
        with fdopen_owned(fd, "w", encoding="utf-8") as stream:
            stream.write(bundle)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise export_contract.ContractError("receipts_out_exists") from None
        fsync_dir(parent, require_dir=False)
    finally:
        with suppress(OSError):
            os.unlink(tmp)


def _receive(args) -> dict:
    """Feed handed-over files to the synthetic reference receiver.

    Contract refusals become terminal rejected (or deleted:false) receipts.
    Receiver storage faults raise instead, so the sender stays held. The
    transport tally never claims collection completeness; that is reported
    separately from the receiver's own diagnostics."""
    from c1_receiver import ReferenceReceiver
    if (args.input is None) != (args.receipts_out is None):
        raise export_contract.ContractError("receive_input_and_receipts_out_required")
    if args.receipts_out is not None and os.path.lexists(args.receipts_out):
        raise export_contract.ContractError("receipts_out_exists")
    try:
        receiver = ReferenceReceiver(args.receiver_root, source_label=args.source_label)
        # Maintenance is part of this mutating receive command, including a
        # run without input. Historical receipt/tombstone metadata is retained.
        expiry = receiver.expire()
    except C1ContractError as e:
        raise export_contract.ContractError(e.code) from e
    transport: Counter = Counter()
    receipts = []
    if args.input is not None:
        source = Path(args.input)
        if source.is_symlink():
            raise export_contract.ContractError("receive_input_unsafe")
        files = sorted(p for p in source.rglob("*.json") if p.is_file() and not p.is_symlink()
                       and not p.name.startswith(".")) if source.is_dir() else [source]
        for path in files:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                raise export_contract.ContractError("receive_input_unsafe") from None
            with fdopen_owned(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise export_contract.ContractError("receive_input_unsafe")
                raw = stream.read(MAX_WIRE_BYTES + 1)
            try:
                peek = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                peek = None
            eid = peek.get("envelope_id") if isinstance(peek, dict) else None
            eid = eid if isinstance(eid, str) and re.fullmatch(r"[0-9a-f]{24}", eid) else None
            contract = peek.get("contract") if isinstance(peek, dict) else None
            if contract is not None and contract not in (C1_CONTRACT, WITHDRAW_CONTRACT):
                # Receipts or /1 copies in the same outbox are not inputs to this receiver.
                transport["skipped"] += 1
                continue
            if contract is None:
                try:
                    export_contract.parse_receipt(raw)  # legacy receipts imported into the outbox carry no contract
                    transport["skipped"] += 1
                    continue
                except export_contract.ContractError:
                    pass
            withdraw = contract == WITHDRAW_CONTRACT
            try:
                if withdraw:
                    receipts.append(receiver.withdraw_wire(raw))
                    transport["deleted"] += 1
                else:
                    receipts.append(receiver.ack(receiver.receive_wire(raw)))
                    transport["accepted"] += 1
                continue
            except C1ContractError as e:
                if e.code.startswith(_RECEIVER_FAULTS):
                    raise export_contract.ContractError(e.code) from e
                code = e.code
            if eid is None:
                transport["unidentified"] += 1
            elif withdraw:
                transport["delete_failed"] += 1
                receipts.append({"contract": RECEIPT_CONTRACT, "kind": "delete",
                                 "envelope_id": eid, "deleted": False, "deleted_at": time.time()})
            else:
                transport["rejected"] += 1
                receipt = {"contract": RECEIPT_CONTRACT, "kind": "receive", "status": "rejected",
                           "envelope_id": eid, "reasons": [code], "rejected_at": time.time()}
                claimed = peek.get("records_sha256")
                if isinstance(claimed, str) and re.fullmatch(r"[0-9a-f]{64}", claimed):
                    receipt["records_sha256"] = claimed
                receipts.append(receipt)
        bundle = "".join(export_contract.canonical_text(export_contract.parse_receipt(r)) + "\n" for r in receipts)
        _write_new_receipts(args.receipts_out, bundle)
    return {"transport": dict(transport), "receipts": len(receipts),
            "expired": expiry["expired"],
            "receiver": receiver.diagnostics(), "binding": "synthetic_reference_receiver"}


def _read_records(path):
    """Load export.jsonl lines, refusing any line that is not a JSON object."""
    records = [json.loads(line) for line in
               Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not all(isinstance(r, dict) for r in records):
        raise export_contract.ContractError("records_invalid")
    return records


def _build_parser():
    """The ext_contract argparse tree (one subparser per action)."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="action", required=True)
    for action in ("deliver", "handoff", "reconcile", "withdraw", "health"):
        command = commands.add_parser(action)
        command.add_argument("--state", required=action != "handoff",
                             help="explicit local journal/audit dir")
        if action in ("deliver", "handoff", "health"):
            command.add_argument("--auth", required=action == "deliver",
                                 help="mcs-ext-auth/1 authorization file")
        if action in ("deliver", "handoff"):
            source = command.add_mutually_exclusive_group(required=action == "deliver")
            source.add_argument("--records", help="existing export.jsonl")
            source.add_argument("--snapshot", help="published static snapshot, requires --c1")
            command.add_argument("--c1", action="store_true", help="explicit export/2")
            command.add_argument("--only-with-facts", action="store_true")
            command.add_argument("--since-days", type=int)
            command.add_argument("--classify-senders", action="store_true",
                                 help="opt-in sender_kind from self ID/explicit orgs/profession")
            command.add_argument("--signal-limit", type=int, default=200)
            command.add_argument("--max-bytes", type=int, default=MAX_WIRE_BYTES)
            command.add_argument("--dry-run", action="store_true")
        command.add_argument("--sink", required=action not in ("health", "handoff"),
                             help="explicit local outbox or fake consumer directory")
        if action in ("deliver", "reconcile", "withdraw"):
            command.add_argument("--sink-kind", choices=("local", "handoff"),
                                 default="local" if action == "deliver" else "handoff",
                                 help="local self-acks as a fake consumer; handoff never self-acks")
        if action == "reconcile":
            target = command.add_mutually_exclusive_group(required=True)
            target.add_argument("--envelope-id")
            target.add_argument("--all", action="store_true")
            target.add_argument("--receipts", help="manual receipt JSON, NDJSON or directory")
        if action == "withdraw":
            target = command.add_mutually_exclusive_group(required=True)
            target.add_argument("--envelope-id")
            target.add_argument("--generation", help="every journaled envelope of one generation")
            command.add_argument("--reason", default="operator_request",
                                 choices=WITHDRAW_REASONS)
        if action == "health":
            command.add_argument("--max-entries", type=int, default=1000,
                                 help="bounded journal scan, 1..10000")
    receive = commands.add_parser(
        "receive", help="synthetic /2 reference receiver; transport receipts are not currentness")
    receive.add_argument("--receiver-root", required=True, help="dedicated private directory")
    receive.add_argument("--source-label", required=True)
    receive.add_argument("--input", help="handed-over envelope/directive file or outbox directory")
    receive.add_argument("--receipts-out", help="new NDJSON receipt bundle, required with --input")
    hints = commands.add_parser("link-hints", help="Hermes local terminal only, no export")
    hints.add_argument("--snapshot", help="published static snapshot")
    hints.add_argument("--limit", type=int, default=200)
    hints.add_argument("--cursor")
    return ap


def _print_json(res) -> None:
    print(json.dumps(res, ensure_ascii=False, allow_nan=False))


def _cmd_link_hints(args, ap, legacy) -> int:
    """Local Hermes terminal only: paged patient ids/names, no export."""
    from mcs_runtime import mode
    from mcs_util import HOME, load_config
    from mcs_view import View
    if mode(load_config()) != "hermes" or not sys.stdout.isatty():
        raise export_contract.ContractError("link_hints_local_terminal_required")
    view = View(args.snapshot or str(
        Path(HOME) / "data" / "snapshots" / "ledger-snapshot.db"))
    try:
        page = view.local_link_hints(limit=args.limit, cursor=args.cursor)
        items = page["items"]
        assert isinstance(items, list)
        for row in items:
            timestamp = row["last_message_at"]
            if not isinstance(timestamp, int | float) \
                    or not finite_number(timestamp) or timestamp <= 0:
                row["last_message_at"] = None
        res = {**page, "snapshot": view.meta, "binding": "local_hint_only"}
        _print_json(res)
        return 0
    finally:
        view.close()


def _cmd_receive(args, ap, legacy) -> int:
    res = _receive(args)
    _print_json(res)
    return 0 if set(res["transport"]) <= {"accepted", "deleted", "skipped"} else 2


def _cmd_health(args, ap, legacy) -> int:
    res = export_delivery.health(args.state, auth_path=args.auth, sink_root=args.sink,
                 max_entries=args.max_entries)
    _print_json(res)
    return 0 if res["status"] == "ok" else 2


def _cmd_handoff(args, ap, legacy) -> int:
    """handoff with no explicit inputs fills them from config ext_export."""
    if not any((args.auth, args.state, args.sink, args.records, args.snapshot)):
        from mcs_util import HOME, load_config
        profile = load_config().get("ext_export")
        if not isinstance(profile, dict) or set(profile) != {
                "auth", "state_dir", "outbox", "since_days"}:
            raise export_contract.ContractError("handoff_profile_required")
        for key in ("auth", "state_dir", "outbox"):
            if not isinstance(profile[key], str) or not Path(profile[key]).is_absolute():
                raise export_contract.ContractError("handoff_profile_path_invalid")
        if type(profile["since_days"]) is not int or profile["since_days"] < 1:
            raise export_contract.ContractError("handoff_profile_window_invalid")
        args.auth, args.state, args.sink = (
            profile["auth"], profile["state_dir"], profile["outbox"])
        args.snapshot = str(Path(HOME) / "data" / "snapshots" / "ledger-snapshot.db")
        args.c1, args.only_with_facts = True, True
        if args.since_days is None:
            args.since_days = profile["since_days"]
    if not all((args.auth, args.state, args.sink, args.records or args.snapshot)):
        ap.error("handoff requires --auth, --state, --sink and one source")
    return _cmd_deliver(args, ap, legacy)


def _c1_envelopes(args, auth):
    """export/2 selection + envelopes from a snapshot or export.jsonl."""
    from c1_records import SnapshotError, assemble_records, select_records
    for directory in (args.state, args.sink):
        path = Path(directory)
        if not path.is_absolute() or path.is_symlink():
            raise export_contract.ContractError("c1_private_directory_required")
        if path.exists():
            st = path.stat()
            if not path.is_dir() or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise export_contract.ContractError("c1_private_directory_required")
    try:
        if args.snapshot:
            from mcs_view import View
            view = View(args.snapshot)
            try:
                policy = None
                if args.classify_senders:
                    from mcs_util import load_config
                    from self_identity import self_sender_id
                    orgs = (load_config().get("signals") or {}).get(
                        "self_organizations")
                    policy = {"self_sender_id": self_sender_id(view.db),
                              "self_organizations": [
                                  o for o in orgs if isinstance(o, str) and o]
                              if isinstance(orgs, list) else []}
                records = assemble_records(
                    view.db, signal_limit=args.signal_limit,
                    sender_policy=policy)["records"]
            finally:
                view.close()
        else:
            records = _read_records(args.records)
        selection = select_records(
            records, auth["fields"], only_with_facts=args.only_with_facts,
            since_days=args.since_days)
        envelopes = build_envelopes(
            selection["records"], profile={k: auth[k] for k in (
                "fields", "patients", "max_snapshot_age_s", "retention_days")},
            auth_id=auth["auth_id"], destination=auth["destination"],
            purpose=auth["purpose"], now=time.time(), max_bytes=args.max_bytes)
    except (C1ContractError, SnapshotError) as e:
        raise export_contract.ContractError(e.code) from e
    return envelopes, selection


def _sink_for(args, kind):
    return export_delivery.HandoffSink(args.sink) if kind == "handoff" else export_delivery.LocalSink(args.sink)


def _journal_exit(args, res, results, kind, legacy) -> int:
    """Print the result and map journal statuses to the exit code."""
    _print_json(res)
    statuses = {result["status"] for result in results}
    if not statuses or statuses - {
            "acked", "already_acked", "withdrawn", "held", "delete_held"}:
        return 1
    if statuses & {"held", "delete_held"}:
        return 0 if args.action in ("deliver", "handoff") and kind == "handoff" else \
            1 if legacy else 2
    return 0


def _cmd_deliver(args, ap, legacy) -> int:
    selection = None
    if not args.c1 and (args.snapshot or args.only_with_facts
                       or args.since_days is not None or args.signal_limit != 200
                       or args.max_bytes != MAX_WIRE_BYTES or args.classify_senders):
        raise export_contract.ContractError("c1_option_required")
    if args.classify_senders and not args.snapshot:
        raise export_contract.ContractError("classify_senders_snapshot_required")
    auth = export_delivery.load_authorization(args.auth, c1=args.c1)
    if args.c1:
        envelopes, selection = _c1_envelopes(args, auth)
    else:
        records = _read_records(args.records)
        snap = next((r.get("snapshot") for r in records if r.get("type") == "meta"), None)
        gen_at = snap.get("generated_at") if isinstance(snap, dict) else None
        envelopes = [export_contract.build_envelope(records, auth, gen_at)]
    if args.dry_run:
        res: dict[str, object] = {"status": "dry_run", "envelopes": [{
            "envelope_id": e["envelope_id"], "part": e.get("part"),
            "records": e["record_count"],
            "bytes": len(encode_envelope(e)) if args.c1
            else len(export_contract.canonical_text(e).encode("utf-8"))} for e in envelopes]}
        if selection is not None:
            res.update({k: selection[k] for k in ("dropped_types", "messages_dropped")})
        _print_json(res)
        return 0
    kind = "handoff" if args.action == "handoff" else args.sink_kind
    sink = _sink_for(args, kind)
    exporter = export_delivery.GovernedExporter(args.state)
    results = [exporter.deliver(e, sink, auth_path=args.auth) for e in envelopes]
    if args.c1:
        assert selection is not None
        res = {"envelopes": [{
            **result, "part": e.get("part"), "records": e["record_count"],
            "bytes": len(encode_envelope(e))}
            for e, result in zip(envelopes, results)]}
        res.update({k: selection[k] for k in ("dropped_types", "messages_dropped")})
        kept = sum(r["type"] == "message" for r in selection["records"])
        res["messages_kept"] = kept
        res["warnings"] = ["messages_kept_zero"] if kept == 0 else []
    else:
        res = results[0]
    return _journal_exit(args, res, results, kind, legacy)


def _cmd_withdraw(args, ap, legacy) -> int:
    if args.envelope_id is not None:
        export_contract.validate_envelope_id(args.envelope_id)
    kind = args.sink_kind
    sink = _sink_for(args, kind)
    exporter = export_delivery.GovernedExporter(args.state)
    if args.generation is not None:
        results = exporter.withdraw_generation(args.generation, sink, args.reason)
        res = {"results": results}
    else:
        res = exporter.withdraw(args.envelope_id, sink, args.reason)
        results = [res]
    return _journal_exit(args, res, results, kind, legacy)


def _cmd_reconcile(args, ap, legacy) -> int:
    if args.envelope_id is not None:
        export_contract.validate_envelope_id(args.envelope_id)
    kind = args.sink_kind
    sink = _sink_for(args, kind)
    exporter = export_delivery.GovernedExporter(args.state)
    if args.receipts is not None:
        results = exporter.import_receipts(args.receipts, sink)
        res = {"results": results}
    elif args.all:
        results = [exporter.reconcile(path.stem, sink)
                   for path in sorted(export_delivery.journal_paths(args.state))
                   if re.fullmatch(r"[0-9a-f]{24}\.json", path.name)]
        res = {"results": results}
    else:
        res = exporter.reconcile(args.envelope_id, sink)
        results = [res]
    return _journal_exit(args, res, results, kind, legacy)


_COMMANDS = {
    "deliver": _cmd_deliver, "handoff": _cmd_handoff, "reconcile": _cmd_reconcile,
    "withdraw": _cmd_withdraw, "health": _cmd_health, "receive": _cmd_receive,
    "link-hints": _cmd_link_hints,
}


def main(argv=None) -> int:
    ap = _build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    legacy = bool(argv and argv[0].startswith("--") and argv[0] != "--help")
    if legacy:
        argv.insert(0, "deliver")
    args = ap.parse_args(argv)
    try:
        return _COMMANDS[args.action](args, ap, legacy)
    except (export_contract.ContractError, OSError, ValueError, TypeError, RecursionError) as e:
        reason = str(e) if isinstance(e, export_contract.ContractError) else type(e).__name__
        print(json.dumps({"status": "refused", "reason": reason},
                         ensure_ascii=False))
        return 1

if __name__ == "__main__":
    sys.exit(main())
