# v1.3.0 受入準備票 — 未公開

この票は v1.3.0 の変更記録と説明資料を現在の候補へ準備した作業の記録です。候補全体の合格、本番適用、公開の完了を示すものではありません。

## 現統合ツリーの確認（2026-11-25 JST）

- PR #368の実統合commitは `b1f7aeb70b4424e55a5efda659f1c5b4cf1f6fb5`（親 `4dc9f0c95696efcc0cb6313a90176205a44c78df`）。通知head `6cd71a39aa5020f0129fbb8d94253c7770258a49` とmerge commitはtree `42b2b6d91e2e7a57a2f99d6dd9324b111ae12c27` で一致。現main `e0da88eb5baf989f4418b9d78cdd074f9aff888b` と比較して `mcs/`・`tests/`・`deployment/`・`resources/` の差分はない。
- PR #368 head `6cd71a39aa5020f0129fbb8d94253c7770258a49` の更新済みcommit checksは22成功・0失敗・1保留・10 skip。保留は必須 `merge-gate`。実merge commit `b1f7aeb…` は4成功・0失敗・0保留・2 skipだが、全機能テストを示すものではない。headとmergeのtreeは同一なので、保留gateを統合treeの合格へ読み替えない。
- PR #357はこの統合mainを基準に54件のリリース資料を更新する。候補PR差分は変更記録・対応表・CHANGELOG・README・受入資料で、患者機能のコードはmainに既にある。PR #368のマージはリリース候補の受入・実機適用・公開承認ではない。
- PR #371は旧#368履歴をmainへ接続するopen PR。head `bb3df8f52e09faec867400060f16693a0c2d5148` のtreeはmain `e0da88e…` と同一（差分なし）で、mainと元head `6cd71a3…` を親に持つ。CIは10成功・0失敗・9保留。機能追加でも必須・マージ済みのゲートでもない。実機取込前に対象端末の現行commitと来歴接続を確認するが、端末SHAは未確認。
- 旧37件の記録IDとSHA256はPR #357 head `937a429bdaf81b5621d2f36588cd01534a84e5a0` と一致。追加17件を含め、archiveとchange-mapのタイトル・SHA256を照合済み。記録を揃えたことは実装検証の代わりではない。
- `jev-health-qc-recovery` と `semantic-unread-verdicts` の2記録は当初54件の外に残っていたが、その後の範囲確定で残り8記録とともに1.3.0へ収録した（62件）。この記録のSHA・件数は収録前の時点のもので、範囲不一致は解消済み。
- さらに後からmainへ入った24件（cloud再試行統一・意味解析assist既定化・独立接続SDKのSHA256照合など）を1.3.0へ収録し、計86件となった（2026-11-26）。変更記録の収録だけで、実装検証・受入の完了ではない。

## 版番号の提案記録

- 比較元（直近の公開済み版）: タグ `v1.2.1` = `4a97288dd74f356c8bb2f7191721819f0a1fdaf3`（CHANGELOGのv1.2.1節と照合済み）。本番反映SHAはこの記録では確認していない。
- 対象: 基準37件に患者コンテキスト3件とその他14件を追加した計54件。分類は新機能12・改善12・不具合修正30。対象範囲の決定は、CI合格・本番適用・Release公開の承認ではない。
- 推奨番号: `1.3.0`（機能アップデート版）。最大区分は MINOR。
- 根拠: 在宅ケア・医薬品検索に加え、患者コンテキストの品質確認と患者選定を改善し、抽出・添付・クラウド処理・通知・スタンプ等の変更を含む。非互換なCLI/JSON/設定/保存データの変更は記録に無い。内部改善の件数・行数・schema番号は区分の代理に使っていない。
- 互換性・必要な操作: `QA_VERSION` 1→2で旧PASSは保存されますが現行版の確認として再利用されず、背景再判定まで患者概要が一時的に非表示になります。通常の更新手順に含まれるサービス再起動・設定確認と、薬剤マスターのオペレーター承認（配布のみで有効化は別承認）は従来どおりです。患者ごとの手動再判定は不要ですが、実サービスでの再起動後の進行・表示回復は未確認です。
- 未確認事項: 最新統合treeの保留CI、Issue #198現行候補への差分QA結果（Reliability QA実施中）、54件外の2記録とmainコードの範囲不一致、fresh-install、実機対象の同一性・適用・rollback、本人の実使用・明示Release承認、tagと画像assetの公開到達確認。

## 今回の対象

- 基準37件を維持し、患者関連3件を追加: `patient-context-quality-concurrent-writes`、`patient-context-quality-foreign-commit`、`patient-context-selection-concurrency`。それぞれ品質確認の並行更新、保存直前の入力再照合、患者選定ロックの競合を扱う。
- その他14件: `160-lab-quote-comparison-unverified`、`attachment-local-deferral-engine-reason`、`cloud-price-recheck-20261124`、`drug-map-deadline-deferred`、`export-archived-patients-20261124`、`karte-scope-index`、`local-rebuild-backlog-priority`、`manual-extraction-upgrade-scope`、`ocr-ack-reboot-identity`、`own02-env-private-read`、`semantic-hold-health-backoff`、`stamp-actor-partial-metadata`、`stamp-reconcile-expiry-20261124`、`tracking-ack-proof-columns`。
- `changes/archive/1.3.0/` と `change-map.json` は54件。先行37件のhashは変更せず、追加17件の各hash・タイトルもarchiveと照合した。カテゴリ合計は新機能12・改善12・不具合修正30。
- `jev-health-qc-recovery` と `semantic-unread-verdicts` は追加対象外。いずれもactive recordのままで、mainには関連コードが存在するため、記録と候補ツリーの整合は未解決。
- PR #367由来の品質確認変更とPR #368内の患者選定修正は、実merge commit `b1f7aeb…` に含まれます。Software Factory由来と別個の未特定ブランチを追加で取り込む作業はしていません。
- 公式マスター、比較表・合成説明図、公開本文、README、受入資料は既存の同一版の素材を更新しています。公開時のPNG asset配置・到達確認は未実施。

## 過去候補で確認済み（現統合ツリーの受入ではない）

以下は2026-11-24時点の旧候補・旧headに対する記録です。#368統合treeとは別SHAであり、今回の54件候補や現行mainに対する受入証明ではありません。実行日は記載どおり。

- `release_notes.py check`: 合格（CHANGELOG構造・版別詳細ファイル・画像URL）。
- `release_notes.py build --version 1.3.0 --date 2026-11-24`: 既存版を上書きしないようcleanな一時worktreeで生成。候補側で更新済みの `jev-optional.json` と `semantic-drain-throughput.json` を用い、archive 37件がPR作業ツリーと完全一致することを確認。
- `release_notes.py export --version 1.3.0` で出力した本文とタイトルは `release-notes-1.3.0.md` と `release-title-1.3.0.txt` に収録しています。公開本文は37件をまとめた `21561f51c` で113,640バイト、Jevの説明を修正した `c0c2dd137` で114,939バイト、抽出QCの説明を統合した `64d3e62f9` で115,091バイトです。いずれも公開本文の上限内です。
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
- このprobeは旧PASSがある患者（過去の不合格なし）の表示回復を確認したもので、過去に品質不合格がある患者の回復を確認したものではない。過去の不合格がある患者はJev対象外でもSKIPPEDへ切り替えて公開せず、保留を維持する（変更記録上、全ての未実施理由に適用）。`test_prior_negative_quality_answer_cannot_be_converted_to_skip` は日次予算0での変換拒否を検証するが、Jev対象外での回復は試験していない。そのケースの回復経路・表示回復は未確認。
- `patient_context_quality_identity` は意味解析候補のソース指紋に含まれるため、QA_VERSION 1→2 で候補指紋も変わります。`semantic.fact_source=canonical` で承認済みの環境は意味解析が止まるため、[昇格ガイドの「Fact source modes」](../../specs/semantic-facts-v2-rollout.md#fact-source-modes)に従って再評価・再承認してください。実機設定は確認しておらず、承認の変更もしていません。
- `release_owner_source.py --check` は `41da58883` のソースで合格（digest `0602f939…`）。その後にmainから取り込んだ変更を含む PR #357 の `94a2b3237` のソース660件から、manifestを再生成しました（digest `9b0127a4…`）。

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

2026-11-25時点で、現候補のコードSHA `eef171b37423df490c8e8ce537671eb21dbf0664` では臨床値抽出と関連テストが変わっています。旧QAの結果を流用せず、差分の影響を受ける既存の合成ケースのみReliability QAが再確認中です。結果は未着で、このPRから重複実行していません。

## 未実行・残条件

- PR #368統合tree `42b2b6d91e2e7a57a2f99d6dd9324b111ae12c27` は旧候補より `mcs/extract/clinical/clinical_values.py` と関連テストが変わった。head/mergeでtree同一だが、headの必須 `merge-gate` は未完了。
- Issue #198の旧独立QAは旧候補 `6845138a8c027f2a68cc7290650497434573c3fd` に限ります。現統合treeでは `mcs/extract/clinical/clinical_values.py` と関連テストが変更されたため、旧QAの数値だけで現候補を合格とはしません。現候補コードSHA `eef171b37423df490c8e8ce537671eb21dbf0664` の差分限定QAをReliability QAが実施中で、結果待ちです。重複QA・全suiteの追加実行はしていません。
- 旧head `937a429` の資料テスト（59 tests + 128 subtests）は追補前の履歴。2026-11-25の現作業treeでは `release_notes.py check`、`readme_release.py --check`、`update_readme.py --check` が成功し、`scripts/run_tests.sh tests/release/` は63 tests・128 subtestsが成功。これはローカル文書・release testの結果で、main統合tree CIや全体受入ではない。
- `jev-health-qc-recovery` と `semantic-unread-verdicts` は選択54件外のactive recordsで、mainの関連コードを含む。どちらを収録するか、または候補からコードを除くかが未決定。現状を54件のみの完全なRelease範囲と扱わない。
- PR #365の修正は現統合treeに含まれる。旧PRではCI 24成功・0失敗・9 skipを記録しているが、現統合treeのCIは上記のとおり保留中。旧Devin Reviewの指摘・資料対応記録は後段に残し、未登録triageを代理承認していない。
- Issue #198の限定QAは薬用量の誤確定0/480、通常検査値の欠落0/12、単位混同0/3パネル、非血液検体の保持18/18・誤分類0/18、節後の血液検査6/6、関連機能テスト3,380件成功との報告。完全合成・旧SHAに限り、性能、LLM候補生成、実データ・サービス、負荷、脆弱性再現は対象外。Issue・Release全体の合格ではない。
- 新規導入の受入（fresh-install workflow相当）: 未実施。
- QA_VERSION切替後の実際の再判定所要時間、サービス再起動後の自動進行、実画面での復帰は未検証。合成probeは再判定処理を直接進めており、サービス監督を含む統合動作や過去不合格記録の再試験はしていない。
- 公式医薬品マスター: 2026-11-25に[公式download menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)と現在適用の[医薬品master](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile)を再確認し、`yFile`から公式ZIPを取得。archive 872,908 bytes・SHA256 `5412101070ad7487beed6255abcaa11fe315c589cee2c7d774aae71971dc64b5`、唯一のmember `y_20261112.csv`、CSV 6,098,622 bytes・SHA256 `835538c20a35c73a24af5bbbecef4b65fac1a7837b9e8a170691f43ff0515550` が同梱manifestと一致。edition `20261112`、19,324件。`check_drug_master_bundle.py`も成功し、`activation: false`、オペレーター承認必須、`conversion_held: terms_unconfirmed`を確認。
- ローカル取込・本番適用・利用者試用: Local Handsの接続設定のみ行い、端末の走査・変更はしていない。対象リポジトリ・本番適用先・権限の同一性確認、取込、適用、サービス再起動は未実施。rollback手順は合成環境の既存更新ツールと事前backupで確認したが、実機では未実施。
- tag `v1.3.0`・GitHub Release公開は、実機適用後にYusukeがしばらく使用し、本人が明示的に承認するまで行わない。

## PR #357 のコミット `21561f51c` で指摘された5件の対応状況

- 対象: PR #357 head `21561f51c05f2535c699078f80a9a0092bfe5ed3` に対するDevin Review `pr-review-job-37119e02329f4248a11eee1298345c80` の未処理5件。資料・証跡の対応と、Devin Review上の判定登録は別の状態として扱う。
- 公開本文の比較表・説明図なし: CHANGELOGに比較表とRelease添付URLを追加し、export本文にも反映。
- Jev取得失敗時の表示説明: `mcs_setup.py check` の通知と、引数なし `mcs_setup.py jev-model` のJSON出力をCHANGELOG・版別詳細で区別。
- Jev一覧取得条件: 施設確認でJevが許可されている条件を変更記録・更新時の注意へ追記。
- 34件/37件の不一致: 当時のPR本文と受入票を37件（12/8/17）へ統一した。現在の54件スコープは冒頭の最新記録を参照。
- 公式ZIP照合: 2026-11-24時点の確認記録。現在の公式照合は冒頭の2026-11-25記録を参照。
- Devin Review上の5件の判定登録: 未登録。登録操作はHTTP 403で拒否され、PRのtriage担当は旧Release Manager側のセッションと確認された。代理判定・承認・ゲート変更は行っていない。

## PR #357 のコミット `a01066d2f` のレビュー記録

- head `a01066d2f4bdc78ba51c530d84218a913c90cc1d` のDevin Review `pr-review-job-e0abc6c08987494e968646dc766eeaf6` は完了。指摘3件のうち2件は前回レビューで既にrejected、1件は `jev-model` JSONの `model` が現在の設定値である説明不足で、今回CHANGELOG・変更記録を修正した。
- このレビューの判定登録も未実施。旧triage担当セッション以外から操作しない。通常のPRレビューと必須CIはGitHub上の現行headで別に確認する。

## 履歴: PR #368 head `79cc372dd` と PR #357 の比較（マージ前）

- 当時のPR #368 head `79cc372ddf755cce415ed258efa28c85e910c62f` と各PRとの比較履歴。後に実統合されたSHA・現候補の状態は冒頭の最新欄を参照。
- PR #357 の `c0c2dd137` と PR #368 の `79cc372dd` では、archiveの37ファイルがすべて一致しました。それぞれの対応表で、全37件のSHA256がファイルと一致しました。
- 当時、37件の外に患者コンテキスト変更記録が3件あった: 別患者の更新時も対象入力が一致すれば品質確認を続ける、保存直前に原文・回答・概況を再照合する、候補予約後に患者選定ロックを解放する。記録は `patient-context-quality-concurrent-writes`、`patient-context-quality-foreign-commit`、`patient-context-selection-concurrency`。現在の対象・証跡は冒頭を参照。
- 当時、患者コンテキストとは別に14件の未archive記録があった。抽出・添付再処理、薬剤・検査値、semantic待機、通知/スタンプ照合、archive患者export、`.env` 権限等。現在の54件への追加は冒頭の対象表を参照。`homecare-local-backup.json` は当時から既存37件のarchive側にあり、新規追加ではなかった。
- PR #368本文に記された83件・254件のテストは各統合元の実行結果で、head `79cc372` の合格証跡ではない。この履歴記録の更新では読み取り比較だけを行い、#368のテストは実行していない。最新のCI状態は冒頭を参照。

## PR #357 head `94a2b3237` のDevin Review

- レビューは3件。Jev対象外で過去に品質不合格がある患者は、確認なしのSKIPPED公開を避けるため保留を維持する。旧PASSだけを使った合成probeとは別ケースで、回復・表示は未確認。
- `patient_context_quality_identity` のソース変更は候補指紋に影響し、canonical承認済み環境には再評価・再承認が必要。更新の影響は[変更記録](../../../changes/archive/1.3.0/semantic-assess-churn.json)に、再評価に必要な証拠と人による再承認の手順は[昇格ガイドの「Fact source modes」](../../specs/semantic-facts-v2-rollout.md#fact-source-modes)に記載している。
- 受入票見出しが記録対象SHAと不一致という指摘は、この追補で `a01066d2f` の記録へ修正した。旧レビュー5件のtriage未登録とは別のレビューで、代理triageは行っていない。
- このheadのGitHubレビューイベントには人によるレビューを確認できず、取得できたbranch protection情報では必須承認数0。現headの通常レビュー・必須CIは下記で別記する。

## PR #357 head `3617e35bf` のCI・Devin Review記録

- CI確認時点で10成功・0失敗・9保留・9 skip。pytest-linux 4 lane、pytest-macos 2 lane、hermes-integration、incident-gates、merge-gateが保留中で、このheadの必須CIは未完了。
- Devin Reviewは3件で完了（bug 1、flag 1、info 1）。Jev対象外で過去に品質不合格がある患者は保留され、表示の回復経路は未確認。安全判定は変更せず、受入可否も未判断。
- 品質確認モジュールのソース変更で候補指紋が変わり、canonical承認環境では再評価・再承認が必要。影響の変更記録と人による再承認手順を本票から参照する。実機設定は未確認。
- info所見は候補ソースmanifestの660ファイルとdigest `9b0127a4…` の一致確認。`release_owner_source.py --check` はこのcleanなheadで成功。
- GitHubレビューイベントには人によるレビューを確認できず、取得できたbranch protection情報では必須承認数0。自動レビュー所見のtriage判定は登録していない。今回の受入追補後の新headのCI・レビューは別途確認する。

## 参照

- 変更の要点と収録物: [README.md](README.md)
- 全54件の対応表: [change-map.json](change-map.json)
