#!/usr/bin/env python3
"""Generate the fictional v1.0.14/v1.0.15 notification explanation."""
from pathlib import Path
import importlib.util
import subprocess
import tempfile

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
module = importlib.util.spec_from_file_location('gallery', ROOT / 'scripts/development/generate_slack_gallery.py')
gallery = importlib.util.module_from_spec(module)
module.loader.exec_module(gallery)
s = gallery.Screen.__new__(gallery.Screen)
s.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="780" height="780" viewBox="0 0 780 780" font-family="{gallery.FONT}">',
           '<title>通知文の比較 — v1.0.14 と v1.0.15</title>',
           '<desc>完全に架空の説明図。旧版はMCS確認カードの固定通知文。新版は患者の山田花子、依頼候補の自動要約、発信者の佐藤直子、所属のあおぞら訪問看護、10月6日8時30分の投稿時刻を通知文に含む。実端末の画面ではなく、実際の省略や受信は通知設定に依存する。</desc>']
s.rect(0, 0, 780, 780, '#f6f4ef')
s.text(30, 47, '患者と内容を通知から確認', 40, gallery.INK, True)
s.text(30, 85, '説明用イメージ / 実端末のキャプチャではありません', 24, gallery.MUTED)
s.rect(24, 109, 732, 149, '#fff', 12, '#dce2dd')
s.text(48, 149, '変更前  v1.0.14 — Slack の通知文', 29, gallery.INK, True)
s.text(48, 197, 'MCS 確認カード', 36, gallery.INK, True)
s.text(48, 234, '患者・発信者・所属・時刻・内容は含まない', 26, gallery.MUTED)
s.rect(24, 282, 732, 304, '#fff', 12, '#087f5b')
s.text(48, 322, '変更後  v1.0.15 — 同じ投稿から通知文', 29, gallery.GREEN, True)
for y, text in [(371, '山田 花子: 投稿の自動要約:'), (415, '依頼候補（未確認）: 朝の薬の変更確認'), (459, '/ 佐藤 直子（あおぞら訪問看護）'), (503, '/ 10-06 08:30')]:
 s.text(48, y, text, 32, gallery.INK, y==371)
s.text(48, 550, '自動抽出の候補と、人が確定した依頼を区別', 26, gallery.GREEN)
s.text(30, 629, '開く前に対象と内容の見当がつく', 32, gallery.INK, True)
s.text(30, 672, '端末の通知プレビュー設定を確認してください。', 27, gallery.INK)
s.text(30, 714, '実際の表示・省略・受信は端末とアプリの設定次第。', 25, gallery.MUTED)
s.text(30, 754, '架空の名前・投稿のみ使用 / 実データ・匿名化例なし', 25, gallery.MUTED)
s.parts.append('</svg>')
svg = OUT / 'mobile-preview-comparison.svg'
svg.write_text('\n'.join(s.parts)+'\n', encoding='utf-8')
with tempfile.TemporaryDirectory(prefix='mcs-release-visual-') as directory:
 subprocess.run(['qlmanage', '-t', '-s', '1180', '-o', directory, str(svg)], check=True, stdout=subprocess.DEVNULL)
 subprocess.run(['sips', '-s', 'format', 'png', str(Path(directory) / (svg.name + '.png')), '--out', str(svg.with_suffix('.png'))], check=True, stdout=subprocess.DEVNULL)
 subprocess.run(['sips', '--resampleWidth', '390', str(svg.with_suffix('.png')), '--out', str(OUT / 'mobile-preview-comparison-390.png')], check=True, stdout=subprocess.DEVNULL)
