#!/usr/bin/env python3
"""Independent MCS update recovery — lives OUTSIDE the repo
(~/.mcs-recovery/mcs_recover.py) so a broken new release can never
take the recovery path down with it (R5).

stdlib + git only. Works when the repo's new code can't import, the
Hermes gateway is dead, or the DB won't open. Driven by the journal in
data/update_state.json — never trusts HEAD alone (R1).

  python3 mcs_recover.py            classify + recover if needed
  python3 mcs_recover.py --if-stale watchdog mode: act only on an
                                  interrupted/stale apply
  python3 mcs_recover.py --status   print state + repo diagnosis

Writes data/recovery_report.json on every action; exits 0 when there
is nothing to do (watchdog-silent convention).
"""
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~/.mcs")
DATA = os.path.join(HOME, "data")
REPO = HOME
STATE_PATH = os.path.join(DATA, "update_state.json")
UPDATE_LOCK = os.path.join(DATA, "update.lock")
RUN_LOCK = os.path.join(DATA, "run.lock")
MARKER_PATH = os.path.join(DATA, "update_in_progress.marker")
MANIFEST_PATH = os.path.join(DATA, "service_manifest.json")
REPORT_PATH = os.path.join(DATA, "recovery_report.json")
LEDGER = os.path.join(DATA, "ledger.db")
AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")

RESIDENT_LABELS = ("ai.mcs.extract-drainer", "ai.mcs.extract-drainer-rt")
WATCHER_LABELS = ("local.mcs-cmd", "local.mcs-int")
EXCLUDED_LABELS = frozenset({"ai.mcs.llamaserver", "org.mcs.recovery"})
# current desired sets — kept in sync with mcs_setup.AGENT_LABELS /
# CRON_JOBS; recovery must never delete what the restored code wants
KNOWN_AGENT_LABELS = frozenset(RESIDENT_LABELS + WATCHER_LABELS)
KNOWN_CRON_SCRIPTS = frozenset({
    "mcs_check.sh", "mcs_deep.sh", "mcs_llm_catchup.sh",
    "mcs_update.sh", "llamacpp_restart_if_idle.sh"})
STALE_S = 1800
GIT_LOCK_MIN_AGE_S = 600
T_GIT = 30


def _git(args, timeout=T_GIT):
    try:
        return subprocess.run(["git", "-C", REPO, *args],
                              capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_out(args, timeout=T_GIT):
    r = _git(args, timeout)
    return r.stdout if r and r.returncode == 0 else ""


def _head():
    return _git_out(["rev-parse", "HEAD"]).strip()


def _clean():
    r = _git(["status", "--porcelain", "-uno"])
    return None if r is None or r.returncode != 0 else r.stdout.strip() == ""


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict) or s.get("v") != 1:
            return {"_corrupt": True}
        return s
    except FileNotFoundError:
        return {"v": 1, "stages": [], "applied": [], "applying": None}
    except (OSError, json.JSONDecodeError, ValueError):
        return {"_corrupt": True}


def _save_state(state):
    fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".ustate.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _report(result, detail):
    try:
        fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".rpt.",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"result": result, "detail": detail,
                       "at": time.time()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, REPORT_PATH)
    except OSError:
        pass


def _try_lock(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _agent_pid(label):
    r = subprocess.run(["launchctl", "print",
                        f"gui/{os.getuid()}/{label}"],
                       capture_output=True, text=True, timeout=T_GIT)
    if r.returncode != 0:
        return None
    m = re.search(r"^\s*pid\s*=\s*(\d+)", r.stdout, re.M)
    return int(m.group(1)) if m else None


def _restart_drainers():
    problems = []
    for label in RESIDENT_LABELS:
        plist = os.path.join(AGENTS_DIR, label + ".plist")
        subprocess.run(["launchctl", "bootout",
                        f"gui/{os.getuid()}/{label}"],
                       capture_output=True, timeout=T_GIT)
        if os.path.exists(plist):
            subprocess.run(["launchctl", "bootstrap",
                            f"gui/{os.getuid()}", plist],
                           capture_output=True, timeout=T_GIT)
        deadline = time.time() + 15
        while time.time() < deadline:
            if _agent_pid(label):
                break
            time.sleep(0.5)
        if _agent_pid(label) is None:
            problems.append(label)
    return problems


def _restart_gateway():
    subprocess.Popen(
        ["launchctl", "kickstart", "-k",
         f"gui/{os.getuid()}/ai.hermes.gateway"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, close_fds=True,
        start_new_session=True)


def _clean_stale_git_locks():
    """Only locks OLDER than GIT_LOCK_MIN_AGE_S — a fresh lock may
    belong to an unrelated live `git` process. A free update.lock
    proves no updater is alive, not that no git is running (H3)."""
    removed = []
    cutoff = time.time() - GIT_LOCK_MIN_AGE_S
    for path in glob.glob(os.path.join(REPO, ".git", "**", "*.lock"),
                          recursive=True):
        try:
            if os.path.getmtime(path) >= cutoff:
                continue
            os.unlink(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def _reconcile_membership(snapshot):
    """Delete owned-but-undesired cron entries + agents per the
    pre-update manifest snapshot. Union with the CURRENT desired sets
    so a no-op rollback never deletes what it still wants; recreation
    belongs to the repo's own `services` run (old code re-renders)."""
    problems = []
    desired_cron = {os.path.basename(c.get("script") or "")
                    for c in (snapshot or {}).get("cron", [])
                    if isinstance(c, dict)}
    desired_agents = {a.get("label") for a in
                      (snapshot or {}).get("agents", [])
                      if isinstance(a, dict)}
    for path in glob.glob(os.path.join(AGENTS_DIR, "*.plist")):
        label = os.path.basename(path)[:-6]
        owned = (label.startswith("local.mcs-")
                 or label.startswith("ai.mcs.extract-")) \
            and label not in EXCLUDED_LABELS
        if owned and label not in desired_agents \
                and label not in KNOWN_AGENT_LABELS:
            subprocess.run(["launchctl", "bootout",
                            f"gui/{os.getuid()}/{label}"],
                           capture_output=True, timeout=T_GIT)
            try:
                os.unlink(path)
            except OSError:
                pass
    # cron: remove owned mcs_*.sh entries that are neither in the
    # snapshot nor in the current desired set
    hermes = shutil.which("hermes") \
        or os.path.expanduser("~/.local/bin/hermes")
    if os.path.isfile(hermes):
        try:
            r = subprocess.run([hermes, "cron", "list", "--all"],
                               capture_output=True, text=True,
                               timeout=T_GIT)
            for block in re.finditer(
                    r"^\s{2}([0-9a-f]{6,})\s+\[[^\]]*\]\n"
                    r"((?:\s{4}\S[^\n]*\n?)+)", r.stdout, re.M):
                jid, body = block.group(1), block.group(2)
                fields = dict(re.findall(
                    r"^\s{4}(\w[\w ]*?):\s{2,}(.+)$", body, re.M))
                script = os.path.basename(
                    (fields.get("Script") or "").strip())
                if script.startswith("mcs_") and script.endswith(".sh") \
                        and script not in desired_cron \
                        and script not in KNOWN_CRON_SCRIPTS:
                    subprocess.run([hermes, "cron", "remove", jid],
                                   capture_output=True, timeout=T_GIT)
        except (OSError, subprocess.TimeoutExpired):
            problems.append("cron_list_unverifiable")
    else:
        problems.append("cron_list_unverifiable")
    # converge content/membership with the restored tree's own services
    setup_py = os.path.join(REPO, "mcs", "ops", "mcs_setup.py")
    if os.path.isfile(setup_py):
        try:
            subprocess.run([sys.executable, setup_py, "services"],
                           capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            problems.append("services_reconcile_failed")
    return problems


def _notify(text):
    """Best-effort — Hermes/Discord may be the very thing that's down;
    failure is silent by design."""
    try:
        import shutil
        hermes = shutil.which("hermes") \
            or os.path.expanduser("~/.local/bin/hermes")
        if os.path.isfile(hermes):
            subprocess.Popen([hermes, "send", "--to", "local",
                              "--quiet", text],
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             close_fds=True, start_new_session=True)
    except Exception:
        pass


def _db_version(path):
    try:
        con = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _mark_restored(backup_path, phase="restored", report_id=None):
    """restore_pending marker — the runner holds every send grant
    until its journal-vs-DB reconcile consumes this. Written BEFORE
    the file swap (a crash between replace and write must not leave
    senders running against a rewound DB; a stale marker is harmless).
    Duplicated from notify_cards.mark_restored because this script
    must keep working when the repo's own modules are broken.
    phase='awaiting_consent' holds sends while the schema-bump DB
    replace waits on a bound ops.restore_approve receipt."""
    marker = os.path.join(DATA, "restore_pending.json")
    payload = {"v": 1, "phase": phase, "backup_path": backup_path,
               "by": "mcs_recover", "at": time.time()}
    if phase == "restored":
        payload["restored_at"] = payload["at"]
    if report_id is not None:
        payload["report_id"] = report_id
    fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".restore.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, marker)
        dfd = os.open(DATA, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Records the DB replace would silently erase — duplicated from
# mcs_update so this script needs none of the repo's modules.
_STORED_TABLES = ("messages", "attachments")
_EFFECT_TABLES = ("notification_cards", "notification_renders",
                  "notification_delivery_attempts",
                  "notification_render_parts", "notification_restore_holds",
                  "notification_view_manifests", "notify_outbox")


def _table_count(con, table):
    try:
        return con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
    except sqlite3.Error:
        return 0


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _loss_report(backup_path):
    """Same deterministic loss report as mcs_update._restore_loss_report
    — a drift between report and live state invalidates every consent
    receipt bound to the stale report_id."""
    try:
        live = sqlite3.connect("file:" + LEDGER + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        back = sqlite3.connect(
            "file:" + backup_path + "?mode=ro", uri=True)
    except sqlite3.Error:
        live.close()
        return None
    try:
        try:
            watermark = back.execute(
                "SELECT MAX(posted_at_ts) FROM messages").fetchone()[0]
        except sqlite3.Error:
            watermark = None
        stored = {t: _table_count(live, t) - _table_count(back, t)
                  for t in _STORED_TABLES}
        effects = {t: _table_count(live, t) - _table_count(back, t)
                   for t in _EFFECT_TABLES}
    finally:
        live.close()
        back.close()
    metrics = {"v": 1,
               "backup_sha256": _file_sha256(backup_path),
               "backup_schema": _db_version(backup_path),
               "watermark_ts": watermark,
               "stored_since_backup": stored,
               "external_effects": effects,
               "intervening_messages": sum(max(0, n)
                                           for n in stored.values()),
               "external_effect_rows": sum(max(0, n)
                                           for n in effects.values())}
    metrics["report_id"] = hashlib.sha256(
        json.dumps(metrics, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    report = dict(metrics, computed_at=time.time())
    try:
        fd, tmp = tempfile.mkstemp(dir=DATA, prefix=".rreport.",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(DATA, "restore_report.json"))
        dfd = os.open(DATA, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass                            # report file is best-effort
    return report


def _consent_for(report):
    """The newest ops.restore_approve receipt bound to this exact loss
    report, or None — an earlier update/rollback approval never counts."""
    try:
        con = sqlite3.connect("file:" + LEDGER + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        try:
            rows = con.execute(
                "SELECT command_id, receipt_json FROM command_receipts"
                " WHERE outcome='applied'"
                " ORDER BY processed_at DESC, rowid DESC").fetchall()
        except sqlite3.Error:
            return None
    finally:
        con.close()
    for cid, rj in rows:
        try:
            rec = json.loads(rj)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("cmd") != "ops.restore_approve" \
                or rec.get("scheduled") is not True:
            continue
        if rec.get("report_id") == report["report_id"] \
                and rec.get("backup_sha256") == report["backup_sha256"] \
                and rec.get("backup_schema") == report["backup_schema"]:
            return cid
    return None


def _restore_db(backup_path):
    """Restore only when the live schema differs — removes WAL/SHM
    sidecars FIRST so stale journals can't replay against the file.
    Returns an error string for the caller to escalate, or None on a
    completed or verified-skipped restore — an unreadable backup during
    rollback recovery is never a silent 'nothing to do' (the mcs_update
    copy escalates backup_invalid in the same situation; a silent skip
    here would leave OLD code running against a NEWER schema while the
    report claims success)."""
    live = _db_version(LEDGER)
    back = _db_version(backup_path)
    if back is None:
        return "backup_unreadable: " + str(backup_path)
    if live is None:
        return "live_db_unreadable"
    if live == back:
        # already at backup schema — but a crash between the marker's
        # 'restored' rewrite and the swap can leave an awaiting_consent
        # marker pinning every send; promote it so the runner-side
        # reconcile releases the hold.
        marker = os.path.join(DATA, "restore_pending.json")
        try:
            with open(marker, "rb") as f:
                current = json.loads(f.read().decode("utf-8"))
        except (OSError, ValueError):
            current = None
        if isinstance(current, dict) \
                and current.get("phase") == "awaiting_consent":
            try:
                _mark_restored(backup_path)
            except OSError as e:
                return f"restore_marker_failed: {e}"
        return None
    # Per-restore human consent (R2): an earlier update/rollback
    # approval does not substitute. The sender hold lands before the
    # loss report so no grant slips between measurement and decision.
    try:
        _mark_restored(backup_path, phase="awaiting_consent")
    except OSError as e:
        return f"restore_marker_failed: {e}"
    report = _loss_report(backup_path)
    if report is None:
        return "restore_report_failed: cannot measure loss"
    try:
        # re-pin the hold with the report identity so operators can
        # correlate marker <-> restore_report.json
        _mark_restored(backup_path, phase="awaiting_consent",
                       report_id=report["report_id"])
    except OSError as e:
        return f"restore_marker_failed: {e}"
    if _consent_for(report) is None:
        return "restore_consent_pending:" + report["report_id"]
    try:
        _mark_restored(backup_path, report_id=report["report_id"])
    except OSError as e:
        return f"restore_marker_failed: {e}"
    for side in (LEDGER + "-wal", LEDGER + "-shm", LEDGER + "-journal"):
        try:
            os.unlink(side)
        except OSError:
            pass
    tmp = LEDGER + ".recover-tmp"
    try:
        with open(backup_path, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, LEDGER)
        dfd = os.open(DATA, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as e:
        return f"restore_failed: {e}"
    if _db_version(LEDGER) != back:
        return "restore_verify_failed"
    return None


def recover(if_stale=False):
    state = _load_state()
    if state.get("_corrupt"):
        _report("corrupt_state",
                "update_state.json unreadable — escalate, do nothing")
        return 2
    applying = state.get("applying")
    stages = [s.get("stage") for s in state.get("stages", [])]
    if not applying and not stages \
            and not os.path.exists(MARKER_PATH):
        return 0
    if if_stale and (applying or stages):
        last = max([s.get("at", 0) for s in state.get("stages", [])]
                   + [(applying or {}).get("at", 0)])
        if last and time.time() - last < STALE_S:
            return 0                        # fresh — leave it alone
    upd_fd = _try_lock(UPDATE_LOCK)
    if upd_fd is None:
        return 0                            # an apply is alive
    run_fd = _try_lock(RUN_LOCK)
    if run_fd is None:
        os.close(upd_fd)
        return 0                            # a tick is running — retry
    try:
        state = _load_state()               # fresh view under the locks
        applying = state.get("applying")
        stages = [s.get("stage") for s in state.get("stages", [])]
        if not applying and not stages:
            _remove_marker()                # orphan — writer is dead
            return 0
        removed = _clean_stale_git_locks()
        prev = (applying or {}).get("prev_sha")
        target = (applying or {}).get("sha")

        def escalate(detail):
            cid = (applying or {}).get("command_id")
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": "escalated", "detail": detail[:200],
                    "at": time.time()}
                _save_state(state)
            _report("escalate", detail)
            _restart_drainers()
            _remove_marker()
            _notify("[MCS] 更新の中断復旧ができません"
                    f"（要手動対応）: {detail}")
            return 1

        if state.get("restore_consent") and not (
                applying and applying.get("rollback")
                and applying.get("backup_path")):
            # A held schema-bump DB replace must reach _restore_db
            # again via the head==target rollback branch — any other
            # journal shape is corruption; fail closed rather than
            # 'finish' into a wedge that keeps every send denied.
            return escalate("restore_consent without a rollback "
                            "journal — refusing to classify")
        if os.path.exists(os.path.join(REPO, ".git", "MERGE_HEAD")):
            if state.get("restore_consent"):
                # rollback's tree reset completed before the consent
                # hold — a MERGE_HEAD here is drift
                return escalate("restore_consent with MERGE_HEAD — "
                                "refusing to classify")
            if not prev:
                return escalate("MERGE_HEAD without known prev_sha")
            r = _git(["merge", "--abort"])
            if (not r or r.returncode != 0) or _head() != prev:
                return escalate("merge --abort failed or HEAD "
                                f"{_head()[:12]} != prev {str(prev)[:12]}")
            _finish(state, "merge_aborted", removed)
            return 0
        if "done" in stages and not applying:
            applied = state.get("applied") or []
            cid = (applied[-1] or {}).get("command_id") \
                if applied else None
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": "applied", "at": time.time()}
            state["stages"] = []
            _save_state(state)
            _remove_marker()
            _report("resumed_done", "completed bookkeeping after crash")
            _notify("[MCS] 中断された更新の後処理を完了しました")
            if applied and applied[-1].get("plugin_changed"):
                _restart_gateway()
            return 0
        if not applying:
            # pre-'applying' remnant: nothing was ever mutated
            _finish(state, "interrupted_pre_merge", removed)
            return 0

        head = _head()
        clean = _clean()
        if clean is None:
            return escalate("git status failed")
        if head == target:
            if not clean:
                _git_out(["reset", "--hard", target])
                if _clean() is not True:
                    return escalate("target tree could not be cleaned")
            if applying.get("rollback") and applying.get("backup_path"):
                err = _restore_db(applying["backup_path"])
                if err and err.startswith("restore_consent_pending:"):
                    # held, not escalated: drainers stay stopped, both
                    # markers stay up, 'applying' stays — each watchdog
                    # pass re-checks the consent receipt until a bound
                    # approval arrives.
                    _report("restore_consent_pending", err)
                    return 0
                if err:
                    return escalate("rollback db restore: " + err)
            problems = _reconcile_membership(
                applying.get("manifest_snapshot")
                or state.get("manifest_snapshot"))
            problems += _restart_drainers()
            if problems:
                return escalate("resume incomplete: "
                                + ",".join(problems))
            if applying.get("rollback"):
                applied = state.get("applied") or []
                if applied and applied[-1].get("sha") == applying.get("prev_sha"):
                    reverted = applied[-1]
                    state["applied"] = applied[:-1]
                    state.setdefault("attempts", {})[reverted.get("tag") or "?"] = {
                        "result": "rolled_back", "at": time.time()}
                result = "rolled_back"
            else:
                state.setdefault("applied", []).append(applying)
                result = "applied"
            state["applying"] = None
            state["stages"] = []
            cid = applying.get("command_id")
            if cid:
                state.setdefault("executed", {})[cid] = {
                    "result": result, "at": time.time()}
            _save_state(state)
            _remove_marker()
            _report("resumed", "post-merge converged after crash")
            _notify("[MCS] 更新の中断を検出し、post-merge を完了しました")
            if applying.get("plugin_changed"):
                _restart_gateway()
            return 0
        if prev and head == prev:
            if state.get("restore_consent"):
                # a held restore resolves ONLY through the head==target
                # rollback branch; landing here means the journal
                # drifted — escalate, never 'finish' into a wedge
                return escalate("restore_consent hold lost its "
                                "rollback target — refusing to classify")
            if not clean:
                # crash mid-merge checkout without MERGE_HEAD — the
                # mixed-tree case stage-gating could never reach
                _git_out(["reset", "--hard", prev])
                if _head() != prev or _clean() is not True:
                    return escalate("prev tree could not be cleaned")
                _finish(state, "mixed_tree_reset", removed)
                return 0
            _finish(state, "interrupted_pre_merge", removed)
            return 0
        return escalate("unclassifiable repo state — no destructive "
                        f"action taken (HEAD={head[:12]})")
    finally:
        os.close(run_fd)
        os.close(upd_fd)


def _finish(state, result, removed):
    applying = state.get("applying") or {}
    cid = applying.get("command_id")
    if cid:
        # consume the receipt — otherwise the next check re-applies the
        # tag and loops crash→recover→apply forever
        state.setdefault("executed", {})[cid] = {
            "result": "interrupted_recovered", "at": time.time()}
    if applying.get("tag"):
        state.setdefault("attempts", {})[applying["tag"]] = {
            "result": result, "at": time.time()}
    state["applying"] = None
    state["stages"] = []
    _save_state(state)
    _remove_marker()
    problems = _restart_drainers()
    _report(result, "removed locks: " + ",".join(removed)
            + (" restart:" + ",".join(problems) if problems else ""))
    _notify(f"[MCS] 更新が中断され復旧しました: {result}")


def _remove_marker():
    try:
        os.unlink(MARKER_PATH)
    except OSError:
        pass


def main():
    if "--status" in sys.argv:
        state = _load_state()
        print(json.dumps({"state": state, "head": _head(),
                          "clean": _clean()},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    return recover(if_stale="--if-stale" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
