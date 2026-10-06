"""MCS summary (#31) — the daily digest card and its on-demand view.

One display model (``parts``: heading + text sections + footer) feeds the
once-per-JST-day relayed notice and the clicker-only 📊 view on every
transport; ``notify_render.parts_text``/``fit_parts`` absorb each chat's
format and size limits. Counts, ids and (opt-in / private view only)
patient names — never bodies or summaries. Recorded fetch gaps are
always shown. A scope narrows the patients: ``all`` / ``mine`` (open
tasks assigned to the clicker, as recorded) / ``station:<name>`` /
``project:<id,...>`` / ``days:<1-7>``. Off unless
``daily_digest.enabled``; the ``notify_outbox`` row is the durable
once-per-day marker (``payload.date``).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _mcs_path  # noqa: F401,E402  CLI entry: registers every subdir

import mcs_signals  # noqa: E402
import structured_view  # noqa: E402
from mcs_queries import JST, coverage_gaps  # noqa: E402
from message_metadata import (get_message_metadata, mentions_self,  # noqa: E402
                              others_reaction_count, reaction_label,
                              STAMP_EMOJI)
from notify_render import (_patient_name, fit_parts, parts_text,  # noqa: E402
                           plain_notice)
from notify_views import assignee_matches  # noqa: E402

KIND = "daily_digest"
MAX_LIST = 10
DAY_S = 86400
MAX_DAYS = 7
STALE_ALERT_D = 3        # an open alert first detected longer ago
                         # than this and never acked is re-surfaced
NOTICE_BUDGET = 1900     # relayed text notice (one chat message)
# request/deadline-type signals are the task block's business, not the
# candidate count's (design #13: 期限・予定・依頼は含めない)
EXCLUDED_SIGNALS = frozenset({"request_overdue", "request_aging",
                              "rx_period_expiry", "rx_period_lapsed"})
NOTE = ("※ 取得済みの記録から数えた件数です。記録が見つからないことは対応が"
        "なかったことを意味せず、取得完了の記録は欠落なしの保証ではありません。")
SCOPE_HELP = "all / mine / station:名前 / project:ID,ID / days:1-7（空白区切りで組合せ）"


def settings(cfg) -> dict | None:
    """Digest settings from config, or None when the digest is off."""
    d = (cfg or {}).get("daily_digest")
    if not isinstance(d, dict) or d.get("enabled") is not True:
        return None
    hour = d.get("hour_jst", 8)
    scope = d.get("scope", "all")
    return {"hour_jst": hour if type(hour) is int and 0 <= hour <= 23 else 8,
            "include_names": d.get("include_names") is True,
            "scope": scope if isinstance(scope, str) else "all"}


def parse_scope(text) -> dict | str:
    """``all mine station:X project:1,2 days:3`` -> filter dict, or a JA
    error string. Kinds combine with AND; repeated station/project OR."""
    out = {"mine": False, "stations": [], "projects": [], "days": 1}
    for tok in str(text or "").split():
        key, _, val = tok.partition(":")
        key = key.lower()
        if key in ("all", "全体") and not val:
            continue
        if key in ("mine", "担当") and not val:
            out["mine"] = True
        elif key in ("station", "施設") and val:
            out["stations"].append(val[:60])
        elif key in ("project", "患者") and val:
            ids = [int(v) for v in val.split(",")
                   if v.isascii() and v.isdigit()]
            if not ids or len(ids) != len(val.split(",")):
                return f"project の指定が不正です: {tok[:40]}"
            out["projects"] += ids
        elif key in ("days", "日数") and val.isascii() and val.isdigit() \
                and 1 <= int(val) <= MAX_DAYS:
            out["days"] = int(val)
        else:
            return f"絞込みを解釈できません: {tok[:40]}（{SCOPE_HELP}）"
    return out


def _scope_label(flt, name) -> str:
    parts = []
    if flt["mine"]:
        parts.append(f"担当（記録上: {plain_notice(name, 30) or '不明'}）")
    if flt["stations"]:
        parts.append("施設 " + "・".join(plain_notice(s, 30)
                                         for s in flt["stations"]))
    if flt["projects"]:
        parts.append("project " + ",".join(map(str, flt["projects"][:5]))
                     + ("…" if len(flt["projects"]) > 5 else ""))
    return " / ".join(parts) or "全患者"


def scope_projects(db, flt, name=None, allowed=None) -> set | None:
    """Live project ids the filter keeps (None = unrestricted). ``allowed``
    is the caller's project scope and always applies."""
    keep = None if allowed is None else set(allowed)

    def narrow(ids):
        nonlocal keep
        keep = set(ids) if keep is None else keep & set(ids)

    if flt["mine"]:
        narrow(r["project_id"] for r in db.execute(
            "SELECT project_id, assignee FROM requests "
            "WHERE status IN ('open','in_progress')")
            if assignee_matches(r["assignee"], name))
    if flt["stations"]:
        narrow(r["project_id"] for r in db.execute(
            "SELECT project_id, station_name FROM patients")
            if any(s in (r["station_name"] or "") for s in flt["stations"]))
    if flt["projects"]:
        narrow(flt["projects"])
    return keep


def _plain(text) -> str:
    return plain_notice(text, 60)


def _ids(pairs, fmt) -> str:
    shown = ", ".join(fmt(p) for p in pairs[:MAX_LIST])
    return shown + (f" 他{len(pairs) - MAX_LIST}件" if len(pairs) > MAX_LIST
                    else "")


def _self_reaction_count(db, since, until, keep=None) -> tuple:
    """観測窓に入るcaptureの現在の本人反応を投稿単位で数える。"""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                      "AND name='message_metadata'").fetchone():
        return 0, {}
    rows = db.execute(
        "SELECT x.message_id,m.project_id FROM message_metadata x "
        "JOIN messages m ON m.message_id=x.message_id "
        "JOIN patients p ON p.project_id=m.project_id "
        "WHERE x.source='capture' AND json_valid(x.content) "
        "AND json_extract(x.content,'$.reactions.observed_at')>=? "
        "AND json_extract(x.content,'$.reactions.observed_at')<? "
        "AND COALESCE(p.is_archived,0)=0 AND COALESCE(m.body_state,'')!='deleted'",
        (since, until))
    posts, counts = 0, {}
    for row in rows:
        if keep is not None and row["project_id"] not in keep:
            continue
        meta = get_message_metadata(db, row[0])
        labels = {STAMP_EMOJI.get(r["type"], "❔") + reaction_label(r["type"])
                  for r in meta["reactions"] or [] if r["self_reacted"]}
        if not labels:
            continue
        posts += 1
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
    return posts, counts


OWN_POST_DAYS = 7
NOT_RESPONSE_NOTE = ("※ 記録が見つからない≠対応がなかった。メンションだけでは"
                     "応答済みにも未対応にもしません。")


def _ago(seconds) -> str:
    return (f"{int(seconds // DAY_S)}日経過" if seconds >= DAY_S
            else f"{int(seconds // 3600)}時間経過")


def _observed(meta) -> str:
    return (f"観測 {datetime.fromtimestamp(meta['reactions_observed_at'], JST):%m-%d %H:%M}"
            if meta["reactions"] is not None else
            "スタンプ取得不正" if meta["reactions_status"] == "invalid"
            else "スタンプ未取得")


def _listed(rows) -> list:
    return rows[:MAX_LIST] + ([f"・…他{len(rows) - MAX_LIST}件"]
                              if len(rows) > MAX_LIST else [])


def _recent_posts(db, until, ok, where, params=()) -> list:
    """直近OWN_POST_DAYS日（投稿時刻）の未削除・非アーカイブ投稿。"""
    return [r for r in db.execute(
        "SELECT m.message_id,m.project_id,m.parent_id,m.sender_id,m.posted_at_ts "
        "FROM messages m JOIN patients p ON p.project_id=m.project_id "
        "WHERE m.posted_at_ts>=? AND m.posted_at_ts<? "
        "AND COALESCE(p.is_archived,0)=0 AND COALESCE(m.body_state,'')!='deleted' "
        + where + " ORDER BY m.posted_at_ts,m.message_id",
        (until - OWN_POST_DAYS * DAY_S, until, *params)) if ok(r["project_id"])]


def _own_unreacted(db, until, ok, room, self_id) -> list:
    """本人のroot投稿で他者の反応が観測されていないもの。未取得・不正は別に数える。"""
    if self_id is None:
        return ["本人の送信者IDが不明のため判定していません。"]
    rows, unfetched, invalid = [], 0, 0
    for r in _recent_posts(db, until, ok, "AND m.parent_id IS NULL"):
        if mcs_signals.normalize_sender_id(r["sender_id"]) != self_id:
            continue
        meta = get_message_metadata(db, r["message_id"])
        others = others_reaction_count(meta)
        if others is None:
            invalid += meta["reactions_status"] == "invalid"
            unfetched += meta["reactions_status"] != "invalid"
        elif others == 0:
            rows.append(f"・{room(r['project_id'])} / message {r['message_id']} "
                        f"{_ago(until - r['posted_at_ts'])}・{_observed(meta)}")
    head = [f"他者反応0件 {len(rows)}投稿（直近{OWN_POST_DAYS}日・本人のroot投稿）"]
    if unfetched or invalid:
        head.append(f"スタンプ未取得 {unfetched}投稿・取得不正 {invalid}（0件に含めません）")
    return head + _listed(rows)


def _response_state(db, r, self_id, as_of) -> tuple:
    """(本人の後続返信あり, 本人スタンプ表示) — 同スレッド・投稿後のみ数える。"""
    meta = get_message_metadata(db, r["message_id"], as_of=as_of)
    observed = meta["response_observation"]
    replied = observed["reply"]["state"] == "observed"
    stamp_state = observed["self_reaction"]["state"]
    mine = (bool(observed["self_reaction"]["types"])
            if stamp_state == "observed" else None)
    stamp = ("本人スタンプ" + ("取得不正" if meta["reactions_status"] == "invalid"
                              else "再取得失敗" if stamp_state == "failed"
                              else "未取得") if mine is None
             else "本人スタンプあり" if mine else "本人スタンプ観測なし")
    reply = ("あり" if replied else "判定不可（本人ID・時刻不明）"
             if observed["reply"]["state"] == "unknown" or self_id is None else "観測なし")
    return replied, mine, f"自分の返信{reply}・{stamp}"


def _addressed_unanswered(db, until, ok, room, self_id, signals_on) -> list:
    """自分宛で応答未観測: 既存シグナルと、本人userメンション後の返信・本人反応なし。"""
    rows = []
    if signals_on:
        for c in mcs_signals._latest_signal_states(db).values():
            mids = ((c.get("evidence") or {}).get("message_ids") or []) if c else []
            if not (c and c["state"] == "open" and ok(c["project_id"])
                    and c["type"] == "pharmacist_request_unanswered" and mids):
                continue
            msg = db.execute("SELECT message_id,project_id,parent_id,posted_at_ts "
                             "FROM messages WHERE message_id=? AND project_id=?",
                             (mids[0], c["project_id"])).fetchone()
            state = (_response_state(db, msg, self_id, until)[2]
                     if msg and self_id else "本人ID不明")
            rows.append(f"・{room(c['project_id'])} / message {mids[0]} "
                        f"薬剤師宛依頼の応答未確認（シグナル）・{state}")
    unknown, station = 0, 0
    if self_id is not None:
        for r in _recent_posts(db, until, ok, ""):
            if mcs_signals.normalize_sender_id(r["sender_id"]) == self_id:
                continue
            meta = get_message_metadata(db, r["message_id"])
            hit = mentions_self(meta, self_id)
            if hit is None:
                unknown += 1
                continue
            station += any(m["type"] == "station" for m in meta["mentions"])
            if not hit:
                continue
            replied, mine, state = _response_state(db, r, self_id, until)
            if not replied and not mine:
                rows.append(f"・{room(r['project_id'])} / message {r['message_id']} "
                            f"本人宛メンション・{_ago(until - r['posted_at_ts'])}・{state}")
    notes = [] if self_id is not None else [
        "本人の送信者IDが不明のためメンションは判定していません。"]
    if station:
        notes.append(f"施設宛（自局判定なし）: {station}投稿")
    if unknown:
        notes.append(f"メンション不明（未取得・取得不正）: {unknown}投稿")
    if not rows and not station:
        return []
    return [f"{len(rows)}件（直近{OWN_POST_DAYS}日のメンションと薬剤師宛依頼シグナル）",
            *notes, *_listed(rows), NOT_RESPONSE_NOTE]


def _stale_open_unacked(db, now: float) -> list:
    """[(key, latest content)] — open alert keys first detected more
    than STALE_ALERT_D days ago whose key no live card acknowledgement
    has ever covered. The open-since clock starts at the key's first
    signal_v1 row: a resolve+reopen keeps the original timestamp, which
    reads as 'this condition persists' rather than 'new alert'."""
    latest = mcs_signals._latest_signal_states(db)
    open_by_key = {k: c for k, c in latest.items()
                   if c and c["state"] == "open"}
    if not open_by_key:
        return []
    stale = set()
    for k, first_open in db.execute(
            "SELECT json_extract(meta,'$.key'), MIN(created_at) "
            "FROM artifacts WHERE kind='signal_v1' AND json_valid(meta) "
            "GROUP BY json_extract(meta,'$.key')"):
        if k in open_by_key and type(first_open) in (int, float) \
                and now - first_open >= STALE_ALERT_D * DAY_S:
            stale.add(k)
    acked = set()
    for (shown,) in db.execute(
            "SELECT m.shown FROM notification_acknowledgements a "
            "JOIN notification_view_manifests m "
            "ON m.manifest_id=a.manifest_id "
            "WHERE a.withdrawn_at IS NULL"):
        try:
            shown_keys = json.loads(shown or "[]")
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(shown_keys, list):
            acked.update(k for k in shown_keys if isinstance(k, str))
    return [(k, open_by_key[k]) for k in stale - acked]


def build(db, cfg, since: float, until: float, flt=None, *,
          names: bool = False, name: str | None = None,
          allowed=None, private: bool = False) -> dict:
    """The summary display model for [since, until). ``flt`` is a
    parse_scope dict (None = all); ``names`` shows patient names next to
    project ids; ``allowed`` is the caller's project scope."""
    flt = flt or parse_scope("")
    keep = scope_projects(db, flt, name, allowed)
    live = {r[0] for r in db.execute(
        "SELECT project_id FROM patients WHERE COALESCE(is_archived,0)=0")}

    def ok(pid) -> bool:
        return pid in live and (keep is None or pid in keep)

    def room(pid) -> str:
        name_ = _plain(_patient_name(db, pid)) if names else ""
        return f"project {pid}" + (f" {name_}" if name_ else "")

    start = datetime.fromtimestamp(since, JST)
    end = datetime.fromtimestamp(until, JST)
    span = (f"直近{round((until - since) / DAY_S)}日"
            if until - since > DAY_S * 1.5 else
            f"{start:%m-%d %H:%M}〜{end:%m-%d %H:%M}")
    title = ("📊 MCS サマリー" if private else "🌅 MCS 日次サマリー") \
        + f"（{end:%m-%d %H:%M} JST）"
    max_age = cfg.get("notify_max_age_h")
    # history imports land with first_seen in the window but an old post
    # time; measured from the window start so days:N keeps its N days
    floor = (min(until - float(max_age) * 3600, since)
             if type(max_age) in (int, float) and max_age > 0 else None)
    rows = [r for r in db.execute(
        """SELECT m.message_id, m.project_id, m.profession, m.posted_at_ts
           FROM messages m JOIN patients p ON p.project_id=m.project_id
           WHERE m.first_seen>=? AND m.first_seen<?
             AND COALESCE(p.is_archived,0)=0
             AND COALESCE(m.body_state,'')!='deleted'
           ORDER BY m.first_seen, m.message_id""", (since, until))
        # history imports land with first_seen now but an old post time
        if ok(r["project_id"])
        # an unknown post time cannot be proven old (Ledger._filter_notify_age)
        and (floor is None or not r["posted_at_ts"] or r["posted_at_ts"] >= floor)]
    urgent = [(r["project_id"], r["message_id"], u) for r in rows
              if (u := structured_view.message_urgency(db, r["message_id"]))]

    today = end.date().isoformat()
    tasks = [t for t in db.execute(
        "SELECT project_id, due_date FROM requests "
        "WHERE status IN ('open','in_progress') ORDER BY request_id")
        if ok(t["project_id"])]
    late = [t["project_id"] for t in tasks
            if t["due_date"] and t["due_date"] < today]
    due = [t["project_id"] for t in tasks if t["due_date"] == today]

    sig = cfg.get("signals")
    signals_on = isinstance(sig, dict) and sig.get("notify") is True
    by_type: dict = {}
    stale_by: dict = {}
    if signals_on:
        for c in mcs_signals._latest_signal_states(db).values():
            if c and c["state"] == "open" and ok(c["project_id"]) \
                    and c["type"] not in EXCLUDED_SIGNALS:
                by_type[c["type"]] = by_type.get(c["type"], 0) + 1
        for _key, c in _stale_open_unacked(db, until):
            if ok(c["project_id"]):
                stale_by[c["type"]] = stale_by.get(c["type"], 0) + 1

    patients = len(live if keep is None else live & keep)
    summary = (f"新着 {len(rows)}件・緊急度高 {len(urgent)}件"
               + (f"・アラート {sum(by_type.values())}件" if signals_on else "")
               + f"・未完了タスク {len(tasks)}件（期限切れ {len(late)}・本日期限 {len(due)}）")
    containers = [
        {"type": "heading", "text": title},
        {"type": "text", "text": f"対象: {_scope_label(flt, name)} {patients}人・{span}"
                                 f"\n{summary}"}]

    def section(head, lines, fold=False):
        if lines:
            containers.append({"type": "text", "fold": fold,
                               "text": "\n".join([f"■ {head}", *lines])})

    reacted, reaction_counts = _self_reaction_count(db, since, until, keep)
    section(f"MCS 本人スタンプ観測: {reacted}投稿", [
        "・" + "・".join(f"{label} {count}" for label, count in
                        sorted(reaction_counts.items())) if reaction_counts else "・本人反応の観測なし",
        "・対象期間に観測した現在の保存状態です。未取得を除き、"
        "押下時刻・操作件数・業務完了を表しません。"])

    self_id = mcs_signals.self_sender_id(db)
    if cfg.get("metadata_refresh_publish") is True:
        section("反応が観測されていない自分の投稿",
                _own_unreacted(db, until, ok, room, self_id), fold=True)
    section("自分宛で応答未観測",
            _addressed_unanswered(db, until, ok, room, self_id, signals_on), fold=True)

    section(f"緊急度高 {len(urgent)}件", [
        f"・{room(p)} / message {m}" + (
            structured_view.urgency_qc_suffix(db, m) if u == "llm" else " 🚨")
        for p, m, u in urgent], fold=True)
    todo = []
    for label, pids in (("期限切れタスク", late), ("本日期限タスク", due)):
        if pids:
            rooms_ = list(dict.fromkeys(pids))
            todo.append(f"・{label} {len(pids)}件: " + _ids(rooms_, room))
    if stale_by:
        todo.append(f"・滞留アラート（{STALE_ALERT_D}日超・未確認）"
                    f"{sum(stale_by.values())}件: " + "・".join(
                        f"{_plain(k)} {n}" for k, n in sorted(stale_by.items())))
    section("要対応", todo)

    per: dict = {}
    for r in rows:
        prof = per.setdefault(r["project_id"], {})
        key = _plain(r["profession"]) or "職種不明"
        prof[key] = prof.get(key, 0) + 1
    ranked = sorted(per.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    section(f"新着（患者別 {len(per)}人）", [
        f"・{room(pid)} {sum(p.values())}件（" + "・".join(
            f"{k} {n}" for k, n in sorted(p.items(), key=lambda x: -x[1]))
        + "）" for pid, p in ranked], fold=True)

    if by_type:
        section("アラート（open）", ["・" + "・".join(
            f"{_plain(k)} {n}" for k, n in sorted(by_type.items()))])

    # 連携サマリー (#21): artifacts stored in the window that change an
    # earlier stored summary (a room's first fetch/backfill is not an
    # update) into a registered one; the comment never leaves the ledger
    summaries = []
    for r in db.execute(
            "SELECT project_id, content FROM artifacts a WHERE "
            "kind='karte_summary' AND created_at>=? AND created_at<? "
            "AND EXISTS (SELECT 1 FROM artifacts b WHERE "
            "b.kind='karte_summary' AND b.project_id=a.project_id "
            "AND b.artifact_id<a.artifact_id) "
            "ORDER BY artifact_id", (since, until)):
        try:
            c = json.loads(r["content"])
        except (ValueError, TypeError):
            continue
        if isinstance(c, dict) and c.get("empty") is not True \
                and ok(r["project_id"]):
            summaries.append(r["project_id"])
    # count rooms, not artifacts — one room refetched twice is one update
    updated = list(dict.fromkeys(summaries))
    section("連携サマリー更新", [f"・{len(updated)}件: " + _ids(updated, room)]
            if updated else [])

    gaps = coverage_gaps(db)
    cov = []
    incomplete = [r for r in gaps["incomplete_rooms"] if ok(r[0])]
    if incomplete:
        cov.append(f"・取得未完了のルーム {len(incomplete)}: " + _ids(
            incomplete, lambda r: f"{room(r[0])}（{_plain(r[1])}）"))
    else:
        cov.append("・未完了として記録されたルーム: なし"
                   "（完全性の保証ではありません）")
    scoped = "（全体）" if keep is not None else ""
    if gaps["jobs"]:
        cov.append(f"・取得待ち/失敗ジョブ{scoped}: " + "・".join(
            f"{_plain(k)} {n}" for k, n in gaps["jobs"].items()))
    if gaps["partial_bodies"]:
        cov.append(f"・本文未取得の投稿{scoped}: {gaps['partial_bodies']}件")
    if gaps["reply_gaps"]:
        cov.append(f"・返信の取得未完了スレッド{scoped}: {gaps['reply_gaps']}件")
    stamp_states = [get_message_metadata(db, r["message_id"])["reactions_status"]
                    for r in rows]
    if "not_fetched" in stamp_states or "invalid" in stamp_states:
        cov.append(f"・スタンプ未取得 {stamp_states.count('not_fetched')}投稿"
                   f"・取得不正 {stamp_states.count('invalid')}")
    held = db.execute("SELECT count(*) FROM notify_outbox WHERE "
                      "state='failed' AND next_try IS NULL").fetchone()[0]
    if held:
        cov.append(f"・送信保留の通知（全体）: {held}件")
    containers.append({"type": "text",
                       "text": "\n".join(["■ 取得状況（記録ベース）", *cov])})
    footer = [NOTE]
    if flt["mine"]:
        footer.insert(0, "※ 担当は担当者欄が表示名と一致する未完了タスクの記録です。"
                         "正式な担当割当ではありません。")
    return fit_parts({"containers": containers,
                      "footer": [{"type": "text", "text": t} for t in footer]})


def _daily_since(cfg, since, until):
    scope = (settings(cfg) or {}).get("scope", "all")
    flt = parse_scope(scope)
    if not isinstance(flt, str) and any(
            tok.partition(":")[0].lower() in ("days", "日数")
            for tok in scope.split()):
        return until - flt["days"] * DAY_S
    return since


def daily_parts(db, cfg, since: float, until: float) -> dict:
    """The daily summary's display model (the card notice). Names only
    when ``include_names``; scope from ``daily_digest.scope``."""
    s = settings(cfg) or {}
    flt = parse_scope(s.get("scope", "all"))
    if isinstance(flt, str) or flt["mine"]:
        flt = parse_scope("")          # mine needs a clicker; validated in setup
    return build(db, cfg, _daily_since(cfg, since, until), until, flt,
                 names=s.get("include_names") is True)


def build_text(db, cfg, since: float, until: float) -> str:
    """The daily text notice — the summary in the plain dialect, folded
    to one relayed chat message (text route and card kill switch)."""
    return parts_text(fit_parts(daily_parts(db, cfg, since, until),
                                NOTICE_BUDGET), "plain")


def view(db, cfg, scope_text, *, name=None, allowed=None, now=None,
         names: bool = True) -> dict:
    """The clicker-only 📊 view: the scope's window ending now, patient
    names shown unless ``names`` is False (an entry whose answer is not
    proven private), caller scope enforced."""
    flt = parse_scope(scope_text)
    if isinstance(flt, str):
        return {"error": flt}
    if flt["mine"] and not " ".join(str(name or "").split()):
        return {"error": "mine には担当の名前が必要です。名前欄に入力してください。"}
    now = time.time() if now is None else now
    return {"parts": build(db, cfg, now - flt["days"] * DAY_S, now, flt,
                           names=names, name=name, allowed=allowed,
                           private=True)}


def maybe_enqueue(ledger, cfg, now=None) -> int:
    """Queue today's digest once, at or after hour_jst. The window
    starts where the previous digest ended (24h back the first time)."""
    import notify_cards
    s = settings(cfg)
    target = (cfg or {}).get("notify_target")
    cards_on = notify_cards.interactive_enabled(cfg or {}) \
        and notify_cards.delivery_scope(cfg or {}) is not None
    if s is None or not (cards_on or (isinstance(target, str)
                                      and target.strip())):
        return 0
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, JST)
    if local.hour < s["hour_jst"]:
        return 0
    day = local.date().isoformat()
    db = ledger.db

    def last_digest():
        prev = db.execute(
            "SELECT event_id, payload FROM notify_outbox WHERE kind=? "
            "ORDER BY event_id DESC LIMIT 1", (KIND,)).fetchone()
        try:
            last = json.loads(prev["payload"]) if prev else {}
        except (ValueError, TypeError):
            last = {}
        return (prev["event_id"] if prev else None,
                last if isinstance(last, dict) else {})

    seen, last = last_digest()
    if last.get("date") == day:
        return 0
    since = last.get("until")
    if type(since) not in (int, float) or not 0 < since < now:
        since = now - 86400
    since = _daily_since(cfg, since, now)
    # the read-heavy body is built outside the write lock; the insert
    # re-checks that no other writer queued a digest meanwhile
    parts = daily_parts(db, cfg, since, now)
    with db:
        db.execute("BEGIN IMMEDIATE")
        if last_digest()[0] != seen:
            return 0
        # both forms frozen: parts for the card notice, text for the
        # text route and the card kill switch
        ledger.outbox_add_tx(KIND, None, {
            "text": parts_text(fit_parts(parts, NOTICE_BUDGET), "plain"),
            "parts": parts, "date": day, "since": since, "until": now},
            # a card notice only while cards are on — off, the text route
            # never waits on (or rolls back into) the card pipeline
            route="interactive" if cards_on else "text")
    return 1


def main(argv=None) -> int:
    """CLI: print the summary for a scope (plain text, no names unless
    --names) — ``notify_digest.py --print [--names] [scope ...]``."""
    import argparse
    import sqlite3
    from pathlib import Path

    from mcs_util import DB, load_config
    ap = argparse.ArgumentParser(description="MCS summary (#31)")
    ap.add_argument("--print", action="store_true", required=True)
    ap.add_argument("--names", action="store_true")
    ap.add_argument("--name", help="担当照合に使う名前（mine）")
    ap.add_argument("scope", nargs="*")
    args = ap.parse_args(argv)
    flt = parse_scope(" ".join(args.scope))
    if isinstance(flt, str):
        print(flt, file=sys.stderr)
        return 2
    # read-only: the CLI never writes or migrates the ledger
    db = sqlite3.connect(Path(DB).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        answer = view(db, load_config(), " ".join(args.scope),
                      names=args.names, name=args.name)
    finally:
        db.close()
    if "error" in answer:
        print(answer["error"], file=sys.stderr)
        return 2
    print(parts_text(answer["parts"], "plain"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
