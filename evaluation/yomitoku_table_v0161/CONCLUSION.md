# 評価結論 表セル重複の発生機序（合成反例で確定）

## 結論

`output_schema_table_overlap` の原因は、YomiToku 0.16.1 が出力する
grid の実際の重複です。MCS の重複検出は正しい fail-closed で、
実関数で機序を再現しました。

## 機序（実関数で証明）

1. `extract_cells` は row/col の交差から 1 起点の cell grid を作る。
2. `filter_contained_cells_within_spancell` は span box に
   `is_contained`（包含率 > 0.8）で完全包含される sub-cell のみ除去
   し、包含子 index の min/max+1 から merged cell の row/col/span を
   作る。
3. 包含率 0.8 未満の sub-cell は除去されず残る。merged cell の index
   範囲にその残存 cell が入ると、同一行で col 範囲が重複する。

合成反例：2×2・各 100px の grid に span `[15,15,200,200]` を与えると、
左上 cell の包含率は 0.7225 で残り、他 3 cell（0.85・0.85・1.0）は
2×2 の merged cell になる。残存 (1,1) と merged (1,1) 2×2 が重複し、
`attachment_ocr._document_text` が `output_schema_table_overlap` で
正しく拒否することを `test_yomitoku_table_geometry_eval.py` で pin
しました。

## 判断への含意

- 拒否は正当（エンジンの非分割 grid 出力）。`aggregate` が cell 毎に
  全文 word リストへ割当するため、重複 cell は複製/分割 text を持ち
  得る。無損失の自動 merge は証明できず、医療文書では fail-closed が
  適切。
- MCS 側の課題は「決定的失敗（同一入力＋pin 済み engine で必ず同一
  結果）と一時失敗の不分離」で、再試行後も `pending_failed` に残り
  常駐警報となる点。終端分離の設計は別判定。

## 限界（断定しないこと）

- 実 10 件すべてがこの機序とは、実測なしでは断定しない。
- 「同一入力なら必ず同一出力」も実測なしでは断定しない（固定
  engine・入力なら決定的と見込まれるが未検証）。
- 合成反例の成功は実モデルの品質評価・昇格の証拠にしない。
