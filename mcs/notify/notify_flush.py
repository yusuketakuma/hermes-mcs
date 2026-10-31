"""Outbox notifier — drains notify_outbox through the configured delivery CLI.

Slack/Discord delivery is delegated to Hermes's standard send path: the destination
comes from ~/.mcs/config.json {notify_target} in `hermes send --to`
syntax (e.g. "slack", "slack:#mcs", "discord:1234"), and Hermes owns
platform connection, credentials, channel resolution and mentions
policy. When config sets "notify_bot_profile" (e.g. "cco"), sends run
under that Hermes profile so posts arrive under that bot's identity.
Attachments ride as MEDIA:<path> references in the message text; the
platform adapter owns upload limits.
LINE WORKS uses hermes-mcs's independent adapter CLI with JSON text and
verified attachment metadata, without a Hermes platform adapter; with
runtime_mode "standalone" Slack/Discord use `mcs_standalone send` the
same way.

Events carry message_ids; message content is looked up in the local
ledger at send time so the outbox payload itself stays tiny.

Send-time mode/generation checks live in semantic_send_gate.
Audited summary presentation and provenance-preserving chunks live in
semantic_render; this module owns delivery and resolves current config.
"""
import json
import os
import re
import sqlite3
import time
from contextvars import ContextVar

import mcs_util
import notify_sender
import mcs_signals
import semantic_send_gate
import semantic_render
import structured_view
from mcs_queries import HOLD_PROGRESS_SET
from mcs_util import CONF_PATH, file_sha256, html_to_text, load_config
from mcs_requests import positive
from notify_render import (SECTION_RULE, extraction_failed, project_display_name, project_identity, signal_evidence,
                           thread_stamp_row)
import clinical_urgency
TEXT_CHUNK_WIDTH = 1900
_NO_RENDER_CONFIG = object()
_RENDER_CONFIG = ContextVar("notify_render_config", default=_NO_RENDER_CONFIG)
# Per-notification file caps; oversized files stay local-only and are
# noted in the text instead of failing the whole send.
_MAX_FILES = 10
_MAX_FILES_BYTES = 23 * 1024 * 1024


def _config() -> dict:
    return load_config(CONF_PATH)


def _attachments_map(ledger, mids: list[int]) -> dict:
    """message_id -> attachment rows (all states) for 📎 markers + files."""
    if not mids:
        return {}
    q = ("SELECT attachment_id,message_id,file_id,name,state,local_path,"
         "bytes,sha256 "
         f"FROM attachments WHERE message_id IN ({','.join('?' * len(mids))}) "
         "AND state != 'withdrawn' ORDER BY attachment_id")
    out: dict[int, list] = {}
    for r in ledger.db.execute(q, mids):
        out.setdefault(r["message_id"], []).append(r)
    return out


def _collect_files(att_map: dict, order: list[int],
                   skipped: dict | None = None) -> list[tuple[str, str]]:
    """Downloaded attachment (filename, local_path) pairs for upload —
    message order, max _MAX_FILES, each <= notify_sender.MAX_FILE_BYTES. A downloaded
    file left out (caps, missing, unverifiable) is recorded in
    ``skipped`` as attachment_id -> text marker so the notice can say
    it was not sent instead of listing it as attached."""
    files = []
    total = 0

    def skip(a, why):
        if skipped is not None:
            skipped[a["attachment_id"]] = why

    for mid in order:
        for a in att_map.get(mid, []):
            if a["state"] != "downloaded":
                continue
            if len(files) >= _MAX_FILES:
                skip(a, "件数上限·未送信")
                continue
            if not a["local_path"] or not os.path.exists(a["local_path"]):
                skip(a, "未送信")
                continue
            try:
                size = os.path.getsize(a["local_path"])
            except OSError:
                skip(a, "未送信")
                continue
            if size > notify_sender.MAX_FILE_BYTES:
                skip(a, "25MB超·未送信")
                continue
            if total + size > _MAX_FILES_BYTES:
                skip(a, "容量上限·未送信")
                continue
            if not a["sha256"]:
                skip(a, "未送信")
                continue
            try:
                digest = file_sha256(a["local_path"])
            except OSError:
                skip(a, "未送信")
                continue
            if digest != a["sha256"]:
                skip(a, "未送信")
                continue
            fname = os.path.basename(str(a["name"] or a["file_id"] or "file"))
            # every control char too: LINE WORKS rejects them and the whole notice stalls
            fname = re.sub(r'[\x00-\x1f\x7f"\\]', "_", fname) or "file"
            files.append((fname[:150], a["local_path"]))
            total += size
    return files


def _signal_text(ledger, payload: dict, latest: dict):
    """Render a review-candidate signal notice for delivery.

    The outbox payload keeps its frozen shape (ids + fixed note, never
    message bodies). At send time we resolve display context from the
    ledger: the patient's name into the location line, and the latest
    triggering mention (sender/time/snippet) as a quote so the notice
    says WHO and WHAT without a manual lookup. Degrades silently — a
    missing patient row or a deleted message never fails the send.
    """
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        return text
    lines = text.split("\n")
    pid = payload.get("project_id")
    if type(pid) is int and len(lines) > 1:
        name = project_display_name(ledger.db, pid, sqlite_id=True)
        if name:
            lines[1] = f"{name}（{lines[1]}）"
        _, m = signal_evidence(ledger.db, {**latest, "project_id": pid})
        if m and m["body_state"] != "deleted" and m["body_text"]:
            snippet = " ".join(str(m["body_text"]).split())[:120]
            lines.append(
                f"最新言及 {m['posted_at'] or '?'} "
                f"{m['sender_name'] or '?'}: {snippet}")
        lines.append(f"確認: /mcs {{\"op\":\"timeline\",\"project_id\":{pid}}}")
    return "\n".join(lines)


def _signal_unit_text(ledger, sigs: list[dict]):
    """Render ONE review unit for delivery — a single signal, or a
    merged same-post med_change group. Display context (patient name,
    latest-mention quote, timeline link) is resolved at send time."""
    latest = sigs[0]
    merged = (mcs_signals.med_followup_group_notice(sigs)
              if len(sigs) > 1 else None)
    if len(sigs) > 1 and merged is None:
        # a unit that cannot render merged must still show EVERY
        # member — never silently collapse to the first signal
        return "\n\n".join(_signal_unit_text(ledger, [s])
                           for s in sigs)
    sig_view = latest
    if merged is not None:
        mids = sorted({m for s in sigs
                       for m in ((s.get("evidence") or {})
                                 .get("message_ids") or [])
                       if type(m) is int})
        sig_view = {"evidence": {"message_ids": mids}}
    text = _signal_text(
        ledger,
        {"text": merged or mcs_signals.signal_notice_text(latest),
         "project_id": latest.get("project_id")},
        sig_view)
    mid, message = signal_evidence(
        ledger.db, {**sig_view, "project_id": latest.get("project_id")})
    urgency = (clinical_urgency.message_urgency(ledger.db, mid)
               if message is not None and mid is not None else None)
    if urgency:
        suffix = (structured_view.urgency_qc_suffix(ledger.db, mid)
                  if urgency == "llm" else "")
        head, sep, tail = text.partition("\n")
        text = (f"{head} — {structured_view.URGENCY_LABEL_PLAIN[urgency]}"
                f"{suffix}{sep}{tail}")
    return text


def _signal_notice_text(ledger, payload: dict) -> str:
    """Review-candidate notice: resolve the current explanation/evidence.
    Last-moment gates like semantic_notice: the flag may have been
    turned off, or the signal may have resolved while queued —
    both are terminal drops, not retries."""
    import notify_scope
    if not notify_scope.signals_notify(_config()):
        raise semantic_send_gate.StaleSend("signals_notify_disabled")
    # Member keys: a merged same-post med intent carries
    # signal_keys[]; a legacy/single intent carries signal_key.
    skeys = payload.get("signal_keys")
    keys = ([k for k in skeys if type(k) is str and k]
            if isinstance(skeys, list) else [])
    skey = payload.get("signal_key")
    if not keys and type(skey) is str and skey:
        keys = [skey]
    # a member that resolved while queued drops out of a merged
    # notice; only a fully-resolved group cancels the send. Digest
    # payloads carry project_id=None, so the per-event archived
    # gate in flush() can't see them — members of an archived
    # patient drop out here instead.
    open_sigs = [s for _, s in mcs_signals.open_signal_rows(
        ledger.db, keys)
        if not (s.get("project_id")
                and ledger.is_archived(s["project_id"]))]
    if not open_sigs:
        raise semantic_send_gate.StaleSend("signal_not_open")
    # Rebuild text at send time: notes can change while evidence
    # IDs stay the same, a digest re-groups its live members, and a
    # merged med notice shrinks to whoever is still open.
    if payload.get("digest") is True:
        units = mcs_signals.sig_units(
            [(mcs_signals.med_group_key(s), s) for s in open_sigs])
        parts = [_signal_unit_text(ledger, us) for us in units]
        text = (f"[MCS] アラートダイジェスト"
                f"（{len(open_sigs)}件）\n\n" + "\n\n".join(parts))
    else:
        text = _signal_unit_text(ledger, open_sigs)
    if not isinstance(text, str) or not text:
        raise ValueError("payload_invalid")
    return text


def _followup_files(ledger, payload: dict, project_id):
    """a body notice went out before this file downloaded — deliver
    just the file now (F11). If the file is no longer sendable
    (pruned/withdrawn/never finished) the intent is terminal."""
    aid = payload.get("attachment_id")
    if not positive(aid):
        raise ValueError("payload_invalid")
    a = ledger.db.execute(
        "SELECT a.message_id,a.file_id,a.name,a.state,a.local_path,a.bytes,a.sha256,m.project_id"
        " FROM attachments a JOIN messages m ON m.message_id=a.message_id"
        " WHERE a.attachment_id=? AND m.body_state IS NOT 'deleted'"
        " AND (? IS NULL OR m.project_id=?)",
        (aid, project_id, project_id)).fetchone()
    if not a or a["state"] != "downloaded" or not a["local_path"]:
        raise semantic_send_gate.StaleSend("attachment_not_ready")
    # a card's part journal already owns this file (planned, sent or
    # uncertain) — a second send would duplicate it. Held parts never
    # send; their thread failure re-queues this followup instead.
    if ledger.db.execute(
            "SELECT 1 FROM notification_render_parts WHERE attachment_id=?"
            " AND kind='attachment_part'"
            " AND state IN ('pending','delivered','unknown') LIMIT 1",
            (aid,)).fetchone():
        raise semantic_send_gate.StaleSend("attachment_in_card_parts")
    files = _collect_files({a["message_id"]: [a]}, [a["message_id"]])
    if not files:
        raise semantic_send_gate.StaleSend("attachment_unsendable")
    import notify_render
    preview = notify_render.notification_preview(
        ledger.db, {"kind": "thread", "project_id": a["project_id"]},
        {"shown": [a["message_id"]]})
    return (f"{preview}\n[MCS] 添付ファイル（後送）\n{a['name'] or 'file'}"), files


def _format_event(ledger, ev) -> tuple[str, list[tuple[str, str]]]:
    """Returns (content, files). files = [(filename, local_path)] to upload."""
    try:
        payload = json.loads(ev["payload"])
    except (ValueError, TypeError, RecursionError) as e:
        raise ValueError("payload_invalid") from e
    if not isinstance(payload, dict):
        raise ValueError("payload_invalid")
    if ev["kind"] == "session_expired":
        return ("[MCS] セッション失効 — 手動再ログインが必要です\n"
                f"run {payload.get('run_id')}: {payload.get('detail','')}"), []
    if ev["kind"] == "session_recovered":
        # auto_login succeeded at the failure point — the expiry is
        # reported as resolved, not as a manual-login demand
        return ("[MCS] セッション失効 → 自動再ログインで復旧しました\n"
                f"run {payload.get('run_id')}: {payload.get('detail','')}"), []
    if ev["kind"] == "run_failed":
        return ("[MCS] チェック失敗 — アダプタを確認してください\n"
                f"run {payload.get('run_id')}: {payload.get('detail','')}"), []
    if ev["kind"] == "health_write_failed":
        return ("[MCS] health.json の書き込み失敗 — 監視用の状態ファイルが更新されていません\n"
                f"run {payload.get('run_id')}: {payload.get('detail','')}"), []
    if ev["kind"] == "daily_digest":
        # frozen at enqueue (ids and counts only); turning the digest
        # off drops a still-queued one instead of sending it late
        import notify_digest
        if notify_digest.settings(_config()) is None:
            raise semantic_send_gate.StaleSend("daily_digest_disabled")
        if ledger.db.execute(
                "SELECT 1 FROM notify_outbox WHERE kind='daily_digest' "
                "AND event_id>?", (ev["event_id"],)).fetchone():
            # a newer day exists — an old summary is never sent late
            raise semantic_send_gate.StaleSend("daily_digest_superseded")
    if ev["kind"] == "task_reminder":
        # the text is frozen at enqueue; the task, card or switch may
        # have changed while it waited
        import notify_cards
        stale = notify_cards.task_reminder_stale(
            ledger, _config(), ev["event_id"])
        if stale == "outside_hours":
            raise semantic_send_gate.DeferredSend(stale)
        if stale:
            raise semantic_send_gate.StaleSend(stale)
    if ev["kind"] in ("update_notice", "task_reminder", "daily_digest"):
        # Frozen text like semantic_notice — sanitized at enqueue time
        # (mentions defused), the sender just relays it.
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("payload_invalid")
        return text, []
    if ev["kind"] == "semantic_notice":
        # audited/degraded semantic notice — the payload carries the
        # final text frozen at enqueue time; the sender never re-derives
        # it (INV-14). Two gates are re-checked at the last moment
        # (§18.4/§22.1): mode must still be enforce, and the audited
        # notice's input fingerprint must still be live — a degraded
        # notice is instead suppressed if its base event has since
        # delivered.
        semantic_send_gate.semantic_gate(ledger.db, ev, payload, _config())
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("payload_invalid")
        return text, []
    if ev["kind"] == "signal":
        return _signal_notice_text(ledger, payload), []
    # urgent_notice never reaches the text path: flush moves a never-begun
    # one to the card route and holds a begun one (see flush)
    if ev["kind"] == "attachment_followup":
        return _followup_files(ledger, payload, ev["project_id"])
    return _message_notice(ledger, ev, payload)


def _msg_rows(ledger, payload: dict, project_id) -> list:
    """message_ids shape check + row load — missing/deleted bodies are
    dropped, a fully-empty result means the intent went stale."""
    ids = payload.get("message_ids") or []
    if (not isinstance(ids, list)
            or any(not positive(mid) for mid in ids)):
        raise ValueError("payload_invalid")
    rows = []
    for mid in ids:
        r = ledger.db.execute("""
          SELECT m.* FROM messages m
          WHERE m.message_id=? AND m.body_state IS NOT 'deleted'
            AND (? IS NULL OR m.project_id=?)""",
            (mid, project_id, project_id)).fetchone()
        if r:
            rows.append(r)
    if not rows:
        raise semantic_send_gate.StaleSend("messages_unavailable")
    return rows


def _fmt_row(ledger, ev, att_map: dict, r, indent: str,
             skipped: dict, *, stamps=True, summary_suffix="") -> str:
    s_lines = structured_view.structured_lines(
        ledger.db, r["message_id"], plain=True)
    body = html_to_text(r["body_html"])
    cap = 500 if s_lines else 600
    if len(body) > cap:
        body = body[:cap] + "…"
    who = r["sender_name"] or "不明"
    meta = " / ".join(x for x in (r["profession"], r["organization"]) if x)
    parent = " (返信)" if r["parent_id"] and not indent else ""
    state = "" if r["body_state"] == "full" else f" [{r['body_state']}]"
    # urgency stays on the summary's own 🚨 line (one spelling, owner 2026-10-09)
    pid = r["project_id"]
    identity = project_identity(ledger.db, pid)
    name = (identity["group_name"] or f"project {pid}" if identity and identity["group"]
            else (identity["patient_name"] if identity else None) or ev["project_id"])
    head = (f"{indent}**{name}**"
            f"{parent}{state}\n"
            f"{indent}{who}{f' ({meta})' if meta else ''} — "
            f"{(r['posted_at'] or '?')[:16]}")
    atts = att_map.get(r["message_id"], [])
    att_line = ""
    if atts:
        marks = []
        for a in atts:
            nm = str(a["name"] or a["file_id"] or "file")
            if a["state"] != "downloaded":
                nm += " (未取得)"
            elif a["attachment_id"] in skipped:
                nm += f" ({skipped[a['attachment_id']]})"
            marks.append(nm)
        more = f"、他{len(marks) - 5}件" if len(marks) > 5 else ""
        att_line = f"\n{indent}📎 {'、'.join(marks[:5])}{more}"
    empty = "作成失敗" if not s_lines and extraction_failed(ledger.db, r["message_id"]) else "処理待ち"
    summary = ([f"{indent}📋 要約"] + [f"{indent}・" + ln.replace("\n", f"\n{indent}") for ln in s_lines]
               if s_lines else [f"{indent}📋 要約 {empty}"])
    line = thread_stamp_row(ledger.db, r["message_id"], r["sender_id"]) if stamps else ""
    if line:            # an observed zero needs no row (as on the card)
        summary.append(indent + line)
    return (head + "\n" + "\n".join(summary) + summary_suffix + "\n"
            + indent + SECTION_RULE + "\n" + indent + "📄 本文\n"
            + indent + (body or "(本文なし)") + att_line)




def _message_notice(ledger, ev, payload: dict):
    """New-message notice body — replies group under their parent when
    both are new in this event; file order follows the text order."""
    rows = _msg_rows(ledger, payload, ev["project_id"])
    src = payload.get("source", "unread")
    att_map = _attachments_map(ledger, [r["message_id"] for r in rows])
    by_id = {r["message_id"]: r for r in rows}
    parents = [r for r in rows if not r["parent_id"]
               or r["parent_id"] not in by_id]
    kids = {}
    for r in rows:
        if r["parent_id"] and r["parent_id"] in by_id:
            kids.setdefault(r["parent_id"], []).append(r)
    # (row, indent) in text order — files are collected before the rows
    # render so each 📎 line can mark the files that did not go out
    seq = []
    for r in parents:
        seq.append((r, ""))
        seq.extend((k, "↳ ") for k in sorted(
            kids.get(r["message_id"], []),
            key=lambda x: x["posted_at"] or ""))
    skipped: dict = {}
    files = _collect_files(att_map, [r["message_id"] for r, _ in seq],
                           skipped)
    stamps = not (notify_sender.target(_config(), ev["kind"]) or "").startswith("lineworks:")
    out = []
    for r, indent in seq:
        summary_suffix = ""
        if not indent:
            try:
                selected_cfg = _RENDER_CONFIG.get()
                if selected_cfg is _NO_RENDER_CONFIG:
                    selected_cfg = _config()
                summary_suffix = semantic_render.semantic_summary_block(ledger.db, r, selected_cfg)
            except Exception:
                pass  # A config/summary failure cannot break the base notice.
        out.append(_fmt_row(ledger, ev, att_map, r, indent, skipped, stamps=stamps,
                            summary_suffix=summary_suffix))
    head = f"[MCS {src}] 新着 {len(rows)} 件"
    return (head + "\n\n" + "\n\n".join(out), files)


def _has_sent_progress(ev) -> bool:
    raw = ev["progress"]
    if raw is None or raw == "":
        return False
    try:
        progress = mcs_util.strict_json(raw)
        if not isinstance(progress, dict) or "invalid_progress" in progress:
            return True
        count, sent = progress.get("next", 0), progress.get("sent", [])
        if (type(count) is not int or count < 0 or not isinstance(sent, list)
                or len(sent) != count or any(not isinstance(item, str) or not item.isdigit() for item in sent)):
            return True
        return count > 0 or progress.get("sending") is not None
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        return True


def _send_never_began(ev) -> bool:
    """No delivery could have happened: the progress receipt is absent
    entirely (the pre-send marker write precedes any subprocess) or it
    records no accepted AND no in-flight chunk. An UNPARSEABLE receipt
    is ambiguous — a corrupted record of a real send — so it fails
    safe (False)."""
    raw = ev["progress"]
    if raw is None or raw == "":
        return True
    try:
        progress = mcs_util.strict_json(raw)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        return False
    if not isinstance(progress, dict) or "invalid_progress" in progress or "hold_reason" in progress:
        return False
    count, sent = progress.get("next", 0), progress.get("sent", [])
    return (type(count) is int and count == 0 and isinstance(sent, list) and not sent
            and progress.get("sending") is None
            and (progress.get("fingerprint") is None or isinstance(progress["fingerprint"], str)))


def hold_event(ledger, ev, cfg, proven_undelivered=False,
                reason="delivery_held", rescue=True):
    """Quarantine an event; for an unsent digest payload, first move
    its still-open member keys into a fresh scheduled digest — a held
    intent stops covering its keys but nothing else would ever
    re-enqueue them (signals notify only at the open transition), so
    without this the members would go permanently silent. Salvage runs
    only when non-delivery is provable: an empty/no-send receipt, or a
    send path that refused before posting (proven_undelivered).
    Borrows ledger without closing it; existing hold/rescue commit boundaries apply."""
    # flush's row predates this attempt. Its current receipt may now
    # contain accepted chunks or an unresolved in-flight send.
    ev = ledger.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                           (ev["event_id"],)).fetchone()
    if ev is None:
        return
    # The hold stores only a stable code alongside the existing receipt
    # (HOLD_PROGRESS_SET); never replace its in-flight evidence with a
    # guessed negative acknowledgement.
    if rescue and (proven_undelivered or _send_never_began(ev)) \
            and not _has_sent_progress(ev):
        payload = mcs_util.loads_dict(ev["payload"])
        if payload is not None:
            ids = payload.get("message_ids")
            mids = [mid for mid in ids if positive(mid)] if isinstance(ids, list) else []
            rescuable = (payload.get("rescue_of") is None and bool(mids)
                         if ev["kind"] == "new_messages" else
                         payload.get("digest") is True
                         or payload.get("signal_keys") or payload.get("signal_key"))
            if rescuable:
                sc = cfg.get("signals")
                ih = sc.get("digest_interval_h") \
                    if isinstance(sc, dict) else None
                interval_h = (ih if type(ih) in (int, float) and ih > 0
                              else mcs_signals.DIGEST_INTERVAL_H)
                # rescue + hold in ONE commit — a crash between them
                # would leave both the old intent and the rescue live,
                # sending the same members twice
                with ledger.db:
                    if ev["kind"] == "new_messages":
                        # a held new_messages intent strands its ids the
                        # same way — notified_at was already consumed, so
                        # nothing can ever re-announce them. Proven
                        # undelivered, so re-enqueue the same id set —
                        # single-shot: a rescued intent that also
                        # quarantines does not respawn (rescue_of marks
                        # the lineage)
                        ledger.outbox_add_tx(
                            "new_messages", ev["project_id"],
                            {"message_ids": mids,
                             "source": payload.get("source"),
                             "rescue_of": ev["event_id"]})
                    else:
                        mcs_signals.rescue_digest_members(
                            ledger, payload, time.time(), interval_h,
                            origin_id=ev["event_id"])
                    ledger.db.execute(
                        "UPDATE notify_outbox SET state='failed',next_try=NULL,"
                        f"{HOLD_PROGRESS_SET},updated_at=? WHERE event_id=?",
                        (reason, time.time(), ev["event_id"]))
                return
    ledger.outbox_hold(ev["event_id"], reason)


# Direct-sender codes (exit 75) that prove nothing was accepted AND name a
# condition that heals on its own: connectivity/DNS/timeout before any post,
# a busy local sender lock, or a platform rate limit. The standalone
# Slack/Discord sender reports every not_sent outcome (including its
# identity check failing on a network error) as delivery_not_accepted.
_TRANSIENT_SEND_CODES = frozenset({
    "transport_unknown", "rate_limited", "sender_busy",
    "delivery_not_accepted", "http_429", "slack_identity_unreachable",
    "discord_unreachable", "lineworks_token_unavailable",
    "slack_configuration_changed_restart_required"})
# Transient pre-send failures quarantine on elapsed time, not on a retry
# count: a ~45 min network outage must not park every text notification
# for manual release. Auth/config/attachment failures keep the 5-attempt cap.
TRANSIENT_HOLD_AFTER_S = 86400
PRE_SEND_MAX_ATTEMPTS = 4


def _retry_backoff(attempts: int) -> int:
    return min(3600, 60 * (2 ** min(int(attempts), 6)))


def _transient_failing_for(attempts: int) -> int:
    """Lower bound of seconds spent failing after ``attempts`` failed
    tries — each retry waits at least its backoff, so the schedule sum
    never overstates the real elapsed time."""
    return sum(_retry_backoff(i) for i in range(max(0, int(attempts))))


def _transient_pre_send(exc) -> bool:
    return (isinstance(exc, notify_sender.SendFailed) and not isinstance(exc, notify_sender.SendBudget)
            and exc.code in _TRANSIENT_SEND_CODES)


class _IntakeSnapshotChanged(Exception):
    """The semantic hold witness changed before a send marker was committed."""


class _SemanticSnapshotChanged(Exception):
    """An outbox row changed while its semantic notice was being prepared."""


def _hold_if_snapshot(ledger, ev, cfg, reason, *, rescue=True):
    if ledger.db.in_transaction:
        raise ValueError('send_marker_transaction_active')
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        current = ledger.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                                    (ev["event_id"],)).fetchone()
        if (current is None or current["state"] not in ("pending", "failed")
                or current["state"] != ev["state"] or current["next_try"] != ev["next_try"]
                or current["payload"] != ev["payload"] or current["progress"] != ev["progress"]
                or _route(current) != _route(ev)):
            return False
        hold_event(ledger, current, cfg, reason=reason, rescue=rescue)
    return True


def _owned_progress(ev, next_chunk, sent_ids, fingerprint, sending=None):
    if isinstance(ev, dict) and ev.get('kind') == 'semantic_notice':
        ev['_failure_progress'] = json.dumps({'next': next_chunk, 'sent': list(sent_ids),
                                              'fingerprint': fingerprint, 'sending': sending})


def _send_marked(ledger, ev, i: int, sent_ids: list[str],
                 fingerprint: str, argv: list[str], chunk: str,
                 files: list[tuple[str, str]] | None,
                 deadline: float | None,
                 attachment_pins: dict[str, str] | None = None,
                 mark_guard=None) -> None:
    """Retain the in-flight marker unless non-delivery is established."""
    notify_sender.send_budget(deadline)
    try:
        ev["state"], ev["next_try"], ev["payload"]
        baseline = ev
    except (KeyError, IndexError):
        baseline = ledger.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                                     (ev["event_id"],)).fetchone()
        if baseline is None:
            raise _SemanticSnapshotChanged()
    if mark_guard is None:
        ledger.outbox_progress(ev["event_id"], i, list(sent_ids), fingerprint, i + 1)
    else:
        if ledger.db.in_transaction:
            raise ValueError('send_marker_transaction_active')
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            mark_guard()
            ledger.outbox_progress(ev["event_id"], i, list(sent_ids), fingerprint, i + 1)
    _owned_progress(ev, i, sent_ids, fingerprint, i + 1)
    try:
        kwargs = {"attachment_pins": attachment_pins} if attachment_pins is not None else {}
        notify_sender.send(argv, chunk, files, deadline=deadline, **kwargs)
    except (notify_sender.SendFailed, notify_sender.SendUsage) as error:
        # Refund only our own still-current in-flight marker. Another
        # writer may have accepted, held, or replaced this event meanwhile.
        with ledger.db:
            if not ledger.db.in_transaction:
                ledger.db.execute('BEGIN IMMEDIATE')
            current = ledger.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                                        (ev["event_id"],)).fetchone()
            try:
                progress = mcs_util.strict_json(current["progress"]) if current is not None else None
            except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
                progress = None
            expected = {"next": i, "sent": list(sent_ids), "fingerprint": fingerprint, "sending": i + 1}
            if (current is None or current["state"] != baseline["state"]
                    or current["next_try"] != baseline["next_try"] or current["payload"] != baseline["payload"]
                    or _route(current) != _route(baseline) or progress != expected):
                raise _SemanticSnapshotChanged() from error
            ledger.outbox_progress(ev["event_id"], len(sent_ids), list(sent_ids), fingerprint)
            _owned_progress(ev, len(sent_ids), sent_ids, fingerprint)
        raise


def _progress(raw: str, count: int) -> tuple[int, list[str], str | None,
                                            int | None]:
    try:
        d = mcs_util.strict_json("{}" if raw is None or raw == "" else raw)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError("progress_invalid") from None
    if (not isinstance(d, dict) or "invalid_progress" in d or "hold_reason" in d
            or type(d.get("next", 0)) is not int):
        raise ValueError("progress_invalid")
    next_chunk = d.get("next", 0)
    sent = d.get("sent", [])
    fingerprint = d.get("fingerprint")
    sending = d.get("sending")
    if (next_chunk < 0 or next_chunk > count or not isinstance(sent, list)
            or len(sent) != next_chunk
            or any(not isinstance(x, str) or not x.isdigit() for x in sent)
            or (fingerprint is not None and not isinstance(fingerprint, str))
            or (sending is not None and (type(sending) is not int
                                         or sending != next_chunk + 1 or sending > count))):
        raise ValueError("progress_invalid")
    return next_chunk, sent, fingerprint, sending


def _route(ev) -> str:
    """Row/dict-safe route read — a fake outbox in tests (or an
    un-migrated caller) that lacks the column is a text event."""
    try:
        return ev["route"] or "text"
    except (KeyError, IndexError):
        return "text"


def _dispatch_interactive(ledger, ev, cfg, res) -> bool:
    """Card-route intent. Sealed batches stay card-owned even with the
    switch off — a text send here could double-deliver, and begins are
    denied until the switch returns. The kill switch converts an
    UNSEALED intent once (provably unsent) so it can flush as plain
    text; returns False in that case so the caller falls through."""
    import notify_cards
    import notify_scope
    import notify_parts
    sealed = ledger.db.execute(
        "SELECT 1 FROM notification_intent_batches "
        "WHERE event_id=?", (ev["event_id"],)).fetchone()
    text_mode = (not notify_scope.interactive_enabled(cfg)
                 or (ev["kind"] == "attachment_followup"
                     and not notify_parts.late_attachment_thread(cfg)))
    if ev["kind"] == "patient_history" and text_mode:
        ledger.outbox_defer(ev["event_id"], RERENDER_RETRY_S)
        res["parked"] += 1
        return True
    if not sealed and ev["kind"] not in ("urgent_notice", "signal") and text_mode:
        if not _send_never_began(ev):
            reason = "thread_route_after_partial" if ev["kind"] == "attachment_followup" else "send_outcome_unknown"
            if _hold_if_snapshot(ledger, ev, cfg, reason, rescue=False):
                if ev["kind"] == "attachment_followup":
                    res["failed"] += 1
                else:
                    res["uncertain"] = res.get("uncertain", 0) + 1
            else:
                res["skipped"] += 1
            return True
        if not notify_cards.revert_to_text(ledger, ev["event_id"], expected=ev):
            res["skipped"] += 1
            return True
        return False
    if ev["kind"] == "attachment_followup" and _route(ev) == "text":
        if not _send_never_began(ev):
            hold_event(ledger, ev, cfg, reason="thread_route_after_partial", rescue=False)
            res["failed"] += 1
            return True
        with ledger.db:
            changed = ledger.db.execute(
                "UPDATE notify_outbox SET route='interactive' WHERE event_id=? "
                "AND kind='attachment_followup' AND route='text' "
                "AND state IN ('pending','failed') AND progress IS ?",
                (ev["event_id"], ev["progress"])).rowcount
        if not changed:
            res["skipped"] += 1
            return True
    try:
        outcome = notify_cards.dispatch_intent(ledger, ev, cfg)
        if outcome.get("text_fallback") and not sealed and _send_never_began(ev) \
                and notify_cards.revert_to_text(ledger, ev["event_id"], expected=ev):
            return False
        if outcome.get("parked"):
            res["parked"] += 1
        elif outcome.get("held"):
            res["failed"] += 1
        elif outcome.get("error") in ("payload_invalid", "resend_exhausted"):
            # dispatch already quarantined it — a malformed frozen
            # payload cannot heal, so never re-arm an hourly retry
            ledger.outbox_hold(ev["event_id"], outcome["error"])
            res["failed"] += 1
        elif outcome.get("error"):
            # a sealed intent whose transport/scope no longer matches the
            # config never heals on its own — quarantine like the
            # exception path instead of failing hourly forever
            if ev["attempts"] >= 4:
                ledger.outbox_hold(ev["event_id"], "dispatch_failed")
            else:
                ledger.outbox_mark(ev["event_id"], "failed",
                                   retry_in=3600)
            res["failed"] += 1
        elif outcome.get("suppressed"):
            res["suppressed"] += 1
        elif not outcome.get("skipped"):
            res["dispatched"] = res.get("dispatched", 0) + 1
    except Exception:
        if ev["attempts"] >= 4:
            ledger.outbox_hold(ev["event_id"], "internal_failure")
        else:
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=3600)
        res["failed"] += 1
    return True


RERENDER_RETRY_S = 60


def _base_render_gate_ok(ledger, ev, render_state, in_progress,
                         res) -> bool:
    """Render gate for a raw new_messages notice. The gate only guards
    the OPTIONAL attached summary: before anything was sent, a stale or
    policy-deferred summary means re-format on the next flush — never
    suppress or hour-park the arrival notice itself. Once a chunk is
    out, the gate's freeze/hold semantics apply unchanged."""
    try:
        semantic_send_gate.semantic_render_gate(ledger.db, ev, render_state, _config(), in_progress=in_progress)
    except (semantic_send_gate.StaleSend, semantic_send_gate.DeferredSend):
        if in_progress:
            raise
        # a re-render is not a failed delivery — keep attempts intact so
        # repeated summary churn can never age the notice into a hold
        ledger.outbox_defer(ev["event_id"], RERENDER_RETRY_S)
        res["rerender"] = res.get("rerender", 0) + 1
        return False
    return True


def _send_text(ledger, ev, cfg, argv, target, res, deadline) -> None:
    owned = dict(ev)
    try:
        return _send_text_owned(ledger, owned, cfg, argv, target, res, deadline)
    except Exception as exc:
        if owned['kind'] == 'semantic_notice':
            exc._mcs_failure_snapshot = owned
        raise


def _send_text_owned(ledger, ev, cfg, argv, target, res, deadline) -> None:
    """Format, chunk and send one text event — per-chunk progress is
    journaled so a crash mid-event resumes at the first unacknowledged
    chunk rather than duplicating accepted posts. Insufficient time raises
    notify_sender.SendBudget without consuming an attempt or reserving a new send."""
    notify_sender.send_budget(deadline)
    # An unknown send outcome is held before formatting: a stale/closed
    # source would otherwise suppress the event and lose the receipt.
    if ev["progress"]:
        try:
            early_progress = mcs_util.strict_json(ev["progress"])
        except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
            raise ValueError("progress_invalid") from None
        if not isinstance(early_progress, dict):
            raise ValueError("progress_invalid")
        if early_progress.get("sending") is not None:
            if _hold_if_snapshot(ledger, ev, cfg, "send_outcome_unknown", rescue=False):
                res["uncertain"] = res.get("uncertain", 0) + 1
            else:
                res["skipped"] = res.get("skipped", 0) + 1
            return
    import notify_urgent
    if ev["kind"] == "new_messages":
        if ledger.db.in_transaction:
            raise ValueError('new_messages_render_transaction_active')
        urgency = _send_never_began(ev) and notify_urgent.settings(cfg) is not None
        with ledger.db:
            # Format and record summary identities against the same snapshot.
            # The later send gate rejects a hold or revision after this view.
            ledger.db.execute("BEGIN IMMEDIATE" if urgency else "BEGIN")
            render_cfg = _config()
            config_token = _RENDER_CONFIG.set(render_cfg)
            try:
                content, files = _format_event(ledger, ev)
            finally:
                _RENDER_CONFIG.reset(config_token)
            render_state = semantic_send_gate.semantic_render_state(ledger.db, ev, render_cfg)
            render_roots = None
            if render_state:
                import semantic_intake_hold
                render_hold_epoch = (semantic_intake_hold.hold_epoch(ledger.db, ev['project_id']),
                                     semantic_intake_hold.unstored_project_epoch(ledger.db, ev['project_id']))
                # Keep marker-time work bounded. For larger notices use the
                # conservative per-project epoch instead of a root scan.
                if len(render_state) <= 64:
                    mids = sorted({entry[0] for entry in render_state})
                    roots = ledger.db.execute(
                        'SELECT message_id,COALESCE(NULLIF(parent_id,0),message_id) root '
                        'FROM messages WHERE project_id=? AND message_id IN '
                        '(SELECT value FROM json_each(?))',
                        (ev['project_id'], json.dumps(mids))).fetchall()
                    if len(roots) == len(mids):
                        render_roots = sorted({str(row['root']) for row in roots})
            else:
                render_hold_epoch = None
            if urgency:
                notify_urgent.capture_initial(ledger, cfg, ev, now=time.time())
    else:
        content, files = _format_event(ledger, ev)
        render_state = ()
        render_hold_epoch = None
        render_roots = None
    attachment_pins = None
    if notify_sender.hermes_free(cfg, target):
        attachment_pins = {}
        for _, path in files:
            hashes = {r[0] for r in ledger.db.execute(
                "SELECT sha256 FROM attachments WHERE local_path=? AND state='downloaded'", (path,))}
            if len(hashes) != 1 or not mcs_util.valid_hash(next(iter(hashes))):
                raise notify_sender.SendFailed("attachment_unverified")
            attachment_pins[path] = next(iter(hashes))
    chunks = (semantic_render.semantic_chunks(content, TEXT_CHUNK_WIDTH)
              if ev["kind"] == "semantic_notice" else
              [content[i:i + TEXT_CHUNK_WIDTH]
               for i in range(0, len(content), TEXT_CHUNK_WIDTH)])
    # resume at the first unacknowledged chunk — a crash after a
    # partial send must not duplicate accepted chunks (B25)
    # an unacknowledged `sending` chunk was already held above (F19)
    start, sent_ids, previous, _ = _progress(
        ev["progress"], len(chunks))
    fingerprint = notify_sender.delivery_fingerprint(target, chunks, files)
    if start and previous != fingerprint:
        if _hold_if_snapshot(ledger, ev, cfg, "delivery_changed"):
            res["failed"] += 1
        else:
            res["skipped"] = res.get("skipped", 0) + 1
        return
    if not start:
        if ev["kind"] == "semantic_notice":
            if ledger.db.in_transaction:
                raise ValueError('send_marker_transaction_active')
            with ledger.db:
                ledger.db.execute('BEGIN IMMEDIATE')
                current = ledger.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                                            (ev["event_id"],)).fetchone()
                if (current is None or current["state"] != ev["state"]
                        or current["state"] not in ("pending", "failed")
                        or current["payload"] != ev["payload"] or _route(current) != _route(ev)
                        or current["next_try"] != ev["next_try"] or current["progress"] != ev["progress"]):
                    raise _SemanticSnapshotChanged()
                ledger.outbox_progress(ev["event_id"], 0, [], fingerprint)
                _owned_progress(ev, 0, [], fingerprint)
        else:
            ledger.outbox_progress(ev["event_id"], 0, [], fingerprint)
    post_files = files

    def gate(i, payload) -> bool:
        """Last-moment pre-chunk gate, re-evaluated before a retry."""
        if ev["kind"] == "semantic_notice":
            semantic_send_gate.semantic_gate(ledger.db, ev, payload, _config(), in_progress=bool(start or i))
            return True
        return not render_state or _base_render_gate_ok(
            ledger, ev, render_state, bool(start or i), res)

    for i in range(start, len(chunks)):
        notify_sender.send_budget(deadline)
        payload = None
        if ev["kind"] == "semantic_notice":
            try:
                payload = json.loads(ev["payload"])
            except (ValueError, TypeError, RecursionError) as e:
                raise ValueError("payload_invalid") from e
        if not gate(i, payload):
            return
        # files ride the FIRST post only; on resume (start>0) they
        # were already delivered with chunk 0. A usage rejection
        # of the file-bearing send (exit 2 — never a delivery
        # failure where acceptance is unknown) drops the files
        # and retries text-only so a bad attachment can never
        # sink the notification itself
        def mark_guard():
            if ev['kind'] == 'semantic_notice':
                current = ledger.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                                            (ev['event_id'],)).fetchone()
                if (current is None or current['state'] != ev['state']
                        or current['state'] not in ('pending', 'failed')
                        or current['next_try'] != ev['next_try']):
                    raise _SemanticSnapshotChanged()
                try:
                    saved_next, saved_ids, saved_fp, sending = _progress(current['progress'], len(chunks))
                except ValueError:
                    raise _SemanticSnapshotChanged() from None
                if (current['payload'] != ev['payload'] or _route(current) != _route(ev)
                        or saved_next != i or saved_ids != sent_ids or saved_fp != fingerprint or sending is not None):
                    raise _SemanticSnapshotChanged()
                if semantic_send_gate.intake_root_held(ledger.db, ev['project_id'], payload['root_id']):
                    raise semantic_send_gate.FreezeSend('semantic_intake_held') if start or i else \
                        semantic_send_gate.DeferredSend('semantic_intake_held')
                marker_cfg = _config()
                semantic_send_gate.semantic_gate(ledger.db, current, payload, marker_cfg,
                                                 in_progress=bool(start or i))
                semantic_send_gate.marker_source_gate(ledger.db, ev['project_id'], payload,
                                                       in_progress=bool(start or i), cfg=marker_cfg)
            elif render_hold_epoch is not None:
                import semantic_intake_hold
                try:
                    if render_roots is None:
                        changed = (semantic_intake_hold.hold_epoch(ledger.db, ev['project_id']),
                                   semantic_intake_hold.unstored_project_epoch(ledger.db, ev['project_id'])) != render_hold_epoch
                    else:
                        semantic_intake_hold.validate_indexes(ledger.db)
                        changed = ledger.db.execute(
                            "SELECT 1 FROM main.artifacts INDEXED BY idx_semantic_intake_hold_root "
                            "WHERE kind='semantic_intake_hold_v1' AND project_id=? AND model IN "
                            "(SELECT value FROM json_each(?)) LIMIT 1",
                            (ev['project_id'], json.dumps(render_roots))).fetchone() is not None
                        if not changed:
                            changed = any(semantic_intake_hold.has_unstored_root(
                                ledger.db, ev['project_id'], int(root)) for root in render_roots)
                except sqlite3.Error:
                    changed = True
                if changed:
                    raise _IntakeSnapshotChanged()

        def send_once(files):
            try:
                _send_marked(ledger, ev, i, sent_ids, fingerprint, argv, chunks[i],
                             files, deadline, attachment_pins=attachment_pins,
                             mark_guard=mark_guard if (ev['kind'] == 'semantic_notice'
                                                       or render_hold_epoch is not None) else None)
            except _IntakeSnapshotChanged:
                if start or i:
                    raise semantic_send_gate.FreezeSend('semantic_intake_changed') from None
                ledger.outbox_defer(ev['event_id'], RERENDER_RETRY_S)
                res['rerender'] = res.get('rerender', 0) + 1
                return False
            return True

        try:
            if not send_once(post_files if i == 0 else None):
                return
        except notify_sender.SendUsage:
            # A file-only followup cannot succeed by discarding its payload.
            if i != 0 or not post_files or ev["kind"] == "attachment_followup":
                raise
            post_files = None
            if not gate(i, payload):
                return
            if not send_once(None):
                return
        sent_ids.append(str(i + 1))
        ledger.outbox_progress(ev["event_id"], i + 1, sent_ids,
                               fingerprint)
        _owned_progress(ev, i + 1, sent_ids, fingerprint)
    ledger.outbox_mark(ev["event_id"], "accepted",
                       sent_ids[-1] if sent_ids else "")
    res["sent"] += 1


def _fail_event(ledger, ev, cfg, res, exc):
    """Per-event failure accounting — quarantine, park, suppress or
    backoff according to the exception class (notify_sender.SendUncertain must be
    checked before OSError, which it subclasses)."""
    if (ev["kind"] == "semantic_notice" and not isinstance(exc, _SemanticSnapshotChanged)
            and not ledger.db.in_transaction):
        # Failure handling must not overwrite a concurrently accepted,
        # suppressed, replaced, or already-held notice.
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            current = ledger.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                                        (ev["event_id"],)).fetchone()
            expected = getattr(exc, '_mcs_failure_snapshot', ev)
            expected_progress = (expected.get('_failure_progress', expected['progress'])
                                 if isinstance(expected, dict) else expected['progress'])
            if (current is None or current["state"] not in ("pending", "failed")
                    or current["next_try"] is None or current["state"] != expected['state']
                    or current["next_try"] != expected['next_try']
                    or current["attempts"] != expected['attempts']
                    or current["payload"] != expected['payload']
                    or _route(current) != _route(expected) or current['progress'] != expected_progress):
                res["skipped"] += 1
                return
            return _fail_event(ledger, current, cfg, res, exc)
    if isinstance(exc, _SemanticSnapshotChanged):
        # The other writer owns the new state and receipt; do not overwrite it.
        res["skipped"] += 1
    elif isinstance(exc, notify_sender.SendBudget):
        res["skipped"] += 1
        res["send_budget_insufficient"] = res.get("send_budget_insufficient", 0) + 1
    elif isinstance(exc, semantic_send_gate.DeferredSend):
        # stays pending but re-checks hourly, not every flush. A
        # parked enforce intent is policy-blocked, not incomplete —
        # count it separately so run_check's notify_incomplete signal
        # doesn't flag every other tick while the mode gate is down
        if _has_sent_progress(ev):
            hold_event(ledger, ev, cfg, reason="deferred_after_partial")
            res["failed"] += 1
        else:
            ledger.outbox_defer(ev["event_id"], 3600)
            res["parked"] += 1
    elif isinstance(exc, semantic_send_gate.FreezeSend):
        # An accepted chunk is immutable. Keep its receipt/progress and
        # freeze the remaining chunks until an explicit retry decision.
        hold_event(ledger, ev, cfg, reason="policy_changed")
        res["failed"] += 1
    elif isinstance(exc, semantic_send_gate.StaleSend):
        if _has_sent_progress(ev):
            hold_event(ledger, ev, cfg, reason="stale_after_partial")
            res["failed"] += 1
        else:
            ledger.outbox_suppress(ev["event_id"])
            res["suppressed"] += 1
    elif isinstance(exc, notify_sender.SendUsage):
        # invocation itself refused (exit 2) — a CLI/config contract
        # problem; retrying cannot fix it, quarantine the event.
        # The refusal is provably pre-delivery, so held digest
        # members may be salvaged.
        hold_event(ledger, ev, cfg, proven_undelivered=True, reason="send_usage")
        res["failed"] += 1
    elif isinstance(exc, notify_sender.SendUncertain):
        hold_event(ledger, ev, cfg, reason="send_outcome_unknown", rescue=False)
        res["uncertain"] = res.get("uncertain", 0) + 1
        res["failed"] += 1
    elif isinstance(exc, ValueError):
        hold_event(ledger, ev, cfg, reason="payload_or_progress_invalid")
        res["failed"] += 1
    elif isinstance(exc, OSError | TimeoutError | KeyError):
        # A permanent pre-send failure (revoked token, deleted channel,
        # missing attach) quarantines after the generic 5-attempt ceiling
        # — with the never-began receipt its digest members still salvage.
        # A provably transient refusal (network, busy sender, rate limit)
        # keeps retrying on backoff and holds only after
        # TRANSIENT_HOLD_AFTER_S of failure, so neither loops forever.
        if _transient_pre_send(exc):
            exhausted = _transient_failing_for(ev["attempts"]) >= TRANSIENT_HOLD_AFTER_S
        else:
            exhausted = ev["attempts"] >= PRE_SEND_MAX_ATTEMPTS
        if exhausted:
            hold_event(ledger, ev, cfg, reason="pre_send_failure")
        else:
            ledger.outbox_mark(ev["event_id"], "failed",
                               retry_in=_retry_backoff(ev["attempts"]))
        res["failed"] += 1
    else:
        # Per-event containment: an unexpected failure inside
        # _format_event/semantic_send_gate (broken import, sqlite error)
        # must not escape flush and starve every later due event.
        # Retry hourly first — a transient fault self-heals; a
        # deterministic bug quarantines after 5 attempts instead of
        # looping forever.
        if ev["attempts"] >= 4:
            hold_event(ledger, ev, cfg, reason="internal_failure")
        else:
            ledger.outbox_mark(ev["event_id"], "failed",
                               retry_in=3600)
        res["failed"] += 1


def flush(ledger, limit: int = 10, deadline: float | None = None) -> dict:
    cfg = _config()
    res = {"sent": 0, "failed": 0, "skipped": 0, "suppressed": 0,
           "parked": 0}
    # a reply parked on its parent's thread resumes once that thread exists
    import notify_cards
    with ledger.db:
        woken = notify_cards.wake_ready_reply_holds(ledger.db, time.time())
    if woken:
        res["reply_woken"] = woken
    due = ledger.outbox_due(limit, history_fair=True)
    import notify_parts
    thread_attachments = notify_parts.late_attachment_thread(cfg)

    def card_route(e) -> bool:
        """Delivered as a card: needs neither the hermes exe nor a text destination."""
        return (_route(e) == "interactive" or e["kind"] in ("urgent_notice", "signal")
                or bool(thread_attachments and e["kind"] == "attachment_followup"))
    exe = mcs_util.hermes_exe(cfg)
    exe_ok = os.path.isfile(exe) and os.access(exe, os.X_OK)
    if not exe_ok and not any(
            card_route(e)
            or notify_sender.hermes_free(cfg, notify_sender.target(cfg, e["kind"])) for e in due):
        res["skipped"] = len(due)
        return res
    for event_index, ev in enumerate(due):
        if deadline is not None and time.monotonic() >= deadline:
            res["skipped"] += len(due) - event_index
            break
        if ev["project_id"] and ledger.is_archived(ev["project_id"]):
            # queued before the patient was archived — archived patient
            # events must never reach the channel; drop terminally, do
            # not count as sent OR as a retryable failure (Oracle F2).
            # A send that began with unknown outcome is held instead,
            # so the receipt is counted as send_outcome_unknown.
            progress = mcs_util.loads_dict(ev["progress"] or "{}")
            if progress is None:
                hold_event(ledger, ev, cfg, reason="payload_or_progress_invalid", rescue=False)
                res["failed"] += 1
                continue
            began = progress.get("sending")
            if began is not None:
                hold_event(ledger, ev, cfg, reason="send_outcome_unknown", rescue=False)
                res["uncertain"] = res.get("uncertain", 0) + 1
                continue
            ledger.outbox_suppress(ev["event_id"])
            res["suppressed"] += 1
            continue
        # A missing text destination is a pre-send configuration error;
        # no SQLite/restore lookup or native delivery can be attempted here.
        if (exe_ok and _route(ev) == "text" and not card_route(ev)
                and not notify_sender.target(cfg, ev["kind"])):
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=3600)
            res["failed"] += 1
            continue
        if ev["kind"] not in notify_sender.SYSTEM_ALERT_KINDS:
            import notify_spool
            if notify_spool.restore_pending(notify_spool.data_root(ledger)) is not None:
                # move it out of the ORDER BY event_id window so held
                # content can never starve a later alert (no attempt used)
                ledger.outbox_defer(ev["event_id"], RERENDER_RETRY_S)
                res["skipped"] += 1
                res["restore_pending"] = res.get("restore_pending", 0) + 1
                continue
        if ev["kind"] in ("urgent_notice", "signal") and _route(ev) != "interactive":
            if not _send_never_began(ev):
                hold_event(ledger, ev, cfg, reason=("send_outcome_unknown" if ev["kind"] == "signal" else "thread_route_after_partial"), rescue=False)
                res["failed"] += 1
                continue
            ledger.db.execute("UPDATE notify_outbox SET route='interactive' WHERE event_id=?", (ev["event_id"],))
            ledger.db.commit()
        if card_route(ev):
            if _dispatch_interactive(ledger, ev, cfg, res):
                continue
            # The interactive kill switch just changed this event's route.
            # Re-read its snapshot, but never adopt unrelated concurrent edits.
            current = ledger.db.execute("SELECT * FROM notify_outbox WHERE event_id=?",
                                        (ev["event_id"],)).fetchone()
            if (current is None or _route(current) != "text"
                    or any(current[key] != ev[key] for key in
                           ("kind", "project_id", "payload", "state", "next_try", "attempts", "progress"))):
                res["skipped"] += 1
                continue
            ev = current
        if not exe_ok and not notify_sender.hermes_free(cfg, notify_sender.target(cfg, ev["kind"])):
            # no hermes exe — text events can't send, but an interactive
            # event later in the queue still dispatches (cards don't
            # need the exe), so skip per-event rather than break
            res["skipped"] += 1
            continue
        target = notify_sender.target(cfg, ev["kind"])
        if not target:
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=3600)
            res["failed"] += 1
            continue
        argv = notify_sender.send_argv(cfg, target, CONF_PATH)
        try:
            _send_text(ledger, ev, cfg, argv, target, res, deadline)
        except Exception as exc:
            _fail_event(ledger, ev, cfg, res, exc)
    return res
