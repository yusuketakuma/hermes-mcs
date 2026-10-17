# 1.0.25 リリース受入記録

基準は公開・本番反映済みv1.0.24（`35b5a5531e04441391bfb43b8389a7ca43a5dc94`）。DB schemaは9を維持する。本票は実装・隔離検証と公開条件の対応表であり、最終SHAのCI・公開・本番反映の成功を先取りしない。

## 要件と実行・配布経路

| 要件 | 実装と配布先 | 受入の要点 |
|---|---|---|
| 夜間の患者背景 | `patient_nightly.py` → 既存slot2 worker → artifacts | 23:00〜翌03:30、通常処理を優先、A/B/Cの調査と再開、原文根拠・保存直前の再照合・失効を保持 |
| 本人向け8画面 | `patient_context_view.py` / `notify_views.py` → 共通カード → 3adapter | 背景/変化/訪問前/返答/食い違いを初期表示、全8節へ到達。主体・否定・時点・不明を保持 |
| 職種別情報 | `patient_context.py` / v1・v4 → read_model・views | 26分類35属性・10職種。7基本情報を既存分類で保持。ルール23・LLM9・患者背景契約4へ更新 |
| 関係者と活用5点 | 患者部屋内の投稿者メタデータ → participants → 担当候補/依頼者/相違/統計/表示順 | 同名別ID・所属を分離、削除済みと別患者を除外、在籍・契約・主担当を推定しない |
| ボタン再編 | `notify_cards.py` → Slack/Discord/LINE WORKS既存renderer | 患者操作とdigest全体操作を分離、検索を必須へ。行・要素上限、薬剤スレッド、旧tokenの認可と人確定を保持 |
| 日本語の導入・更新・復旧 | install → setup、mcs CLI → target updater / 独立recovery | 推奨/詳細、再入力・中止・後回し。計画と明示適用を分離し、未適用を成功としない。復旧本体は再導入で同期 |
| 抽出・収集・保存・配送修正 | v1/clinical_values/rollup/ledger/pipeline/adapter | 家族/職員/否定/過去の誤採用、古い依頼・予定、既読化直前の新着、巡回/操作順序、fullfsyncを回帰 |
| 更新の安全性 | update/recoveryのdirty検査と既存restore承認 | indexのみの変更も検出し、対象外の手動変更を消すresetを拒否。復元hold・送信結果不明を解除しない |
| 依存 | Hermes固定版b7758d8、開発pin、独立SDK固定版 | coreはstdlib。Hermesとstandaloneは別環境、既存専用SDK laneで受入 |

生成・限定閲覧は省略時に有効、明示falseは維持し、不正値は無効とする。既存環境にも適用される。`patient_state_context`は既定無効。第2入力源はユーザー指示で将来対応とし、実装しない。住所・電話は定期の共有抜粋へ一括転載しない。

## ワークツリーと全領域

全登録WTをcommit祖先・patch同等性・未コミット差分で追跡する。既存auditの各branchは基準mainの祖先、旧reviewの64差分は同一または後続実装で置換済み。4077の対話機能は `4bc1d850` / `eefbc252` として引継ぎ、旧1.0.24の草案metadataは今回版の記録へ適合させた。原WT・dirty・所有lock・stash・公開済み履歴は破棄しない。必要なcommitを開発候補へ実mergeし、最終mainからの到達を確認する。

[全領域の基準記録](REPOSITORY_HYGIENE_1.0.23.md)と[更新取り込み表](UPDATE_ITEMS_1.0.23.md)、[前版受入](ACCEPTANCE_1.0.24.md)を再利用し、全追跡ファイルの同一性と今回の変更・共有呼出元を8領域で再照合する。OpenWiki本文は調査根拠に使わない。動的import・CLI・plugin・CI・導入更新復旧・互換経路を確認せず、参照0件だけで削除しない。新しい管理基盤や行数だけの分割は不要。

## 新規導入・全対応更新元・復旧

空の一時HOME、合成DB/設定、スタブの取得先・サービスで、実入口から初期設定・起動・診断・読込み→保存→抽出→viewを確認する。テストは実MCS・認証情報・原本DB・実LLMへアクセスしない。

- 更新元はv1.0.0〜v1.0.24の25版、40現行経路（Hermes25、standalone15）。既存26履歴経路と手動v1.0.0〜2を保持。
- v1.0.24の公開SHAからschemaを限定AST抽出し、schema9・56定義がshape-11と同一。原DDLの同一群と版別plan/apply/bootstrap/reinstall/rollbackを区別する。
- schema7/8/9、backup、journal、中断、再試行、設定/保存データ保持、復元承認、layout3以前へのrollback制限を既存回帰で維持する。過去のupdaterバイナリ全件を再実行したという意味ではない。
- ソフトの合成中断・checkpoint再開は実停電実験と区別する。実通信・実Mac新規導入・医学的正確性をローカル成功から推定しない。

## レビューと最終検証の条件

初回コミット `b8a19ff4` を先に固定して独立レビューとCodeRabbit（82/82ファイル、outcome=completed、exit0）を実施。指摘13点を検証し `343355b2` で修正、読取り失敗の0件化を `40b1dd15` で追加修正した。対話機能とボタン再編の独立レビュー、および統合候補の固定CodeRabbitは別の受入として記録する。

Python3.14の隔離runnerで全体を領域別・逐次に検証する。編集中のsuiteやPython3.13による環境gate失敗を最終候補の成功としない。通常suiteのSDK専用skipと固定Hermes/standalone実SDK lane、macOS専用検証を分離する。Ruff、10安全gate、事故回帰対応、README/Release/画像生成整合とリンクを確認する。関連するソース・環境が不変の成功結果は再利用する。

## 公式医薬品マスター

2026-10-17 23:20 JSTに公式downloadMenu/yFile/利用条件と仕様を新しく照合した。20261013版ZIPは1,158,682 bytes、SHA256 `c3efc8aa772b9bf54cfa992e8df3bc82480f86f35bbeb52522d0997144140fb5`、member `y_20261013.csv`。同梱版と一致し更新不要。実行時converterで全19,286行のoffline検証とbundle checkerに成功した。出典と利用条件、`activation=false`・`terms_unconfirmed`を保持し、私有辞書の採用・有効化は行わない。

## 公開・本番適用

README5領域・CHANGELOG生成/export・図を完成後、最終exact SHAでlint-test、readme-sync、hygiene、incident-gates、macOS、Hermes実SDK、standalone実SDK、Release文書CIを確認する。tag workflowの下書きを照合して一度だけ公開し、remote main/tag/SHA/Release本文と公開状態を再取得する。

本番は正規target updaterでv1.0.24から適用する。記録済み `--no-brew --no-llm`、導入済み復旧機能と安全な独立Pythonを維持し、Hermes固定版へ再導入。backup、applying=null、setup check、再plan=noop、復旧本体のbyte一致、gateway・通知worker・抽出workerの更新後起動を別証拠で確認する。hold解除・強制全件再処理・実通知テストは含めない。

R1私有snapshotの独立臨床評価、R2実機shadowの精度/遅延/占有実測、R3運用者の閲覧受入は未実施で、個別の運用許可を要する。合成受入・公開・配備はこれらの代替ではない。
