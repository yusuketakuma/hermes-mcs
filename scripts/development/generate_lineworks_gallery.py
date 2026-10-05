#!/usr/bin/env python3
"""Generate source-based, fully synthetic LINE WORKS illustrations for README."""

import argparse
import hashlib
from html import escape
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zlib

from generate_slack_gallery import (BODY, FONT, HEADING, INK, MUTED, POSTS, RULE,
                                    SENDER, Screen, verify_png)


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/screenshots/lineworks-gallery"
GREEN = "#087f5b"
BLUE = "#176cc2"


class TalkScreen(Screen):
    def __init__(self, number, title, subtitle):
        self.height = 1100
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="1100" '
            f'viewBox="0 0 1100 1100" font-family="{FONT}">',
            f"<title>{escape(title)} — 架空のLINE WORKS表示例</title>",
            "<desc>独自アダプターの連続投稿と本人との1:1入力・確認に基づく説明図。"
            "実画面のキャプチャではなく、実患者・実投稿・匿名化データは使用していません。</desc>",
        ]
        self.rect(0, 0, 1100, 1100, "#f6f4ef")
        self.text(28, 34, f"HERMES-MCS  /  LINE WORKS  /  {number:02}", 14, GREEN, True)
        self.text(28, 78, title, 31, INK, True)
        self.text(28, 112, subtitle, 17, MUTED)
        self.text(28, 1000, "抽出は候補。原文を確認し、人が判断・確定します。", 19, GREEN, True)
        self.text(28, 1038, "架空の名前・投稿を使った説明図 / 実画面のキャプチャではありません", 16, MUTED)
        self.text(28, 1070, "実データ不使用 / 画像内のボタンは操作できません", 15, MUTED)

    def panel(self, x, title, subtitle):
        self.rect(x, 146, 508, 804, "#edf3f5", 12, "#d1dce1")
        self.rect(x, 146, 508, 78, "#fff", 12)
        self.text(x + 20, 177, title, 21, INK, True)
        self.text(x + 20, 206, subtitle, 15, MUTED)
        self.line(x, 225, x + 508, 225, "#d1dce1")

    def bot(self, x, y):
        self.rect(x, y, 30, 30, GREEN, 8)
        self.text(x + 7, y + 22, "M", 18, "#fff", True)
        self.text(x + 42, y + 21, "MCS Bot", 16, MUTED, True)

    def bubble(self, x, y, value, user=False, height=64):
        self.rect(x, y, 422, height, "#dbf3e3" if user else "#fff", 9, "#d5e1e5")
        self.paragraph(x + 15, y + 28, value, width=390, size=17)

    def block(self, x, y, lines, size=15):
        """A bot text post sized to its (value, color, bold) lines; returns its bottom."""
        mark, end = len(self.parts), y + 30
        for value, color, bold in lines:
            end = self.paragraph(x + 16, end, value, 390, size, color, bold)
        self.rect(x, y, 422, end - y - 10, "#fff", 9, "#d5e1e5")
        self.parts.insert(mark, self.parts.pop())
        return end - 10

    def template(self, x, y, labels):
        for label in labels:
            self.rect(x, y, 422, 33, "#fff", 0, "#d5e1e5")
            self.parts.append(f'<text x="{x + 211}" y="{y + 23}" font-size="16" '
                              f'fill="{BLUE}" text-anchor="middle">{escape(label)}</text>')
            y += 33


def delivery():
    s = TalkScreen(1, "同じトークルームで、要約・原文・添付を確認。",
                   "カードに続いて原文と添付を連続投稿。入力・確認は本人との1:1トークへ。")
    s.panel(24, "① 共有トークルーム", "MCS Botからの新着通知カード")
    s.panel(568, "②・③ 同じトークルームの続き", "スレッドではなく、通常のトークとして届きます")
    s.bot(44, 245)
    sender, bullets = POSTS[0]
    summary = [("📋 要約", GREEN, True), *(("・" + b, INK, False) for b in bullets)]
    y = s.block(66, 286, [(HEADING, INK, True), ("1投稿 · 📎 1 · @自分宛て", MUTED, False),
                          (RULE, MUTED, False), (sender, INK, True), *summary,
                          (RULE, MUTED, False), ("👤 担当: 田中 · ✅ 確認: 田中", INK, False),
                          ("📝 タスク 1件 · スタンプ 👀2 🙆1 · 自分 1投稿", INK, False)])
    s.template(66, y, ["確認する", "担当する", "タスク作成", "MCSで開く", "その他の操作"])
    s.bot(588, 245)
    y = s.block(610, 286, [(f"↳ 山田 花子 様 · 10-01 09:40 {SENDER}", INK, True), *summary,
                           (RULE, MUTED, False), (BODY, INK, False)])
    s.bot(588, y + 10)
    s.rect(610, y + 51, 422, 100, "#fff", 9, "#d5e1e5")
    s.rect(626, y + 67, 390, 68, "#f0f5f1", 8)
    s.text(646, y + 94, "添付ファイル", 18, INK, True)
    s.text(646, y + 124, "服薬カレンダー.jpg", 16, MUTED)
    return s.finish()


def task_confirm():
    s = TalkScreen(2, "その他の操作とタスクは、本人との1:1トークで。",
                   "共有カードのボタンから開始。メニュー選択・項目入力・確認は本人だけに届きます。")
    s.panel(24, "① Botとの1:1トーク", "許可ユーザー本人だけのトーク / 抜粋")
    s.panel(568, "② タスクの確認と確定", "「確定する」を選ぶまで登録されません")
    s.text(44, 252, "「その他の操作」を押したとき", 16, GREEN, True)
    s.bot(44, 266)
    s.template(66, s.block(66, 304, [(HEADING, INK, True)]), ["患者の記録まとめ", "誤りを報告", "自分のタスク", "未確認一覧",
                         "この患者を検索", "全体の新着集計"])
    s.text(44, 600, "「タスク作成」を押したとき", 16, GREEN, True)
    s.bot(44, 614)
    s.bubble(66, 652, "タスク内容\n中止する場合は「取消」。", height=83)
    s.bubble(88, 750, "次回訪問時に残薬を確認", user=True)
    s.paragraph(44, 850, "続いて担当者・期限・理由を同様に入力します。", 468, 16, MUTED)
    s.bot(588, 245)
    s.rect(610, 286, 422, 279, "#fff", 9, "#d5e1e5")
    s.text(626, 321, "確認 — タスク作成", 21, INK, True)
    s.paragraph(626, 366, "内容: 次回訪問時に残薬を確認\n担当: 田中さん\n期限: 2026-10-02\n理由: 次回訪問で確認するため", 390, 18)
    s.template(610, 565, ["確定する", "取消"])
    s.text(588, 685, "本人が「確定する」を選んだ後", 17, GREEN, True)
    s.bubble(610, 710, "受け付けました。")
    s.text(588, 814, "処理結果を取得した後", 17, MUTED)
    s.bubble(610, 839, "反映しました。")
    return s.finish()


SCREENS = {"01-delivery": delivery, "02-task-confirm": task_confirm}


def export_png(path, svg):
    temporary = path.with_name(f".{path.stem}.tmp.png")
    try:
        if shutil.which("inkscape"):
            subprocess.run(["inkscape", str(path), "--export-type=png", "--export-width=1650",
                            f"--export-filename={temporary}"], check=True, stdout=subprocess.DEVNULL)
        elif sys.platform == "darwin" and shutil.which("qlmanage"):
            # These square canvases avoid Quick Look's thumbnail aspect-ratio change.
            with tempfile.TemporaryDirectory(prefix="mcs-lineworks-gallery-") as tmp:
                subprocess.run(["qlmanage", "-t", "-s", "1650", "-o", tmp, str(path)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                shutil.copyfile(Path(tmp) / (path.name + ".png"), temporary)
        else:
            raise ValueError("PNG export requires Inkscape, or macOS Quick Look with local CJK fonts")
        verify_png(temporary, svg)
        temporary.replace(path.with_suffix(".png"))
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--png", action="store_true", help="also export PNGs with local CJK fonts")
    args = parser.parse_args()
    if not args.check:
        OUT.mkdir(parents=True, exist_ok=True)
    for name, build in SCREENS.items():
        path, svg = OUT / f"{name}.svg", build()
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != svg:
                raise ValueError(f"stale LINE WORKS illustration: {path.name}")
        else:
            path.write_text(svg, encoding="utf-8")
            if args.png:
                export_png(path, svg)
    if args.check or args.png:
        for name, build in SCREENS.items():
            verify_png(OUT / f"{name}.png", build())
        hashes = {name: {ext: hashlib.sha256((OUT / f"{name}.{ext}").read_bytes()).hexdigest()
                         for ext in ("svg", "png")} for name in SCREENS}
        manifest = OUT / "assets.json"
        if args.check:
            if json.loads(manifest.read_text(encoding="utf-8")) != hashes:
                raise ValueError("LINE WORKS SVG/PNG pairs changed; regenerate with --png")
        else:
            manifest.write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
    print(f"{len(SCREENS)} synthetic LINE WORKS illustrations {'verified' if args.check else 'generated'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, zlib.error, subprocess.CalledProcessError) as exc:
        print(f"LINE WORKS gallery: {exc}", file=sys.stderr)
        raise SystemExit(1)
