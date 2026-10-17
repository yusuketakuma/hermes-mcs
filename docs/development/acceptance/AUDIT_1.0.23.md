# 1.0.23候補の初期調査と次期計画

> この文書は初期調査時点の履歴です。後続指示で実装範囲を拡張しました。現在の不具合修正・全項目・未検証事項は [1.0.23受入票](ACCEPTANCE_1.0.23.md) を参照してください。下記の「未修正」「最終結果」や旧レイアウト移行の記述を、現在候補の状態として使わないでください。

2026-10-17。対象は `release/v1.0.23-prep` の `5d71c1a0d7e51d8da1aec2bd6006626b68a26ac6`、比較元は `v1.0.22 / f8c6c20d`。
この票は調査・計画の記録であり、リリース受入票ではない。現行の優先順位は [ROADMAP](../../ROADMAP.md) に置く。
実装・合成検証・実環境受入・有効化・公開を区別する。後続のユーザー指示で追加された返信要約の表示変更は、関連する表示更新の欠落も含めてローカル実装・検証を完了した。

## 結論と範囲

配送と運用案内を先に安定させ、次に証拠に基づく個別復旧、品質受入、薬剤言及履歴へ進める。
人手評価や復元訓練を機能追加で置き換えず、既に実装された診断・backup・患者状態参照・構造化同期は再実装しない。

Codex親が仕様・既存計画・受入・差分を統合し、独立した3子担当が配送/運用、データ/品質、導入/配布を読取り監査した。
実投稿・患者情報・秘密設定・私有ログは調査やfixtureに使わず、実サービス・本番DB・実LLMには接続していない。OpenWikiは不使用。
監査対象の8差分ファイルは下記2コミットから統合済み。主要ops・install・deployment・CI・SDK scriptは前版とbytes同一であることを確認した。

| 既に統合した修正 | 根拠 | 今回の位置づけ |
|---|---|---|
| Slack profile所有者制限 | `4237548b`、`hermes_plugin/card_workers.py`、通常/再接続の回帰 | 正しいowner以外はSupervisor作成前に拒否。固定SDKの実属性契約は別確認 |
| 分割規則の版・更新手順 | `95861469`、`clinical_chunking.PLAN_VERSION=2`、UPGRADE_AGENT | v4 checkpoint/contextとsemantic fingerprintまで共有。全件強制再解析ではない |

## 確認した不具合

優先度P2は影響の順位であり、公開例外の承認ではない。今回の配送安定化の目的に直接関係するため、B-01/B-02は1.0.23の公開前に解消する計画とする。
行番号は監査基準SHAのもの。証明用テストの成功は、不具合が発生するという観測の成功であり、修正成功ではない。

| ID | 条件・影響 | 根拠と検証 | 処置・受入 |
|---|---|---|---|
| **B-01 / P2** | 結果不明のpartで照合待ちでも「次回の収集で送信されます」と警報が案内する。待つだけで回復すると誤認させる | `mcs/ingest/health_watch.py:602,647,773`。両runtimeで照合待ちと次回送信の同時表示を完全合成2例で確認 | 待機理由に対応した次操作へ。unknown/held/上限/通常待機/混在/古い観測/欠測を区別。再送条件は緩めない。未修正 |
| **B-02 / P2** | HermesのDiscord factoryが別プロファイルを受け入れる。設定application_idが実bot IDより優先され、設定scopeのロックを先に取得すると正しいworkerを阻害し得る | `hermes_plugin/card_workers.py:20,81`。同一/異なるbot IDの2例で別ownerからSupervisor起動を確認。`adapters/common/worker.py:179–224,467–473`、`adapters/discord/tasks.py:102–110`でscope/lock経路を確認 | ownerを構築前に確認し、実bot identityの照合可能時点を固定。別owner先着/後着/正常再接続/ID不一致を回帰。実SDK競合再現は未実施。未修正 |

B-02はfactoryの誤受理を確認したもので、実サービス上の誤送信や漏えいを観測したという意味ではない。
送信結果不明・上限到達による保留自体は安全動作であり、バグ件数に加えない。`ops.card_resolve`はcard本体の既存入口であり、part_idを受けないためpart/noticeの万能復旧機能とはしない。

## 追加された返信要約・表示順の要件

要求: 返信にも要約を表示し、通知先のスレッドを **要約 → スタンプ → 本文** にする。

返信の解析対象除外は確認されなかった。`ledger.py:1149–1195`は返信もsemanticへ登録し、`mcs_queries.ELIGIBLE_MESSAGE`と`extract_llm._selection_sql`は親投稿限定ではない。
`notify_cards.py:1187–1191`がスレッドの要約を明示的に省略し、`notify_render._message_post`のlayout2が本文の後ろにスタンプを置いている。
これはコード経路の診断であり、ユーザーが提示した個別投稿の実際の解析完了状態は照会していない。

実装の受入条件:

- 親/返信それぞれのIDに対応する要約を使い、未完成/失敗を別表示する。schedulerの重複追加や強制再解析をしない。
- 共通描画で順序を揃え、スタンプなしの場合は行を省略。本文の欠落・削除・未取得を推測で埋めない。
- 遅い要約完成・現在カードページ外の返信・スタンプ更新・長文分割を反映する。Slack/Discordは既存remote IDへ更新し、余った古いchunkの安全な処置を維持する。
- LINE WORKSは編集不可なので、要約/スタンプの内容が変化した更新本文を追加投稿する。byte同一の本文は既存のhash比較で再送しない。観測時刻だけの変化で投稿を増やさないよう本文のスタンプ時刻は省くが、古い/不明/失敗は保持する。表示版の変更により通常巡回中の既存対象カードも一度更新されるため、LINE WORKSでは変更した本文と更新版カードの追加投稿が発生する。過去の全投稿を無条件に再送する操作はしない。
- unknown/未確定partの保留、送信receipt、route/対象hash、上限と省略表示を維持する。完全合成の回帰とSlack説明図を同期する。

ページ外の返信のスタンプ件数のみが変わったとき、従来のactor情報だけのfingerprintでは更新を見落とす経路も合成で再現した。今回の表示要件に直接関係するため、投稿ごとの実表示をpresentation fingerprintへ含める修正に加えた。最終検証結果は後述する。

## 新機能と受入不足の区別

| 項目 | 現状・根拠 | 次工程 |
|---|---|---|
| 安全な配送復旧 | 保留理由/notice上限/card resolveはあるが、partやnoticeを同じ手順で個別解決する入口は未整備 | まずread-only plan。必要な種別のみ人承認・証拠束縛・receipt・冪等性を追加 |
| 版に束縛したcanonical昇格 | `semantic_policy.py:95–107`はtoken非空検査、`mcs_setup.py:1984–1986`はcriteria版+report digest。model/prompt/PLAN_VERSIONとの照合を確認できない | `semantic-evaluation.md:391`の停止条件として保持。legacy/shadowの現在障害とはしない |
| 人手品質評価 | `evaluation/g6-criteria-v1.json`、`test_canonical_human_review_assets.py`。220合成例はhuman=0/pending、昇格不可 | 人手200以上、held-out、必須/表示/配送recall・evidence closure、silent drop等の現行G6を維持 |
| 患者状態参照 | `patient_state_context.py`、1.0.21実装、既定off。過去200投稿、source束縛した参照 | 実モデルA/Bと有効化判断。新規実装候補から除く |
| 構造化薬歴/観測値 | `project_metadata.py:514`に任意定期同期・12 GET/tick・上限/再開/世代固定、viewにempty/unknown | 実GET/未読保持/権限/ページング・氏名/関連付けの受入後に併記 |
| バックアップ/復元 | `mcs_backup.py`、`mcs_restore.py`、BACKUP guide。encrypt/verify/drill/restore/approve/resumeあり | 媒体・鍵escrow・独立receipt・RPO・端末喪失訓練。新backup engine不要 |
| 薬剤言及履歴/別名承認 | DM-1/2/4は既実装、DM-3/5はRELEASE_1.0.16計画に未着手記録 | 読取のDM-3→影響確認→人承認DM-5。処方正本・薬剤同等性・臨床判断を自動生成しない |
| 配備コードの観測 | doctorはdisk revisionとPID/mtimeを区別しloaded revisionをunknownにできる | owner/profile/loaded codeの無害な証跡は任意改善。PID変化だけを読込み証明にしない |

## 公開前に残る受入

1. B-01/B-02と今回追加の表示変更を検証し、最終差分を固定する。
2. `integration/test_hermes_slack.py:70–74`のhostはowner属性のないSimpleNamespace。現在のSDKテスト成功だけでは新guardを実native adapterで確認したことにならない。固定Hermes `f36c1da0016ce14c9c531cb9514ab4b0b667240d`で接続前の属性設定とowner受入/拒否を別途確認する。実機で確認した別revisionの証拠を流用しない。
3. 更新manifestはtarget1.0.22、source1.0.21まで（22公開元・34現行経路・26歴史経路）。1.0.22原schema同一性を確認してsource/targetを更新し、全宣言経路を追跡する。古いfixtureを削除しない。
4. 新規導入と更新後に、隔離環境で配布物取得・依存・起動・合成入力→保存→出力を確認する。stub化したGit/サービス回帰だけで実配布受入を代替しない。
5. 正式な準備時に公式masterを新しく照合する。今回はoffline bundle検証のみで、公式更新なしと判定しない。
6. 版・変更記録・CHANGELOG/README生成・READMEの5領域確認・受入票、固定候補の全体/SDK検証・CodeRabbit差分レビュー・最終SHA必須CIを揃える。現在のmetadataが1.0.22なのは準備不足でありruntimeバグではない。
7. release workflowのtagイベントはmainのgalleryも検査するが、tag checkout側の検査集合は同一ではない（`.github/workflows/release-notes.yml:39,125–138`）。main≠tagの成功をtagの証拠にしない。最終SHAでgalleryも確認し、検査集合の整合は小さな後続改善とする。

## 8領域の調査範囲

全ファイルの逐語監査や全機能の実行を主張しない。前版の受入記録をbaselineとして、変更と高リスク共有経路を確認した。未読範囲を「改善不要」に置換しない。

| 領域 | 確認したファイル群・経路 | 判定/制約 |
|---|---|---|
| 無駄・リファクタ | 今回8差分、共通描画、chunk版の全共有呼出元、既存backup/updater | 無関係な再実装・行数分割は不要。全repoの到達不能コード解析は未実施 |
| 構成・資産 | mcs/adapter/plugin/standaloneの入口、配布pin、生成画面、旧ROADMAP | 旧計画と現行を分離。履歴/migration/assetsを削除しない |
| 冗長処理・性能 | worker lock/claim、metadataの取得上限、患者状態参照上限、診断・進捗の既存境界 | 性能改善率・本番ETA・長時間無リークは未測定 |
| 正しさ・保存・互換 | ingest tombstone/revision、semantic store、reply/extract、配送journal/reconcile、backup/update/restore | B-01/B-02、昇格契約不足を記録。合成回帰の範囲で確認 |
| 安全・privacy | profile/scope、unknown hold、人承認restore、MAC前復号拒否、原文根拠/公開条件 | 実サービスでの攻撃再現・実鍵/PHI検査は未実施 |
| 依存・CI・供給 | install/update/CI pin、固定SDK入口、tag workflow、公開masterbundle | pin一致・static成功。最新脆弱性調査や公式鮮度は未実施 |
| テスト・導入・配布 | 既存隔離runner、下表の回帰、過去版manifestと原schema証拠の扱い | 通常合成成功と実SDK/実接続/配布起動を区別 |
| 文書・画面・運用 | README、ROADMAP32項目、各詳細計画、1.0.21/22受入、BACKUP/UPGRADE、画面生成元 | 旧判断を保持して現在の順序へ再構築。今回追加の画面変更は別に検証 |

## 調査時の検証

Python3.14.8、pytest9.1.1、既存 `scripts/run_tests.sh` の一時HOME・合成DB・socket遮断を使用。関連範囲が重なるため件数を合算して網羅数としない。

| 検証 | 結果 | 対象 |
|---|---|---|
| 候補統合時の既存証拠 | 394成功を再利用 | clinical chunk planning、Slack、plugin。基準SHAで成功 |
| 配送/警報の調査 | 97成功（うち4件は不具合が現れることの証明） | health_wait_reasons、worker_reconcile_integrity、journal_phase_recovery、notify_reconcile、repo外の合成証明 |
| データ/品質 | 121成功 | semantic density/store/fact_source/human-review assets、ingest tombstone/reply failure、core revisions/edit binding |
| 構造化同期/患者状態 | 78成功 | clinical_metadata_acquisition、project_metadata_view、patient_state_pipeline/references |
| 返信解析対象 | 20成功 | semantic_ingest_revisions、extract_llmの返信context2例 |
| 導入/更新/復元 | 640成功、skipなし | supported_update_paths、runtime_compatibility、backup_integration、backup_restore_consent、restore_staging_ownership、install_sh、install_sh_standalone、install_localの8ファイル |
| 静的・配布原本 | gates10/10、mine gates成功、bundle成功 | 公開20261013 master19,286行、activation=false。公式siteの再確認ではない |

この時点で全体test suite・今回SHAの実SDK lane・実モデル・実API・本番復元・公開は未実施。
追加表示変更後の検証は、その差分の欄へ別に記録する。

## Fableへの相談と採否

2026-10-17 10:24 JST、同タブClaudeから **Fable 5.1（claude-fable-5-1、当該セッション）** のread-only批評を受領した。Codexの初期判断後に計画/ソースだけを共有し、実投稿は渡していない。別モデルへの置換や他タブへの依頼はしていない。

| 指摘 | 判断と反映 |
|---|---|
| 表示変更がR0に欠ける | 採用。追加指示後の並行更新と読取り時点の差だった。現在のR0/調査票へ実装・3通知先・図・変更記録を明記 |
| 表示版変更は既存カードの一度の再描画を起こす | 採用。Slack/Discordの編集増加、LINE WORKSの本文/更新版カード追加をguide/upgradeへ明記。本番適用は未実施 |
| LINE WORKSのスタンプ再観測で投稿が増え得る | 一部採用。ユーザーの全通知先の表示順指定を満たすためスタンプ非表示案は採らず、時刻のみの変化を本文から除外する最小修正と回帰を採用。件数/押下者/状態が変わった場合は既存の更新版追加方式で表示 |
| Discordのownerと実bot IDを両方照合する | 採用。B-02の修正仕様と先着/後着/ID不一致の受入へ反映。今回の表示変更とは別の未修正項目 |
| Hermes private属性の変更で配送停止し得る | 固定SDK契約・属性欠落の受入と診断導線をR0へ追加。具体的な将来renameは未検証仮説。fail-closedを緩める案にはしない |
| pendingだけでなくdeferredの案内も理由別にする | 採用。B-01実装時に両文言とATTENTION分類を確認する |
| R1を実際の障害種別で受け入れる | 採用。part不明2件/上限digest1件を合成再現し、既存の安全な入口が適用できるかを分類。本番の現在件数とは断定しない |
| 版に束縛した昇格をQ1必須にする | 採用。既存のmodel/prompt/分割版・bundle指紋との整合を設計条件にする。extractのdigestだけでsemantic全候補を束縛できるとは仮定しない |
| Slack/Discordのスタンプ更新は編集量を増やす | 採用。移行/変更時の編集量を記録し、既存のrate limit・結果不明保持を維持 |
| R0→R1→R2→R3、metadata不足の分類 | 同意を確認。新しい臨床機能や追加の監査ループを増やさない |


## 表示変更のレビュー

CodeRabbit CLI 0.9.0、開発差分の比較元 `5d71c1a0`。秘密値・実投稿・私有履歴を含まない別Gitの固定snapshotで18テキストファイルを確認した。終了0、`review_completed` / `outcome=completed`、対象18件一致、指摘0。変更なしのため再レビューは行わない。PNGは画面生成/画像検査/目視で検証し、計画・本調査票はFable相談の対象として区別した。最初の起動場所の誤りはGit無しでレビュー開始前に終了し、有効なレビューは1回である。

初回の広い関連テストでは、Discordの既存テスト1件が旧「本文→スタンプ・要約なし」の期待値で失敗した。今回の明示要件に合わせて順序のassertを更新し、配送先/再実行/receiptのassertは保持した。最終テストは下記の通り成功した。

## 最終結果

- 返信も含む要約→スタンプ→本文、後着要約、ページ外スタンプ更新、LINE WORKSの時刻だけの更新抑制を実装済み。未完成/失敗、旧無名本文の凍結、結果不明保留、長文上限を維持。個別実投稿の解析状態は照会していない。
- 最終固定ソースで `scripts/run_tests.sh tests/notify/ tests/adapters/ tests/plugin/ tests/ingest/test_message_metadata.py -q -r fEs` が終了0。**2,535件成功**、Discord実SDK専用モジュール1件は依存未導入でskip（成功扱いにしない）。Python3.14.8/pytest9.1.1、隔離HOME・通信遮断。
- Ruff（CIと同じ範囲）、gates10/10、mine gates、変更記録、生成README、readme_release、Slack gallery8枚、diff check成功。画面例は完全合成で、SVG/PNGの同期と目視を確認。実アプリ画面の検証ではない。
- CodeRabbit18ファイル完全一致、指摘0、レビュー後の実装hash不変。Fable批評を計画とLINE WORKS追加修正へ反映済み。
- 現行ROADMAPの32項目を全件対応させ、旧決定本文を改変せず折畳み履歴へ保持。新規リンクと全IDを確認した。B-01/B-02は計画へ記録した未修正の別項目。
- 作業場所は1.0.23リリースワークツリーのみ。変更は未コミット。mainの既存5ファイルoverlay・設定・DB・サービスは変更していない。push/公開/本番適用/再起動なし。適用後は既存カードの一度の更新があり、LINE WORKSでは追加投稿になる。

非機密の証跡は `~/.local/state/mcs-quality/v1.0.23/audit-roadmap-20261017/`（監査・合成再現・最終テスト・固定レビュー対象/hash・結果）。旧広域テストと最終成功を区別して保存している。
