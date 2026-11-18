#!/usr/bin/env python3
"""Create a completely synthetic snapshot for the local patient chat demonstration."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))
import _mcs_path  # noqa: E402,F401
from ledger import Ledger, publish_snapshot
from mcs_contract import Message
from patient_context import extract_context
from inference_version import CURRENT_VERSION, VERSION_KEY


def create(output):
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    path = output / "synthetic-ledger.db"
    store = Ledger(str(path))
    try:
        with store.db:
            for pid, name in ((101, "デモ 花子"), (102, "サンプル 太郎"), (103, "テスト さくら")):
                store.db.execute("INSERT INTO patients(project_id,patient_name,fetch_state,is_archived,history_floor) "
                                 "VALUES(?,?,'incomplete',0,NULL)", (pid, name))
        rows = [
            (1, 101, None, "2026-11-16T09:00:00+09:00", "訪問看護 デモ", "看護師", "病名: 合成デモ疾患A\nADL: 歩行は見守り\n生活環境: 長女と同居\nアレルギー: なし\n訪問しました。体温36.5℃。朝食は全量摂取されています。\n水分摂取の状況を引き続き確認します。", 0),
            (2, 101, None, "2026-11-17T10:20:00+09:00", "薬局 サンプル", "薬剤師", "服薬カレンダーを確認しました。飲み忘れの記録はありません。\n次回訪問時に残薬を確認予定です。", 1),
            (3, 101, 2, "2026-11-17T11:05:00+09:00", "訪問看護 デモ", "看護師", "共有ありがとうございます。次回の訪問で服薬状況を確認します。", 0),
            (4, 101, None, "2026-11-18T09:15:00+09:00", "ケア担当 テスト", "介護支援専門員", "ご家族より、今週の訪問予定について相談がありました。\n金曜日の午後を希望されています。", 2),
            (5, 101, 4, "2026-11-18T09:45:00+09:00", "訪問看護 デモ", "看護師", "金曜日14時で調整しています。確定しましたら共有します。", 0),
            (6, 101, 4, "2026-11-18T10:10:00+09:00", "薬局 サンプル", "薬剤師", "同日の訪問で調整可能です。", 0),
            (7, 101, None, "2026-11-18T13:30:00+09:00", "訪問看護 デモ", "看護師", "本日の訪問記録を添付しました。\n体温36.6℃、食欲に変化なし。室内を歩行されています。", 0),
            (8, 102, None, "2026-11-18T10:00:00+09:00", "薬局 サンプル", "薬剤師", "病名: 合成デモ疾患B\nADL: 自立\nサンプル太郎さんの合成記録です。お薬の受け取り予定を確認しました。", 0),
            (9, 103, None, "2026-11-18T11:00:00+09:00", "ケア担当 テスト", "介護支援専門員", "テストさくらさんの合成記録です。次週のサービス予定を確認中です。", 0),
        ]
        messages = [Message(mid, pid, parent, 100 + mid % 3, sender, "staff", profession,
                            "合成デモ事業所", stamp, text.replace("\n", "<br>"), "full", False, replies)
                    for mid, pid, parent, stamp, sender, profession, text, replies in rows]
        store.save_messages(messages)
        with store.db:
            store.db.execute("INSERT INTO attachments(message_id,name,state,bytes,error) "
                             "VALUES(7,'合成_訪問記録.pdf','failed',0,'download_failed')")
            for message in messages:
                store.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=?",
                                 (int(datetime.fromisoformat(message.posted_at).timestamp()), message.message_id))
                row = store.db.execute("SELECT body_text,content_hash FROM messages WHERE message_id=?",
                                       (message.message_id,)).fetchone()
                store.artifact_add_tx("extract_v1", json.dumps(
                    {"patient_context": extract_context(row[0])}, ensure_ascii=False),
                    project_id=message.project_id, message_id=message.message_id,
                    meta={"hash": row[1], VERSION_KEY: CURRENT_VERSION})
        return publish_snapshot(str(path), str(output / "snapshots"))
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory; existing paths are refused")
    print(create(parser.parse_args().output))
