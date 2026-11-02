#!/bin/sh
# MCS の導入に必要なソフトを、次の6段階で準備します。
#   1. 基本ソフト（Homebrew・Python・Google Chrome など）
#   2. 実行環境（Hermes 経由、または MCS 単独）
#   3. Slack／Discord の操作カード用プラグイン
#   4. この Mac で動かす AI（llama-server と約5.7 GBのモデル）
#   5. 定期収集などの常駐サービス
#   6. 更新に失敗したときの復旧ツールと独立した監視
#
# 使い方: ./install.sh [オプション] [HERMES_HOME]
# HERMES_HOME: Hermes の保存先（省略時 ~/.hermes）
#
# 最初は --preflight で準備状況を確認してください。
#   --preflight, --check-only  変更せずに確認。問題と対処を表示します。
#                             導入できない問題があると終了コード1になります。
#   --dry-run                 変更せず、事前確認と導入予定を表示します。
#   --mode hermes             Hermes を通じて接続します（既定）。
#   --mode standalone         Hermes を使わず MCS 単独で接続します。
#                             専用の Python 環境と公式接続 SDK を導入します。
#                             省略時は既存の設定を優先します。
#   --recovery-python PATH    復旧に使う独立した Python の絶対パス。
#                             更新対象フォルダの外にある既存のものを指定します。
#   --force-repo              既存の導入先をこのフォルダへ切り替えます。
#                             プラグイン・復旧ツール・サービスの参照先が変わります。
#   --no-brew                 基本ソフトの導入を省略（導入済みの場合）。
#   --no-llm                  ローカル AI の導入を省略。別途 OpenAI 互換
#                             サーバーを 127.0.0.1:8080 で動かしてください。
#   --no-plugin               操作カード用プラグインの導入を省略。
#                             Slack／Discord の操作カードには必要です。
#   --no-services             定期収集などの常駐サービスの登録を省略。
#   --no-recovery             復旧ツールと更新監視の導入を省略。
#   -h, --help                この説明を表示します。
#
# 通常のユーザーで実行してください（sudo は使いません）。
# 導入済みのものは確認して維持します。途中で止まった場合は表示された
# 案内に従ってください。保留記録がある場合は設定と記録を保持し、担当者に確認してください。
# 失敗した段階で停止し、その後の段階は実行しません。取得途中のファイルは再開に利用します。
# 最後に mcs setup を案内します。ログイン情報や通知先は、そこで
# 説明を読みながら入力します。パスワード・トークンは画面に表示しません。
set -eu
# files this script creates are the owner's only; existing modes are kept
umask 077

usage() {
    # Keep the complete Japanese prerequisites and option explanations above.
    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
    printf '\n通常はオプションなしで実行し、画面の番号を選んでください。\n'
    printf 'ソフトの準備後は、初期設定へ続けて進むか後で設定するか選べます。\n'
}

# EOF is cancellation, never acceptance of a recommended action.
choose() {
    while :; do
        printf '%s' "$1"
        _answer=""
        if ! read -r _answer; then
            printf '\n入力を終了したため、ここで中止しました。\n'
            return 130
        fi
        case "$_answer" in
            ""|1) _answer=1; return ;;
            2) return ;;
            0) printf '中止しました。\n'; return 130 ;;
            *) printf '1 または 2 を入力してください。中止は 0 です。\n' ;;
        esac
    done
}

MODE=install
RUNTIME_MODE=hermes
RUNTIME_EXPLICIT=0
RECOVERY_OPTION=""
RECOVERY_PYTHON=""
SKIP_BREW=0; SKIP_LLM=0; SKIP_PLUGIN=0; SKIP_SERVICES=0; SKIP_RECOVERY=0
FORCE_REPO=0
ARG_HOME=""
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)       [ $# -ge 2 ] || { printf '%s\n' '--mode の後に hermes または standalone を指定してください' >&2; exit 2; }
                      RUNTIME_MODE="$2"; RUNTIME_EXPLICIT=1; shift
                      case "$RUNTIME_MODE" in hermes|standalone) ;; *) printf '%s\n' '--mode は hermes または standalone を指定してください' >&2; exit 2 ;; esac ;;
        --no-brew)     SKIP_BREW=1 ;;
        --recovery-python) if [ $# -lt 2 ] || [ -z "$2" ]; then printf '%s\n' '--recovery-python の後に Python の絶対パスを指定してください' >&2; exit 2; fi
                      RECOVERY_OPTION="$2"; shift ;;
        --no-llm)      SKIP_LLM=1 ;;
        --no-plugin)   SKIP_PLUGIN=1 ;;
        --no-services) SKIP_SERVICES=1 ;;
        --no-recovery) SKIP_RECOVERY=1 ;;
        --force-repo)  FORCE_REPO=1 ;;
        --preflight|--check-only) MODE=preflight ;;
        --dry-run)     MODE=dry-run ;;
        -h|--help)     usage; exit 0 ;;
        -*) printf '未対応のオプションです: %s\n' "$1" >&2; usage >&2; exit 2 ;;
        *)  if [ -z "$ARG_HOME" ]; then ARG_HOME="$1"
            else printf '余分な引数があります: %s\n' "$1" >&2; exit 2; fi ;;
    esac
    shift
done

say()  { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  完了: %s\n' "$*"; }
skip() { printf '  省略: %s\n' "$*"; }
warn() { printf '  注意: %s\n' "$*" >&2; }
die()  { printf 'エラー: %s\n' "$*" >&2; exit 1; }
SUMMARY=""
sum()  { SUMMARY="$SUMMARY  $*
"; }

# lexical absolute path without trailing slash; an existing directory is
# cleaned of ./.. via cd (logical — ~/.hermes may itself be a symlink)
abspath() {
    case "$1" in /*) _p="$1" ;; *) _p="$PWD/$1" ;; esac
    if [ -d "$_p" ]; then _p="$(CDPATH='' cd -- "$_p" && pwd)"; fi
    while [ "${#_p}" -gt 1 ] && [ "${_p%/}" != "$_p" ]; do _p="${_p%/}"; done
    printf '%s\n' "$_p"
}
# physical path of an existing file or directory (symlinks resolved)
physpath() {
    if [ -d "$1" ]; then (CDPATH='' cd -- "$1" && pwd -P)
    else
        _d="$(CDPATH='' cd -- "$(dirname -- "$1")" && pwd -P)" || return 1
        printf '%s/%s\n' "$_d" "$(basename -- "$1")"
    fi
}
# physical path a symlink points at (one level, relative targets allowed)
link_target() {
    _t="$(readlink "$1")" || return 1
    case "$_t" in /*) ;; *) _t="$(dirname -- "$1")/$_t" ;; esac
    [ -e "$_t" ] || return 1
    physpath "$_t"
}

REPO="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)"
if [ "${MCS_ROOT+x}" = x ]; then
    [ -n "$MCS_ROOT" ] || die "MCS_ROOT が空です。unset MCS_ROOT を実行してから導入してください（データの保存先は ~/.mcs）"
    case "$MCS_ROOT" in
        \~/*) _mcs_root="$HOME/${MCS_ROOT#\~/}" ;;
        *) _mcs_root="$MCS_ROOT" ;;
    esac
    _mcs_root="$(abspath "$_mcs_root")"
    # Match the runtime's lexical normalization before resolving symlinks.
    _mcs_root="$(awk 'BEGIN {
        n = split(ARGV[1], parts, "/"); top = 0
        for (i = 1; i <= n; i++) {
            if (parts[i] == "" || parts[i] == ".") continue
            if (parts[i] == "..") { if (top > 0) top--; continue }
            stack[++top] = parts[i]
        }
        out = ""; for (i = 1; i <= top; i++) out = out "/" stack[i]
        print (out == "" ? "/" : out)
    }' "$_mcs_root")"
    _mcs_default="$(physpath "$HOME/.mcs")"
    _mcs_root="$(physpath "$_mcs_root" 2>/dev/null)" \
        || die "この MCS_ROOT は導入に使えません。unset MCS_ROOT を実行してください（データの保存先は ~/.mcs）"
    [ "$_mcs_root" = "$_mcs_default" ] \
        || die "この MCS_ROOT は導入に使えません。unset MCS_ROOT を実行してください（データの保存先は ~/.mcs）"
fi
MCS_ROOT="$(abspath "$HOME")/.mcs"
export MCS_ROOT

# Keep this lock through software preparation and the optional setup child.
# macOS lockf and Linux flock operate on the inherited descriptor, so no
# Python installation is needed on a fresh machine.
if [ "$MODE" = install ]; then
    [ "$(id -u)" -ne 0 ] || die "install.sh は root や sudo で実行しないでください。通常のユーザーで再実行してください"
    _lock_uid="$(/usr/bin/id -u)" || die "実行ユーザーを確認できません"
    [ "$_lock_uid" -ne 0 ] || die "install.sh は root や sudo で実行しないでください。通常のユーザーで再実行してください"
    _lock_data="$MCS_ROOT/data"
    _lock_file="$_lock_data/update.lock"
    if [ -n "${MCS_UPDATE_REINSTALL:-}" ]; then
        _install_lock_fd="${MCS_UPDATE_LOCK_FD:-}"
        case "$_install_lock_fd" in ''|*[!0-9]*) die "更新の排他制御を確認できません。更新ツールから再導入してください" ;; esac
    elif [ -n "${MCS_UPDATE_LOCK_FD:-}" ]; then
        die "更新の排他制御が不明です。担当者に確認してください"
    fi
    _directory_info() {
        stat -Lc '%u %a' "$1" 2>/dev/null || stat -Lf '%u %Lp' "$1" 2>/dev/null
    }
    _lock_info() {
        stat -c '%d %i %u %a %h' "$1" 2>/dev/null || stat -f '%d %i %u %Lp %l' "$1" 2>/dev/null
    }
    _held_info() {
        stat -Lc '%d %i %u %a %h' "/dev/fd/$1" 2>/dev/null || stat -f '%d %i %u %Lp %l' <&"$1" 2>/dev/null
    }
    _safe_dir() {
        [ -d "$1" ] || return 1
        _info="$(_directory_info "$1")" || return 1
        IFS=' ' read -r _dir_owner _dir_mode <<EOF
$_info
EOF
        [ "$_info" = "$_dir_owner $_dir_mode" ] && [ "$_dir_owner" = "$_lock_uid" ] || return 1
        case "$_dir_mode" in ''|*[!0-7]*) return 1 ;; esac
        [ $((0$_dir_mode & 022)) -eq 0 ]
    }
    _unsafe_dir() {
        die "$1 の所有者または書き込み権限を確認できません。所有者が本人ではないか、本人以外に書き込みを許可している可能性があります。設定の改変を防ぐため中止しました。所有者と権限を管理者に確認してください。既存設定は保持しています"
    }
    _check_install_dirs() {
        _safe_dir "$HOME" || _unsafe_dir "ホーム"
        if [ -e "$MCS_ROOT" ] || [ -L "$MCS_ROOT" ]; then
            _safe_dir "$MCS_ROOT" || _unsafe_dir "設定フォルダー"
        fi
        if [ -e "$_lock_data" ] || [ -L "$_lock_data" ]; then
            _safe_dir "$_lock_data" || _unsafe_dir "データフォルダー"
        fi
    }
    # Pin a private root symlink before any fresh data or lock is created.
    # Physical cwd prevents a later replacement of the alias from redirecting
    # the installer's writes; keep its identity for later conflict checks.
    _root_alias=""
    _root_alias_info=""
    _root_alias_text=""
    _verify_root_alias() {
        [ -n "$_root_alias" ] || return 0
        [ -L "$_root_alias" ] && [ "$(_lock_info "$_root_alias")" = "$_root_alias_info" ] \
            && [ "$(readlink "$_root_alias")" = "$_root_alias_text" ] \
            && [ "$(_lock_info "$_root_alias/.")" = "$(_lock_info .)" ]
    }
    _safe_dir "$HOME" || _unsafe_dir "ホーム"
    if [ -L "$MCS_ROOT" ]; then
        _root_alias="$MCS_ROOT"
        _root_alias_info="$(_lock_info "$_root_alias")" || _unsafe_dir "設定フォルダー"
        IFS=' ' read -r _alias_dev _alias_ino _alias_owner _alias_mode _alias_links <<EOF
$_root_alias_info
EOF
        [ "$_alias_owner" = "$_lock_uid" ] || _unsafe_dir "設定フォルダー"
        _root_alias_text="$(readlink "$_root_alias")" || _unsafe_dir "設定フォルダー"
        cd -P "$_root_alias" || _unsafe_dir "設定フォルダー"
        _safe_dir . || _unsafe_dir "設定フォルダー"
        _verify_root_alias || die "設定フォルダーのリンクが変更されました。記録を保持してください"
        MCS_ROOT="$(pwd -P)" || _unsafe_dir "設定フォルダー"
        export MCS_ROOT
    fi
    _lock_data="$MCS_ROOT/data"
    _lock_file="$_lock_data/update.lock"
    _install_lock_held=0
    _fresh_data_info=""
    acquire_install_lock() {
    [ "$_install_lock_held" -eq 0 ] || return 0
    _check_install_dirs
    _verify_root_alias || die "設定フォルダーのリンクが変更されました。記録を保持してください"
    if [ ! -e "$MCS_ROOT" ] && [ ! -L "$MCS_ROOT" ]; then
        [ -z "${MCS_UPDATE_REINSTALL:-}" ] || die "更新の設定フォルダーを確認できません"
        mkdir -m 700 "$MCS_ROOT" || die "設定フォルダーを作成できません"
    fi
    _safe_dir "$MCS_ROOT" || _unsafe_dir "設定フォルダー"
    if [ ! -e "$_lock_data" ] && [ ! -L "$_lock_data" ]; then
        [ -z "${MCS_UPDATE_REINSTALL:-}" ] || die "更新のデータフォルダーを確認できません"
        mkdir -m 700 "$_lock_data" || die "データフォルダーを作成できません"
        _fresh_data_info="$(_lock_info "$_lock_data")" || die "新規データフォルダーを確認できません"
    fi
    _safe_dir "$_lock_data" || _unsafe_dir "データフォルダー"
    if [ -n "${MCS_UPDATE_REINSTALL:-}" ]; then
        _lock_py="$MCS_ROOT/venv/bin/python3"
        [ -x "$_lock_py" ] || _lock_py="$(command -v python3 || true)"
        [ -n "$_lock_py" ] || die "更新の排他制御を確認する Python が必要です"
        "$_lock_py" -I -c '
# mcs-installer-inherited-lock
import fcntl, os, stat, sys
try:
    fd = int(sys.argv[1]); path = sys.argv[2]
    if fd < 3: raise ValueError
    probe = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        held, actual = os.fstat(fd), os.fstat(probe)
        if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid() or held.st_nlink != 1
                or stat.S_IMODE(held.st_mode) != 0o600
                or (held.st_dev, held.st_ino) != (actual.st_dev, actual.st_ino)):
            raise ValueError
        try: fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError: pass
        else: raise ValueError
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = os.stat(path, follow_symlinks=False)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino): raise ValueError
    finally: os.close(probe)
except (OSError, ValueError, OverflowError): raise SystemExit(1) from None
' "$_install_lock_fd" "$_lock_file" || die "更新の排他制御を確認できません。記録を保持して担当者に確認してください"
    else
        if [ -e "$_lock_file" ] || [ -L "$_lock_file" ]; then
            if [ ! -f "$_lock_file" ] || [ -L "$_lock_file" ]; then
                die "排他制御ファイルを確認できません"
            fi
        fi
        if [ "${MCS_UPDATE_RUN_LOCK_FD:-}" = 9 ]; then
            exec 8>>"$_lock_file" || die "排他制御ファイルを開けません"
            _install_lock_fd=8
        else
            exec 9>>"$_lock_file" || die "排他制御ファイルを開けません"
            _install_lock_fd=9
        fi
        _lock_stat="$(_lock_info "$_lock_file")" || die "排他制御ファイルを確認できません"
        _fd_stat="$(_held_info "$_install_lock_fd")" || die "排他制御ファイルを確認できません"
        [ "$_lock_stat" = "$_fd_stat" ] || die "排他制御ファイルが変更されました。担当者に確認してください"
        IFS=' ' read -r _lock_dev _lock_inode _lock_owner _lock_mode _lock_links <<EOF
$_lock_stat
EOF
        if ! { [ "$_lock_stat" = "$_lock_dev $_lock_inode $_lock_owner $_lock_mode $_lock_links" ] \
            && [ "$_lock_owner" = "$_lock_uid" ] && [ "$_lock_mode" = 600 ] && [ "$_lock_links" = 1 ]; }; then
            die "排他制御ファイルの所有者または権限を確認できません"
        fi
        if command -v lockf >/dev/null 2>&1; then
            lockf -s -t 0 "$_install_lock_fd" || die "別の導入または更新が進行中です。完了後に再実行してください"
        elif command -v flock >/dev/null 2>&1; then
            flock -n "$_install_lock_fd" || die "別の導入または更新が進行中です。完了後に再実行してください"
        else
            die "排他制御を行う lockf または flock が必要です。導入してから再実行してください"
        fi
        [ "$(_lock_info "$_lock_file")" = "$(_held_info "$_install_lock_fd")" ] \
            || die "排他制御ファイルが変更されました。担当者に確認してください"
    fi
    _verify_root_alias || die "設定フォルダーのリンクが変更されました。記録を保持してください"
    MCS_INSTALL_UPDATE_LOCK_FD="$_install_lock_fd"
    export MCS_INSTALL_UPDATE_LOCK_FD
    _install_lock_held=1
    }
    _check_install_dirs
    # Check interrupted provider migrations before creating or opening a lock.
    if [ -e "$_lock_data/.provider-settings-pending.json" ] || [ -L "$_lock_data/.provider-settings-pending.json" ] \
        || [ -e "$_lock_data/.provider-settings-authority.json" ] || [ -L "$_lock_data/.provider-settings-authority.json" ] \
        || [ -e "$_lock_data/provider_settings" ] || [ -L "$_lock_data/provider_settings" ]; then
        _provider_py="$MCS_ROOT/venv/bin/python3"
        [ -x "$_provider_py" ] || _provider_py="$(command -v python3 || true)"
        [ -n "$_provider_py" ] || die "提供元設定の確認には Python が必要です。変更は行っていません"
        "$_provider_py" -B "$REPO/mcs/ops/install_options.py" provider-check "$MCS_ROOT/config.json" "$_lock_data/install_options.json" - \
            || die "提供元設定を確認できないため変更せず中止しました"
    fi
    # Existing installs and updater children must respect the held lock before
    # reading settings. A fresh install creates its lock only after the guards.
    if [ -e "$_lock_file" ] || [ -L "$_lock_file" ] || [ -n "${MCS_UPDATE_REINSTALL:-}" ]; then
        acquire_install_lock
    fi
fi
CONFIG_ABSENT=0
CONFIG_EMPTY=0
RESUME_UNCONFIGURED=0
CONFIG_BASE=""
if [ -e "$MCS_ROOT/config.json" ] || [ -L "$MCS_ROOT/config.json" ]; then
    MODE_PY="$MCS_ROOT/venv/bin/python3"
    if [ ! -x "$MODE_PY" ]; then MODE_PY="$(command -v python3 || true)"; fi
    [ -n "$MODE_PY" ] || die "既存設定の接続方法を確認するには Python が必要です。Python を導入して再実行してください"
    read_install_config() {
    "$MODE_PY" - "$MCS_ROOT/config.json" "$RUNTIME_EXPLICIT" "$RUNTIME_MODE" <<'PYMODE'
import json
import hashlib
import os
import stat
import sys
# Keep in sync with mcs_util.CONFIG_MAX_BYTES; this runs before the MCS venv exists.
config_max_bytes = 1024 * 1024
try:
    fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > config_max_bytes:
            raise ValueError
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(config_max_bytes + 1)
        if len(raw) > config_max_bytes:
            raise ValueError
        cfg = json.loads(raw.decode("utf-8"))
        ident = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if ident(info) != ident(os.fstat(fd)) or ident(info) != ident(os.stat(sys.argv[1], follow_symlinks=False)):
            raise ValueError
    finally:
        os.close(fd)
    if not isinstance(cfg, dict):
        raise ValueError
    mode = sys.argv[3] if sys.argv[2] == "1" else cfg.get("runtime_mode", "hermes")
    if mode not in ("hermes", "standalone"):
        raise ValueError
    token = ":".join(str(n) for n in ident(info)) + ":" + hashlib.sha256(raw).hexdigest()
    if not cfg:
        print(mode + ":empty:" + token)
    else:
        print(mode + ":existing:" + token)
except (OSError, ValueError, AttributeError, TypeError, RecursionError):
    raise SystemExit(1) from None
PYMODE
    }
    MODE_STATUS="$(read_install_config)" || die "config.json を読み取れないため接続方法を決定できません。設定ファイルを修復してください（--mode は読み取れる設定の接続方法だけを上書きします）"
    RUNTIME_MODE="${MODE_STATUS%%:*}"
    MODE_DETAIL="${MODE_STATUS#*:}"
    CONFIG_BASE="${MODE_DETAIL#*:}"
    if [ "${MODE_DETAIL%%:*}" = empty ]; then
        CONFIG_EMPTY=1
    fi
    case "$RUNTIME_MODE" in hermes|standalone) ;; *) die "接続方法を読み取れません。--mode hermes または --mode standalone を指定してください" ;; esac
else
    CONFIG_ABSENT=1
fi
# Only a checked bootstrap authority can authorize a later initial choice.
_bootstrap_runtime_token=""
if [ "$MODE" = install ] && [ "$CONFIG_ABSENT" -eq 0 ] \
        && { [ -e "$MCS_ROOT/data/.provider-settings-authority.json" ] || [ -L "$MCS_ROOT/data/.provider-settings-authority.json" ]; }; then
    _bootstrap_inspect=$("$MODE_PY" -B "$REPO/mcs/ops/install_options.py" bootstrap-runtime-inspect \
        "$MCS_ROOT/config.json" "$MCS_ROOT/data/install_options.json" "$CONFIG_BASE") \
        || die "初回設定を確認できません。設定と記録を保持してください"
    case "$_bootstrap_inspect" in
        none) ;;
        bootstrap:*) _bootstrap_runtime_token="$_bootstrap_inspect" ;;
        *) die "初回設定を確認できません。設定と記録を保持してください" ;;
    esac
fi
# An interrupted noninteractive first install can have created only {}.
# A completed install or any other existing settings retain their mode.
if [ "$RUNTIME_EXPLICIT" -eq 0 ] && [ "$MODE" = install ] && [ -t 0 ] && [ -t 1 ] \
        && { [ "$CONFIG_ABSENT" -eq 1 ] || [ -n "$_bootstrap_runtime_token" ] || { [ "$CONFIG_EMPTY" -eq 1 ] \
        && [ ! -e "$MCS_ROOT/data/install_options.json" ] \
        && [ ! -L "$MCS_ROOT/data/install_options.json" ]; }; }; then
    printf '%s\n' "MCS の導入を始めます。番号を入力して Enter キーで進みます。" \
        "Slack / Discord との連携方式を選んでください。" \
        "  1) わからないので推奨を使う — Hermes Agent と連携（標準）" \
        "  2) MCS 単独で使う — Hermes Agent を導入しない" \
        "LINE WORKS を使う場合も、通知先は後の初期設定で選べます。"
    choose '選択 [1・推奨 / 2 / 0・中止]: '
    case "$_answer" in
        2) RUNTIME_MODE=standalone ;;
        1) RUNTIME_MODE=hermes ;;
    esac
    RUNTIME_EXPLICIT=1
    [ "$CONFIG_ABSENT" -eq 1 ] || RESUME_UNCONFIGURED=1
fi
DEFAULT_HERMES_HOME="$(abspath "$HOME")/.hermes"
ENV_HERMES_HOME="${HERMES_HOME:-}"
if [ -n "$ARG_HOME" ]; then HERMES_HOME="$(abspath "$ARG_HOME")"
else HERMES_HOME="$DEFAULT_HERMES_HOME"; fi
if [ -n "$ENV_HERMES_HOME" ] \
        && [ "$(abspath "$ENV_HERMES_HOME")" != "$HERMES_HOME" ]; then
    warn "環境変数 HERMES_HOME=$ENV_HERMES_HOME は使用しません。導入先は $HERMES_HOME です（別の保存先は引数で指定してください）"
fi
# every hermes command below must act on the home being installed
export HERMES_HOME
RUNTIME_HOME="$HERMES_HOME"
if [ "$RUNTIME_MODE" = standalone ]; then
    [ -z "$ARG_HOME" ] || die "MCS 単独接続では HERMES_HOME を指定できません。保存先の引数を外してください"
    RUNTIME_HOME="$MCS_ROOT"
    SKIP_PLUGIN=1
fi

HERMES_DIR="$HERMES_HOME/hermes-agent"
HERMES_REPO="https://github.com/yusuketakuma/hermes-agent.git"
# Pinned: MCS companion based on upstream v0.22.2.
# Native context/attestation and async pre-dispatch contracts remain required.
HERMES_PIN="2c08ca7ade8f867d07e4bff6581be54405b0c9d0"
# Pins validated by earlier MCS releases; only these move to
# HERMES_PIN, and only during an updater-proven runtime migration.
HERMES_PREVIOUS_PINS="b7758d8555931846f4b8f798d4ea9715f65077ca f36c1da0016ce14c9c531cb9514ab4b0b667240d 6f35b1ed84536f7e7fd2f605f7779dd051cce05f fd50a275e2616118c48fe07e7e1c878782b15ccd 1e8fbfe6b41399a46e232f6cde5618cfdfa2729a 16390dc80d248b7b49554c0cde52caf22e1fc3d8"
CLONE_MARK="$HERMES_HOME/.hermes-agent.install-in-progress"
VENV="$HERMES_DIR/venv"
if [ "$RUNTIME_MODE" = standalone ]; then VENV="$RUNTIME_HOME/venv"; fi
VENV_PY="$VENV/bin/python"
if [ "$RUNTIME_MODE" = standalone ]; then VENV_PY="$VENV/bin/python3"; fi
VENV_HERMES="$VENV/bin/hermes"
PIP_MARK="$VENV/.mcs-pip-incomplete"
HERMES_BIN_DIR="$HOME/.local/bin"
SHIM="$HERMES_BIN_DIR/hermes"
MCS_SHIM="$HERMES_BIN_DIR/mcs"
PLUGIN_LINK="$HERMES_HOME/plugins/mcs-discord-commands"
LLM_MODELS_URL="http://127.0.0.1:8080/v1/models"
MODEL_DIR="$RUNTIME_HOME/models"
MODEL_FILE="$MODEL_DIR/Qwen3.5-9B-Q4_K_M.gguf"
# Pinned revision and its LFS sha256 (Hugging Face API + resolve headers,
# 2026-10-11); a floating main could swap the bytes that read PHI bodies.
MODEL_URL="https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/3885219b6810b007914f3a7950a8d1b469d598a5/Qwen3.5-9B-Q4_K_M.gguf"
MODEL_SHA256="03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8"
# The pin is overridable only for tests and explicit verification runs.
if [ -n "${MCS_MODEL_SHA256:-}" ]; then
  if [ "${MCS_ALLOW_MODEL_SHA_OVERRIDE:-}" = 1 ]; then
    MODEL_SHA256="$MCS_MODEL_SHA256"
    warn "MCS_MODEL_SHA256 の指定値でモデルを照合します（固定 sha256 を使いません）"
  else
    warn "MCS_MODEL_SHA256 は MCS_ALLOW_MODEL_SHA_OVERRIDE=1 がないため無視し、固定 sha256 で照合します"
  fi
fi
AGENTS_DIR="$HOME/Library/LaunchAgents"
LLAMA_PLIST="$AGENTS_DIR/ai.mcs.llamaserver.plist"
HERMES_LLAMA_PLIST="$AGENTS_DIR/ai.hermes.llamacpp.plist"
MCS_DATA="$MCS_ROOT/data"
RECOVERY_DIR="$HOME/.mcs-recovery"
WATCH_PLIST="$AGENTS_DIR/org.mcs.recovery.plist"
RECOVERY_TEMPLATE="$REPO/deployment/launchagents/org.mcs.recovery.plist"
SETUP="$REPO/mcs/ops/mcs_setup.py"
# shellcheck disable=SC2016  # printed for the user, never run here
BREW_INSTALL='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
OS="$(uname -s)"
IS_ROOT=0; [ "$(id -u)" -ne 0 ] || IS_ROOT=1

# Quote data for the generated shell shim and XML/sed replacements.
shell_quote() {
    printf "'"
    printf '%s' "$1" | sed "s/'/'\\\\''/g"
    printf "'"
}
xml_sed() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' \
        -e 's/>/\&gt;/g' -e 's/[\\&|]/\\&/g'
}
# <interpreter> <min minor> <max minor, exclusive> — Python 3.x in range
py_ok() {
    case "$1" in
        */*) [ -x "$1" ] || return 1 ;;
        *)   command -v "$1" >/dev/null 2>&1 || return 1 ;;
    esac
    "$1" -c "import sys; sys.exit(0 if (3, $2) <= sys.version_info[:2] < (3, $3) else 1)" \
        >/dev/null 2>&1
}
# hermes-agent requires >=3.14,<3.15 — prefer an explicitly versioned
# interpreter over a possibly-too-new python3
find_py() {
    for _c in python3.14 python3; do
        if py_ok "$_c" 14 15; then command -v "$_c"; return 0; fi
    done
    return 1
}

llm_serving() {
    _probe_py="${MCS_PY:-}"
    if [ -z "$_probe_py" ]; then _probe_py="$(command -v python3 || true)"; fi
    [ -n "$_probe_py" ] || return 1
    curl -sf --noproxy "*" --max-filesize 1048576 -m 3 "$LLM_MODELS_URL" 2>/dev/null | "$_probe_py" -c '
# mcs-model-list-probe
import json, sys
try:
    value = json.load(sys.stdin)
    rows = value.get("data") if isinstance(value, dict) else None
    valid = isinstance(rows, list) and any(
        isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].strip()
        for row in rows)
except (ValueError, RecursionError):
    valid = False
raise SystemExit(0 if valid else 1)
' 2>/dev/null
}

# Verify the inherited run.lock and journaled path, including resumed installs.
migration_path() {
    _mb_py="${PYBIN:-$(find_py || true)}"
    [ -n "$_mb_py" ] || return 1
    "$_mb_py" -I -c '
# mcs-runtime-migration-lock
import fcntl, os, re, stat, sys
try:
    number = os.environ.get("MCS_UPDATE_RUN_LOCK_FD", "")
    if not number.isdecimal() or int(number) < 3:
        raise ValueError
    fd = int(number)
    lock = os.lstat(sys.argv[2])
    held = os.fstat(fd)
    if (not stat.S_ISREG(lock.st_mode) or not stat.S_ISREG(held.st_mode)
            or lock.st_uid != os.getuid() or held.st_uid != os.getuid()
            or (lock.st_dev, lock.st_ino) != (held.st_dev, held.st_ino)):
        raise ValueError
    backup = os.environ.get("MCS_UPDATE_RUNTIME_BACKUP", "")
    if (not os.path.isabs(backup)
            or not re.fullmatch(re.escape(sys.argv[1]) + r"\.pre-python314(?:\.[1-9][0-9]*)?", backup)):
        raise ValueError
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print(backup)
except (OSError, ValueError, OverflowError):
    raise SystemExit(1) from None
' "$VENV" "$MCS_DATA/run.lock"
}

# Moving the old venv always requires an unused backup; never replace one.
migration_backup() {
    _mb_backup="$(migration_path)" || return 1
    [ ! -e "$_mb_backup" ] && [ ! -L "$_mb_backup" ] || return 1
    printf '%s\n' "$_mb_backup"
}

# [project].requires-python of the Hermes checkout, judged by the Python 3.14
# that builds the venv (the same script mcs_update's preflight runs).
hermes_admits_314() {
    _ha_py="${PYBIN:-$(find_py || true)}"
    [ -n "$_ha_py" ] || return 1
    "$_ha_py" -I "$REPO/mcs/ops/requires_python_admits.py" "$HERMES_DIR/pyproject.toml"
}

preserve_old_venv() {
    if [ ! -e "$VENV" ] && [ ! -L "$VENV" ]; then return; fi
    if [ -n "${MCS_UPDATE_RUN_LOCK_FD:-}" ] || [ -n "${MCS_UPDATE_RUNTIME_BACKUP:-}" ]; then
        _prior_venv="$(migration_backup)" \
            || die "実行環境の移行ロックまたはバックアップが不正です。既存の Python 環境は保持しました"
    else
        # A scheduled tick can start after a process check; established installs
        # require the updater's inherited run.lock for runtime migration.
        if [ -e "$MCS_DATA/ledger.db" ] || [ -e "$MCS_DATA/service_manifest.json" ]; then
            die "導入済み MCS の Python 移行には更新ツールが必要です。既存環境は保持しました。リリースの更新手順に従ってください"
        fi
        _runtime_processes="$(ps -axo command=)" \
            || die "実行環境の停止を確認できません。既存の Python 環境は保持しました"
        if printf '%s\n' "$_runtime_processes" | MCS_VENV_PREFIX="$VENV/" awk '
            index($0, ENVIRON["MCS_VENV_PREFIX"]) == 1 { busy=1 }
            END { exit !busy }'; then
            die "既存環境は稼働中です。Gateway または独立接続サービスを通常の停止手順で止めてから再実行してください。既存環境は保持しました"
        fi
        unset _runtime_processes
        _prior_venv="$VENV.pre-python314"
        _prior_n=0
        while [ -e "$_prior_venv" ] || [ -L "$_prior_venv" ]; do
            _prior_n=$((_prior_n + 1))
            _prior_venv="$VENV.pre-python314.$_prior_n"
        done
    fi
    mv "$VENV" "$_prior_venv" || die "旧実行環境を退避できません。再構築は行っていません"
    ok "旧実行環境の退避先: $_prior_venv"
}

recovery_runtime_ok() {  # <probe host Python>; never substitutes the template's executable
    "$1" -I -B -c '
import os, plistlib, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/mcs")
import _mcs_path
import lifecycle_io
import service_contract
import mcs_config
import mcs_runtime
import runtime_inspection
import run_log_install
from scheduled_jobs import render_template
try:
    cfg = lifecycle_io.read_owner_config(sys.argv[3])
except ValueError as exc:
    raise SystemExit("復旧設定を読み取れません: " + str(exc)) from None
if sys.argv[4]:
    cfg["recovery_python"] = sys.argv[4]
if sys.argv[5] == "1" and "recovery_python" not in cfg:
    sys.exit(0)
desired = runtime_inspection.recovery_python(cfg)
problem = mcs_config.recovery_python_problem(desired, sys.argv[1])
if problem:
    raise SystemExit("復旧用 Python に問題があります: " + problem)
if "recovery_python" in cfg and os.path.lexists(
        Path(sys.argv[6], service_contract.RECOVERY_LABEL + ".plist")):
    deployed = runtime_inspection.recovery_executable(runtime_inspection.read_recovery_plist(
        Path(sys.argv[6], service_contract.RECOVERY_LABEL + ".plist")))
    if deployed != desired:
        raise SystemExit("復旧用 Python の設定と稼働設定が一致しません。処理が動いていないことを確認し、mcs setup init --yes、mcs setup services の順に修復してください")
from html import escape
text = Path(sys.argv[2]).read_text()
helper = run_log_install.helper_path(Path(sys.argv[3]).parent, sys.argv[7], checkout=sys.argv[1])
rendered = render_template(text, {"RECOVERY_PYTHON":escape(desired, quote=False),
    "RUN_LOG_HELPER":escape(helper, quote=False), "RECOVERY":escape(sys.argv[7], quote=False),
    "DATA":escape(str(Path(sys.argv[3]).parent / "data"), quote=False)})
actual = runtime_inspection.recovery_executable(plistlib.loads(rendered.encode()))
problem = mcs_config.recovery_python_problem(actual, sys.argv[1])
if problem:
    raise SystemExit("復旧サービスのテンプレートに問題があります: " + problem)
if "recovery_python" in cfg and actual != desired:
    raise SystemExit("復旧サービスのテンプレートが指定した Python と一致しません")
facts = runtime_inspection.probe_python(actual) if actual is not None else None
problem = mcs_runtime.runtime_problem(facts, recovery=True)
if problem:
    print("復旧用 Python に問題があります: " + problem, file=sys.stderr)
    sys.exit(1)
print(desired)
' "$REPO" "$RECOVERY_TEMPLATE" "$MCS_ROOT/config.json" "$RECOVERY_OPTION" "$SKIP_RECOVERY" "$AGENTS_DIR" "$RECOVERY_DIR"
}
loaded() { launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1; }
# launchd may still be tearing down a just-booted-out job ("5: Input/
# output error") — retry briefly; success is the label being loaded, not
# a zero exit (same semantics as service_management.launchd_bootstrap).
bootstrap_agent() {  # <label> <plist>
    _n=0
    while [ "$_n" -lt 3 ]; do
        launchctl bootstrap "gui/$(id -u)" "$2" && break
        _n=$((_n + 1))
        sleep 1
    done
    loaded "$1"
}
# <tmp> <dst>: publish tmp over dst (rename) only when the content
# differs; returns 0 when dst changed
publish() {
    if [ -f "$2" ] && cmp -s "$1" "$2"; then
        rm -f "$1" || die "一時ファイル $1 を削除できません。アクセス権を確認して install.sh を再実行してください"
        return 1
    fi
    # Called in an if: POSIX shells disable errexit throughout this function.
    mv -f "$1" "$2" || die "$2 を配置できません。アクセス権と空き容量を確認して install.sh を再実行してください"
}
# the checkout an existing install points at, when it is not $REPO
# (a recorded checkout that no longer exists is not a conflict)
repo_conflict() {
    if [ -f "$RECOVERY_DIR/repo_path" ]; then
        _r="$(sed -n 1p "$RECOVERY_DIR/repo_path")"
        if [ -d "$_r" ] && [ "$(physpath "$_r")" != "$REPO" ]; then
            printf '%s\n' "$_r"; return 0
        fi
    fi
    if [ "$RUNTIME_MODE" = hermes ] && [ -L "$PLUGIN_LINK" ] && _t="$(link_target "$PLUGIN_LINK")" \
            && [ "$_t" != "$REPO/hermes_plugin" ]; then
        dirname -- "$_t"
    fi
}
# the recovery tool is wanted: --recovery-python given, or macOS without --no-recovery
recovery_wanted() {
    [ -n "$RECOVERY_OPTION" ] || { [ "$OS" = Darwin ] && [ "$SKIP_RECOVERY" -eq 0 ]; }
}
# resident services are registered but cannot use a custom HERMES_HOME
custom_home_blocks_services() {
    [ "$RUNTIME_MODE" = hermes ] && [ "$HERMES_HOME" != "$DEFAULT_HERMES_HOME" ] && [ "$SKIP_SERVICES" -eq 0 ]
}
# why ~/.local/bin/mcs blocks the launcher step: "foreign" (not an MCS
# lifecycle launcher), "other" (another checkout's launcher) or nothing
mcs_shim_conflict() {
    [ -e "$MCS_SHIM" ] || [ -L "$MCS_SHIM" ] || return 0
    if ! { [ -f "$MCS_SHIM" ] && [ ! -L "$MCS_SHIM" ] \
        && grep -qx '# MCS lifecycle launcher' "$MCS_SHIM"; }; then
        printf 'foreign\n'
    elif ! grep -qF "$(shell_quote "$REPO/mcs/ops/mcs_cli.py")" "$MCS_SHIM"; then
        printf 'other\n'
    fi
}

# ------------------------------------------------------------ preflight
PF_NG=0; PF_WARN=0
pf_ok()   { printf '  正常  %s\n' "$1"; }
pf_info() { printf '  --    %s\n' "$1"; }
pf_warn() {
    printf '  注意  %s\n' "$1"; PF_WARN=$((PF_WARN + 1))
    if [ -n "${2:-}" ]; then printf '        対処: %s\n' "$2"; fi
}
pf_ng() {
    printf '  要対処  %s\n        対処: %s\n' "$1" "$2"; PF_NG=$((PF_NG + 1))
}
pf_net() {  # <label> <url> <needed: 1 = blocker when unreachable>
    if curl -sSfIL -m 10 -o /dev/null "$2" >/dev/null 2>&1; then
        pf_ok "通信: $1 に接続できました"
    elif [ "$3" -eq 1 ]; then
        pf_ng "通信: $1 に接続できません" "ネットワーク・プロキシ設定を確認して接続を再確認: curl -I $2"
    else
        pf_warn "通信: $1 に接続できません（今回は不要です）"
    fi
}

preflight() {
    say "事前確認（設定やファイルは変更しません）"
    pf_info "接続方法: ${RUNTIME_MODE}（--mode hermes または --mode standalone で変更）"
    if [ "$IS_ROOT" -eq 1 ]; then
        pf_ng "管理者 root で実行されています" "sudo を使わず、通常のユーザーで再実行してください"
    else
        pf_ok "通常のユーザーで実行されています"
    fi
    if [ "$OS" = Darwin ]; then
        _v="$(sw_vers -productVersion 2>/dev/null || echo unknown)"
        case "${_v%%.*}" in
            ''|*[!0-9]*) pf_warn "macOS の版を確認できません（${_v}）" ;;
            *) if [ "${_v%%.*}" -ge 13 ]; then pf_ok "macOS $_v"
               else pf_ng "macOS $_v は古すぎます（Homebrew／llama.cpp は13以降が必要）" "システム設定 → 一般 → ソフトウェアアップデートで macOS を更新してください"; fi ;;
        esac
        _arch="$(uname -m)"
        if [ "$_arch" = arm64 ]; then pf_ok "CPU: ${_arch}（Apple シリコン）"
        else pf_warn "CPU: $_arch — ローカル AI の Metal 高速化が使えず、処理に時間がかかります" ""; fi
        if xcode-select -p >/dev/null 2>&1; then pf_ok "Xcode Command Line Tools"
        else pf_ng "Xcode Command Line Tools が未導入です" "xcode-select --install"; fi
    else
        pf_warn "macOS ではありません（${OS}）。段階4の AI 常駐設定と段階6の復旧監視を省略します" ""
    fi

    if [ "$SKIP_BREW" -eq 1 ]; then pf_info "Homebrew の導入を省略します（--no-brew）"
    elif command -v brew >/dev/null 2>&1; then pf_ok "Homebrew: $(command -v brew)"
    else pf_ng "Homebrew が未導入です" "$BREW_INSTALL"
    fi
    _brew_fills=0
    if [ "$SKIP_BREW" -eq 0 ] && command -v brew >/dev/null 2>&1; then _brew_fills=1; fi
    if [ "$RUNTIME_MODE" = hermes ] && [ "$SKIP_BREW" -eq 1 ] && ! command -v uv >/dev/null 2>&1; then
        pf_ng "uv が未導入です（Hermes の導入に uv が必要です）" "brew install uv"
    fi
    if _py="$(find_py)"; then pf_ok "Python 3.14: $_py"
    elif [ "$_brew_fills" -eq 1 ]; then pf_info "Python 3.14 は未導入です。段階1で python@3.14 を導入します"
    else pf_ng "実行できる Python 3.14 が見つかりません" "brew install python@3.14"; fi
    if recovery_wanted; then
        if [ -n "$_py" ] && RECOVERY_PYTHON="$(recovery_runtime_ok "$_py")"; then
            pf_ok "復旧用 Python: 3.14以降で SQLite の安全条件を満たしています"
        elif [ -z "$_py" ] && [ -z "$RECOVERY_OPTION" ] && [ "$_brew_fills" -eq 1 ]; then
            # same as the install path: stage 6 re-verifies after stage 1 adds Python
            pf_warn "python@3.14 の導入後、段階6で復旧用 Python を確認します" ""
        else
            pf_ng "復旧サービスのテンプレートに問題があります: Python の安全条件を満たさないか、確認できません" \
                "安全条件を満たす独立した既存 Python を --recovery-python で指定してください。自動で別の Python を選択・導入することはありません"
        fi
    fi
    _need_clone=0
    if [ "$RUNTIME_MODE" = hermes ] && { [ -f "$CLONE_MARK" ] || [ ! -e "$HERMES_DIR" ]; }; then _need_clone=1; fi
    if command -v git >/dev/null 2>&1; then pf_ok "git: $(command -v git)"
    elif [ "$_brew_fills" -eq 1 ]; then pf_info "git は未導入です。段階1で導入します"
    elif [ "$_need_clone" -eq 1 ]; then pf_ng "git が未導入です" "xcode-select --install   （または brew install git）"
    fi

    _need_gb=12
    if [ "$SKIP_LLM" -eq 1 ] || [ -f "$MODEL_FILE" ]; then _need_gb=6; fi
    _free_kb="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
    case "$_free_kb" in
        ''|*[!0-9]*) pf_warn "ディスク: $HOME の空き容量を確認できません" "" ;;
        *) if [ "$_free_kb" -ge $((_need_gb * 1024 * 1024)) ]; then
               pf_ok "ディスク: 空き $((_free_kb / 1024 / 1024)) GB（必要容量は約 $_need_gb GB）"
           else
               pf_ng "ディスク: $HOME の空きは $((_free_kb / 1024 / 1024)) GB です（必要容量は約 $_need_gb GB、モデルは5.7 GB）" "空き容量を増やして再実行してください"
           fi ;;
    esac
    pf_net github.com https://github.com "$_need_clone"
    _need_hf=0
    if [ "$SKIP_LLM" -eq 0 ] && [ "$OS" = Darwin ] && [ ! -f "$MODEL_FILE" ]; then _need_hf=1; fi
    pf_net huggingface.co "$MODEL_URL" "$_need_hf"

    if [ "$RUNTIME_MODE" = standalone ]; then
        pf_info "MCS 単独接続の実行環境: ${VENV}（Hermes は不要です）"
        pf_net pypi.org https://pypi.org 1
    else
    if command -v hermes >/dev/null 2>&1; then pf_info "Hermes コマンド: $(command -v hermes)"
    else pf_info "Hermes コマンドは未導入です。段階2で $SHIM を作成します"; fi
    if [ -f "$CLONE_MARK" ]; then
        pf_info "$HERMES_DIR の取得が途中で止まっています。段階2でやり直します"
    elif [ -e "$HERMES_DIR/.git" ]; then
        if ! _h="$(git -C "$HERMES_DIR" rev-parse HEAD 2>/dev/null)"; then
            pf_ng "$HERMES_DIR の現在の版（HEAD）を読み取れません" "mv $(shell_quote "$HERMES_DIR") $(shell_quote "$HERMES_DIR.bak")   # 段階2で再取得します"
        elif [ "$_h" = "$HERMES_PIN" ]; then pf_ok "Hermes は検証済みの固定版です"
        else pf_warn "Hermes は $_h です。検証済み固定版 $HERMES_PIN と異なります（現状を保持します）" "git -C $(shell_quote "$HERMES_DIR") checkout --detach $HERMES_PIN"; fi
    elif [ -e "$HERMES_DIR" ]; then
        pf_ng "$HERMES_DIR は存在しますが Git で取得したフォルダではありません" "mv $(shell_quote "$HERMES_DIR") $(shell_quote "$HERMES_DIR.bak")"
    else pf_info "Hermes は未取得です。段階2で $HERMES_PIN を取得します"; fi
    if py_ok "$VENV_PY" 14 15; then pf_ok "Hermes 用 Python: $VENV_PY"
    elif [ -e "$VENV" ]; then pf_info "Hermes 用 Python 3.14 が使用できません。段階2で環境を再作成します"
    else pf_info "Hermes 用 Python 環境は未作成です。段階2で作成します"; fi
    fi

    if [ -f "$REPO/.git" ]; then
        pf_warn "Git の作業用フォルダ（${REPO}）から実行しています。サービスとプラグインはこの場所を参照するため、削除すると動かなくなります" "恒久的に使うメインのフォルダから install.sh を実行してください"
    fi
    _c="$(repo_conflict)"
    if [ -n "$_c" ] && [ "$FORCE_REPO" -eq 0 ]; then
        pf_ng "既存の導入先は別のフォルダです: $_c" "$(shell_quote "$_c/install.sh") を実行してください。$REPO へ切り替える場合は --force-repo を指定します"
    elif [ -n "$_c" ]; then
        pf_warn "--force-repo: 導入先を $_c から $REPO へ切り替えます" ""
    fi
    case "$(mcs_shim_conflict)" in
        foreign) pf_ng "$MCS_SHIM は MCS の起動コマンドではありません" "mv $(shell_quote "$MCS_SHIM") $(shell_quote "$MCS_SHIM.bak")   # 自動では上書きしません" ;;
        other) if [ "$FORCE_REPO" -eq 0 ]; then
                   pf_ng "$MCS_SHIM は別のフォルダを参照しています" "$REPO へ切り替える場合は --force-repo を指定して再実行してください"
               else pf_warn "--force-repo: $MCS_SHIM の参照先を $REPO へ切り替えます" ""; fi ;;
    esac
    if custom_home_blocks_services; then
        pf_ng "常駐サービスは独自の HERMES_HOME（${HERMES_HOME}）に対応していません" "既定の ~/.hermes を使うか、--no-services でサービス登録を省略してください"
    fi
    if [ -e "$MCS_ROOT/.install-options-migration.pending" ] || [ -L "$MCS_ROOT/.install-options-migration.pending" ]; then
        pf_ng "導入設定の移行結果を確認できません" "保留記録と設定を保持し、担当者に確認してください"
    fi

    if [ "$OS" = Darwin ]; then
        if [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then pf_ok "Google Chrome は導入済みです"
        elif [ "$_brew_fills" -eq 1 ]; then pf_info "Google Chrome は未導入です。段階1で導入します"
        else pf_warn "Google Chrome が未導入です（MCS ログインに必要）" "brew install --cask google-chrome"; fi
    fi
    if llm_serving; then
        pf_ok "ポート8080: OpenAI 互換サーバーの応答を確認しました"
    elif command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
        if [ "$SKIP_LLM" -eq 1 ]; then
            pf_warn "ポート8080は使用中ですが /v1/models の応答がありません" ""
        else
            pf_ng "ポート8080が AI サーバー以外の処理に使用されています" "lsof -nP -iTCP:8080 -sTCP:LISTEN   # 使用中の処理を確認して停止するか --no-llm を指定してください"
        fi
    elif [ "$SKIP_LLM" -eq 1 ]; then
        pf_warn "ポート8080の応答がありません。--no-llm の場合はご自身のサーバーを 127.0.0.1:8080 で起動してください" ""
    else pf_ok "ポート8080は使用できます（段階4で AI サーバーを起動します）"; fi

    printf '\n事前確認: 導入を妨げる問題 %s件、注意 %s件\n' "$PF_NG" "$PF_WARN"
}

plan_line() { printf '  %s %s\n' "$1" "$2"; }
state() { if [ -e "$1" ] || [ -L "$1" ]; then printf '既存'; else printf '新規'; fi; }
dry_run_plan() {
    say "導入予定（--dry-run: 以下の処理は実行しません）"
    plan_line "起動コマンド" "$HOME/.local/bin/mcs → 選択した実行環境 + $REPO/mcs/ops/mcs_cli.py"
    if [ "$SKIP_BREW" -eq 1 ]; then plan_line "1/6 基本ソフト" "省略（--no-brew）"
    else plan_line "1/6 基本ソフト" "git・python@3.14・uv・llama.cpp・Google Chrome の未導入分を Homebrew で導入"; fi
    if [ "$RUNTIME_MODE" = standalone ]; then
        plan_line "2/6 独立接続" "専用 Python 環境 $VENV と deployment/requirements-standalone.txt の公式 SDK を導入（runtime_mode=standalone）"
    else
    plan_line "2/6 Hermes" "ソース $HERMES_DIR [$(state "$HERMES_DIR")] @ $HERMES_PIN （既存の取得済みソースは保持）"
    plan_line "" "Python 環境 $VENV [$(state "$VENV_PY")] + uv pip install -e hermes-agent[messaging]（Hermes ディレクトリで実行）"
    plan_line "" "起動コマンド $SHIM [$(state "$SHIM")] （MCS 以外の既存ファイルは保持）"
    fi
    if [ "$RUNTIME_MODE" = standalone ]; then plan_line "3/6 プラグイン" "MCS 単独接続では使用しません"
    elif [ "$SKIP_PLUGIN" -eq 1 ]; then plan_line "3/6 プラグイン" "省略（--no-plugin。LINE WORKS では不要）"
    else plan_line "3/6 プラグイン" "リンク $PLUGIN_LINK [$(state "$PLUGIN_LINK")] -> $REPO/hermes_plugin; hermes plugins enable mcs-discord-commands"; fi
    if [ "$SKIP_LLM" -eq 1 ] || [ "$OS" != Darwin ]; then plan_line "4/6 ローカルAI" "省略"
    else
        # mirror stage 4's order: a hermes-managed agent or a foreign
        # server on :8080 is kept — no second llama-server is planned
        if [ -f "$HERMES_LLAMA_PLIST" ]; then
            plan_line "4/6 ローカルAI" "Hermes 管理の $HERMES_LLAMA_PLIST を保持（起動状態を確認。モデル取得・サーバー重複起動はしません）"
        elif llm_serving \
                && [ ! -f "$LLAMA_PLIST" ] && ! loaded ai.mcs.llamaserver; then
            plan_line "4/6 ローカルAI" "ポート8080の既存サーバーを保持（追加導入なし）"
        else
            plan_line "4/6 ローカルAI" "AI モデル $MODEL_FILE [$(state "$MODEL_FILE")] （新規の場合は約5.7 GBを取得）"
            plan_line "" "起動設定 $LLAMA_PLIST [$(state "$LLAMA_PLIST")]; launchctl bootstrap ai.mcs.llamaserver 未起動の場合に起動"
        fi
    fi
    if [ "$SKIP_SERVICES" -eq 1 ]; then plan_line "5/6 常駐サービス" "省略（--no-services）"
    else
        plan_line "5/6 常駐サービス" "mkdir $MCS_DATA{,/cmd,/cmd_int} （本人のみアクセス可: 700）"
        plan_line "" "$VENV_PY $SETUP services （選択した接続方法の常駐管理）"
    fi
    if [ "$SKIP_RECOVERY" -eq 1 ]; then plan_line "6/6 復旧" "省略（--no-recovery）"
    else
        plan_line "6/6 復旧" "$RECOVERY_DIR/mcs_recover.py [$(state "$RECOVERY_DIR/mcs_recover.py")], repo_path = $REPO"
        plan_line "" "起動設定 $WATCH_PLIST [$(state "$WATCH_PLIST")]; 変更があるか未起動の場合だけ org.mcs.recovery を起動・再起動"
    fi
    plan_line "省略項目の保存" "$MCS_ROOT/config.json （更新計画の install_args に表示。旧記録は保持）"
}

if [ "$MODE" != install ]; then
    preflight
    if [ "$MODE" = dry-run ]; then dry_run_plan; fi
    [ "$PF_NG" -eq 0 ] || exit 1
    exit 0
fi

# ---------------------------------------------------------- guards
[ "$IS_ROOT" -eq 0 ] || die "install.sh は root や sudo で実行しないでください。自分のホームへ導入するため、通常のユーザーで再実行してください"
# mcs_setup services renders cron wrappers/plists against ~/.hermes (its
# HERMES_PY and scripts dir are fixed there) — a custom HERMES_HOME would
# install services pointing at an interpreter that does not exist
if custom_home_blocks_services; then
    printf '%s\n' "常駐サービスは独自の HERMES_HOME（${HERMES_HOME}）に対応していません。" \
        "既定の ~/.hermes を使うか、--no-services を指定してサービスを手動登録してください" >&2
    exit 2
fi
CONFLICT="$(repo_conflict)"
if [ -n "$CONFLICT" ]; then
    [ "$FORCE_REPO" -eq 1 ] || die "この Mac は別フォルダ（${CONFLICT}）から導入されています。その場所の install.sh を実行してください。プラグイン・復旧・サービスを $REPO へ切り替える場合は --force-repo を指定します"
    warn "--force-repo: 導入先を $CONFLICT から $REPO へ切り替えます"
fi
case "$(mcs_shim_conflict)" in
    foreign) die "$MCS_SHIM は MCS の起動コマンドではないため変更しません。別の場所へ退避して再実行してください" ;;
    other) [ "$FORCE_REPO" -eq 1 ] || die "$MCS_SHIM は別のフォルダを参照しています。切り替える場合は --force-repo を指定してください" ;;
esac
if [ -f "$REPO/.git" ]; then
    warn "Git の作業用フォルダ（${REPO}）から実行しています。ここを削除するとサービスとプラグインが動かなくなります。恒久的なメインのフォルダを使ってください"
fi

if recovery_wanted; then
    if _probe_host="$(find_py)"; then
        RECOVERY_PYTHON="$(recovery_runtime_ok "$_probe_host")" \
            || die "指定した復旧用 Python の安全性が確認できないか、稼働設定と一致しません。変更は行っていません"
    elif [ -n "$RECOVERY_OPTION" ]; then
        die "--recovery-python は導入前に Python 3.14 で検証します。brew install python@3.14 などで導入して再実行してください。変更は行っていません"
    else
        # A fresh Mac gets Python from stage 1; stage 6 checks the recovery runtime then.
        warn "Python 3.14 は未導入です。復旧用 Python は段階6で確認します"
    fi
fi

if [ -t 0 ] && [ -t 1 ]; then
    printf '\nこれから行うこと\n'
    printf '  必要なソフト、通知連携、この Mac で動く AI、自動実行、復旧ツールを準備します。\n'
    printf '  指定した省略オプションは維持します。連携方式: %s\n' "$RUNTIME_MODE"
    printf '  初回はインターネット接続が必要です。\n'
    if [ "$SKIP_LLM" -eq 0 ] && [ "$OS" = Darwin ] && [ ! -f "$MODEL_FILE" ]; then
        printf '  AI の取得は約 5.7 GB で時間がかかります。\n'
    fi
    printf '  準備後に、MCS のログイン情報と通知先を画面に沿って設定します。\n'
    printf '  1) この内容で導入する（推奨）\n  2) 今は中止する\n'
    choose '選択 [1・推奨 / 2 / 0・中止]: '
    [ "$_answer" = 1 ] || { printf '導入を中止しました。\n'; exit 130; }
fi

# All read-only guards have passed. Hold the lock before any software or
# settings change; creating the lock alone is not an existing opt-out record.
_prechecks_locked="$_install_lock_held"
acquire_install_lock
if [ "$_prechecks_locked" -eq 0 ]; then
    if [ "$CONFIG_ABSENT" -eq 1 ]; then
        if [ -e "$MCS_ROOT/config.json" ] || [ -L "$MCS_ROOT/config.json" ]; then
            die "導入前の確認中に設定が作成されました。設定を保持して中止したため、内容を確認して再実行してください"
        fi
    else
        _current_mode="$(read_install_config)" \
            || die "導入前の確認中に設定を読み取れなくなりました。設定を保持して中止しました"
        [ "${_current_mode#*:}" = "$MODE_DETAIL" ] \
            || die "導入前の確認中に設定が変更されました。設定を保持して中止したため、内容を確認して再実行してください"
    fi
fi
_fresh_marker="$MCS_DATA/.provider-bootstrap-fresh.json"
_fresh_authorized=0
_legacy_uninitialized=0
if [ -e "$_fresh_marker" ] || [ -L "$_fresh_marker" ]; then
    die "初回導入が中断された記録があります。設定と記録を保持し、担当者に確認してください"
fi
_fresh_complete="$MCS_DATA/.provider-bootstrap-complete.json"
if [ -e "$_fresh_complete" ] || [ -L "$_fresh_complete" ]; then
    _complete_py="${MODE_PY:-}"
    [ -n "$_complete_py" ] || _complete_py="$(command -v python3 || true)"
    [ -n "$_complete_py" ] || die "初回導入の完了記録を確認できません。記録を保持してください"
    "$_complete_py" -B "$REPO/mcs/ops/install_options.py" fresh-complete "$MCS_ROOT/config.json" \
        "$MCS_DATA/install_options.json" - "$MCS_INSTALL_UPDATE_LOCK_FD" \
        || die "初回導入の完了を確認できません。設定と記録を保持してください"
fi
_split_runtime_token=""
if [ -n "$_bootstrap_runtime_token" ] && { [ "$RUNTIME_EXPLICIT" -eq 1 ] || [ -n "$RECOVERY_OPTION" ]; }; then
    _bootstrap_result=$("$MODE_PY" -B "$REPO/mcs/ops/install_options.py" bootstrap-runtime-apply \
        "$MCS_ROOT/config.json" "$MCS_DATA/install_options.json" "$_bootstrap_runtime_token" \
        "$RUNTIME_EXPLICIT" "$RUNTIME_MODE" "$RECOVERY_OPTION") \
        || die "初回設定が変更されたため停止しました。設定と記録を保持してください"
    IFS=' ' read -r CONFIG_BASE _split_runtime_token <<EOF
$_bootstrap_result
EOF
    [ "$_bootstrap_result" = "$CONFIG_BASE $_split_runtime_token" ] || die "初回設定の結果を確認できません"
    case "$_split_runtime_token" in split:*) ;; *) die "初回設定の結果を確認できません" ;; esac
elif [ "$CONFIG_ABSENT" -eq 0 ] && { [ "$RUNTIME_EXPLICIT" -eq 1 ] || [ -n "$RECOVERY_OPTION" ]; }; then
    _guard_py="${MODE_PY:-}"
    [ -n "$_guard_py" ] || _guard_py="$(command -v python3 || true)"
    [ -n "$_guard_py" ] || die "設定の変更可否を確認できません。既存設定を保持してください"
    _runtime_decision=$("$_guard_py" -B "$REPO/mcs/ops/install_options.py" runtime-edit-decision "$MCS_ROOT/config.json" \
        "$MCS_DATA/install_options.json" - "$CONFIG_BASE" "$RUNTIME_EXPLICIT" "$RUNTIME_MODE" "$RECOVERY_OPTION") \
        || die "提供元設定のある環境では接続方法をここで変更できません。設定を保持してください"
    case "$_runtime_decision" in
        legacy) ;;
        split:*) _split_runtime_token="$_runtime_decision" ;;
        *) die "設定の変更可否を確認できません。既存設定を保持してください" ;;
    esac
fi
verify_split_runtime() {
    [ -n "$_split_runtime_token" ] || return 0
    "$MCS_PY" -B "$REPO/mcs/ops/install_options.py" runtime-noop-verify "$MCS_ROOT/config.json" \
        "$MCS_DATA/install_options.json" "$_split_runtime_token" "$CONFIG_BASE" "$RUNTIME_EXPLICIT" "$RUNTIME_MODE" "$RECOVERY_OPTION" \
        || die "提供元設定が変更されたため停止しました。設定と記録を保持して確認してください"
}
_verify_root_alias || die "設定フォルダーのリンクが変更されました。記録を保持してください"
if [ -n "$_fresh_data_info" ] && [ "$CONFIG_ABSENT" -eq 1 ]; then
    # Refuse known prior state before writing the hold. The clone itself is
    # permitted; these paths indicate prior runtime credentials or data.
    for _prior in .env chrome-profile token_cache.json backups exports mcs-exports .install-options-migration.pending; do
        if [ -e "$MCS_ROOT/$_prior" ] || [ -L "$MCS_ROOT/$_prior" ]; then
            die "以前の設定またはデータがあるため初回導入できません。更新・復旧の手順を確認してください"
        fi
    done
    for _prior in "$MCS_ROOT"/.env.* "$MCS_ROOT"/.env-*; do
        case "$_prior" in "$MCS_ROOT/.env.example"|"$MCS_ROOT/.env.template") continue ;; esac
        if [ -e "$_prior" ] || [ -L "$_prior" ]; then
            die "以前の設定があるため初回導入できません。更新・復旧の手順を確認してください"
        fi
    done
    _fresh_root_info="$(_lock_info "$MCS_ROOT")" || die "初回導入の保存先を確認できません"
    IFS=' ' read -r _fr_dev _fr_ino _fr_uid _fr_mode _fr_links <<EOF
$_fresh_root_info
EOF
    IFS=' ' read -r _fd_dev _fd_ino _fd_uid _fd_mode _fd_links <<EOF
$_fresh_data_info
EOF
    for _number in "$_fr_dev" "$_fr_ino" "$_fr_uid" "$_fr_mode" "$_fr_links" "$_fd_dev" "$_fd_ino" "$_fd_uid" "$_fd_mode" "$_fd_links"; do
        case "$_number" in ''|*[!0-9]*) die "初回導入の保存先を確認できません" ;; esac
    done
    if ! { [ "$_fr_uid" = "$_lock_uid" ] && [ "$_fd_uid" = "$_lock_uid" ] && [ "$_fd_mode" = 700 ]; }; then
        die "初回導入の保存先を確認できません"
    fi
    (set -C; printf '{"version":1,"root":[%s,%s,%s,%s],"data":[%s,%s,%s,%s],"config":null}\n' \
        "$_fr_dev" "$_fr_ino" "$_fr_uid" "$((0$_fr_mode))" \
        "$_fd_dev" "$_fd_ino" "$_fd_uid" "$((0$_fd_mode))" > "$_fresh_marker") \
        || die "初回導入の記録を保存できません。データを保持してください"
    _fresh_authorized=1
elif [ "$CONFIG_ABSENT" -eq 1 ]; then
    _legacy_py="${MODE_PY:-}"
    [ -n "$_legacy_py" ] || _legacy_py="$(command -v python3 || true)"
    [ -n "$_legacy_py" ] || die "既存データの確認に Python が必要です。記録を保持してください"
    "$_legacy_py" -B "$REPO/mcs/ops/install_options.py" require-uninitialized-legacy "$MCS_ROOT/config.json" \
        "$MCS_DATA/install_options.json" - "$MCS_INSTALL_UPDATE_LOCK_FD" \
        || die "既存データの設定状態を確認できません。記録を保持してください"
    _legacy_uninitialized=1
fi

# Inspect the opt-out record before software, service or config changes.
# The empty state needs no Python until the runtime has been installed.
set --
[ "$SKIP_BREW" -eq 0 ] || set -- "$@" --no-brew
[ "$SKIP_LLM" -eq 0 ] || set -- "$@" --no-llm
[ "$SKIP_PLUGIN" -eq 0 ] || set -- "$@" --no-plugin
[ "$SKIP_RECOVERY" -eq 0 ] || set -- "$@" --no-recovery
OPTIONS_BASE=empty
OPTIONS_ACTION=inspect
[ -z "${MCS_UPDATE_REINSTALL:-}" ] || OPTIONS_ACTION=inspect-update
if { [ -e "$MCS_ROOT/config.json" ] || [ -L "$MCS_ROOT/config.json" ] || [ -e "$MCS_DATA/install_options.json" ] || [ -L "$MCS_DATA/install_options.json" ] || [ -e "$MCS_ROOT/.install-options-migration.pending" ] || [ -L "$MCS_ROOT/.install-options-migration.pending" ] || [ -e "$MCS_DATA/.provider-settings-pending.json" ] || [ -L "$MCS_DATA/.provider-settings-pending.json" ] || [ -e "$MCS_DATA/.provider-settings-authority.json" ] || [ -L "$MCS_DATA/.provider-settings-authority.json" ] || [ -e "$MCS_DATA/provider_settings" ] || [ -L "$MCS_DATA/provider_settings" ]; }; then
    OPTIONS_PY="${MODE_PY:-}"
    [ -n "$OPTIONS_PY" ] || OPTIONS_PY="$(command -v python3 || true)"
    [ -n "$OPTIONS_PY" ] || die "以前の導入設定の確認には Python が必要です。変更は行っていません"
    OPTIONS_BASE="$("$OPTIONS_PY" "$REPO/mcs/ops/install_options.py" "$OPTIONS_ACTION" "$MCS_ROOT/config.json" "$MCS_DATA/install_options.json" - "$@")" \
        || die "導入設定を変更せず中止しました。旧記録と設定を確認してください"
fi
if [ -n "${MCS_UPDATE_REINSTALL:-}" ] && [ -n "${MCS_UPDATE_OPTIONS_BASE:-}" ]; then
    case "$OPTIONS_BASE" in migrate:*|skip:*) _options_current="${OPTIONS_BASE#*:}" ;; *) _options_current="$OPTIONS_BASE" ;; esac
    [ "$_options_current" = "$MCS_UPDATE_OPTIONS_BASE" ] \
        || die "導入設定が変更されたため再導入を開始しません。現在の設定を確認してください"
fi

# every non-zero exit from here on is a stopped install, not a usage error
trap 'if [ "$?" -ne 0 ]; then
    if [ -e "$MCS_DATA/.provider-bootstrap-fresh.json" ] || [ -L "$MCS_DATA/.provider-bootstrap-fresh.json" ] \
            || [ -e "$MCS_DATA/.provider-settings-pending.json" ] || [ -L "$MCS_DATA/.provider-settings-pending.json" ]; then
        printf "%s\n" "エラー: 導入は保留中です。設定と記録を保持し、担当者に確認してください。" >&2
    else
        printf "%s\n" "エラー: 導入を中断しました。失敗した段階の原因を確認してください。" >&2
    fi
fi' EXIT

# ---------------------------------------------------------------- 1. brew
say "【1/6】基本ソフトの準備（Homebrew）"
if [ "$SKIP_BREW" -eq 1 ]; then
    skip "--no-brew のため省略します。git・Python・uv・llama.cpp・Chrome は別途導入済みである必要があります"
    sum "1/6 基本ソフト:     省略（--no-brew）"
elif ! command -v brew >/dev/null 2>&1; then
    die "Homebrew が見つかりません。先に次のコマンドで導入してください:
  $BREW_INSTALL
その後 install.sh を再実行してください（基本ソフトを別途管理する場合は --no-brew）"
else
    _new=""
    for pkg in git python@3.14 uv llama.cpp; do
        if brew list --versions "$pkg" >/dev/null 2>&1; then
            skip "$pkg は導入済みです"
        else
            printf '  導入中: %s\n' "$pkg"
            (umask 022; brew install "$pkg")
            _new="$_new $pkg"
        fi
    done
    if [ "$OS" != Darwin ]; then
        skip "macOS ではないため Chrome の導入を省略します。Chrome／Chromium を別途導入してください"
    elif brew list --cask --versions google-chrome >/dev/null 2>&1 \
            || [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
        skip "Google Chrome は導入済みです"
    else
        printf '  導入中: %s\n' "google-chrome (cask)"
        (umask 022; brew install --cask google-chrome)
        _new="$_new google-chrome"
    fi
    if [ -n "$_new" ]; then sum "1/6 基本ソフト:     導入済み:$_new"
    else sum "1/6 基本ソフト:     すべて導入済みです"; fi
fi

# ----------------------------------------------------------- 2. hermes
LOCKED_DEPENDENCIES=0
if [ -e "$REPO/deployment/sdc-release-lock.json" ] || [ -L "$REPO/deployment/sdc-release-lock.json" ]; then
    LOCKED_DEPENDENCIES=1
    LOCK_CHECK_PY="$(find_py)" || die "固定依存の確認には Python 3.14 が必要です。既存環境は保持しました"
    locked_dependencies() {
        "$1" -I -S -c 'import sys; sys.path[:0] = [sys.argv[1], sys.argv[1]+"/mcs"]; import _mcs_path; from release_dependency_lock import main; raise SystemExit(main(sys.argv[2:]))' \
            "$REPO" --repo "$REPO" --python "$VENV_PY" --venv "$VENV" \
            --mode "$RUNTIME_MODE" --host-source "$HERMES_DIR" ${2:+"$2"}
    }
    locked_dependencies "$LOCK_CHECK_PY" --check-only \
        || die "固定依存の入力を確認できません。既存 SDK と実行環境は保持しました"
fi
if [ "$RUNTIME_MODE" = standalone ]; then
    say "【2/6】MCS 単独接続の実行環境"
    mkdir -p "$RUNTIME_HOME"
    if ! py_ok "$VENV_PY" 14 15; then
        PYBIN="$(find_py)" || die "Python 3.14 が必要です。brew install python@3.14 で導入してください"
        preserve_old_venv
        if command -v uv >/dev/null 2>&1; then
            uv venv --python "$PYBIN" "$VENV"
        else
            "$PYBIN" -m venv "$VENV"
        fi
        : > "$PIP_MARK"
    fi
    REQUIREMENTS="$REPO/deployment/requirements-standalone.txt"
    [ -f "$REQUIREMENTS" ] || die "独立接続の依存一覧がありません。deployment/requirements-standalone.txt を確認してください"
    if [ "$LOCKED_DEPENDENCIES" -eq 1 ]; then
        locked_dependencies "$VENV_PY" || die "固定依存の導入が未完了です。記録を削除せず確認してください"
        rm -f "$PIP_MARK"
    elif [ -f "$PIP_MARK" ] || ! cmp -s "$REQUIREMENTS" "$VENV/.mcs-connector-requirements"; then
        : > "$PIP_MARK"
        if command -v uv >/dev/null 2>&1; then
            uv pip install --python "$VENV_PY" -r "$REQUIREMENTS"
        else
            "$VENV_PY" -m pip install -r "$REQUIREMENTS"
        fi
        cp "$REQUIREMENTS" "$VENV/.mcs-connector-requirements.tmp"
        mv -f "$VENV/.mcs-connector-requirements.tmp" "$VENV/.mcs-connector-requirements"
        rm -f "$PIP_MARK"
    fi
    py_ok "$VENV_PY" 14 15 || die "導入後の独立接続用 Python を実行できません。上のエラーを確認してください"
    MCS_PY="$VENV_PY"
    sum "2/6 実行環境:  独立した Python 環境と公式接続 SDK を導入"
else
say "【2/6】Hermes の実行環境"
command -v uv >/dev/null 2>&1 \
    || die "Hermes の安全な依存設定を使うため uv が必要です。brew install uv を実行し、install.sh を再実行してください"
mkdir -p "$HERMES_HOME"
# checkout: a fresh clone is bracketed by CLONE_MARK, so an interrupted
# clone/checkout is redone on re-run instead of being accepted unpinned.
# An existing checkout without the marker is someone's — never rewritten.
if [ -f "$CLONE_MARK" ] || [ ! -e "$HERMES_DIR" ]; then
    command -v git >/dev/null 2>&1 \
        || die "Hermes の導入には git が必要です。brew install git または xcode-select --install を実行後、install.sh を再実行してください"
    : > "$CLONE_MARK"
    rm -rf "$HERMES_DIR"   # only ever our own interrupted clone (marker)
    printf '  ソース取得: %s @%s → %s\n' "$HERMES_REPO" "$HERMES_PIN" "$HERMES_DIR"
    if ! git clone --filter=blob:none "$HERMES_REPO" "$HERMES_DIR"; then
        rm -rf "$HERMES_DIR"
        git clone "$HERMES_REPO" "$HERMES_DIR" \
            || die "$HERMES_REPO の取得に失敗しました。github.com への接続を確認し install.sh を再実行してください"
    fi
    if ! git -C "$HERMES_DIR" checkout -q --detach "$HERMES_PIN"; then
        # Full clones may omit commits reachable only through non-branch refs.
        if ! git -C "$HERMES_DIR" fetch -q origin "$HERMES_PIN" \
            || ! git -C "$HERMES_DIR" checkout -q --detach "$HERMES_PIN"; then
            die "$HERMES_REPO の固定版 $HERMES_PIN を取得できません。通信と指定版の公開状況を確認してください。Python 環境の作成は開始していません"
        fi
    fi
fi
[ -e "$HERMES_DIR/.git" ] \
    || die "$HERMES_DIR は存在しますが Git で取得したフォルダではありません。退避（mv $(shell_quote "$HERMES_DIR") $(shell_quote "$HERMES_DIR.bak")）後、install.sh を再実行してください"
HEAD_SHA="$(git -C "$HERMES_DIR" rev-parse HEAD)" \
    || die "$HERMES_DIR の現在の版（HEAD）を読み取れません。修復または退避して install.sh を再実行してください"
_runtime_backup=""
if [ -n "${MCS_UPDATE_RUN_LOCK_FD:-}" ] || [ -n "${MCS_UPDATE_RUNTIME_BACKUP:-}" ]; then
    _checkout_flags="$(git -C "$HERMES_DIR" ls-files -v)" \
        || die "Python 移行: $HERMES_DIR の追跡設定を確認できません。既存ソースと環境は保持しました"
    if [ -n "$_checkout_flags" ] && printf '%s\n' "$_checkout_flags" | grep -qv '^H '; then
        die "Python 移行: $HERMES_DIR に変更の確認を省略する Git 設定があります。設定と手編集を確認してください。既存ソースと環境は保持しました"
    fi
fi
if [ -f "$CLONE_MARK" ]; then
    [ "$HEAD_SHA" = "$HERMES_PIN" ] \
        || die "取得した版 $HEAD_SHA は指定版 $HERMES_PIN と一致しません。install.sh を再実行してください"
    rm -f "$CLONE_MARK"
    ok "Hermes の固定版 $HERMES_PIN を取得しました"
    _ck="固定版 $HERMES_PIN を取得"
elif [ -n "${MCS_UPDATE_KEEP_HERMES_CHECKOUT:-}" ] \
        && { [ -n "${MCS_UPDATE_RUN_LOCK_FD:-}" ] || [ -n "${MCS_UPDATE_RUNTIME_BACKUP:-}" ]; }; then
    # Operator-confirmed (mcs_upgrade --keep-hermes-checkout): keep this exact,
    # clean checkout that already declares Python 3.14 support; never move it.
    [ "$HEAD_SHA" = "$MCS_UPDATE_KEEP_HERMES_CHECKOUT" ] \
        || die "Python 移行: $HERMES_DIR の版 $HEAD_SHA が確認済みの $MCS_UPDATE_KEEP_HERMES_CHECKOUT と一致しません。既存ソースと環境は保持しました"
    _checkout_status="$(git -C "$HERMES_DIR" status --porcelain --untracked-files=no)" \
        || die "Python 移行: $HERMES_DIR の変更状態を確認できません。既存ソースと環境は保持しました"
    [ -z "$_checkout_status" ] \
        || die "Python 移行: $HERMES_DIR にコミットされていない変更があります。既存ソースと環境は保持しました"
    hermes_admits_314 \
        || die "Python 移行: $HERMES_DIR は Python 3.14 対応を宣言していません。既存ソースと環境は保持しました"
    migration_path >/dev/null \
        || die "Python 移行のロックまたはバックアップが不正です。既存ソースと環境は保持しました"
    ok "Python 3.14 への移行に、確認済みの Hermes $HEAD_SHA を保持します"
    _ck="確認済みの版 $HEAD_SHA を保持"
elif [ -n "${MCS_UPDATE_RUN_LOCK_FD:-}" ] || [ -n "${MCS_UPDATE_RUNTIME_BACKUP:-}" ]; then
    # Re-check even at the target pin: a crash may precede the venv move,
    # and the checkout may have been edited before this retry.
    _branch_status=0
    git -C "$HERMES_DIR" symbolic-ref -q HEAD >/dev/null 2>&1 || _branch_status=$?
    case "$_branch_status" in 0|1) ;; *)
        die "Python 移行: $HERMES_DIR のブランチ状態を確認できません。既存ソースと環境は保持しました" ;;
    esac
    _checkout_status="$(git -C "$HERMES_DIR" status --porcelain --untracked-files=no)" \
        || die "Python 移行: $HERMES_DIR の変更状態を確認できません。既存ソースと環境は保持しました"
    if [ "$_branch_status" -eq 0 ] || [ -n "$_checkout_status" ]; then
        die "Python 移行: $HERMES_DIR はブランチ上にあるか未コミットの変更があります。既存ソースと環境は保持しました"
    fi
    _runtime_backup="$(migration_path)" \
        || die "Python 移行のロックまたはバックアップが不正です。既存ソースと環境は保持しました"
    if [ "$HEAD_SHA" = "$HERMES_PIN" ]; then
        skip "Hermes は固定版 $HERMES_PIN を取得済みです"
        _ck="取得済みの固定版を保持"
    else
        # Update only a recognized, clean, detached old pin. Its complete
        # dependency environment is backed up even if Python is already 3.14.
        case " $HERMES_PREVIOUS_PINS " in *" $HEAD_SHA "*) ;; *)
            die "Python 移行: $HERMES_DIR は未検証の版 $HEAD_SHA です。$HERMES_PIN への変更を確認・実施してから再実行してください。既存環境は保持しました" ;;
        esac
        migration_backup >/dev/null \
            || die "Python 移行のロックまたはバックアップが不正です。既存ソースと環境は保持しました"
        if ! git -C "$HERMES_DIR" fetch -q "$HERMES_REPO" "$HERMES_PIN" \
            || ! git -C "$HERMES_DIR" checkout -q --detach "$HERMES_PIN"; then
            die "Python 移行: $HERMES_DIR を $HERMES_PIN に変更できません。既存環境は保持しました"
        fi
        [ "$(git -C "$HERMES_DIR" rev-parse HEAD)" = "$HERMES_PIN" ] \
            || die "Python 移行: 変更後も $HERMES_DIR が指定版 $HERMES_PIN と一致しません"
        ok "Hermes を $HEAD_SHA から $HERMES_PIN へ変更しました"
        _ck="旧固定版から $HERMES_PIN へ変更"
    fi
elif [ "$HEAD_SHA" = "$HERMES_PIN" ]; then
    skip "Hermes は固定版 $HERMES_PIN を取得済みです"
    _ck="取得済みの固定版を保持"
else
    warn "$HERMES_DIR は $HEAD_SHA です（指定固定版 $HERMES_PIN とは異なります）"
    warn "  取得済みソースを保持します。版の変更は更新手順に従ってください"
    _ck="取得済みの $HEAD_SHA を保持（検証済み固定版とは異なります）"
fi

# venv: converge on re-run — a venv without a usable python is rebuilt,
# and PIP_MARK brackets the package install so a failed one is retried
if { [ -n "$_runtime_backup" ] && [ ! -e "$_runtime_backup" ] && [ ! -L "$_runtime_backup" ] \
        && { [ -e "$VENV" ] || [ -L "$VENV" ]; }; } || ! py_ok "$VENV_PY" 14 15; then
    PYBIN="$(find_py)" \
        || die "Hermes には Python 3.14 が必要です（3.15以降は対象外）。brew install python@3.14 を実行して install.sh を再実行してください"
    if [ -e "$VENV" ]; then
        if py_ok "$VENV_PY" 14 15; then
            warn "$VENV の実行環境を退避して再作成します"
        else
            warn "$VENV の Python 3.14 が使用できないため環境を再作成します"
        fi
    fi
    preserve_old_venv
    uv venv --python "$PYBIN" "$VENV" || die "uv による Python 環境の作成に失敗しました。上のエラーを確認し install.sh を再実行してください"
    : > "$PIP_MARK"
    _venv="Python 環境を作成"
else
    _venv="Python 環境を保持"
fi
if [ "$LOCKED_DEPENDENCIES" -eq 1 ]; then
    locked_dependencies "$VENV_PY" || die "固定依存の導入が未完了です。記録を削除せず確認してください"
    rm -f "$PIP_MARK"
    _venv="$_venv, 固定依存を確認"
elif [ -f "$PIP_MARK" ] || [ ! -x "$VENV_HERMES" ]; then
    : > "$PIP_MARK"
    # Hermes's uv security overrides apply only when its project is selected.
    (cd "$HERMES_DIR" && uv pip install --python "$VENV_PY" -e ".[messaging]") \
        || die "$VENV への Hermes 導入に失敗しました。上のエラー（通信・コンパイラ等）を解消し install.sh を再実行してください。失敗した導入を再試行します"
    rm -f "$PIP_MARK"
    _venv="$_venv, Hermes を導入"
fi
# this interpreter is what stage 5 bakes into every plist/wrapper
py_ok "$VENV_PY" 14 15 \
    || die "$VENV_PY がないか Python 3.14 ではありません。$VENV の内容を確認・退避してから install.sh を再実行してください"
[ -x "$VENV_HERMES" ] \
    || die "導入後も $VENV_HERMES がありません。$VENV の内容を確認・退避してから install.sh を再実行してください"
ok "Hermes 用 Python 環境: $VENV_PY"
MCS_PY="$VENV_PY"
hermes_cli() { (unset PYTHONPATH PYTHONHOME; exec "$VENV_HERMES" "$@"); }

# shim: published by rename, so an existing symlink is replaced rather
# than written through (which would overwrite its target)
mkdir -p "$HERMES_BIN_DIR"
SHIM_BODY="$(printf '#!/usr/bin/env bash\nunset PYTHONPATH\nunset PYTHONHOME\nexec %s "$@"' \
    "$(shell_quote "$VENV_HERMES")")"
_shim=replace
if [ -f "$SHIM" ] && [ ! -L "$SHIM" ]; then
    if [ "$(cat "$SHIM")" = "$SHIM_BODY" ]; then _shim=keep
    elif ! { grep -q 'unset PYTHONHOME' "$SHIM" && grep -qF "$VENV_HERMES" "$SHIM"; }; then
        _shim=foreign
    fi
elif [ -L "$SHIM" ] && [ -e "$SHIM" ] \
        && [ "$(link_target "$SHIM")" != "$(physpath "$VENV_HERMES")" ]; then
    _shim=foreign
elif [ -e "$SHIM" ] && [ ! -L "$SHIM" ]; then
    _shim=foreign
fi
case "$_shim" in
    keep) skip "起動コマンド $SHIM は最新です"; _shimsum="起動コマンドを保持" ;;
    foreign)
        warn "$SHIM は MCS の起動コマンドではないため保持します。hermes コマンドが $VENV_HERMES を実行することを確認してください"
        _shimsum="起動コマンドを作成せず保持（$SHIM は別の用途で使用中）" ;;
    replace)
        printf '%s\n' "$SHIM_BODY" > "$SHIM.tmp.$$"
        chmod 700 "$SHIM.tmp.$$"
        mv -f "$SHIM.tmp.$$" "$SHIM"
        ok "起動コマンド: $SHIM -> $VENV_HERMES"
        _shimsum="起動コマンドを作成" ;;
esac
case ":$PATH:" in
    *":$HERMES_BIN_DIR:"*) ;;
    *) warn "$HERMES_BIN_DIR が PATH にありません。hermes を使えるようシェルの設定で PATH に追加してください" ;;
esac
sum "2/6 Hermes:   $_ck; $_venv; $_shimsum"
fi

# Publish a pinned lifecycle entrypoint for both runtimes. A foreign command
# is never overwritten; an MCS shim from another checkout needs --force-repo.
MCS_BODY="$(printf '#!/bin/sh\n# MCS lifecycle launcher\nunset PYTHONPATH PYTHONHOME\nexport MCS_LIFECYCLE_PINNED=1\nexec %s %s "$@"' \
    "$(shell_quote "$MCS_PY")" "$(shell_quote "$REPO/mcs/ops/mcs_cli.py")")"
case "$(mcs_shim_conflict)" in
    foreign) die "$MCS_SHIM は MCS の起動コマンドではないため変更しません" ;;
    other) [ "$FORCE_REPO" -eq 1 ] || die "$MCS_SHIM は別のフォルダを参照しています。切り替える場合は --force-repo を指定してください" ;;
esac
mkdir -p "$HERMES_BIN_DIR"
printf '%s\n' "$MCS_BODY" > "$MCS_SHIM.tmp.$$"
chmod 700 "$MCS_SHIM.tmp.$$"
publish "$MCS_SHIM.tmp.$$" "$MCS_SHIM" || :
sum "起動コマンド: ${MCS_SHIM}（Python: ${MCS_PY}）"
case ":$PATH:" in
    *":$HERMES_BIN_DIR:"*) ;;
    *) warn "$HERMES_BIN_DIR が PATH にありません。mcs を使えるようシェルの設定で PATH に追加してください" ;;
esac

# Verify the desired target before even the first private config write.
if recovery_wanted; then
    RECOVERY_PYTHON="$(recovery_runtime_ok "$MCS_PY")" \
        || die "指定した復旧用 Python の安全性を確認できません。設定は保存していません"
fi
# Make the private hold durable before an initial config can be published.
if [ "$_fresh_authorized" -eq 1 ]; then
    "$MCS_PY" -B "$REPO/mcs/ops/install_options.py" fresh-sync "$MCS_ROOT/config.json" \
        "$MCS_DATA/install_options.json" - "$MCS_INSTALL_UPDATE_LOCK_FD" \
        || die "初回導入の記録を確認できません。設定と記録を保持してください"
fi
# Create the initial local config, or persist explicitly selected settings.
_verify_root_alias || die "設定フォルダーのリンクが変更されました。記録を保持してください"
CONFIG_CREATION_PROOF=""
if [ -n "$_split_runtime_token" ]; then
    verify_split_runtime
elif [ "$CONFIG_ABSENT" -eq 1 ] || [ "$RUNTIME_EXPLICIT" -eq 1 ] || [ -n "$RECOVERY_OPTION" ]; then
    CONFIG_CREATION_PROOF=$(MCS_INSTALL_LEGACY_UNINITIALIZED="$_legacy_uninitialized" MCS_INSTALL_RESUME_UNCONFIGURED="$RESUME_UNCONFIGURED" MCS_INSTALL_CONFIG_BASE="$CONFIG_BASE" "$MCS_PY" - "$REPO" "$RUNTIME_MODE" "$RECOVERY_OPTION" "$CONFIG_ABSENT" "$RUNTIME_EXPLICIT" "$RECOVERY_PYTHON" <<'PYMODE'
import hashlib, json, os, secrets, stat, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "mcs"))
import _mcs_path
import runtime_inspection
from mcs_util import CONF_PATH, CONFIG_MAX_BYTES, REPO, parse_config
from safe_io import atomic_write, create_exclusive, fdopen_owned

class UnsafeParent(ValueError):
    pass

if sys.argv[4] == "1":
    cfg = {}
    if sys.argv[5] == "1" or sys.argv[3]:
        cfg["runtime_mode"] = sys.argv[2]
    if sys.argv[3]:
        cfg["recovery_python"] = sys.argv[3]
    why = runtime_inspection.configured_recovery_problem(cfg, sys.argv[1], checkout=REPO)
    if why:
        raise SystemExit("復旧用 Python の設定に問題があります: " + why + "。設定は保存していません")
    path = Path(CONF_PATH)
    body = (json.dumps(cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    created = False
    created_identity = None
    winner_proof = None
    try:
        home_fd = os.open(path.parent.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            home_stat = os.fstat(home_fd)
            if home_stat.st_uid != os.getuid() or home_stat.st_mode & 0o022:
                raise UnsafeParent
            try:
                os.mkdir(path.parent.name, 0o700, dir_fd=home_fd)
            except FileExistsError:
                pass
            dir_fd = os.open(path.parent.name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=home_fd)
            try:
                def check_parent():
                    opened = os.fstat(dir_fd)
                    current = os.stat(path.parent.name, dir_fd=home_fd)
                    named = os.stat(path.parent)
                    if opened.st_uid != os.getuid() or opened.st_mode & 0o022:
                        raise UnsafeParent
                    if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
                        raise ValueError

                def check_legacy():
                    if os.environ.get("MCS_INSTALL_LEGACY_UNINITIALIZED") == "1":
                        import install_options
                        install_options.require_uninitialized_legacy(
                            CONF_PATH, str(Path(CONF_PATH).parent / "data" / "install_options.json"),
                            os.environ["MCS_INSTALL_UPDATE_LOCK_FD"], allow_config=True)

                check_parent()
                check_legacy()
                staging = ".alias-" + secrets.token_hex(16) + ".tmp"
                fd = create_exclusive(staging, dir_fd=dir_fd)
                try:
                    with fdopen_owned(fd, "wb") as stream:
                        stream.write(body)
                        stream.flush()
                        os.fsync(stream.fileno())
                        created_info = os.fstat(stream.fileno())
                        created_identity = (created_info.st_dev, created_info.st_ino)
                    check_parent()
                    check_legacy()
                    try:
                        os.link(staging, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                                follow_symlinks=False)
                        created = True
                        os.fsync(dir_fd)
                    except FileExistsError:
                        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=dir_fd)
                        with fdopen_owned(fd, "rb") as stream:
                            opened = os.fstat(stream.fileno())
                            if not stat.S_ISREG(opened.st_mode) or opened.st_size > CONFIG_MAX_BYTES:
                                raise ValueError
                            raw = stream.read(CONFIG_MAX_BYTES + 1)
                            closed = os.fstat(stream.fileno())
                        current = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
                        check_parent()
                        identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink, s.st_size,
                                              s.st_mtime_ns, s.st_ctime_ns)
                        if (len(raw) > CONFIG_MAX_BYTES or not stat.S_ISREG(current.st_mode)
                                or opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600
                                or opened.st_nlink != 1 or identity(opened) != identity(closed)
                                or identity(closed) != identity(current)):
                            raise ValueError
                        winner = parse_config(raw)
                        if (not isinstance(winner, dict) or winner.get("runtime_mode", "hermes") != sys.argv[2]
                                or sys.argv[3] and winner.get("recovery_python") != sys.argv[3]
                                or sys.argv[6] and runtime_inspection.recovery_python(winner) != sys.argv[6]):
                            raise ValueError
                        from mcs_config import validate_config
                        errors, _ = validate_config(winner)
                        if any(not error.startswith("必須の設定項目がありません: ") for error in errors):
                            raise ValueError
                        winner_proof = (identity(current), hashlib.sha256(raw).hexdigest())
                    check_parent()
                    check_legacy()
                finally:
                    os.unlink(staging, dir_fd=dir_fd)
                if created or winner_proof is not None:
                    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
                    with fdopen_owned(fd, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        raw = stream.read(CONFIG_MAX_BYTES + 1)
                        after = os.fstat(stream.fileno())
                    current = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
                    identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink, s.st_size,
                                          s.st_mtime_ns, s.st_ctime_ns)
                    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                            or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                            or created and (before.st_dev, before.st_ino) != created_identity
                            or identity(before) != identity(after) or identity(after) != identity(current)
                            or created and raw != body
                            or not created and (identity(current), hashlib.sha256(raw).hexdigest()) != winner_proof):
                        raise ValueError
                    check_parent()
                    print(json.dumps({"identity": identity(after), "sha256": hashlib.sha256(raw).hexdigest()}))
            finally:
                os.close(dir_fd)
        finally:
            os.close(home_fd)
    except UnsafeParent:
        raise SystemExit("ホームフォルダーまたは ~/.mcs の所有者が本人ではないか、本人以外に書き込みを許可しています。設定の改変を防ぐため作成を中止しました。所有者と権限を管理者に確認してください。既存設定は保持しています") from None
    except (OSError, ValueError, TypeError, RecursionError):
        raise SystemExit("初期設定ファイルの保存先または競合を確認できません。既存設定を保持したため、内容を確認してから再実行してください") from None
    raise SystemExit(0)

import config_edit
publication_started = False
try:
    with config_edit._edit_lock():
        config_edit._require_legacy_settings()
        raw, base = config_edit._snapshot()
        current = os.stat(CONF_PATH, follow_symlinks=False)
        token = ":".join(str(n) for n in base[0]) + ":" + base[1].hex()
        # Preserve the previous installer reader's ownership and size contract.
        if (current.st_uid != os.getuid() or config_edit._identity(current) != base[0]
                or len(raw) > 65536 or token != os.environ.get("MCS_INSTALL_CONFIG_BASE")):
            raise ValueError("config_changed")
        cfg = config_edit._parse(raw)
        if os.environ.get("MCS_INSTALL_RESUME_UNCONFIGURED") == "1" and cfg != {}:
            raise ValueError("config_changed")
        cfg["runtime_mode"] = sys.argv[2]
        if sys.argv[3]:
            cfg["recovery_python"] = sys.argv[3]
        why = runtime_inspection.configured_recovery_problem(cfg, sys.argv[1], checkout=REPO)
        if why:
            raise SystemExit("復旧用 Python の設定に問題があります: " + why + "。設定は保存していません")
        body = json.dumps(cfg, ensure_ascii=False, indent=2, allow_nan=False)
        if len(body.encode("utf-8")) > 65536:
            raise ValueError("config_too_large")
        def write_checked(handle):
            handle.write(body)
            config_edit._require_legacy_settings()
            if config_edit._snapshot()[1] != base:
                raise config_edit.ConfigConflict("config_changed")
        publication_started = True
        atomic_write(CONF_PATH, write_checked, 0o600, makedirs=False)
except config_edit.ConfigConflict:
    raise SystemExit("設定が変更されたか、別の設定保存が進行中です。保存せず中止しました。設定を確認してから再実行してください") from None
except (OSError, ValueError, TypeError, RecursionError):
    if publication_started:
        raise SystemExit("設定の保存結果を確認できません。現在の設定を確認してから再実行してください") from None
    raise SystemExit("設定が変更されたか読み取れません。保存せず中止しました。設定を確認してから再実行してください") from None
PYMODE
) || die "初期設定を確認できません。既存の設定と記録を保持してください"
fi
if [ "$_fresh_authorized" -eq 1 ]; then
    _verify_root_alias || die "設定フォルダーのリンクが変更されました。記録を保持してください"
    [ -n "$CONFIG_CREATION_PROOF" ] \
        || die "初回導入の設定作成を確認できません。設定と記録を保持し、担当者に確認してください"
    MCS_FRESH_RUNTIME_MODE="$RUNTIME_MODE" MCS_FRESH_HERMES_HOME="$HERMES_HOME" MCS_FRESH_HERMES_EXE="$VENV_HERMES" \
        "$MCS_PY" "$REPO/mcs/ops/install_options.py" bootstrap-fresh "$MCS_ROOT/config.json" \
        "$MCS_DATA/install_options.json" "$CONFIG_CREATION_PROOF" "$_fresh_data_info" "$MCS_INSTALL_UPDATE_LOCK_FD" \
        || die "初回導入の安全性を確認できません。設定と保留記録を保持し、担当者に確認してください"
fi
# -------------------------------------------------------- 3. plugin
say "【3/6】Slack／Discord の操作カード用プラグイン"
if [ "$SKIP_PLUGIN" -eq 1 ]; then
    if [ "$RUNTIME_MODE" = standalone ]; then
        skip "MCS 単独接続では使用しません。独立接続機能が操作カードを提供します"
        sum "3/6 プラグイン:   MCS 単独接続では不要"
    else
        skip "--no-plugin のため省略します。Slack／Discord の操作カードを有効にする前に、このオプションなしで再実行してください（LINE WORKS では不要。docs/guides/LINEWORKS.md を参照）"
        sum "3/6 プラグイン:   省略（--no-plugin）"
    fi
else
    mkdir -p "$HERMES_HOME/plugins"
    if [ -e "$PLUGIN_LINK" ] && [ ! -L "$PLUGIN_LINK" ]; then
        die "$PLUGIN_LINK はリンクではありません。内容を確認・退避してから install.sh を再実行してください"
    fi
    if [ -L "$PLUGIN_LINK" ] && [ "$(readlink "$PLUGIN_LINK")" = "$REPO/hermes_plugin" ]; then
        skip "プラグインのリンクは設定済み → $REPO/hermes_plugin"
    else
        ln -sfn "$REPO/hermes_plugin" "$PLUGIN_LINK"
        ok "リンクを作成: $PLUGIN_LINK -> $REPO/hermes_plugin"
    fi
    if hermes_cli config get plugins.enabled 2>/dev/null \
            | grep -q "mcs-discord-commands"; then
        skip "プラグインは有効化済みです"
    elif hermes_cli plugins enable mcs-discord-commands --no-allow-tool-override; then
        ok "プラグインを有効化しました"
    else
        die "プラグインの有効化に失敗しました。Hermes の設定を修復するか、プロファイルの config.yaml の plugins.enabled に mcs-discord-commands を追加して install.sh を再実行してください"
    fi
    sum "3/6 プラグイン:   リンク作成・有効化済み"
fi

# Install the independent helper before either llama or the regular services
# can start using it. This is a prefix import; old open FDs remain sources.
mkdir -p "$MCS_DATA"
"$MCS_PY" -I "$REPO/mcs/ops/run_log_install.py" "$MCS_ROOT" "$REPO" "$RECOVERY_DIR" \
    || die "ログの統合準備を確認できません。旧ログを保持して常駐サービスの登録前に停止しました。内容を確認して再実行してください"
RUN_LOG_HELPER="$("$MCS_PY" -I "$REPO/mcs/ops/run_log_install.py" --helper-path \
    "$MCS_ROOT" "$REPO" "$RECOVERY_DIR" --require-installed)" \
    || die "ログ処理の配置を確認できません。常駐サービスの登録前に停止しました"

# -------------------------------------------------- 4. local LLM server
say "【4/6】この Mac で動かす AI（llama-server、ポート8080）"
if [ "$SKIP_LLM" -eq 1 ]; then
    skip "--no-llm のため省略します。別途 OpenAI 互換サーバーを 127.0.0.1:8080 で動かしてください"
    sum "4/6 ローカルAI:      省略（--no-llm）"
elif [ "$OS" != Darwin ]; then
    skip "AI の常駐設定は macOS 専用です。別途 OpenAI 互換サーバーを 127.0.0.1:8080 で動かしてください"
    sum "4/6 ローカルAI:      macOS 以外のため省略"
else
    SERVING=0
    if llm_serving; then SERVING=1; fi
    if [ -f "$HERMES_LLAMA_PLIST" ]; then
        # hermes manages its own llama.cpp agent — never start a second one
        if loaded ai.hermes.llamacpp; then
            skip "Hermes 管理の AI（ai.hermes.llamacpp）が起動済みです。ポート8080を使用します"
            sum "4/6 ローカルAI:      Hermes 管理の AI が起動済み（ai.hermes.llamacpp）"
        elif [ "$SERVING" -eq 1 ]; then
            skip "ai.hermes.llamacpp は未起動ですがポート8080に応答があるため、既存サーバーを保持します"
            sum "4/6 ローカルAI:      ポート8080の既存サーバーを保持"
        else
            warn "Hermes 管理の $HERMES_LLAMA_PLIST は未起動で、ポート8080の応答もありません。次のコマンドで起動してください:"
            warn "  launchctl bootstrap gui/$(id -u) $(shell_quote "$HERMES_LLAMA_PLIST")"
            sum "4/6 ローカルAI:      注意: ai.hermes.llamacpp は未起動です。上の対処を確認してください"
        fi
    elif [ "$SERVING" -eq 1 ] && [ ! -f "$LLAMA_PLIST" ] && ! loaded ai.mcs.llamaserver; then
        skip "ポート8080の OpenAI 互換サーバーは応答しているため保持します"
        sum "4/6 ローカルAI:      ポート8080の既存サーバーを保持"
    else
        LLAMA_BIN="$(command -v llama-server || true)"
        if [ -z "$LLAMA_BIN" ] && command -v brew >/dev/null 2>&1; then
            LLAMA_BIN="$(brew --prefix)/bin/llama-server"
        fi
        if [ -z "$LLAMA_BIN" ] || [ ! -x "$LLAMA_BIN" ]; then
            die "llama-server が見つかりません。brew install llama.cpp で導入して再実行してください（別サーバーをポート8080で動かす場合は --no-llm）"
        fi
        if [ -f "$MODEL_FILE" ]; then
            _got="$(shasum -a 256 < "$MODEL_FILE" | awk '{print $1}')"
            if [ "$_got" != "$MODEL_SHA256" ]; then
                die "既存モデルの整合性が確認できません（SHA256: ${_got}）。ファイルは保持しました。出典と内容を確認し、固定版のモデルを別途準備して再実行してください（別サーバーを使う場合は --no-llm）"
            fi
            skip "AI モデルは取得済みで、整合性を確認しました"
        else
            printf '  AI モデルを取得中（約5.7 GB、中断後に再開できます）: %s\n' "$MODEL_FILE"
            mkdir -p "$MODEL_DIR"
            # .part is kept on failure so the next run resumes (-C -).
            # A .part that already holds every byte (interrupted between
            # the transfer and the rename) is not re-requested: a range
            # past the end answers 416 and would fail every re-run.
            _have=0; _want=""
            if [ -f "$MODEL_FILE.part" ]; then
                _have="$(wc -c < "$MODEL_FILE.part" | tr -d ' ')"
                _want="$(curl -fsIL --retry 3 "$MODEL_URL" 2>/dev/null \
                    | tr -d '\r' | awk 'tolower($1)=="content-length:"{v=$2} END{print v}')"
            fi
            if [ -n "$_want" ] && [ "$_have" = "$_want" ]; then
                printf '  取得途中のファイルは全容量を取得済みです（%s バイト）\n' "$_have"
            else
                curl -fL --retry 5 --retry-delay 5 -C - --progress-bar \
                        -o "$MODEL_FILE.part" "$MODEL_URL" \
                    || die "モデル取得に失敗しました。install.sh を再実行すると再開します（途中のファイル: $MODEL_FILE.part）"
            fi
            _got="$(shasum -a 256 < "$MODEL_FILE.part" | awk '{print $1}')"
            if [ "$_got" != "$MODEL_SHA256" ]; then
                rm -f "$MODEL_FILE.part"
                die "モデルの整合性が確認できません（SHA256: ${_got}）。取得途中のファイルを削除しました。install.sh を再実行してください"
            fi
            mv -f "$MODEL_FILE.part" "$MODEL_FILE"
            ok "AI モデルを取得しました"
        fi
        mkdir -p "$AGENTS_DIR" "$RUNTIME_HOME/logs"
        sed -e "s|__LLAMA_BIN__|$(xml_sed "$LLAMA_BIN")|g" \
            -e "s|__PYTHON__|$(xml_sed "$MCS_PY")|g" \
            -e "s|__RUN_LOG_HELPER__|$(xml_sed "$RUN_LOG_HELPER")|g" \
            -e "s|__DATA__|$(xml_sed "$MCS_DATA")|g" \
            -e "s|__MODEL__|$(xml_sed "$MODEL_FILE")|g" \
            -e "s|__HERMES_HOME__|$(xml_sed "$RUNTIME_HOME")|g" \
            "$REPO/deployment/launchagents/ai.mcs.llamaserver.plist" \
            > "$LLAMA_PLIST.tmp"
        _changed=0
        if publish "$LLAMA_PLIST.tmp" "$LLAMA_PLIST"; then _changed=1; fi
        if ! loaded ai.mcs.llamaserver; then
            bootstrap_agent ai.mcs.llamaserver "$LLAMA_PLIST" \
                || die "AI サーバーの起動に失敗しました。$LLAMA_PLIST を確認し install.sh を再実行してください"
            ok "AI サーバーの常駐サービスを起動しました"
            sum "4/6 ローカルAI:      AI サーバーを起動"
        elif [ "$_changed" -eq 0 ]; then
            skip "AI サーバーは起動済みで、起動設定にも変更はありません"
            sum "4/6 ローカルAI:      AI サーバーは起動済み"
        elif [ "$SERVING" -eq 1 ]; then
            # never interrupt a serving model mid-request — tell the user
            warn "AI サーバーの起動設定を更新しましたが、稼働中の処理は保持しました。処理が終わってから次を実行して反映してください:"
            warn "  launchctl bootout gui/$(id -u)/ai.mcs.llamaserver; launchctl bootstrap gui/$(id -u) $(shell_quote "$LLAMA_PLIST")"
            sum "4/6 ローカルAI:      起動設定を更新、再起動待ち（上の注意を参照）"
        else
            launchctl bootout "gui/$(id -u)/ai.mcs.llamaserver" 2>/dev/null || true
            bootstrap_agent ai.mcs.llamaserver "$LLAMA_PLIST" \
                || die "AI サーバーの起動に失敗しました。$LLAMA_PLIST を確認し install.sh を再実行してください"
            ok "更新した設定で AI サーバーを再起動しました"
            sum "4/6 ローカルAI:      設定変更に合わせて AI サーバーを再起動"
        fi
    fi
fi

# ------------------------------------------- 5. launchd + hermes cron
_verify_root_alias || die "設定フォルダーのリンクが変更されました。記録を保持してください"
verify_split_runtime
say "【5/6】定期収集などの常駐サービス（${RUNTIME_MODE}）"
# the agents stage 5/6 load log to / watch these — they must exist first
# (new ones are 0700 via umask; existing modes are kept)
mkdir -p "$MCS_ROOT" "$MCS_DATA" "$MCS_DATA/cmd" "$MCS_DATA/cmd_int"
if [ "$SKIP_SERVICES" -eq 1 ]; then
    skip "--no-services のため省略します。後で次のコマンドで登録してください:"
    skip "  $(shell_quote "$MCS_PY") $(shell_quote "$SETUP") services"
    sum "5/6 常駐サービス: 省略（--no-services）"
elif "$MCS_PY" "$SETUP" services; then
    ok "常駐サービスを登録しました"
    sum "5/6 常駐サービス: 登録済み（Python: ${MCS_PY}）"
else
    die "常駐サービスの登録に問題があります。deployment/launchagents/README.md を確認し install.sh を再実行してください"
fi

# --------------------------------- 6. update recovery (independent)
say "【6/6】更新失敗に備える復旧ツールと監視"
if [ "$SKIP_RECOVERY" -eq 1 ]; then
    skip "--no-recovery のため省略します"
    sum "6/6 復旧: 省略（--no-recovery）"
else
    if [ "$OS" = Darwin ]; then
        RECOVERY_PYTHON="$(recovery_runtime_ok "$MCS_PY")" \
            || die "復旧用 Python の安全性が確認できません。段階6は導入していません。別の Python へ自動切替はしていません"
    fi
    mkdir -p "$RECOVERY_DIR"
    RECOVERY_SRC="$REPO/deployment/recovery/mcs_recover.py"
    RECOVERY_DST="$RECOVERY_DIR/mcs_recover.py"
    # preserve the prior generation — on a bad update it may be the only
    # thing that can still run (only when the generation actually changes
    # — a same-release re-run must not overwrite the real previous
    # generation with the current one). Both files are published via
    # tmp + rename so an interrupted install never leaves a truncated
    # tool or .prev behind.
    _rchanged=0
    if ! cmp -s "$RECOVERY_SRC" "$RECOVERY_DST"; then
        if [ -f "$RECOVERY_DST" ]; then
            cp -p "$RECOVERY_DST" "$RECOVERY_DST.prev.tmp"
            mv -f "$RECOVERY_DST.prev.tmp" "$RECOVERY_DST.prev"
        fi
        cp "$RECOVERY_SRC" "$RECOVERY_DST.tmp"
        chmod 700 "$RECOVERY_DST.tmp"
        mv -f "$RECOVERY_DST.tmp" "$RECOVERY_DST"
        _rchanged=1
    fi
    # the tool runs outside the repo — record which checkout it recovers
    printf '%s\n' "$REPO" > "$RECOVERY_DIR/repo_path.tmp"
    if publish "$RECOVERY_DIR/repo_path.tmp" "$RECOVERY_DIR/repo_path"; then _rchanged=1; fi
    ok "復旧ツール: $RECOVERY_DST"

    if [ "$OS" = Darwin ]; then
        mkdir -p "$AGENTS_DIR"
        sed -e "s|__RECOVERY__|$(xml_sed "$RECOVERY_DIR")|g" \
            -e "s|__RUN_LOG_HELPER__|$(xml_sed "$RUN_LOG_HELPER")|g" \
            -e "s|__DATA__|$(xml_sed "$MCS_DATA")|g" \
            -e "s|__RECOVERY_PYTHON__|$(xml_sed "$RECOVERY_PYTHON")|g" \
            "$RECOVERY_TEMPLATE" \
            > "$WATCH_PLIST.tmp"
        if publish "$WATCH_PLIST.tmp" "$WATCH_PLIST"; then _rchanged=1; fi
        if [ "$_rchanged" -eq 0 ] && loaded org.mcs.recovery; then
            skip "復旧監視は起動済みで変更はありません"
            sum "6/6 復旧: 復旧監視は起動済み"
        else
            launchctl bootout "gui/$(id -u)/org.mcs.recovery" 2>/dev/null || true
            bootstrap_agent org.mcs.recovery "$WATCH_PLIST" \
                || die "復旧監視を起動できません。$WATCH_PLIST を確認してください（起動: launchctl bootstrap gui/$(id -u) $(shell_quote "$WATCH_PLIST")）。その後 install.sh を再実行してください"
            ok "復旧監視を起動しました（15分ごと）"
            sum "6/6 復旧: 復旧監視を起動・再起動"
        fi
    else
        skip "復旧監視の自動登録は macOS 専用です。定期実行を別途設定してください"
        sum "6/6 復旧: ツールを導入。macOS 以外のため復旧監視登録を省略"
    fi
fi

# ------------------------------------------------------------ opt-outs
# The updater's plan reports these so a reinstall repeats the same opt-outs
# without asking (mcs_update.INSTALL_ARGS; --no-services is the updater's own).
# Updater runs add migration/repair opt-outs the operator did not choose.
if [ -z "${MCS_UPDATE_REINSTALL:-}" ]; then
    "$MCS_PY" "$REPO/mcs/ops/install_options.py" save "$MCS_ROOT/config.json" "$MCS_DATA/install_options.json" "$OPTIONS_BASE" "$@" \
        || die "ソフトの準備は途中まで反映されています。導入設定を確認してから再実行してください"
elif [ "${OPTIONS_BASE#migrate:}" != "$OPTIONS_BASE" ]; then
    "$MCS_PY" "$REPO/mcs/ops/install_options.py" migrate "$MCS_ROOT/config.json" "$MCS_DATA/install_options.json" "${OPTIONS_BASE#migrate:}" \
        || die "ソフトの準備は途中まで反映されています。導入設定を確認してから再実行してください"
else
    _options_current="${OPTIONS_BASE#skip:}"
    "$MCS_PY" "$REPO/mcs/ops/install_options.py" verify "$MCS_ROOT/config.json" "$MCS_DATA/install_options.json" "$_options_current" \
        || die "ソフトの準備は途中まで反映されています。導入設定を確認してから再実行してください"
fi

# ------------------------------------------------------------ summary
# Software preparation succeeded; a later setup failure has its own guidance.
trap - EXIT
SETUP_STATUS=0
PYQ="$(shell_quote "$MCS_PY")"
SETUPQ="$(shell_quote "$SETUP")"
# `-m` resolves only from the checkout: install.sh may run from elsewhere.
INREPO="cd $(shell_quote "$REPO") &&"
printf '\n============================================================\n'
printf 'ソフトの準備が完了しました。まだ利用開始前の初期設定が必要です。\n'
printf '%s' "$SUMMARY"
printf '============================================================\n'
printf '\n次は MCS のログイン情報と通知先を設定します。\n'
printf 'アカウント情報は、ご自身の端末で入力してください。\n'
printf '準備ができていない項目は初期設定の案内に従って後で設定できます。\n'
printf '初期設定だけでは MCS に接続できません。専用 Chrome の起動・MCS へのログイン、通知先の接続確認まで行ってください。\n'
printf '手順書: docs/guides/EASY_SETUP.md\n'
printf '後で続ける場合のコマンド:\n    %s setup\n' "$(shell_quote "$MCS_SHIM")"
printf '    # 同じ初期設定を開く別のコマンド:\n'
printf '    %s %s init\n' "$PYQ" "$SETUPQ"
if [ -t 0 ] && [ -t 1 ]; then
    printf '\n  1) 続けて初期設定を始める（推奨）\n  2) 後で設定する\n'
    if ! choose '選択 [1・推奨 / 2 / 0・終了]: '; then
        _answer=2
        printf 'ソフトの準備は完了しています。初期設定は後で再開できます。\n'
    fi
    if [ "$_answer" = 1 ]; then
        "$MCS_PY" "$SETUP" init || SETUP_STATUS=$?
        if [ "$SETUP_STATUS" -ne 0 ]; then
            printf '\nソフトの準備は完了していますが、初期設定は完了していません。\n'
            printf '入力内容や表示された問題を確認し、次のコマンドで再開してください:\n    %s setup\n' "$(shell_quote "$MCS_SHIM")"
        fi
    fi
fi
printf '\n設定後の確認コマンド:\n    %s %s check\n' "$PYQ" "$SETUPQ"
if [ "$RUNTIME_MODE" = standalone ]; then
    printf '    %s %s -m mcs_standalone status\n' "$INREPO" "$PYQ"
    printf '\n詳しい説明: docs/guides/STANDALONE.md\n'
fi
cat <<'EOF'

LINE WORKS を選んだ場合は、専用の接続設定も必要です。
詳しい説明: docs/guides/LINEWORKS.md
EOF
for sub in init check; do
    printf '    %s %s -m lineworks_adapter %s\n' "$INREPO" "$PYQ" "$sub"
done
if [ "$RUNTIME_MODE" = standalone ]; then
    printf 'MCS が LINE WORKS の接続を起動します。専用サービスを別に起動しないでください。\n'
else
    printf 'LINE WORKS の接続設定と確認を終えたら、自動起動を登録します:\n'
    printf '    %s %s -m lineworks_adapter service\n' "$INREPO" "$PYQ"
fi
printf '\n自動実行の登録を省略した場合は、初期設定後に次を実行してください。\n'
printf '    %s %s services\n' "$PYQ" "$SETUPQ"
exit "$SETUP_STATUS"
