"""Validate shared MCS configuration without importing a CLI or probing services."""
from __future__ import annotations

import os
from pathlib import Path
import re

from backup_config import validate_backup_config as _backup_config
import bounded_http
from mcs_util import REPO

# ---- typesafe config rules ------------------------------------------
# name -> (required, check) — check(v) -> error string | None
def _nonempty_str(v):
    return None if isinstance(v, str) and v.strip() else "空でない文字列を指定してください"


def _bool(v):
    return None if type(v) is bool else "true（有効）かfalse（無効）を指定してください"


def _int_range(lo, hi):
    def f(v):
        if type(v) is not int or not (lo <= v <= hi):
            return f"{lo}から{hi}までの整数を指定してください"
        return None
    return f


def positive_number_problem(v):
    return (None if type(v) in (int, float) and 0 < v <= 1e9
            else "0より大きく10億以下の数値を指定してください")


def _bot_profile(v):
    ok = isinstance(v, str) and re.fullmatch(r"[a-z0-9_-]+", v)
    return None if ok else "半角英小文字・数字・ハイフン・アンダースコアだけで指定してください"


def _dict(v):
    return None if isinstance(v, dict) else "JSONオブジェクト（キーと値の組）を指定してください"


def _drug_map_config(value):
    import drug_map
    problem = drug_map.config_error(value)
    return f"薬剤辞書の設定を確認してください（詳細: {problem}）" if problem else None


# Optional setup guidance: the code default applies until a release flips it,
# while an explicit true/false in optional_features always wins over that flip.
# The value only decides whether the guidance is shown; it never registers,
# consents, sends or starts anything.
OPTIONAL_FEATURE_DEFAULTS = {"csdc": False}


def _optional_features(value):
    if not isinstance(value, dict):
        return "JSONオブジェクト（キーと値の組）を指定してください"
    bad = sorted(k for k, v in value.items() if k in OPTIONAL_FEATURE_DEFAULTS and type(v) is not bool)
    return f"{bad[0]}: true（案内を表示）かfalse（案内を省略）を指定してください" if bad else None


def feature_enabled(cfg, name: str) -> bool:
    """The explicit optional_features choice, else this release's default."""
    if name not in OPTIONAL_FEATURE_DEFAULTS:
        return False                                # a feature this release does not implement
    value = cfg.get("optional_features") if isinstance(cfg, dict) else None
    choice = value.get(name) if isinstance(value, dict) else None
    return choice if type(choice) is bool else OPTIONAL_FEATURE_DEFAULTS[name]


def _stamps_config(value):
    from stamp_contract import config_error
    problem = config_error(value)
    return f"MCSスタンプの設定を確認してください（{problem}）" if problem else None


def recovery_python_problem(path, update_repo=None, *, checkout=REPO):
    """An independent runtime cannot live in either mutable checkout."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return "Python実行ファイルの絶対パスを指定してください"
    try:
        for root in (checkout, update_repo):
            if root is not None and Path(path).resolve().is_relative_to(Path(root).resolve()):
                return "復旧用Pythonは更新対象フォルダーの外に置いてください"
    except (OSError, RuntimeError):
        return "復旧用Pythonの保存場所を確認できません"
    return None


def _urgency_escalation(v):
    """Reuse notify_urgent.settings(): on/shadow with a policy it would
    silently drop (no explicit room_cooldown_min, bad type) is an error."""
    if not isinstance(v, dict):
        return "JSONオブジェクト（キーと値の組）を指定してください"
    mode = v.get("mode", "off")
    if mode not in ("off", "on", "shadow"):
        return 'mode: "off"（無効）、"on"（有効）、"shadow"（比較のみ）から選んでください'
    if mode == "off":
        return None
    import notify_urgent
    if notify_urgent.settings({"urgency_escalation": v}) is None:
        return ("通知条件が不足しています。sourceは\"llm\"とし、room_cooldown_minを"
                "指定してください。after_min/repeat_min/room_cooldown_minは"
                "正の数、max_repeats/max_per_dayは0以上の整数で指定してください")
    return None


CONFIG_RULES = {
    "runtime_mode":          (False, lambda v: None if v in ("hermes", "standalone")
                             else '"hermes"（Hermes経由）か"standalone"（独立動作）を選んでください'),
    "recovery_python":       (False, recovery_python_problem),
    "mcs_login_id":          (True,  _nonempty_str),
    "notify_target":         (True,  _nonempty_str),
    "notify_bot_profile":    (False, _bot_profile),
    "notify_system_target":  (False, _nonempty_str),
    "hermes_bin":            (False, _nonempty_str),
    "discover_archived":     (False, _bool),
    "deep_history":          (False, _bool),
    "trickle_pages":         (False, _int_range(1, 40)),
    "notify_max_age_h":      (False, positive_number_problem),
    "notify_all_replies":    (False, _bool),
    "job_budget_seconds":    (False, positive_number_problem),
    "self_posts":            (False, _bool),
    "metadata_shadow":       (False, _bool),
    "metadata_refresh_publish": (False, _bool),
    "metadata_actors":       (False, _bool),
    "stamps":                (False, _stamps_config),
    "optional_features":     (False, _optional_features),
    "clinical_metadata":     (False, _bool),
    "notify":                (False, _dict),
    "signals":               (False, _dict),
    "semantic":              (False, _dict),
    "extract":               (False, _dict),
    "update":                (False, _dict),
    "local_llm":             (False, _dict),
    "health":                (False, _dict),
    "daily_digest":          (False, _dict),
    "drug_map":              (False, _drug_map_config),
    "backup":                (False, _backup_config),
    "urgency_escalation":    (False, _urgency_escalation),
    "watchdog_grace_s":      (False, lambda v: None if type(v) is int and 0 <= v <= 3600
                             else "0から3600までの整数を指定してください"),
}


def validate_standalone(cfg: dict) -> list[str]:
    """runtime_mode=standalone: the grants Hermes plugin settings carry
    (allowed users, projects, Discord chats/roles) live in notify.<t>,
    and Slack/Discord targets must name a concrete channel."""
    if cfg.get("runtime_mode") != "standalone":
        return []
    errors = []
    for key in ("notify_target", "notify_system_target"):
        target = cfg.get(key)
        if not isinstance(target, str):
            continue
        if target == "local":  # Existing placeholder for an installation without notifications.
            continue
        transport, _, channel = target.strip().partition(":")
        if transport not in ("discord", "slack", "lineworks"):
            errors.append(f"{key}: 独立動作の通知先はdiscord:、slack:、"
                          "lineworks:のいずれかで指定してください")
        elif transport == "discord" and not channel.isdecimal():
            errors.append(f"{key}: 独立動作ではdiscord:<チャンネルID>を指定してください")
        elif transport == "slack" and not re.fullmatch(r"[CGD][A-Z0-9]{8,}", channel):
            errors.append(f"{key}: 独立動作ではslack:<C/G/Dで始まる会話ID>を指定してください "
                          "（スレッド・ユーザー宛ての指定はHermes経由のみ対応）")
    return errors


def _validate_llm(ll: dict) -> list[str]:
    """local_llm subkeys — url must stay an unauthenticated loopback
    http endpoint (mirrors bounded_http.loopback_endpoint_allowed):
    message bodies are PHI and must not leave the machine."""
    errors = []
    for key in ("url", "model"):
        if key in ll and (not isinstance(ll[key], str)
                          or not ll[key].strip()):
            errors.append(f"local_llm.{key}: 空でない文字列を指定してください")
    url = ll.get("url")
    if isinstance(url, str) and url.strip():
        try:
            allowed = bounded_http.loopback_endpoint_allowed(url)
        except Exception:
            allowed = False
        if not allowed:
            errors.append(
                "local_llm.url: この端末内のhttp://接続先を指定してください "
                "（127.0.0.1/localhost/::1）。認証情報や#以降の指定は使えません。"
                "投稿本文を端末の外へ送信しないための制限です")
    return errors


# = notify_cmds.SCOPE_MAX: the runner rejects, and the plugin truncates to,
# a static project scope longer than this
PROJECT_SCOPE_MAX = 1000


def validate_notify(ntf: dict) -> list[str]:
    """Subkey checks for the notify block — mirrors notify_cards'
    contracts: interactive is a closed enum, discord delivery scope is
    all-or-nothing, and scope is required when cards are on."""
    errors = []
    if "interactive" in ntf \
            and ntf["interactive"] not in ("discord", "slack", "lineworks", "off"):
        errors.append('notify.interactive: "discord"、'
                      '"slack"、"lineworks"、"off"（無効）から選んでください')
    if "card_thread" in ntf and type(ntf["card_thread"]) is not bool:
        errors.append("notify.card_thread: true（有効）かfalse（無効）を指定してください")
    if "reply_cards" in ntf and type(ntf["reply_cards"]) is not bool:
        errors.append("notify.reply_cards: true（有効）かfalse（無効）を指定してください")
    for key in ("route_epoch", "card_thread_archive_min"):
        if key in ntf:
            err = _int_range(1, 10 ** 9)(ntf[key])
            if err:
                errors.append(f"notify.{key}: {err}")
    if "operator" in ntf:
        err = _nonempty_str(ntf["operator"])
        if err:
            errors.append(f"notify.operator: {err}")
    for transport, tenant in (("discord", "guild_id"),
                              ("slack", "team_id"),
                              ("lineworks", "team_id")):
        d = ntf.get(transport)
        if d is None:
            if ntf.get("interactive") == transport:
                errors.append(f'notify.{transport}: 通知カードの接続設定が必要です。'
                              f'現在の接続先は"{transport}"です')
        elif not isinstance(d, dict):
            errors.append(f"notify.{transport}: JSONオブジェクト（キーと値の組）を指定してください")
        else:
            errors.extend(
                f"notify.{transport}.{k}: 空でない文字列を指定してください"
                for k in ("profile", "application_id", tenant,
                          "channel_id")
                if not isinstance(d.get(k), str) or not d[k].strip())
            # delivery_scope() rejects a slack block carrying
            # guild_id outright — flag it at config time too
            if transport in ("slack", "lineworks") and "guild_id" in d:
                errors.append(f"notify.{transport}.guild_id: この項目は使えません "
                              f"（{transport}ではteam_idを指定してください）")
            if transport == "lineworks":
                for key in ("application_id", "team_id"):
                    v = d.get(key)
                    if not (isinstance(v, str) and re.fullmatch(r"[1-9][0-9]{0,18}", v)
                            and 0 < int(v) < 2**63):
                        errors.append(f"notify.lineworks.{key}: 0より大きい整数を文字列で指定してください")
                for key in ("channel_id", "profile"):
                    v = d.get(key)
                    pattern = r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}" if key == "channel_id" else r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
                    if not isinstance(v, str) or not re.fullmatch(pattern, v):
                        errors.append(f"notify.lineworks.{key}: IDの形式を確認してください")
                users = d.get("allowed_user_ids")
                if not (isinstance(users, list) and users
                        and all(isinstance(v, str) and re.fullmatch(
                            r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}", v) for v in users)):
                    errors.append("notify.lineworks.allowed_user_ids: IDを1件以上のリストで指定してください")
                names = d.get("user_names", {})
                if not (isinstance(names, dict) and all(
                        isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9._@-]{1,128}", k)
                        and isinstance(v, str) and 0 < len(v.strip()) <= 40
                        for k, v in names.items())):
                    errors.append("notify.lineworks.user_names: {userId: 表示名(40字以内)}の形式で指定してください")
                automatic = d.get("project_ids_auto", False)
                if type(automatic) is not bool:
                    errors.append("notify.lineworks.project_ids_auto: true（有効）かfalse（無効）を指定してください")
                projects = d.get("project_ids", [])
                if not (isinstance(projects, list)
                        and all(type(v) is int and 0 < v < 2**63 for v in projects)
                        and (projects or automatic is True)):
                    errors.append("notify.lineworks.project_ids: 0より大きい整数のリスト、またはproject_ids_auto=trueを指定してください")
    return errors


def _validate_update(upd: dict) -> list[str]:
    """Subkey checks for the update block — the self-updater's policy.
    mode is a killswitch/opt-in gate; auto_delay_h==0 means 'apply as
    soon as detected' (explicit opt-in, default is delayed)."""
    errors = []
    if "mode" in upd and upd["mode"] not in ("off", "notify", "auto"):
        errors.append('update.mode: "off"（無効）、"notify"（通知のみ）、"auto"（自動更新）から選んでください')
    if "auto_delay_h" in upd:
        v = upd["auto_delay_h"]
        if type(v) not in (int, float) or not 0 <= v <= 1e9:
            errors.append("update.auto_delay_h: 0以上10億以下の数値を指定してください")
    if "include_prerelease" in upd \
            and type(upd["include_prerelease"]) is not bool:
        errors.append("update.include_prerelease: true（有効）かfalse（無効）を指定してください")
    return errors


def _validate_health(h: dict) -> list[str]:
    """Subkey checks for the health block consumed by health_watch:
    tick_interval_s is the producer's check cadence, max_missed_runs
    the missed-tick grace window before staleness is declared (the
    schedule settings require an int in [0,100])."""
    errors = []
    if "tick_interval_s" in h:
        v = h["tick_interval_s"]
        if type(v) not in (int, float) or not 0 < v <= 86400:
            errors.append("health.tick_interval_s: "
                          "0より大きく86400以下の数値を指定してください")
    if "max_missed_runs" in h:
        err = _int_range(0, 100)(h["max_missed_runs"])
        if err:
            errors.append(f"health.max_missed_runs: {err}")
    return errors


def validate_config(cfg: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for config.json — typesafe required
    conditions. Unknown keys warn (forward-compat) but never fail."""
    errors, warnings = [], []
    if not isinstance(cfg, dict):
        return ["config.jsonはJSONオブジェクト（キーと値の組）で保存してください"], []
    warnings.extend(f"未対応の設定項目: {key}（この版では使いません）"
                    for key in sorted(set(cfg) - set(CONFIG_RULES)))
    for key, (required, check) in CONFIG_RULES.items():
        if key not in cfg:
            if required:
                errors.append(f"必須の設定項目がありません: {key}")
            continue
        err = check(cfg[key])
        if err:
            errors.append(f"{key}: {err}")
    features = cfg.get("optional_features")
    if isinstance(features, dict):
        # a later release's feature name survives a rollback as a warning only
        warnings.extend(f"未対応の任意機能: optional_features.{key}（この版では使いません）"
                        for key in sorted(set(features) - set(OPTIONAL_FEATURE_DEFAULTS)))
    sig = cfg.get("signals")
    ext = cfg.get("extract")
    if isinstance(ext, dict) and "patient_state_context" in ext and type(ext["patient_state_context"]) is not bool:
        errors.append("extract.patient_state_context: true（有効）かfalse（無効）を指定してください")
    if isinstance(ext, dict) and "patient_context_nightly" in ext:
        nightly = ext["patient_context_nightly"]
        prefix = "extract.patient_context_nightly"
        if not isinstance(nightly, dict):
            errors.append(f"{prefix}: JSONオブジェクト（キーと値の組）を指定してください")
        else:
            if set(nightly) - {"enabled", "publication"}:
                errors.append(f"{prefix}: enabledとpublication以外の設定項目は使えません")
            for key in ("enabled", "publication"):
                if key in nightly and type(nightly[key]) is not bool:
                    errors.append(f"{prefix}.{key}: true（有効）かfalse（無効）を指定してください")
    if isinstance(sig, dict) and "notify" in sig and type(sig["notify"]) is not bool:
        errors.append("signals.notify: true（有効）かfalse（無効）を指定してください")
    ntf = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    if isinstance(sig, dict) and sig.get("notify") is True \
            and ntf.get("interactive", "off") == "off":
        # alerts are delivered as cards only: with cards off every alert
        # is held and nothing says so
        errors.append('signals.notify: アラートは通知カードで届くため、notify.interactiveを'
                      '"slack"、"discord"、"lineworks"のいずれかに設定してください')
    esc = cfg.get("urgency_escalation")
    if isinstance(esc, dict) and esc.get("mode") == "on" and (
            ntf.get("card_thread") is not True
            or ntf.get("interactive") not in ("slack", "discord")):
        # the re-check notice posts only in the source thread, which
        # needs card threads on Slack/Discord; anywhere else it waits
        # out its hold and is dropped
        errors.append('urgency_escalation.mode "on": 再確認通知にはnotify.card_threadを'
                      'true、notify.interactiveを"slack"か"discord"に設定してください')
    if isinstance(sig, dict):
        for key in ("self_organizations", "self_professions",
                    "request_targets", "med_exclude_names"):
            if key in sig:
                v = sig[key]
                if not (isinstance(v, list)
                        and all(isinstance(x, str) and x.strip()
                                for x in v)):
                    errors.append(f"signals.{key}: 空でない文字列を並べたリストを指定してください")
        for key in ("digest", "self_reaction_response"):
            if key in sig and type(sig[key]) is not bool:
                errors.append(f"signals.{key}: true（有効）かfalse（無効）を指定してください")
        if "digest_interval_h" in sig:
            err = positive_number_problem(sig["digest_interval_h"])
            if err:
                errors.append(f"signals.digest_interval_h: {err}")
        if "tiers" in sig and not isinstance(sig["tiers"], dict):
            errors.append("signals.tiers: JSONオブジェクト（キーと値の組）を指定してください")
    dd = cfg.get("daily_digest")
    if isinstance(dd, dict):
        if "enabled" in dd and type(dd["enabled"]) is not bool:
            errors.append("daily_digest.enabled: true（有効）かfalse（無効）を指定してください")
        if "hour_jst" in dd:
            err = _int_range(0, 23)(dd["hour_jst"])
            if err:
                errors.append(f"daily_digest.hour_jst: {err}")
        if "include_names" in dd and type(dd["include_names"]) is not bool:
            errors.append("daily_digest.include_names: true（有効）かfalse（無効）を指定してください")
        if "scope" in dd:
            import notify_digest
            flt = (notify_digest.parse_scope(dd["scope"])
                   if isinstance(dd["scope"], str) and len(dd["scope"]) <= 200
                   else "文字列で指定してください")
            if isinstance(flt, str) or flt["mine"]:
                errors.append("daily_digest.scope: 通知する範囲の指定を確認してください。" + (
                    flt if isinstance(flt, str) else
                    "mineは操作したユーザーの指定が必要です。station:/project:/days:を使ってください"))
    if isinstance(cfg.get("notify"), dict):
        errors.extend(validate_notify(cfg["notify"]))
        # a scope block for the transport that is NOT active is stale —
        # keep it as a warning so a discord->slack switch leaves a
        # visible note instead of a silently ignored config
        act = cfg["notify"].get("interactive")
        # standalone sends to notify_target/notify_system_target through
        # their own scope block (validate_standalone_scope requires it)
        used = {t.split(":", 1)[0] for t in (
            cfg.get("notify_target"), cfg.get("notify_system_target"))
            if isinstance(t, str)} if cfg.get("runtime_mode") == "standalone" else set()
        for other in ("discord", "slack", "lineworks"):
            if other != act and other not in used and isinstance(cfg["notify"].get(other),
                                           dict):
                warnings.append(
                    f"notify.{other}: 接続設定がありますが、現在の通知先は"
                    f"{act!r}です。切り替えるまでこの設定は使いません")
        for transport in ("discord", "slack", "lineworks"):
            scope = cfg["notify"].get(transport)
            projects = scope.get("project_ids") if isinstance(scope, dict) else None
            if isinstance(projects, list) and len(projects) > PROJECT_SCOPE_MAX \
                    and scope.get("project_ids_auto") is not True:
                warnings.append(
                    f"notify.{transport}.project_ids: {len(projects)}件は"
                    f"カード・要約の上限{PROJECT_SCOPE_MAX}件を超えています。"
                    "IDの小さい順に集計します。project_ids_auto=trueの利用を検討してください")
    if isinstance(cfg.get("local_llm"), dict):
        errors.extend(_validate_llm(cfg["local_llm"]))
    if isinstance(cfg.get("semantic"), dict):
        try:
            from semantic_policy import semantic_config
            errors.extend(f"semantic: 意味解析の設定を確認してください（詳細: {error}）"
                          for error in semantic_config(cfg)[1])
        except Exception as e:  # validator itself must not crash check
            errors.append(f"semantic: 設定の検証に失敗しました（{type(e).__name__}）")
    if isinstance(cfg.get("update"), dict):
        errors.extend(_validate_update(cfg["update"]))
    if isinstance(cfg.get("health"), dict):
        errors.extend(_validate_health(cfg["health"]))
    if cfg.get("runtime_mode") == "standalone":
        errors.extend(validate_standalone(cfg))
        errors.extend(validate_standalone_scope(cfg))
    return errors, warnings


def validate_standalone_scope(cfg: dict) -> list[str]:
    """Require explicit actor and project scopes for independent connectors."""
    errors = []
    ntf = cfg.get("notify") if isinstance(cfg.get("notify"), dict) else {}
    targets = [cfg.get("notify_target"), cfg.get("notify_system_target")]
    active = {ntf.get("interactive")} if isinstance(ntf.get("interactive"), str) else set()
    active |= {
        value.split(":", 1)[0] for value in targets if isinstance(value, str)}
    for transport in ("slack", "discord"):
        if transport not in active:
            continue
        scope = ntf.get(transport)
        if not isinstance(scope, dict):
            errors.append(f"notify.{transport}: 独立動作では操作を許可する範囲を明示してください")
            continue
        users = scope.get("allowed_user_ids")
        if not (isinstance(users, list) and users and all(
                isinstance(uid, str) and uid.strip() for uid in users)):
            errors.append(f"notify.{transport}.allowed_user_ids: IDを1件以上のリストで指定してください")
        automatic = scope.get("project_ids_auto", False)
        if type(automatic) is not bool:
            errors.append(f"notify.{transport}.project_ids_auto: true（有効）かfalse（無効）を指定してください")
        projects = scope.get("project_ids", [])
        if not (isinstance(projects, list) and all(
                type(pid) is int and 0 < pid < 2**63 for pid in projects)
                and (projects or automatic is True)):
            errors.append(f"notify.{transport}.project_ids: 0より大きい整数のリスト、またはproject_ids_auto=trueを指定してください")
        if transport == "discord" and "allowed_chat_ids" in scope:
            chats = scope["allowed_chat_ids"]
            try:
                valid_chats = isinstance(chats, list) and chats and all(
                    isinstance(chat, str) and chat.isdecimal() and int(chat) > 0 for chat in chats)
            except ValueError:
                valid_chats = False
            if not valid_chats:
                errors.append("notify.discord.allowed_chat_ids: チャンネルIDを1件以上のリストで指定してください")
        if transport == "discord" and "allowed_role_ids" in scope:
            roles = scope["allowed_role_ids"]
            if not (isinstance(roles, list) and all(isinstance(role, str)
                    and role.strip() and role != scope.get("guild_id") for role in roles)):
                errors.append("notify.discord.allowed_role_ids: IDをリストで指定してください。@everyoneは許可できません")
    return errors


def transport_enabled(cfg, transport):
    return (cfg.get("notify") or {}).get("interactive") == transport or (
        cfg.get("runtime_mode") == "standalone" and any(
            isinstance(cfg.get(key), str) and cfg[key].startswith(transport + ":")
            for key in ("notify_target", "notify_system_target")))
