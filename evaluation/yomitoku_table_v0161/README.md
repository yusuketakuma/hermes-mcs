# yomitoku_table_v0161 表セル幾何の評価資産（非商用の技術評価限定）

`output_schema_table_overlap` の発生機序を、導入版 YomiToku 0.16.1 の
実コードで再現するための評価専用資産です。製品コード経路からは
import しません（MCS 収集・解析コアの標準ライブラリ限定の例外ではなく、
評価層の隔離資産です）。

## 出典・来歴

- 取得元：<https://github.com/kotaro-kinoshita/yomitoku>
- 固定 commit：`0279cf256e2588430d085e2dc958e8740be82921`（tag `v0.16.1`）
- 著作権：YomiToku © 2024 by Kotaro Kinoshita
- ライセンス：**CC BY-NC-SA 4.0**
  <https://creativecommons.org/licenses/by-nc-sa/4.0/>
- 導入済み実ライブラリ（`<OCR_ENV>`・yomitoku 0.16.1）の対象 file と
  抽出元 file は byte 一致を確認済み。

## 抽出範囲（`upstream_cells.py`）

元関数 body は無改変。import を除いた 6 関数の集約・抽出です。

| 関数 | 対応元 path | 元行 | 元 file sha256（先頭16 hex） |
|---|---|---|---|
| `extract_cells` | `src/yomitoku/table_structure_recognizer.py` | L27 | `52361dcd8fbd7480` |
| `filter_contained_cells_within_spancell` | 同上 | L49 | 同上 |
| `filter_by_flag` | `src/yomitoku/utils/misc.py` | L37 | `2f9c59a39e3662cd` |
| `calc_overlap_ratio` | 同上 | L56 | 同上 |
| `is_contained` | 同上 | L74 | 同上 |
| `calc_intersection` | 同上 | L96 | 同上 |

モデル・重み・SDK のコピーはありません。製品経路の変更・本番 import
もありません。

## upstream 文書の食い違い（検証基準の明記）

`is_contained` の docstring は `threshold` の既定を 0.9 と説明しますが、
実装の既定値は 0.8 です。抽出 body は upstream 原文のまま保存して
います。本評価の判定は docstring でなく実装コードの 0.8 を基準に
します。

## 使い方

`tests/extract/test_yomitoku_table_geometry_eval.py` が
`importlib` でこの module を読み、完全合成の 2×2 grid に対して
実関数を実行して MCS の `_document_text` 拒否を検証します。

結論は `CONCLUSION.md`。
