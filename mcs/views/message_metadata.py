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
              "last_error": None, "mentions_observed_at": None}
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
        if key + "_invalid" in errors:
            result[status] = "invalid"
            continue
        field = content.get(key) if isinstance(content, dict) else None
        if not isinstance(content, dict):
            result[status] = "invalid"
            continue
        if field is None:
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
        elif key == "mentions":
            result["mentions_observed_at"] = observed
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
        result["response_observation"] = response_observation(
            db, mid, result, as_of=as_of)
    return result


def response_observation(
        db, mid, metadata, *, as_of
) -> dict[str, dict[str, str | float | list[str] | None] | str | None]:
    """保存済み返信と本人のUI操作を別々に示し、業務完了や未対応を推定しない。"""
    import mcs_signals
    self_id = mcs_signals.self_sender_id(db)
    row = db.execute(
        "SELECT project_id,parent_id,posted_at_ts FROM messages WHERE message_id=?",
        (mid,)).fetchone()
    reply_at = None
    time_context = "unknown"
    known = (self_id is not None and row is not None
             and _timestamp(row[2]) is not None and _timestamp(as_of) is not None)
    if known and row is not None:
        root = row[1] or mid
        time_context = "bounded"
        for reply in db.execute(
            "SELECT sender_id,posted_at_ts FROM messages WHERE project_id=? "
            "AND parent_id=? "
            "AND COALESCE(body_state,'')!='deleted' ORDER BY posted_at_ts,message_id",
            (row[0], root)):
            actor = mcs_signals.normalize_sender_id(reply[0])
            at = _timestamp(reply[1])
            if actor is None:
                if at is None or at > row[2]:
                    time_context = "unknown"
                continue
            if actor != self_id:
                continue
            if at is None or at > as_of:
                time_context = "unknown"
            elif at > row[2] and reply_at is None:
                reply_at = at
    if known and reply_at is None and row is not None:
        # Fewer stored replies than the thread reports: an unseen reply may be ours.
        total = db.execute("SELECT reply_count FROM messages WHERE message_id=?",
                           (row[1] or mid,)).fetchone()
        stored = db.execute("SELECT COUNT(*) FROM messages WHERE project_id=? AND parent_id=?",
                            (row[0], row[1] or mid)).fetchone()[0]
        if total is not None and type(total[0]) is int and total[0] > stored:
            known = False
    reactions = metadata["reactions"]
    return {
        "reply": {"state": "observed" if reply_at is not None else
                  "not_observed" if known else "unknown", "posted_at": reply_at},
        "reply_time_context": time_context,
        "self_reaction": {
            "state": "invalid" if metadata["reactions_status"] == "invalid" else
                     "failed" if metadata["last_error"] else metadata["reactions_status"],
            "types": sorted({r["type"] if r["type"] in REACTION_LABELS else "unknown"
                             for r in reactions or [] if r["self_reacted"]}),
            "observed_at": metadata["reactions_observed_at"],
            "checked_at": metadata["checked_at"],
            "age_s": _observation_age(metadata, as_of),
            "current_state": "unknown",
            "basis": "ui_operation_only"},
        "clinical_completion": None,
        "nonresponse": None,
        "unread": None,
    }


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


# MCS stamp -> chat emoji. Unicode stand-ins for the official stamp
# images (the images themselves are not fetched); unknown future types
# stay visible as ❔ rather than being dropped.
STAMP_EMOJI = {"viewed": "👀", "accepted": "🙆", "thanked": "🙏",
               "good": "👍", "completed": "✅"}


def stamp_counts(metadata) -> dict | None:
    """{emoji: count} in STAMP_EMOJI order — for one's own post the
    count excludes one's own stamp. None when unfetched or invalid."""
    reactions = metadata["reactions"]
    if reactions is None:
        return None
    own = metadata.get("own_post")
    out: dict = {}
    for r in sorted(reactions, key=lambda r: list(STAMP_EMOJI).index(r["type"])
                    if r["type"] in STAMP_EMOJI else len(STAMP_EMOJI)):
        n = r["count"] - (1 if own and r["self_reacted"] else 0)
        if n > 0:
            e = STAMP_EMOJI.get(r["type"], "❔")
            out[e] = out.get(e, 0) + n
    return out


def self_stamps(metadata) -> str:
    """Emoji of the stamps one pressed oneself on this post ("" if none)."""
    return "".join(dict.fromkeys(
        STAMP_EMOJI.get(r["type"], "❔")
        for r in metadata["reactions"] or [] if r["self_reacted"]))


def stamp_line(metadata) -> str:
    """One compact line for a single post: emoji counts, own stamps and
    the observation time (never the press time)."""
    counts = stamp_counts(metadata)
    if counts is None:
        return "MCS スタンプ未取得"
    when = datetime.fromtimestamp(metadata["reactions_observed_at"], JST)
    mine = self_stamps(metadata)
    text = "MCS " + (" ".join(f"{e}{n}" for e, n in counts.items())
                     or ("他者なし" if mine else "スタンプなし"))
    if mine:
        text += f"（自分 {mine}）"
    return (text + f" · 観測 {when:%m-%d %H:%M}"
            + ("・再取得失敗" if metadata["last_error"] else ""))


def self_reaction_text(metadata) -> str:
    """既存呼出元にも絵文字と観測日時を持つスタンプ行を返す。"""
    return stamp_line(metadata)


def own_post_reaction_text(metadata) -> str:
    """自投稿の互換入口で本人分を除いたスタンプ行を返す。"""
    return stamp_line(dict(metadata, own_post=True))


ACTOR_NAMES_MAX = 12


def actor_line(summary) -> str | None:
    """Who pressed which stamp (#22-D2: names shown), one line grouped by
    emoji. None until a walk completed once. A stale or failed walk is
    labelled with its last complete time — never presented as current."""
    if not summary or summary.get("complete_at") is None:
        return None
    groups: dict = {}
    for a in summary["actors"]:
        label = "自分" if a["self"] else (a["name"] or "氏名不明")
        if a["profession"] and not a["self"]:
            label += f"（{a['profession']}）"
        groups.setdefault(STAMP_EMOJI.get(a["reaction_type"], "❔"), []).append(label)
    shown, parts = 0, []
    for e in [*STAMP_EMOJI.values(), "❔"]:
        names = groups.get(e, [])
        take = names[:max(0, ACTOR_NAMES_MAX - shown)]
        shown += len(take)
        if take:
            parts.append(f"{e} " + "・".join(take)
                         + (f" 他{len(names) - len(take)}名" if len(names) > len(take) else ""))
        elif names:
            parts.append(f"{e} {len(names)}名")
    text = "押した人: " + (" / ".join(parts) or "なし")
    if summary["state"] != "complete":
        when = datetime.fromtimestamp(summary["complete_at"], JST)
        text += f"（{when:%m-%d %H:%M} 時点）"
    return text


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
