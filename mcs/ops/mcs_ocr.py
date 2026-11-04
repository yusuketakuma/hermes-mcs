"""添付の文字起こしを独立して処理し、チャット原文とは分けて保存する。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401

import attachment_ocr  # noqa: E402
from ledger import Ledger  # noqa: E402
from mcs_util import DB, load_config  # noqa: E402
import writer_lock  # noqa: E402
from writer_lock import acquire_run_lock  # noqa: E402

DATA = str(Path(DB).parent)


def run(cfg):
    """Return aggregate status, retaining the ingestion lock only while opening the ledger."""
    if attachment_ocr.config_error(cfg):
        return {"reason": "config_invalid"}
    if not cfg.get("attachment_ocr", {}).get("enabled", False):
        return {"reason": "disabled"}
    if not Path(DB).is_file():
        return {"reason": "database_unavailable"}
    generation = writer_lock.updater_state()
    if generation[0]:
        return {"reason": "writer_blocked"}
    fd = acquire_run_lock(os.path.join(DATA, "run.lock"))
    if fd is None:
        return {"reason": "writer_busy"}
    ledger = None
    try:
        if writer_lock.updater_state() != generation:
            return {"reason": "writer_blocked"}
        ledger = Ledger(DB)
    finally:
        os.close(fd)
    try:
        return attachment_ocr.run_pending(ledger, cfg, deadline=time.monotonic() + 125, limit=1)
    finally:
        ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True,
                        help="設定で有効にした添付OCRを1件まで実行する")
    parser.parse_args()
    try:
        result = run(load_config())
    except (OSError, ValueError, sqlite3.Error):
        result = {"reason": "local_processing_failed"}
    print(json.dumps({"attachment_ocr": result}, ensure_ascii=False))
    return 0 if result.get("reason") in (None, "disabled", "writer_busy", "busy") else 1


if __name__ == "__main__":
    raise SystemExit(main())
