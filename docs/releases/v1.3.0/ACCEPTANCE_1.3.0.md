# v1.3.0 受入準備票 — 未公開

この票は v1.3.0 の変更記録と説明資料を現在の候補へ準備した作業の記録です。候補全体の合格、本番適用、公開の完了を示すものではありません。

## 版番号の提案記録

- 比較元（直近の公開済み版）: タグ `v1.2.1` = `4a97288dd74f356c8bb2f7191721819f0a1fdaf3`（CHANGELOG の v1.2.1 節と照合済み）。本番反映SHAはこの記録では確認していない。
- 対象SHA: `devin/release-1.3.0-prep` の最終 commit は push 時点で固定する（準備時点の HEAD は `face4449`、base は origin/main `bbcb846a`）。
- 推奨番号: `1.3.0`（機能アップデート版）。最大区分は MINOR。
- 根拠: 未リリース変更記録34件の内訳が 新機能12・改善8・不具合修正14。12件の `added` 記録は互換な機能追加（在宅ケア台帳・医薬品検索共通入口・患者横断検索・Jev要求内訳表示など）。根拠path: `changes/archive/1.3.0/` 各記録と `docs/releases/v1.3.0/change-map.json`。非互換な CLI/JSON/設定/保存データの変更は記録に無い。内部改善の件数・行数・schema番号は区分の代理に使っていない。
- 互換性・必要な操作: 互換。更新時の必要操作は CHANGELOG「更新時の注意」にある再起動・設定確認、薬剤マスターのオペレーター承認（配布のみで有効化は別承認）、`QA_VERSION` 1→2（品質確認の受入れ規則変更に伴う版マーカー: Jev対象外患者を `out_of_scope` の SKIPPED として記録・`project_ids` から `jev_scope_policy` への置き換え・Jev モデル jev-1.14.0 への更新）。
- 未確認事項: タグ `v1.3.0` 作成・GitHub Release 公開・説明図PNGのasset添付と到達確認、本番適用・常駐サービス再起動、対象SHAでのCI最終run。

## 今回の対象

- v1.2.1（タグ 2026-11-22）以降の変更記録34件を `changes/archive/1.3.0/` へ収録。機能アップデート版として番号 `1.3.0` を指定（所有者 2026-11-24）。
- 内訳: 新機能12件（在宅ケア台帳・職員・ケアチーム・訪問歴・処方歴・訪問ルート・常駐モード・ローカルバックアップ対象・医薬品検索共通入口・患者横断検索・照会返答待ちアラート・日次サマリー処方期限一覧・Jev要求内訳表示・ローカル患者画面の表示とMCS引き渡し）、改善8件（Jev 1.14.0・モジュール整理・QC再抽出・意味解析まとめ処理など）、不具合修正14件（書込みロック待ち・意味解析滞留・費用集計・導入途中停止・照会待ちなど）。
- CHANGELOG の v1.3.0 節・README のリリース案内を生成し、説明図1点（在宅ケア台帳と医薬品検索・照会、SVG正本・PNG描画、合成の説明図で実画面ではない旨を本文に記載）を導入部へ掲載。公開本文は上限内。
- 説明図の来歴とhashは `visual-source.json`（PNG 1960×840 の sha256 記録済み）、34件の対応表は `change-map.json`。
- `docs/development/readme-review.json` を 1.3.0 へ更新し、README本文の見直し結果を記録済み。

## 検証済み（この作業の範囲・ローカル実測）

対象は `devin/release-1.3.0-prep` ブランチの作業ツリー（base: origin/main `bbcb846a`、HEAD `face4449`）。実行日は 2026-11-24（JST）。

- `release_notes.py check`: 合格（CHANGELOG構造・版別詳細ファイル・画像URL）。
- `release_notes.py build --version 1.3.0 --date 2026-11-24`: 生成成功（archive移動・CHANGELOG・README更新・changelog-1.3.0.md）。
- `release_notes.py export --version 1.3.0`: 本文・タイトル出力成功。`release-notes-1.3.0.md`・`release-title-1.3.0.txt` に収録（本文 107,596 byte、上限内）。
- `update_readme.py`: README 生成ブロック再生成済み（release・modules・signals・stats・cli）。
- `readme_release.py --check`: 合格。
- `check_drug_master_bundle.py`: 同梱 bundle は公式最新版 20261112（MHLW download menu の医薬品マスター最終更新日・令和8年11月12日・19,324件との一致を 2026-11-24 に確認）。有効化は要オペレーター承認で未実施。
- `scripts/run_tests.sh tests/release/`: 合格（59 tests + 128 subtests、2026-11-24 実測）。
- `ruff check`（CI同一範囲）: 合格。`ci/gates.py`: 10/10 合格。`ci/mine_gates.py --check`: 合格（全抽出インシデント covered または tracked）。
- 図版: PNG 1点（1960×840）を1372×588相当へ縮小した表示で文字欠落・重なり・余白を確認。右パネルの説明文が枠をはみ出す件は短縮して修正済み。`visual-source.json` に生成後の hash を記録。
- `patient_context_quality_identity.QA_VERSION`: 上記の受入れ規則変更に対応して 1→2 へ bump。hash 対象（`QUALITY_FUNCTIONS`・`QUALITY_CONSTANTS`）の関数・定数に差分なし。
- `release_owner_source.py --check`: 合格（main 取込後に manifest を再生成、digest `0602f939…`）。

### 新規インストールの受入

- クリーンな一時HOME・合成データでの導入・起動・診断・代表機能: `tests/release/` の検証に含まれる新規導入シナリオ（59 tests + 128 subtests）を実測合格。実接続が必要な部分（実MCS・実サービス）は権限・環境の範囲外のため未実施。
- 配布候補の取得・依存導入: fresh-install CI lane は最終SHAのCI run で確認予定（未実行）。

### 過去版からの更新の受入

- 対応を宣言する更新元: 全公開版からの更新経路を `tests/fixtures/schema_upgrade/` で追跡。直近公開タグを含む origins を同tagの実コードから生成したDDL fixture と hash で追加済み: v1.1.2 `ff038ef8`→`shape-16.sql`（72オブジェクト）、v1.2.0 `204cbdbb`・v1.2.1 `4a97288d`→`shape-17.sql`（74オブジェクト。2版は対象ソースがbyte同一で同一 fixture）。
- `update-paths.json` の target は v1.3.0。`test_supported_update_paths.py`・`test_supported_schema_upgrade.py`・`test_release_origin_tick.py`（fixture 追加後に失敗した項目を fixture のDDL・hash修正で再検証）: 616 tests 合格（2026-11-24 実測）。
- 更新元の合成DB・設定から更新コードの実入口・バックアップ・保持・再起動後の診断・rollback: 上記3ファイルのシナリオがカバー。実機の本番DBでの適用は未実施（別承認）。

## 未実行・残条件

- 全体 suite・CI の全6 lane: 本票作成時点ではリリースPRの CI run が最終の実測証拠になる。対象SHAでのrunを照合する。
- 独立レビュー（Devin Review）: PR に付く見込み。指摘があれば対応を記録する。
- tag `v1.3.0` の作成・GitHub Release の公開・説明図 PNG の release asset 添付と到達・表示確認: 未実施。本文中の画像URLは公開後に照合が必要。
- 本番適用・常駐サービスの再起動: 未実施。「更新時の注意」に記載の再起動条件は公開後の適用時に従う。
- 未リリース記録 `changes/drug-alias-over-limit-cache.json`: 準備中に main へ届いた記録で、次版の集約対象として未収録のまま残す。

## 参照

- 変更の要点と収録物: [README.md](README.md)
- 全34件の対応表: [change-map.json](change-map.json)
