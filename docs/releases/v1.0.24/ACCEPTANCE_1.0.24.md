# 1.0.24 リリース受入記録

2026-10-17。基準は公開・本番反映済み v1.0.23（`1c11122808bde5472800df24fb01b94f6f1158be`）。対象は導入・更新・復旧の日本語化と、その互換性・利用案内。DB schema 9、通知形式、モデル、取得範囲、既定値、人承認・復旧保留条件を維持する。

この記録は固定前のローカル受入証拠をまとめる。最終commitのCI、タグ公開、本番適用は別工程で確認し、GitHub Actionsと公開タグ・Releaseへ束縛する。ローカル成功を実機配備成功と読み替えない。

## 実装と取り込み対応

| 項目 | 入口・配布先 | 確認 |
|---|---|---|
| インストーラー | install.sh / scripts/install_local.py | 質問・進捗・失敗と対処・完了案内を日本語化。既存オプションと設定保持 |
| 初期設定・診断 | mcs/ops/mcs_setup.py と mcs CLI | 最大12区分、必要な質問のみ、番号・はい／いいえ、既存y/n・Enter保持。秘密入力と再利用の既定noを維持 |
| アップデーター | scripts/mcs_upgrade.py → 移行先の mcs_update.py | 表示と機械コードを分離。既知13診断の英日表記を同一視し、動的値や未知の異常は区別 |
| リカバリー | deployment/recovery/mcs_recover.py → checkout外の配置 | 日本語通知。helpで復旧しない、不明引数はexit2。再導入で本体を同期するまで実機反映済みとしない |
| 接続初期化 | standalone / LINE WORKS の入口 | 秘密値の非表示入力を維持。stderrの機械コードを保持し、日本語の説明はstdoutへ表示 |
| 文書・図 | README・導入更新ガイド・lifecycle・生成DEVELOPMENT | 実出力に照合。完全合成の説明図を通常サイズと390px幅で確認。READMEの5領域を新版へ記録 |

## 全領域の再照合

[1.0.23の全領域整理記録](../v1.0.23/REPOSITORY_HYGIENE_1.0.23.md)と[取り込み対応表](../v1.0.23/UPDATE_ITEMS_1.0.23.md)を基準に、全追跡ファイルを分類し差分と同一性を照合した。OpenWikiの本文は参照していない。変更のない領域の成功証拠を再利用し、未確認の実接続や性能を成功扱いしない。

| 領域 | 判定・根拠 |
|---|---|
| 不要コード | 動的import・CLI・互換入口・既存8候補の用途を再確認。新たに削除できる根拠なし。独立配布されるparserの共通化は依存を増やすため行わない |
| ファイル・フォルダー | コアの配置・公開入口・旧schema fixture・履歴の用途は不変。移動・削除不要 |
| 冗長処理・性能 | 収集・LLM・配送の処理は同一。新しいpolling・再試行・外部I/Oなし。性能改善を主張しない |
| 正しさ・保存・互換 | 対話入力、help非実行、診断baseline移行、機械JSON・code・exit・設定保持を回帰検証 |
| 安全・プライバシー | no-proxy/no-redirect、人承認receipt、復旧保留、秘密非表示を維持。完全合成テストのみ |
| 依存・CI | core stdlib、SDKとHermesのpin、CI権限・Action SHAは基準同一。公式依存情報も再確認。最終SHAの既存全CIを公開前条件とする |
| 検証・配布 | 新規導入、全対応更新元、失敗・中断・再実行・rollbackの既存経路を維持し、下記の新版対応を追加 |
| 文書・運用 | 日本語の実表示・接続URL全パス・安全なパスワード入力例・図・README5領域を照合。更新時の再導入と復旧本体同期を明記 |

## 新規導入と全対応更新元

新規導入は空の一時HOMEで、macOS、Hermes/standalone、接続先、導入省略オプション、既存設定保持、失敗・再実行・復旧配置を合成検証した。外部インストール先・ダウンロード・常駐サービスはスタブであり、実Macへの新規導入の証拠ではない。

更新元はv1.0.0〜v1.0.23の24版、38経路を追跡する。

- Hermesのv1.0.0〜2は手動経路、v1.0.3〜23は移行先bootstrap。
- standaloneのv1.0.10〜23は14経路。host経由の更新と再導入条件を保持。
- schema 7/8/9の原DDLとhashを `tests/fixtures/schema_upgrade/origins.json` に保存。v1.0.23は上記の正確な公開SHAからAST抽出し、schema 9・56定義・shape-11が候補と一致することを確認した。
- `update-paths.json` のtargetをv1.0.24へ更新し、既存26履歴経路も保持。履歴の旧updaterバイナリを今回全実行したという意味ではない。
- 実plan/apply・journal・backup・postcheck・migration・再適用・rollback・復元承認は合成Git/サービス境界で検証。原DDLの同一群ごとに収集tick→保存→抽出→viewまで確認する既存回帰を維持。
- layout 3非対応版への直接rollback禁止、人承認付き復元と保留を維持。実DBの復元・実送信はテストしない。

新版fixture関連3モジュールは401件成功。日本語化はinstaller261件、関連44ファイル1,882件、修正後setup248件・installer影響4件と診断境界回帰が成功した。件数は重複するため合算しない。全lint・10静的ゲート・事故回帰対応・生成物とRelease文書チェックは成功。

## レビュー

CodeRabbitは固定差分を2巡完走（初回31ファイル、再確認42ファイル）。永続結果コードの誤翻訳を修正し、parser継承の指摘は実テストから不適用と確認。操作ガイドの古い表示例を修正した。

Claudeの独立レビューで診断文変更による誤巻戻しを発見し、13種類の既知の英日診断を厳密に照合する互換処理で修正。旧診断の基準を取り直して新規異常を隠す方式は使わない。修正後の44ファイルhash・13差分ファイルを再確認し、確定指摘なし。Chrome、Keychain、AI処理枠の実診断関数を用いる合成回帰で旧診断との一致・値変更の検出を確認した。追加の無変更レビューは行わない。

## 公式医薬品マスター

2026-10-17 17:17:44 JSTに公式downloadMenuとyFileを容量制限・no-proxy・no-redirectで新規取得。20261013版ZIP（1,158,682 bytes）のSHA256は `c3efc8aa772b9bf54cfa992e8df3bc82480f86f35bbeb52522d0997144140fb5` で同梱版と一致し、更新なし。全19,286行を実行時converterで検証し、bundle checker成功。公式URL・版・hash・利用条件は既存manifestを保持する。私有辞書の作成・承認・有効化は実施せず、terms_unconfirmedの保留を維持。

## 公開・本番適用の条件

最終SHAでlint/test、macOS、固定Hermes実SDK、固定standalone実SDK、hygiene、incident-gates、readme-sync、Release文書検査を確認してからタグとReleaseを公開する。通常pytestのSDK skipと専用laneの成功を区別する。

本番はv1.0.23から通常の移行先updaterを使い、記録済み `--no-brew --no-llm` を維持する。導入済みの復旧機能を省略せず、独立した安全な復旧Pythonを保持。事前backup、再導入、復旧本体の一致、状態のapplying=null、setup check成功、再plan=noop、必要な常駐処理の更新後起動を確認する。成否不明の送信や復元保留は解除しない。
