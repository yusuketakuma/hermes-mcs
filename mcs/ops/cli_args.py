"""MCSコマンドの日本語ヘルプと引数エラーを共通化する。"""
import argparse
import sys


class JapaneseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._positionals.title = "操作"
        self._optionals.title = "オプション"
        for action in self._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = "この説明を表示して終了する"

    def format_help(self):
        return super().format_help().replace("usage: ", "使い方: ", 1)

    def format_usage(self):
        return super().format_usage().replace("usage: ", "使い方: ", 1)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"入力内容を確認してください。--help で指定方法を確認できます。\n詳細: {message}\n")
