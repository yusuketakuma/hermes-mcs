# hermes-mcs

### MCSの連絡を、探せる記録と次の確認へ。

**MedicalCareStation（MCS）の記録を、自分のMacで収集・整理・検索。**
**Slack（推奨）**の通知カードから、連絡の確認、担当の記録、タスクの作成までつなげます。
在宅医療・介護のチームで交わされた相談や経過を、あとからたどるためのローカルシステムです。
Slack・DiscordはHermes公式接続、[LINE WORKS](docs/guides/LINEWORKS.md)は独立した独自アダプターを使います。
Hermesを入れない[スタンドアローンモード](docs/guides/STANDALONE.md)では、Slack・Discordの直接接続と共通の操作経路を選べます。接続先SDKと実機運用の受入は別に確認します。
LINE WORKSでは同じ要約・原文・添付をトークルームの連続投稿で配信します。

[画面を見る](#demo) · [できること](#features) · [使い方を選ぶ](#use-cases) · [導入する](#quickstart) · [データの行き先](#data) · [最新の更新](#release)

| 収集 | 保存・整理 | 通知・操作 |
|---|---|---|
| 24時間・既定5分間隔 | Mac上のSQLite + ローカルLLM | Slack（推奨） / Discord / LINE WORKS |

このREADMEはmainの機能を説明します。[1.0.13のローカル支援](#candidate)は既定offで、実機・外部の受入条件は分けて記載します。導入する版の変更・更新手順は[CHANGELOG](CHANGELOG.md)と[Releases](https://github.com/yusuketakuma/hermes-mcs/releases)で確認してください。
通知を有効にすると患者名・本文・送信対象の添付が設定先へ送られます。[情報の行き先と安全境界](#data)を導入前に確認してください。

<a name="demo"></a>

## 画面イメージ — Slack

**通知先とカード操作はSlackを推奨します。** 要点の確認からタスクの確定まで、7画面で紹介します。
掲載画像の名前・投稿・数値はすべて架空です。実データや実投稿の匿名化例は使用していません。現行実装に基づく説明図で、実画面のキャプチャではありません。

![Slackの全体像：通知カードと本文・添付のスレッドを並べた画面例](docs/screenshots/slack-gallery/01-overview.png)

カードで要点を読み、スレッドで原文と添付を確認します。
画像を開くと拡大できます。画像内のボタンは説明用で、実際の操作はSlackの許可ユーザーが行います。

[確認・担当](#slack-card) · [操作メニュー](#slack-menu) · [タスク入力・確定](#slack-task) · [タスク一覧](#slack-tasks) · [患者サマリー](#slack-summary)

<a name="slack-card"></a>

### 確認・担当を、カードから共有

![Slack通知カード：確認済み・担当中と操作した人を表示する画面例](docs/screenshots/slack-gallery/02-notification.png)

`☐ 確認`・`👤 担当する`の結果をカードに反映します。新しい返信で表示内容が変わると、確認状態も更新されます。
MCSのスタンプはカードと未確認一覧に絵文字の集計1行で表示し、投稿ごとの観測日時はスレッドに併記します。自投稿は送信者IDで「自分」と表示します。
**カードの確認済み表示やMCSの「完了」スタンプは、正式なタスク完了を意味しません。**

<a name="slack-menu"></a>

### 必要な操作を、メニューから選ぶ

![Slackの操作メニュー：タスク作成・患者サマリー・検索などの画面例](docs/screenshots/slack-gallery/03-actions.png)

タスク作成、患者サマリー、抽出の誤り報告、自分のタスク、未確認一覧、検索を`操作を選ぶ…`にまとめています。
`☑ タスク完了`などの表示は、カードの状態や設定で変わります。

<a name="slack-task"></a>

### タスクは、入力してから内容を確認・確定

| ① 入力フォーム | ② 本人だけに表示される確認画面 |
|---|---|
| [![Slackのタスク入力フォーム：内容・担当者・期限・理由の画面例](docs/screenshots/slack-gallery/04-task-form.png)](docs/screenshots/slack-gallery/04-task-form.png) | [![Slackのタスク確認画面：内容と理由を照合して確定する画面例](docs/screenshots/slack-gallery/05-task-preview.png)](docs/screenshots/slack-gallery/05-task-preview.png) |
| 投稿に由来する下書きを確認し、担当者・期限・理由を入力して`確認へ`。 | 内容・担当・期限・理由を照合し、`確定する`を選ぶまで登録しません。 |

スタッフ一覧を取得できる場合は担当者を選択できます。一覧がない場合は手入力になります。
期限と理由は任意で、理由が空なら「通知カードからタスク作成」を記録します。
タスク作成はMCSへの依頼投稿を自動化する機能ではありません。

<a name="slack-tasks"></a>

### スレッドのタスクを、対応中・完了へ

![Slackの本人向けタスク一覧：タスクごとに対応中・完了を記録する画面例](docs/screenshots/slack-gallery/06-task-list.png)

未完了タスクがあるカードの`☑ タスク完了`から、このスレッドの一覧を本人に表示します。
対象のタスクを選び、人が`対応中`・`完了`を記録します。本文からAIが抽出した「完了」とも区別します。

<a name="slack-summary"></a>

### 訪問前に、患者サマリーで記録をたどる

![Slackの本人向け患者サマリー：薬・次回予定・未完了タスク・履歴取得状況の画面例](docs/screenshots/slack-gallery/07-patient-summary.png)

`🧾 患者サマリー`から、取得済み投稿の薬・最新バイタル・次回予定・未完了タスク・履歴取得状況を本人に表示します。
**暫定集約であり、確定した処方一覧ではありません。未取得の記録も「無い」とは扱いません。**

[Slackの導入・接続・許可ユーザー設定](docs/guides/INSTALLATION.md) · [操作の詳しい使い方](docs/guides/USER_GUIDE.md) · [画面画像のソースと更新手順](docs/screenshots/slack-gallery/README.md)

<details>
<summary><strong>Discordを使う場合の画面例を開く</strong></summary>

![Discord通知カードの画面例](docs/screenshots/discord-card.svg)

Discordでも通知カードと本文・添付のスレッドを利用できます。
詳細は[利用者ガイド](docs/guides/USER_GUIDE.md)を参照してください。

</details>

<details>
<summary><strong>LINE WORKSを使う場合の画面例を開く</strong></summary>

**要約・原文・添付を、トークルームへ連続投稿**

![LINE WORKS通知：要約カード・原文・添付をトークルームへ連続投稿する画面例](docs/screenshots/lineworks-gallery/01-delivery.png)

同じ要約・原文・送信対象の添付を、共有トークルームへ順に配信します。
表示の更新は新しい投稿になり、古いボタンと開いている確認画面は無効になります。

**タスクは、本人との1:1トークで入力・確認・確定**

![LINE WORKSのタスク操作：本人との1:1トークで入力し、内容と理由を確認して確定する画面例](docs/screenshots/lineworks-gallery/02-task-confirm.png)

許可ユーザーがカードから操作すると、入力や回答は本人との1:1トークへ届きます。
タスクは内容・担当・期限・理由を照合し、本人が`確定する`を選ぶまで登録しません。画像内のボタンは説明用です。

[LINE WORKSの導入・接続・許可範囲](docs/guides/LINEWORKS.md) · [画像のソースと更新手順](docs/screenshots/lineworks-gallery/README.md)

</details>

<a name="features"></a>

## できること

| したいこと | hermes-mcsでできること |
|---|---|
| 新しい連絡を確認する | 新着投稿を構造化要約カードで通知。本文・添付はSlack/Discordのスレッド、LINE WORKSの連続投稿で確認 |
| 過去のやり取りを探す | 履歴の保存・全文検索・患者ごとのタイムライン。途中で止まった履歴取得は再開 |
| 長い連絡の要点を読む | 薬・症状・依頼・バイタル・検査値などをローカルLLMとルールで抽出 |
| 患者の記録をまとめて見る | 薬・最新バイタル・次回予定・未完了タスク・MCS連携サマリーを暫定集約 |
| 次に確認する記録を見つける | 処方期間の終了前・終了後や、後続の記録が見つからない状態などをアラートとして提示 |
| 確認・担当・依頼を共有する | カードの確認・担当を記録。タスクは内容と理由を確認してから確定 |
| 全体の傾向を把握する | 投稿量・職種別内訳・未解決依頼などの読み取り専用統計。任意の日次ダイジェスト |

Slack・Discordでは通知カードと操作メニューに加え、`/mcs`コマンドから閲覧・依頼管理・運用承認を行えます。
LINE WORKSではカードのボタンと本人との1:1トークの `mcs JSON` を使います。Hermesあり・なしで共通の機能と人承認条件を使い、LINE WORKSの接続はどちらも独立アダプターが所有します。
`📊 サマリー`から全体・施設・患者・記録上の担当で絞り込めます。Slackは`/mcs-summary`、Discordは`/mcs`の`summary`、LINE WORKSは本人DMの「サマリー」でも呼び出せます。元データは読取り専用スナップショットです。
詳細は[利用者ガイド](docs/guides/USER_GUIDE.md)と[プラグインガイド](hermes_plugin/README.md)を参照してください。

**AIの抽出は候補です。患者サマリーは確定した処方一覧ではなく、カードの確認済み表示もタスク完了を意味しません。**

<a name="candidate"></a>

### 1.0.13のローカル支援（既定off・実機受入は別途）

次はmainで実装された任意の入口です。既定の有効化、外部GET・公開・配備の承認、実機の成功を意味しません。SDK境界、canonicalの品質・昇格、urgencyの通知条件、各経路の統合受入は継続中で、計画の全32項目が受入済みとは扱いません。

| ローカルで確認できる支援 | 入口と適用条件 |
|---|---|
| 暗号化offsite・鍵escrow・別配置への復元 | `mcs_backup.py`。保存先・鍵・独立SHA receipt・保持のオーナー承認が必要。定期運用は明示opt-in、新端末の復元は同意待ちで停止。[バックアップ・端末喪失ガイド](docs/guides/BACKUP.md) |
| 構造化薬歴・観測値とチャット候補の別出典閲覧 | `project_metadata.py`（明示sync）と`mcs_view.py`のsnapshot読取り。表示は`--publication`で明示し、`include_chat`は既定off。同一項目・正本・対応完了を自動判定しません。GETはsession・未読保持・権限の実受入後に承認。[MCS連携データの条件](docs/roadmap/mcs-api-survey.md) |
| 薬剤の表層名とは独立した成分候補注釈 | 明示した私有辞書の出所・SHA・承認に束縛した候補。別名の服薬行を統合せず、総称や複数候補を成分に確定しません。本番辞書の保管責任・利用条件は未確定。[抽出の範囲と残条件](docs/roadmap/extraction.md) |
| 導入・更新・ローカル診断の統一入口 | `sh scripts/mcs install`、導入後の`mcs setup`・`mcs update`・`mcs doctor`。診断結果やSDK版の取得は接続・更新後プロセスの受入証明ではありません。[更新時の確認](docs/guides/UPGRADE_AGENT.md) |

<a name="use-cases"></a>

## 使い方を選ぶ

<details>
<summary><strong>新着連絡を読み、チームで確認・担当を共有したい</strong></summary>

1. 通知カードの送信者・要約を読み、必要な本文や添付を確認します。Slack/Discordはスレッド、LINE WORKSは同じトークルームの連続投稿です。
2. `☐ 確認`で今の表示内容を確認したことを記録し、必要なら`👤 担当する`で担当を記録します。
3. 作業が必要ならタスクを作り、確認画面で内容・担当・期限・理由を確認して確定します。

新しい返信で表示内容が変わると、確認状態も更新されます。確認記録と実作業の完了は別に扱います。
[カードの操作と表示条件](docs/guides/USER_GUIDE.md)

</details>

<details>
<summary><strong>訪問前に、患者の経過と過去の相談をたどりたい</strong></summary>

患者サマリーで暫定集約を確認し、検索やタイムラインで根拠となる投稿をたどれます。
薬の変更、症状、相談、判断、実施、再評価を、保存済みの記録から確認します。
まだ取得していない範囲は検索されません。履歴の取得状況も合わせて確認してください。
[患者サマリー・検索・タイムライン](docs/guides/USER_GUIDE.md)

</details>

<details>
<summary><strong>後続の記録や期限を、もう一度確認したい</strong></summary>

アラートは「記録上、確認するとよい状態」を提示します。
薬の期間表現や依頼・経過の記録を根拠に、原文を読み、人が確認・判断します。
日次ダイジェストを有効にすると、未完了タスクや滞留アラートなどもまとめて確認できます。
機械がMCSへ依頼を自動送信することはありません。
[アラートの意味・対象・限界](docs/guides/USER_GUIDE.md)

</details>

<details>
<summary><strong>MCSの記録から、どんな傾向を見られる？</strong></summary>

診療と診療の間の多職種コミュニケーションを、次の7分野で扱います。

| 分野 | 記録からたどれること |
|---|---|
| 患者経過 | 症状、バイタル、状態変化、入退院、問題の反復 |
| 薬物療法 | 開始、中止、増減量、期間表現、再評価 |
| 多職種連携 | 誰から誰への相談、回答、判断、実施 |
| 組織間連携 | 薬局・診療所・訪看・居宅のやり取り |
| 時系列 | 記録上の対応時間、変化点、再発間隔 |
| 業務 | 記録の集中、未解決依頼、長期滞留 |
| 確認支援 | 通常との差、フォロー記録が見つからない候補 |

投稿数は重症度を、薬剤名の記載は現在の服用を、記録上の前後関係は因果関係を証明しません。
多職種の多さは連携の質を保証せず、アラートがないことも患者の安全を保証しません。
未来を予測する機能ではありません。[データ一覧と解釈の限界](docs/guides/USER_GUIDE.md)

</details>

<a name="quickstart"></a>

## クイックスタート

**導入前提:** macOS 13以降、普段のユーザー（sudo不要）、Xcode Command Line Tools、Homebrew、空き約12GB、利用権限のあるMCSアカウント。Pythonはインストーラーが用意します。

```bash
git clone https://github.com/yusuketakuma/hermes-mcs.git
cd hermes-mcs
./install.sh --preflight   # 読み取り専用の事前チェック
./install.sh               # 依存一式を導入。途中で止まったら修正して再実行
```

事前チェックのNG行に表示される`fix:`を確認し、`0 blocker(s)`になってから導入します。
初回の`./install.sh`は、Hermes経由かHermesなし（[スタンドアローン](docs/guides/STANDALONE.md)）かを尋ねます。`--mode standalone`で直接指定もできます。
最後の`Installed. Summary:`に表示される初回設定コマンドを、そのままコピーして実行してください。
設定ウィザードが通知先・本体の設定・最終チェックまで案内します。Slack/DiscordではHermesとの同期も行います。
本体の初回設定後は、ブラウザーの準備など診断に残った項目を解消します。LINE WORKSの認証・Callback・常駐設定と再診断は[接続ガイド](docs/guides/LINEWORKS.md)で続けます。
既存環境を更新する場合は[更新エージェント手順](docs/guides/UPGRADE_AGENT.md)を使います。移行先版の更新コードで計画・バックアップ・適用を行い、再導入は必要な場合に明示指定します。

**AIに導入を任せる場合**は、リポジトリを開いたエージェントに次のように依頼できます。

```text
docs/guides/SETUP_AGENT.md に従って、このMacへhermes-mcsを導入してください。
既存設定と回答を再利用し、まず通知先を選んで進めてください。
認証情報は私が端末へ直接入力します。投稿の収集・通知・既読化は開始前に範囲を確認してください。
```

| 次に進む先 | 内容 |
|---|---|
| [Slackを設定する（推奨）](docs/guides/INSTALLATION.md) | Slack app・接続・カード操作・許可ユーザーの設定 |
| [LINE WORKSを設定する](docs/guides/LINEWORKS.md) | 独自接続アダプター・秘密値入力・Callback・許可範囲の設定 |
| [Hermesなしで使う](docs/guides/STANDALONE.md) | `install.sh --mode standalone`・Slack/Discordの直接接続・切り替え |
| [導入ガイド](docs/guides/INSTALLATION.md) | 最短手順・成功の目安・通知先の設定・トラブル対応 |
| [AIエージェント向け導入手順](docs/guides/SETUP_AGENT.md) | エージェントに導入を任せるときの確認・実行手順 |
| [更新・バックアップ・復旧](docs/specs/lifecycle-spec.md) | 導入後の運用と更新時の確認 |

困ったときは、表示された同じインタープリターで`mcs_setup.py doctor`を実行します。
1.0.13の統一入口は`mcs doctor`です。既定はローカル診断で、LLM通信やサービス状態のprobeは明示指定します。診断が成功しても、SDKの通信・カード操作や配備済みworkerの反映まで検証したとは扱いません。

<a name="data"></a>

## 仕組みと情報の行き先

![収集・保存・整理・通知の流れ](docs/assets/flow-overview.svg)

収集した記録をMac上に保存し、ローカルで整理して、通知・検索・統計・アラートとして提示します。

| 経路 | 保存・送信される情報 | 条件 |
|---|---|---|
| このMac | 投稿・添付・患者情報と、ローカルAIによる整理結果 | 収集・保存・構造化抽出の基本経路 |
| Slack（推奨） / Discord / LINE WORKS | 患者名・本文・要約・送信対象の添付。タスク通知には内容・担当者名も含む | 通知先・対話カード・関連機能を設定した場合 |
| TypeSafe Jev API | 本文と必要なスレッド文脈。匿名化なし | 任意・既定OFFの意味チェック／抽出監査を有効にした場合 |
| 知識ストア向けローカル出力 | 患者名・病名・要約・薬剤等を含むMarkdown。匿名化なし | 明示的にエクスポート。出力後の同期・LLM利用は別経路で管理 |

「ローカルLLM」は、システム全体が外部へ送信しないという意味ではありません。
送信先と閲覧権限、AIの使用箇所は[SECURITY.md](SECURITY.md)で確認してください。

### 人が確認して判断するための境界

- **記録がない ≠ 対応していない。** アラートは記録上の候補で、対応漏れを断定しません。
- **人承認を記録。** 依頼登録・更新、アラートの却下、閾値変更は所定の承認とreceiptの記録を伴います。MCSへの依頼送信を自動化しません。
- **保存完了後に既読化。** 取得完了・DBへのcommit・snapshot timestampを条件にします。
- **スタンプは観測情報。** 件数・本人反応・観測日時を本文と分けて保存し、未取得と0件を区別します。取得日時は押下時刻ではありません。スタンプだけで確認・担当・業務完了を変更しません。専用の再取得は既定で無効です。有効化すると直近30日活動スレッドの先頭・返信を対象に、活動時期に応じた間隔で最大8件/回を再取得します。押下者取得も既定で無効で、有効化すると直近7日活動スレッドの全投稿について氏名・所属・職種を取得し、氏名・職種をスレッドに表示します。押下者ごとの最後の観測状態を無期限に保持し、取消を観測した時刻を残します。再押下時には取消状態を解除し、過去の全操作履歴は復元しません。未読保持の実証後に定期shadowを有効化できます。既定では反応値を通常表示へ反映せず、公開を別途有効化した場合だけ正常な取得値を表示へ反映します。本人の承知・完了を未応答候補の判定に使う設定も既定で無効で、正式タスクの完了とは別です。
- **認証とログの境界。** no-redirect / no-proxy。定期実行のログへ本文・氏名を出しません。明示的な閲覧出力は別に扱います。
- **説明用の架空データだけをリポジトリへ。** 患者データ・秘密情報・実投稿の匿名化例を入れません。

<a name="faq"></a>

## よくある質問

<details>
<summary><strong>AIが処方や医療判断を確定する？</strong></summary>

抽出・要約・アラートは候補です。原文と取得範囲を確認して人が判断します。
投稿から抽出した「完了」と、人がタスク操作で確認した完了も区別します。

</details>

<details>
<summary><strong>添付画像やPDFも解析する？</strong></summary>

添付は保存・通知とメタ情報の管理までで、内容の意味解析は行いません。
長文の要約も全件完了を保証せず、入力上限を超える記録は要確認として扱います。
[解析の限界](SECURITY.md)

</details>

<details>
<summary><strong>バックアップがあれば端末故障にも対応できる？</strong></summary>

日次SQLiteバックアップは同じMac上の保存です。端末喪失時の復旧保証にはなりません。
1.0.13の暗号化backup・escrow-key drill・新規配置restoreはローカル支援であり、原本上書きや通知再開の許可ではありません。別媒体・鍵・独立SHA receipt・保持の承認と復元訓練は運用側で行います。[鍵・同意待ちの条件](docs/guides/BACKUP.md)と[復旧の限界](docs/specs/lifecycle-spec.md)を確認してください。

</details>

<a name="release"></a>

## 最新の更新

<!-- BEGIN GENERATED:release -->

**v1.0.13 · 2026-10-05** — **安定稼働版：バックアップ・復元、導入と更新、取得の信頼性を強化**

暗号化バックアップと同意付き復元、統一の導入・更新・診断コマンド、取得失敗理由の保存、緊急度判定の修正に加え、スタンプ再取得と通知表示を統合します。新機能は既定offで、macOSでは復旧用Pythonの選択が必要になる場合があります。

> 更新前の確認：次回の更新（mcs_update の適用後に自動実行される services）、復旧（mcs_recover）、`mcs_setup.py services` の実行、または install.sh の再実行で雛形が再描画され、内容が変わった常駐ジョブ（llama-server・抽出worker 2本・cmd/int 取込・独立実行）はそれぞれ1回再起動されて Umask 077 が適用されます。再起動で処理中のLLM抽出が中断され得るため、抽出が空いている時間帯の更新を推奨します。それまで稼働中のジョブは従来の権限のままです。既に作成済みのログ（例: extract_drain_2.log）の権限は変わらないため、必要に応じて所有者のみ（chmod 600）に変更してください。独立実行の定期ジョブ（mcs_setup.py の _cron_plist と mcs_standalone/service.py が生成する plist、data/cron.log・data/standalone.log）にはまだ Umask を設定しておらず、別項目で対応します。

<details>
<summary>主な変更と更新時の注意を開く</summary>

- **新機能 · バックアップ鍵のエスクローと新端末への安全な復元**
  明示した人のエスクロー操作で専用Keychainへ鍵を作成し、既存鍵・既存記録の置換を拒否します。復元は新しい私有ディレクトリだけに行い、DB配置前に人の同意待ちマーカーを保存します。オフサイト・検証・訓練・復元の結果をローカルに永続記録し、秘密値を表示しないstatusで確認できます。

- **改善 · 時間切れの抽出と集計を次回へ継続**
  収集tickの残り時間がなくなった場合、ルール抽出と患者集計を処理単位の間で止めます。完了分は保存し、未処理分は次回へ残して、通知・終了処理の時間を確保します。

- **不具合修正 · 任意バックアップジョブの復旧時所有権を保護**
  復旧時のcron退役をmanifestのID・Script一致または既知wrapperの正規identityに限定します。mcs_offsite.shを既知ジョブとして扱い、名前だけが似た共有cronを削除しません。

- **動作・設定の変更 · スタンプを押した人の氏名をSlackに表示し、記録を無期限に保持**
  スレッドの各投稿で、スタンプ行の下に押した人の氏名と職種を表示します。押下者ごとの最後の観測状態を無期限に保持し、取消を観測した時刻を残します。再押下時には取消状態を解除し、過去の全操作履歴は復元しません。

**更新時の注意**

- 既定6ジョブは変更しません。任意backupは明示enabledと復旧先wrapper templateがある場合、または復旧snapshotで希望された場合に保持します。退役には所有証拠が必要です。standaloneは既存hostへ委譲し外部schedulerを追加しません。repo外に配備したrecoveryへの反映は別のオーナー作業です。

- 適用前にdocs/guides/BACKUP.mdで全必須policy、保存許可・媒体、端末外の鍵と独立SHA receipt、保持・RPO・平文scratchの保管/清掃・OS OpenSSLの利用方針を確認してください。私有の--state-dirと0600のpolicyが必要で、定期実行はconfig.json.backup.enabledとpolicyのboolean scheduled=trueの両方を明示した場合だけ有効です。設定・health・定期job所有の合成検証は完了していますが、実機反映・鍵の外部保管・復元訓練・稼働再開の人のreason/receipt/照合は別途必要で、既存の更新/rollback用restore承認を汎用解除に流用しません。

- 新しい plan / preflight は明示ポリシーと記録ディレクトリで実行します。鍵の作成・読取り、暗号化、復元、削除、サービス操作は適用しません。既存の scheduled:true の条件、保持・暗号・復元・状態更新の動作は変更しません。未知の容量ピークや認証済み復元は未確認として終了コード2、阻害条件は1で報告します。

- 対象はmcs_backup restoreで作った新規の私有配置だけです。サービスを起動せず、mcs_restore.pyのplan・approve・resumeを順に本人が実行し、独立したbundle SHA、actor、reason、custody-ref、hold_allを明示します。実機同意、サービス開始、通知再開、保管・保持方針は別のオーナー判断です。既存の更新・rollback承認経路は変更しません。

- 定期jobと管理manifestの実機状態は更新前に確認してください。実hostでの停止・退役は未実施です。独立runtimeは従来どおり単一hostが所有します。

- 新方式はbackup.snapshotへ絶対パスを明示し、backup.snapshot_dirと同時指定しません。従来のsnapshot_dirと日次scheduleはそのまま動作します。複数時刻は既存scheduleのhour欄をownerが列挙します。private policyのscheduled:true、max_rpo_seconds=86400、媒体・鍵保管・verify/drill・空き保持枠と、各offsite時刻より前の静的snapshot生成をownerが確認して設定・適用してください。この機能だけで実際のRPO24h達成を保証しません。

- 追加操作は不要です。この追加だけでは本文送付や外部受信を有効にしません。送付直前の本人認可、CLI・状態機械への接続、相手側受入と共同fixture固定は別工程です。

- 自動有効化や実送付は行いません。新版は明示された7型・全患者・3600秒以内のsnapshot鮮度・30日以内の保持が必要です。producer CLI、集合currentと撤回指示書の接続・相手側受入は別工程で、既存の旧CLIを新版へ自動切替しません。

- 新契約を使う場合だけ明示7型を持つ人承認済みauthを用意してください。ext_exportはauth・state_dir・outboxの絶対パスとsince_daysを指定し、既定の公開snapshotを使います。既存の明示フラグ形式は旧契約のままで、新版へ自動切替しません。network・定期送付・設定作成・実機有効化は行いません。

- 追加操作は不要です。本文送付は有効になりません。新しいローカル参照契約はmcs-ext-export/2へ分離する方針を採用済みですが、既存経路への送付接続・相手側受入は別工程です。

- 追加操作は不要です。既存送付経路・CLIは変更しません。これは私有ローカルファイルを使う合成用参照実装で、外部サービスへの接続・本番staging・暗号化保存・相手側の受入を有効にしません。

- 既存CLI・通常aggregate出力・hash・認可既定・wire契約・送付・公開は変更しません。呼出元が検証済みViewのread transactionを所有した状態で明示的に利用するローカル候補APIです。sender_kindはunknown固定で、分類とwire版のオーナー合意、相手側受入、分割・認可・CLI統合は未完了です。

- 既存の/1送付・認可・journal・旧CLI形式は変わりません。sender分類は --classify-senders を指定した場合だけ有効です。私有configの ext_export.since_days は1以上の整数が必要になり、小数や0の設定は拒否されます。受信側コマンドは合成用の参照実装で、外部への送信・本番staging・相手側の受入は行いません。

- 設定変更は不要です。モデル・抽出世代・canonical切替・監査対象・通知経路は変更しません。合成結果は人手200件以上のheld-out評価、G6・calibration、実モデル精度や本番容量の受入を満たしません。

- 既定投影・Loop入力・モデル・抽出世代・公開世代・設定は変更しません。新項目は未確認候補のままで、本番昇格には新項目の監査方針、Loop同一性と世代改版のオーナー判断、人手200件以上のG6およびcalibrationが必要です。承認receipt・昇格tokenは生成しません。

- 製品設定・モデル・抽出・公開・Loop・既存評価schema・G6基準は変更しません。全件pendingかつpromotion_eligible=false、人手検証済みラベルは0件です。220件の作成で人手200件以上・calibration・実capacityの条件を満たしたとは扱いません。検証と評価票の生成はevaluation/request_following_review.pyで明示実行します。

- 追加操作は不要です。更新前に保留された通知は理由が記録されていないため、引き続き not_recorded と表示されます。

- 抽出世代・LLM出力スキーマ・モデル・canonical設定は変更しません。新しい注釈は更新後に処理する抽出結果へ付加され、過去全件の自動再抽出は行いません。常駐抽出workerは更新時に再起動が必要です。この表記正規化だけでは販売名と成分を同一視しません。別機能の成分候補注釈には、辞書の出所・利用条件・承認・管理主体の確認と明示設定が必要です。

- 設定変更は不要です。既存cohortの読取り、推論の既定off、変換上限と公開・payload削除の承認条件を維持します。旧行の削除や修復は行いません。

- 既定では取得・公開とも無効です。cross_lists.pyのCLIは--database・--dataset・--token-cache・--read-only-getの明示が必要で、既存writer lockを取得して保存だけを行います。mcs_view.py cross_lists --datasetと--publicationで閲覧を別途許可します。定期実行には組み込みません。実APIの非空応答、ページ継続、未読true保持、セッション副作用は未確認で、本番有効化前に本人の受入が必要です。

- 処理単位の期限対応には設定変更は不要です。ルール抽出に45秒、rollupに30秒の終了余白を適用します。CLI直接実行のwatchdogは--watchdog-grace明示時だけ有効です。servicesで再生成する収集・履歴wrapperは既定60秒の猶予を渡します。config.jsonのwatchdog_grace_sで0（無効）〜3600秒を指定できます。60秒は暫定の設定値で、実機実測値や所有者の受入を示しません。実機へのwrapper反映・再起動は未実施です。

- 本番適用には辞書の入手・保管責任と利用条件の確認、approved_by、絶対パスの私有ファイルとSHA-256固定が必要です。config.jsonのdrug_mapにpathとsha256を明示するとtickの抽出後に候補を作成します。辞書なし・未承認・無効では注釈を適用せず、既存候補を退役します。LLMの再抽出や既読化・通知・人承認条件の変更はありません。

- 自動有効化・鍵生成・Keychain取得・既存原本の置換はありません。利用前に保存許可を表すpolicy_id、既存保存先とdevice/inode、同期対象外の私有scratch_dir、鍵保管確認、OS OpenSSL利用承認、max_snapshots、deletion=manual、max_rpo_seconds、max_snapshot_bytesを指定し、32バイトの鍵を専用fdまたは注入providerで渡してください。SHA-256 receiptは保存媒体と独立した信頼できる場所へ保管します。保持上限では削除せず停止します。復元訓練は新規隔離ディレクトリのみで、awaiting_consentの配送・writer保留を残します。訓練結果の平文保持・削除、鍵エスクロー、実機訓練と通知照合の方針は別途オーナー判断が必要です。

- 追加操作は不要です。保存形式と既存コードの表示は変わりません。

- GET・定期取得・公開の既定offは維持します。既存sessionと明示取得、公開には既存publicationオプトインが必要です。旧project-metadata/1のgroup scopeは根拠として維持し、任意の保存project_typeがある場合は矛盾を拒否します。患者への関連付け、臨床完了判定、相談詳細・返信取得、既読化、session更新は追加しません。実APIの受入完了は主張しません。

- 旧 --auth --records --state --sink の呼出しとLocalSinkの合成自己ackを維持します。handoffには同じ4入力を明示し、reconcileとwithdrawはstate・sinkを明示します。新しいhandoffの受領待ちは終了コード0とheld表示、reconcile/withdrawの未確認は2、拒否は1です。既存の認可・数値canonical・hash・envelopeを変更せず、実受信側との接続やC0/C1全体の受入を意味しません。

- 追加操作は不要です。延期が発生した実行だけ health.json の run に deferred_stages が追加され、延期がない実行では従来どおり出力されません。

- 追加操作は不要です。新しいフィールドは通常の収集実行で更新されます。last_ok_atは既存のoverall判定がokだった時刻で、全通知の配送確認やデータの完全性を保証する値ではありません。過去の正常判定を確認できない場合はnullです。

- 追加操作は不要です。新しい監視サービスは追加していません。理由の項目を持たない旧形式の health.json や、英小文字・数字・下線以外を含む不正な理由コードは unknown と表示し、理由なしとは扱いません。最終成功時刻が不明な場合も last_ok_at=unknown と表示します。health.json が期限切れ（stale）の場合、記録時点の理由は recorded_state_reasons として残し、警告行の reasons は unknown と表示します。

- 追加操作は不要です。案内の文言だけが変わります。

- 自動公開・定期出力・export・既存presetへの追加はありません。run_statsでstat=interaction_latencyを明示選択し、職種セルを表示する場合だけinteraction_privacy_policyのmin_pairs・min_actors・min_projectsを明示します。これらの値と職種群の粒度はオーナー判断が必要です。ケアチームを補助根拠に使うには、投稿以前に取得された新鮮で完全なproject_metadata_v1が必要です。

- DB schemaを8から9へ更新します。既存の保存データ・再試行回数・cursorは保持し、過去の理由がないjobは不明のまま表示します。旧snapshotも読取り可能です。更新前の旧DBを保持する通常のbackup/rollback手順を使用してください。

- 通常運用の追加操作やスキーマ更新は不要です。監査を行う場合だけ、mcs/core/ledger_audit.pyへ--dbで静的なスナップショットまたはバックアップのパスを指定します。既定のSQLite実行ステップ上限は10000000です。監査は修復・収集・送信を行わず、live WAL台帳は未確認として扱います。

- Ledgerの初期化時に同一書込みトランザクションで関係別の件数監査とガード導入を行います。既存DBは監査件数がゼロでもshadowから自動昇格しません。実DBの監査、観測期間、旧違反の容認・修復と有効化はオーナー確認が必要です。コードの巻戻しではDBのトリガーは消えません。

- 初回はsh scripts/mcs installを実行します。既存環境はinstall.shを再実行して~/.local/bin/mcsを導入し、同ディレクトリをPATHに追加してください。setupは既存設定を保持し秘密入力は端末で行います。update planは取得済みタグだけを使用し、apply・rollbackは既存の停止・バックアップ・承認契約を維持します。standaloneの外部applyは未対応のまま阻害を表示します。doctorの通信・サービス状態確認には--probe llm / --probe servicesを明示してください。サービス用PythonのSQLiteはWAL-reset修正版（3.51.3以降、3.50系は3.50.7以降、3.44系は3.44.6以降）が必要です。ホストruntimeを自動更新しません。

- message_revisions表を既存DBへ追加します。導入以前の編集は復元できず、API上の編集時刻や未観測の変更回数も保証しません。新しい本文保存と履歴は同じトランザクションに含まれます。

- --publicationを付けた明示閲覧だけが保存結果を返します。従来のstatusや外部exportへ自動追加せず、氏名表示は既定無効です。新GETの本番有効化は未読保持・session・権限の実受入後に行ってください。

- Hermes pluginの反映は既存のgateway再起動手順に従ってください。復元テキストの保留は自動解除しません。配送先での個別確認とオーナーの復旧方針が必要です。alertの結果不明時の自動再送は従来どおり行わず、カードのscoped receipt・grant・解除経路は変更しません。

- 通常は件数・理由・hashだけのreport-onlyです。出力は明示したabsoluteの新規destinationと本人所有private directoryが必要で、0600・上書きなしで作成します。network取得・設定有効化・モデル/DB実行は行いません。terms/承認・status方針の実確認はoperatorの別作業であり、値が不明なら保留します。旧mcs-drug-map/1のingredient/class辞書の読取り・既定設定は変更しません。

- 操作は不要です。更新後に恒久系の失敗が起きたjobから適用され、既存のpending jobは次の失敗時に判定されます。新しい取込依頼で従来どおり再開できます。

- 追加操作は不要です。ケアチームの同期と押下者取得（metadata_actors）が既定offのままなら常に不明です。氏名・IDは表示しません。

- 新しいGET・定期取得・公開は既定で有効になりません。実行には取得済みinventory、既存session cacheと明示オプトインが必要です。氏名保持と表示はそれぞれ既定offで、オーナーが判断した場合だけcare_teamのsyncに--retain-names、viewに--show-namesを明示します（写真・連絡先は指定に関係なく保存しません）。実APIでの未読保持・session非延長・権限・ページングの受入後にオーナーが本番有効化を判断してください。

- 本番への手渡し・外部送信・承認の自動生成は行いません。既存の厳密なLocalSink受領票と送付先・内容に束縛済みの旧journalは引き続き照合できます。束縛のない旧journalは自動補完せず確認対象として拒否します。認可・集計範囲・snapshot鮮度・既定preset・公開enumは変更しません。

- 既存jobのcursorと再試行間隔を保持します。floorの変更、対象患者の拡張、既読化、通知追加は行いません。この記録はAPI上の全履歴や臨床的完了の保証ではありません。

- 未指定時の既定は /usr/bin/python3 のままです。代替実行ファイルの自動選択・インストール・復旧ジョブの自動有効化は行いません。変更する場合は利用者が既存の安全な絶対パスを指定してください。既存ジョブと選択が異なるinstallerは書込み前に停止するため、mcs setup init --yes --recovery-python に絶対パスを付けて希望を保存し、所有権が確認でき、実行中でない状態で mcs setup services を使って修復してください。1.0.12以前からの更新では、config.json に recovery_python を手で追記してから plan を再実行し、reinstall経路は --install-arg=--no-recovery を付けてapplyしてください。所有済みで停止中のjobはmerge後のservicesが置き換えます。実機への適用・Python更新・配備は別途承認が必要です。

- 自動適用や既定動作の変更はありません。計画は明示した静的スナップショットを使用します。記録には人の確認・理由・操作者のハッシュ・計画との一致、別デバイス上の実際に検証したバックアップと期待ハッシュ、所有者のみアクセスできる明示ディレクトリが必要です。本番修復、疑わしいfloorの扱い、GET負荷、実施担当は別途オーナー判断と既存の承認・receipt経路が必要です。

- 設定変更・DB移行は不要です。既存のHermes経由・独立実行・LINE WORKSの操作と承認条件は同じです。出力ファイルと基準統計の保存でディスク同期に失敗した場合は、保存処理がエラーとして報告されます。この変更は公開済み1.0.12の後続変更で、稼働中のサービスへの反映は別途必要です。

- 既定の_FACT_V2_PROMPT・モデル・fact source・抽出schema・公開projection・Loop入力と世代は変更しません。候補実行はextract_facts_v2のrequest_following=Trueまたはsemantic_v4.extract_request_followingを明示して呼ぶ必要があります。repairにも同じflagを渡します。人手200件以上のG6、calibration、新項目の監査とLoop同一性・公開世代の判断は引き続き昇格条件です。自動昇格や承認tokenは追加しません。

- 公開は既定offです。mcs_view.py response_observationsで--publicationと--max-age-sを明示して閲覧します。callableの公開offではSQLを実行せず、primary入口も候補行を調べません。鮮度の新しい既定秒数は設けず、未指定はfreshness_policy_unknownを返します。通常収集で値が変わらず観測時刻が保持される場合や、本文更新後の束縛を証明できない場合も、不明として扱います。公開・鮮度条件の所有者判断と実機受入は別途必要です。

- 追加操作は不要です。第1層公開・actor取得の既定offと独立した公開条件は変更しません。本番の有効化・公開は既存の所有者判断と実API受入に従ってください。

- doctorのJSONにruntimes.selectedとruntimes.recoveryを追加します。選択runtimeはPython 3.10以上、独立recoveryはPython 3.9以上を確認し、SQLiteは3.51.3以降または3.50.7・3.44.6系列の修正版を必要とします。servicesは退役・描画・起動前、applyはlock・journal・中断回復前に停止します。macOS installerは復旧用Python（既定/usr/bin/python3）が危険・不明なら書込み前に停止します（Python 3.11以上が未導入の新規Macではstage 6のtool/plist配備前）。macOS標準Pythonは影響版のため、--recovery-pythonで安全な独立Pythonを指定するか--no-recoveryを選ぶまで、更新・services適用も停止します。代替Pythonへの自動切替は行いません。

- 追加設定は不要です。signal_feedback統計はローカルで明示選択した場合だけ利用でき、既存export presetには追加しません。新しい原因と抑制は更新後の観測から記録し、旧行の不明理由は補完しません。20未満の標本では率・区間・所要時間を非表示にします。プライバシー上の最小集団基準は未決事項として出力します。

- include_chatは既定falseで、従来の読取りでは候補データを調べません。mcs_view.py project_metadataの--publicationと--include-chatを両方明示すると、medication_periods・observation_items・observation_valuesの本文候補を別出典として併記します。取得・本番適用・モデル既定・schema・薬剤辞書や単位変換は変更せず、項目の同一性や正本を自動決定しません。

- 追加操作は不要です。既存シグナルカードは次回の更新処理で表示が整合します。定期配信・患者名・公開・semantic採用・新規再通知の有効化条件は変更しません。新規の緊急再通知とシグナルtext経路の警告整合は、親側の別実装・既存オーナー判断に従ってください。更新直後は表示元の指紋が変わるため、送信済みカードが一度ずつ再描画（編集）される場合があります。

- 追加操作は不要です。削除済み返信を含むスレッドがある環境では、状態表示と修復計画の未取得スレッド数が減ることがあります。reply_countの実APIでの意味は未確認のため、この件数は取得完了の証明ではありません。

- 実行時の設定変更や配備操作は不要です。v1.0.0〜1.0.2の手動更新条件、standaloneのhost経由条件、schema巻戻しの個別承認を維持します。テストコマンドはtests/fixtures/schema_upgrade/update-paths.jsonに記録しています。

- 既定はoffのままです。収集tickとinteractive初報の封印へ接続済みで、有効化にはurgency_escalationのmode・明示的なroom_cooldown_min・通知先・応答者識別の設定が必要です。sourceは既存計画どおりLLMのみを扱い、after_min=30、repeat_min=60、max_repeats=2、max_per_day=10を継承します。shadowは送信せず監査だけを残し、有効化・冷却時間・確認者・通知先の実受入は別途必要です。

- ルール抽出世代を6から7へ更新します。旧世代のルールartifactは通常の抽出処理で再生成され、再集約・通知表示へ反映されます。再抽出が完了するまでは旧結果が残り得ます。サービス反映は通常の更新手順で行い、暗黙の時制や臨床的緊急性を判定する機能ではありません。

- 追加設定は不要です。媒体・scratchとの重複、元DBと記録の同一ディレクトリ配置は引き続き拒否します。

- receiveは入力ファイルがなくてもローカル受信状態を更新します。保持期限に合わせて明示的に実行してください。定期ジョブは追加されず、実受信側の期限内削除を証明するものではありません。

- Hermes連携でDiscordのカード操作を使っている場合は、プラグインを更新した後にgatewayを再起動してください。設定し直す必要はありません。

- 次回の更新（mcs_update の適用後に自動実行される services）、復旧（mcs_recover）、`mcs_setup.py services` の実行、または install.sh の再実行で雛形が再描画され、内容が変わった常駐ジョブ（llama-server・抽出worker 2本・cmd/int 取込・独立実行）はそれぞれ1回再起動されて Umask 077 が適用されます。再起動で処理中のLLM抽出が中断され得るため、抽出が空いている時間帯の更新を推奨します。それまで稼働中のジョブは従来の権限のままです。既に作成済みのログ（例: extract_drain_2.log）の権限は変わらないため、必要に応じて所有者のみ（chmod 600）に変更してください。独立実行の定期ジョブ（mcs_setup.py の _cron_plist と mcs_standalone/service.py が生成する plist、data/cron.log・data/standalone.log）にはまだ Umask を設定しておらず、別項目で対応します。

- 追加操作は不要です。LINE WORKS独立アダプターの再起動後に有効になります。

- 追加操作は不要です。すでに回数が失われた失敗行は、この更新後の再試行から改めて最大3回まで数えます。

- 追加操作は不要です。既定以外の保管場所で運用していた場合、以前のバックアップとスナップショットが既定の保管場所のdata配下に残っていることがあるため、必要に応じて確認してください。

- 追加操作は不要です。統計の定義版(definition_version)が2026-10-05に上がるため、以前の出力と件数を比べる場合は版の違いに注意してください。

- 追加操作は不要です。既定値・設定・対象の範囲は変わりません。

- 追加操作は不要です。Hermes plugin の反映には gateway 再起動が必要です。

- この不具合で/mcsが使えない場合は、mcs_setup init --plugin-project-ids 1,2 のように設定し直すか、hermes config setでproject_idsを整数の一覧に設定してください。設定済みの値は自動では書き換えません。

- 追加操作は不要です。config.jsonをシンボリックリンクで配置している場合は、通常ファイルに置き換えてください。

- 追加操作は不要です。既定値・設定は変わりません。

- 追加操作は不要です。反映には通知ワーカーの再起動が必要です。

- 追加操作は不要です。standaloneモードの常駐プロセス再起動後に有効になります。Hermesモードとlaunchd運用は変わりません。

- 追加操作は不要です。終了コードは従来どおり1です。

- 追加操作は不要です。拒否された場合はgitの状態を確認してから承認をやり直してください。

- 追加操作は不要です。

- 追加操作は不要です。schema_version_unknownが出た場合はDBファイルの状態を確認してから更新をやり直してください。

- 追加設定は不要です。urgency_escalationは既定offを維持します。有効化済みの場合、E2は初回通知のカード配送が証明できる投稿だけが対象となり、text初報にはE1だけを送ります。

- 追加操作は不要です。旧通知先の記録は自動で決着させず保持します(再送しません)。health.jsonのcards.retired_unsettledとnotify.retired_heldで件数を確認できます。

- metadata_actors=trueの環境で反映されます。反映にはHermesではgateway、独立実行では対応adapterの再起動が必要です。既定offのままで、実機有効化・再起動は別作業です。氏名・所属を新たに保存するため、押下者表に列を加法で追加します(統合後のschemaは9)。アイコンは保存しません。カード本体には氏名を出しません。最後の完全取得から24時間を過ぎた・取得に失敗した場合は時点を併記します。

- 追加設定は不要です。反映にはHermes gatewayまたは独立アダプターの再起動が必要です。絵文字は見ました👀・承知🙆・感謝🙏・いいね👍・完了✅の代替表示で、MCSのスタンプ画像そのものではありません。観測時刻は押下時刻ではありません。

- 既定では動作は変わりません。metadata_refresh_publishは未読保持の実証とmcs/views/metadata_report.pyでの照合後、metadata_actorsは押下者の表示・保持の判断（2026-10-03決定）を踏まえてに有効化します。どちらもmetadata_shadow=trueが前提です。DBはschema 8のまま押下者用の表を加法で追加します。

- metadata_shadow=trueの環境で反映されます。MCSへの取得は1回の収集あたり最大8件(従来5件)、押下者の取得は最大4件(従来2件)に増えます。未読保持と実機負荷を確認してから有効化してください。30日を超えたスレッドは再取得せず、最後の観測を保持します。未確認カードと未応答の薬剤師宛候補は経過によらず30分以内に再取得します。

- 追加設定は不要です。反映にはHermes gatewayまたは独立アダプターの再起動が必要です。これから取り込む履歴補完の返信も新しいスレッド投稿になるため、スレッドの通知が増えることがあります。既に前の投稿へまとめて書き込まれた返信と、1件ずつ分ける前の古いまとめ投稿はそのまま残し、再投稿しません。

</details>

[すべての変更と技術詳細](CHANGELOG.md) · [GitHub Releases](https://github.com/yusuketakuma/hermes-mcs/releases)

<!-- END GENERATED:release -->

<a name="docs"></a>

## ドキュメント

[安定稼働版1.0.13の計画](docs/development/RELEASE_1.0.13.md)は、
旧1.0.13〜1.0.15・追加提案・バグ修正・4つの統一コマンドを集約しました。
ローカル実装と合成検証は完了し、実機・実API・人手・相手側の受入条件は同計画の§6で追跡します。

| 読む目的 | 文書 |
|---|---|
| 使い方・画面・データの読み方 | [利用者ガイド](docs/guides/USER_GUIDE.md) |
| 導入・接続・設定・トラブル対応 | [導入ガイド](docs/guides/INSTALLATION.md) · [LINE WORKS接続](docs/guides/LINEWORKS.md) · [スタンドアローン](docs/guides/STANDALONE.md) · [エージェント向け手順](docs/guides/SETUP_AGENT.md) |
| データ取扱い・安全境界・AI・復旧の限界 | [SECURITY](SECURITY.md) |
| 更新・バックアップ・復旧 | [ライフサイクル仕様](docs/specs/lifecycle-spec.md) · [鍵escrow・端末喪失ガイド](docs/guides/BACKUP.md) · [配備資産](deployment/README.md) |
| 開発・コマンド・統計・アラート定義 | [開発リファレンス](docs/development/DEVELOPMENT.md) · [Hermesプラグイン](hermes_plugin/README.md) |
| エクスポート・意味解析の評価 | [外部出力契約](docs/specs/external-export-contract.md) · [意味解析の評価](docs/specs/semantic-evaluation.md) · [rollout](docs/specs/semantic-facts-v2-rollout.md) |
| 今後の計画 | [ロードマップ](docs/ROADMAP.md) |
| リリースとREADMEの更新ルール | [リリースノート規則](docs/development/RELEASE_NOTES.md) · [README運用](docs/development/README_MAINTENANCE.md) |

## ライセンス

Private repository — 現時点で公開・再配布は想定していません。
利用・改変はリポジトリ管理者の明示許可に従ってください（[LICENSE](LICENSE)）。
