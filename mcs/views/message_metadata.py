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


def _valid_reactions(value) -> bool:
    return isinstance(value, list) and not any(
        not isinstance(r, dict) or not isinstance(r.get("type"), str)
        or not r["type"] or type(r.get("count")) is not int or r["count"] < 0
        or type(r.get("self_reacted")) is not bool for r in value)


def _valid_mentions(value) -> bool:
    return isinstance(value, list) and not any(
        not isinstance(m, dict) or m.get("type") not in ("user", "station", "project")
        or type(m.get("id")) is not int or not 0 < m["id"] < 2**63 for m in value)


# 保存キー -> (検証, 結果の状態キー)。未取得・不正の値は None のまま返す。
_FIELDS = {"reactions": (_valid_reactions, "reactions_status"),
           "mentions": (_valid_mentions, "mentions_status"),
           "is_bookmarked": (lambda v: type(v) is bool, "bookmark_status"),
           "is_pinned": (lambda v: type(v) is bool, "pin_status")}


def _read_metadata(db, mid, source) -> dict:
    result = {"reactions": None, "reactions_status": "not_fetched",
              "reactions_observed_at": None, "checked_at": None,
              "last_error": None}
    for key, (_, status) in _FIELDS.items():
        result.setdefault(key, None)
        result[status] = "not_fetched"
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
        content = None
    errors = str(error).split(",") if error else []
    for key, (valid, status) in _FIELDS.items():
        field = content.get(key) if isinstance(content, dict) else None
        if not isinstance(content, dict):
            result[status] = "invalid"
            continue
        if field is None:
            if key + "_invalid" in errors:
                result[status] = "invalid"
            continue
        value = field.get("value") if isinstance(field, dict) else None
        observed = (_timestamp(field.get("observed_at"))
                    if isinstance(field, dict) else None)
        if observed is None or not valid(value):
            result[status] = "invalid"
            continue
        result[key], result[status] = value, "observed"
        if key == "reactions":
            result["reactions_observed_at"] = observed
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


def own_post_reaction_text(metadata) -> str:
    """自分の投稿への他者反応を種別ごとの件数で表示する（氏名・押下者は出さない）。"""
    reactions = metadata["reactions"]
    if reactions is None:
        return "MCS: スタンプ未取得"
    when = datetime.fromtimestamp(metadata["reactions_observed_at"], JST)
    others: dict = {}
    for r in reactions:
        label = reaction_label(r["type"])
        others[label] = others.get(label, 0) + max(0, r["count"] - r["self_reacted"])
    value = "/".join(f"{k}{n}" for k, n in others.items() if n) or "他者0件"
    return (f"MCS: 自分の投稿への反応 {value}（観測 {when:%m-%d %H:%M} JST）"
            + ("・再取得失敗" if metadata["last_error"] else ""))


def others_reaction_count(metadata) -> int | None:
    """本人分を除いた反応件数。未取得・不正は None（0件と区別する）。"""
    if metadata["reactions"] is None:
        return None
    return sum(max(0, r["count"] - r["self_reacted"]) for r in metadata["reactions"])


def mentions_self(metadata, self_id) -> bool | None:
    """本人の送信者ID宛のuserメンションがあるか。未取得・不正・本人ID不明は None。"""
    if metadata["mentions"] is None or self_id is None:
        return None
    return any(m["type"] == "user" and m["id"] == self_id
               for m in metadata["mentions"])


def self_mentioned(db, mid) -> bool | None:
    """captureのメンションが本人宛か（True/False）、判定できなければ None。"""
    import mcs_signals
    return mentions_self(get_message_metadata(db, mid), mcs_signals.self_sender_id(db))


def flag_lines(db, metadata) -> list:
    """メンション・しおり・ピン留めの取得状態付き表示（CLI/evidence用）。"""
    def state(value, status, yes="あり", no="なし"):
        return (yes if value else no) if status == "observed" else (
            "取得不正" if status == "invalid" else "未取得")
    import mcs_signals
    hit = mentions_self(metadata, mcs_signals.self_sender_id(db))
    mention = (state(hit, "observed", "本人宛あり", "本人宛なし") if hit is not None
               else "本人判定不可（本人ID不明）"
               if metadata["mentions_status"] == "observed"
               else state(None, metadata["mentions_status"]))
    return [f"メンション: {mention}",
            "しおり: " + state(metadata["is_bookmarked"], metadata["bookmark_status"]),
            "ピン留め: " + state(metadata["is_pinned"], metadata["pin_status"])]


def is_self_sender(db, sender_id) -> bool:
    """氏名の一致ではなく本人の送信者IDとの一致で判定する。"""
    import mcs_signals
    return mcs_signals.is_self_message(db, sender_id)
