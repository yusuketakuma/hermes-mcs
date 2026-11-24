# v1.3.0 受入準備票 — 未公開

この票は v1.3.0 の変更記録と説明資料を現在の候補へ準備した作業の記録です。候補全体の合格、本番適用、公開の完了を示すものではありません。

## 版番号の提案記録

- 比較元（直近の公開済み版）: タグ `v1.2.1` = `4a97288dd74f356c8bb2f7191721819f0a1fdaf3`（CHANGELOG の v1.2.1 節と照合済み）。本番反映SHAはこの記録では確認していない。
- 対象SHA: QA_VERSIONの合成probeは過去の候補 `27f2619`、Issue #198の範囲限定独立QAは候補 `6845138a8c027f2a68cc7290650497434573c3fd` で実施。PR #365の最終head `d71f58d936265aeff973d1bc5f348f367c4dd086` はmain merge commit `76e2e2d65e993adf07d049fcdef15c58b0365383` に含まれます。37件更新後のPR #357 head `21561f51c05f2535c699078f80a9a0092bfe5ed3` では確認時点でCI 21成功・0失敗・1 pending（merge-gate）・10 skip。レビュー追補後は新しいheadで再確認する。
- 推奨番号: `1.3.0`（機能アップデート版）。最大区分は MINOR。
- 根拠: 未リリース変更記録37件の内訳は新機能12・改善8・不具合修正17。#352/#358の常駐処理誤起動防止、#356の薬名辞書再走査防止、#359のクラウド再処理防止を含む。#365は既存のJevモデル切替記録を更新したもので、件数を増やしていない。非互換なCLI/JSON/設定/保存データの変更は記録に無い。内部改善の件数・行数・schema番号は区分の代理に使っていない。
- 互換性・必要な操作: 保存済みPASSは削除しませんが、`QA_VERSION` 1→2で旧PASSは現行版の確認として再利用されず、背景再判定まで概要が一時的に非表示になります。通常の更新手順に含まれるサービス再起動・設定確認と、薬剤マスターのオペレーター承認（配布のみで有効化は別承認）は従来どおり必要です。QA_VERSION は内部の版マーカーで、担当者が手動変更する操作ではありません。患者ごとの追加操作は不要で、稼働中のresident workerが背景再判定を進めます。
- 未確認事項: PR #357最終headに対する必須CI、PR #367とSoftware Factoryで依頼された患者コンテキスト改善の関係・対象、実機の対象端末/本番適用先/接続権限の同一性、ローカル取込・適用・本人の実使用、tag `v1.3.0`・GitHub ReleaseとPNG assetの到達確認。

## 今回の対象

- v1.2.1（タグ 2026-11-22）以降の変更記録37件を `changes/archive/1.3.0/` へ収録。機能アップデート版として番号 `1.3.0` を指定（所有者 2026-11-24）。
- 内訳: 新機能12件（在宅ケア台帳・職員・ケアチーム・訪問歴・処方歴・訪問ルート・常駐モード・ローカルバックアップ対象・医薬品検索共通入口・患者横断検索・照会返答待ちアラート・日次サマリー処方期限一覧・Jev要求内訳表示・ローカル患者画面の表示とMCS引き渡し）、改善8件（Jev 1.14.0・モジュール整理・QC再抽出・抽出QCのまとめ処理など）、不具合修正17件（既存14件に#352/#358・#356・#359を追加）。
- CHANGELOG の v1.3.0 節と公開Release本文を生成し、同じ比較表・説明図PNGを掲載。README のリリース案内も生成。図は合成の説明図で実画面ではない旨を記載し、公開URLの到達・Release画面での表示は未確認（tag/asset未公開）。公開本文は上限内。
- 説明図の来歴とhashは `visual-source.json`（PNG 1960×840 の sha256 記録済み）、37件の対応表は `change-map.json`。
- PR #367（head `54d435b5c86a7cc10dd5073ac4074d2b041279fd`）は別候補として管理し、この37件には含めない。PR本文に記された稼働Mac側の未pushブランチ `codex/patient-context-progress-20261124` と患者選定ロック案は、対象・内容ともこちらでは照合していない。PR #367とSoftware Factoryで依頼された改善の対応関係は未確定。担当側で関連テストの旧head比較中のため、重複実装・テスト・実機変更・取込は行わない。
- `docs/development/readme-review.json` を 1.3.0 へ更新し、README本文の見直し結果を記録済み。

## 検証済み（この作業の範囲・ローカル実測）

対象はPR #357の `devin/release-1.3.0-prep` ブランチ。main merge commit `76e2e2d65e993adf07d049fcdef15c58b0365383` と37件の変更記録を含みます。最終head SHAと必須CIはこの資料更新をpushした後にPR上で確認します。実行日は2026-11-24（JST）。

- `release_notes.py check`: 合格（CHANGELOG構造・版別詳細ファイル・画像URL）。
- `release_notes.py build --version 1.3.0 --date 2026-11-24`: 既存版を上書きしないようcleanな一時worktreeで生成。候補側で更新済みの `jev-optional.json` と `semantic-drain-throughput.json` を用い、archive 37件がPR作業ツリーと完全一致することを確認。
- `release_notes.py export --version 1.3.0`: 本文・タイトル出力成功。`release-notes-1.3.0.md`・`release-title-1.3.0.txt` に収録（本文115,091バイト、上限内）。
- `update_readme.py`: README生成ブロックは生成済み状態と一致（release・modules・signals・stats・cli）。
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

- PR #357の最終headに対する必須CI: 37件の資料更新をpushした後、正確なhead SHAのrequired checksを確認する。旧候補 `6845138a...` の16成功・2 pending結果や、別PRの成功を統合候補のCIとして流用しない。
- 既存の3修正PRの証跡: #358（常駐処理）は最終head `d60fd48b4` で関連テストとCI 24件成功、#356（薬名辞書）はCI 21成功・0失敗・9 skip、#359（クラウド再処理）はCI 22成功・0失敗・8 skip。これらは各PRのheadに対する既存証跡で、37件を含む#357候補のCI成功の代替ではない。
- PR #365統合: 最終head `d71f58d936265aeff973d1bc5f348f367c4dd086` はmain merge commit `76e2e2d65e993adf07d049fcdef15c58b0365383` に取り込まれ、候補ツリーに含む。既存PR CIは24成功・0失敗・0 pending・9 skip。関連対象テスト135件は最終headと変更記録の記述差1行の `8d74b2724` で合格。旧モデル名の誤ラベル評価を使用中モデルで再評価する回帰テストも追加済み。
- #365のDevin Review: 最終headのレビュー結果コメントは5件の自動修正を記録。成果物・使用記録のモデル整合、送信直前の許可再確認、canonical切替拒否とモデル束縛、receipt先行保存と同じcommand-idの再実行、canonical再評価・再承認手順をコード・記録・ガイドで確認した。旧モデルQCジョブは`config_generation`と実行時guardで送信/書込み前に停止するとのレビュー回答があり、既定の役割として扱う。親ディレクトリfsyncの失敗を注入した耐久性試験は既存証跡で確認できず、読取り可能性と永続性を分けた実機検証も未実施。
- Issue #198の限定QAは旧候補 `6845138a...` で実施。#365から同候補までのコード差分は`mcs/core/`、`mcs/ops/`、`mcs/semantic/`と対応テストに限られ、QA対象の抽出処理 `mcs/extract/` は変更されていない。薬用量・検査値・検体・単位に関する範囲限定結果は対象経路に引き続き関係するが、IssueはopenのままでありIssue全体・release全体の合格ではない。
- Software Factoryの患者コンテキスト改善: 対象PR/Issue/SHAは未特定。0%表示は調査中の意味で、#366は別件との連絡を受けた。追加分はYusukeの対象特定と実装完了を待ち、現在の37件とは別に記録・検証する。最終収録範囲は未確定。
- 新規導入の受入（fresh-installワークフロー相当）: 未実施。
- QA_VERSION切替後の実際の再判定所要時間、サービス再起動後の自動進行、実画面での復帰は未検証。合成probeは再判定処理を直接進めており、サービス監督を含む統合動作や不合格記録の再試験はしていない。
- 公式医薬品マスター: 2026-11-24に[公式download menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)を確認し、`yFile`から公式ZIPを再取得。archive 872,908 bytes・SHA256 `5412101070ad7487beed6255abcaa11fe315c589cee2c7d774aae71971dc64b5`、唯一のmember `y_20261112.csv`、CSV 6,098,622 bytes・SHA256 `835538c20a35c73a24af5bbbecef4b65fac1a7837b9e8a170691f43ff0515550` が同梱manifestと一致。edition `20261112`、19,324件。`check_drug_master_bundle.py`も成功し、`activation: false`、オペレーター承認必須、`conversion_held: terms_unconfirmed`を確認した。
- ローカル取込・本番適用・利用者試用: Local Handsの接続設定のみ行い、端末の走査・変更はしていない。対象リポジトリ・本番適用先・権限の同一性確認、取込、適用、サービス再起動は未実施。rollback手順は合成環境の既存更新ツールと事前backupで確認したが、実機では未実施。
- tag `v1.3.0`・GitHub Release公開は、実機適用後にYusukeがしばらく使用し、本人が明示的に承認するまで行わない。

## PR #357 のコミット `21561f51c` で指摘された5件の対応状況

- 対象: PR #357 head `21561f51c05f2535c699078f80a9a0092bfe5ed3` に対するDevin Review `pr-review-job-37119e02329f4248a11eee1298345c80` の未処理5件。資料・証跡の対応と、Devin Review上の判定登録は別の状態として扱う。
- 公開本文の比較表・説明図なし: CHANGELOGに比較表とRelease添付URLを追加し、export本文にも反映。
- Jev取得失敗時の表示説明: `mcs_setup.py check` の通知と、引数なし `mcs_setup.py jev-model` のJSON出力をCHANGELOG・版別詳細で区別。
- Jev一覧取得条件: 施設確認でJevが許可されている条件を変更記録・更新時の注意へ追記。
- 34件/37件の不一致: 現行PR本文と受入票は37件（12/8/17）に統一。レビュー指摘は旧PR本文に対するもの。
- 公式ZIP照合: 2026-11-24に再取得し、archive・CSVのbyte数/SHA256とmember名がmanifestに一致することを上記「公式医薬品マスター」に記録。
- Devin Review上の5件の判定登録: 未登録。登録操作はHTTP 403で拒否され、PRのtriage担当は旧Release Manager側のセッションと確認された。代理判定・承認・ゲート変更は行っていない。

## PR #357 のコミット `a01066d2f` のレビュー記録

- head `a01066d2f4bdc78ba51c530d84218a913c90cc1d` のDevin Review `pr-review-job-e0abc6c08987494e968646dc766eeaf6` は完了。指摘3件のうち2件は前回レビューで既にrejected、1件は `jev-model` JSONの `model` が現在の設定値である説明不足で、今回CHANGELOG・変更記録を修正した。
- このレビューの判定登録も未実施。旧triage担当セッション以外から操作しない。通常のPRレビューと必須CIはGitHub上の現行headで別に確認する。

## 参照

- 変更の要点と収録物: [README.md](README.md)
- 全37件の対応表: [change-map.json](change-map.json)
