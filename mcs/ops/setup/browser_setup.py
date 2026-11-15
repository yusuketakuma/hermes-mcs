"""専用Chromeを開いて本人がMCSへログインし、収集に使うセッションを確認します。"""
from __future__ import annotations

import os
import sys

import mcs_util

# what each adapter state means for the person at the terminal
_STOP = {
    "manual_required:multi_user_host": (
        "このMacには他にログインできるユーザーがいるため、専用Chromeを開きませんでした。",
        "他のユーザーから接続できない状態にする必要があります。施設の管理担当者に相談してください。"
        "ユーザーの削除や安全確認の解除で回避しないでください。"),
    "failed": (
        "専用Chromeを起動できないか、応答がありませんでした。",
        "Google Chromeが /Applications に入っているか確認し、同じ専用Chromeが固まっている場合は"
        "そのウィンドウを閉じてから、もう一度 mcs setup browser を実行してください。"),
}


def run(*, adapter=None, ask=input, platform: str = sys.platform,
        chrome_bin: str = mcs_util.CHROME_BIN, profile_dir: str = mcs_util.CHROME_PROFILE) -> int:
    """Guide the first sign-in: open the dedicated Chrome, wait for the user, confirm the session.

    No credential is filled: the user signs in by hand. Only the existing
    adapter path opens Chrome (refusing on a multi-user Mac) and confirms
    the session with one read-only API check, as auto_login does."""
    print("\n【MCSへのログイン】収集に使う専用のChromeを開き、MCSへのログインを確認します。"
          "\n  普段のChromeとは別のウィンドウです。このChromeは閉じずに、そのまま使い続けます。")
    if platform != "darwin":
        print("  この操作はmacOSでのみ使えます。")
        return 1
    if not os.path.exists(chrome_bin):
        print(f"  Google Chromeが見つかりません: {chrome_bin}"
              "\n  対処: Google Chromeを入れてから、もう一度 mcs setup browser を実行してください。")
        return 1
    if adapter is None:
        from mcs_adapter import MCSAdapter
        adapter = MCSAdapter(token_cache=mcs_util.CACHE)
    os.makedirs(profile_dir, mode=0o700, exist_ok=True)
    os.chmod(profile_dir, 0o700)
    while True:
        state = adapter.open_browser_session(profile_dir, chrome_bin)
        if state == "ok":
            print("  完了: MCSへログインした状態を確認しました。定期収集はこのログインを使います。"
                  "\n  次にすること: 10分ほど待ってから mcs doctor を実行し、収集と通知が届いたかを確認します。")
            return 0
        if state != "login_needed":
            reason, action = _STOP.get(state, _STOP["failed"])
            print(f"  停止: {reason}\n  対処: {action}")
            return 1
        print("\n  開いた専用ChromeでMCSのログイン画面に、ログインIDとパスワードを入力してログインしてください。"
              "\n  パスワードはChromeの画面にだけ入力し、ここやチャットには入力しません。")
        try:
            answer = ask("  ログインできたら Enter を押してください（q で中止）: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "q"
        if answer in ("q", "quit", "中止"):
            print("  中止しました。ログインが確認できるまで収集は始まりません。"
                  "\n  再開するコマンド: mcs setup browser")
            return 130
        print("  ログインを確認しています…")
