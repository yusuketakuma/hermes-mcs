"""Compute update ordering, upgrade impact and preflight decisions from observed facts."""
from __future__ import annotations

import re

import mcs_runtime

REINSTALL_ERRORS = ("install_sh_changed", "standalone_requirements_changed")
_MENTION_RE = re.compile(r"<@[!&]?\d+>|<#\d+>|@everyone|@here")


# The one release-tag grammar: updater detection/notice, approval and the
# CLI share it; scripts/mcs_upgrade.py keeps a self-contained copy.
RELEASE_TAG_RE = re.compile(
    r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


def release_tag(value):
    """Return whether value is a vX.Y.Z[-pre] release tag of at most 255 characters."""
    return isinstance(value, str) and len(value) <= 255 \
        and RELEASE_TAG_RE.fullmatch(value) is not None


def version_key(name: str) -> tuple | None:
    """Semver ordering key: (major, minor, patch, release-flag, pre).
    A prerelease sorts BELOW the same release; None when not semver."""
    if not release_tag(name):
        return None
    m = RELEASE_TAG_RE.fullmatch(name)
    pre = m.group(4)
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part)
                        for part in pre.split(".")) if pre else ()
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
            0 if pre else 1, identifiers)


def defuse_mentions(text: str) -> str:
    """Neutralise Discord mention syntax in release notes (F4) —
    fullwidth the @ AND the <> wrappers: <#id> carries no @."""
    return _MENTION_RE.sub(
        lambda m: m.group(0).replace("@", "＠")
                  .replace("<", "＜").replace(">", "＞"), text)


def environment_impact(names: list[str], cfg: dict) -> list[str]:
    """Explain changes from a repository fileset and the selected runtime."""
    notes = []
    if any(n in ("mcs/ops/mcs_setup.py", "mcs/ops/service_contract.py",
                 "mcs/ops/service_management.py", "mcs/ops/services/service_contract.py",
                 "mcs/ops/services/service_management.py", "mcs/core/mcs_config.py", "config.json") or
           n.endswith("/config.json") for n in names):
        notes.append("新しい必須設定の可能性があります")
    if any(n.startswith("deployment/") for n in names):
        notes.append("定期実行の設定し直し（services）が必要な変更を含みます")
    if "install.sh" in names:
        notes.append("導入手順（install.sh）が変わっています。必要なソフトが増える可能性があるため、"
                     "自動更新（auto）では適用を中止します")
    # mcs_standalone/ is loaded only by the standalone connector
    connector = ("mcs_standalone/",) if mcs_runtime.standalone(cfg) else ()
    if any(n.startswith(tuple(path + "/" for path in mcs_runtime.GATEWAY_SOURCE_DIRS)
                        + connector) for n in names):
        notes.append("通知機能が変わっています。適用後に通知接続（gateway）の再起動が必要です。独立動作では ai.mcs.standalone を再起動してください")
    # same condition as plan()'s lineworks_adapter restart
    interactive = (cfg.get("notify") or {}).get("interactive") \
        if isinstance(cfg.get("notify"), dict) else None
    if interactive == "lineworks" and any(n.startswith((
            "mcs/", "adapters/common/", "adapters/lineworks/", "lineworks_adapter/")) for n in names):
        notes.append("LINE WORKSの接続機能が変わっています。適用後に接続診断（check）と独立アダプターの再起動が必要です")
    return notes


_CHANGELOG_VER_RE = re.compile(r"^## \[?v?([0-9]+\.[0-9]+\.[0-9]+[^\]\s]*)\]?")


def parse_upgrade_notes(text: str, tag: str, cur_tag: str | None) -> list[dict]:
    """[{version, notes}] — the target's CHANGELOG '### 更新時の注意'
    sections for versions in (cur_tag, tag], oldest first."""
    lo, hi = version_key(cur_tag or "v0.0.0"), version_key(tag)
    out, ver, section, buf = [], None, None, []

    def flush():
        if ver and buf and "".join(buf).strip():
            out.append({"version": ver, "notes": "".join(buf).strip()})

    for line in text.splitlines(keepends=True):
        m = _CHANGELOG_VER_RE.match(line)
        if m or line.startswith("## "):
            if section == "notes":
                flush()
            key = version_key(m.group(1)) if m else None
            ver = m.group(1) if key and lo and hi and lo < key <= hi else None
            section, buf = None, []
        elif line.startswith("### "):
            if section == "notes":
                flush()
            section = "notes" if line.strip() == "### 更新時の注意" else None
            buf = []
        elif section == "notes":
            buf.append(line)
    if section == "notes":
        flush()
    return sorted(out, key=lambda n: version_key(n["version"]))


def preflight_decision(local_errors: list[str], tag_errors: list[str], state: dict,
                       *, restore_pending: bool, legacy_source: bool,
                       standalone: bool, automatic: bool, noop: bool) -> dict:
    """Classify already-observed gates without reading files, git or services."""
    reinstall = [error for error in tag_errors if error in REINSTALL_ERRORS]
    bump = [error for error in tag_errors if error.startswith("schema_bump:")]
    blockers = [*local_errors, *(error for error in tag_errors
                                 if error not in REINSTALL_ERRORS and error not in bump)]
    if state.get("_corrupt"):
        blockers.append("update_state_corrupt")
    elif state.get("applying") or state.get("stages"):
        blockers.append("update_in_progress_or_interrupted")
    if state.get("restore_consent") or restore_pending:
        blockers.append("restore_consent_pending")
    last = (state.get("applied") or [{}])[-1]
    if last.get("reinstall") and not last.get("reinstall_done"):
        blockers.append("reinstall_incomplete")
    if legacy_source:
        blockers.append("legacy_source_manual")
    if standalone:
        blockers.append("standalone_external_apply")
    if bump and automatic:
        blockers.append("schema_bump_auto_blocked")
    return {"route": ("noop" if noop else "blocked" if blockers
                      else "reinstall" if reinstall else "apply"),
            "blockers": sorted(set(blockers)), "reinstall": reinstall,
            "schema_bump": bump[0] if bump else None}


# Old update journals contain English diagnostics. Match only these translated
# templates, retaining every dynamic value so new failures still block updates.
ENV_DIAGNOSTIC_TRANSLATIONS = (
    (
        'runtime_mode is invalid — select hermes or standalone before checking runtime '
        'services',
        '実行方法が不正です。hermes（Hermes経由）かstandalone（独立動作）を設定してから再確認してください',
    ),
    (
        "Keychain entry '{}' not found — re-login cannot inject the MCS password",
        "キーチェーンに'{}'が見つかりません — MCSパスワードを取得できず、再ログインできません。初期設定でパスワードを登録してください",
    ),
    (
        "Keychain entry '{}' is present but unreadable — the keychain may be locked or access "
        'may require an interactive permission prompt. Check the login keychain lock and this '
        "process's access permission in an interactive session, then re-run check; or use the "
        'existing init password input to configure the .env fallback.',
        "キーチェーンに'{}'はありますが、読み取れません。キーチェーンのロックや、画面でのアクセス許可が必要な可能性があります。ログインキーチェーンのロックと、"
        'このプログラムのアクセス権限を画面で確認し、checkを再実行してください。または初期設定のパスワード入力で.envにも予備の認証情報を保存してください。',
    ),
    (
        "Keychain entry '{}' exists but its password cannot be read (rc={}) — re-register via "
        '`mcs_setup init`',
        "キーチェーンに'{}'はありますが、パスワードを読み取れません（終了コード={}）。`mcs_setup.py init`で再登録してください",
    ),
    (
        'Chrome binary missing: {}',
        'Chromeが見つかりません。インストール先を確認してください: {}',
    ),
    (
        'standalone check failed — see docs/guides/STANDALONE.md',
        '独立動作の診断に失敗しました。次の手順を確認してください: docs/guides/STANDALONE.md',
    ),
    (
        'LINE WORKS adapter local check failed — run `python -m lineworks_adapter check` and '
        'follow docs/guides/LINEWORKS.md to repair the existing credentials',
        'LINE WORKS接続の診断に失敗しました。`python -m lineworks_adapter check`を実行し、'
        'docs/guides/LINEWORKS.mdに従って認証情報を確認してください',
    ),
    (
        'hermes CLI not resolvable ({}) — notifications cannot be sent',
        'Hermesコマンドが見つかりません（{}）。通知を送信できません。install.shでHermesの導入状態を確認してください',
    ),
    (
        'semantic.mode is enabled but TYPESAFE_API_KEY is not resolvable — Jev verdicts fail '
        'as no_api_key',
        '意味解析は有効ですが、TYPESAFE_API_KEYを取得できません。外部解析の認証情報を設定してください。エラーコード: no_api_key',
    ),
    (
        'LaunchAgent {} installed but not loaded — run `mcs_setup.py services` or `launchctl '
        'bootstrap gui/{} {}`',
        '常駐処理{}は導入済みですが読み込まれていません。`mcs_setup.py services`を実行してください。個別の起動方法: `launchctl '
        'bootstrap gui/{} {}`',
    ),
    (
        'admission broker route table lacks {} — those callers are rejected permanently while '
        'admission (local_llm.admission or MCS_LLM_ADMISSION) is enabled',
        'AIの実行枠の登録に不足があります: {} — 受付制御（local_llm.admission または MCS_LLM_ADMISSION）が有効な間、これらの処理は受け付けられません。実行枠の登録を確認してください',
    ),
    (
        'admission broker unusable: {}',
        'AIの実行枠を管理できません。設定と稼働状態を確認してください（詳細: {}）',
    ),
    (
        'llama-server advertises {} slots but the selected count is {} — fix -np or lower '
        'SLOT_COUNT',
        'AIサーバーの同時処理枠は{}ですが、設定では{}です。サーバーの-npを修正するか、SLOT_COUNTを減らしてください',
    ),
)


def environment_error_identity(message: str):
    for original, translated in ENV_DIAGNOSTIC_TRANSLATIONS:
        for template in (original, translated):
            pattern = "(.*?)".join(re.escape(part) for part in template.split("{}"))
            match = re.fullmatch(pattern, message, flags=re.DOTALL)
            if match:
                return original, match.groups()
    return message, ()


def classify_recovery_journal_position(*, has_applying: bool, has_done: bool) -> str:
    """Select bookkeeping or HEAD observation after the caller's live merge guard."""
    if not has_applying:
        return "complete_done_bookkeeping" if has_done else "finish_pre_merge"
    return "observe_head"


def classify_recovery_head_position(*, head, previous_sha, target_sha,
                                    restore_incomplete: bool) -> str:
    """Classify observed HEAD before the caller measures tree cleanliness."""
    if restore_incomplete and head != target_sha:
        return "restore_incomplete_wrong_head"
    if head == target_sha:
        return "target"
    if previous_sha and head == previous_sha:
        return "previous"
    return "unknown"


def classify_recovery_target_continuation(*, rollback: bool, runtime_migration: bool,
                                         reinstall: bool, reinstall_done: bool) -> str:
    """Select continuation only after target convergence and any database restore."""
    if rollback:
        return "rollback_runtime" if runtime_migration else "rollback"
    if reinstall and not reinstall_done:
        return "undo_interrupted_migration" if runtime_migration else "manual_reinstall_required"
    return "forward_runtime" if runtime_migration else "forward"


def classify_recovery_previous_continuation(*, held: bool, rollback: bool,
                                           runtime_migration: bool, clean: bool) -> str:
    """Select previous-HEAD continuation from the caller's freshly reread hold."""
    if held:
        return "held_lost_rollback_target"
    if rollback and runtime_migration:
        return "undo_interrupted_migration"
    return "finish_pre_merge" if clean else "reset_previous_tree"
