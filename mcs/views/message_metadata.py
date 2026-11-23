"""Present non-stamp post flags and resolve their existing self-identity checks."""
from __future__ import annotations


def mentions_self(metadata, self_id) -> bool | None:
    """本人の送信者ID宛のuserメンションがあるか。未取得・不正・本人ID不明は None。"""
    if metadata["mentions"] is None or self_id is None:
        return None
    return any(m["type"] == "user" and m["id"] == self_id
               for m in metadata["mentions"])


_UNSET = object()      # an omitted preloaded self_sender_id; None means "self unknown"


# One wording for @self / pin / bookmark and their states, shared by the evidence lines and the workspace.
FLAG_LABELS = (("mention_self", "@自分宛て"), ("pinned", "ピン留め"), ("bookmarked", "ブックマーク"))
FLAG_STATE_WORDS = {"not_fetched": "未取得", "failed": "取得失敗", "invalid": "読み取れません",
                    "self_unknown": "自分のアカウントを特定できません"}
UNKNOWN_FLAG_STATE = "状態不明"


def flags(metadata, self_id) -> dict:
    """@self, pin and bookmark: value true/false only when observed, otherwise null with the state."""
    hit = mentions_self(metadata, self_id)
    mention = ("observed" if hit is not None else
               "self_unknown" if metadata["mentions_status"] == "observed" else metadata["mentions_status"])

    def observed(value, status):
        return {"value": value if status == "observed" else None, "state": status}
    return {"mention_self": {"value": hit, "state": mention},
            "pinned": observed(metadata["is_pinned"], metadata["pin_status"]),
            "bookmarked": observed(metadata["is_bookmarked"], metadata["bookmark_status"])}


def flag_state_text(flag) -> str:
    """あり / なし when observed, else the plain state word (未取得・取得失敗・読み取れません …)."""
    if flag.get("state") == "observed" and type(flag.get("value")) is bool:
        return "あり" if flag["value"] else "なし"
    return FLAG_STATE_WORDS.get(flag.get("state"), UNKNOWN_FLAG_STATE)


def flag_lines(db, metadata, *, self_id=_UNSET) -> list:
    """@自分宛て・ピン留め・ブックマークの取得状態付き表示（CLI/evidence用）。"""
    if self_id is _UNSET:
        import self_identity
        self_id = self_identity.self_sender_id(db)
    shown = flags(metadata, self_id)
    return [f"{label}: {flag_state_text(shown[key])}" for key, label in FLAG_LABELS]


def is_self_sender(db, sender_id, *, self_id=_UNSET) -> bool:
    """氏名の一致ではなく本人の送信者IDとの一致で判定する。"""
    import self_identity
    if self_id is _UNSET:
        return self_identity.is_self_message(db, sender_id)
    return self_identity.is_self_message(db, sender_id, self_id=self_id)
