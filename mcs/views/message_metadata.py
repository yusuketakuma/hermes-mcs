"""Present non-stamp post flags and resolve their existing self-identity checks."""
from __future__ import annotations


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
    import self_identity
    hit = mentions_self(metadata, self_identity.self_sender_id(db))
    mention = (state(hit, "observed", "本人宛あり", "本人宛なし") if hit is not None
               else "本人判定不可（本人ID不明）"
               if metadata["mentions_status"] == "observed"
               else state(None, metadata["mentions_status"]))
    return [f"メンション: {mention}",
            "しおり: " + state(metadata["is_bookmarked"], metadata["bookmark_status"]),
            "ピン留め: " + state(metadata["is_pinned"], metadata["pin_status"])]


def is_self_sender(db, sender_id) -> bool:
    """氏名の一致ではなく本人の送信者IDとの一致で判定する。"""
    import self_identity
    return self_identity.is_self_message(db, sender_id)
