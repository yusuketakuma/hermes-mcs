"""Outbox notifier — drains notify_outbox through `hermes send`.

Delivery is delegated to Hermes's standard send path: the destination
comes from ~/.mcs/config.json {notify_target} in `hermes send --to`
syntax (e.g. "slack", "slack:#mcs", "discord:1234"), and Hermes owns
platform connection, credentials, channel resolution and mentions
policy. When config sets "notify_bot_profile" (e.g. "cco"), sends run
under that Hermes profile so posts arrive under that bot's identity.
Attachments ride as MEDIA:<path> references in the message text; the
platform adapter owns upload limits.

Events carry message_ids; message content is looked up in the local
ledger at send time so the outbox payload itself stays tiny.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

import mcs_signals
from mcs_queries import med_is_patient_current
from mcs_util import html_to_text, load_config

CONF_PATH = os.path.expanduser("~/.mcs/config.json")
_MAX_LEN = 1900
# Per-notification file caps; oversized files stay local-only and are
# noted in the text instead of failing the whole send.
_MAX_FILES = 10
_MAX_FILE_BYTES = 24 * 1024 * 1024
_MAX_FILES_BYTES = 23 * 1024 * 1024


class _DeferredSend(Exception):
    """Not deliverable under CURRENT policy (e.g. semantic mode left
    enforce) — the committed intent stays pending for a later flush;
    it is not destroyed (INV-23) and never sent past its gate."""


class _StaleSend(Exception):
    """The source generation this intent was built for has moved on —
    suppress terminally instead of publishing stale results (§18.4)."""


class _FreezeSend(Exception):
    """A semantic intent changed after a chunk was accepted — preserve its
    receipt/progress and quarantine the remaining chunks."""


def _config() -> dict:
    return load_config(CONF_PATH)


def _hermes_exe(cfg: dict) -> str:
    """`hermes` binary for delivery. config {hermes_bin} overrides; the
    default falls back to the standard user-local install because the
    launchd PATH is minimal."""
    exe = cfg.get("hermes_bin")
    if isinstance(exe, str) and exe.strip():
        return exe.strip()
    return (shutil.which("hermes")
            or os.path.expanduser("~/.local/bin/hermes"))


def _target(cfg: dict, kind: str) -> str | None:
    """Delivery target in `hermes send --to` syntax. Patient-content
    events ONLY go to the explicitly configured target — a missing
    config must never spill bodies into a fallback destination
    (Oracle B15). System alerts may override via notify_system_target."""
    t = cfg.get("notify_target")
    if kind in ("session_expired", "run_failed"):
        st = cfg.get("notify_system_target")
        if isinstance(st, str) and st.strip():
            t = st
    return t.strip() if isinstance(t, str) and t.strip() else None


def _send_argv(cfg: dict, target: str) -> list[str]:
    """`hermes [-p profile] send --to <target> --quiet` argv."""
    argv = [_hermes_exe(cfg)]
    profile = cfg.get("notify_bot_profile")
    if isinstance(profile, str) and re.fullmatch(r"[a-z0-9_-]+", profile):
        argv += ["-p", profile]
    argv += ["send", "--to", target, "--quiet"]
    return argv


def _artifact(ledger, kind: str, mid: int) -> dict | None:
    r = ledger.db.execute(
        "SELECT a.content FROM artifacts a JOIN messages m "
        "ON m.message_id=a.message_id WHERE a.kind=? AND a.message_id=? "
        "AND CASE WHEN json_valid(a.meta) THEN "
        "json_extract(a.meta,'$.error') IS NOT 1 AND "
        "json_extract(a.meta,'$.hash')=m.content_hash ELSE 0 END "
        "AND CASE WHEN json_valid(a.content) THEN "
        "json_type(a.content)='object' ELSE 0 END "
        "ORDER BY a.artifact_id DESC LIMIT 1", (kind, mid)).fetchone()
    if not r:
        return None
    try:
        d = json.loads(r["content"])
    except (json.JSONDecodeError, TypeError):
        return None
    return d if isinstance(d, dict) else None


_RX_LABEL = {"start": "開始", "stop": "中止", "change": "変更",
             "none": "変更なし", "no_change": "変更なし",
             "increase": "増量", "decrease": "減量"}
_REQ_LABEL = {"confirm": "確認", "contact": "連絡", "share": "共有",
              "ask": "質問", "request": "依頼", "report": "報告"}
_EVT_LABEL = {"admission": "入院", "discharge": "退院", "exam": "受診/検査",
              "visit": "訪問", "medication": "投薬", "adherence": "服薬",
              "care": "介護", "eol": "看取り", "media_ref": "添付",
              "transfer": "転院/移動", "fall": "転倒",
              "family_contact": "家族連絡", "other": "その他"}


def _structured_lines(ledger, mid: int) -> list[str]:
    """Compact structured summary from extract_v1/extract_llm artifacts.
    Returns [] when nothing usable exists (caller falls back to raw only)."""
    v1 = _artifact(ledger, "extract_v1", mid) or {}
    llm = _artifact(ledger, "extract_llm", mid) or {}
    if not v1 and not llm:
        return []
    lines: list[str] = []
    if (llm.get("summary") or "").strip():
        lines.append(f"要約: {llm['summary'].strip()[:80]}")
    pts = [str(p).strip() for p in (llm.get("points") or [])
           if isinstance(p, str) and p.strip()]
    if pts:
        lines.append("要点: " + " / ".join(p[:40] for p in pts[:3]))
    evs = [e for e in (llm.get("events") or v1.get("events") or [])
           if e in _EVT_LABEL]
    if evs:
        lines.append("区分: " + "・".join(_EVT_LABEL[e] for e in evs[:5]))
    vit = llm.get("vitals") if isinstance(llm.get("vitals"), dict) else None
    if not vit and isinstance(v1.get("vitals"), dict):
        vit = v1["vitals"]
    if vit:
        parts = []
        if vit.get("sbp") is not None:
            parts.append(f"BP {vit['sbp']}/{vit.get('dbp')}")
        for k, lab in (("bt", "BT"), ("hr", "HR"), ("rr", "RR"),
                       ("spo2", "SpO2"), ("bs", "BS")):
            if vit.get(k) is not None:
                parts.append(f"{lab} {vit[k]}")
        if parts:
            lines.append("バイタル: " + "  ".join(parts))
    syms, neg, seen, neg_seen = [], [], set(), set()
    llm_symptoms = [s for s in llm.get("symptoms") or []
                    if isinstance(s, dict) and isinstance(s.get("text"), str)
                    and s["text"]]
    for s in llm_symptoms:
        if isinstance(s, dict) and s.get("text"):
            if s.get("subject") in ("family", "other") or s.get("unverified"):
                continue
            # resolved/past/negated all cancel an earlier positive —
            # they enter neg_seen so a v1 positive below is contradicted
            # (same resolver semantics as rollup.py)
            if s.get("negated") or s.get("status") in ("resolved", "past"):
                if s["text"] not in neg_seen:
                    neg_seen.add(s["text"])
                    if s.get("negated"):
                        neg.append(s["text"])
            elif s["text"] not in seen:
                seen.add(s["text"])
                syms.append(s["text"])
    for s in v1.get("symptoms") or []:
        if not isinstance(s, str):
            continue
        if any(x["text"] in s or s in x["text"] for x in llm_symptoms):
            continue  # Typed polarity/subject must not reappear through rules.
        contradicted = any(n in s or s in n for n in neg_seen)
        if s and s not in seen and not contradicted:
            syms.append(s)
    if syms or neg:
        line = "症状: " + "、".join(syms[:6])
        if neg:
            line += ("　" if syms else "") + "、".join(
                f"{n}なし" for n in neg[:4])
        lines.append(line)
    meds = []
    for m in llm.get("meds") or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        # negated / other-person / historical meds must not read as the
        # patient's own medication (planned survives — shown as [予定])
        if not med_is_patient_current(m):
            continue
        d = str(m["name"]) + (f" {m['dose']}" if m.get("dose") else "")
        if m.get("action") in _RX_LABEL:
            d += f"[{_RX_LABEL[m['action']]}]"
        if m.get("status") == "planned":
            d += "[予定]"
        meds.append(d)
    unverified_meds = []
    if not llm.get("meds"):
        # v1 fallback only when the LLM saw NO meds — if it saw meds
        # but all were filtered (negated/family/past), falling back to
        # v1 would re-display the very mentions that were filtered out
        for m in v1.get("medications") or []:
            if isinstance(m, dict) and m.get("name"):
                unverified_meds.append(str(m["name"]) +
                                       (f" {m['dose']}" if m.get("dose") else ""))
        unverified_meds.extend(
            f"{_RX_LABEL[a['action']]}:{a['ctx'][:18]}"
            for a in v1.get("rx_actions") or []
            if isinstance(a, dict) and a.get("action") in _RX_LABEL
            and a.get("ctx"))
    if meds:
        lines.append("薬剤: " + "、".join(meds[:6]))
    if unverified_meds:
        lines.append("薬剤候補（未確認）: " + "、".join(unverified_meds[:6]))
    reqs = []
    for r in llm.get("requests") or []:
        if isinstance(r, dict) and (r.get("to") or r.get("action")):
            to = str(r.get("to") or "")
            to = "" if to in ("", "不明", "unknown", "-") else f"{to}へ"
            frm = str(r.get("from") or "")
            prefix = "" if frm in ("", "不明", "unknown", "-") \
                else f"{frm}→"
            due = r.get("due")
            suffix = f"(期限:{due})" if isinstance(due, str) and due \
                else ""
            reqs.append(prefix + to + str(r.get("action") or "")[:30]
                        + suffix)
    if not reqs:
        for r in v1.get("requests") or []:
            if isinstance(r, dict) and r.get("ctx"):
                reqs.append(f"{_REQ_LABEL.get(r.get('kind'), '依頼')}:"
                            f"{r['ctx'][:24]}")
    if reqs:
        lines.append("依頼: " + " / ".join(reqs[:3]))
    if v1.get("med_periods"):
        mp = v1["med_periods"][0]
        if isinstance(mp, dict) and mp.get("start"):
            lines.append(f"服薬期間: {mp['start']}〜{mp.get('end') or '?'}")
    if v1.get("next_planned"):
        lines.append(f"次回予定: {v1['next_planned']}")
    return lines


def _urgency(ledger, mid: int) -> str | None:
    for kind in ("extract_llm", "extract_v1"):
        d = _artifact(ledger, kind, mid)
        if d and d.get("urgency") == "high":
            return "high"
    return None


def _attachments_map(ledger, mids: list[int]) -> dict:
    """message_id -> attachment rows (all states) for 📎 markers + files."""
    if not mids:
        return {}
    q = ("SELECT message_id,file_id,name,state,local_path,bytes,sha256 "
         f"FROM attachments WHERE message_id IN ({','.join('?' * len(mids))}) "
         "AND state != 'withdrawn' ORDER BY attachment_id")
    out: dict[int, list] = {}
    for r in ledger.db.execute(q, mids):
        out.setdefault(r["message_id"], []).append(r)
    return out


def _collect_files(att_map: dict, order: list[int]) -> list[tuple[str, str]]:
    """Downloaded attachment (filename, local_path) pairs for upload —
    message order, max _MAX_FILES, each <= _MAX_FILE_BYTES."""
    files = []
    total = 0
    for mid in order:
        for a in att_map.get(mid, []):
            if len(files) >= _MAX_FILES:
                return files
            if a["state"] != "downloaded" or not a["local_path"]:
                continue
            if not os.path.exists(a["local_path"]):
                continue
            try:
                size = os.path.getsize(a["local_path"])
            except OSError:
                continue
            if size > _MAX_FILE_BYTES or total + size > _MAX_FILES_BYTES:
                continue
            if not a["sha256"]:
                continue
            h = hashlib.sha256()
            try:
                with open(a["local_path"], "rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(block)
            except OSError:
                continue
            if h.hexdigest() != a["sha256"]:
                continue
            fname = os.path.basename(str(a["name"] or a["file_id"] or "file"))
            fname = re.sub(r'["\\\r\n]', "_", fname) or "file"
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
        r = ledger.db.execute(
            "SELECT patient_name FROM patients WHERE project_id=?",
            (pid,)).fetchone()
        name = r["patient_name"].strip() if r and r["patient_name"] else ""
        if name:
            lines[1] = f"{name}（{lines[1]}）"
        ev = latest.get("evidence")
        mids = (ev.get("message_ids") if isinstance(ev, dict)
                else None) or []
        mid = (mids[-1] if mids and type(mids[-1]) is int
               else (ev.get("discharge_message_id")
                     if isinstance(ev, dict) else None))
        if type(mid) is int:
            m = ledger.db.execute(
                "SELECT sender_name, posted_at, body_text FROM messages"
                " WHERE message_id=?", (mid,)).fetchone()
            if m and m["body_text"]:
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
    return _signal_text(
        ledger,
        {"text": merged or mcs_signals.signal_notice_text(latest),
         "project_id": latest.get("project_id")},
        sig_view)


def _format_event(ledger, ev) -> tuple[str, list[tuple[str, str]]]:
    """Returns (content, files). files = [(filename, local_path)] to upload."""
    try:
        payload = json.loads(ev["payload"])
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError("payload_invalid") from e
    if not isinstance(payload, dict):
        raise ValueError("payload_invalid")
    if ev["kind"] == "session_expired":
        return ("[MCS] セッション失効 — 手動再ログインが必要です\n"
                f"run {payload.get('run_id')}: {payload.get('detail','')}"), []
    if ev["kind"] == "run_failed":
        return ("[MCS] チェック失敗 — アダプタを確認してください\n"
                f"run {payload.get('run_id')}: {payload.get('detail','')}"), []
    if ev["kind"] == "semantic_notice":
        # audited/degraded semantic notice — the payload carries the
        # final text frozen at enqueue time; the sender never re-derives
        # it (INV-14). Two gates are re-checked at the last moment
        # (§18.4/§22.1): mode must still be enforce, and the audited
        # notice's input fingerprint must still be live — a degraded
        # notice is instead suppressed if its base event has since
        # delivered.
        _semantic_gate(ledger, ev, payload)
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("payload_invalid")
        return text, []
    if ev["kind"] == "signal":
        # Review-candidate notice: resolve the current explanation/evidence.
        # Last-moment gates like semantic_notice: the flag may have been
        # turned off, or the signal may have resolved while queued —
        # both are terminal drops, not retries.
        sig_cfg = _config().get("signals")
        if not (isinstance(sig_cfg, dict)
                and sig_cfg.get("notify") is True):
            raise _StaleSend("signals_notify_disabled")
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
            raise _StaleSend("signal_not_open")
        # Rebuild text at send time: notes can change while evidence
        # IDs stay the same, a digest re-groups its live members, and a
        # merged med notice shrinks to whoever is still open.
        if payload.get("digest") is True:
            units = mcs_signals.sig_units(
                [(mcs_signals.med_group_key(s), s) for s in open_sigs])
            parts = [_signal_unit_text(ledger, us) for us in units]
            text = (f"[MCS] レビュー候補ダイジェスト"
                    f"（{len(open_sigs)}件）\n\n" + "\n\n".join(parts))
        else:
            text = _signal_unit_text(ledger, open_sigs)
            if payload.get("urgent") is True:
                head, _, tail = text.partition("\n")
                text = f"{head} — 原投稿が urgency:high" \
                       + (f"\n{tail}" if tail else "")
        if not isinstance(text, str) or not text:
            raise ValueError("payload_invalid")
        return text, []
    if ev["kind"] == "attachment_followup":
        # a body notice went out before this file downloaded — deliver
        # just the file now (F11). If the file is no longer sendable
        # (pruned/withdrawn/never finished) the intent is terminal.
        aid = payload.get("attachment_id")
        if type(aid) is not int:
            raise ValueError("payload_invalid")
        a = ledger.db.execute(
            "SELECT message_id,file_id,name,state,local_path,bytes,sha256"
            " FROM attachments WHERE attachment_id=?", (aid,)).fetchone()
        if not a or a["state"] != "downloaded" or not a["local_path"]:
            raise _StaleSend("attachment_not_ready")
        files = _collect_files({a["message_id"]: [a]}, [a["message_id"]])
        if not files:
            raise _StaleSend("attachment_unsendable")
        return (f"[MCS] 添付ファイル（後送）\n{a['name'] or 'file'}"), files
    ids = payload.get("message_ids") or []
    if (not isinstance(ids, list)
            or any(type(mid) is not int or mid <= 0 for mid in ids)):
        raise ValueError("payload_invalid")
    rows = []
    for mid in ids:
        r = ledger.db.execute("""
          SELECT m.*, p.patient_name FROM messages m
          LEFT JOIN patients p ON p.project_id = m.project_id
          WHERE m.message_id=?""", (mid,)).fetchone()
        if r:
            rows.append(r)
    src = payload.get("source", "unread")
    if not rows:
        return f"[MCS] 新着 {len(ids)} 件 (project {ev['project_id']})", []
    att_map = _attachments_map(ledger, [r["message_id"] for r in rows])
    # group replies under their parent when both are new in this event
    by_id = {r["message_id"]: r for r in rows}
    parents = [r for r in rows if not r["parent_id"]
               or r["parent_id"] not in by_id]
    kids = {}
    for r in rows:
        if r["parent_id"] and r["parent_id"] in by_id:
            kids.setdefault(r["parent_id"], []).append(r)

    def _fmt(r, indent=""):
        s_lines = _structured_lines(ledger, r["message_id"])
        urg = _urgency(ledger, r["message_id"])
        body = html_to_text(r["body_html"])
        cap = 500 if s_lines else 600
        if len(body) > cap:
            body = body[:cap] + "…"
        who = r["sender_name"] or "不明"
        meta = " / ".join(x for x in (r["profession"], r["organization"]) if x)
        parent = " (返信)" if r["parent_id"] and not indent else ""
        state = "" if r["body_state"] == "full" else f" [{r['body_state']}]"
        warn = " ⚠️" if urg == "high" else ""
        head = (f"{indent}**{r['patient_name'] or ev['project_id']}**"
                f"{parent}{state}{warn}\n"
                f"{indent}{who}{f' ({meta})' if meta else ''} — "
                f"{r['posted_at'][:16]}")
        atts = att_map.get(r["message_id"], [])
        att_line = ""
        if atts:
            marks = []
            for a in atts:
                nm = str(a["name"] or a["file_id"] or "file")
                if a["state"] == "downloaded":
                    if (a["bytes"] or 0) > _MAX_FILE_BYTES:
                        nm += " (25MB超·未送信)"
                else:
                    nm += " (未取得)"
                marks.append(nm)
            att_line = f"\n{indent}📎 {'、'.join(marks[:5])}"
        if s_lines:
            struct = "\n".join(f"{indent}・{ln}" for ln in s_lines)
            return (f"{head}\n{indent}📋 構造化\n{struct}\n"
                    f"{indent}───── 原文 ─────\n"
                    f"{indent}{body or '(本文なし)'}{att_line}")
        return f"{head}\n{indent}{body or '(本文なし)'}{att_line}"

    def _sem_block(r):
        """Enforce-mode: audited summary section appended to this post.
        Only the newest semantic_summary with audit_status PASS on the
        CURRENT generation qualifies — stale/failed/unaudited ones are
        silently absent, and in shadow/assist/off the notification stays
        byte-identical to pre-Phase-J (INV-16, spec §20.2)."""
        try:
            import semantic as _sem
            from mcs_operations import paused
            if paused(ledger.db, r["project_id"]):
                return ""
            scfg, _ = _sem.semantic_config(_config())
            if scfg["mode"] != "enforce" or scfg["summary_mode"] != "enforce":
                return ""
            if scfg["project_ids"] is not None and r["project_id"] not in scfg["project_ids"]:
                return ""
            art = ledger.db.execute(
                "SELECT content,meta FROM artifacts "
                "WHERE kind='semantic_summary' AND message_id=? "
                "ORDER BY artifact_id DESC LIMIT 1",
                (r["message_id"],)).fetchone()
            if not art:
                return ""
            meta = json.loads(art["meta"] or "{}")
            if (meta.get("audit_status") != "PASS" or meta.get("stale")
                    or meta.get("publication_mode") != "enforce"
                    or meta.get("policy_fingerprint") != _sem.policy_fingerprint(scfg)):
                return ""
            try:
                import semantic as _sem
                root = r["parent_id"] or r["message_id"]
                bundle = _sem.thread_bundle(ledger, r["project_id"], root,
                                             [r["message_id"]])
            except Exception:
                return ""
            # The audited summary must describe the current whole-thread
            # generation, not only the target's body revision (INV-15).
            if bundle is None or meta.get("fingerprint") != \
                    bundle["source_fingerprint"]:
                return ""
            target = next((m for m in bundle["members"]
                           if m["message_id"] == r["message_id"]), None)
            if target is None or meta.get("target_revision") != \
                    target["revision"]:
                return ""
            summ = json.loads(art["content"])
            # Keep every audited claim; the existing notifier chunker is the
            # one place that splits a notification.
            lines = [str(c.get("text", ""))
                     for c in summ.get("claims", [])
                     if isinstance(c, dict) and c.get("text")]
            if not lines:
                return ""
            body = "\n".join(f"・{x}" for x in lines)
            lims = [str(x) for x in (summ.get("limitations") or [])
                    if isinstance(x, str)]
            if lims:
                body += "\n・（原文確認）" + "；".join(lims)
            return f"\n───── 要約（自動検査済） ─────\n{body}"
        except Exception:
            return ""   # never let a summary render break delivery

    out = []
    order = []
    for r in parents:
        out.append(_fmt(r) + _sem_block(r))
        order.append(r["message_id"])
        for k in sorted(kids.get(r["message_id"], []),
                        key=lambda x: x["posted_at"]):
            out.append(_fmt(k, "↳ "))
            order.append(k["message_id"])
    head = f"[MCS {src}] 新着 {len(rows)} 件"
    return (head + "\n\n" + "\n\n".join(out),
            _collect_files(att_map, order))


def _semantic_stale(reason: str, in_progress: bool):
    if in_progress:
        raise _FreezeSend(reason)
    raise _StaleSend(reason)


def _semantic_summary_current(ledger, project_id: int, message_id: int,
                              bundle: dict,
                              fingerprint: str, policy: str) -> bool:
    """Require the frozen notice to still have its current PASS summary."""
    row = ledger.db.execute(
        "SELECT content,meta FROM artifacts "
        "WHERE kind='semantic_summary' AND project_id=? AND message_id=? "
        "ORDER BY artifact_id DESC LIMIT 1",
        (project_id, message_id)).fetchone()
    if row is None:
        return False
    try:
        content = json.loads(row["content"] or "null")
        meta = json.loads(row["meta"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(content, dict) or not isinstance(meta, dict):
        return False
    target = next((m for m in bundle.get("members", [])
                   if m["message_id"] == message_id), None)
    return (meta.get("audit_status") == "PASS"
            and not meta.get("stale")
            and meta.get("publication_mode") == "enforce"
            and meta.get("policy_fingerprint") == policy
            and meta.get("fingerprint") == fingerprint
            and target is not None
            and meta.get("target_revision") == target["revision"])


def _semantic_source_event(ledger, project_id: int, event_id: int):
    """Load an origin event only inside the semantic notice's project."""
    row = ledger.db.execute(
        "SELECT state,attempts,progress,payload FROM notify_outbox "
        "WHERE event_id=? AND kind='new_messages' AND project_id=?",
        (event_id, project_id)).fetchone()
    if row is None:
        return None, None
    try:
        source = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return row, None
    return row, source if isinstance(source, dict) else None


def _semantic_gate(ledger, ev, payload: dict, in_progress: bool = False):
    """Recheck a semantic intent immediately before every external post."""
    import semantic as _sem
    if not isinstance(payload, dict):
        raise ValueError("payload_invalid")
    scfg, _ = _sem.semantic_config(_config())
    if scfg["mode"] != "enforce" or scfg["summary_mode"] != "enforce":
        if in_progress:
            raise _FreezeSend("semantic_not_enforce")
        raise _DeferredSend("semantic_not_enforce")
    project_id = ev["project_id"]
    if type(project_id) is not int or project_id <= 0:
        raise ValueError("payload_invalid")
    from mcs_operations import paused
    if paused(ledger.db, project_id):
        raise _FreezeSend("semantic_paused") if in_progress else _DeferredSend("semantic_paused")
    if scfg["project_ids"] is not None and project_id not in scfg["project_ids"]:
        raise _FreezeSend("project_out_of_scope") if in_progress \
            else _DeferredSend("project_out_of_scope")
    policy_version = payload.get("policy_version")
    if not isinstance(policy_version, str) \
            or policy_version != _sem.POLICY_VERSION:
        _semantic_stale("policy_changed", in_progress)
    degraded = payload.get("degraded", False)
    if type(degraded) is not bool:
        raise ValueError("payload_invalid")
    policy = payload.get("policy_fingerprint")
    if not isinstance(policy, str) \
            or policy != _sem.policy_fingerprint(scfg):
        _semantic_stale("policy_changed", in_progress)
    fp = payload.get("fingerprint")
    root = payload.get("root_id")
    src = payload.get("src_event_id")
    if not isinstance(fp, str) or not fp or type(root) is not int \
            or root <= 0 or type(src) is not int or src <= 0:
        raise ValueError("payload_invalid")
    source_row, source = _semantic_source_event(ledger, project_id, src)
    if source_row is None or source is None:
        _semantic_stale("src_event_ineligible", in_progress)
    try:
        source_ids = source.get("message_ids")
        if (not isinstance(source_ids, list)
                or any(type(mid) is not int or mid <= 0 for mid in source_ids)):
            raise ValueError
        source_ids = set(source_ids)
    except (AttributeError, ValueError):
        _semantic_stale("src_event_ineligible", in_progress)
    if source_row["state"] == "suppressed":
        _semantic_stale("src_event_ineligible", in_progress)
    if degraded:
        target_ids = payload.get("target_message_ids")
        if (not isinstance(target_ids, list) or not target_ids
                or any(type(mid) is not int or mid <= 0 for mid in target_ids)
                or len(set(target_ids)) != len(target_ids)
                or not set(target_ids).issubset(source_ids)):
            raise ValueError("payload_invalid")
        try:
            progress = json.loads(source_row["progress"] or "{}")
        except (json.JSONDecodeError, TypeError):
            progress = None
        if (source_row["state"] == "accepted"
                or source_row["attempts"] > 0
                or not isinstance(progress, dict)
                or bool(progress.get("sent"))):
            _semantic_stale("base_delivered", in_progress)
        bundle = _sem.thread_bundle(ledger, project_id, root)
        if bundle is None or bundle["source_fingerprint"] != fp:
            _semantic_stale("stale_generation", in_progress)
        member_ids = {m["message_id"] for m in bundle["members"]}
        if not set(target_ids).issubset(member_ids):
            _semantic_stale("stale_generation", in_progress)
    else:
        target = payload.get("target_message_id")
        target_revision = payload.get("target_revision")
        if (type(target) is not int or target <= 0
                or not isinstance(target_revision, str) or not target_revision
                or target not in source_ids):
            raise ValueError("payload_invalid")
        bundle = _sem.thread_bundle(ledger, project_id, root)
        if bundle is None or bundle["source_fingerprint"] != fp:
            _semantic_stale("stale_generation", in_progress)
        member = next((m for m in bundle["members"]
                       if m["message_id"] == target), None)
        if member is None or member["revision"] != target_revision:
            _semantic_stale("stale_generation", in_progress)
        if not _semantic_summary_current(ledger, project_id, target, bundle,
                                         fp, policy):
            _semantic_stale("summary_stale", in_progress)


def _semantic_render_state(ledger, ev) -> tuple:
    """Current identities of audited summaries attached to a raw notice."""
    import semantic as _sem
    from mcs_operations import paused
    if paused(ledger.db, ev["project_id"]):
        return ()
    scfg, _ = _sem.semantic_config(_config())
    if scfg["mode"] != "enforce" or scfg["summary_mode"] != "enforce":
        return ()
    if scfg["project_ids"] is not None and ev["project_id"] not in scfg["project_ids"]:
        return ()
    project_id = ev["project_id"]
    if type(project_id) is not int or project_id <= 0:
        return ()
    try:
        payload = json.loads(ev["payload"])
        ids = payload.get("message_ids") or []
        import semantic as _sem
    except Exception:
        return ()
    state = []
    for mid in ids if isinstance(ids, list) else []:
        if type(mid) is not int:
            continue
        row = ledger.db.execute(
            "SELECT project_id,message_id,parent_id,content_hash "
            "FROM messages WHERE project_id=? AND message_id=?",
            (project_id, mid)).fetchone()
        if row is None:
            continue
        art = ledger.db.execute(
            "SELECT artifact_id,content,meta FROM artifacts "
            "WHERE kind='semantic_summary' AND project_id=? "
            "AND message_id=? ORDER BY artifact_id DESC LIMIT 1",
            (project_id, mid)).fetchone()
        if art is None:
            continue
        try:
            meta = json.loads(art["meta"] or "{}")
            root = row["parent_id"] or row["message_id"]
            bundle = _sem.thread_bundle(ledger, row["project_id"], root, [mid])
        except Exception:
            continue
        target = next((m for m in (bundle or {}).get("members", [])
                       if m["message_id"] == mid), None)
        if (bundle is None or meta.get("audit_status") != "PASS"
                or meta.get("stale")
                or meta.get("publication_mode") != "enforce"
                or meta.get("policy_fingerprint") != _sem.policy_fingerprint(scfg)
                or meta.get("fingerprint") != bundle["source_fingerprint"]
                or target is None
                or meta.get("target_revision") != target["revision"]):
            continue
        state.append((mid, art["artifact_id"], meta["fingerprint"]))
    return tuple(state)


def _semantic_render_gate(ledger, ev, initial: tuple,
                          in_progress: bool = False):
    if not initial:
        return
    cfg = _config().get("semantic")
    current = _semantic_render_state(ledger, ev)
    if not isinstance(cfg, dict) or cfg.get("mode") != "enforce":
        if in_progress:
            raise _FreezeSend("semantic_not_enforce")
        raise _DeferredSend("semantic_not_enforce")
    if current != initial:
        raise _FreezeSend("semantic_generation_changed") if in_progress \
            else _StaleSend("semantic_generation_changed")


def _semantic_chunks(content: str) -> list[str]:
    """Split a frozen semantic notice while repeating its provenance.

    ``semantic.render_notice`` already puts the patient, coverage/audit
    labels, and stored MCS URL in the frozen payload. A raw character slice
    would leave those lines only in part 1, so each part gets the same frozen
    header and link. The payload is never rebuilt from the ledger here.
    """
    lines = content.splitlines()
    footer_index = next(
        (i for i, line in enumerate(lines) if line == "▶ MCSで確認"),
        None,
    )
    link_index = next(
        (i for i, line in enumerate(lines[footer_index + 1:], footer_index + 1)
         if line.startswith("https://")),
        None,
    ) if footer_index is not None else None
    if link_index is None or footer_index is None:
        raise ValueError("semantic_provenance_missing")
    link = lines[link_index].strip()
    if not link.startswith("https://"):
        raise ValueError("semantic_provenance_missing")

    # The renderer's leading block is stable and ends at the first blank line
    # or section. Keep every line in that block so target time and any
    # degraded-notice explanation remain visible on every part.
    header_end = len(lines)
    for index, line in enumerate(lines[:footer_index]):
        if not line.strip() or line.startswith("■"):
            header_end = index
            break
    header = [line for line in lines[:header_end] if line.strip()]
    if not any(line.startswith("取得：") for line in header):
        raise ValueError("semantic_coverage_missing")
    if not any(line.startswith("要約：") for line in header):
        raise ValueError("semantic_audit_missing")

    # Remove the original header/footer before reattaching them to every
    # chunk. This keeps the frozen claims intact without duplicating the
    # first block inside part 1.
    body_start = header_end
    while body_start < footer_index and not lines[body_start].strip():
        body_start += 1
    body_lines = lines[body_start:footer_index]
    body = "\n".join(body_lines).strip()

    footer = ["▶ MCSで確認", link]
    static = "\n".join(header + footer)
    count = 1
    chunks = []
    for _ in range(20):
        marker = f"part {count}/{count}"
        overhead = len(static) + len(marker) + 3
        capacity = _MAX_LEN - overhead
        if capacity < 1:
            raise ValueError("semantic_provenance_too_long")
        chunks = [body[i:i + capacity] for i in range(0, len(body), capacity)]
        if not chunks:
            chunks = [""]
        next_count = len(chunks)
        if next_count == count:
            break
        count = next_count
    else:
        raise ValueError("semantic_chunking_failed")

    out = []
    for index, chunk in enumerate(chunks, 1):
        marker = f"part {index}/{count}"
        part = "\n".join(header + [marker, chunk, *footer])
        if len(part) > _MAX_LEN:
            raise ValueError("semantic_chunk_limit")
        out.append(part)
    return out


def _has_sent_progress(ev) -> bool:
    try:
        progress = json.loads(ev["progress"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return type(progress.get("next")) is int and progress["next"] > 0


def _send_never_began(ev) -> bool:
    """No delivery could have happened: the progress receipt is absent
    entirely (the pre-send marker write precedes any subprocess) or it
    records no accepted AND no in-flight chunk. An UNPARSEABLE receipt
    is ambiguous — a corrupted record of a real send — so it fails
    safe (False)."""
    raw = ev["progress"]
    if not raw:
        return True
    try:
        progress = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(progress, dict):
        return False
    return (not progress.get("next") and progress.get("sending") is None
            and not progress.get("sent"))


def _hold_event(ledger, ev, cfg, proven_undelivered=False):
    """Quarantine an event; for an unsent digest payload, first move
    its still-open member keys into a fresh scheduled digest — a held
    intent stops covering its keys but nothing else would ever
    re-enqueue them (signals notify only at the open transition), so
    without this the members would go permanently silent. Salvage runs
    only when non-delivery is provable: an empty/no-send receipt, or a
    send path that refused before posting (proven_undelivered)."""
    if (proven_undelivered or _send_never_began(ev)) \
            and not _has_sent_progress(ev):
        try:
            payload = json.loads(ev["payload"])
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            rescuable = (
                payload.get("digest") is True
                or payload.get("signal_keys")
                or payload.get("signal_key")
                or (ev["kind"] == "new_messages"
                    and payload.get("rescue_of") is None
                    and any(type(m) is int
                            for m in (payload.get("message_ids") or []))))
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
                        mids = [m for m in (payload.get("message_ids")
                                            or []) if type(m) is int]
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
                        "UPDATE notify_outbox SET state='failed',"
                        "next_try=NULL,updated_at=? WHERE event_id=?",
                        (time.time(), ev["event_id"]))
                return
    ledger.outbox_hold(ev["event_id"])


class _SendFailed(OSError):
    """Delivery did not begin; the outbox may safely retry."""


class _SendUncertain(OSError):
    """Delivery began but acceptance is unknown; never retry automatically."""


class _SendUsage(Exception):
    """`hermes send` refused the invocation itself (exit 2) — a CLI/
    config contract problem that retrying cannot fix → outbox_hold."""


def _media_path(name: str, path: str) -> str:
    """MEDIA: delivery path for a stored attachment. Downloaded files are
    saved extensionless (attachments/<id>); platforms derive the upload
    filename from the path basename, so a bare id posts as an
    extensionless blob Discord won't render inline. Alias the file to
    `<path><ext>` (hardlink, copy fallback) carrying the original
    extension so the upload keeps a renderable filename."""
    ext = os.path.splitext(name)[1].lower() if name else ""
    if (not ext or not re.fullmatch(r"\.[a-z0-9]{1,8}", ext)
            or os.path.splitext(path)[1]):
        return path
    alias = path + ext
    if os.path.exists(alias):
        return alias
    try:
        os.link(path, alias)
    except OSError:
        try:
            shutil.copy2(path, alias)
        except OSError:
            return path
    return alias


# `hermes send` parses MEDIA:/voice/document directives from the WHOLE
# stdin stream — untrusted content (post bodies, sender names, LLM
# output) must never form one, or a crafted post could attach any
# readable local file to the notification. The keyword is defused to a
# fullwidth form (visible, non-parsing); the [[...]] directives get
# single brackets. Verified attachments are appended afterwards as
# genuine directives — see _compose_body.
_CONTROL_TAG_RE = re.compile(r"(?i)([`\"'*_]{0,3})MEDIA:")
_DOUBLE_BRACKET_RE = re.compile(
    r"\[\[(as_document|audio_as_voice)\]\]", re.IGNORECASE)


def _defuse_control_syntax(text: str) -> str:
    text = _CONTROL_TAG_RE.sub(r"\1MEDIA：", text)
    return _DOUBLE_BRACKET_RE.sub(r"[\1]", text)


def _compose_body(content: str,
                  files: list[tuple[str, str]] | None) -> str:
    body = _defuse_control_syntax(content)
    if files:
        body += "".join(f"\nMEDIA:{_media_path(fn, path)}"
                        for fn, path in files)
    return body


def _send(argv: list[str], content: str,
          files: list[tuple[str, str]] | None = None,
          deadline: float | None = None) -> None:
    """One chunk via `hermes send` (body on stdin; attachments as MEDIA:
    references — the adapter owns upload limits and mention policy)."""
    body = _compose_body(content, files)
    timeout = 180
    if deadline is not None:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise _SendFailed("deadline_exceeded")
        timeout = min(timeout, remain)
    try:
        r = subprocess.run(argv, input=body, text=True,
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise _SendUncertain("hermes send timed out") from e
    except OSError as e:
        raise _SendFailed("hermes send could not start") from e
    detail = (r.stderr or r.stdout or "").strip()[:400]
    if r.returncode == 2:
        raise _SendUsage(detail or "hermes send usage error")
    if r.returncode != 0:
        # A failed child can have delivered text or some attachments
        # before losing its response. Its exit status is not a negative ACK.
        raise _SendUncertain(detail or f"hermes send exit {r.returncode}")


def _delivery_fingerprint(target: str, chunks: list[str],
                          files: list[tuple[str, str]]) -> str:
    h = hashlib.sha256(target.encode())
    for chunk in chunks:
        h.update(b"\0text\0")
        h.update(chunk.encode())
    for name, path in files:
        h.update(b"\0file\0")
        h.update(name.encode())
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
    return h.hexdigest()


def _send_marked(ledger, ev, i: int, sent_ids: list[str],
                 fingerprint: str, argv: list[str], chunk: str,
                 files: list[tuple[str, str]] | None,
                 deadline: float | None) -> None:
    """Retain the in-flight marker unless non-delivery is established."""
    ledger.outbox_progress(ev["event_id"], i, list(sent_ids),
                           fingerprint, i + 1)
    try:
        _send(argv, chunk, files, deadline=deadline)
    except (_SendFailed, _SendUsage):
        ledger.outbox_progress(ev["event_id"], len(sent_ids),
                               list(sent_ids), fingerprint)
        raise


def _progress(raw: str, count: int) -> tuple[int, list[str], str | None,
                                            int | None]:
    try:
        d = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        raise ValueError("progress_invalid") from e
    if not isinstance(d, dict) or type(d.get("next", 0)) is not int:
        raise ValueError("progress_invalid")
    next_chunk = d.get("next", 0)
    sent = d.get("sent", [])
    fingerprint = d.get("fingerprint")
    sending = d.get("sending")
    if (next_chunk < 0 or next_chunk > count or not isinstance(sent, list)
            or len(sent) != next_chunk
            or any(not isinstance(x, str) or not x.isdigit() for x in sent)
            or (fingerprint is not None and not isinstance(fingerprint, str))
            or (sending is not None and type(sending) is not int)):
        raise ValueError("progress_invalid")
    return next_chunk, sent, fingerprint, sending


def _route(ev) -> str:
    """Row/dict-safe route read — a fake outbox in tests (or an
    un-migrated caller) that lacks the column is a text event."""
    try:
        return ev["route"] or "text"
    except (KeyError, IndexError):
        return "text"


def flush(ledger, limit: int = 10, deadline: float | None = None) -> dict:
    cfg = _config()
    res = {"sent": 0, "failed": 0, "skipped": 0, "suppressed": 0,
           "parked": 0}
    due = ledger.outbox_due(limit)
    exe = _hermes_exe(cfg)
    exe_ok = os.path.isfile(exe) and os.access(exe, os.X_OK)
    if not exe_ok and not any(
            _route(e) == "interactive" for e in due):
        res["skipped"] = len(due)
        return res
    for event_index, ev in enumerate(due):
        if deadline is not None and time.monotonic() >= deadline:
            res["skipped"] += len(due) - event_index
            break
        if ev["project_id"] and ledger.is_archived(ev["project_id"]):
            # queued before the patient was archived — archived patient
            # events must never reach the channel; drop terminally, do
            # not count as sent OR as a retryable failure (Oracle F2)
            ledger.outbox_suppress(ev["event_id"])
            res["suppressed"] += 1
            continue
        if _route(ev) == "interactive":
            import notify_cards
            sealed = ledger.db.execute(
                "SELECT 1 FROM notification_intent_batches "
                "WHERE event_id=?", (ev["event_id"],)).fetchone()
            if sealed or notify_cards.interactive_enabled(cfg):
                # sealed intents stay card-owned even with the switch
                # off — a text send here could double-deliver; begins
                # are denied until the switch returns
                try:
                    outcome = notify_cards.dispatch_intent(
                        ledger, ev, cfg)
                    if outcome.get("error"):
                        ledger.outbox_mark(ev["event_id"], "failed",
                                           retry_in=3600)
                        res["failed"] += 1
                    elif outcome.get("suppressed"):
                        res["suppressed"] += 1
                    elif not outcome.get("skipped"):
                        res["dispatched"] = res.get("dispatched", 0) + 1
                except Exception:
                    if ev["attempts"] >= 4:
                        ledger.outbox_hold(ev["event_id"])
                    else:
                        ledger.outbox_mark(ev["event_id"], "failed",
                                           retry_in=3600)
                    res["failed"] += 1
                continue
            # kill switch: an UNSEALED interactive intent is provably
            # unsent — convert once and let it flush as plain text
            notify_cards.revert_to_text(ledger, ev["event_id"])
        if not exe_ok:
            res["skipped"] += len(due) - event_index
            break
        target = _target(cfg, ev["kind"])
        if not target:
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=3600)
            res["failed"] += 1
            continue
        argv = _send_argv(cfg, target)
        try:
            content, files = _format_event(ledger, ev)
            render_state = (_semantic_render_state(ledger, ev)
                            if ev["kind"] == "new_messages" else ())
            chunks = (_semantic_chunks(content)
                      if ev["kind"] == "semantic_notice" else
                      [content[i:i + _MAX_LEN]
                       for i in range(0, len(content), _MAX_LEN)])
            # resume at the first unacknowledged chunk — a crash after a
            # partial send must not duplicate accepted chunks (B25)
            start, sent_ids, previous, sending = _progress(
                ev["progress"], len(chunks))
            if sending is not None and sending > start:
                # a previous attempt began chunk `sending` and died before
                # recording the ack — delivery is UNCERTAIN: resending
                # could duplicate a post that did go out. Hold it for
                # human reconciliation instead (F19)
                _hold_event(ledger, ev, cfg)
                res["uncertain"] = res.get("uncertain", 0) + 1
                continue
            fingerprint = _delivery_fingerprint(target, chunks, files)
            if start and previous != fingerprint:
                _hold_event(ledger, ev, cfg)
                res["failed"] += 1
                continue
            if not start:
                ledger.outbox_progress(ev["event_id"], 0, [], fingerprint)
            post_files = files
            for i in range(start, len(chunks)):
                if deadline is not None and time.monotonic() >= deadline:
                    res["skipped"] += len(due) - event_index
                    return res
                if ev["kind"] == "semantic_notice":
                    try:
                        payload = json.loads(ev["payload"])
                    except (json.JSONDecodeError, TypeError) as e:
                        raise ValueError("payload_invalid") from e
                    _semantic_gate(ledger, ev, payload,
                                   in_progress=bool(start or i))
                elif render_state:
                    _semantic_render_gate(ledger, ev, render_state,
                                          in_progress=bool(start or i))
                # files ride the FIRST post only; on resume (start>0) they
                # were already delivered with chunk 0. A usage rejection
                # of the file-bearing send (exit 2 — never a delivery
                # failure where acceptance is unknown) drops the files
                # and retries text-only so a bad attachment can never
                # sink the notification itself
                try:
                    _send_marked(ledger, ev, i, sent_ids, fingerprint,
                                 argv, chunks[i],
                                 post_files if i == 0 else None,
                                 deadline)
                except _SendUsage:
                    if i != 0 or not post_files:
                        raise
                    post_files = None
                    if ev["kind"] == "semantic_notice":
                        _semantic_gate(ledger, ev, payload,
                                       in_progress=bool(start or i))
                    elif render_state:
                        _semantic_render_gate(ledger, ev, render_state,
                                              in_progress=bool(start or i))
                    _send_marked(ledger, ev, i, sent_ids, fingerprint,
                                 argv, chunks[i], None, deadline)
                sent_ids.append(str(i + 1))
                ledger.outbox_progress(ev["event_id"], i + 1, sent_ids,
                                       fingerprint)
            ledger.outbox_mark(ev["event_id"], "accepted",
                               sent_ids[-1] if sent_ids else "")
            res["sent"] += 1
        except _DeferredSend:
            # stays pending but re-checks hourly, not every flush. A
            # parked enforce intent is policy-blocked, not incomplete —
            # count it separately so run_check's notify_incomplete signal
            # doesn't flag every other tick while the mode gate is down
            if _has_sent_progress(ev):
                _hold_event(ledger, ev, cfg)
                res["failed"] += 1
            else:
                ledger.db.execute(
                    "UPDATE notify_outbox SET next_try=?,updated_at=? "
                    "WHERE event_id=?",
                    (time.time() + 3600, time.time(), ev["event_id"]))
                ledger.db.commit()
                res["parked"] += 1
        except _FreezeSend:
            # An accepted chunk is immutable. Keep its receipt/progress and
            # freeze the remaining chunks until an explicit retry decision.
            _hold_event(ledger, ev, cfg)
            res["failed"] += 1
        except _StaleSend:
            if _has_sent_progress(ev):
                _hold_event(ledger, ev, cfg)
                res["failed"] += 1
            else:
                ledger.outbox_suppress(ev["event_id"])
                res["suppressed"] += 1
        except _SendUsage:
            # invocation itself refused (exit 2) — a CLI/config contract
            # problem; retrying cannot fix it, quarantine the event.
            # The refusal is provably pre-delivery, so held digest
            # members may be salvaged.
            _hold_event(ledger, ev, cfg, proven_undelivered=True)
            res["failed"] += 1
        except _SendUncertain:
            _hold_event(ledger, ev, cfg)
            res["uncertain"] = res.get("uncertain", 0) + 1
            res["failed"] += 1
        except ValueError:
            _hold_event(ledger, ev, cfg)
            res["failed"] += 1
        except (OSError, TimeoutError, KeyError):
            backoff = min(3600, 60 * (2 ** ev["attempts"]))
            ledger.outbox_mark(ev["event_id"], "failed", retry_in=backoff)
            res["failed"] += 1
        except Exception:
            # Per-event containment: an unexpected failure inside
            # _format_event/_semantic_gate (broken import, sqlite error)
            # must not escape flush and starve every later due event.
            # Retry hourly first — a transient fault self-heals; a
            # deterministic bug quarantines after 5 attempts instead of
            # looping forever.
            if ev["attempts"] >= 4:
                _hold_event(ledger, ev, cfg)
            else:
                ledger.outbox_mark(ev["event_id"], "failed",
                                   retry_in=3600)
            res["failed"] += 1
    return res
