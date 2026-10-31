# インストールガイド

Hermes経由の導入・更新にはuvが必要です。Hermesのディレクトリで依存を解決し、安全性のための依存設定を適用します。uvがない場合は変更前に停止し、pipへ切り替えません。`--no-brew`を使う場合はuvを事前に導入してください。

1.0.25の推奨Hermes agentは **[公式v0.22.2](https://github.com/NousResearch/hermes-agent/releases/tag/v0.22.2)をベースにしたMCS companion** です。導入とCIは同じcommit `2c08ca7ade8f867d07e4bff6581be54405b0c9d0` に固定します。MCS用の本人確認コンテキストと認証設定の互換性を維持した版を使います。既存checkoutの移行は更新手順に従います。この固定版は依存の既知の問題に対処した版です。配布元からの固定SHAの取得と、実SDKによる合成統合テストを別々に確認しています。実サービスへの接続確認は導入後の診断で行います。本番への反映は更新手順で別途行います。

**初めての方は[日本語の対話形式で進める手順](EASY_SETUP.md)を先にお読みください。** このページは設定の詳細と管理担当者向けの補足です。
今回の初回受入候補であるmacOS・Apple silicon・Slackのstandaloneは、[独立実行ガイド](STANDALONE.md)から進みます。下記Path Aの最短手順はHermes連携用です。installerの既定値と既存の導入方式は変更しません。

このガイドは現行のinstall/update/setup/doctorと復旧手順に対応します。1.0.25で日本語の対話入口を追加しました。各版の受入状態は版別の受入票で確認してください。
1.0.25の受入結果は[1.0.25のリリース受入票](../releases/v1.0.25/ACCEPTANCE_1.0.25.md)、1.0.26の受入と公開前の残件は[1.0.26のリリース受入票](../releases/v1.0.26/ACCEPTANCE_1.0.26.md)、版ごとの記録一覧は[リリース記録](../releases/README.md)にあります。過去版の検証記録は[1.0.21のリリース受入票](../releases/v1.0.21/ACCEPTANCE_1.0.21.md)、実機受入の進め方は[開発・受入計画](../releases/v1.0.13/RELEASE_1.0.13.md)を参照してください。

### 2026-10-04追記: 現在のローカル入口

[共通CLI](../../mcs/ops/mcs_cli.py)は実装済みです。初回は
`sh scripts/mcs install`、導入後は`mcs install / update / setup / doctor`を利用できます。
`install.sh`が`~/.local/bin/mcs`を配置するため、同ディレクトリをPATHへ追加します。
`mcs setup`は既存の`init`へ委譲し、推奨コース／全項目コースを選べます。既存設定・非表示の秘密入力を維持します。
`mcs update`は更新計画と実行確認、`mcs recover`は中断した更新の状態確認と復旧確認を日本語で案内します。既存の明示サブコマンドと自動監視の動作は維持します。
下記の`install.sh`、`mcs_setup.py init/check/doctor`、更新bootstrapは引き続き利用できます。

`mcs doctor --json`は既定で通信・秘密取得・患者DBの参照をせず、
未検査と正常を区別します。選択runtime・配備recoveryのPython/SQLiteとSDK配布metadataの
表示は[診断実装](../../mcs/ops/mcs_setup.py)に従い、SDKの実接続互換を証明しません。
`--probe llm`や`--probe services`は明示した対象だけを追加確認します。
launcherの選択runtime追随、backup lifecycle、緊急度の後追い通知は統合検証中です。
この追記は1.0.13の公開・実機配備・本番有効化の完了を意味しません。

手動更新を中断した場合は、更新後処理・再インストールの子プロセス群を停止・回収してから中断を返します。
回収の待機時間を制限し、元の中断・エラーを維持します。中断を更新完了とは扱わず、完了状態は既存の復旧手順で確認してください。

> **AI エージェントにセットアップさせる場合:** 対話実行用の手順書は
> [SETUP_AGENT.md](SETUP_AGENT.md) にあります — その文書を
> エージェントに読み込ませれば、前提確認→形態選択→設定投入→検証
> までを対話的に実行します。

**LINE WORKS を使う場合:** 独自接続アダプターの
[導入・接続手順](LINEWORKS.md)へ進んでください。本体の依存導入後に
`mcs_setup.py init` と `python -m lineworks_adapter init/check/run` で設定します。
LINE WORKS 通知には Hermes の通知 plugin・gateway は不要です。

## 最短手順（Path A・初めての方向け）

**事前に用意するもの**（Python は不要 — `install.sh` が導入する）:

- macOS 13 以降の Mac（Apple silicon 推奨）に、普段使いのユーザーで
  ログインしていること（`sudo` や root では実行しない）
- Xcode Command Line Tools（`xcode-select --install`）と
  [Homebrew](https://brew.sh/)
- 空きディスク約 12 GB（LLM モデル 5.7 GB を含む）と、github.com・
  huggingface.co へ接続できるネットワーク
- MCS のログイン ID とパスワード
- 通知を使う場合: Slack app（推奨・作り方は付録B）、Discord bot（付録A）、または[LINE WORKS Bot](LINEWORKS.md)

**手順**:

```bash
git clone https://github.com/yusuketakuma/hermes-mcs.git
cd hermes-mcs

./install.sh --preflight   # 1. 読取り専用の事前チェック（何も書き込まない）
                           #    要対処の行の下の "対処:" を実行し、
                           #    "事前確認: 導入を妨げる問題 0件" になるまで繰り返す
./install.sh --dry-run     # 2. （任意）各ステージで何が作られるかを表示
./install.sh               # 3. 本導入。途中で止まっても直して再実行すれば続きから進む
```

LLM・復旧設定のファイル配置や一時ファイルの削除に失敗した場合も、`install.sh` は非0で停止する。
権限・空き容量を修復して再実行する。`--recovery-python` を明示した場合は、
`--no-recovery` と併用した事前チェックでも指定を検証する。

`install.sh` の最後に「ソフトの準備が完了しました」と、次に実行するコマンドが
**フルパスで**表示される。ターミナルでの実行時は初期設定へ続けて進む選択肢も表示する。それをそのままコピーして実行する（下はその形。
`python` は install.sh が作った venv のインタプリタで、macOS 標準の
`python3` では動かない）:

```bash
PY=~/.hermes/hermes-agent/venv/bin/python
$PY mcs/ops/mcs_setup.py init       # 4. 本体設定・最終チェック。Slack/Discordではplugin・gatewayも同期
$PY mcs/ops/mcs_setup.py doctor     #    （困ったとき）範囲別の件数だけの読取り診断（check の代わりではない）
```

`init` が `設定確認: 正常` で終われば初回設定は完了です。`--no-services` で
導入した場合や、更新後に配置 drift が出た場合だけ、`services` → `check`
を実行します。LINE WORKSでは[接続ガイド](LINEWORKS.md)で認証・Callback・独立常駐を設定し、再診断してください。
収集開始前に Chrome を MCS にログインしたプロファイルで
CDP ポート `:9333` 付きで起動してください（§A-5）。

**成功の目安**:

- `--preflight` の最終行が `事前確認: 導入を妨げる問題 0件、注意 N件`（exit 0）
- `install.sh` が `ソフトの準備が完了しました。まだ利用開始前の初期設定が必要です。` と 6 ステージ分の結果を表示して終わる
  （途中で止まった場合は `エラー: 導入を中断しました。…` が出る — §7-1）
- `check` の最終行が `設定確認: 正常 （問題0件、注意N件）`（exit 0）。
  `注意:` 行は動作を止めないが、内容は一度確認する
- `check` が OK であれば `local.mcs-cmd`・`local.mcs-int`・
  `ai.mcs.extract-drainer`・`ai.mcs.extract-drainer-2`・`org.mcs.recovery`
  と、llama-server（`ai.hermes.llamacpp` か `ai.mcs.llamaserver` の
  どちらか）のロード状態も確認済み（未ロードは `check` の error になる）。
  `doctor` は launchd の状態を表示しない

メッセージ別の対処は §7 トラブルシューティング。詳細な仕組みは以下の各節。

## 概要

hermes-mcs の新規導入手順。導入形態は次の2つ:

- **Path A — hermes-agent アドオン**（installerの既定）: 収集→SQLite→
  Discord/Slack 通知＋対話カード。`./install.sh` が依存一式を導入する
- **Path B — スタンドアローン**（Hermesなし）: 収集・保存・閲覧・
  抽出・Slack/Discord/LINE WORKS通知と操作・定期実行・監視・更新を独立して実行する。
  `./install.sh --mode standalone`で導入する。[専用手順](STANDALONE.md)を参照

## 0. 導入形態の選択

B は `install.sh --mode standalone`（[STANDALONE.md](STANDALONE.md)）の場合。
§3 の独立モードでも、接続先の設定と認証情報を登録すれば Slack/Discord の配送・カードを使える。

<a id="capabilities-acceptance"></a>

### 1.1.0の能力と確認状況

外部利用は個別に書面契約した顧客が対象です。最初の受入候補はmacOS・Apple silicon・Slackのstandaloneで、installerの既定値変更や正式対応・実機受入済みを意味しません。正式なmacOS対応版・最小RAM・同時提供する構成は未確定です。問い合わせ窓口は当面空欄です。CSDC（状態監視・診断の自動送信・遠隔支援）は、1.1.0ではコードを同梱しますが、有効化・接続して提供しません（新規導入では`config.sdc.enabled`が未設定で、専用ジョブは登録されません。既存の有効設定は更新で自動停止しません）。接続は、中央側と実機の受入が整った後の版で行います（2026-10-25 の決定。以前の「1.1.0必須」という扱いを置き換えます）。

「実装済み」は機能の入口があるという意味です。合成確認・実接続・医学的品質・顧客提供の受入は分けます。今回の対象確認は初期設定・通知復旧等の12モジュール668件が合格し、固定候補のCodeRabbit確認は15/15ファイル・指摘0でした。前の検証件数との合算や、全体suite・全機能の合格を意味しません。各行の残条件と[1.1.0作業計画](../roadmap/v1.1.0-public-launch-plan.md)を併せて確認します。

| 能力・方式 | 既定・有効条件 | 実装 | 今回確認した範囲 | 残る受入・根拠 |
|---|---|---|---|---|
| 1. OS・CPU・メモリ | 初回候補はmacOS・Apple silicon。資源条件は次節 | 導入条件の検査あり | installer条件と版境界の合成確認 | 正式対応版・最小RAM・新Macでの負荷は未確定。[前提条件](#1-共通の前提条件) |
| 2. 実行方式と定期実行 | 未指定はHermes、既存設定を保持。standaloneは明示選択 | AはHermes cron＋launchd、Bは独立host。収集・保存・解析・通知・操作の共通機能を提供 | 初期設定・モード選択・再開の対象合成確認 | 専用端末の常駐・再起動後復帰は未受入。[独立実行](STANDALONE.md) |
| 3. Slack・Discord・LINE WORKS | 通知先・許可範囲・資格情報が必要。初回候補はSlack | AのSlack/DiscordはHermes接続、Bは独立SDK。両方式のLINE WORKSは専用接続・Callback・本人1:1操作 | 接続設定・所有者判定の対象合成確認 | 実SDK接続・実配信・全方式同等の受入は未完。[Slack準備](STANDALONE.md#slackとdiscordの接続準備)・[LINE WORKS](LINEWORKS.md) |
| 4. 収集・保存・全履歴・検索 | 定期収集は10分間隔。Chrome/CDPとMCSセッションが必要。既読化は取得完了とsnapshot timestampの条件を維持 | SQLite保存・履歴取得・FTS5・`mcs_view`閲覧を両方式で提供 | 今回の設定・通知確認を、全MCS APIや全履歴の確認へ拡張しない | 新Macのログイン・取得・未読保持は別受入。[初回接続](#初回の専用chromeとセッション確認両モード共通)・[取得範囲](USER_GUIDE.md) |
| 5. 通知・カード・タスク・シグナル | `card_thread`はinit既定ON。`reply_cards`は既定ONで、Slack/Discordかつ`card_thread=true`の場合だけ有効（LINE WORKS対象外）。旧カードの形式を保持。シグナル通知は`signals.notify`既定OFF | 原文・返信・添付、確認・担当、本人向け検索、タスク入力・確定、再確認候補を両方式で提供。Slack/Discordの`/mcs`も両方式で対応 | カード・親待ち・配送結果不明・復旧の対象合成確認 | 実配信・通知到着・実アプリ操作は未受入。タスクは人の確定、配送unknownは自動再送しない。[表示・操作](USER_GUIDE.md) |
| 6. MCSスタンプ | MCS本人ID・1人の操作者・対話通知が揃うと省略時ON。`stamps.enabled=false`で無効。押下者取得は既定OFF | 投稿を指定した本人操作と承認記録で送信・取消 | 今回のカード確認を実MCSへの送信確認としない | 実送信・取消・未読保持・本人照合は別受入。[スタンプ契約](../specs/stamp-management.md) |
| 7. ローカルAIの抽出・要約 | ローカルLLMを使う。`--no-llm`では別途準備が必要。未解析・処理待ち・失敗を表示。共有brokerは別の明示opt-in（`config.json` の `local_llm.admission`、従来の `MCS_LLM_ADMISSION` も可）で既定OFF | ルール抽出とローカルLLM抽出・要約を両方式で提供 | モデルファイル整合と対象制御の合成確認 | 実モデルの速度・メモリ・生成品質は未測定。医療判断の確定ではない。[抽出の範囲](USER_GUIDE.md) |
| 8. 患者コンテキスト・原記録の閲覧 | `patient_context_nightly`の生成・限定閲覧は省略時ON。不正設定は停止。新着抽出への`patient_state_context`は別設定で既定OFF。登録薬剤・検査GETの`clinical_metadata`も既定OFF | 現行世代で全投稿を解析済みの患者を対象に作成。取得範囲・原文根拠とともに本人向け表示 | 設定・表示条件の対象合成確認 | 実生成・長期負荷・人手品質は未受入。未取得を記録なしとしない。[患者情報](USER_GUIDE.md#患者コンテキスト作成既定は有効) |
| 9. Jev追加解析・canonical | キーがありmode未指定ならassist・1日10000要求。明示modeで予算省略時は0。`summary_mode`・`loop_mode`・`extract_qc`は既定OFF。本文の外部送信には対象・予算等の条件が必要 | 任意の追加解析経路あり。Jev非採用でもローカル抽出・患者コンテキストは残る | 今回の設定表示確認をJev実行や品質評価としない | canonical昇格・過去参照は未評価のまま有効化しない。brokerの送信制御とは別に判定する。[評価・公開条件](../specs/semantic-facts-v2-rollout.md) |
| 10. 医薬品照合・公開薬情報 | 辞書は出所・SHA・承認へ束縛。公開マスターの薬品名・一般疾患だけを照会。TinyFishトークンなし時のみExa | 辞書照合・照会と限定Web経路を提供。患者本文や私有辞書をWebへ送らない | Exa限定実接続で1品名のPMDA本文と1疾患のMinds検索を確認。3要求、医学的評価は`unreviewed` | 全品目・医学的品質・鮮度は未受入。強制再取得不可時の定期確認は未取得。[薬情報の条件](USER_GUIDE.md#公開医薬品情報の照会添付文書ガイドライン) |
| 11. ローカル診断・共有表示 | `mcs doctor`の既定では追加probeなし。`--share`は固定項目の表示と手動コピー | 通常doctorを維持し、設定値・患者件数・生例外等を除く共有JSONを追加 | 共有射影・不正入力・未知状態・既存終了コードの対象合成確認 | 手動コピーは自動送信や遠隔grantではない。`check`も選択方式の必須条件検査で、実配送の証明ではない。[診断共有](COLLECTION_DIAGNOSTICS.md#サポートへ共有する診断) |
| 12. CSDCの接続監視 | 1.1.0では有効化・接続しない（コード同梱、新規導入は無効）。未登録・未同意は無効。監視は専用同意と登録が必要 | MCSはR1自社監視とR2/R3顧客鍵保管・公開パッケージinventoryの候補実装。heartbeatは送信専用で命令受信なし | R3固定候補の合成416件と独立再現5件が合格。実顧客接続の確認とは別 | 初回自動接続・中央顧客分離の受入・専用実機受入は未完。[R1の境界](SDC_SELF_MONITORING.md) |
| 13. 診断の自動送信 | 1.1.0では有効化・接続しない（旧計画の「1.1.0必須」は撤回）。監視とは別同意、未同意はOFF | 固定診断イベントの送信・停止の候補実装あり（`adapters/sdc/diagnostic_sender.py`）。ローカル`--share`を送信機能とはしない | 候補実装の合成確認だけ。外部送信の実接続受入は未完 | 中央契約・保存/保持・顧客分離とMCS射影の一致が必要。[ERR計画](../roadmap/v1.1.0-public-launch-plan.md#err-01-双方の固定診断イベント契約) |
| 14. 遠隔支援・限定再起動 | 1.1.0では有効化・接続しない（旧計画の「1.1.0必須」は撤回）。案件・顧客同意・署名・短寿命grant・既存操作所有者が必要 | 定型診断（前景で1回）・限定再起動の候補実装と機械契約あり。中央の親 owner・core の統合、独立受入は未完 | 候補実装の合成確認だけ。実署名・顧客環境・実接続の受入は未完 | ローカルの人承認をremote grantへ流用しない。R4cは後段計画、R1へ命令を混ぜない。[SUP計画](../roadmap/v1.1.0-public-launch-plan.md#sup-01-顧客担当者端末の支援許可契約) |
| 15. バックアップ・端末復元 | ローカル静的DBは日次・7世代。暗号化offsiteの`backup.enabled`は既定OFF、鍵・独立receipt・保持等を明示設定 | 日次コピー・暗号化・検証・訓練・復元holdの入口あり | 今回の対象確認は媒体喪失・端末喪失の手動訓練ではない | 添付本体・設定・Keychain・Chrome profile・配送journalはbundle外。別保管鍵/媒体/receiptと復旧範囲の受入が必要。[バックアップ](BACKUP.md) |
| 16. 更新・中断復旧・rollback | 既存の計画・確認・lock・互換判定を使用。DB復元等の権限を追加しない | 共通CLIと独立復旧入口、方式ごとのプロセス管理あり | モデル整合・初期化・配送復旧の対象合成確認 | 対応更新元と専用環境のapply/rollbackは未受入。固定候補の差分影響を照合する。[更新手順](UPGRADE_AGENT.md) |

実装上の既定値は各機能の設定・仕様を正本とし、この表で変更しません。顧客契約・能力表の整理、合成検証、実機受入、公開・配備は別々の完了判定です。

## 1. 共通の前提条件

| 項目 | 要件 |
|---|---|
| OS | macOS 13 以降（Keychain・launchd を使用。他 OS は収集本体のみ手動で動くが未検証） |
| CPU・メモリ | ローカルAIはApple siliconを推奨。installerはarm64以外では警告しますが、性能や正式対応を保証しません。RAMの最低値・推奨値は未測定で、installerにRAM容量の合否判定はありません |
| 実行ユーザー | 普段使いのユーザー。`install.sh` は root / `sudo` 実行を拒否する |
| Xcode Command Line Tools | `xcode-select --install`（git と Homebrew が必要とする） |
| Homebrew | `brew` が使えること（Path A で `--no-brew` を使わない場合） |
| Python | `install.sh` は両形態でPython 3.14系（`<3.15`）の実行環境を用意する。Hermesはhermes-agentのvenv、独立モードはMCS専用venvを使う。`--no-brew`では必要なPythonを別途準備する。通常の実行・診断はPython ≥3.14が必要。更新前のPython 3.10互換は移行用の限定経路であり、通常動作の対応下限ではない |
| SQLite | 定期ジョブ・復旧 watchdog を実際に動かす Python がリンクする SQLite が WAL-reset 修正済みであること: 3.51.3 以降、または 3.50 系は 3.50.7 以降・3.44 系は 3.44.6 以降。`sqlite3` コマンドの版ではなく、その Python の `sqlite3.sqlite_version` で判定する。macOS 標準 `/usr/bin/python3` の SQLite（3.51.0 など）は対象外のため、復旧 watchdog には `--recovery-python` で安全な独立 Python を指定するか `--no-recovery` を使う。影響版・版不明は `services`・`update`・installer stage 6 が変更前に停止する |
| Google Chrome | MCS にログインしたプロファイルで CDP ポート `:9333` を使う |
| ディスク | `--preflight`の導入前閾値は通常12 GB、`--no-llm`またはモデルファイルが既存なら6 GB。モデル約5.7 GBは固定版を別途取得する。閾値は長期運用の必要容量ではなく、DB・添付・ログ・バックアップは利用量に応じた余裕が必要 |
| MCS アカウント | ログイン ID とパスワード（自動再ログイン `auto_login` で使用） |
| TYPESAFE_API_KEY | Jev用。MCSの `.env`（0600）に登録しmode未指定ならassistで自動使用 |
| MCS_ANTHROPIC_API_KEY | Anthropic用。MCSの `.env`（0600）に登録し、利用先の設定に従って患者文脈更新・履歴解析に使用 |
| MCS_OPENAI_API_KEY | OpenAI用。MCSの `.env`（0600）に登録し、選択時は用途別モデルで患者文脈更新・履歴解析に使用 |
| 通知先アプリ | Slack app・Discord botは両形態で作成・権限設定（付録A/B）。Path Bでは[独立モード](STANDALONE.md#slackとdiscordの接続準備)へ認証情報を登録する。LINE WORKS Botは両形態で[独立接続](LINEWORKS.md)を設定 |

共通設定は `config.json`、提供元別の設定は `data/provider_settings/` の私有JSON、APIキーはMCS専用 `.env` に保持します。設定変更には既存の設定画面またはCLIを使います。
Jev・Anthropic・OpenAIには患者名を含み得る本文・文脈を匿名化せず送ります。キー登録前に[対象・予算・保持条件](ANTHROPIC.md)を確認してください。明示したOFFと低い予算は維持します。

導入時の取得物と運用中の通信は別です。基本ツールはHomebrew、Python依存はPyPI、Hermes連携本体はGitHub、モデルはHugging Faceから必要に応じて取得します。モデル・Chrome・通知SDKはこのリポジトリへ同梱していません。`--no-llm`はモデル取得とAI常駐の設定を省略しますが、AIの処理が利用可能になったことを意味しません。

上の表は実装上の導入条件です。合成installer・版境界テストの成功と、新しいMacでの導入・通知到着・実モデルの速度やメモリの受入は区別します。1.1.0の正式な提供構成は[作業計画](../roadmap/v1.1.0-public-launch-plan.md)で決定・受入を追跡しています。

## 2. Path A — hermes-agent アドオン（全機能）

### A-1. リポジトリの取得と依存の自動導入

```bash
git clone https://github.com/yusuketakuma/hermes-mcs.git
cd hermes-mcs
./install.sh --preflight   # 読取り専用の事前チェック（正常 / 注意 / 要対処 と 対処: を表示）
./install.sh               # 冪等: 何度実行しても既存分は skip、中断分は再開
```

`./install.sh -h` が全オプションを表示する。主なもの:

| オプション | 動作 |
|---|---|
| `--preflight`（別名 `--check-only`） | 前提条件を読取り専用で確認し `正常`/`注意`/`要対処` を表示。要対処には `対処:` 行で直し方が付く。要対処が1つでもあれば exit 1。何も書き込まない |
| `--dry-run` | preflight に加えて、各ステージが何を作成・変更するか（`[新規]`/`[既存]`）を表示。何も書き込まない。要対処があれば exit 1 |
| `--mode hermes\|standalone` | 実行方式。省略時は既存 `config.json` の `runtime_mode` を引き継ぎ、それも無い初回の対話実行では尋ねる（Enter=hermes）。非対話は hermes。standalone では位置引数 `HERMES_HOME` は指定不可（[STANDALONE.md](STANDALONE.md)） |
| `--recovery-python PATH` | 復旧 watchdog（stage 6）を実行する既存の独立 Python の絶対パス。何かを書き込む前に Python ≥3.14 と SQLite の WAL-reset 修正を検証し、更新されるチェックアウト内の実行ファイルは拒否する。私有 `config.json` の `recovery_python` に保存される（後から `mcs_setup.py init --yes --recovery-python <絶対パス>` でも変更可）。未指定時は `/opt/homebrew/bin/python3.14`・`/usr/local/bin/python3.14`・`/usr/bin/python3.14` の順で最初に実行できるもの。代替 Python の自動選択・導入はしない |
| `--force-repo` | 別の checkout から導入済みの環境（plugin symlink・`~/.mcs-recovery/repo_path`・services）を、この checkout に切り替えることを許可する（§A-7） |
| `--no-brew` / `--no-llm` / `--no-plugin` / `--no-services` / `--no-recovery` | ステージ 1 / 4 / 3 / 5 / 6 をスキップ（下記） |
| `[HERMES_HOME]`（位置引数） | 既定 `~/.hermes`。既定以外は services ステージが非対応のため `--no-services` 併用が必須（無いと exit 2） |

- 未知のオプション（`-x` など `-` で始まる引数）や2つ目の位置引数は
  exit 2。root / `sudo` での実行は拒否する（exit 1）
- 環境変数 `HERMES_HOME` は使われない（引数と異なる値なら警告し、
  導入先は引数か既定 `~/.hermes`）
- 新しく作るファイル・ディレクトリは所有者のみ読み書き可（`umask 077`。
  既存ファイルの権限は変えない。brew の導入物は brew 既定の権限）
- git worktree から実行すると警告する — services と plugin がその
  worktree を指すため、削除すると壊れる。main checkout から実行する

一部導入済みの環境ではステージ単位でスキップできる:

```bash
./install.sh --no-llm        # 自前の LLM サーバを使う
                             #   （MCS は 127.0.0.1:8080 の
                             #     OpenAI 互換エンドポイントを期待）
./install.sh --no-plugin     # プラグイン導入を自分で管理する
                             #   （interactive=off のテキスト通知のみ
                             #     なら不要）
./install.sh --no-brew       # brew 管理外の依存を自前で用意済み
./install.sh --no-services   # launchd/cron 登録を後で行う
./install.sh --no-recovery   # 復旧 watchdog を導入しない
```

フラグ無しでも検出ベースで skip する: `:8080` で OpenAI 互換
エンドポイントが応答し、`ai.mcs.llamaserver` の plist も無ければ
llama-server の導入・モデル DL をスキップし、導入済みの
`plugins.enabled`・launchd agent・hermes cron・`~/.hermes/hermes-agent`
checkout はそのまま残る。`:8080` を占有する自前サーバがある場合、
そのサーバが MCS の LLM として使われる（`/slots` が `SLOT_COUNT` と
一致するか `check` が検証する）。`hermes` が既に PATH にあっても
ステージ 2 の venv（`~/.hermes/hermes-agent/venv`）は作る — services が
全ジョブをこのインタプリタで起動するため。

`install.sh` が行うこと（6 ステージ）。**失敗したステージで install は
止まり**（非 0 終了・`エラー: 導入を中断しました。失敗した段階の原因を解消し、install.sh を再実行してください`）、後続ステージは実行されない。原因を直して
再実行すると、既存の状態を確認して必要な段階を進める:

| # | 内容 | 再実行・失敗時 |
|---|---|---|
| 1 | brew パッケージ: `git` `python@3.14` `uv` `llama.cpp` `google-chrome`(cask)。導入済みは skip | `brew` が無い・`brew install` 失敗で停止 |
| 2 | hermes-agent を pin 済み commit で `~/.hermes/hermes-agent` に clone・venv 構築（`pip install -e hermes-agent[messaging]`）・`~/.local/bin/hermes` shim を作成。既存 checkout は保持し、pin 不一致は警告のみ。MCS のものでない shim ファイルは上書きしない | 中断した clone/checkout・Python の無い venv・失敗した pip install は再実行時にやり直す。clone・venv・pip の失敗で停止 |
| 3 | `~/.hermes/plugins/mcs-discord-commands` をこのリポジトリの `hermes_plugin/` に symlink + `hermes plugins enable`（discord/slack 両対応の1プラグイン。導入済みなら skip） | 同名の非 symlink がある・enable 失敗で停止 |
| 4 | llama-server を `ai.mcs.llamaserver` LaunchAgent で常駐化（`127.0.0.1:8080`・`-np 3`・Qwen3.5-9B GGUF 約 5.7 GB を `~/.hermes/models/` へ DL）。hermes 管理の `ai.hermes.llamacpp` の plist があれば導入せず、未ロードで `:8080` も無応答なら bootstrap コマンドを警告で案内。plist 内容が変わった場合、サーバが応答中なら再読込せず適用コマンドを警告で案内（処理中の要求を切らない）、無応答なら再読込 | モデルは固定した Hugging Face の revision から DL し、`.part` から続きを再開（`curl -C -`）。DL 後は `install.sh` に記録した sha256 と照合（`MCS_MODEL_SHA256` による上書きは `MCS_ALLOW_MODEL_SHA_OVERRIDE=1` を併せて指定したときだけ有効で、警告を出す）し、不一致なら `.part` を消して停止。`llama-server` 不在・DL 失敗・bootstrap 失敗で停止 |
| 5 | `~/.mcs/data{,/cmd,/cmd_int}` を作成し、`mcs_setup.py services` — launchd agent 5件 + hermes cronの定期6件（`backup.enabled=true`ならoffsiteを加え7件）の配置・登録（§6 参照） | services が問題を報告したら停止 |
| 6 | 復旧 watchdog `org.mcs.recovery` を独立系統で導入（`~/.mcs-recovery/mcs_recover.py`、旧版は `.prev`。復旧対象の checkout を `~/.mcs-recovery/repo_path` に記録。15分間隔で中断した update を復旧）。内容・記録が変わった時か未ロード時だけ再読込 | macOS では配備前に復旧用 Python（既定は `/opt/homebrew/bin/python3.14`・`/usr/local/bin/python3.14`・`/usr/bin/python3.14` の順で最初に実行できるもの、または `--recovery-python`）の Python ≥3.14 と SQLite の WAL-reset 修正を検査し、影響版・版不明なら tool・plist を配備せず停止（自動で別 Python へ切り替えない）。bootstrap 失敗でも停止 |

ステージ2の最後に、接続方法にかかわらず `~/.mcs/config.json` がなければ本人だけが読み書きできる設定ファイルを作成します。既存の設定は保持し、`--preflight` と `--dry-run` では作成しません。ファイルの作成だけでは初期設定は完了しません。

最後に「ソフトの準備が完了しました」として各段階の結果と、次に実行する
初期設定のコマンド（`mcs setup` と venv インタプリタのフルパス付きの `init`）、
設定後の `check` が表示される。対話端末では、そのまま初期設定へ進むか
後で行うかを選べる。非対話で設定する場合は、
[SETUP_AGENT.md の「5-1. 設定ウィザード実行」](SETUP_AGENT.md#5-1-設定ウィザード実行)
にある `init --yes` の例を参照する。

### A-2. 通知先（Slack / Discord）側の準備

通知先は **Slack を推奨**します（カード操作がコンパクトで、導入も
`hermes slack` コマンド一発。Discord も同じカード形式で使えます）。

収集する値:

| 用途 | 値 | 入手先 |
|---|---|---|
| Slack | `SLACK_BOT_TOKEN`（`xoxb-`） | api.slack.com → Install App |
| Slack | `SLACK_APP_TOKEN`（`xapp-`） | 同上 → Socket Mode で `connections:write` 付き生成 |
| Slack | `team_id`（ワークスペースID） | Slack 管理画面や API |
| Slack | `application_id`（api_app_id） | api.slack.com → Basic Information |
| Slack | `channel_id` | チャンネル名 → チャンネル詳細 → 最下部の ID |
| Slack | member ID（許可ユーザー） | プロフィール → ⋮ → Copy member ID |
| Discord | `application_id` | Developer Portal → General Information |
| Discord | `guild_id`（サーバーID） | Discord 開発者モード → サーバー名右クリック |
| Discord | `channel_id`（カード投稿先） | 同上 → チャンネル右クリック |
| Discord | `DISCORD_BOT_TOKEN` | Developer Portal → Bot → Reset Token（**一度しか表示されない**） |
| Discord | 自分の user ID | 開発者モード → 自分の名前右クリック → Copy User ID |

アプリ・bot の作成手順は hermes-agent リポジトリのドキュメントを
**付録A（Discord）・付録B（Slack）に転記済み** — そちらをそのまま
使える。MCS 側で必要な権限は付録内の「MCS に必要な最小権限」を参照。

### A-3. `mcs_setup.py init` — 設定ウィザード

```bash
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py init
```

（`mcs_setup` の実行は Python ≥3.14 が必要。install.sh が用意した venv
インタプリタを使うのが確実 — macOS 標準の `/usr/bin/python3` は
3.9 系で古い）

対話ウィザードで推奨コースと詳細コースを選びます。推奨コースは必須情報・通知先・権限範囲を確認します。通知スレッドと通知のまとめ方も、必要な場合に質問します。質問しない設定は既存値を保持し、未設定ならその版の既定値を使います。詳細コースは全設定を順に案内します。各項目は説明と選択肢を表示し、Enterで表示された値を使います。関連機能がオフの項目は自動スキップします。`init` が行うこと:

- `~/.mcs/config.json` の既存値を保持し、必要な設定を保存（0600）。ファイルがなければ作成
- Keychain `mcs-adapter` へ MCS パスワードを登録
- `~/.mcs/.env` に `MCS_PASSWORD`（Keychain ロック中のフォールバック）
  と `TYPESAFE_API_KEY`・`MCS_ANTHROPIC_API_KEY`・`MCS_OPENAI_API_KEY`（環境変数で渡した場合）を保存
- `notify.interactive=slack`/`discord` を選んだ場合、hermes 側へも
  書き込む（Slack は `slack_*` settings キーにマップされる）:
  - プラグイン settings を serving profile の config.yaml へ
    （`hermes -p <profile> config set` 経由 — snapshot/inbox/
    allowlists/scope 一式）
  - `SLACK_BOT_TOKEN`+`SLACK_APP_TOKEN`（Discord は
    `DISCORD_BOT_TOKEN`）を同 profile の `.env` へ（stdin 経由、
    argv には載せない。既設定済みのトークンは残る）

非対話でも実行できる（CI・再現用）:

```bash
# secrets は対話入力か安全な端末で環境変数に準備し、履歴に平文を残さない
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py init --yes \
    --login-id <ID> --notify-target slack:<チャンネルID> \
    --set 'notify.interactive="slack"' \
    --set 'notify.slack={"profile":"P","application_id":"A","team_id":"T","channel_id":"C"}' \
    --plugin-profile <serving profile> \
    --plugin-user-ids <uid> \
    --plugin-project-ids <pid>
```

`--set` の値は JSON（文字列は内側の引用符が必要）。Discord の場合は
`notify.interactive="discord"` +
`notify.discord={"profile","application_id","guild_id","channel_id"}`・
`--notify-target discord:<チャンネルID>`・`--plugin-chat-ids`、
トークンは `DISCORD_BOT_TOKEN`。例は `init --help` の末尾にも表示される。

1.0.23ではSlack・Discordとも、指定profileの所有者を確認できる接続だけが配送workerを起動します。Discordは設定したapplication_idと実bot IDも照合します。更新後は使用中の全profileのgatewayを再起動し、指定profileの`worker_started`を確認してください。所有者不明・不一致・起動失敗はdoctorで区別し、gatewayのPIDが生存していることだけを配送成功の根拠にしません。

- 既存の `~/.mcs/config.json` が壊れている（JSON として読めない・
  オブジェクトでない）場合、`init` は何も書かずに停止する（exit 1）。
  手で直すか、`init --yes` で退避して既定値から作り直す。本人所有で
  内容を確認できる1 MiB以下の通常ファイルまたはシンボリックリンクは、
  `config.json.corrupt-<日時>` へコピーする。通常ファイルの退避先は0600で、中身を保持する。
  シンボリックリンクはリンク自体をコピーし、リンク先は変更しない。
  開始時に1 MiB超、読取不能、FIFO、複数のハードリンクがある通常ファイルの場合は退避せず停止する。
  コピーや保存に失敗した場合は表示された案内に従って状態を確認する。親フォルダーのmodeで本人以外に書き込みが許可される場合も
  停止し、シンボリックリンクではmodeで本人以外のアクセスが許可される場合も停止する。
  これらは所有者とmodeの検査であり、ACLによるアクセスは別途確認する
- `init` は通知プラグイン設定の後、対話通知を使うときは gateway を同期し、
  `check` を自動実行する。設定書込み・同期・チェックの失敗は成功扱いにしない

設定キー全一覧は §4 を参照。個別キーは `--set KEY=JSON`（例: `--set self_posts=true`）、Jev 連携は `--semantic-mode`（`off`/`shadow`/`enforce`）で指定できる。

### A-4. サービスの再同期と再検証（更新・復旧時）

過去版からの更新（データ移行・install.sh再実行を含む）は、AIエージェントに
[更新実行手順書](UPGRADE_AGENT.md)を読ませて実行できます。

```bash
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py services   # launchd + hermes cron + gateway
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py check      # 必須条件の検証（exit 1 で失敗）
~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py doctor     # 範囲別の件数だけの読取り診断（不具合報告用）
```

- `services` — launchd agent 5件（cmd/cmd_int WatchPaths・extract
  drainer×2・外部health watch）を配置・bootstrap、hermes cronの定期6件を登録（冪等・
  `--dry-run` で確認可。内容差分は reconcile）。`backup.enabled=true`の場合は
  offsite backupを追加してcron 7件になる。`notify.interactive`
  が `discord`/`slack` のとき `hermes gateway install` + `start` で
  gateway 常駐化も行う。全 wrapper・plist は
  `~/.hermes/hermes-agent/venv/bin/python` で起動するよう描画される
  ため、このインタプリタが無い（または Python ≥3.14 でない）場合は
  何も描画せず exit 1 で止まる（install.sh の再実行でステージ 2 が
  venv を作り直す）
- `check` — 次の順に検証し、エラーを優先度順に並べる:
  1. 定期ジョブの実行基盤: services 用インタプリタ
     （`~/.hermes/hermes-agent/venv/bin/python`）、`hermes` が launchd の
     最小 PATH（`~/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin`）でも解決
     できるか、復旧 watchdog `org.mcs.recovery`（導入有無・repo 版との
     差分・`~/.mcs-recovery/repo_path` がこの checkout か・ロード状態）、
     llama-server agent（`ai.hermes.llamacpp`/`ai.mcs.llamaserver`）の
     ロード状態
  2. config: 読める JSON か、必須キーの型、任意キーの範囲
  3. マシン: Keychain 存在と読取可否、Chrome バイナリ、ローカルLLM
     到達性とスロット数、`hermes send` の解決、gateway の supervised
     状態、semantic 有効時の TYPESAFE_API_KEY、LaunchAgent の配置と
     ロード状態、admission broker の経路表
  4. 配置 drift: `~/.hermes/scripts/` の wrapper が repo から描画した
     内容と一致するか

  出力は `注意:` / `問題:` 行の後、エラーがあれば
  `動作を妨げる問題がN件あります。次の順で修正してください:` に番号付きで要約（各項目に
  `対処:` 行）、最後に `設定確認: 正常／要修正 （問題N件、注意N件）`。
  上から順に直して再実行する
- `doctor` — 既定ではローカルの読取りだけで、configuration・
  interpreter・runtime・recovery_runtime などの範囲ごとに状態と
  error/warning の件数だけを表示する（パス・値・メッセージは出さない）。
  `check` は実行しない。`llm`・`services`・`credentials`・`data` は既定で
  `not_checked` で、`not_checked` は正常を意味しない。`--probe llm` /
  `--probe services` で明示した範囲だけ追加確認する。終了コードは
  いずれかの範囲が `blocked` のときだけ 1。drift・`hermes` の解決・
  Keychain・launchd agent のロード状態を含む blocker 一覧は `check` で確認する

### A-5. 動作確認

#### 初回の専用Chromeとセッション確認（両モード共通）

初回は、専用プロファイルの準備・本人のログイン・CDPの稼働・MCSセッションへの到達・収集を別々に確認します。ソフトの導入やCDPの応答だけでは、MCSへ接続済みとは扱いません。CDPも保存済みトークンもない状態では初期認証が`bootstrap_error`で止まるため、自動再ログインがChromeを準備することを前提にしません。

CDPは同じMacの別ユーザーからも到達できるため、他にログイン可能なアカウントがある、または確認できない環境では、以下のCDP起動・セッション確認へ進まず管理者に相談します。`manual_required:multi_user_host`が出た場合は停止を保持します。専用OSユーザーを追加しただけで解消したとせず、アカウントの削除や安全ゲートの解除で回避しません。

1. 導入時に選んだMCS保存先の`chrome-profile`を専用プロファイルとして使います。普段のChromeプロファイルをコピーしません。次のplaceholderをその絶対パスへ置き換えます。すでに同じプロファイルのChromeが動いている場合は重ねて起動せず、本人が保存中の作業を確認してから対処します。

   ```bash
   MCS_CHROME_PROFILE="/absolute/path/to/mcs-root/chrome-profile"
   mkdir -p "$MCS_CHROME_PROFILE"
   chmod 700 "$MCS_CHROME_PROFILE"
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --remote-debugging-port=9333 --user-data-dir="$MCS_CHROME_PROFILE" \
     --no-first-run --no-default-browser-check \
     --disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching,OptimizationGuideOnDeviceModel \
     https://www.medical-care.net/authentication/login
   ```

2. 開いた専用Chromeで、本人がMCSへログインします。ID・パスワード・Cookie・トークンをチャットへ貼りません。MCSの画面を開けたことを確認し、ログイン操作の代行や秘密値の表示は行いません。
3. 別のターミナルで`curl --noproxy '*' --max-redirs 0 -sf -m 3 http://127.0.0.1:9333/json/version`がJSONを返すことを確認します。これはChromeの稼働確認で、MCSセッションの有効性とは別です。
4. MCSへの認証確認が許可された範囲で、同じcheckoutのルートから次を実行します。`PY`は導入時に選んだMCS用Pythonの絶対パスへ置き換えます。既存アダプターでセッションの可否だけを確認し、トークン・患者情報を表示せず、トークンキャッシュも保存しません。

   ```bash
   PY="/absolute/path/to/selected-mcs-python"
   "$PY" - <<'PYSESSION'
   import sys
   sys.path.insert(0, "mcs")
   import _mcs_path
   from mcs_adapter import MCSAdapter
   from mcs_contract import MCSError
   adapter = MCSAdapter()
   try:
       adapter.bootstrap_token()
       ok = adapter.check_session()
   except MCSError as error:
       print("MCS session: " + error.kind)
       raise SystemExit(1) from None
   print("MCS session: " + ("ok" if ok else "unavailable"))
   raise SystemExit(0 if ok else 1)
   PYSESSION
   ```

5. セッション確認が成功してから、承認した範囲の初回収集と通知到着を確認します。未実施・失敗なら、その段階を未確認として残します。standaloneは[独立モード](STANDALONE.md)のPythonを使います。

以下のPath Aの例は実データの取得・通知・既読化を伴うため、その範囲が承認済みの場合だけ進めます。

```bash
# 収集（手動で1回実行 — 以後は cron が定期実行）
PY=~/.hermes/hermes-agent/venv/bin/python   # install.sh が作った venv（標準の python3 3.9 系は不可）
$PY mcs/ingest/run_check.py --json --download-files --mark-read

# 状態確認
$PY mcs/views/mcs_view.py status
$PY mcs/views/mcs_view.py signals
cat ~/.mcs/data/health.json                # collection/extraction 状態
tail ~/.mcs/data/mcs.log                   # 実行ログ
```

Discord を使う場合は、対象チャンネルで新着投稿があるとカードが投稿
され、コンパニオンスレッド `💬 患者名 — MM-DD` に本文＋添付が届く。

### A-6. 運用上の注意

- **`hermes_plugin/` のコードを変更・更新したら `hermes gateway
  restart` が必須** — gateway は plugin を起動時に読み込む長寿命
  プロセス。再起動しないと新形式 spec を旧 worker が処理し、カード
  だけ届いてスレッド本文・添付が欠落する（2026-09 実機事案）。
  リンクボタン（`MCSで開く`）や人名表示（`parts.mentions`）を含む
  カードは、旧 worker では配送されず保留される（再起動後に配送）
- **カードを操作できる人を増やす（Discord）** — 薬局スタッフ全員に
  ロールを付け、そのロール ID を `allowed_role_ids` に入れる:
  `python3 mcs/ops/mcs_setup.py init --plugin-role-ids <role id>[,<role id>]`
  （または `hermes -p <profile> config set
  plugins.entries.mcs-discord-commands.settings.allowed_role_ids
  '["<role id>"]'`）。guild ID（@everyone ロール）は指定できない。
  ロールを取得できないメンバーは許可されない（fail closed）。ユーザー単位なら `allowed_user_ids` に列挙する。
  いずれも設定後に `hermes gateway restart`。Slack は
  `slack_allowed_user_ids` に複数の member ID を列挙する
- Slack カードを使う場合は `notify.interactive="slack"` +
  `notify.slack` ブロック（`profile`・`application_id`・`team_id`・
  `channel_id`、**guild_id は不可**）。wizard/init の対応で
  プラグイン settings（`slack_adapter_enabled: true` +
  `slack_team_id`/`slack_application_id`/`slack_channel_id`/
  `slack_allowed_user_ids`/`slack_profile`/`project_ids`/
  `data_root`）と `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` を serving
  profile に自動で書き込む — 既設定の値は保持される
- **Slack カードを送るのは `slack_profile` で指定した profile だけ** —
  複数 profile を1つの gateway で動かす場合、他の profile が同じ plugin
  settings を引き継いでいても、その Slack 接続はカード配送を始めない。
  見送った接続はイベントログ `hermes.plugin.mcs_slack` に
  `worker_disabled`（`reason=profile_mismatch`）を1行残す。カードが
  届かないときは、`slack_profile` が配送させたい bot の profile 名と
  一致しているかを先に確認する。plugin のコードを更新した後は上記の
  とおり `hermes gateway restart` で反映する
- **配送の失敗を手作業で再送しない** — `channel_not_found` だけを根拠に
  再送しない（接続した bot の profile 違いでも起きる）。成否不明
  （`unknown`）や再送上限（`resend_exhausted`）の配送も、保存データの
  書換えや強制再送で解除しない。原因を直したうえで、既存の確認・解除の
  手順だけを使う。対応する解除手順がないものは保留を維持し、未解決として
  報告する

### A-7. checkout の場所と移動

導入物は install.sh を実行した checkout の**絶対パス**を指す:
`~/.hermes/plugins/mcs-discord-commands` の symlink、services が描画する
wrapper・plist（`__REPO__`）、復旧ツールが復旧する checkout を記録した
`~/.mcs-recovery/repo_path`（1行のパス。無い場合 `mcs_recover.py` は
`~/.mcs` を復旧対象とみなす）。

- **checkout を移動・再 clone したら、新しい場所で `./install.sh` を
  再実行する**（ステージ 3・5・6 が新しいパスへ張り替える）。移動だけ
  では plugin・定期ジョブ・watchdog が旧パスを指したまま壊れる
- 旧 checkout がまだ存在する場合、install.sh は
  `この Mac は別フォルダ（<旧パス>）から導入されています。` で始まる案内を表示して停止する
  （preflight では 「既存の導入先は別のフォルダです」 の 要対処）。
  切り替える意図なら `./install.sh --force-repo`、そうでなければ旧 checkout
  側の `install.sh` を使う。旧パスが既に存在しなければ競合扱いにならない
- 記録のずれは `check` が
  `復旧監視の対象は<パス>です。` で始まる案内で、現在の導入先との違いを警告する

## 3. Path B — スタンドアローン（Hermesなし・全MCS機能）

収集・保存・検索・抽出に加え、Slack / Discord / LINE WORKSの通知、
対話カード、人承認操作、定期実行、監視、更新・復旧を独立して動かせます。
Hermesが導入済みでも`runtime_mode=standalone`を明示すれば独立モードを使えます。
未指定の既存設定は従来のHermesモードを維持します。

### B-1. 導入・設定

同じcheckoutのルートで実行します。macOSの共通前提条件は§1を参照してください。

```bash
./install.sh --mode standalone --preflight
./install.sh --mode standalone
PY="$HOME/.mcs/venv/bin/python3"
$PY mcs/ops/mcs_setup.py init --runtime-mode standalone
$PY mcs/ops/mcs_setup.py check
$PY -m mcs_standalone status
```

installerは独立venvと公式接続SDK、ローカルLLM、常駐プロセスと復旧watchdogを
用意します。ウィザードがMCS設定・配信先・許可ユーザー・プロジェクト範囲と
認証情報を設定します。初回設定中は接続が起動できず再試行する場合があります。
設定後の`check`はHermes未導入をエラー扱いせず、実際の必須条件を検証します。
`設定確認: 要修正`を想定内として無視しないでください。

### B-2. 接続・常駐・更新

[独立モードの接続・設定・移行手順](STANDALONE.md)を使用します。
SlackはBot/App token、DiscordはBot tokenをowner-onlyの専用ファイルへ保存します。
LINE WORKSの秘密情報・HTTPS Callbackは[LINE WORKS手順](LINEWORKS.md)に従います。

独立hostが既存と同じ6定期ジョブ、cmd/cmd_int取込、抽出worker2本を所有します。
Hermes cron、手動crontab、旧抽出LaunchAgentを同時に登録しないでください。
既存モードからの切替では`services`が旧manifest所有のMCSジョブだけを停止します。
手動登録のジョブや別プロセスのLINE WORKSアダプターは、対象を確認して別途停止します。
共有Hermes gatewayや他用途のジョブは停止しません。

独立モードの更新後はhost自身が処理終了・ロック解放後に新コードで再起動します。
独立モード非対応の旧タグへは戻せません。必要なら先にHermesモードへ移行します。

## 4. config.json 設定リファレンス

`~/.mcs/config.json`（0600）。`init` の詳細コースが全項目を案内する
（`--set KEY=JSON` で非対話設定も可）。必須は `mcs_login_id` と
`notify_target` の2つのみ。

| キー | 型 | 既定 | 説明 |
|---|---|---|---|
| `runtime_mode` | choice | `hermes` | `hermes` / `standalone`。未指定は既存Hermes連携を維持 |
| `mcs_login_id` | str | — （必須） | MCS のログインID |
| `notify_target` | str | — （必須） | 通知の送り先。`discord:<チャンネルID>`・`slack:#ch` 等 `hermes send --to` 形式、又は独自接続の `lineworks:<トークルームID>`。通知を送らない運用では便宜値 |
| `notify.interactive` | choice | `off` | `discord`/`slack`/`lineworks`=対話カード / `off`=テキストのみ |
| `notify.discord.profile` | str | — | 配送に使う hermes プロファイル（interactive=discord 時必須） |
| `notify.discord.application_id` | str | — | Discord アプリケーションID（同上） |
| `notify.discord.guild_id` | str | — | Discord サーバーID（同上） |
| `notify.discord.channel_id` | str | — | カードの投稿先チャンネルID（同上） |
| `notify.slack.profile/application_id/team_id/channel_id` | str | — | Slack 版の配送スコープ（interactive=slack 時必須。guild_id 不可） |
| `notify.lineworks` | object | — | 独自アダプターの Bot・ドメイン・部屋・許可ユーザー・プロジェクト範囲。[設定手順](LINEWORKS.md) |
| `notify.operator` | str | なし | 運用者の Discord ユーザーID |
| `notify.card_thread` | bool | `true`（ウィザード既定。キー未設定のconfigではオフ） | 患者スレッドごとにカードのコンパニオンスレッドを立て本文・添付を配送 |
| `notify.card_thread_archive_min` | int | `10080` | 設定キーのみ（自動アーカイブは未実装） |
| `notify.route_epoch` | int | `1` | 配送先を変えたとき +1 する番号 |
| `notify_bot_profile` | str | なし | 通知投稿に使う hermes プロファイル（`[a-z0-9_-]+`） |
| `notify_system_target` | str | なし | 障害・システム通知の送り先（空欄=`notify_target` と同じ） |
| `notify_max_age_h` | num | なし | この時間より古い投稿は通知しない。設定時は期間内の新規取込みを既読でも通知（既読投稿は投稿時刻が必要） |
| `notify_all_replies` | bool | false | リアルタイム通知経路で新規取得した返信を既読・投稿時刻によらず全件通知。返信には `notify_max_age_h` を適用しない。履歴一括取込み・保存済み返信の再送は対象外 |
| `hermes_bin` | str | 自動検出 | `hermes` コマンドのパス |
| `daily_digest.enabled` | bool | `false` | 朝の日次ダイジェスト（件数と ID のみ）を `notify_target` に送る |
| `daily_digest.hour_jst` | int(0-23) | `8` | 日次ダイジェストを送る時刻（JST。この時刻以降の最初の実行で1日1回） |
| `daily_digest.include_names` | bool | `false` | 一覧の project ID に患者名を添える（送信先は `notify_target`） |
| `self_posts` | bool | `false` | 自分の投稿も取り込んで通知（latest probe 経由） |
| `metadata_shadow` | bool | `false` | 監視対象のスタンプを保存のみ再取得。未読保持の実証と監視集合・予算の運用合意後に限り有効化する。shadowの反応値はカード・digest・CLIの反応表示に出さず、CLIでは取得状態・日時・理由のみ参照できる |
| `metadata_refresh_publish` | bool | `false` | `metadata_shadow`の再取得が成功した値をカード・digest・CLIの反応表示へ反映する。失敗時と保存済みのshadow値は反映しない。未読保持の実証と`mcs/views/metadata_report.py`での照合後に限り有効化する |
| `metadata_actors` | bool | `false` | `metadata_shadow=true`も必要。直近7日に動きのあったスレッドの全投稿（先頭・返信）について、スタンプを押した人の氏名・所属・職種を取得し、Slack・DiscordのスレッドとLINE WORKSの原文投稿に氏名・職種を表示する（アイコンは保存しない）。最後の観測状態は無期限に保持し、取消観測時刻を残す（再押下時は解除、全操作履歴は復元しない）（#22-D2・D3、2026-10-03）。shadowと同じtickの予算内で最大4件。氏名の保存・通知先の閲覧範囲と未読保持を確認して有効化する |
| `deep_history` | bool | `true` | 初回に全履歴を遡って保存 |
| `discover_archived` | bool | `false` | アーカイブ済み患者も収集対象にする |
| `trickle_pages` | int(1-40) | `3` | 1回の実行で履歴を遡るページ数 |
| `job_budget_seconds` | num | 既定 | 内部処理の時間予算・秒 |
| `signals.notify` | bool | `false` | アラートを通知に出す |
| `signals.digest` | bool | `true`（キー未設定時。ウィザード既定は `false`） | 複数候補をダイジェストにまとめる |
| `signals.digest_interval_h` | num | 既定 | ダイジェスト間隔・時間 |
| `signals.self_reaction_response` | bool | `false` | 依頼投稿そのものに本人の承知・完了スタンプが観測されたら、薬剤師宛依頼の未応答候補から外す。見ました・他者のスタンプ・未取得は数えない。切替え時に既存候補のresolve・再openが起き得る（#22-D5） |
| `signals.self_organizations` | list[str] | 自動検出 | 自施設名（MCS プロフィールから自動検出を上書き） |
| `signals.self_professions` | list[str] | 自動検出 | 自職種（同上） |
| `signals.request_targets` | list[str] | なし | 依頼先として数える宛名 |
| `signals.med_exclude_names` | list[str] | なし | 薬剤判定から除外する語 |
| `urgency_escalation.mode` | choice | `off` | `off`/`shadow`/`on`。現在のLLM抽出で緊急度highの投稿の再確認候補（E1/E2）。`shadow`は送信せず監査のみ、`on`は`notify_target`へ通知。有効化は確認者・通知先の運用合意後 |
| `urgency_escalation.room_cooldown_min` | num(>0) | — （mode が off 以外では必須） | 同じ部屋への再確認通知の冷却時間・分。未指定・不正値では機能が無効のままとなり、`check` がエラーを出す |
| `urgency_escalation.after_min` / `repeat_min` | num(>0) | `30` / `60` | 初回表示から再確認までの分 / 再通知の間隔・分 |
| `urgency_escalation.max_repeats` / `max_per_day` | int(>=0) | `2` / `10` | 1投稿あたりの再通知回数 / 1日の上限 |
| `urgency_escalation.source` | choice | `llm` | 判定元。`llm`のみ対応 |
| `vital_urgency.mode` | choice | `off` | `off`/`flag`/`high`。バイタル数値の決定論的閾値判定。`flag` は閾値超過の測定値を確認用に記録・表示するのみ、`high` は緊急度を高にし閾値根拠を urgency_evidence にする。バイタル欄は測定対象・時制を保持しないため、本人以外の測定・条件節・過去報告・測定不能の文脈は近傍テキストのヒューリスティックで除外する（完全ではない）。有効化・閾値は臨床責任者の承認が前提 |
| `vital_urgency.thresholds` | object | SpO2≤90 / SBP≤90 / SBP≥180 / BS≤70 | 閾値の上書き（`spo2_lte` / `sbp_lte` / `sbp_gte` / `bs_lte`） |
| `local_llm.url` | str | `http://127.0.0.1:8080/v1/chat/completions` | ローカルLLMのエンドポイント（loopback http のみ — それ以外は `check` が拒否。別ポートの自前サーバを指せる） |
| `local_llm.model` | str | `Qwen3.5-9B` | モデル名（OpenAI 互換 API の `model` フィールド） |
| `semantic.mode` | choice | キーありなら`assist`、なしなら`off` | `off`以外は本文を外部Jev APIへ送信。明示OFFを維持。`shadow`=記録のみ、`assist`=既存の支援経路、`enforce`=評価・承認を満たした判定への使用 |
| `semantic.project_ids` | list[int] / null | 自動有効化で対象も省略なら全対象 | 対象プロジェクトID。手動でmodeを有効にする場合は明示必須。`null`=全対象、`[]`=対象なし |
| `semantic.extract_qc` | choice | `off` | `annotate`=抽出結果への Jev 監査注記 |
| `semantic.daily_request_budget` | int | 自動modeは10000、明示modeは0 | 日本時間の1日あたりHTTP要求数。再試行も加算。0は停止、10000超でも実効上限10000 |
| `semantic.job_budget_seconds` | num | 既定 | 意味解析の1ジョブの時間予算・秒（5〜1260）。バックログでは1220以上で、通信後に実行ロックを取り直す余裕540秒を全量確保 |
| `update.mode` | choice | `off` | `off`/`notify`/`auto` — 自己更新ポリシー |
| `update.auto_delay_h` | num | — | auto 時の適用遅延（0=検出次第即適用） |
| `update.include_prerelease` | bool | `false` | プレリリースを更新対象に含める |
| `health.max_missed_runs` | int | `2` | 欠測許容回数。10分間隔の予定実行と完了猶予から判定 |

## 5. 秘密情報の配置

| 場所 | 内容 | 備考 |
|---|---|---|
| Keychain `mcs-adapter` | MCS パスワード | `auto_login` がフォーム投入時に読む |
| `~/.mcs/.env` (0600) | `MCS_PASSWORD`（Keychain ロック中のフォールバック）・`TYPESAFE_API_KEY`・`MCS_ANTHROPIC_API_KEY`・`MCS_OPENAI_API_KEY`。`runtime_mode=standalone` の互換トークン入力。`init`は私有JSONへ取り込み、run/sendはJSONを優先する | 平文 — FileVault/物理セキュリティ前提 |
| `~/.mcs/data/{slack,discord}-credentials.json` (0600) | 独立Slack/DiscordのBot/App token。`mcs_standalone init`が非表示入力または既存の私有 `.env`から登録 | run/sendが優先して検証。秘密値の環境自動取得なし |
| hermes profile `.env`（Path A のみ） | `DISCORD_BOT_TOKEN` / `SLACK_BOT_TOKEN`+`SLACK_APP_TOKEN` | `init` または `hermes config set --stdin` で書込み（argv に載せない） |

## 6. スケジュール構成（Path A 導入後）

収集ジョブは **hermes cron 6件 + launchd 6件** のハイブリッド
（services 所有: cron 6件と launchd 5件 `local.mcs-cmd`・`local.mcs-int`・
`ai.mcs.extract-drainer`・`ai.mcs.extract-drainer-2`・`local.mcs-health`。install.sh 所有:
復旧 watchdog `org.mcs.recovery`。`backup.enabled=true`の場合はoffsite backupも
cronに追加され7件になる。別に LLM サーバ常駐の
`ai.mcs.llamaserver` / `ai.hermes.llamacpp`）。

ジョブ・スケジュール・実行スクリプトの一覧表は
[deployment/launchagents/README.md](../../deployment/launchagents/README.md)
に一本化している。正本は `mcs/core/scheduled_jobs.py` の `CRON_JOBS`
（hermes cron）と `mcs/ops/mcs_setup.py` の `AGENT_LABELS`（launchd）で、`services` はこれを
登録する。MCSサーバーの負荷対策として、未読収集を5分間隔から、24時間を通じて10分間隔へ変更した。
`mcs_check.sh` に夜間の間引きはない。
既存環境では更新後に `mcs setup services` で取得ジョブを再設定する。
`health.tick_interval_s` を300秒と明示している場合は、
`mcs setup init --yes --set health.tick_interval_s=600` で600秒へ変更する。
未指定時の既定は600秒。ローカルのヘルス監視自体は5分間隔を維持する。

## 7. トラブルシューティング

まず `./install.sh --preflight`（導入前・導入途中）または
`~/.hermes/hermes-agent/venv/bin/python mcs/ops/mcs_setup.py check`
（導入後）を実行し、出力されたメッセージを下の表で引く。どちらも
直し方を `対処:` 行に出すので、基本はそれを上から順に実行して再実行する。
表中の `$PY` は `~/.hermes/hermes-agent/venv/bin/python`。

### 7-1. `install.sh --preflight` / `install.sh` のメッセージ

| 表示内容の要旨（パス・版番号などは環境によって異なります） | 原因 | 対処 |
|---|---|---|
| 管理者 root で実行されています | `sudo` や root で実行した | 普段のユーザーで `sudo` なしに再実行 |
| `macOS <版> は古すぎます` | macOS 13 未満 | macOS をアップデート |
| `Xcode Command Line Tools が未導入です`／`git が未導入です` | 開発ツール未導入 | `xcode-select --install` |
| Homebrew が未導入です | Homebrew 未導入 | `対処:` 行の Homebrew 公式インストールコマンドを実行（brew を使わないなら `--no-brew`） |
| 実行できる Python 3.14 が見つかりません | `--no-brew` 等で brew が Python を入れられない | `brew install python@3.14`（brew があれば stage 1 が入れるので 要対処 にならない） |
| `ディスク: <パス> の空きは N GB です（必要容量は約 N GB、モデルは5.7 GB）` | 空き容量不足（モデル込みで約 12 GB） | 空きを作って再実行 |
| `通信: <接続先> に接続できません` | clone・モデル DL に必要な通信ができない | ネットワーク・プロキシを確認し `curl -I <URL>` で疎通確認 |
| `ポート8080が AI サーバー以外の処理に使用されています` | 別のプロセスが :8080 を使用 | `lsof -nP -iTCP:8080 -sTCP:LISTEN` で特定して停止、または自前 LLM なら `--no-llm` |
| 既存の導入先は別のフォルダです | 別の checkout から導入済み | 旧 checkout の `install.sh` を使うか、切り替えるなら `./install.sh --force-repo`（§A-7） |
| `常駐サービスは独自の HERMES_HOME（<パス>）に対応していません` | `~/.hermes` 以外を引数に指定 | 既定の `~/.hermes` を使うか `--no-services` を付ける |
| `<パス> は存在しますが Git で取得したフォルダではありません`／`<パス> の現在の版（HEAD）を読み取れません` | hermes-agent ディレクトリが壊れている | `対処:` 行の `mv ... .bak` で退避して再実行（stage 2 が clone し直す） |
| `Hermes は <現在の版> です。検証済み固定版 <指定版> と異なります（現状を保持します）` | 既存 checkout が検証済み commit と違う（保持される） | そのままでも続行可。揃えるなら `対処:` 行の `git ... checkout --detach <pin>` |
| `Git の作業用フォルダ（<パス>）から実行しています` | worktree から実行している | main checkout から実行する |
| Google Chrome が未導入です | Chrome 未導入（brew があれば stage 1 が入れる） | `brew install --cask google-chrome` |
| `未対応のオプションです: <指定値>` | 存在しないオプション | `./install.sh -h` で確認 |
| 導入を中断しました。失敗した段階の原因を解消し、install.sh を再実行してください | 直前の `エラー:` 行のステージで停止した | その `エラー:` の指示に従って直し、`./install.sh` を再実行（完了分は skip） |
| `モデル取得に失敗しました` | モデル DL が途中で失敗 | そのまま `./install.sh` を再実行（`.part` から再開） |
| `モデルの整合性が確認できません（SHA256: <検証値>）` | DL 結果が `install.sh` 記録の sha256（`MCS_ALLOW_MODEL_SHA_OVERRIDE=1` 指定時は `MCS_MODEL_SHA256`）と不一致 | 通信経路を確認し、上書きしている場合は値を確認して再実行（`.part` は削除済みで最初から DL） |
| Hermes 導入に失敗しました | pip install の失敗（通信・コンパイラ） | 上に出たエラーを直して再実行（pip install をやり直す） |
| llama-server が見つかりません | llama.cpp 未導入 | `brew install llama.cpp`、または `--no-llm` |
| プラグインの有効化に失敗しました | hermes CLI・profile 設定の問題 | hermes の設定を直すか profile の `config.yaml` の `plugins.enabled` に `mcs-discord-commands` を追加して再実行 |
| `常駐サービスの登録に問題があります` | stage 5 の `mcs_setup.py services` が失敗 | 直前の services の出力を確認、`$PY mcs/ops/mcs_setup.py check` の blocker を直して再実行 |
| `復旧サービスのテンプレートに問題があります: Python の安全条件を満たさないか、確認できません`／`指定した復旧用 Python の安全性が確認できないか、稼働設定と一致しません` | 復旧 watchdog 用 Python（既定は上記の `python3.14` 候補）が Python <3.14、または SQLite の WAL-reset 修正が無い・版不明。導入済みジョブと選択が異なる場合も停止 | 修正済み SQLite をリンクした既存の独立 Python を `--recovery-python <絶対パス>` で指定して再実行するか、`--no-recovery` で stage 6 を省く。既存ジョブとの不一致は `mcs_setup.py init --yes --recovery-python <絶対パス>` で希望を保存し、復旧ジョブが実行中でない時に `mcs_setup.py services` で修復 |
| `<パス> が PATH にありません` | shell から `hermes` が見えない | シェルの profile で `~/.local/bin` を PATH に追加（定期ジョブは独自の PATH を使うので影響しない） |
| `AI サーバーの起動設定を更新しましたが、稼働中の処理は保持しました` | plist 更新時にサーバが応答中だった | 処理が空いた時に表示された `launchctl bootout ...; launchctl bootstrap ...` を実行 |
| `Hermes 管理の <パス> は未起動で、ポート8080の応答もありません` | hermes 管理の LLM agent が止まっている | 表示された `launchctl bootstrap ...` を実行 |


### 7-2. `mcs_setup.py check` / `doctor` のメッセージ

| 表示内容の要旨（詳しい説明と対処は診断結果に表示されます） | 原因・対処 |
|---|---|
| 設定ツールに必要な Python の版を満たしていません | 素の `python3`（3.9 系）や旧 venv で実行した — `$PY`（Python 3.14 の venv）で実行する。1.0.17 以前の venv は更新ツールで移行する（[更新手順](UPGRADE_AGENT.md)） |
| 実行用または復旧用 Python の SQLite が安全条件を満たしていません | 定期ジョブ（または復旧 watchdog）を実際に動かす Python のリンクする SQLite が影響版。`services`・`update` は変更前に停止し、`doctor` は `checks.runtime` / `checks.recovery_runtime` を blocked にする（`runtimes.*` は事実の表示だけ） — 修正済み SQLite（§1 の要件）をリンクした Python で venv を作り直す。復旧側は `--recovery-python` / `init --recovery-python` で安全な独立 Python を指定する |
| 実行用 Python が見つからないか、実行できません | 定期ジョブが起動できない — `./install.sh` を再実行（stage 2 が venv を作り直す）→ `$PY mcs/ops/mcs_setup.py services`。`services` もこの状態では何も描画せず止まる（独立モードでは専用venvを診断する） |
| 定期実行から Hermes を呼び出せません | 手元の shell では見えるが定期ジョブから見えない — 表示の `ln -s <hermes> ~/.local/bin/hermes`、または `init --set hermes_bin='"<パス>"'` |
| 設定ファイルを読み取れないか、形式が不正です | `~/.mcs/config.json` を確認して直す。本人所有で内容を確認できる1 MiB以下の通常ファイルまたはシンボリックリンクは、対応環境で `$PY mcs/ops/mcs_setup.py init --yes` により `config.json.corrupt-<日時>` へ退避できる。通常ファイルに複数のハードリンクがある場合や、親フォルダーの権限が退避に適さない場合は停止する。退避を完了できない場合は停止し、表示に従って現在の設定と退避先を確認する |
| 必須設定（mcs_login_id、notify_target）が不足しています | `init` で再登録（両方とも必須） |
| Keychain に MCS のパスワードが登録されていません | パスワード未登録 — `init` で登録（`.env` `MCS_PASSWORD` があれば警告に格下げ） |
| Keychain の登録内容を読み取れません | login keychain がロック中 — `security unlock-keychain` か GUI ログイン。再起動後も収集が必要な場合は `init` の `.env` フォールバック設定を確認 |
| Google Chrome が見つかりません | Chrome が `/Applications` に無い — `brew install --cask google-chrome` |
| ローカル AI に接続できません | llama-server 未起動 — Path A-1 または [スタンドアローン導入](STANDALONE.md)。収集自体は動く（警告） |
| AI の同時処理枠が不足しています | `-np` が選択スロット数（3）未満 — plist の `-np 3` を確認 |
| ローカル AI の起動設定はありますが、起動していません | LLM サーバが止まっている — 表示の `launchctl bootstrap gui/<uid> <plist>` |
| ローカル AI の起動設定がありません（注意） | 自前サーバを `local_llm.url` で使うなら問題なし。そうでなければ `./install.sh`（stage 4） |
| 更新の復旧監視が未導入です（注意） | `./install.sh`（stage 6。`--no-recovery` で導入しなかった場合は想定内） |
| 配置済みの復旧ツールが配布版と異なります（注意） | 復旧ツールが古い — `./install.sh` を再実行（旧版は `.prev` に残る） |
| 復旧監視が別の保存先を参照しています（注意） | checkout を移動した・別 checkout から実行している — 正しい checkout で `./install.sh`（§A-7） |
| 復旧監視の設定はありますが、起動していません | 表示の `launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/org.mcs.recovery.plist` |
| 常駐処理の設定はありますが、起動していません | MCS の agent が止まっている — `$PY mcs/ops/mcs_setup.py services` |
| 常駐処理の設定がありません（注意） | `services` 未実行 — `$PY mcs/ops/mcs_setup.py services` |
| 配置済みのスクリプトが配布版と異なります | 更新後に services を再実行していない — `$PY mcs/ops/mcs_setup.py services` |
| 定期実行スクリプトが未配置です（注意） | `services` 未実行 — `$PY mcs/ops/mcs_setup.py services` |
| このセッションから macOS の常駐処理を確認できません（注意） | ssh 等 GUI セッション外で実行している — ログイン中の端末で再実行して確認 |
| Hermes コマンドが見つかりません | hermes 未導入（Hermesモードのエラー。独立モードならSTANDALONE.mdを確認。Path Aなら `install.sh` 再実行か `hermes_bin` 設定） |
| Hermes の通知接続が常駐管理されていません | `services` を実行（`hermes gateway install`+`start` で常駐化） |
| 通知接続が更新前のプログラムで動いています（注意） | `hermes gateway restart`（§A-6） |
| 外部の意味解析に必要な TYPESAFE_API_KEY が未設定です | semantic 有効時に必須 — `~/.mcs/.env` に登録 |


### 7-3. その他の症状

| 症状 | 原因・対処 |
|---|---|
| `session_expired` 通知が来る | セッション失効 — tick 内で `auto_login` が `_recover_session`→フォーム投入をその場で試行。失敗時のみこの通知が来る（detail の `auto_login=<state>` を確認。`manual_required`/`keychain_locked` は上記 Keychain 節）。成功時は代わりに `session_recovered` 通知が来て run は継続する |
| カードだけ届きスレッド本文が無い | gateway が旧 plugin を保持 — `hermes gateway restart`（§A-6） |

### セッション失効・Keychain 状態の読み方

README「導入方法」から移設（文言は同じ。段落を項目に分けた）。

- セッション切れは tick 内の失敗点で `auto_login` が1回試行され、成功すれば
  その run のまま再開する。結果は通知に出る — 成功なら `session_recovered`
  (`run N: <stage>: ...`)、失敗なら従来どおり `session_expired` で detail に
  `auto_login=<state>` が付く。run log の `relogin_attempts` に試行記録が残る。
- `keychain_locked` は「エントリはあるが login
  keychain がロック中で読めない」状態 — `security unlock-keychain` または
  GUI ログインで解除してから次回 run を待てばよい(エントリ再登録は不要)。
- `~/.mcs/.env` の `MCS_PASSWORD` はリブート直後のロック中にも効く
  フォールバック(`mcs_setup init` が Keychain と併記する; 平文のため
  FileVault/物理セキュリティ前提)。頻発する場合はログイン状態と Keychain の
  読み取り可否を確認し、自動ロックを収集失敗の回避策として無条件に解除しない。
- `manual_required` はエントリ未登録かつ .env 未設定、またはフォーム非検出
  — `mcs_setup init` で再登録する。

## 付録A: Discord 接続設定（hermes-agent リポジトリより転記）

> 以下は `hermes-agent` リポジトリの
> `website/docs/user-guide/messaging/discord.md` から、MCS の通知・
> カード配送に必要な部分を転記したもの。最新の全文は
> hermes-agent checkout の同ファイルを参照。

### Discord の bot 作成（Step 1–8）

**Step 1: Create a Discord Application** — Go to the Discord Developer
Portal and sign in. Click **New Application**, enter a name, click
**Create**. On the **General Information** page, note the
**Application ID**.

**Step 2: Create the Bot** — In the left sidebar, click **Bot**.
Under **Authorization Flow**, set **Public Bot** ON (recommended) and
leave **Require OAuth2 Code Grant** OFF.

**Step 3: Enable Privileged Gateway Intents** — On the **Bot** page,
scroll to **Privileged Gateway Intents** and enable **Server Members
Intent** and **Message Content Intent** (both required — without Message
Content Intent the bot cannot see message text). Click **Save Changes**.
If your bot is in 100+ servers, Discord requires a verification
application for privileged intents.

**Step 4: Get the Bot Token** — Still on the **Bot** page, under
**Token** click **Reset Token** (2FA code if enabled). **Copy it
immediately — the token is shown only once.** Never share or commit it.

**Step 5: Generate the Invite URL** — Option A (recommended): sidebar →
**Installation** → enable **Guild Install** → **Discord Provided Link**;
under Default Install Settings select scopes `bot` and
`applications.commands` plus the permissions below. Option B (manual):

```text
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=274878286912
```

Required permissions: View Channels, Send Messages, Embed Links,
Attach Files, Read Message History. Recommended additions: Send
Messages in Threads, Add Reactions. Permission integers: minimal
`117760`, recommended `274878286912`.

**Step 6: Invite to Your Server** — open the invite URL, select your
server, **Authorize** (requires Manage Server permission).

**Step 7: Find Your Discord User ID** — Discord Settings → Advanced →
Developer Mode ON → right-click your username → **Copy User ID**.
Channel/Server IDs are copied the same way.

**Step 8: Configure** — `hermes gateway setup`（対話）または
`~/.hermes/.env` に `DISCORD_BOT_TOKEN` と `DISCORD_ALLOWED_USERS`
を書き、`hermes gateway` で起動。MCS では `mcs_setup.py init` が
token を serving profile の `.env` に自動登録する（§A-3）。

### MCS に必要な最小権限（Discord）

カード配送・コンパニオンスレッドには上記に加えて:

- **Create Public Threads** — `💬 患者名 — MM-DD` スレッドを立てる
- **Send Messages in Threads** — スレッド内に本文・添付を投稿
- **Attach Files** — PDF・画像の添付配送

`Message Content Intent` は thread 内本文の remote-match（重複配送
防止）と `/mcs` コマンドのメッセージ照合に必要。

## 付録B: Slack 接続設定（hermes-agent リポジトリより転記）

standaloneではStep 1の`From scratch`とStep 2〜7・9を使います。Step 8のHermes用設定は行わず、[独立モードの認証情報登録](STANDALONE.md#slackとdiscordの接続準備)へ進みます。以下のHermes用manifest・profile・gateway設定はPath A向けです。

> 以下は `hermes-agent` リポジトリの
> `website/docs/user-guide/messaging/slack.md` から転記。Hermes は
> Socket Mode（WebSocket・公開URL不要）で Slack と接続する。

### Slack のアプリ作成（Step 1–9）

**Step 1: Create a Slack App** — Option A（推奨）: `hermes slack
manifest --agent-view --write` で `~/.hermes/slack-manifest.json` を
生成し、[api.slack.com/apps](https://api.slack.com/apps) →
**Create New App** → **From an app manifest** に貼り付け（スコープ・
イベント・Socket Mode が自動設定される）。Option B: **From scratch**
で手動作成し Steps 2–6 を実施。

**Step 2: Bot Token Scopes** — **Features → OAuth & Permissions** の
Bot Token Scopes に追加: `chat:write`, `app_mentions:read`,
`channels:history`, `channels:read`, `groups:history`, `im:history`,
`im:read`, `im:write`, `mpim:history`, `mpim:read`, `users:read`,
`files:read`, `files:write`（channels:history/groups:history が無いと
チャンネルメッセージを受け取れない）。

**Step 3: Enable Socket Mode** — **Settings → Socket Mode** を ON、
`connections:write` スコープで **App-Level Token** を生成 →
`xapp-` トークンをコピー（`SLACK_APP_TOKEN`）。

**Step 4: Subscribe to Events** — **Features → Event Subscriptions**
を ON → Subscribe to bot events: `message.im`, `message.mpim`,
`message.channels`, `message.groups`（推奨）, `app_mention` → Save。

**Step 5: Enable the Messages Tab** — **Features → App Home** →
Show Tabs → **Messages Tab** ON → 「Allow users to send Slash commands
and messages from the messages tab」をチェック（これが無いと DM が
完全にブロックされる）。

**Step 6: Install App to Workspace** — **Settings → Install App** →
Install → Allow → `xoxb-` の **Bot User OAuth Token** をコピー
（`SLACK_BOT_TOKEN`）。スコープやイベントを変えたら再インストール
が必要。

**Step 7: Find User IDs** — ユーザー名 → View full profile → ⋮ →
**Copy member ID**（`U01...` 形式）。

**Step 8: Configure** — `~/.hermes/.env` に:

```bash
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…
SLACK_ALLOWED_USERS=U01ABC2DEF3        # カンマ区切り Member ID
SLACK_HOME_CHANNEL=C01234567890        # 任意: cron/通知の既定ch
```

**Step 9: Invite the Bot** — 各チャンネルで `/invite @<app名>` を
実行（bot は自動参加しない）。

### MCS に必要な設定（Slack）

- runner 側 config: `notify.interactive="slack"` +
  `notify.slack={"profile","application_id","team_id","channel_id"}`
  （`guild_id` は不可 — team_id を使う）
- plugin settings（`hermes config set
  plugins.entries.mcs-discord-commands.settings.<key>` で手動）:
  `slack_adapter_enabled: true`, `slack_team_id`,
  `slack_application_id`, `slack_channel_id`,
  `slack_allowed_user_ids`, `slack_profile`, `project_ids`,
  `data_root`, `snapshot`

### 任意: `/mcs-summary`（本人専用の要約サマリー）

カードの「全体の新着集計」に加え、Slackのslash commandからも呼び出せます（v1.0.11〜）。
使う場合だけ、Slackアプリ設定で次を追加して**アプリを再インストール**します。

- **Slash Commands** → Create New Command: Command `/mcs-summary`、
  Short Description「MCS 要約サマリー（本人専用）」、Usage Hint `all / mine / station:施設名 / project:ID / days:1-7`
  （Socket Modeでは Request URL は不要）
- **OAuth & Permissions** の Bot Token Scopes に `commands`（manifest生成済みなら含まれているか確認）

Hermes連携では plugin settings の `snapshot` が必要です（未設定だと「元データが設定されていません」と返します）。
返答は押した本人だけに見え（ephemeral）、`slack_allowed_user_ids` 以外のユーザーは拒否されます。
同名のコマンドを他アプリ・Hermes側で登録していないことを確認してください。

### 任意: `/mcs <JSON>`（全機能の閲覧・依頼管理・運用承認）

Slackアプリの **Slash Commands** に `/mcs`、Usage Hint `<JSON>` を登録し、
Bot Token Scopesの `commands` を確認してアプリを再インストールします。
Socket ModeではRequest URLは不要です。`/mcs-summary` は引き続き使えます。
許可ユーザー・固定チャンネル・公開snapshot・cmd inboxとinteractive設定が必要です。
Hermesでは既存Slack接続を使い、独立モードでは同じコマンドを独立SDK接続に登録します。

```text
/mcs {"op":"read","kind":"qc","project_id":1}
/mcs {"op":"read","kind":"signals","project_id":1}
```

全コマンドと人承認のpreview/confirm形式は[plugin手順](../../hermes_plugin/README.md)を参照してください。
変更コードの反映にはHermes gatewayまたは独立Slackアダプターの再起動が必要です。
