# v1.3.0 受入準備票 — 未公開

この票は v1.3.0 の変更記録と説明資料を現在の候補へ準備した作業の記録です。候補全体の合格、本番適用、公開の完了を示すものではありません。

## 版番号の提案記録

- 比較元（直近の公開済み版）: タグ `v1.2.1` = `4a97288dd74f356c8bb2f7191721819f0a1fdaf3`（CHANGELOG の v1.2.1 節と照合済み）。本番反映SHAはこの記録では確認していない。
- 対象SHA: QA_VERSION の合成probeは過去の候補 `27f2619` で実施。Issue #198の範囲限定独立QAは候補 `6845138a8c027f2a68cc7290650497434573c3fd` を対象に実施。今回の文書追補はコードを変更しないため、このQAの対象コードは変わりませんが、PRの最終HEADとCIは文書push後に別途照合します。
- 推奨番号: `1.3.0`（機能アップデート版）。最大区分は MINOR。
- 根拠: 未リリース変更記録34件の内訳が 新機能12・改善8・不具合修正14。12件の `added` 記録は互換な機能追加（在宅ケア台帳・医薬品検索共通入口・患者横断検索・Jev要求内訳表示など）。根拠path: `changes/archive/1.3.0/` 各記録と `docs/releases/v1.3.0/change-map.json`。非互換な CLI/JSON/設定/保存データの変更は記録に無い。内部改善の件数・行数・schema番号は区分の代理に使っていない。
- 互換性・必要な操作: 保存済みPASSは削除しませんが、`QA_VERSION` 1→2で旧PASSは現行版の確認として再利用されず、背景再判定まで概要が一時的に非表示になります。通常の更新手順に含まれるサービス再起動・設定確認と、薬剤マスターのオペレーター承認（配布のみで有効化は別承認）は従来どおり必要です。QA_VERSION は内部の版マーカーで、担当者が手動変更する操作ではありません。患者ごとの追加操作は不要で、稼働中のresident workerが背景再判定を進めます。
- 未確認事項: タグ `v1.3.0` 作成・GitHub Release 公開・説明図PNGのasset添付と到達確認、本番適用・常駐サービス再起動、対象SHAでのCI最終run。

## 今回の対象

- v1.2.1（タグ 2026-11-22）以降の変更記録34件を `changes/archive/1.3.0/` へ収録。機能アップデート版として番号 `1.3.0` を指定（所有者 2026-11-24）。
- 内訳: 新機能12件（在宅ケア台帳・職員・ケアチーム・訪問歴・処方歴・訪問ルート・常駐モード・ローカルバックアップ対象・医薬品検索共通入口・患者横断検索・照会返答待ちアラート・日次サマリー処方期限一覧・Jev要求内訳表示・ローカル患者画面の表示とMCS引き渡し）、改善8件（Jev 1.14.0・モジュール整理・QC再抽出・意味解析まとめ処理など）、不具合修正14件（書込みロック待ち・意味解析滞留・費用集計・導入途中停止・照会待ちなど）。
- CHANGELOG の v1.3.0 節と公開Release本文を生成し、同じ比較表・説明図PNGを掲載。README のリリース案内も生成。図は合成の説明図で実画面ではない旨を記載し、公開URLの到達・Release画面での表示は未確認（tag/asset未公開）。公開本文は上限内。
- 説明図の来歴とhashは `visual-source.json`（PNG 1960×840 の sha256 記録済み）、34件の対応表は `change-map.json`。
- `docs/development/readme-review.json` を 1.3.0 へ更新し、README本文の見直し結果を記録済み。

## 検証済み（この作業の範囲・ローカル実測）

対象は `devin/release-1.3.0-prep` ブランチの作業ツリー（base: origin/main `bbcb846a`、HEAD `f4b35f7`）。実行日は 2026-11-24（JST）。

- `release_notes.py check`: 合格（CHANGELOG構造・版別詳細ファイル・画像URL）。
- `release_notes.py build --version 1.3.0 --date 2026-11-24`: 生成成功（archive移動・CHANGELOG・README更新・changelog-1.3.0.md）。
- `release_notes.py export --version 1.3.0`: 本文・タイトル出力成功。`release-notes-1.3.0.md`・`release-title-1.3.0.txt` に収録（本文 107,596 byte、上限内）。
- `update_readme.py`: README 生成ブロック再生成済み（release・modules・signals・stats・cli）。
- `readme_release.py --check`: 合格。
- `check_drug_master_bundle.py`: 同梱 bundle は公式最新版 20261112（MHLW download menu の医薬品マスター最終更新日・令和8年11月12日・19,324件との一致を 2026-11-24 に確認）。有効化は要オペレーター承認で未実施。
- `scripts/run_tests.sh tests/release/`: 合格（59 tests + 128 subtests、2026-11-24 実測）。
- `ruff check`（CI同一範囲）: 合格。`ci/gates.py`: 10/10 合格。`ci/mine_gates.py --check`: 合格（全抽出インシデント covered または tracked）。
- 図版: PNG 1点（1960×840）を1372×588相当へ縮小した表示で文字欠落・重なり・余白を確認。右パネルの説明文が枠をはみ出す件は短縮して修正済み。`visual-source.json` に生成後の hash を記録。
- `patient_context_quality_identity.QA_VERSION`: 上記の受入れ規則変更に対応して 1→2 へ bump。hash 対象（`QUALITY_FUNCTIONS`・`QUALITY_CONSTANTS`）の関数・定数に差分なし。旧PASSは保存されるが新しい識別値には一致せず、`extract_llm_resident` の背景レーンから `patient_context_refresh.run_due` が自動で再判定する。通常のサービス再起動後にworkerが稼働していることが前提で、患者ごとの手動再判定操作は不要。

### 患者コンテキスト表示の更新前後確認（合成記録のみ）

- QA_VERSION 1の完全合成データで旧PASSを作り、更新前の患者概要が表示されることを確認する。
- 同じ合成台帳で候補のQA_VERSION 2を適用し、旧PASSが保存されたまま概要が一時的に非表示になることを確認する。
- 独立した2ケースで背景再判定を進める。Jev対象内はstubの新しいPASS後に表示が戻ること、対象外はJev要求なしでsource/summaryの両方に `SKIPPED(out_of_scope)` が記録され、表示が戻ることを確認する。
- 2026-11-24の合成probeでは上記2経路を確認。workerループは常駐レーンから `run_due` を呼ぶが、probe自体は処理単位を直接進めたため、サービス再起動・worker監督・画面を含む一連の統合動作は未検証。実Jev・実患者データ・本番DBは使っていない。
- `release_owner_source.py --check`: 合格（main 取込後に manifest を再生成、digest `0602f939…`）。

### 新規インストールの受入

- クリーンな一時HOME・合成データでの導入・起動・診断・代表機能: 未実施。`tests/release/`（59 tests + 128 subtests、合格）はリリース資料・公開ツールの検証で、新規導入・起動・診断は実行しない。新規導入ワークフロー（`.github/workflows/fresh-install.yml`）はこのPRの自動実行条件に該当しない。
- 配布候補の取得・依存導入: 未実施。

### 過去版からの更新の受入

- 対応を宣言する更新元: 全公開版からの更新経路を `tests/fixtures/schema_upgrade/` で追跡。origins に直近公開タグを含む3件を同tagの実コードから生成したDDL fixture と hash で追加済み: v1.1.2 `ff038ef8`→`shape-16.sql`（72件の正規化DDL定義）、v1.2.0 `204cbdbb`・v1.2.1 `4a97288d`→`shape-17.sql`（74件。2版は対象ソースがbyte同一で同一 fixture）。SQLite内部テーブルとFTS5 shadowテーブルは既存 fixture の慣行どおり除外。
- `update-paths.json` の target は v1.3.0。`test_supported_update_paths.py`・`test_supported_schema_upgrade.py`・`test_release_origin_tick.py`: 616 tests 合格（2026-11-24 実測）。fixture 追加直後の初回実行では生成DDLの順序・内部テーブル取り込みが原因で66件失敗し、fixtureのDDLを修正して同項目のみ再実行で合格（失敗後の再検証）。実行したツリーは、fixture 追加を含む作業ツリー＝コミット `3520c87`（fixtures・test更新）と `f4b35f7`（受入票・図版hash）の内容。コードは push 済み HEAD と同一。
- 合成環境で確認した範囲: 更新コードの実入口・plan/apply・事前バックアップ・設定・保存データの保持・rollback・失敗時の復旧（runtimeの再起動・サービス監視はスタブ）。再起動後の診断・代表機能の稼働確認と実機の本番DBでの適用は未実施（別承認）。

## Issue #198の範囲限定独立QA

対象は候補SHA `6845138a8c027f2a68cc7290650497434573c3fd` の完全合成記録で、通常の臨床記載に限ります。

| 確認項目 | 結果 |
|---|---|
| 薬用量を検査値として誤確定 | 0/480 |
| 通常の検査値が結果から欠落 | 0/12 |
| 連続する検査値の単位混同 | 0/3パネル |
| 非血液検体で値・単位を保持 | 18/18。血液への誤分類 0/18 |
| 検体節の後に続く血液検査 | 6/6 |
| 関連するローカル機能テスト | 3,380件合格との報告 |

[Reliability QAの結果](https://app.devin.ai/sessions/6cbdca77960645d7978b33ef5698e8f8)はこのSHA・上記範囲に限ります。Issue #198全体の受入やv1.3.0の公開合格を示すものではありません。性能78項目、LLM候補生成、実データ、実サービス、脆弱性再現、負荷試験は対象外で、追加の機能QAは重複実行していません。

## 未実行・残条件

- PR #357のCI（候補SHA `6845138a8c027f2a68cc7290650497434573c3fd`）: 2026-11-24時点で16成功・0失敗・2 pending（`pytest-linux (4)`、`merge-gate`）、4 skipped。文書追補後のPR HEADについてCIを再照合する。
- 独立レビュー（Devin Review）: PR に付く見込み。指摘があれば対応を記録する。
- tag `v1.3.0` の作成・GitHub Release の公開・説明図 PNG の release asset 添付と到達・表示確認: 未実施。本文中の画像URLは公開後に照合が必要。
- 新規導入の受入（fresh-install ワークフロー相当）: 未実施（上記）。
- 本番適用・常駐サービスの再起動: 未実施。「更新時の注意」に記載の再起動条件は公開後の適用時に従う。
- QA_VERSION切替後の実際の再判定所要時間、サービス再起動後の自動進行、実画面での復帰は未検証。合成probeの対象に既存の品質不合格記録はなく、そのケースの再試験も未実施。
- Issue #198: 上記の範囲限定QAで機能正確性の一部は確認済みですが、Issueはopen/reopenedのままです。除外した性能・LLM候補生成等を含むIssue全体の受入とは区別します。
- リリース範囲: `changes/drug-alias-over-limit-cache.json`（#356）、`changes/launchd-unknown-agent-state.json`（#352）、`changes/cloud-unresolved-reselect-20261124.json` は未アーカイブです。一方、候補SHAは最新mainを含むため、これらのソース変更もタグに入ります。34件の記録だけを対象とする従来の説明と実際の候補ツリーに差があり、v1.3.0への収録境界を確定するまで実機適用・公開を保留します。
- PR #350/#365の候補差分: PR #365は未マージで候補に含まれません。候補と#365の差分・関連コードから、次の4点を分けて確認しました。
  - モデル記録: 候補のJev成果物・使用量記録は`jev.JEV_MODEL`を記録します。非既定`semantic.model`を選択した場合に実モデルと表示・記録がずれる残件です。
  - 外部通信許可: 候補はモデル一覧取得前に利用可否を確認しますが、予約後・送信直前の再確認はありません。確認後に設定や許可が変わるとGETが続く可能性があり、既定モデルの環境でも起こり得ます。
  - 承認範囲: 候補の`candidate_fingerprint`は選択モデルを含まず、切替処理にも`fact_source=canonical`の拒否がありません。canonicalの承認済み証跡とモデルを結び付けられず、切替後に旧証跡が再利用される可能性があります。初期値が既定モデルかどうかだけでは判定できません。
  - 中断時の記録: 候補は切替設定を保存してから承認receiptをcommitします。間でプロセスが中断すると、設定変更とreceiptが不一致になる可能性があり、非既定モデル利用の有無にかかわらず切替操作が対象です。
  これらは静的な候補差分の確認であり、実機設定や実サービスでの検証ではありません。対象設定は調査していません。通信許可・承認範囲・中断時の回復確認が終わるまで取込・適用を保留します。
- 公式医薬品マスター: 2026-11-24に最新版20261112・19,324件を照合した既存証跡と、`activation: false`・オペレーター承認必須のmanifestを保持しています。この追補では公式サイトから再取得していないため、公開前に更新有無を再確認します。
- ローカル取込・本番適用・利用者試用: Local Handsの接続設定のみ行い、端末の走査・変更はしていません。対象リポジトリ／本番適用先の同一性確認、取込、適用、通常サービス再起動は未実施です。rollbackは既存更新ツールと事前backupを使う手順を合成環境で確認していますが、実機では未実施です。適用時は更新前backupを確認し、問題があれば`"$PY" "$REPO/mcs/ops/mcs_update.py" rollback`を使います。schema変更に伴うDB復元では別途人の承認が必要な場合があり、コードだけを戻す操作で代用しません。
- tag `v1.3.0` 作成・GitHub Release公開は、Yusukeの実使用後の明示OKまで行いません。

## 参照

- 変更の要点と収録物: [README.md](README.md)
- 全34件の対応表: [change-map.json](change-map.json)
