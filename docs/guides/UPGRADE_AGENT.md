# MCS 更新実行手順書（AIエージェント用）

Hermes経由の導入・更新にはuvが必要です。Hermesのディレクトリで依存を解決し、安全性のための依存設定を適用します。uvがない場合は変更前に停止し、pipへ切り替えません。`--no-brew`を使う場合はuvを事前に導入してください。

1.0.25〜1.1.0の推奨Hermes agentはv0.22.2ベースのMCS companion（固定commit `2c08ca7ade8f867d07e4bff6581be54405b0c9d0`）です。新規導入・CI・更新の固定値をそろえています。以前の固定版 `b7758d8`・`f36c1da` と、それ以前の認識済み固定版は再導入経路の移行対象です。変更中・未知のcheckoutは自動で置き換えません。Hermes経由の環境は再導入（`--reinstall`）で固定版を移行し、gatewayを再起動します。独立動作（Hermesなし）の環境はHermesの移行対象ではありません。


このガイドはv1.1.0候補のinstall/update/setup/doctorと復旧手順に対応します。公開前の未受入事項は[1.1.0の受入票](../releases/v1.1.0/ACCEPTANCE_1.1.0.md)で確認してください。1.0.29の状況は[1.0.29の受入票](../releases/v1.0.29/ACCEPTANCE_1.0.29.md)に残します。1.0.28の状況は[1.0.28の受入票](../releases/v1.0.28/ACCEPTANCE_1.0.28.md)に残します。
1.0.22の合成検証・公開・実機反映の状況は[リリース受入票](../releases/v1.0.22/ACCEPTANCE_1.0.22.md)、
1.0.21は[1.0.21の受入票](../releases/v1.0.21/ACCEPTANCE_1.0.21.md)で追跡する。
過去の1.0.13実機受入計画は[開発・受入計画](../releases/v1.0.13/RELEASE_1.0.13.md)に残す。

### 1.1.0で追加された更新時の確認

- 対話形式の更新が完了したとき、または更新が不要だったとき、Jev・Anthropic・OpenAI、Slack、Discord、LINE WORKSの認証情報設定を選べます。省略した場合は、後から `~/.local/bin/mcs setup api-keys` を実行できます。起動コマンドや実行環境を確認できない場合は、担当者に導入状態の確認を依頼してください。更新の確認（plan）と非対話の更新では認証情報設定を起動しません。
- Jev・Anthropic・OpenAIのキーはMCSの `.env` に保存します。Slack・DiscordのHermes経由の接続は、選んだHermesプロファイルの `.env` に保存します。独立動作のSlack・DiscordとLINE WORKSは、それぞれの既存の保存方法を使います。独立動作のSlack・DiscordとLINE WORKSでは、既存の認証情報の変更は保留中です。表示される案内に従い、担当者に変更方法を相談してください。
- Anthropicの費用設定を省略した環境では、1日25ドル・1か月75ドルの上限を適用します。明示した `null`（個別の上限を解除）・`0`・低い上限・`mode=off`・対象範囲は維持します。以前のCLIによる解除で項目が削除されていた場合も既定値が適用されるため、解除を続ける場合は更新後に `cloud-llm budget --daily-usd none` または `--monthly-usd none` を指定してください。設定ファイルへ既定値を書き込む移行はありません。
- Jevはキーがあり、modeと予算が未指定なら、従来どおり通常運用の `assist`・1日10000要求です。明示したmode・低い上限・対象範囲を保持し、modeを明示して予算を省略した環境は上限 `0` を保ちます。キーがない場合は両サービスとも送信しません。費用・件数の確認と変更は[設定ガイド](ANTHROPIC.md)に従ってください。
- 任意機能のセットアップ案内に「既定に従う／案内を表示する／案内を省略する」の選択が加わりました。CSDC（状態監視・遠隔支援）の案内は、この版では既定で省略します。更新後に `mcs_setup.py doctor` の「任意機能の案内」で現在の選択を確認し、変える場合は端末で `mcs setup features`（PATH未設定なら `~/.local/bin/mcs setup features`）を実行してください。
- この選択は案内を表示するかだけを決めます。CSDCの登録・同意・鍵の保管・送信は行わず、案内を省略しても既存の登録や送信は止まりません。登録や稼働の状態はCSDCの手順と診断で確認します。
- 1.1.0ではCSDCを有効化・接続して提供しません（コードは同梱）。新規導入では`config.sdc.enabled`が未設定のため、CSDCの専用ジョブは登録されません。すでに`sdc.enabled=true`を設定している環境は、この更新で自動的に停止・変更しません（停止は別の明示操作です）。保存済みの有効状態だけで送信を開始しない設定再確認の修正を含みます。送信開始済みのHTTP要求を即時に回収する保証はありません。`optional_features.csdc`は案内の表示だけを決めます。
- 「既定に従う」を選んだ（または選んでいない）場合は、将来の版で既定が変わるとその既定に従います。明示した選択は版が変わっても保持します。

### 1.0.29で追加された更新時の確認

- 通常の更新ツールで、Hermes gateway、使用中の独立通知アダプター、抽出workerへ同じ候補を反映してください。接続世代・範囲の確認、収集、抽出の修正は再起動後または次の定期処理から有効になります。checkoutだけの変更では適用完了になりません。
- 共通の設定読取は、`config.json` がシンボリックリンク・通常ファイル以外・1 MiB超過なら読まず、未設定と同じ扱いにします。共通の認証情報読取は環境変数を優先し、`~/.mcs/.env`・`~/.hermes/.env` がシンボリックリンク・通常ファイル以外・64 KiB超過ならそのファイルを読み飛ばします。初期設定などには、読み取れない設定をそのまま保存せず停止する場合もあります。導入時の復旧設定確認・既存設定の明示保存とSDCの設定処理は、`config.json` の上限が64 KiBです。リンクにしている場合は更新前に実ファイルへ置き換えてください。共通読取ではグループ/他者が書込みできるファイルは警告 `local_file_permissions_weak` を出して読み込みます。`runtime_mode=standalone` の `config.json` がグループ/他者書込み可なら `standalone_configuration_invalid` で起動を止めるので、`chmod 600`（または644）にしてください。
- MCSサーバーがHTTP 429を返した場合、Retry-After（最大60秒、その回の期限内）を守って再試行し、1回の定期実行で2回受けたら残りの取得を次回へ回します。取得範囲・既読化・人承認・no-redirect/no-proxyは変わりません。本文を取得できていない親投稿がある患者は既読にしません。
- DB schema 9、ルール世代24、LLM抽出世代9、モデルは維持します。患者集約は版12（期間検査）で再構築されます。抽出・意味判定のコードの識別値が変わるため、旧候補に対する承認・保留receiptは新しい候補の承認になりません。保存済みの結果と人の承認は自動で置き換えません。
- 次にDBを開いたとき返信用の索引を自動で作ります（schema versionは変えません）。返信の多いDBでは初回に時間がかかります。更新後の最初の処理で読取り用スナップショットを1回作り直します。複数の変更境界のバックアップを保持するため、空き容量を確認してください。
- 送られなかった通知の記録は、90日より古く、同じカードに新しい版があり、外部への影響の記録や参照がないものを日次の整理で削除します。旧版へ戻しても削除済みの記録は戻りません。
- ローカルAIの定期再起動スクリプトを使用する環境では、`deployment/` の更新後の配備候補を適用してください。スロット状態を取得できない環境では自動再起動を見送ります。
- 新しい診断の確認は `mcs_setup.py doctor --probe data`、保存済み添付の点検は `mcs_repair.py attachments` です（[収集の診断](COLLECTION_DIAGNOSTICS.md)・[添付の点検](ATTACHMENT_RECOVERY.md)）。どちらも読取り専用で、更新の成否判定には使いません。

### 1.0.28で追加された更新時の確認

- 内部モジュールの配置を整理しました。通常の更新ツールでサービス定義の同期と再起動まで行い、Hermes gateway、使用中の独立通知アダプター、抽出workerへ同じ候補を反映してください。旧プロセスが新しいカードを扱うと本文・添付が欠落するおそれがあります。checkoutだけの変更では適用完了になりません。復旧ツールの同期と再導入の要否は§2の計画に従い、既存の省略オプションを維持します。
- MCSスタンプ送信は、`stamps.account_user_id`と1人の`stamps.operator`を設定すると、`stamps.enabled`を省略しても有効になります。両方を設定済みで`enabled`を省略している環境は、今回の更新でOFFからONへ変わります。無効のままにする場合は`stamps.enabled=false`を設定してください。設定した操作者が押したときだけ、MCS収集アカウント名義で操作します。チャットの絵文字の自動転送は行いません。押下者名の収集設定は別で、既定OFFのままです。
- カードに直接表示されるスタンプは表示中の最新投稿を対象とし、自分の同じスタンプを押すと取消になります。別の投稿や理由を指定する操作は従来のフォームと確認画面を使います。画像・PDFは添付が属する投稿が対象です。既存カードのボタンは通常の更新時に反映されます。LINE WORKSでは設定切替時に対象カードの更新版が追加投稿される場合があります。
- `data/stamp_operations`の記録を削除・巻戻ししないでください。送信結果が不明な操作はGETで照合し、自動では再送しません。旧版へ戻す際も未完了・結果不明の記録を保持し、旧版で未対応の操作を処理済みと扱わないでください。1.0.27は今回のスタンプ操作に対応していないため、戻した後に保留中の操作が実行されることはありません。[スタンプの復元契約](../specs/stamp-management.md)を参照します。
- 抽出・意味解析の実装を移したため、コードの識別値が変わります。旧候補に対する承認・保留receiptは新しい候補の承認になりません。必要な再評価・承認は既存の手順で行います。DB schema 9、ルール世代24、LLM抽出世代9、モデル・公開条件は維持し、配置整理だけを理由に全投稿を一括再解析しません。
- 読取り用スナップショットの検証時間はコピー時間も考慮します。上限180秒と収集全体の期限、検証内容は維持します。公式医薬品マスターの同梱と私有辞書の承認・有効化は別です。

### 1.0.27で追加された更新時の確認

- 抽出workerの引数が`--slot`から`--lane 2|3`に変わります。通常の `mcs update apply` は常駐plistの再同期と再起動を自動で行います。手動でcheckoutだけを更新した場合は `mcs_setup.py services` が必要です。旧`--slot`のままのworkerは引数エラーで起動しません。`mcs_check.sh`はレーン1（wire 0）を固定します。設定項目`extract.patient_context_nightly`・モデル・DB schemaは変わりません。従来の夜間の患者まとめは「患者コンテキスト作成」に改称し、24時間、保存済み投稿が現在の解析方式で解析済みのアクティブ患者を対象にします（legacyは世代9、canonicalは検証済みの現行投影。再試行上限に達した投稿は対象判定から除外）。
- レーン3は持ち越した結果の保存、患者コンテキスト作成、過去分の抽出の順に進みます。レーン2/3の推論はGPUの順番待ちで直列化します。患者コンテキスト作成の呼出時間上限は60秒から300秒になります。公開条件、抽出世代、モデル、収集間隔は変わりません。
- 薬剤辞書が未設定の対話初期設定では、追加照合を既定「はい」で提案します。利用条件・採用方針の確認は運用者が行います。既存辞書は保持し、非対話の `init --yes` で自動作成・承認はしません。
- 性能比較用のベンチは手順とスクリプトのみです。通常更新では実行せず、サーバ設定 `-ub` も変更しません。実測の実行窓は別に承認を得ます。

### 1.0.26で追加された更新時の確認

- 使用中のHermes gateway、独立動作のhost・通知アダプター、解析workerを更新・再起動してください。LINE WORKSの独立アダプターも対象です。既存の導入内容・設定・履歴を保持し、1.0.25と同じHermes固定版を使います。
- ルール世代を24へ更新します。再起動後、保存済みの投稿を通常の処理で順次再解析します。患者コンテキスト作成（当時の名称は夜間の患者まとめ）も世代が異なる旧走査結果を再利用せず再走査するため、一時的に待機が増えます。LLM抽出世代9、処理枠・既定値は変えません。履歴の削除や手動の全件再投入は不要です。
- 一覧と通常の解析はアクティブ患者を優先し、アーカイブ済み患者は後に処理します。アーカイブ済み患者の新規・変更投稿も通知せず解析対象に保存しますが、過去投稿の一括投入はしません。患者コンテキスト作成とスタンプ取得は引き続きアクティブ患者だけが対象です。
- 更新・復旧で手編集を検出した場合は、対象を表示して停止します。表示されたファイルを確認し、手編集を保持してから再計画してください。復旧監視を導入済みの場合は、更新計画の`recovery_tool_changed`も確認します。再導入を伴わないapply経路では、§2の表に従ってcheckout外の復旧ツール本体も同期してください。`services`の再設定だけでは本体を更新しません。前回の導入内容・opt-out・復旧Python・復元承認と保留を維持します。
- 通知は自動で取り消しません。送らず取り消すと運用者が決めた対象だけを、[通知の復旧手順](NOTIFICATION_RECOVERY.md#4-未発行の通知を送らず取り消すslackdiscordline-works)で扱います。未発行の更新通知と、未送信が確定して再送上限に達した集計通知に限り、計画の照合・通知workerの停止・人承認・理由・操作IDが必要です。過去の送信結果不明という記録や、復元保留は変更しません。

### 1.0.25で追加された更新時の確認

- Hermes経由の環境では、更新時に`--reinstall`を指定して上記の固定版へ移行します。Pythonが既に3.14でも、認識済みの旧固定版はコードと接続用ライブラリを更新し、旧環境を退避して新しい環境を作ります。未知のcommit・ブランチ上・変更中のcheckoutは自動で置き換えません。§4で確定した導入済み機能とopt-outを維持し、使用中のgatewayを再起動してください。独立動作ではhostと使用中の通知アダプターを更新・再起動します。
- 患者コンテキスト作成（当時の名称は夜間の患者まとめ）は、`extract.patient_context_nightly.enabled`（生成）と`publication`（限定閲覧）が**省略時ともに有効**です。既存環境も設定を省略したまま更新すると有効になります。明示した`false`は維持し、不正な設定では無効にして診断します。生成だけを評価する場合は`publication=false`、生成を止める場合は`enabled=false`を明示してください。新着抽出に過去文脈を注入する`extract.patient_state_context`は既定無効のままです。共有チャットへの自動配信・第2入力源は追加しません。
- ルール世代23・LLM抽出世代9・患者背景契約4へ更新します。抽出workerの再起動後、旧世代の結果は通常の処理で順次再生成され、一時的に処理待ちが増えます。患者コンテキスト作成は契約不一致の旧走査結果を再利用せず、履歴を再走査します。再走査が済むまで患者コンテキストの表示は未反映扱いになります。履歴を消す操作、手動の全件再解析、保存済み結果の書換えは不要です。
- DB schemaは9のままで、既存の履歴・設定・バックアップを保持します。checkout外の`~/.mcs-recovery/mcs_recover.py`も§2の表に従って同期し、復旧機能を使う環境では`--no-recovery`で省略しません。復旧本体の同期と、本番の復元操作は別です。復元hold・送信結果不明・再送上限は更新で解除せず、対象backupと損失報告へ束縛した既存の人承認を使います。

### 1.0.24で追加された日本語の操作案内

導入・初期設定・診断・更新・復旧のヘルプと対処案内は日本語です。`mcs update --help`で操作を確認できます。コマンド名・引数・終了コード、計画/状態のJSONと状態コードは自動処理との互換性のため維持しています。配備済み復旧コピーの同期と、更新後の実接続・通知確認は下記の手順で別々に確認します。

### 1.0.23で追加された更新時の確認

実装・合成検証と未受入事項は[1.0.23受入票](../releases/v1.0.23/ACCEPTANCE_1.0.23.md)で確認します。公開・本番適用は別工程です。

- 新規カードだけlayout3（要約・スタンプのヘッダと原文を分離）を使います。既存layout1/2の指紋・部品名を維持し、表示版だけによる一斉編集・再送は行いません。既存カードの実内容が変わった場合は旧部品構成のまま更新し、要約等を含む本文を追加投稿する場合があります。
- LINE WORKSは内容を編集・撤回できません。初回は最大120秒の要約待ち（緊急時を除く）、新規カードはヘッダと原文の最低2部品、以後の改訂は上限案内を含め投稿ごと最大3件です。旧形式の内容変更時には原文の追加投稿が残ります。`data/health.json` の `cards.layout_inventory` のカードlayout別件数・結果不明部品を確認してください。件数だけで所要時間を断定せず、部品数・接続先の待機時間・実際の配送進捗を併せて確認します。
- Hermesは使用中の全profileのgateway、独立実行はhostと該当adapterを既存手順で再起動します。指定ownerで `worker_started` が記録され、他profileは所有者不一致で起動しないことを確認します。doctorのworker証跡・PID確認は実配送成功の証明ではありません。通常のdoctorはDBを開かず、復元markerがない場合の配送holdは未確認とします。個別の復元holdは明示した `doctor --probe data` で読み取り確認します。owner不明・不一致・実bot ID不一致では起動を拒否し、設定と固定SDKの契約を確認します。
- 更新で送信結果不明、再送上限、復元holdを解除しません。部品ごとの解決はplanと人承認receiptへ束縛します。LINE WORKSの遠隔不存在は操作者の申告であり、未送信確定ではありません。
- 1.0.23はDB schemaを変更しないため、通常のrollbackではDBを復元しません。layout3カードが1件でも作られた後は、1.0.22以前へのrollbackを拒否します。1.0.23を維持するか、人承認付きの復元手順で更新前backupを復元してから戻します。旧コードの直接起動やDB書換えで回避しません。
- canonicalの昇格証拠は候補指紋と独立した人承認receiptへ束縛されます。モデル・prompt・分割規則・設定が変われば旧証拠を再利用せずlegacyへ戻ります。PLAN_VERSION 1→2も証拠失効の対象です。合成データの評価成功だけでは昇格できません。
- 復元訓練はcollect-onlyで、配送holdを維持します。期限超過backupのoverrideはありません。端末交換まで含むRPOを設定し、offsite作成、検証、別保管鍵の訓練を別の証跡として確認します。

- 意味解析を shadow または canonical で運用している場合、判定ルールの識別値が変わるため、保存済みの意味解析はバックログ枠で順次再解析され、一時的に処理待ちが増えます。依頼ループ情報も新しい条件で再作成されるまで旧情報として扱われます。既存の履歴は保持し、手動の全件再解析は不要です。

### 1.0.22で追加された更新時の確認

`plan` の `install_args` は、利用者が直接実行して成功した install.sh の opt-out です。再導入（`route=reinstall`）ではこれを使い、`null` のときだけ確認します（§4）。1.0.21以前の導入には記録がありません。起動役の取り出しは zsh でも動くよう `"${TAG}:scripts/mcs_upgrade.py"` と書きます。抽出の旧世代（`mcs/extract/v2`・`v3`）のファイルが checkout に残っていても import されません。

### 1.0.21で追加された運用診断

更新後の収集実行から、通知待ちの理由、前回の中断検出、日次ローカルバックアップの状態を保存します。`mcs_setup.py doctor` は保存済みの値、`semantic_observe.py` はDBの詳細集計を表示します。古い記録や上限超過は未確認とし、0件と区別します。[残数・速度・停滞の読み方](USER_GUIDE.md#cli--処理の残数停滞の確認)も確認してください。

設定やDB schemaの移行はありません。Hermes gateway、独立host、独立LINE WORKSは、利用している実行形態の既存の更新・再起動手順で反映します。1.0.21では抽出世代を8（ルール世代21）へ更新したため、更新後に抽出workerを再起動すると旧世代の解析結果が通常の処理で順次再抽出され、一時的に解析待ちが増えます。対象範囲・予算・再試行は従来どおりで、手動の強制再解析や保存済み結果の直接書換えは不要です。以前の服薬管理の報告を参考にする設定（`extract.patient_state_context`）は既定で無効のままです。抽出監査の基準統計を使っている場合は、新しい計測定義で基準を取り直して本人の承認を受けてください。医薬品マスターの同梱版は20261013ですが、既存の私有辞書は保持され、運用者による確認・採用・有効化は別操作です。

### 1.0.20で追加された更新時の確認

- 独立実行では、更新後に選択したPythonで `mcs/ops/mcs_setup.py services` を1回実行し、監視と本人だけが読み書きできるサービス定義を反映します。hostは再起動し、処理中のLLM抽出は中断されます。既存の `data/standalone.log` の権限は変わらないため、運用者が `chmod 600` を実行してください。サービス定義を手動配置している場合は対応テンプレートも更新します。
- Hermes gatewayとLINE WORKSの独立アダプターは、新しいコードを読み込むため更新手順に従って再起動します。セットアップが停止確認・通知フラグの公開に失敗した場合は、原因を解消して `services` を再実行します。
- 更新前から保留中の通知、自分の投稿の確認で通知されずに保存された履歴、失敗済みの意味解析は自動で再送・再処理しません。必要な対象だけを既存の人承認手順で扱います。
- モデルを新規取得するときは固定revisionとSHAを照合します。導入済みモデルは今回の更新で再取得・再照合しません。公式医薬品masterの同梱と、私有辞書の承認・有効化は引き続き別操作です。

### 1.0.17以前から1.0.18以降への更新で確認すること

- 1.0.18以降から1.0.20への更新は実行環境の移行がなく、下記の通常の手順で行えます。以下は1.0.17以前から更新する場合の確認事項です。
- 1.0.18はPython 3.14以上が必要です。更新は必ず更新ツール（`scripts/mcs_upgrade.py`、下記の手順）で行います。更新ツールは定期処理とworkerを止めた状態のまま、3.14未満の実行環境（venv）を`<venv>.pre-python314`へ退避して3.14で作り直し、Hermesのcheckoutが過去の固定commitにあれば新しい固定commitへ進めます。更新ツールの実行中に失敗した場合は、その場で旧版のコード・旧環境・checkoutへ戻します。電源断や強制終了でinstall.shの途中に止まった場合は、独立の復旧watchdogが自動では再実行も復元もせず`reinstall_incomplete`として管理者へ通知して止まります。このときは`/opt/homebrew/bin/python3.14 "$REPO/mcs/ops/mcs_update.py" recover`（Python 3.10以上の任意のPython）を実行すると、旧版のコード・旧環境・checkoutへ戻してworkerを再開し、更新ツールでの再更新を通知します。退避した旧環境は削除しません。
- 1.0.17以前の自動更新（`update.mode=auto`）は実行環境を移行できないため、1.0.18の適用を取り消して元の版に戻り、更新ツールの利用を通知します。通知を受けたら下記の手順で更新ツールを実行してください。
- Hermesのcheckoutが過去の固定commit・新しい固定commit以外、ブランチ上、またはローカル変更ありの場合は、`plan`の事前確認で`runtime_migration: hermes_checkout_*`として止まり、何も変更しません。checkoutを固定commitへ揃えてから再実行します。1.0.19以降、Python 3.14対応を宣言している独自のcheckout（変更なし）を使い続ける場合は、`plan`と`apply`に`--keep-hermes-checkout <そのcheckoutの40桁のcommit>`を付けると、checkoutを動かさずに実行環境だけを作り直します（commit不一致・変更あり・3.14非対応は`runtime_migration: keep_hermes_checkout_*`などで止まります）。
- 復旧watchdogのPythonもPython 3.14以上とSQLiteのWAL-reset修正が必要です。既存の選択が`/usr/bin/python3`（3.9系）などの場合、事前確認で`recovery_runtime: runtime Python version unsupported`として止まります。`brew install python@3.14`の後、`mcs_setup.py init --yes --recovery-python /opt/homebrew/bin/python3.14`（実在する絶対パス）で選び直してから更新します。
- Hermesでは抽出worker2本とgateway、独立実行ではhostを更新・再起動します。ヘルス監視は更新が実行する`mcs_setup.py services`で両モードに配置します（macOS: `local.mcs-health`、Linux: `mcs-health.timer`）。
- 必須の設定変更はありません。ルール20へ更新し、旧ルール結果を通常処理で順次再生成します。更新後の最初の定期処理は患者集計の署名の初回更新で一時的に長くなります。DB schemaは9のままです。
- 試行上限で失敗済みの意味解析ジョブは自動で戻らず、既存の人承認による再試行を使います。サービス再設定の失敗は固定の理由と終了コードで記録されるため、詳細は`mcs_setup.py services`を手動で実行して確認します。
- 同梱医薬品masterの辞書は自動では作成・有効化されません。使う場合は[利用ガイド](USER_GUIDE.md#同梱マスターを辞書として使う)の`mcs drug install-bundled`を運用者が実行します。

### 1.0.17への更新で確認すること（過去版の注意）

- 新機能を加えない安定化版です。gatewayまたは使用中の独立host・通知アダプターと抽出worker2本を更新・再起動し、checkout外の復旧本体も既存の手順で同期します。Hermes agent本体の更新は含みません。
- ルール19・LLM抽出7・集約期間確認8・閲覧投影5へ更新し、既存本文を通常枠で順次再処理します。モデル・取得10分・既読化・人承認の条件は維持します。古い修正結果や結果不明の配送を自動で再試行・再送しません。
- Slackでは、新しく取り込む遅延返信を元スレッドへ新規投稿し、チャンネルにも通知します。過去の返信は一斉再送しません。端末の通知・プレビュー設定を確認してください。
- DB schemaは9を維持し、履歴・ログは保持します。復元は対象backupと損失報告へ束縛した別の人承認が必要です。`fact_source=canonical`での旧表示の扱い、手動再試行、旧受付枠の条件は[CHANGELOG](../../CHANGELOG.md)の1.0.17更新注意を確認してください。

### 1.0.16への更新で確認すること（過去版の注意）

- gatewayまたは使用中の独立host・通知アダプターと抽出workerを更新・再起動し、§6の証拠で反映を確認します。新しい配送形式を旧workerが受け取った場合は保留されます。通知先の現在の配送世代をrunnerが再公開してから配送を再開します。成否不明の配送を未送信と推測して再送しません。
- DB schemaは9を維持します。既存カードの表示はレイアウト1のまま、新規カードだけレイアウト2になります。投稿・履歴・過去ログを削除する移行はありません。
- ルール抽出18・LLM抽出6・詳細情報契約2へ更新し、保存済み本文を通常workerで順次再処理します。モデル、最大1600トークン、300秒の呼出上限、取得間隔と人承認・既読化の条件は維持します。再処理中は未完を表示し、途中結果を完成した情報として公開しません。
- 旧版の意味解析の閲覧用投影は現行版4へ更新するまで採用を保留します。解析を有効にしている環境では既存の保守処理で1回25件まで再生成します。薬剤の用量が同じ事実の原文根拠に一致しない場合は確認待ちのままです。元の履歴は保持し、配送済み・送信中・成否不明の通知を自動訂正したことにはなりません。
- 登録薬剤・観測値の巡回取得は `clinical_metadata=true` の明示選択が必要です（既定false）。公式医薬品マスターの同梱は私有辞書の承認・有効化を代替しません。
- `install_sh_changed` または `recovery_tool_changed` の場合は§2の再導入・checkout外の復旧ツール同期を行います。既存のopt-out、復旧Pythonと復元同意を維持します。
- 更新されたHermesの新しい分離起動形式も、既知のコード・所有する配置・moduleが完全一致する場合だけ再起動対象とします。新Hermesで `plugins.isolation=host` を明示する形態はnative SDKを渡すMCS接続に未対応です。既定の `in_process` と、配布時の固定Hermes版での受入を使います。
- 緊急度再確認を有効にする場合は `notify.card_thread=true`、患者別シグナル通知を有効にする場合は対話カードが有効であることを確認します。配送できない組合せは設定検証で拒否します。過去のHTTP 402で再試行上限に達したジョブの手動再投入や、本番の一括再処理は別途対象を確認して実施します。

### 1.0.15への更新で確認すること（過去版の注意）

追加設定は不要です。DB schemaは9を維持し、検索・取得・配送用の索引を加法的に追加します。
台帳の保存データを削除する移行や、抽出モデルの変更はありません。古い版から更新する場合は、その間のschema変更・導入条件も`plan`で確認します。

- Hermesモードはgatewayと抽出ワーカー2本、独立実行は使用中のhost・通知アダプター・抽出ワーカーへ新しいコードを反映します。LINE WORKSの独立アダプターも再起動してください。この版への更新直後は旧更新コードの再起動処理に頼らず、§6で稼働プロセスの反映を確認します。
- `recovery_tool_changed`がtrueなら、復旧watchdogの**本体**も同期します。`mcs setup services`だけではcheckout外の本体を更新しません。§2の表に従い、導入済みの機能とopt-outを維持して`install.sh ... --no-services`を再実行します。復旧機能を使う環境では、この同期を`--no-recovery`で省略しません。既存の安全な`recovery_python`を維持し、別のPythonへ自動で切り替えません。
- 通知文に患者名・投稿内容が含まれます。端末の通知プレビュー設定を確認してください。更新後の稼働確認は、実端末での受信・表示・カード操作の確認とは区別します。

### 2026-10-04追記: 現在の更新入口と回帰資産

[共通CLI](../../mcs/ops/mcs_cli.py)の`mcs update plan`は取得済みタグだけを対象にし、
fetchせずに既存bootstrapへ委譲します。阻害時は非ゼロ終了であり、表示できたことを
適用可能と扱いません。既存の`mcs_upgrade.py`・`mcs_update.py`による手順は残ります。
v1.0.0〜1.0.2は手動更新条件を保持し、standaloneの外部applyは阻害されるためhost経由の
既存手順を使います。apply・rollbackには従来の停止・バックアップ・承認契約が適用され、
schema巻戻しの個別復旧同意を更新承認で代用できません。

[更新経路manifest](../../tests/fixtures/schema_upgrade/update-paths.json)は過去26経路の根拠と、
現行30公開版（v1.0.0〜v1.0.29）・50経路のplan/apply/rollback/bootstrap/reinstall回帰を区別します。
テストは完全合成でGit履歴に依存しませんが、旧target updater全体の26経路再実行、
実Git・host協調停止・SDK導入・実機配備を検証したものではありません。
launcherの選択runtime追随とbackup lifecycleは合成回帰で検証し、
ローカルCLI提供や合成検証を実機受入・配備完了と読み替えません。

> **この文書は AI エージェント（Claude Code / Codex / Devin 等）が読み込み、
> 既存の MCS 導入を過去版（v1.0.0〜）から新しい版へ、データ移行を含めて
> 更新するための実行手順書です。** 新規導入は [SETUP_AGENT.md](SETUP_AGENT.md)、
> 人向けの設定・サービスの説明は [INSTALLATION.md](INSTALLATION.md) を参照します。

## 0. 実行契約（最初に読むこと）

- **更新は配備に当たる。** ユーザーが更新を明示依頼した範囲でだけ実行する。
  文書の閲覧・`plan`だけでは適用の承認にならない。
- 各ステップは【実行】→【検証】の順。検証を飛ばさない。失敗時は【失敗時】を実行し、
  解決しなければ原因（エラートークン）を報告してユーザー判断を待つ。
- **既存状態を保護する。** checkout の未コミット変更を stash / reset / clean / checkout で消さない。
  `data/`・`config.json`・`.env` を手で編集・削除しない。秘密値を表示・入力させない
  （必要なら [SETUP_AGENT.md §0-1](SETUP_AGENT.md#0-1-秘密情報プロトコル) に従う）。
- 更新中は収集・抽出・通知が止まる。適用はタグの照合・取得でネットワークが必要。
  再導入（install.sh）を伴う場合はbrew・pipの導入で数分〜最大1時間かかる。認識済みの旧固定版Hermesは
  更新ツールの再導入経路で移行し、未知・変更中のcheckoutは停止する。開始前にユーザーへ伝える。
- **更新を実行するのは常に移行先版のコード。** 現在の checkout の更新機能は使わない。

## 1. 対象と Python の特定

| # | 確認 | コマンド | 合格条件 |
|---|---|---|---|
| 1 | checkout | 次の順で最初に見つかったものを使う（以下 `$REPO`）: `sed -n 1p ~/.mcs-recovery/repo_path` → `grep -o "/[^']*/mcs/ops/mcs_cli.py" ~/.local/bin/mcs`（末尾の `/mcs/ops/mcs_cli.py` を除く） → `readlink ~/.hermes/plugins/mcs-discord-commands`（末尾の `/hermes_plugin` を除く） → `git -C ~/.mcs rev-parse --show-toplevel` → `git -C ~/.local/share/hermes-mcs rev-parse --show-toplevel`（`install_local.py` の置き場所） | 候補が `git -C "$REPO" rev-parse --show-toplevel` で同じパスを返す。どれも無ければユーザーに確認する |
| 2 | 導入形態 | `python3 -c "import json;print(json.load(open('$HOME/.mcs/config.json')).get('runtime_mode','hermes'))"` | `hermes` か `standalone` |
| 3 | Python | hermes: `PY=~/.hermes/hermes-agent/venv/bin/python` / standalone: `PY=~/.mcs/venv/bin/python3`。`$PY -V` | 3.10 以上（更新ツールの入口。1.0.18 への更新で 3.14 未満の venv は 3.14 へ移行される） |

素の `python3`（macOS の 3.9 系）で更新処理を実行しない。

## 2. 起動役の取得と計画（書込みなし）

【実行】移行先タグ（ユーザー指定。無ければ最新リリース。以下 `$TAG`、例 `v1.0.15`）から
起動役を取り出し、計画を出す。`fetch` はタグを取り込むだけで checkout を変えない。

```bash
git -C "$REPO" fetch --tags origin
WORK=$(mktemp -d)        # 本人だけが読める作業dir。共有の /tmp 直下に置かない
git -C "$REPO" show "${TAG}:scripts/mcs_upgrade.py" > "$WORK/mcs_upgrade.py"   # ${TAG}: zshでは "$TAG:s…" が修飾子になる
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch plan --to "$TAG"
```

【検証】JSON が出る。主な項目:

| 項目 | 意味 |
|---|---|
| `route` | `apply`（通常適用）/ `reinstall`（install.sh 再実行を伴う適用）/ `blocked`（§3で解消）/ `noop`（更新済み） |
| `current` / `target` | 現在のタグ・SHA・DB schema / 移行先 |
| `blockers` | 解消が必要なエラートークン |
| `reinstall` | `install_sh_changed`（install.sh 変更）・`standalone_requirements_changed`（独立モード SDK 固定版の変更） |
| `schema_bump` | DB の版変更。あれば事前バックアップからの復元が巻戻しに含まれる |
| `restarts` | 更新後に必要な再起動（`gateway`・`lineworks_adapter`） |
| `recovery_tool_changed` | 復旧 watchdog の変更。`reinstall` 経路なら自動で反映（`--no-recovery` を付けた場合を除く）。`apply` 経路では適用後に、承認を得て `./install.sh <opt-out> --no-services` を再実行する |
| `install_args` | 前回成功した install.sh の opt-out（`~/.mcs/config.json`。未記録時は旧 `data/install_options.json` を参照）。`null` は記録なし、読取不能、形式不正、または新旧の記録が不一致 |
| `notes` | 現在版より新しく移行先までの各版の「更新時の注意」（CHANGELOG） |

1.0.13以降、macOS 標準 `/usr/bin/python3` の SQLite は WAL-reset 影響版のため、
`update apply`・`services` は復旧 watchdog の実行 Python が安全と確認できるまで停止する
（`plan` は merge 前に `recovery_runtime:` を blockers に出し、手順を `notes` に示す）。
オーナー承認のうえ、checkout 外にある既存の安全な独立 Python の絶対パスを `recovery_python` として保存する。
1.0.13 以降が入っていれば `mcs setup init --yes --recovery-python <path>` と `mcs setup services` で反映する。
1.0.12 以前から更新する場合は init にこのオプションが無いため、`~/.mcs/config.json` に
`"recovery_python": "<path>"` を手で追記してから `plan` を再実行する。所有済みで停止中の watchdog は
merge 後の `services` が置き換える。`reinstall` 経路では `--install-arg=--no-recovery` を付けて apply する
（install.sh は不一致の watchdog を置き換えずに停止するため）。自動で別 Python へは切り替えない。

`${TAG}:scripts/mcs_upgrade.py` が無い（v1.0.10 以前を移行先にした）場合は、その版へはこの手順で更新できない。

### 2-A. 公開前の固定候補をこのMacへ適用する場合

運用者が「本番で動作を確認してから公開する」と承認した場合は、`--candidate-sha` で未公開のローカル候補を指定できます。タグ・GitHub Releaseは作成しません。候補の40桁SHAと、レビュー時点の本番HEADの40桁SHAを固定し、変更をコミットしてから使います。branch名・省略SHA・tag object・tree・blobは指定できません。候補は本番checkoutのローカルGit objectとして存在し、本番HEADの子孫である必要があります。

```bash
CANDIDATE_SHA='<レビュー済み候補の40桁SHA>'
BASE_SHA='<レビュー時点の本番HEADの40桁SHA>'
WORK=$(mktemp -d)
git -C "$REPO" show "${CANDIDATE_SHA}:scripts/mcs_upgrade.py" > "$WORK/mcs_upgrade.py"
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" plan \
  --candidate-sha "$CANDIDATE_SHA" --base-sha "$BASE_SHA"
```

`plan`の`target.kind`は`local_candidate`、`target.tag`は`null`です。`blockers`が空になるまで§3の手順で対処します。候補に`install.sh`等の変更があれば、公開版と同じく`route=reinstall`になります。前回の導入内容とopt-outは§4で確認し、そのまま引き継いでください。復旧watchdogを導入済みなら省略しません。

明示承認後、`route=apply`なら次を実行します。`route=reinstall`では同じコマンドに`--reinstall`と、§4で確認した各`--install-arg=...`を追加します。理由には秘密値・患者情報を含めず、200字以内で記載してください。

```bash
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" apply \
  --candidate-sha "$CANDIDATE_SHA" --base-sha "$BASE_SHA" \
  --confirm-human --reason 'レビュー済み候補を本番で確認するため'
```

候補の取得・照合では通信しません。再導入を伴う場合の依存取得などは、通常の再導入と同じです。`--to`・`--tag`・`--sha`・`--command-id`との併用、自動更新・チャット承認からの候補指定には対応しません。`--confirm-human`はこのローカル適用への承認であり、DB復元の人承認を代行しません。

候補でも、保護path・設定・schema・更新前HEAD・fast-forwardの確認、バックアップ、update/run lock、更新記録、worker停止、新コードによるサービス同期・再起動・適用後確認は同じ更新処理を通ります。未公開候補と本人確認・理由を更新記録へ保存し、失敗・中断・rollbackは§7の既存手順で扱います。保留中の復元承認や送信結果不明は解除しません。

本番checkoutのmainを先に通常の`git merge`で進めると、その時点で稼働コードが置き換わります。開発worktreeでmainを祖先に含む候補を作り、更新処理がworkerを停止してからmainを進めてください。適用後は§6の稼働確認を行い、実際の動作を確認してから公開可否を判断します。

本番適用後はしばらく運用して確認し、見つかった不具合を修正してからタグ付け・GitHub Release・pushへ進みます。直後の正常な収集や合成テストだけで、運用中の確認が済んだとは扱いません。

後で公開するタグは、検証済みの候補そのもの、または修正を含む子孫コミットを指す必要があります。適用済みのコミットはsquash・rebase・amendで書き換えないでください。祖先を含まない版へは`not_fast_forward`で更新が止まります。別系統へ切り替える場合は直接resetせず、§7の巻戻し条件と承認を確認してから、既存の`rollback`で更新前のコミットへ戻します。候補の運用を続けるだけなら巻戻しは不要です。

## 3. 阻害要因の解消

`blockers` ごとに対処し、§2 の `plan` を再実行して `blockers` が空になるまで繰り返す。

| トークン | 対処 |
|---|---|
| `tree_dirty` | `git -C "$REPO" status --short` を示し、**【ユーザー確認】**。他の作業者の変更の可能性がある。勝手に退避・破棄しない |
| `config: …` / `new_config: …` | 現在 / 移行先の設定検証エラー。キーと理由を示し、承認後に `"$PY" "$REPO/mcs/ops/mcs_setup.py" init --set KEY=JSON` で直す（秘密値は扱わない） |
| `update_in_progress_or_interrupted` | 前回の更新が途中。`"$PY" "$REPO/mcs/ops/mcs_update.py" status` を確認し、§7の移行先版の更新ツールで復旧してから再計画 |
| `runtime_migration_pending`（復旧レポート） | Pythonや接続用ライブラリの移行が途中。定期の復旧ツールは環境・更新記録を保持して停止する。通常の復旧を繰り返さず、§7の移行先版の更新ツールを確認する |
| `restore_consent_pending` | DB 復元の人承認待ち。[バックアップ・復元ガイド §8](BACKUP.md#consent) の損失報告・バックアップhashに束縛された復元承認手順をユーザーが行う |
| `hermes_not_resolvable` / `standalone_interpreter_unavailable` | 実行基盤が無い。`./install.sh --preflight` の `対処:` を確認しユーザーと対処 |
| `insufficient_disk` | DBの2倍に、64MiBと設定された空き容量下限の大きい方を加えた空きが必要。下限の既定値は512MiB。原本と必要なバックアップを保護して容量を確保 |
| `not_fast_forward: …` | checkout が移行先の祖先でない（ローカルコミットや新しすぎる版）。**【ユーザー確認】** |
| `untracked_collision: …` | 未追跡ファイルが移行先の追跡ファイルと衝突。内容を示し **【ユーザー確認】** |
| `schema_downgrade:…` | 移行先が古い。対象タグを見直す |
| `schema_bump_auto_blocked` | `update.mode=auto` では schema 変更を適用しない。ユーザーと方針を決める（`notify` へ変更等） |
| `standalone_external_apply` | 独立モードは host 経由で更新する（[STANDALONE.md](STANDALONE.md#更新復旧)）。`reinstall` がある場合は同書の再導入手順 |
| `legacy_source_manual` | v1.0.0〜1.0.2 からの更新。§8 の手動経路へ |
| `reinstall_incomplete` | 前回の再導入が install.sh の途中で中断した。`status` を示し **【ユーザー確認】** の上で `cd "$REPO" && ./install.sh <前回と同じ opt-out（plan の install_args）> --no-services` → `"$PY" mcs/ops/mcs_setup.py services` → `"$PY" mcs/ops/mcs_update.py reinstall-done`（HEAD が再導入した版のときだけ完了を記録し、残りの後処理を復旧処理で完了）→ `check`。復旧処理は install.sh を無人で再実行しない |
| `reinstall_requires_upgrade_tool` | 1.0.29 以前の `mcs/ops/mcs_update.py` を直接実行して再導入を指定したため、更新の排他制御を引き継げず、再導入を始める前に止めて元の版への復元を試みた。`status` と復旧の結果を確認し、復元できていれば §7 の移行先版の更新ツール（`scripts/mcs_upgrade.py apply --reinstall`）で再実行する |
| `install_options_unverified` | 承認後の変更か、古い更新記録に照合の根拠がないため、再導入を開始していない。記録を削除せず、現在の状態と導入内容を担当者に確認してもらう |
| `recovery_incomplete`（復旧レポート） | 抽出処理の起動確認が未完了。中断記録を保持するため、起動失敗の原因を修復して `recover` を再実行する。同じ復旧の再試行では起動済み処理を停止し直さない |
| `update_state_corrupt` | `data/update_state.json` が読めない。内容を変えずに報告し、ユーザー判断を待つ |
| `tag_not_fetched` | `git fetch --tags origin` が失敗している。ネットワーク・タグ名を確認 |
| `validate_config_failed` / `preflight_failed: …` | 現在 / 移行先コードで設定検証が実行できない。出力を報告 |
| `standalone_runtime_missing_in_target` | 移行先が独立モード非対応。対象タグを見直す |
| `plan_failed: …` | 計画中の検査が失敗した。内容を報告し再試行の可否をユーザーと決める |
| その他（`protected_path`・`unsafe_path`・`ignored_path_tracked` 等） | 移行先タグの異常。適用せず報告 |

## 4. ユーザー確認

次をまとめて提示し、適用の承認を得る（既に同じ内容で承認済みなら聞き直さない）。

- 現在版→移行先、`route`、停止の見込み時間
- `notes` の要約（版ごと。必要な追加操作を明示）
- `reinstall` の場合: install.sh の再実行と、使う opt-out。`plan` の `install_args` が配列なら
  その opt-out を使い、確認は不要（空配列は opt-out なし）。`null` のときは記録の欠落・不正・
  不一致を区別できないため、自動更新と再導入を停止し、担当者に確認を依頼する。
  旧記録と中央設定の両方に省略項目がないと確認できた場合は、ユーザーに
  `--no-llm`（自前 LLM サーバ）・`--no-brew`・`--no-plugin`・`--no-recovery` のどれを使っていたか確認する
  （`--mode` は不要。設定から自動判定）。`~/.mcs-recovery/` があるなら復旧機能は導入済みなので、
  `install_args` や回答に `--no-recovery` があっても除き、`notes` が求める場合だけ付ける
  （付けると復旧ツールが古いまま残る）。
  新しい選択は利用者が install.sh を直接実行して成功したときに記録する。standalone では
  明示指定の有無にかかわらず `--no-plugin` を記録する。通常の更新では、
  検証済みの旧記録があり中央設定に記録がなければ、run.lock を取得後、mergeの前にその値を中央設定へ写す。
  既存の更新条件を満たす場合、最新版であっても移行が必要なら `plan` は `settings_migration: required` と表示し、確認後の更新で写す。
  認証情報と保存データは現在の保存先に保持し、DBの形式変更は既存のバックアップ・移行手順で扱う。
  Python 移行や修復のために足した opt-out は記録しない。
  旧記録はそのまま保持され、有効な旧記録と異なる opt-out を保存する直接導入は停止する。
  更新が中断・失敗した場合も、同じ値を写した設定は残る場合がある。
  旧watchdogが `interrupted_pre_merge` と報告しても、設定へのコピーが行われなかった証拠にはならない。
  保存後の同期に失敗した場合は結果を確定できないため、旧記録と設定を保持して担当者に確認を依頼する。
  設定を写す前に `config.json` と同じフォルダーへ `.install-options-migration.pending` を排他的に作成して同期し、保存と照合が終わった場合に取り除く。
  この記録が残っている場合や設定・`data` のフォルダーの安全性を確認できない場合、新版の導入・更新・復旧は停止する。
  設定と記録は削除・編集しない。移行先の検証済みタグを `$TAG` に設定し、
  そのタグの更新ツールで状態を確認する。表示に設定値や秘密値は含まれない。
  ```bash
  WORK="$(mktemp -d)"
  git -C "$REPO" show "refs/tags/$TAG:scripts/mcs_upgrade.py" > "$WORK/mcs_upgrade.py" &&
    "$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch reconcile-settings --to "$TAG"
  ```
  状態を確認できた場合、担当者が復旧を判断し、上の最後のコマンドに
  `--confirm-human --reason '確認理由'` を加えて実行できる。理由には患者情報や秘密値を含めない。
  設定へのコピー後は同じ内容を同期し直し、確認記録を残してから保留を解除する。
  旧形式の保留記録、旧記録や保存先の不一致、release更新の保留などは自動解除できない。
  その場合や保存結果が確認できない場合は、状態を保持し担当者に確認を依頼する。
  設定・`data` のフォルダーは本人の所有で、グループと他のユーザーから書き込めない必要がある。ACL はこの確認の対象ではない。
  旧watchdogや旧updaterはこの記録を理解しないため、旧版の起動による操作を防げない。
  release-owned 経路は承認時の設定を変えないため、この記録の移行が必要または確認不能な場合は停止する。
  owner の計画時と適用開始前にも確認する。開始前に検出した場合は適用開始記録を作らない。
  中央の grant が発行された後に検出した場合は、既存の承認・記録を保持して担当者に確認を依頼する。
  開始後の保留は通常の更新・復旧で解除できない。状態ファイルを削除しない。
  対話で確認した導入設定は適用前と再導入前に照合し、変更された場合は停止する。
  手動で指定した再導入引数は記録値と別に扱う。古い更新記録に照合の根拠がない場合、
  自動再導入を停止して中断した更新の復旧手順を案内する。
  旧版は中央の記録を参照しないため、旧版へ戻すときは導入内容と旧記録を確認する。
  旧記録がなく `null` の場合、対話式の自動更新は再導入を開始しない。
  更新前から開いていた旧版の手動設定は共通ロックを使わないため、移行後に中央設定を上書きする場合がある。
  旧記録は保持され、中央の項目が失われた場合は次の更新で照合して再度写す。
- `schema_bump` の有無、`restarts`

## 5. 適用

【実行】

```bash
# route=apply
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch apply --to "$TAG"
# route=reinstall（install_args の各要素、または確認した opt-out を --install-arg=… で付ける）
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch apply --to "$TAG" --reinstall
```

上の再導入例は opt-out なしの場合。§4 で確定した opt-out があるときだけ、各要素を
`--install-arg=…` として追加する。`--no-llm` は自前 LLM サーバー・モデルの導入を省略する指定で、
既存の LLM 処理を停止する設定ではない。

`--no-fetch` は起動役の事前取得を省くだけで、適用自体はタグの照合のためネットワークを使う。
適用は移行先版の `mcs_update.apply` が行う: 更新ロック取得 → 検査 → `data/backups/preupdate-*.db` 取得 →
実行ロック → 常駐処理の停止 → `git merge --ff-only` → 新コードで（再導入 →）サービス再同期 → 再起動 →
DB を開いて移行（加法 migration）→ 事後検証。失敗時は自動で元の SHA へ戻し、schema 変更があれば DB を復元する
（復元前の損失報告に人承認が必要な場合は保留になる）。移行記録にあるHermesのコードとvenvは退避から戻せるが、brewなど再導入のその他の副作用は巻き戻らない。

【検証】終了コード 0。0 以外は §7。

## 6. 後処理と検証

1. `restarts` に `gateway` があり Hermes モードなら、apply が再起動を依頼済み。`hermes gateway status` で稼働を確認する。
2. `lineworks_adapter` があれば、[LINEWORKS.md](LINEWORKS.md) の手順で独立アダプターを再起動し `python -m lineworks_adapter check`。1.0.15では自動更新・巻戻し・復旧後の再起動を追加しましたが、この版への初回更新後も明示的な再起動と反映確認を行います。
3. `notes` にある追加操作（承認済みのもの）を実行する。`apply` 経路で `recovery_tool_changed` が true なら §2 の表のとおり install.sh を再実行する。
4. 使用中のgatewayまたは独立host・通知アダプターと抽出ワーカー2本について、更新後に起動したプロセスであることと稼働状態を確認します。古いworkerが新しい通知形式を保留している場合、保存ファイルを削除して再送させず、更新したworkerで通常の配送を再開します。
   Hermes の Slack では、カードを送るのは plugin settings の `slack_profile` の profile だけです。複数 profile の gateway で他の profile の接続が見送られた場合は、イベントログ `hermes.plugin.mcs_slack` に `worker_disabled`（`reason=profile_mismatch`）が出ます。これは正常な動作です。`channel_not_found` だけを根拠に再送しません。成否不明（`unknown`）や再送上限（`resend_exhausted`）の配送を、保存データの書換えや強制再送で解除しません。
5. 検証:

以下は公開タグの確認例です。§2-Aの未公開候補では、`git -C "$REPO" rev-parse HEAD` と `status` の `current_sha`・`state.applied[-1].sha` が候補SHAに一致し、`state.applied[-1].target_kind=local_candidate`、`state.applying=null` であることを確認します。
再planでは `--to "$TAG"` の代わりに `--candidate-sha "$CANDIDATE_SHA" --base-sha <適用後HEADの40桁SHA>` を指定します。更新前の `BASE_SHA` を再利用すると `base_sha_mismatch` で止まるため、その結果だけで更新失敗と判断せず、適用後のHEADで再計画してください。

```bash
"$PY" "$REPO/mcs/ops/mcs_update.py" status     # applied の最後が $TAG、applying が null
"$PY" "$REPO/mcs/ops/mcs_setup.py" check       # exit 0
"$PY" "$WORK/mcs_upgrade.py" --repo "$REPO" --no-fetch plan --to "$TAG"   # route=noop
rm -r "$WORK"
```

## 7. 失敗時・巻戻し

Pythonや接続用ライブラリの移行中に止まった場合は、**移行先版の**`mcs/ops/mcs_update.py recover`で退避した環境を戻します。通常の`mcs recover`や定期の復旧監視は、この移行を進めず停止します。
管理担当者は、記録された安全な独立Python（`recovery_python`）と更新記録の移行先SHAを確認してください。MCSのHEADがそのSHAと一致する場合は、そのPythonで`"$REPO/mcs/ops/mcs_update.py" recover`を実行します。HEADが異なる場合や対象コードがない場合は、旧版の更新ツールを代わりに起動せず、記録を保持して管理担当者が移行先コードによる復旧方法を確認します。壊れたvenvのPythonや自動選択した別Pythonは使いません。
Hermesのコードが更新開始時・移行先のどちらとも違う場合、ブランチ上や変更中の場合、移行記録が空・不正な場合も、環境や退避を動かす前に停止します。既存の`--keep-hermes-checkout`を明示した環境は、記録したcommitから変わっておらず変更もない場合に限り、ブランチ上でも保持できます。変更を破棄したり記録を削除したりして回避しないでください。
1.0.24以前の更新ツールが作った古い移行記録が残っている場合も、移行先の版を記録から確認できず停止することがあります。管理担当者が元の更新対象と退避を確認してください。

- apply が非0: `status` の `attempts[$TAG].detail` を報告する。自動巻戻し済みなら checkout は元の SHA。
  `rollback failed — escalate` の場合は追加操作をせず報告する（復旧 watchdog と人の判断に委ねる）。
  schema 変更を伴う更新で旧版の checkout へ戻した後なら、DB 復元が完了するまで抽出処理と更新 marker を止めたままにする（通知は復旧レポートで行う）。
  以後の `recover` も、復元の保留markerや未完了記録が残る間は処理を強制再開しない。独立した復旧監視のレポートに `writers_stopped` がない場合、保留だけでwriterの停止を確認済みとは扱わず、実際のプロセス状態を確認する。
  使えるバックアップと復元承認がそろい、復元後の照合も完了した場合に通常の再開に戻る。
  `restore_reconcile_schema_blocked` は、復元したDBを読込済みの新版writerで開くと再移行してしまうため、照合と再開を停止した状態です。
  DBはバックアップのschemaを保ち、復元済みmarkerと`restore_incomplete`を残します。追加の承認では解除されません。
  独立した復旧監視の`restore_reconcile_pending`も、DBのコピー後に通知照合が未完了であることを示します。更新journalが空・不在でも、更新・復旧処理の復元済みmarkerが残っていれば保留を維持します。
  旧schemaで通知を自動再照合できないため、管理担当者が適合する版・復旧Python・通知の照合方法を確認するまで、markerやjournalを削除せず収集・通知を強制再開しないでください。
  独立した復旧ツールは、復元DBと実際に再開するcleanなcheckoutのschemaが一致し、更新記録・HEAD・正規のschema定義を確認できる場合に限り、既存の `mcs recover` でwriterを再開します。
  配送の保留markerは残し、再開したrunnerが既存の通知記録照合を完了するまで通知を許可しません。版が不明・不一致、記録や定義を読めない場合、および復元の承認待ちは保留を維持します。適合するcheckoutの確認後に同じ `mcs recover` で再確認し、marker削除や追加承認で迂回しないでください。
- 適用後に問題が見つかり戻す場合（ユーザー承認後）: `"$PY" "$REPO/mcs/ops/mcs_update.py" rollback`。
  schema 変更があった更新は DB 復元を伴い、損失報告への人承認を求められることがある。
- `rollback_blocked_layout_floor` は保存済みカード形式を戻り先が扱えないことを示します。自動巻戻し・中断復旧にも同じ下限を適用し、旧コードへの切替前に拒否します。1.0.23から旧版へ戻すには、必要な損失確認と人承認を経て更新前backupを復元してください。復元holdは維持し、通常収集や通知を手動で強制再開しません。
- install.sh が失敗した場合は出力末尾の `要対処` / `対処:` を示し、ユーザーと対処してから §2 からやり直す。

## 8. v1.0.0〜1.0.2 からの手動経路

この3版には更新機能・サービス同期が無く、自動の巻戻しが成立しない。各手順をユーザーに確認しながら行う。

1. 停止: `launchctl list | grep -E 'ai\.mcs|local\.mcs'` で MCS のジョブを示し、承認後に
   `launchctl bootout gui/$(id -u)/<label>` で止める。`pgrep -fl 'run_check|extract_llm|semantic_drain'` が空になるのを確認。
   Hermes cron の MCS ジョブも `hermes cron list` で確認し一時停止する。
2. バックアップ: `sqlite3 ~/.mcs/data/ledger.db ".backup '$HOME/.mcs/data/backups/preupgrade-manual-$(date +%Y%m%d-%H%M%S).db'"`、
   取得したファイルで `PRAGMA integrity_check` が `ok`。現在の SHA を記録（`git rev-parse HEAD`）。
3. 更新: `git -C "$REPO" merge --ff-only "$TAG"`。
4. 再導入: `cd "$REPO" && ./install.sh`（§4 で確認した opt-out を付ける）。install.sh がサービスを配置する。
5. 検証: `"$PY" mcs/ops/mcs_setup.py check` が exit 0、§6 の 1〜3。
6. 巻戻し（必要時・承認後）: `git -C "$REPO" reset --keep <記録したSHA>`、DB を手順2のバックアップへ戻し、
   手順1で止めたジョブを再読込する。

## 9. 完了報告

```
MCS 更新: <現在版> → <移行先>（route=<…>）
- 適用: 成功 / 失敗（理由トークン）/ 巻戻し済み
- DB: schema <前>→<後>、事前バックアップ <パス>
- 再起動: gateway <済/不要>、LINE WORKS <済/不要/未実施>
- 検証: status <…>、check <exit>、再plan <noop>
- 各版の注意で実施した操作 / 未実施の操作と理由
```

## 更新・復旧の日本語表示

更新ランチャーと更新管理コマンドのヘルプ、対処案内、更新の影響説明は日本語です。
`mcs update --help` で操作を確認できます。コマンド名・引数・終了コードと
計画/状態のJSONは自動処理との互換性のため維持しています。JSON内の状態コードは翻訳しません。

復旧ツールの使い方だけを確認するときは、
`python3 ~/.mcs-recovery/mcs_recover.py --help` を使います。
`--help` / `-h` は復旧を実行しません。不明な引数もエラーで終了し、復旧を開始しません。
`--status` は引き続き機械処理用JSONを返します。引数なしの実行は復旧処理なので、
ヘルプの代わりに実行しないでください。配備済みの復旧コピーは通常の更新・同期後に反映されます。
OSや外部コマンドの技術的な詳細には英語が含まれることがあります。
