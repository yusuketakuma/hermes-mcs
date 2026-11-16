"""外部AI送信の施設確認と、患者情報への閲覧・操作記録の確認・改ざん検査を行います。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402, F401
import access_log  # noqa: E402
from cli_args import JapaneseArgumentParser  # noqa: E402
import external_transfer  # noqa: E402
from mcs_util import HOME, safe_error_code  # noqa: E402

JST = timezone(timedelta(hours=9))
OFF_HOURS = (20, 7)  # JST 20:00-06:59 is reviewed as outside usual hours


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _patients_of(entry) -> set:
    ids = {pid for pid in entry.get("patients") or () if type(pid) is int}
    if type(entry.get("project_id")) is int:
        ids.add(entry["project_id"])
    return ids


def review(entries, *, max_patients: int) -> list[dict]:
    """Summarize each actor so a person can spot unusual access without reading every line."""
    actors = defaultdict(lambda: {"records": 0, "denied": 0, "off_hours": 0, "patients": set(),
                                   "widest": 0, "partial": False})
    for entry in entries:
        row = actors[(entry.get("via"), entry.get("actor"))]
        row["records"] += 1
        if entry.get("outcome") != "ok":
            row["denied"] += 1
        hour = datetime.fromtimestamp(entry.get("ts", 0), JST).hour
        if hour >= OFF_HOURS[0] or hour < OFF_HOURS[1]:
            row["off_hours"] += 1
        ids = _patients_of(entry)
        more = entry.get("patients_more") or 0
        row["patients"].update(ids)
        # IDs past the per-record limit are only counted, so they cannot be deduplicated across
        # records: the widest single access is a lower bound, never a sum of overflows.
        row["widest"] = max(row["widest"], len(ids) + more)
        row["partial"] = row["partial"] or more > 0
    result = []
    for (via, actor), row in sorted(actors.items(), key=lambda item: str(item[0])):
        patients = max(len(row["patients"]), row["widest"])
        flags = [name for name, hit in (
            ("many_patients", patients > max_patients),
            ("denied", row["denied"] > 0),
            ("off_hours", row["off_hours"] > 0)) if hit]
        result.append({"via": via, "actor": actor, "records": row["records"],
                       "patients": patients, "patients_at_least": row["partial"],
                       "denied": row["denied"], "off_hours": row["off_hours"], "flags": flags})
    return result


def main(argv=None) -> int:
    parser = JapaneseArgumentParser(prog="mcs_compliance.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="外部AI送信の確認状態と記録の改ざん検査結果を表示する")
    ack = sub.add_parser("acknowledge", help="施設として外部AIへの送信を確認したことを記録する")
    ack.add_argument("--by", required=True, help="確認した責任者の氏名・役職")
    ack.add_argument("--reason", required=True, help="確認の根拠（契約・同意・規程の版など）")
    ack.add_argument("--services", required=True,
                     help="送信を認めるサービス（カンマ区切り）: " + ",".join(external_transfer.SERVICES))
    ack.add_argument("--confirm-human", action="store_true", required=True)
    log = sub.add_parser("access-log", help="閲覧・操作記録を表示・検査する")
    log.add_argument("action", choices=("show", "verify", "review"))
    log.add_argument("--days", type=int, default=30, help="対象期間（日、既定30）")
    log.add_argument("--project", type=int, help="show: 患者（project）IDで絞る")
    log.add_argument("--checkpoint", help="verify: 前回の点検記録に書いた 件数:head（末尾の削除・改変の検出）")
    log.add_argument("--max-patients", type=int, default=20,
                     help="review: この人数を超えて閲覧した利用者に印を付ける")
    args = parser.parse_args(argv)
    data_dir = os.path.join(HOME, "data")
    try:
        if args.command == "status":
            _print({"external_transfer": external_transfer.status(),
                    "access_log": access_log.verify(data_dir)})
            return 0
        if args.command == "acknowledge":
            record = external_transfer.acknowledge(
                by=args.by, reason=args.reason, confirm_human=args.confirm_human,
                services=[name.strip() for name in args.services.split(",") if name.strip()])
            _print({"ok": True, "acknowledged": record["acknowledged"]})
            return 0
        if args.action == "verify":
            checkpoint = None
            if args.checkpoint:
                records, _, head = args.checkpoint.partition(":")
                if not records.isdigit() or len(head) != 64:
                    raise ValueError("checkpoint_invalid")
                checkpoint = (int(records), head)
            result = access_log.verify(data_dir, checkpoint=checkpoint)
            _print(result)
            return 0 if result["ok"] else 1
        since = time.time() - max(args.days, 0) * 86400
        entries = access_log.read(data_dir, since=since)
        if args.action == "show":
            for entry in entries:
                if args.project is None or args.project in _patients_of(entry):
                    _print(entry)
            return 0
        for row in review(entries, max_patients=args.max_patients):
            _print(row)
        return 0
    except (ValueError, OSError, access_log.AccessLogError) as error:
        code = str(error) if isinstance(error, access_log.AccessLogError) else safe_error_code(error)
        _print({"ok": False, "error": code})
        return 1


if __name__ == "__main__":
    sys.exit(main())
