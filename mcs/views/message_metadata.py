"""取得済み投稿のスタンプを観測日時と取得状態付きで読み取る。"""
from __future__ import annotations

import json
import re
from datetime import datetime

from mcs_queries import JST

REACTION_LABELS = {"viewed": "見ました", "accepted": "承知", "thanked": "感謝",
                   "good": "いいね", "completed": "完了"}


def _timestamp(value):
    return (value if type(value) in (int, float)
            and 0 < value < 253402214400 else None)


def reaction_label(kind):
    """公式の既知種別を表示し、将来種別は値を埋め込まず未知と表示する。"""
    return REACTION_LABELS.get(kind, "未知")


def _read_metadata(db, mid, source) -> dict:
    result = {"reactions": None, "reactions_status": "not_fetched",
              "reactions_observed_at": None, "checked_at": None,
              "last_error": None}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                      "AND name='message_metadata'").fetchone():
        return result
    row = db.execute("SELECT content,checked_at,last_error FROM message_metadata "
                     "WHERE message_id=? AND source=?", (mid, source)).fetchone()
    if row is None:
        return result
    raw, checked, error = row
    result["checked_at"] = _timestamp(checked)
    if error:
        result["last_error"] = (error if isinstance(error, str)
                                and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error)
                                else "metadata_error")
    try:
        content = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        result["reactions_status"] = "invalid"
        return result
    field = content.get("reactions") if isinstance(content, dict) else None
    if field is None:
        if result["last_error"] and ("reactions_invalid" in str(error).split(",")):
            result["reactions_status"] = "invalid"
        return result
    reactions = field.get("value") if isinstance(field, dict) else None
    observed = _timestamp(field.get("observed_at")) if isinstance(field, dict) else None
    if not isinstance(reactions, list) or observed is None or any(
            not isinstance(r, dict) or not isinstance(r.get("type"), str)
            or not r["type"] or type(r.get("count")) is not int or r["count"] < 0
            or type(r.get("self_reacted")) is not bool for r in reactions):
        result["reactions_status"] = "invalid"
        return result
    result.update(reactions=reactions, reactions_status="observed",
                  reactions_observed_at=observed)
    return result


def _observation_age(metadata, as_of):
    observed = metadata["reactions_observed_at"]
    return (max(0, as_of - observed) if observed is not None
            and _timestamp(as_of) is not None else None)


def get_message_metadata(db, mid, *, as_of=None) -> dict:
    """capture のみを読み、未取得と空配列を区別する（旧snapshotにも対応）。"""
    result = _read_metadata(db, mid, "capture")
    if as_of is not None:
        result.update(
            source="capture", age_as_of=_timestamp(as_of),
            reactions_age_s=_observation_age(result, as_of),
            freshness_note="通常収集時の観測です。古い投稿の反応更新には遅延があり、"
                           "shadowの再取得結果はこの表示に反映しません。")
    return result


def get_metadata_shadow_status(db, mid, *, as_of=None) -> dict:
    """shadowの取得状態と日時だけを返し、反応の値は公開しない。"""
    metadata = _read_metadata(db, mid, "shadow")
    state = ("failed" if metadata["last_error"] else
             "invalid" if metadata["reactions_status"] == "invalid" else
             "observed" if metadata["reactions_observed_at"] is not None else
             "checked_without_reactions" if metadata["checked_at"] is not None else
             "not_attempted")
    return {"mode": "shadow", "state": state, "displayed": False,
            "checked_at": metadata["checked_at"],
            "reactions_observed_at": metadata["reactions_observed_at"],
            "reactions_age_s": _observation_age(metadata, as_of),
            "age_as_of": _timestamp(as_of), "last_error": metadata["last_error"]}


def self_reaction_text(metadata) -> str:
    """本人のスタンプまたは取得状態を表示し、観測日時を押下時刻と区別する。"""
    reactions = metadata["reactions"]
    if reactions is None:
        return "MCS: スタンプ未取得"
    when = datetime.fromtimestamp(metadata["reactions_observed_at"], JST)
    labels = list(dict.fromkeys(reaction_label(r["type"]) for r in reactions
                                if r["self_reacted"]))
    value = ("本人 " + "/".join(labels) if labels else
             "スタンプ0件" if not reactions else "本人反応は記録されていません")
    return (f"MCS: {value}（観測 {when:%m-%d %H:%M} JST）"
            + ("・再取得失敗" if metadata["last_error"] else ""))


def is_self_sender(db, sender_id) -> bool:
    """氏名の一致ではなく本人の送信者IDとの一致で判定する。"""
    import mcs_signals
    return mcs_signals.is_self_message(db, sender_id)
