#!/usr/bin/env python3
"""Generate source-based, fictional Slack illustrations for README."""

import argparse
import hashlib
from html import escape
import json
from pathlib import Path
import struct
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zlib


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/screenshots/slack-gallery"
INK = "#202124"
MUTED = "#66716f"
GREEN = "#087f5b"
FONT = "Noto Sans CJK JP,Hiragino Sans,Yu Gothic,Droid Sans Fallback,sans-serif"


class Screen:
    def __init__(self, number, title, subtitle, height=800):
        self.height = height
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="{height}" '
            f'viewBox="0 0 1100 {height}" font-family="{FONT}">',
            f"<title>{escape(title)} — 架空のSlack表示例</title>",
            "<desc>架空の名前・投稿・数値を使った説明図。"
            "実データ・実投稿の匿名化例は使用していません。実画面のキャプチャではありません。</desc>",
        ]
        self.rect(0, 0, 1100, height, "#f6f4ef")
        self.text(28, 34, f"HERMES-MCS  /  SLACK  /  {number:02}", 14, GREEN, True)
        self.text(28, 76, title, 32, INK, True)
        self.text(28, 107, subtitle, 18, MUTED)
        self.rect(24, 134, 1052, height - 215, "#fff", 12, "#dce2dd")
        self.rect(24, 134, 142, height - 215, "#351638", 12)
        self.rect(148, 134, 18, height - 215, "#351638")
        self.text(44, 176, "MCSデモ", 22, "#fff", True)
        self.text(44, 218, "チャンネル", 14, "#cbbfcf")
        self.rect(34, 232, 122, 36, "#64406a", 6)
        self.text(44, 256, "# mcs-demo", 15, "#fff")
        self.text(44, 300, "説明用データ", 14, "#cbbfcf")
        self.line(166, 194, 1076, 194, "#e1e5e2")
        self.text(192, 172, "# mcs-demo", 22, INK, True)
        self.text(1050, 172, "架空の表示例", 15, MUTED, end=True)
        self.text(28, height - 45, "説明用の画面イメージ。実際の配置はSlackのバージョン・設定で異なります。", 16, MUTED)
        self.text(28, height - 20, "実データ不使用 / 画像内の操作はできません", 15, GREEN)

    def rect(self, x, y, w, h, fill, radius=0, stroke=None):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
                          f'rx="{radius}" fill="{fill}"'
                          + (f' stroke="{stroke}"' if stroke else "") + "/>")

    def line(self, x1, y1, x2, y2, color="#e3e7e4"):
        self.parts.append(f'<path d="M{x1} {y1} L{x2} {y2}" stroke="{color}" fill="none"/>')

    def text(self, x, y, value, size=19, color=INK, bold=False, end=False):
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}"'
                          + (' font-weight="bold"' if bold else "")
                          + (' text-anchor="end"' if end else "")
                          + f'>{escape(value)}</text>')

    def paragraph(self, x, y, value, width=770, size=19, color=INK):
        used, line = 0, ""
        for c in value:
            advance = size * (1 if unicodedata.east_asian_width(c) in "WF" else .62)
            if c == "\n" or used + advance > width:
                self.text(x, y, line, size, color)
                y += size * 1.6
                used, line = 0, ""
            if c != "\n":
                line += c
                used += advance
        if line:
            self.text(x, y, line, size, color)
            y += size * 1.6
        return y

    def app(self, x=192, y=228, private=False):
        self.rect(x, y, 36, 36, GREEN, 8)
        self.text(x + 9, y + 26, "M", 22, "#fff", True)
        self.text(x + 48, y + 16, "MCS", 19, INK, True)
        self.rect(x + 100, y, 38, 20, "#edf0ee", 4)
        self.text(x + 106, y + 15, "APP", 11, MUTED)
        self.text(x + 151, y + 16, "10:02", 14, MUTED)
        if private:
            self.text(x + 48, y + 42, "操作した本人にだけ表示", 15, MUTED)
        return x + 48, y + (76 if private else 55)

    def button(self, x, y, label, width, primary=False):
        self.rect(x, y, width, 40, GREEN if primary else "#fff", 6,
                  None if primary else "#a9b5ae")
        self.text(x + 14, y + 27, label, 18, "#fff" if primary else INK, primary)

    def actions(self, x, y, confirmed=False):
        self.button(x, y, "確認済み" if confirmed else "☐ 確認", 116, confirmed)
        self.button(x + 126, y, "担当中" if confirmed else "担当する", 116)
        self.button(x + 252, y, "操作を選ぶ… ▾", 173)

    def card(self, x, y, width=770, compact=False):
        self.text(x, y, "山田さん — 10-01", 23, INK, True)
        y += 35
        self.text(x, y, "10-01 09:40 訪問看護 佐藤さん", 17, MUTED)
        y += 32
        self.text(x, y, "要約（抽出候補）", 17, GREEN, True)
        y += 30
        y = self.paragraph(x, y, "要約: 次回訪問時に残薬を確認してほしいとの連絡", width, 19)
        self.text(x, y + 5, "区分: 依頼", 19)
        y += 38
        if not compact:
            y = self.paragraph(x, y, "依頼: 残薬確認 / 期限表現: 次回訪問時", width, 19)
            y += 12
        return y

    def finish(self):
        return "\n".join(self.parts + ["</svg>"]) + "\n"


def overview():
    s = Screen(1, "連絡の要点と原文を、同じSlackで。", "カードで確認・担当を共有。本文と添付はスレッドへ。", 810)
    s.line(662, 194, 662, 729)
    x, y = s.app(188, 227)
    y = s.card(x, y, 397, compact=True)
    # Slack wraps controls in a narrow pane; the menu remains one select.
    s.button(x, y + 16, "☐ 確認", 110)
    s.button(x + 122, y + 16, "担当する", 116)
    s.button(x, y + 68, "操作を選ぶ… ▾", 173)
    s.text(x, y + 146, "MCSで開く", 18, "#1264a3")
    s.text(x, y + 182, "本文・添付はスレッドに投稿済み", 16, MUTED)
    s.text(689, 227, "スレッド", 23, INK, True)
    s.text(689, 257, "山田さん — 10-01", 16, MUTED)
    s.line(682, 275, 1059, 275)
    s.text(689, 313, "MCS / 原文の配送", 18, INK, True)
    y = s.paragraph(689, 350, "訪問看護 佐藤さん\n次回訪問時に残薬を確認してください。\n服薬カレンダーの写真を共有します。", 354, 18)
    s.rect(689, y + 9, 352, 105, "#f3f6f4", 8, "#dce2dd")
    s.text(707, y + 43, "添付: 服薬カレンダー.jpg", 16)
    s.text(707, y + 76, "説明用の添付表示 / 内容解析なし", 15, MUTED)
    s.text(689, y + 156, "原文を読んで、人が確認・判断します。", 16, GREEN)
    return s.finish()


def notification():
    s = Screen(2, "通知カードから、確認と担当を共有。", "カードは件数、スレッドは投稿ごとのスタンプと押した人。", 1070)
    x, y = s.app()
    y = s.card(x, y)
    s.line(x, y + 8, 1028, y + 8)
    s.text(x, y + 45, "確認: 田中さん / 担当: 田中さん", 18, MUTED)
    s.text(x, y + 77, "スタンプ 👀5 🙆2 · 自分 2投稿", 18, GREEN)
    s.actions(x, y + 105, confirmed=True)
    s.text(x, y + 171, "MCSで開く", 19, "#1264a3")
    s.text(x, y + 204, "本文・添付はスレッドに投稿済み", 17, MUTED)
    s.text(x, y + 236, "スタンプの観測は、担当引受・タスク完了の記録ではありません。", 16, MUTED)
    s.text(x, y + 274, "スレッドの投稿ごとの表示（押下者取得を有効にした場合）", 16, GREEN, True)
    s.rect(x, y + 290, 788, 178, "#f3f6f4", 8, "#dce2dd")
    s.text(x + 18, y + 321, "10-01 09:40 佐藤さん（訪問看護）", 17, MUTED)
    s.text(x + 18, y + 353, "スタンプ 👀2 🙆1 · 観測 10-01 10:00", 18, GREEN)
    s.paragraph(x + 18, y + 385, "押した人: 👀 田中 花子、佐藤 一郎 / 🙆 鈴木 太郎（自分） · 観測 10-01 10:00", 752, 17)
    s.line(x + 18, y + 420, x + 770, y + 420, "#c4ccc6")
    s.text(x + 18, y + 450, "次回訪問時に残薬を確認してください。", 18)
    s.text(x, y + 489, "観測は取得した時点。スタンプを押した時刻ではありません。", 15, MUTED)
    return s.finish()


def menu():
    s = Screen(3, "次の操作を、ひとつのメニューから。", "タスク作成・患者サマリー・検索など、必要な操作だけを選びます。", 940)
    x, y = s.app()
    s.card(x, y, compact=True)
    s.actions(x, 465)
    s.rect(x + 252, 513, 348, 310, "#fff", 8, "#aebbb2")
    labels = ("タスク作成", "タスク完了", "患者サマリー", "抽出の誤りを報告",
              "自分のタスク", "未確認一覧", "この患者を検索")
    for i, label in enumerate(labels):
        yy = 526 + i * 42
        if i == 0:
            s.rect(x + 259, yy - 2, 334, 38, "#e5f3ec", 4)
        s.text(x + 278, yy + 24, label, 19, GREEN if i == 0 else INK, i == 0)
    s.text(x, 552, "表示項目はカードの", 18, MUTED)
    s.text(x, 582, "状態・設定で変わります。", 18, MUTED)
    return s.finish()


def task_form():
    s = Screen(4, "依頼を、内容・担当・期限のあるタスクへ。", "入力後は「確認へ」。この画面だけでは登録されません。", 1050)
    s.rect(166, 194, 910, 775, "#e5e6e6")
    s.rect(293, 211, 616, 734, "#fff", 12, "#c2cdc5")
    s.text(321, 255, "タスク作成", 25, INK, True)
    s.text(876, 255, "×", 24, MUTED, end=True)
    s.line(293, 278, 909, 278)
    fields = [
        ("タスク内容", "次回訪問で残薬を確認", 83),
        ("担当者（一覧から）", "田中さん（みどり薬局）  ▾", 45),
        ("担当者（その他・手入力）", "", 45),
        ("期限 YYYY-MM-DD（任意）", "2026-10-03", 45),
        ("理由（任意）", "訪問看護からの依頼を確認するため", 78),
    ]
    y = 311
    for label, value, height in fields:
        s.text(321, y, label, 17, INK, True)
        s.rect(321, y + 13, 560, height, "#fff", 5, "#a9b5ae")
        if value:
            s.text(335, y + 43, value, 18)
        y += height + 59
    s.line(293, 866, 909, 866)
    s.button(610, 888, "取消", 96)
    s.button(724, 888, "確認へ", 156, True)
    return s.finish()


def task_preview():
    s = Screen(5, "登録前に、内容と理由を確認。", "本人だけに表示される確認画面で「確定する」を選びます。", 800)
    x, y = s.app(private=True)
    s.text(x, y, "確認 — タスク作成", 24, INK, True)
    y += 46
    y = s.paragraph(x, y, "内容: 次回訪問で残薬を確認\n担当: 田中さん（みどり薬局）\n期限: 2026-10-03\n理由: 訪問看護からの依頼を確認するため", 767, 21)
    s.button(x, y + 20, "確定する", 144)
    s.button(x + 162, y + 20, "取消", 96)
    s.text(x, y + 118, "内容・担当・期限・理由を照合してから確定します。", 18, GREEN)
    s.text(x, y + 151, "タスクの作成とMCSへの投稿は別です。", 17, MUTED)
    return s.finish()


def task_list():
    s = Screen(6, "未完了タスクを、次の状態へ。", "このスレッドの一覧を本人に表示。対応中・完了を人が記録します。", 820)
    x, y = s.app(private=True)
    s.text(x, y, "**タスク**（このスレッド）", 23, INK, True)
    s.rect(x, y + 27, 13, 13, "#fff", 2, "#a9b5ae")
    s.text(x + 25, y + 43, "#101 次回訪問で残薬を確認", 21)
    s.text(x + 25, y + 78, "担当: 田中さん / 期限: 2026-10-03", 19, MUTED)
    s.rect(x, y + 121, 13, 13, "#fff", 2, "#a9b5ae")
    s.text(x + 25, y + 137, "#102 添付された記録を確認", 21)
    s.text(x + 25, y + 172, "担当: 田中さん / 期限: 2026-10-04", 19, MUTED)
    s.button(x, y + 212, "対応中 #101", 166)
    s.button(x + 184, y + 212, "完了 #101", 153, True)
    s.button(x, y + 266, "対応中 #102", 166)
    s.button(x + 184, y + 266, "完了 #102", 153, True)
    s.text(x, y + 355, "カードの確認済み表示は、タスク完了ではありません。", 18, GREEN)
    return s.finish()


def patient_summary():
    s = Screen(7, "訪問前に、患者の記録をたどる。", "取得済み投稿の暫定集約。未取得の記録を「無い」と扱いません。", 1080)
    x, y = s.app(private=True)
    s.text(x, y, "山田さん — 患者サマリー（暫定集約）", 23, INK, True)
    y += 38
    y = s.paragraph(x, y, "※ 取得済み投稿から自動作成した暫定集約です。未取得・未抽出・訂正前の記録があり得るため、確定した処方一覧や依頼台帳の代わりにはなりません。原本で確認してください。", 758, 18, MUTED)
    y += 12
    y = s.paragraph(x, y, "履歴取得: 未完了（指定日より前は未取得）（取れていない記録は「無い」ではありません。欠落なしの保証ではありません）", 758, 18, MUTED)
    y += 19
    rows = [
        "■ 薬（投稿から抽出。確定した処方ではありません）",
        "・薬剤A（最終言及 2026-10-01）",
        "■ 抽出されたバイタルなし",
        "■ 次回予定（抽出表現）: 10月3日の訪問",
        "連携サマリー（MCS）: 未取得",
        "■ 未完了タスク",
        "・#101 次回訪問で残薬を確認 — 担当 田中さん",
        "  期限 2026-10-03",
    ]
    for row in rows:
        y = s.paragraph(x, y, row, 758, 19)
        y += 10
    return s.finish()


SCREENS = {"01-overview": overview, "02-notification": notification,
           "03-actions": menu, "04-task-form": task_form,
           "05-task-preview": task_preview, "06-task-list": task_list,
           "07-patient-summary": patient_summary}


def verify_png(path, svg):
    """Check complete PNG chunks, CRCs and decoded image data before publishing."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"invalid PNG: {path.name}")
    offset, compressed, header, ended = 8, [], None, False
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError(f"truncated PNG: {path.name}")
        kind = data[offset + 4:offset + 8]
        chunk = data[offset + 8:end - 4]
        crc = struct.unpack(">I", data[end - 4:end])[0]
        if zlib.crc32(kind + chunk) & 0xffffffff != crc:
            raise ValueError(f"PNG CRC mismatch: {path.name}")
        if kind == b"IHDR":
            if offset != 8 or length != 13:
                raise ValueError(f"invalid PNG header: {path.name}")
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            compressed.append(chunk)
        elif kind == b"IEND":
            if length or end != len(data):
                raise ValueError(f"invalid PNG end: {path.name}")
            ended = True
            break
        offset = end
    if not ended or header is None or not compressed:
        raise ValueError(f"incomplete PNG: {path.name}")
    width, height, depth, color, compression, filtering, interlace = header
    expected_height = int(int(ET.fromstring(svg).attrib["height"]) * 1.5)
    if (width, height) != (1650, expected_height) or depth != 8 \
            or color not in (2, 6) or (compression, filtering, interlace) != (0, 0, 0):
        raise ValueError(f"unexpected generated PNG format: {path.name}")
    raw_size = height * (1 + width * (3 if color == 2 else 4))
    decoder = zlib.decompressobj()
    raw = decoder.decompress(b"".join(compressed), raw_size + 1)
    if len(raw) != raw_size or not decoder.eof or decoder.unused_data:
        raise ValueError(f"invalid PNG image data: {path.name}")
    stride = 1 + width * (3 if color == 2 else 4)
    if any(raw[offset] > 4 for offset in range(0, raw_size, stride)):
        raise ValueError(f"invalid PNG image data: {path.name}")


def asset_hashes():
    return {name: {ext: hashlib.sha256((OUT / f"{name}.{ext}").read_bytes()).hexdigest()
                   for ext in ("svg", "png")} for name in SCREENS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--png", action="store_true", help="also export with Inkscape and local CJK fonts")
    args = parser.parse_args()
    if args.check and args.png:
        parser.error("--check and --png cannot be combined")
    if not args.check:
        OUT.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, build in SCREENS.items():
        path = OUT / f"{name}.svg"
        data = build()
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != data:
                stale.append(path.name)
            continue
        path.write_text(data, encoding="utf-8")
        if args.png:
            temporary = path.with_name(f".{name}.tmp.png")
            try:
                subprocess.run(["inkscape", str(path), "--export-type=png",
                                "--export-width=1650",
                                f"--export-filename={temporary}"], check=True,
                               stdout=subprocess.DEVNULL)
                verify_png(temporary, data)
                temporary.replace(path.with_suffix(".png"))
            finally:
                temporary.unlink(missing_ok=True)
    if stale:
        print("stale Slack gallery SVGs: " + ", ".join(stale), file=sys.stderr)
        return 1
    manifest = OUT / "assets.json"
    if args.check or args.png:
        for name, build in SCREENS.items():
            verify_png(OUT / f"{name}.png", build())
        hashes = asset_hashes()
        if args.check:
            if json.loads(manifest.read_text(encoding="utf-8")) != hashes:
                print("Slack SVG/PNG pairs changed; regenerate with --png", file=sys.stderr)
                return 1
        else:
            manifest.write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
    print(f"{len(SCREENS)} fictional Slack illustrations {'verified' if args.check else 'generated'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, zlib.error, subprocess.CalledProcessError) as exc:
        print(f"Slack gallery: {exc}", file=sys.stderr)
        raise SystemExit(1)
