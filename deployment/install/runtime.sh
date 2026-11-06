# shellcheck shell=sh
# Sourced only by the verified install.sh after preflight and write guards.
: "${_fresh_authorized?}" "${_legacy_uninitialized?}" "${_fresh_data_info?}"
: "${RECOVERY_DIR:?}" "${SETUP:?}"

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

if [ "$WITH_YOMITOKU" -eq 1 ]; then
    "$MCS_PY" "$REPO/scripts/setup_yomitoku.py" --root "$MCS_ROOT" \
        || die "OCR専用環境の導入に失敗しました。既存モデルは保持しています"
    sum "追加OCR: $MCS_ROOT/ocr-venv/bin/yomitoku（利用開始は別途設定）"
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
    # One helper publishes the same captured entry bytes after their complete bundle.
    _rchanged="$("$MCS_PY" "$REPO/deployment/recovery/prepare_recovery.py" "$RECOVERY_SRC" "$RECOVERY_DIR")" \
        || die "復旧ツールの世代を確認できません。既存の復旧ツールを保持して中止しました"
    case "$_rchanged" in 0|1) ;; *) die "復旧ツールの配置結果を確認できません" ;; esac
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
trap '_install_stage_cleanup' EXIT
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
# Explicit installer lifecycle only; ordinary runtime startup never creates an intent.
if [ "$SETUP_STATUS" -eq 0 ]; then
    if [ "$SKIP_SERVICES" -eq 1 ]; then
        skip "Slack専用チャンネルの自動準備は --no-services のため省略します。後で mcs setup で再開できます"
    else
        "$MCS_PY" "$SETUP" slack-channels || SETUP_STATUS=$?
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
