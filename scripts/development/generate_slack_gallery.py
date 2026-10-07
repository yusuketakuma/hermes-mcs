#!/usr/bin/env python3
"""Generate source-based, fictional Slack illustrations for README."""

import argparse
import hashlib
from html import escape
import json
import re
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
# Fully fictional example shared by every screen (no real or anonymised posts).
HEADING = "💬 山田 花子（あおぞら）"
# layout 2: the line under the title — start date, then counts
META = "10-01〜 · {n}投稿 · 📎 1 · @自分宛て"
SENDER = "佐藤さん（訪問看護・あおぞら）"
POSTS = (
    (f"10-01 09:40 {SENDER}",
     ("次回訪問時に残薬を確認してほしいとの連絡",
      "要点: 残薬の確認 / 服薬カレンダーを共有",
      "区分: 依頼・添付",
      "依頼: 確認依頼:次回訪問時に残薬を確認(期限:次回訪問時)")),
    ("10-01 10:05 鈴木さん（ケアマネ・ひなた）",
     ("確認結果を担当者会議で共有してほしいとの連絡",
      "区分: 連絡")),
)
STATE = ("👤 担当: 田中 · ✅ 確認: 田中",
         "📝 タスク 1件 · スタンプ 👀5 🙆2 · 自分 2投稿")
RULE = "─" * 12
BODY = "次回訪問時に残薬を確認してください。服薬カレンダーの写真を共有します。"


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

    def paragraph(self, x, y, value, width=770, size=19, color=INK, bold=False):
        used, line = 0, ""
        for c in value:
            advance = size * (1 if unicodedata.east_asian_width(c) in "WFA" else .62)
            if c == "\n" or used + advance > width and c not in "。、）」":
                # keep dates, times and words whole: carry a trailing ASCII run
                word = re.search(r"[0-9A-Za-z.:()-]+$", line) if c.isascii() and c.strip() else None
                carry = line[word.start():] if word and word.start() else ""
                self.text(x, y, line[:len(line) - len(carry)], size, color, bold)
                y += size * 1.6
                line, used = carry, size * .62 * len(carry)
            if c != "\n":
                line += c
                used += advance
        if line:
            self.text(x, y, line, size, color, bold)
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

    def rule(self, x, y, width):
        self.line(x, y - 12, x + width, y - 12, "#d5dcd7")
        return y + 16

    def actions(self, x, y, confirmed=False, wrap=False):
        """確認する・担当する・タスク作成 + one select; MCSで開く is a text link below."""
        self.button(x, y, "確認する", 110, confirmed)
        self.button(x + 120, y, "担当する", 110)
        self.button(x + 240, y, "タスク作成", 128)
        sx, sy = (x, y + 52) if wrap else (x + 378, y)
        self.button(sx, sy, "操作を選ぶ… ▾", 173)
        self.text(x, sy + 76, "MCSで開く", 18, "#1264a3")
        return sy + 100

    def card(self, x, y, width=770, full=False):
        """Card zones top to bottom: patient title, meta line, per post a
        small time/sender line over its summary, state."""
        y = self.paragraph(x, y, HEADING, width, 21, INK, True)
        y = self.paragraph(x, y - 4, META.format(n=2 if full else 1), width, 17, MUTED)
        if full:
            y = self.paragraph(x, y - 4, "🆕 返信+1", width, 17, GREEN)
        for sender, bullets in POSTS[:2 if full else 1]:
            y = self.rule(x, y, width)
            y = self.paragraph(x, y, sender, width, 15, MUTED)
            for bullet in bullets:
                y = self.paragraph(x, y - 2, "・" + bullet, width, 18)
        y = self.rule(x, y, width)
        for line in STATE if full else ("📝 タスク 1件 · スタンプ 👀2 🙆1",):
            y = self.paragraph(x, y - 2, line, width, 17, MUTED)
        return self.rule(x, y, width)

    def thread_post(self, x, y, width, size=17, sender=SENDER, stamps=True):
        """One native thread post with source metadata, stamps and the source body."""
        y = self.paragraph(x, y, f"↳ 10-01 09:40 {sender}", width, size, INK, True)
        if stamps:
            y = self.paragraph(x, y + 4, "スタンプ 👀2 田中・佐藤 / 🙆1 鈴木 · 10-01 10:00時点",
                               width, size, GREEN)
        y = self.paragraph(x, y, RULE, width, size, MUTED)
        y = self.paragraph(x, y - 2, "📄 本文", width, size, GREEN, True)
        return self.paragraph(x, y - 2, BODY, width, size)

    def finish(self):
        return "\n".join(self.parts + ["</svg>"]) + "\n"


def overview():
    s = Screen(1, "連絡の要点と原文を、同じSlackで。", "カードで確認・担当を共有。本文と添付はスレッドへ。", 1000)
    s.line(662, 194, 662, 919)
    x, y = s.app(188, 227)
    # Slack wraps controls in a narrow pane; the menu remains one select.
    s.actions(x, s.card(x, y, 397), wrap=True)
    s.text(689, 227, "スレッド", 23, INK, True)
    s.text(689, 257, "# mcs-demo", 16, MUTED)
    s.line(682, 275, 1059, 275)
    s.text(689, 313, "MCS", 18, INK, True)
    y = s.thread_post(689, 348, 354, 16)
    s.text(689, y + 24, "MCS", 18, INK, True)
    y = s.paragraph(689, y + 58, "📎 服薬カレンダー.jpg — 山田 花子 10-01 09:40 佐藤さん", 354, 16)
    s.rect(689, y - 8, 200, 120, "#f3f6f4", 8, "#dce2dd")
    s.text(789, y + 58, "画像", 16, MUTED)
    return s.finish()


def notification():
    s = Screen(2, "通知カードから、確認と担当を共有。", "上から概要・投稿ごとの要約・状態・操作の順。区切り線で分かれます。", 950)
    x, y = s.app()
    s.actions(x, s.card(x, y, full=True), confirmed=True)
    return s.finish()


def menu():
    s = Screen(3, "次の操作を、ひとつのメニューから。", "記録まとめ・誤りの報告・検索など、ボタン以外の操作を選びます。", 1040)
    x, y = s.app()
    y = s.card(x, y)
    s.actions(x, y)
    labels = ("患者の記録まとめ", "誤りを報告", "タスク一覧", "自分のタスク",
              "未確認一覧", "この患者を検索", "全体の新着集計")
    mx, my = x + 378, y + 48
    s.rect(mx, my, 300, 18 + 42 * len(labels), "#fff", 8, "#aebbb2")
    for i, label in enumerate(labels):
        yy = my + 12 + i * 42
        if i == 0:
            s.rect(mx + 7, yy - 2, 286, 38, "#e5f3ec", 4)
        s.text(mx + 24, yy + 24, label, 19, GREEN if i == 0 else INK, i == 0)
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
    s = Screen(6, "未完了タスクを、次の状態へ。", "このスレッドの一覧を本人に表示。対応中・完了を人が記録します。", 720)
    x, y = s.app(private=True)
    s.text(x, y, "📋 タスク（このスレッド）", 23, INK, True)
    y += 44
    for number, title, due in ((101, "次回訪問で残薬を確認", "2026-10-03"),
                               (102, "添付された記録を確認", "2026-10-04")):
        s.text(x, y, f"⬜ #{number} {title}", 21)
        s.text(x, y + 34, f"担当: 田中さん ・ 期限: {due}", 19, MUTED)
        s.button(x, y + 54, f"対応中 #{number}", 166)
        s.button(x + 184, y + 54, f"完了 #{number}", 153, True)
        y += 140
    return s.finish()


def patient_summary():
    s = Screen(7, "訪問前に、患者の記録をたどる。", "取得済み投稿の暫定集約。未取得の記録を「無い」と扱いません。", 1600)
    x, y = s.app(private=True)
    s.text(x, y, "山田 花子 — 患者の記録まとめ（暫定集約）", 23, INK, True)
    y += 38
    y = s.paragraph(x, y, "※ 取得済み投稿から自動作成した暫定集約です。未取得・未抽出・訂正前の記録があり得るため、確定した処方一覧や依頼台帳の代わりにはなりません。原本で確認してください。", 758, 18, MUTED)
    y += 12
    y = s.paragraph(x, y, "履歴取得: 未完了（指定日より前は未取得）（取れていない記録は「無い」ではありません。欠落なしの保証ではありません）", 758, 18, MUTED)
    y += 12
    y = s.paragraph(x, y, "抽出の処理状況（最新3投稿）: 処理中1・完了1・要確認1（完了区間4/7）", 758, 18, INK, True)
    y = s.paragraph(x, y, "完了は抽出処理のみ。記録や臨床情報の完全性は保証しません。", 758, 16, MUTED)
    y += 19
    rows = [
        "■ 薬（投稿から抽出。確定した処方ではありません）",
        "・薬剤A（最終言及 2026-10-01）",
        "■ 抽出されたバイタルなし",
        "■ 次回予定（抽出表現）: 10月3日の訪問",
        "■ 依頼候補の返信状況（記録上）: なし",
        "■ 背景・療養情報（原記録の抜粋。現在の確定情報ではありません）",
        "・服薬管理（投稿#701）: ご家族が服薬を管理しています。",
        "・療養方針（投稿#701）: 本人は通所を希望されています。",
        "※ 背景情報は抜粋です。詳しい内容や以前の記載は原記録をご確認ください。",
        "■ MCS登録情報（チャットとは別の記録・抜粋。現在の状態を断定しません）",
        "・薬剤登録: 取得済み（1処方期間）",
        "・観測項目: 取得済み（1項目）",
        "・観測値: 取得済み 1項目",
        "  体重: 45.2 kg（記録日 2026-10-06）",
        "連携サマリー（MCS）: 未取得",
        "■ 未完了タスク",
        "・#101 次回訪問で残薬を確認 — 担当 田中さん — 期限 2026-10-03",
    ]
    for row in rows:
        y = s.paragraph(x, y, row, 758, 19)
        y += 10
    return s.finish()


def urgent_notice():
    # Invoke the real formatter over a completely synthetic, in-memory source.
    import tempfile
    sys.path.insert(0, str(ROOT / "mcs"))
    import _mcs_path  # noqa: F401
    import alert_view
    from ledger import Ledger
    from mcs_adapter import Message
    with tempfile.TemporaryDirectory(prefix="mcs-fictional-gallery-") as temporary:
        ledger = Ledger(str(Path(temporary) / "ledger.db"))
        try:
            ledger.ensure_patient(1)
            ledger.db.execute("UPDATE patients SET patient_name='山田 花子',station_name='あおぞら' WHERE project_id=1")
            ledger.db.commit()
            ledger.save_messages([Message(701, 1, None, 1, "佐藤さん", "user", "訪問看護", "あおぞら",
                "2026-10-01T09:40:00+09:00", "本人は息苦しいと話しています。担当者へ連絡します。",
                "full", False, 0)])
            parts = alert_view.render_parts({"project_id": 1, "message_id": 701,
                "subject": "patient", "reasons": ["本人は息苦しいと話しています。"],
                "observed_at": 1790816400}, ledger.db)
        finally:
            ledger.close()
    s = Screen(8, "再確認の理由を、該当スレッドで。",
               "AIの確認候補。原文・対象人物・時点を見て、人が確認します。", 1040)
    x, y = s.app()
    s.text(x, y, "↳ 元の通知スレッドへの追加投稿", 17, MUTED)
    y += 43
    for part in parts["containers"]:
        heading = part["type"] == "heading"
        text = ("引用: " if part["type"] == "quote" else "") + part["text"]
        y = s.paragraph(x, y, text, 760, 23 if heading else 20,
                        INK, heading)
        y += 17
    for part in parts["footer"]:
        y = s.paragraph(x, y, part["text"], 760, 17, MUTED)
        y += 12
    return s.finish()


SCREENS = {"01-overview": overview, "02-notification": notification,
           "03-actions": menu, "04-task-form": task_form,
           "05-task-preview": task_preview, "06-task-list": task_list,
           "07-patient-summary": patient_summary, "08-urgent-notice": urgent_notice}


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
