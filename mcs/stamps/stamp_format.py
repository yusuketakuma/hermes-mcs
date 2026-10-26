"""Format stored stamp counts, actor names and observation states without I/O."""
from __future__ import annotations

import unicodedata
from datetime import datetime

from mcs_queries import JST
from message_observations import observation_timestamp
from stamp_contract import STAMP_EMOJI

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


ACTOR_NAMES_MAX = 12


def _actor_text(value, cap) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    text = "".join(c for c in text if unicodedata.category(c)[0] != "C").strip()
    text = text.translate(str.maketrans("<>@`*_~|", "＜＞＠｀＊＿～｜"))
    return text if len(text) <= cap else text[:cap - 1] + "…"


def actor_line(summary) -> str:
    """投稿ごとのスタンプ氏名・取得状態・観測日時を上限付きの1行で示す。"""
    summary = summary or {}
    observed_at = observation_timestamp(summary.get("complete_at"))
    state = summary.get("state") or ""
    if observed_at is None:
        return "押した人: 未取得" + ("（取得失敗）" if state == "failed" else "")
    groups: dict = {}
    for a in summary["actors"]:
        label = _actor_text(a["name"], 40) or "氏名不明"
        profession = _actor_text(a["profession"], 20)
        if profession:
            label += f"（{profession}）"
        if a["self"]:
            label += "（自分）"
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
    status = "" if state == "complete" else "（" + {
        "stale": "古い情報", "failed": "再取得失敗"}.get(state, "取得状態不明") + "）"
    when = datetime.fromtimestamp(observed_at, JST)
    return (f"押した人{status}: " + (" / ".join(parts) or "なし")
            + f" · 観測 {when:%m-%d %H:%M}")


def thread_stamp_line(metadata, summary=None, *, observed_time=True) -> str:
    """スレッド投稿の1行: 絵文字ごとの件数と（取得済みなら）押した人、観測日時。"""
    counts = stamp_counts(metadata)
    failed = "・再取得失敗" if metadata["last_error"] else ""
    if counts is None:
        return "スタンプ 未取得" + failed
    summary = summary or {}
    names: dict = {}
    if observation_timestamp(summary.get("complete_at")) is not None:
        for a in summary.get("actors") or []:
            if a.get("self") and metadata.get("own_post"):
                continue          # one's own post counts others only
            names.setdefault(STAMP_EMOJI.get(a["reaction_type"], "❔"), []).append(
                _actor_text(a["name"], 40) or "氏名不明")
    parts, shown = [], 0
    for e, n in counts.items():
        who = names.get(e, [])
        take = who[:max(0, ACTOR_NAMES_MAX - shown)]
        shown += len(take)
        part = f"{e}{n}"
        if take:
            part += " " + "・".join(take) + (
                f" 他{len(who) - len(take)}名" if len(who) > len(take) else "")
        parts.append(part)
    # one's own stamp is not listed (owner 2026-10-26): the stamp menu shows it
    text = "スタンプ " + ((" / " if shown else " ").join(parts) or "なし")
    state = summary.get("state")
    if counts and (state == "failed" or (state == "stale" and observed_time)):
        text += " · 押した人 " + ("取得失敗" if state == "failed" else "古い情報")
    if observed_time:
        when = datetime.fromtimestamp(metadata["reactions_observed_at"], JST)
        text += f" · {when:%m-%d %H:%M}時点"
    if not observed_time:
        text += " · 投稿時点"
    return text + failed


def others_reaction_count(metadata) -> int | None:
    """本人分を除いた反応件数。未取得・不正は None（0件と区別する）。"""
    if metadata["reactions"] is None:
        return None
    return sum(max(0, r["count"] - r["self_reacted"]) for r in metadata["reactions"])
