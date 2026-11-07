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
#   --with-yomitoku           非商用OCR専用 Python 3.13 環境とモデルを導入。
#                             OCRの利用開始は attachment_ocr の別途設定が必要です。
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
WITH_YOMITOKU=0
FORCE_REPO=0
ARG_HOME=""
while [ $# -gt 0 ]; do
    case "$1" in
        --mode)       [ $# -ge 2 ] || { printf '%s\n' '--mode の後に hermes または standalone を指定してください' >&2; exit 2; }
                      RUNTIME_MODE="$2"; RUNTIME_EXPLICIT=1; shift
                      case "$RUNTIME_MODE" in hermes|standalone) ;; *) printf '%s\n' '--mode は hermes または standalone を指定してください' >&2; exit 2 ;; esac ;;
        --with-yomitoku) WITH_YOMITOKU=1 ;;
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
# Read-only bootstrap modes do not require checkout companions.
INSTALL_RUNTIME_STAGE="$REPO/deployment/install/runtime.sh"
INSTALL_RUNTIME_SHA256="c9bd834ff3c0ab28180815b2a4f7f47c4a833d80c9c3e6e440742849ad98d9c0"
_install_stage_private_dir=""
# shellcheck disable=SC2317,SC2329  # Invoked by EXIT after the sourced stage exits.
_install_stage_cleanup() {
    if [ -n "$_install_stage_private_dir" ]; then
        rm -rf -- "$_install_stage_private_dir"
    fi
}
if [ "$MODE" = install ]; then
    if [ ! -f "$INSTALL_RUNTIME_STAGE" ] || [ -L "$INSTALL_RUNTIME_STAGE" ]; then
        die "導入処理の補助ファイルがありません。完全な配布版から再実行してください"
    fi
    trap '_install_stage_cleanup' EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    _install_stage_private_dir="$(mktemp -d "${TMPDIR:-/tmp}/mcs-install-runtime.XXXXXXXX")" \
        || die "導入処理を固定する私有フォルダーを作成できません"
    INSTALL_RUNTIME_FIXED="$_install_stage_private_dir/runtime.sh"
    cp "$INSTALL_RUNTIME_STAGE" "$INSTALL_RUNTIME_FIXED" \
        || die "導入処理の補助ファイルを固定できません"
    chmod 400 "$INSTALL_RUNTIME_FIXED" || die "導入処理の私有コピーを保護できません"
    if command -v shasum >/dev/null 2>&1; then
        _stage_hash="$(shasum -a 256 "$INSTALL_RUNTIME_FIXED" | awk '{print $1}')"
    elif command -v sha256sum >/dev/null 2>&1; then
        _stage_hash="$(sha256sum "$INSTALL_RUNTIME_FIXED" | awk '{print $1}')"
    else
        die "導入処理の世代を検証できません。shasum または sha256sum が必要です"
    fi
    [ "$_stage_hash" = "$INSTALL_RUNTIME_SHA256" ] \
        || die "導入処理の世代が一致しません。既存設定を変更せず停止しました"
fi
# END verified installer runtime stage

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
    if [ "$WITH_YOMITOKU" -eq 1 ]; then
        plan_line "追加OCR" "$MCS_ROOT/ocr-venv に Python 3.13・Yomitoku 0.16.1・CPU liteモデルを導入（非商用向け、利用開始は別途設定）"
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
[ "$WITH_YOMITOKU" -eq 0 ] || set -- "$@" --with-yomitoku
OPTIONS_BASE=empty
OPTIONS_ACTION=inspect
[ -z "${MCS_UPDATE_REINSTALL:-}" ] || OPTIONS_ACTION=inspect-update
if { [ -e "$MCS_ROOT/config.json" ] || [ -L "$MCS_ROOT/config.json" ] || [ -e "$MCS_DATA/install_options.json" ] || [ -L "$MCS_DATA/install_options.json" ] || [ -e "$MCS_ROOT/.install-options-migration.pending" ] || [ -L "$MCS_ROOT/.install-options-migration.pending" ] || [ -e "$MCS_DATA/.provider-settings-pending.json" ] || [ -L "$MCS_DATA/.provider-settings-pending.json" ] || [ -e "$MCS_DATA/.provider-settings-authority.json" ] || [ -L "$MCS_DATA/.provider-settings-authority.json" ] || [ -e "$MCS_DATA/provider_settings" ] || [ -L "$MCS_DATA/provider_settings" ]; }; then
    OPTIONS_PY="${MODE_PY:-}"
    [ -n "$OPTIONS_PY" ] || OPTIONS_PY="$(command -v python3 || true)"
    [ -n "$OPTIONS_PY" ] || die "以前の導入設定の確認には Python が必要です。変更は行っていません"
    _ocr_previous=$("$OPTIONS_PY" -I - "$REPO/mcs" "$MCS_ROOT/config.json" "$MCS_DATA/install_options.json" <<'PYOCR'
import sys
sys.path.insert(0, sys.argv[1])
import _mcs_path
import install_options
install_options.inspect(sys.argv[2], sys.argv[3])
print(int('--with-yomitoku' in (install_options.selection(sys.argv[2], sys.argv[3]) or [])))
PYOCR
    ) || die "以前のOCR導入設定を確認できません。変更は行っていません"
    if [ "$_ocr_previous" = 1 ] && [ "$WITH_YOMITOKU" -eq 0 ]; then
        WITH_YOMITOKU=1
        set -- "$@" --with-yomitoku
    fi
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
fi
_install_stage_cleanup' EXIT

# shellcheck source=deployment/install/runtime.sh
. "$INSTALL_RUNTIME_FIXED"
