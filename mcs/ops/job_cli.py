"""失敗ジョブの理由と対策を確認し、人の確認後に再試行を予約します。"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import uuid

from cli_args import JapaneseArgumentParser
import cloud_cli
import job_diagnostics
import mcs_requests
import mcs_util


def _config():
    from mcs_config import validate_config
    cfg = mcs_util.load_config_checked()
    if cfg is None or validate_config(cfg)[0]:
        raise ValueError("config_invalid")
    return cfg


def main(argv=None):
    """失敗理由の確認と、診断状態を照合する再試行を行います。"""
    parser = JapaneseArgumentParser(prog="mcs jobs", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    failed = commands.add_parser("failed", help="失敗理由と対策を表示（変更しない）")
    failed.add_argument("--job-id", type=int)
    failed.add_argument("--limit", type=int, default=50)
    retry = commands.add_parser("retry", help="対策を確認したジョブの再試行を予約")
    retry.add_argument("--job-id", type=int, required=True)
    retry.add_argument("--diagnosis-hash", required=True)
    retry.add_argument("--expected-payload-hash", required=True)
    retry.add_argument("--remediation", required=True,
                       choices=("permission", "scope", "source", "budget", "quality", "model", "jev", "investigate"))
    retry.add_argument("--additional-attempts", type=int, choices=(1, 2, 3))
    retry.add_argument("--confirm-human", action="store_true")
    retry.add_argument("--reason", help="調査結果と実施した対策（本文や秘密値を含めない）")
    retry.add_argument("--command-id", help="同じ操作の再確認時には同じUUIDを使用")
    retry.add_argument("--actor", default="local-operator")
    args = parser.parse_args(argv)
    try:
        if args.action == "retry":
            # Reject unconfirmed or malformed input before opening even a reader.
            req = {"version": 1, "cmd": "ops.retry", "command_id": args.command_id or str(uuid.uuid4()),
                   "actor": args.actor, "human_confirmed": args.confirm_human, "project_id": 1,
                   "job_id": args.job_id, "expected_payload_hash": args.expected_payload_hash,
                   "diagnosis_hash": args.diagnosis_hash, "remediation": args.remediation,
                   "reason": args.reason}
            if args.additional_attempts is not None:
                req["additional_attempts"] = args.additional_attempts
            if mcs_requests.validate(req):
                print("再試行には有効な診断情報、--confirm-human、--reason が必要です。")
                return 1
        cfg = _config()
        with cloud_cli._reader() as (db, _deadline):
            if args.action == "failed":
                report = ({"jobs": [job_diagnostics.diagnose_job(db, cfg, args.job_id)], "truncated": False}
                          if args.job_id is not None else job_diagnostics.list_failed(db, cfg, args.limit))
                # Internal project IDs are only needed for the receipt writer.
                for job in report["jobs"]:
                    job.pop("project_id", None)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            row = db.execute("SELECT project_id FROM main.fetch_jobs WHERE job_id=?", (args.job_id,)).fetchone()
            if row is None:
                raise ValueError("job_not_found")
            req["project_id"] = row[0]
        if mcs_requests.validate(req):
            raise ValueError("job_invalid")
        from writer_lock import acquire_run_lock, wait_run_lock, code_stamp
        from ledger import Ledger
        expected_code = code_stamp()
        path = Path(mcs_util.HOME) / "data" / "ledger.db"
        if path.is_symlink() or not path.is_file():
            raise ValueError("ledger_unavailable")
        lock_fd = acquire_run_lock(str(path.parent / "run.lock"))
        if lock_fd is None:
            lock_fd = wait_run_lock(str(path.parent / "run.lock"), wait_s=5.0,
                expected_code_stamp=expected_code, marker_path=str(path.parent / "update_in_progress.marker"))
        if lock_fd is None:
            print("ほかの保存処理が終わってから再試行してください。")
            return 3
        try:
            if (path.parent / "update_in_progress.marker").exists() or code_stamp() != expected_code:
                raise ValueError("update_in_progress")
            # Reload under the writer lock: a changed scope must invalidate the diagnosis.
            cfg = _config()
            with closing(Ledger(str(path))) as ledger:
                receipt = mcs_requests.apply_command(ledger, req, cfg=cfg)
        finally:
            os.close(lock_fd)
        print(json.dumps({key: receipt.get(key) for key in ("command_id", "outcome", "error", "control")},
                         ensure_ascii=False))
        return 0 if receipt["outcome"] == "applied" else 1
    except (OSError, ValueError, sqlite3.Error):
        print("ジョブの状態を確認できません。設定・保存記録を確認してください。本文や認証情報は表示していません。")
        return 1
