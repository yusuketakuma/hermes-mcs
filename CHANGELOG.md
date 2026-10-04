# Changelog

利用者向けの変更履歴です。各版は公開当時の仕様を記載しています。
更新手順・既定値・取得範囲は、導入する版の記録を確認してください。
記載ルールと自動生成の手順は [RELEASE_NOTES.md](docs/development/RELEASE_NOTES.md) を参照してください。

## [Unreleased]

## [1.0.13] — 2026-10-05

**安定稼働版：バックアップ・復元、導入と更新、取得の信頼性を強化**

暗号化バックアップと同意付き復元、統一の導入・更新・診断コマンド、取得失敗理由の保存、緊急度判定の修正に加え、スタンプ再取得と通知表示を統合します。新機能は既定offで、macOSでは復旧用Pythonの選択が必要になる場合があります。

> 更新前の確認：次回の更新（mcs_update の適用後に自動実行される services）、復旧（mcs_recover）、`mcs_setup.py services` の実行、または install.sh の再実行で雛形が再描画され、内容が変わった常駐ジョブ（llama-server・抽出worker 2本・cmd/int 取込・独立実行）はそれぞれ1回再起動されて Umask 077 が適用されます。再起動で処理中のLLM抽出が中断され得るため、抽出が空いている時間帯の更新を推奨します。それまで稼働中のジョブは従来の権限のままです。既に作成済みのログ（例: extract_drain_2.log）の権限は変わらないため、必要に応じて所有者のみ（chmod 600）に変更してください。独立実行の定期ジョブ（mcs_setup.py の _cron_plist と mcs_standalone/service.py が生成する plist、data/cron.log・data/standalone.log）にはまだ Umask を設定しておらず、別項目で対応します。

### 新機能

- **バックアップ鍵のエスクローと新端末への安全な復元**
  明示した人のエスクロー操作で専用Keychainへ鍵を作成し、既存鍵・既存記録の置換を拒否します。復元は新しい私有ディレクトリだけに行い、DB配置前に人の同意待ちマーカーを保存します。オフサイト・検証・訓練・復元の結果をローカルに永続記録し、秘密値を表示しないstatusで確認できます。

- **鍵や書込みの前にバックアップと復元の準備状況を確認**
  明示した私有ポリシー、静的スナップショット、媒体、作業領域、信頼済み記録、新規復元先を読取り専用で確認できます。容量・保持上限・RPO・未確認事項と人が決める鍵保管、receipt保管、訓練の条件を示し、患者名・本文・秘密値・パス識別子は出力しません。

- **新端末の復元に専用の同意経路を追加**
  暗号化バックアップから新規配置したDBを、対象を固定した人の理由・receiptを別段階で検証して収集用に採用できます。更新・rollbackの承認は代用できず、採用後も通知と配送照合は全件保留のままです。

- **明示した静的snapshotと複数時刻でoffsiteを予約**
  opt-in backup設定は従来の日次snapshot_dirに加え、ownerが明示した静的snapshotファイルを選べます。scheduleは同じminuteと1〜24個の重複しないhourを指定でき、Hermesとstandaloneで共通parserを利用します。新しい既定時刻・保存先や自動有効化は追加しません。

- **C1候補の分割送付形式と不完全な集合の検査を追加**
  明示的なmcs-ext-export/2形式で、本文と対応投稿を分離せずに容量内へ分割し、欠落・混在・改ざんのある集合を完全な受領と誤認しない参照検査を追加しました。既存の/1送付・認可・journalは変更しません。

- **新契約の予約と手渡し受領を既存の安全経路へ接続**
  export/2のローカル参照送付でも、送付直前の人承認・失効・鮮度・許可範囲を検査し、予約後の結果不明を自動再送しません。本文を含む新版は型別件数が一致する独立受領票で照合し、旧契約のhash・ID・認可既定は維持します。

- **新契約のsnapshot手渡しと変更なしの確認を追加**
  明示したexport/2で公開snapshotを読取り、本文と投稿を対にした選別・分割・私有outboxへの手渡しができます。引数なしhandoffは設定済みext_exportの4項目を使い、dry-runは送付・出力ファイルなしで件数と容量を確認します。

- **C1候補データの独立したローカル契約検査を追加**
  本文と患者別の取得状態を扱うC1候補データについて、明示された許可範囲、本文の容量・整合性・投稿との対応、不明な取得状態を純粋関数で検査できるようにしました。既存の集約出力や手渡し送付の動作・hash・認可は変更しません。

- **ローカル参照受信側に集合と投稿版のライフサイクルを追加**
  合成データの受領ACKと完全集合・投稿のcurrentを区別し、分割不足や取得範囲不明を対応完了と誤認しない参照受信側を追加しました。認可IDが変わっても同じsourceの投稿同一性を保ち、本文変更・削除・本文消失を世代別に追跡します。

- **C1候補レコードの読み取り専用組立と対保持選択を追加**
  公開snapshotの同じ読み取りtransactionから、既存aggregateのmeta・coverage・message・signalに本文候補と患者別取得範囲を組み立てる単体APIを追加しました。空本文を含む完全取得本文を保持し、選択時にも本文とmessageを分離しません。

- **手渡し送付に撤回指示書・合成受信側コマンド・C0固定fixtureを追加**
  明示の/2手渡しで送付を撤回すると、outboxへ自由文を含まない撤回指示書を作成し、受信側の削除receiptを取り込むまでdelete_heldを維持します。世代単位の撤回はjournalから全envelopeへ展開します。合成の参照受信側をreceiveコマンドで実行でき、受理・拒否・削除のreceipt bundleを作り、受領件数と完全集合の判定を分けて表示します。本文のsender_kindは既定でunknownのままで、明示した場合だけ自局ID・設定した自組織・職種から分類し、職種だけで自局とは判定しません。

- **依頼と返信のcanonical追従を合成評価で確認**
  架空ケースから旧・新canonicalの実際の投影を比較し、依頼項目の欠落、重複候補、誤った完了、依頼と返信の混同を件数・分母・混同行列で確認できるようになりました。

- **canonicalの依頼項目をローカル候補で保持**
  宛先・依頼者・期限原文・条件・依頼種別をvalidatorとnormalizerで保持し、明示した候補投影で利用できます。根拠のない項目だけを不明に落とし、依頼自体は失いません。現在の投稿・threadに束縛したstaging候補を重複なく保存できます。

- **依頼追従の人手レビュー待ち合成集合を追加**
  完全に創作した複数投稿会話220件をheld-out test集合として準備しました。依頼・質問・自己予定・返信・受領・誤完了・条件・依存・時点・宛先不明・依頼者不明の11領域を各20件持ち、原文・合成提案・空の人手ラベルを分けて確認できます。

- **薬剤の表記正規化と根拠付き検査候補を追加**
  薬剤の原名・用量を保ったまま全半角・かな・剤形・規格の表記を注釈します。検査候補は項目・値・単位・明示測定日・根拠・確認状態を区別し、値や単位が引用と一致しない結果は既存の保存結果も含めて未確認表示へ分離します。

- **本人宛メンションとしおりの横断読取りを追加**
  明示的に呼び出した場合だけ横断一覧を取得し、完全取得・空・不明・失敗・古い観測を区別してローカルで参照できます。通常の本文収集、未読の取得範囲、通知、依頼完了、抽出結果は変更しません。

- **薬剤の表層名を保った成分候補注釈**
  承認済みの私有辞書と完全一致する薬剤に、出所と辞書版を伴う未確認の成分候補を付けます。別名を同一の服薬行へ統合せず、複数候補・総称・不明を分けて表示します。

- **認証付き暗号化バックアップと隔離復元訓練を追加**
  明示した保存方針と鍵で、検証済みSQLiteバックアップを暗号化し、改ざんや別世代への差し替えを拒否して隔離先へ復元検証できます。保存先・鍵管理の承認がない場合は実行せず、実機オフサイト保全の完了は主張しません。

- **集計exportの手渡しとreceipt照合を手動CLIで操作**
  既存の集計exportにdeliver・handoff・reconcile・withdraw・healthのローカル手動コマンドを追加しました。handoffは受領ackを自作せずheldを表示し、単一JSON・NDJSON・ディレクトリのreceiptをreconcileで照合できます。healthは件数・状態・固定理由だけを有界に読取り、本文・actor・秘密値を表示しません。

- **職種間の構造的な返信時間を明示選択で確認**
  親投稿と最初に観測されたスレッド返信の投稿時刻差を、職種群の組合せ別に集計できます。役割・時刻・取得状況の不明を分離し、オーナーの小集団方針が未指定の場合は職種セルを全て非表示にします。投稿時刻はケア実施時刻ではなく、返信がないことから未対応を断定しません。

- **DB整合性を件数だけで監査**
  明示した静的SQLite台帳を読取り専用で監査し、孤児添付・投稿に結び付かない派生情報・患者範囲の不一致・重複キー・既存外部キー違反を安定した理由コードと件数で確認できます。本文・氏名・患者や投稿のID・ファイル内容は出力しません。旧DBで検査に必要な項目がない場合や処理上限に達した場合は、正常ではなく未確認として返します。

- **導入・更新・初期設定・診断を共通コマンドに集約**
  mcs install / update / setup / doctorで既存の導入・更新・設定処理を利用できます。doctorは既定で通信・秘密取得・DBアクセスを行わず、未検査を正常と区別した共有用結果を返します。

- **本文を残さない編集・削除の観測履歴**
  保存済みの完全本文から編集・削除を観測したとき、前後のhashと観測時刻だけを記録します。過去の本文を別表へ保持せず、未取得本文や同一内容の再取得は編集とみなしません。

- **新しい読取データをsnapshotから明示閲覧**
  project_metadataとcross_listsを既存mcs_viewの読取り専用入口へ接続します。ケアチーム・構造化値・group一覧と、横断メンション・しおりを、取得とは独立した明示選択で閲覧できます。

- **公式医薬品マスターのオフライン変換を追加**
  厚生労働省の現行医薬品マスターを選択した方針に合わせ、明示したローカルZIP/CSVとSHA-256 pinから私有辞書候補を作るconverterを追加しました。一般名コードと標準処方記載が揃う場合だけ一般名処方identityを使い、それ以外は製品identityとし、成分同一性は推定しません。

- **自分の投稿で医師の「見ました」未観測人数を表示**
  ローカル閲覧の投稿証拠に、ケアチームの医師のうち「見ました」が観測されていない人数を追加しました。名簿と押下者一覧の両方が完全かつ最新の場合だけ人数を出し、どちらかが古い・失敗・未取得なら不明と理由を表示します。未観測は未読や未対応を意味しません。

- **ケアチームと構造化情報の読取り専用取得を追加**
  明示実行でケアチーム・薬歴・観測項目と値・group相談の最小メタデータを保存し、空・不明・取得失敗・古い観測を区別してローカル閲覧できます。相談はgroup単位で保持し、根拠のない患者への関連付けを行いません。

- **再照合の完全巡回を記録**
  既存の再照合で全ページの取得と返信保存を確認した場合だけ、巡回完了回数と観測時刻をjobへ残します。途中取得・返信未完了・エラーを完全巡回と数えません。

- **復旧用Pythonを明示して独立配置を維持**
  初回導入の --recovery-python と mcs setup init --recovery-python で、既存の独立したPythonの絶対パスを私有configの recovery_python に保存できます。保存前に選択先のPython 3.9以上とSQLiteのWAL修正を検証し、更新されるチェックアウト内の実行ファイルは拒否します。doctorは希望する設定と実際に配置された復旧ジョブを分けて報告し、設定だけ安全でも未修復のジョブを正常扱いしません。

- **読取り専用の修復計画と実施観測の記録を追加**
  公開済みスナップショットから修復候補を氏名・本文・対象IDなしの集計で確認できます。段階順を守る私有の追記記録と証拠集約を提供し、履歴の完全性や修復完了を主張しません。

- **依頼追従の抽出を独立した候補世代で実行**
  明示したrequest_following候補だけに宛先・依頼者・期限原文・条件・種別の抽出案内を追加し、既存抽出と別のprompt hash世代でキャッシュします。専用APIは実抽出と既存のsource束縛stagingを接続し、PENDING候補だけを保存します。

- **本人宛の返信・UI観測不足を読取り専用で一覧**
  公開snapshotのcaptureで明示的な本人宛userメンションを確認でき、返信・本人UI操作のどちらかが未観測または不明の投稿を、scope付きの一覧として取得できます。返信とUI操作を別々の事実として返し、返信があるだけで不明なUI観測を隠しません。本人ID・メンション・鮮度・本文世代を確認できない候補は本人宛とせず、不明の件数と理由を返します。

- **シグナルの解消理由と再発・抑制を内部で計測**
  シグナルの検知条件を変えず、記録上の解消原因、再open、同一根拠による却下後の抑制を保存・集計します。依頼登録とページ確認は別々に計上し、診療や対応の完了とは扱いません。

- **構造化薬歴・観測値とチャット候補の任意併記**
  構造化データの期間・項目ID・単位・取得状態を保持したまま、同じsnapshotの現行抽出世代に束縛されたチャット候補を別の出典として読めます。同名や値の違いから同一項目・正本・対応完了を自動判定せず、不一致は未確認として扱います。

- **対応版ごとの更新経路を再実行可能な合成資産に追加**
  過去26更新経路の版・runtime・根拠を機械可読manifestへ保持し、現行更新処理への13公開版・16構成のplan、適用、巻戻し、起動役、再導入を旧DDLと完全合成の境界で再検証できるようにします。

- **緊急度の独立した再確認候補を記録・通知**
  初回通常表示の記録後に現在のLLM抽出でhighが判明した場合のE1と、設定時間を過ぎても現在の表示世代の確認・自施設の後続投稿・依頼登録を観測しない場合のE2を、独立した通知intentとして記録します。本文hashと段階ごとに重複を防ぎ、送信直前にも現在世代と停止条件を再検査します。シグナルtextの緊急度表示も保存済みurgentフラグではなく現在の抽出へ統一します。

- **スタンプ再取得の反映と押下者取得を既定offで追加**
  監視対象のスタンプ再取得が成功した値を表示へ反映する設定と、スタンプを押した人の氏名・所属・職種を取得する設定を追加します。どちらも既定offで、アイコンは保存しません。

### 改善

- **時間切れの抽出と集計を次回へ継続**
  収集tickの残り時間がなくなった場合、ルール抽出と患者集計を処理単位の間で止めます。完了分は保存し、未処理分は次回へ残して、通知・終了処理の時間を確保します。

- **時間切れで次回へ延期した処理をhealthに記録**
  1.0.13で追加した時間切れの段延期について、次回へ回した処理（通知配送・意味抽出・定期整理など）の名前をhealth.jsonとstatus表示の実行情報にも表示します。どの処理が延期されたかを状態確認で説明できます。

- **稼働状態の理由と直近の正常判定を表示**
  health出力に、取得不完全・通知保留・延期・失敗・ディスク不足等の理由コードと、直近で正常判定した時刻を追加します。既存の滞留件数・最古の経過時間と合わせ、未取得や配送保留の場所を確認できます。

- **健康監視の状態ファイルと警告行に非正常の理由を表示**
  独立した健康監視が degraded や failed を報告するとき、状態ファイルと警告行に理由コードと最終成功時刻を含めます。状態ファイルには保留通知の理由別件数と各キューの最古滞留時間も載り、health.json を手で開かずに原因を確認できます。

- **取得できない理由を保持して状態表示**
  返信・履歴取得の失敗理由を保存し、通信の一時障害、取得不能、応答の構造不整合、未完了の区別を状態画面へ表示します。取得不能や記録未確認を、対応がなかった証拠として扱いません。

- **保存・認可・配送復旧と解析の共通処理を整理**
  利用者向けの操作や判定条件を維持して、ファイル保存、認可、対話の期限管理、ハッシュ計算、返信取得の重複実装を既存処理へ統一しました。出力ファイルと基準統計の保存にも共通の同期処理を使い、評価レポートの再読込みや解析・配送復旧の不要な中間コピーを除きました。

- **MCSスタンプを絵文字で表示し、要約・スタンプ・本文の順に整理**
  カード本体のスタンプ表示を絵文字の集計1行(例: MCS 👀5 🙆2 · 自分 2投稿)に縮め、各投稿の詳細はスレッドの投稿へ移します。すべての投稿を見出し・要約・スタンプ・MCSの本文の順に表示します。

### 不具合修正

- **任意バックアップジョブの復旧時所有権を保護**
  復旧時のcron退役をmanifestのID・Script一致または既知wrapperの正規identityに限定します。mcs_offsite.shを既知ジョブとして扱い、名前だけが似た共有cronを削除しません。

- **更新巻戻しで共有cronを保持**
  バックアップを含むcronの退役は、管理manifestのIDとScriptの一致、または既知wrapperの正規パスを必要とします。同じbasenameだけの共有jobを巻戻し時に削除しません。

- **カード経路と復元照合で保留した通知にも理由を記録**
  カード通知の不正な内容・再送上限・配送設定の不一致・処理例外による保留と、復元後の照合による保留でも理由が記録され、health の held_reasons で not_recorded ではなく理由別に表示されます。

- **変換cohortの投稿参照を整合**
  全体の変換cohortは投稿参照を持たず、項目のreceiptは実投稿のプロジェクトへ束縛します。投稿が無くなった項目の診断も対象IDを内容に保持し、不存在の投稿への参照を作りません。

- **取得状態の理由コードをそのまま表示**
  閲覧CLIのstatusと添付一覧で、親投稿本文の未取得・権限拒否・空の添付・既読化結果不明などの理由が「other_error」にまとめられず、収集側が記録した安定コードのまま表示されます。

- **グループ相談の取得種別と公開状態を保護**
  相談一覧の取得前と各ページで保存済みgroup種別を確認し、取得中の種別変更は完全取得として保存しません。snapshotの種別と保存scopeが一致する相談メタデータだけを読み、取得上限・snapshot不足のpartial、失敗、古い履歴、完全な空集合、不明を分けます。

- **インストーラーがLINE WORKSの導入手順を案内**
  install.shの完了案内とdry-runで、LINE WORKSはHermesのpluginとgatewayを使わず、lineworks_adapterのinit・check・serviceで設定・常駐させることを示します。--no-pluginの案内も、pluginが必要なのはSlackとDiscordだけと正しく表示します。秘密値は表示しません。

- **派生データと添付の投稿参照をDBで保護**
  投稿が存在しない派生データ・添付や、投稿とプロジェクトが異なる派生データの保存をDBでも検査します。新規DBは関係別の監査件数がゼロの場合に保存を拒否するガードを有効化し、既存DBへの初回導入は保存を止めず件数を記録するshadowから始めます。

- **通知の送信余白と復元後の配送保留を統一**
  テキスト通知は残り30秒未満で送信やin-flight予約を開始せず、再試行回数を消費しません。復元中は本文通知を止め、照合後も配送済みか確認できない復元テキストを保留するため、DBの巻戻りによる重複再送を防ぎます。

- **取得不能な返信・履歴jobを初回で失敗扱いに**
  返信・履歴取得でHTTP 4xx（408・425・429を除く）や応答の構造不整合が返った場合、8回再試行せず初回でfailedにして状態画面の検証未完了へ表示します。一時障害は従来どおり8回まで再試行します。

- **外部集計受領結果の誤認と重複送付を防止**
  合成の手渡し検証で受領成功・受領拒否・削除結果を区別し、拒否された送付は終端として再送しません。受領票の未知項目・不正な件数・理由・ハッシュを拒否し、結果不明は保留を維持します。

- **返信と本人スタンプの観測を独立表示**
  snapshotの投稿メタデータに、同スレッドの後続本人返信と本人スタンプ操作の観測を別々に返します。本人ID不明・取消・古い観測・取得失敗・期限経過から業務完了や未対応を断定しません。集計レポートは未知種別の値を露出せず、再確認時刻ではなく既知の観測時刻の差を使います。

- **実際の起動対象PythonとSQLiteの診断を補完**
  doctorは選択されたHermesまたはstandaloneのPython・SQLite版を実際に取得し、診断コマンド自身の版と区別して表示します。servicesと更新前検査も、選択runtimeまたは配備済みrecoveryが影響版・metadata不明なら変更を開始しません。installerはrecovery templateの実行対象を配備前に検査します。

- **サマリーの範囲と事後の緊急度表示を整合**
  日次・対話サマリーでアーカイブ済み患者のタスク・アラート・連携サマリー更新を除外します。自分宛の応答観測は既存の読取りhelperへ統一し、集計時点より未来の返信や取得失敗後の古いスタンプを応答済みとして扱いません。シグナルカードも現在の抽出に基づく緊急度と判定元を表示し、後から判明した緊急度を既存カードの更新で反映します。

- **削除済み返信を含むスレッドが未取得のまま表示され続ける問題を修正**
  削除済み（tombstone）の返信を含むスレッドで、返信取得ジョブが完了しても状態表示と修復計画が返信未取得として数え続けていました。状態表示・修復計画の未取得スレッド数を、返信取得ジョブと同じく本文取得済みまたは削除済みの返信で判定します。

- **否定・過去・将来予定の緊急語を区別**
  明示された否定、対応済み、過去や先の予定を含む緊急語だけで通知の緊急度を上げないよう修正します。現在の至急指示は維持し、表示では機械照合とAI抽出の根拠を引き続き区別します。

- **標準配置の日次バックアップを診断できるよう修正**
  バックアップ記録先のdata配下にあるsnapshotを、planとpreflightが誤って保存先の衝突として拒否する問題を修正しました。診断は引き続き読取り専用です。

- **参照受信CLIから保持期限後の本文を削除**
  合成参照受信CLIのreceiveは、実行時に保持期限を過ぎた本文を削除し、削除件数を返すようになりました。受領receiptと再送拒否用の記録は保持します。

- **数字だけのDiscord IDを設定するとカード操作が動かない問題を修正**
  hermes config setで設定したDiscordのチャンネル・サーバー・アプリケーションIDは、数字だけの値だと整数として保存されます。この場合、Discordのカード操作が起動しないか接続時に失敗し、操作の受付記録やカード操作が scope_mismatch で拒否されていました。整数として保存されたIDも文字列のIDと同じように扱うように修正しました。

- **セキュリティ：常駐ジョブのログを所有者だけが読める権限で作成**
  常駐ジョブの雛形に作成ファイルの権限制限を追加し、新しく作られるログが所有者だけ読める状態になります。収集・抽出・通知の動作は変わりません。

- **LINE WORKSで確定後の受付に失敗した時も返信するよう修正**
  確定するを押した後に依頼の受付処理が失敗した場合もDMで返信するようになりました。受付ファイルが作られていなければ「送信に失敗しました。」と伝えて再度の確定を受け付け、作られた可能性があれば結果確認待ちであることを伝えます。これまでは返信がなく、再度押しても最大10分間「処理中です。」と表示されるだけでした。

- **LINE WORKSの個別回答がテキスト通知の送信中に消える問題を修正**
  テキスト通知の送信と重なっても、閲覧の回答・反映結果・入力フォームの次の質問がDMで届くようになりました。これまでは送信ロックの競合で個別回答が送られずに失われることがありました。

- **失敗し続ける本文の夜間自動再試行が上限で止まるよう修正**
  LLM抽出が毎回失敗する同じ本文は、夜間の自動再試行を3回までで止めます。これまでは再試行の失敗時に回数が引き継がれず、毎晩ローカルLLMを無駄に呼び続けることがありました。

- **独立実行で別の保管場所を指定したときのバックアップ先を修正**
  独立実行で既定以外の保管場所を指定した場合も、バックアップ・読取り用スナップショット・ログ整理がその保管場所の中で行われます。既定の保管場所で使っている場合の動作は変わりません。

- **薬の変更件数から「〜は出来ない」という可否の記述を除外**
  薬の変更負荷とフォローアップ記録なしの統計が、「自己注射は出来ない」のような可否の記述を中止などの変更として数えていた問題を直しました。シグナル一覧と同じ基準になり、変更件数の水増しや誤った「フォローアップ記録なし」の行が出なくなります。

- **スタンプ影確認の対象選定が履歴の増加で極端に遅くなる問題を修正**
  metadata_shadowを有効にしている場合、5分ごとの対象選定が投稿とシグナルの蓄積に応じて急激に遅くなり、tickの時間を使い切ることがありました。対象となる投稿の範囲は変えず、選定を短時間で終えるようにしました。

- **依頼の確認・プレビューで失敗理由を具体的に表示**
  依頼の作成・更新や操作のプレビュー・確定で、元投稿が見つからない・取得が不完全・依頼が見つからない・期限の形式誤りなどの理由を、一律の失敗ではなく個別の理由として返します。入力誤りと投稿取得の不足とシステム障害を見分けられます。

- **Hermes連携の初期設定でproject IDが文字列として保存される問題を修正**
  mcs_setup initで--plugin-project-idsをカンマ区切りで指定、または対話で入力すると、project IDが文字列の一覧として保存されていました。プラグインは整数の一覧だけを受け付けるため、/mcsが plugin_config_incomplete を返し、Discord・Slackのカード操作も動きませんでした。整数の一覧として保存するように修正しました。正の整数でない値は保存せず、未設定として表示します。

- **/mcs の状況要約が操作カード未起動の環境で失敗する問題を修正**
  Hermes の /mcs コマンドで状況要約（op=summary）を求めたとき、Slack・Discord の操作カードが起動していない環境や起動前の呼び出しでも、operation_failed にならず要約を返します。

- **セキュリティ：復旧ジョブの通知で設定ファイルの安全確認を統一**
  復旧ジョブが障害を通知するとき、config.jsonがシンボリックリンクや過大なファイルなら通知を送りません。通常の設定での通知先と送信方法は変わりません。

- **アーカイブ済みの部屋で処方期間のシグナルが残る問題を修正**
  ケアが終了してアーカイブされた部屋でも、期間表現の終了間近・終了後のシグナルが確認一覧や書き出しに表示され続けていました。他のシグナルと同じくアーカイブ済みの部屋を対象外とし、既存の未対応シグナルはアーカイブを理由に解消されます。

- **Slackで確定した操作の結果が届かないことがある不具合を修正**
  Slackで確認して確定した操作は、カードが更新されると『受け付けました』のあとに『反映しました』や却下理由が届かないことがありました。カード更新後も、現在の権限を確認したうえで本人に結果を届けるようにしました。

- **Slackの表示名取得が一時的に失敗しても、あとで名前を取り直すよう修正**
  Slackで職員名の取得が一時的に失敗すると、ワーカーを再起動するまでカードの確認者・担当者が『メンバー』表示のままになり、📋担当タスクの照合もずれることがありました。失敗時は15分後に取り直すようにしました。

- **投稿が多い患者のスタンプ再取得で監視対象選択の負荷を低減**
  押下者の再取得で、患者の全投稿を候補ごとに走査する処理を索引で絞り込み、本文収集などと共有する予算を圧迫しにくくします。取得対象・件数・公開条件は変更しません。

- **独立実行でカード操作の連続処理が30秒待つ問題を修正**
  standaloneモードで、カード・承認操作の取込が正常終了した直後に新しい操作が届いた場合は待たずに処理を始めます。これまでは最大30秒待ち、Discordの応答待ち20秒を超えることがありました。

- **独立実行の初期設定・確認で失敗理由のコードを表示するよう修正**
  独立実行のinitでトークンを打ち間違えた場合や、checkでLINE WORKSの鍵権限・送信先の設定に問題がある場合、これまでは『failed ValueError』などの型名だけが表示され原因が分かりませんでした。standalone_credentials_invalidなど、秘密値を含まない固定の理由コードを表示するようにしました。

- **セキュリティ：更新承認で現在の版を特定できないときは承認を受け付けないよう修正**
  ops.update_applyの承認時に現在のcommit(HEAD)を確認できなかった場合、承認を記録せずupdate_base_unresolvableで拒否します。これまでは基準commitなしの承認が記録され、承認後にHEADが変わっても更新が進むことがありました。

- **最新タグより先の版で更新通知が誤って届く問題を修正**
  更新確認(update.modeがnotifyまたはauto)で、現在の版が最新タグをすでに含んでいる場合に「新しいバージョンを検出」や自動更新の「中止」通知が届かないようにしました。

- **更新前検査でDBの版を読めないときに誤った版変更を報告しないよう修正**
  更新計画・適用の事前検査で稼働中DBの版を読み取れなかった場合、版0からの変更(schema_bump)と誤って報告せず、schema_version_unknownで停止します。版が不明なことと版0を区別します。

- **緊急度の再確認通知を初回表示と確認可能な経路へ整合**
  確認操作のないtext初報から未確認再通知（E2）を送らないよう修正しました。初報の通常表示後にAI抽出で緊急度が高くなる後追い通知（E1）は維持し、初報の描画と判定元の記録の間に並行抽出が入ってE1を取りこぼす問題を防ぎます。

- **通知先を切り替えた後も旧通知先の未決着で稼働状態が劣化のまま残る問題を修正**
  カード通知先をDiscordからSlackへ切り替えた後、旧Discordの配送結果不明と送信保留が稼働状態を劣化にし続けていました。現在の通知先だけを数え、旧通知先の分は別枠の件数として表示します。

### 動作・設定の変更

- **スタンプを押した人の氏名をSlackに表示し、記録を無期限に保持**
  スレッドの各投稿で、スタンプ行の下に押した人の氏名と職種を表示します。押下者ごとの最後の観測状態を無期限に保持し、取消を観測した時刻を残します。再押下時には取消状態を解除し、過去の全操作履歴は復元しません。

- **スタンプの再取得をスレッドの動きに合わせた間隔で先頭・返信の全投稿へ拡大**
  スタンプは書込みの後から付くため、直近30日に動きのあったスレッドの先頭投稿と返信をすべて再取得します。間隔はスレッドの最新投稿からの経過で10分〜24時間に変わり、新しい返信が付くとそのスレッド全体が短い間隔に戻ります。

- **スレッドへの書込みを1投稿ずつ分離**
  同じタイミングで取り込んだ投稿や、履歴の補完で見つかった返信も、スレッドではそれぞれ別のメッセージとして書き込みます。前の書込みへ追記してまとめることはしません。

### 更新時の注意

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

- 設定変更は不要です。次回DBを開くときに投稿のスレッド・日時の索引を加法で作成します。既存データは保持し、schema番号は9のままです。実機での性能や稼働反映は未確認です。

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

### 技術詳細

<details>
<summary>技術詳細・根拠を表示</summary>

#### 任意バックアップジョブの復旧時所有権を保護

- 現在のservice_manifestと復旧snapshotのID・Scriptを照合します。別pathへ変更された同名ジョブや、所有根拠のないmcs_接頭辞ジョブは削除しません。未知のbackup意図は診断して所有ジョブを保持します。
- disabledまたは旧復旧先にtemplateがない任意ジョブは、所有根拠がありsnapshotでも希望されていない場合だけ退役します。offsiteを外部calendar agentの既定希望集合へ追加しません。
- 暗号化・Keychain・offsite処理は起動しません。既存の更新marker・復旧同意・owner receipt/reasonの契約を保持します。
- 合成job一覧・temp manifestだけで検証します。実hostの一覧・停止・cron退役・repo外recoveryの配備と、updater側の同型所有境界の統合は別途確認が必要です。
- 根拠: deployment/recovery/mcs_recover.py、tests/meta/test_mcs_recover.py

#### バックアップ鍵のエスクローと新端末への安全な復元

- backup.enabled=trueはpolicy・snapshotまたはsnapshot_dirのどちらか一方・毎日のschedule（複数時刻可）・verify_interval_s・drill_interval_sを必須とし、保存先・閾値・保持の既定値を補いません。servicesは任意jobをHermes cronへ渡し、独立runtimeは同じjobを単一host内で所有します。既定6jobと共有jobは保持します。
- healthは秘密値やreceiptの私有パスを出さず、バックアップ失敗・復旧点のRPO超過・検証/訓練の証拠欠落と期限超過を表示します。keygenで表示しただけでは端末外の鍵保管を確認したことにはなりません。
- 鍵は正確に32バイト、Keychainはmcs-backup serviceで、既存のstdin-only保存とread-back/rollbackを再利用します。.envや環境変数から鍵を取得しません。
- encrypt-then-MAC、別途信頼したSHA-256 receipt、固定OS OpenSSL、私有ディレクトリのidentity pin、容量上限と手動保持方針を維持します。
- backup_state.jsonとdrills/*.jsonは0600で原子的に書き込みます。鍵の作成途中の失敗は秘密値のないpending記録を保持し、再試行で既存鍵を置き換えません。
- 定期wrapperは静的な日次バックアップだけを対象とし、更新中は起動せず、鍵・DB内容・子プロセス出力をstdoutへ転記しません。新しい転送サービスや自動削除は追加しません。
- policyのscheduledが文字列・整数・nullなど真偽値以外の場合、offsite --scheduledは無効扱いの正常終了にせずbackup_policy_requiredで失敗します。offsiteの失敗は試行時刻とともに別に記録し、後続のverifyやdrillが成功してもstatusとhealthのバックアップ失敗表示は次のoffsite成功まで残ります。
- 根拠: mcs/ops/mcs_backup.py、mcs_standalone/runtime.py、deployment/scripts/mcs_offsite.sh、tests/ops/test_mcs_backup.py、tests/ops/test_backup_scheduler.py、docs/guides/BACKUP.md、SECURITY.md、docs/specs/lifecycle-spec.md

#### 鍵や書込みの前にバックアップと復元の準備状況を確認

- 容量は空きバイト数・保存済みbundle数とサイズ・保持枠・入力上限を分離して表示し、任意の最小空き容量や既定の保存先・周期を追加しません。
- 暗号化bundleのreceiptハッシュ一致は認証や復元成功の証明とはせず、収集完全性・臨床上の不在・添付本文の復元も保証しません。
- SQLite検査はimmutable・query_only・メモリ一時領域を使い、既定1000万VM命令・5秒の上限を設定します。plan/preflightのみ --max-steps / --max-seconds で検査上限を明示変更できます。中断は unknown / budget_exhausted とし、部分件数や検査完了を返しません。暗号・検証の既存呼出しは従来の検証契約を維持します。
- 完全合成の正負テストでファイル内容・mtime・モード・ツリーが変化しないことを検証します。
- 根拠: mcs/ops/mcs_backup.py、tests/ops/test_backup_preflight.py

#### 新端末の復元に専用の同意経路を追加

- receiptをDBの平文SHA、source bundle SHA、restore.json、元の同意待ちマーカー、配置先と各ファイルのdevice/inodeに束縛する。
- 独立したreceipt SHAと明示的な人確認・actor・reasonをresume時にも照合する。run.lock下で静的DBを再確認し、置換・改変・競合時は保留する。
- 原本や復元DBの内容を変更せず、履歴floor拡張、通知再送、臨床上の承認、再暗号化、配送照合、サービス開始を行わない。
- 正常採用後のDB更新は許容するが、配置先・DBの置換と復元証拠の変更は再び収集を保留する。既存の不明配送と未検証textは採用で解除しない。
- 同じ承認receiptでのresume再実行は冪等に成功し、恒久的な保留を残さない。別のreceiptは書込み前に拒否する。
- 根拠: mcs/ops/mcs_restore.py、mcs/notify/notify_cards.py、tests/ops/test_backup_restore_consent.py

#### 更新巻戻しで共有cronを保持

- 根拠: mcs/ops/mcs_update.py、tests/ops/test_mcs_update.py

#### 明示した静的snapshotと複数時刻でoffsiteを予約

- renderされたwrapperはsnapshotなら既存--snapshot、snapshot_dirなら従来--snapshot-dirを渡し、live DBへの切替や新しい収集tickを起動しません。
- 未設定時の6基本ジョブ、明示opt-in、scheduledのliteral true条件、既存の所有権付き退役・手動削除・source世代への束縛は維持します。
- 完全合成の静的ledger-snapshot.dbと実render wrapperで新鮮・86400秒超過・時刻不明を検証します。古い/不明なsourceは成功・healthyとして扱いません。実機のNAS mount、実行時刻、snapshot生成間隔、起動停止・遅延、receipt・鍵・訓練は未検証です。
- 根拠: mcs/ops/mcs_setup.py、deployment/scripts/mcs_offsite.sh、tests/ops/test_backup_integration.py、tests/ops/test_backup_scheduler.py

#### C1候補の分割送付形式と不完全な集合の検査を追加

- 送付形式はmcs-ext-export/2。分割しない場合はpartを省略し、分割時はindex・count・setをIDとintentに束縛する。records hash、集合hash、IDの順に確定する。
- meta・coverage・signals_truncated・患者別coverageを各分割に複製し、投稿と本文を不可分に扱う。同一投影のsignalは件数を減らさず同じpartへまとめる。
- 実際のUTF-8 wire bytesをparse前に1048576bytes以下へ制限し、最終のindex・count・集合hashを含む容量で分割する。収まらない共通メタデータや不可分な組を削除・切詰めして合わせない。
- 不足した集合はincompleteで完全性の証拠を返さない。同一auth・世代の既存完全集合の証拠が与えられた場合は、別集合を競合として拒否する。currentの保存・選択、撤回wire、送信者の分類は実装しない。
- 根拠: mcs/ops/c1_envelopes.py、tests/ops/test_c1_envelopes.py

#### 新契約の予約と手渡し受領を既存の安全経路へ接続

- LocalSink.receive_wireはparse前のwire byte上限と新版の厳密な検査を行い、保存payloadだけに新版canonicalを使う。journal・監査・receiptの旧serializerは維持する。
- GovernedExporterは契約版とpart・型別件数をjournalに束縛し、予約後のheld・拒否・撤回を終端として扱う。HandoffSinkは自己ackを出さない。
- 新版は型別件数が一致しない受領票や旧略式ackで本文送付を成功へ変えない。旧の束縛済みjournalと略式ackは互換を維持する。
- 集合の輸送受理を臨床完了や完全集合のcurrent証拠とは扱わない。本文例外はmessage_bodyのroot body_textだけで、他の禁止項目を解放しない。
- health --authは新版の7型認可を新版として検査し正常と報告する。scope省略の新版認可はaggregate扱いで例外にならず、受領票束はすべてのjournal束縛を先に確認し、一部だけ適用してから拒否することはない。
- 根拠: mcs/ops/ext_contract.py、mcs/ops/c1_envelopes.py、tests/ops/test_c1_delivery.py、tests/ops/test_c1_authorization.py

#### 新契約のsnapshot手渡しと変更なしの確認を追加

- deliver/handoffの--c1と--snapshot、--only-with-facts、--since-days、--max-bytes、--dry-runを追加する。全partを先に検証し、各送付の直前にも失効・鮮度を再検査する。
- handoffだけの実行はprivate configのext_exportから明示したprofileを読む。無設定・不正profileは拒否し、旧authの省略fieldsを新しい本文grantへ補完しない。
- 新版の既存state/outboxは所有者・私有mode・symlinkを検査し、共有directoryのmodeを自動変更しない。
- 要約はenvelope ID・part・件数・bytes・statusだけで本文を出さない。messages_kept=0は警告に留め、業務対応の不在や完了を推定しない。
- link-hintsはHermesのlocal TTYでだけ患者名・project_id・最終投稿日を表示する。redirectとstandaloneは読取り前に拒否し、snapshot世代に束縛したページングを使う。氏名のfile出力・wire送付・自動患者リンクは行わない。
- 根拠: mcs/ops/ext_contract.py、mcs/ops/c1_records.py、mcs/ops/c1_envelopes.py、tests/ops/test_c1_cli.py

#### C1候補データの独立したローカル契約検査を追加

- 数値はsafe integerと指定範囲の有限binary64に限定し、固定小数表記・負のゼロの正規化・不正Unicodeの拒否を独立して実装する。任意のJCS入力への対応は保証しない。
- profileはfields・patients・max_snapshot_age_s・retention_daysの4項目を検査する。load_authorization(c1=True)から明示的に呼び、本人承認・失効・期限・理由の既存検査も維持する。旧envelopeは新型のgrantを拒否する。
- message_bodyはtext形式、UTF-8で8192bytes以下、送出本文のhash、同じproject・message・世代のfull本文投稿との対応を必須にする。本文例外は同recordのroot body_textだけで、他のrecord・自由項目・入れ子の禁止キーを許可しない。
- patient_coverageのnullは不明のまま保持し、ledgerのfloor変換や送信者の分類は行わない。分割・wire識別子・envelope IDは追加しない。
- 根拠: mcs/ops/c1_contract.py、mcs/ops/ext_contract.py、tests/ops/test_c1_contract.py、tests/ops/test_c1_authorization.py

#### ローカル参照受信側に集合と投稿版のライフサイクルを追加

- sourceラベルから分離した私有名前空間で、受領メタデータ・集合証拠・投稿版hashを単一ファイルへ原子的に保存する。
- 最初の完全集合をauth・世代ごとに固定し、後着の競合集合を拒否する。古い世代や新しい不完全集合で現在の本文を誤って置換しない。
- 欠落投稿の降格は患者complete・既知のfloor/上端・範囲内の投稿時刻が揃う場合だけとし、臨床的な完了・無応答とは扱わない。
- ローカル撤回は到着前にもtombstoneを保持し、期限切れは初回受領時刻から判定して本文を削除する。受領・撤回のメタデータは再送防止のため保持し、容量上限では自動破棄せず拒否する。
- 診断は固定理由と件数だけを返し、actor・本文・sourceや投稿の識別子を含めない。撤回wireや共同fixture pinは別工程。
- 根拠: mcs/ops/c1_receiver.py、tests/ops/test_c1_receiver.py

#### C1候補レコードの読み取り専用組立と対保持選択を追加

- assemble_records(db, signal_limit=200)は現在のaggregate read model、current_open、既存project_recordを再利用し、DBを開き直さずBEGIN/COMMIT/ROLLBACKやDMLを発行しない。患者scopeは既存の全patientsを維持し、archivedを含むことと現在の上流fetch filter所属が不明であることを別のローカルscopeで明示する。
- message_bodyはbody_state=fullかつbody_text非NULLのみ生成し、空文字も保持する。UTF-8の8192 bytes上限を文字境界で切り、送信する切詰め後bytesのSHA-256を計算する。完成したc1_contract.validate_recordでproject/message/世代とfullの対応を検証する。氏名・職種・組織属性を送り出さず、自局推定もしない。
- patient_coverageは0-message患者とarchivedを含む全source患者について生成する。検証済み上端はledger.coverage_ts()と同じpatients.coverage_tsであり、最終取得時刻や最新保存投稿へ置き換えない。floorのNULL/0/欠列はnull、-1は0、正値はそのepochへ写像する。現在のC1 enumで表せないfetch_stateはC1ContractErrorで全体を拒否し、患者を除外したりpending/completeへ推測変換したりしない。
- patients_incompleteは新規coverage copyだけへ追加する。元のread modelと旧brain_export JSONL、既存serializer・record allowlistは変更しない。DB NULLのbody_stateはC1 message copyだけunknownへ正規化する。
- select_records(records, allowed, only_with_facts=False, since_days=None)は未許可typeの除去件数とmessage除去・時刻不明件数を返す。facts非空・deleted tombstone・許可された対本文のいずれかを持つmessageを残す。未許可messageや古い世代・異なるproject・orphanの本文は残さない。
- since_days窓はsnapshot generated_atから計算した開始とsnapshot時刻の両端を含む。時刻不明は窓から除き不明件数を明示し、壁時計で補わない。既知floorだけを窓開始へclampし、integer epochのfloorには開始値のceilを使う。不明floorと検証済みcoverage上端は変更しない。selectionの欠落を返信なし・臨床的な不在へ変換しない。
- 合成の実Ledger→publish_snapshot→View→組立/選択で、元DBとsnapshotのbytes hash/mtime・書込み数・caller transaction・既存JSONL不変、本文のempty/full/snippet/unknown/deleted/NULLとUTF-8境界、全患者coverage、完成validatorの対検証をテストする。実データ・network・LLM・サービス・新規依存を使用しない。
- 残るC0/CD判断はwire契約版・同一性/hash/part集合・sender identity分類・archived/現在の取得対象scopeの外部送付・相手側の完全集合と降格規則・最終fixture pin。本単体の成功をC0合意や実送付・本番・human/G6の受入とはしない。
- 根拠: mcs/ops/c1_records.py、tests/ops/test_c1_records.py

#### 手渡し送付に撤回指示書・合成受信側コマンド・C0固定fixtureを追加

- 撤回指示書 mcs-ext-withdraw/1 は contract・envelope_id・auth_id・理由コードだけで4,096 bytes以下。staging後始末は指示書を作らない。
- 受信側は指示書が原本より先に届けばtombstoneで後着を拒否し、part単位の撤回は集合をpartialとして扱う。到着済みenvelopeのauth_idと一致しない撤回は deleted:false を返す。
- receiveは契約違反を終端のrejected receiptにし、受信側の保存障害ではreceiptを作らず送信側をheldのまま残す。既存のreceipt出力ファイルは上書きしない。
- /2の検証でも禁止キーを forbidden_field と該当キー名 として報告し、stat等の非対象型は record_type_not_accepted と型名、負のhistory_floorは patient_coverage_floor_invalid とする。
- 受理12・拒否23（生成のみ1件を除く）・receipt 6・撤回4のC0 fixtureとMANIFEST.sha256、fixture set IDを固定し、送信側・wire parser・参照受信側の判定一致を検証する。
- CLIの --since-days は整数として受け付ける。
- receiveは同じoutbox内のreceipt等を入力とせずskipと数え、受理済みのenvelopeに後から届いた拒否receiptは上書きせず保留する。reconcile --allはjournalの一時ファイルを無視する。
- 世代単位の撤回と reconcile --all はjournalの件数上限（health診断用の1,000件既定）で止まらず、1,000件を超えるjournalでも全envelopeへ撤回を展開する。
- 根拠: mcs/ops/ext_contract.py、mcs/ops/c1_envelopes.py、mcs/ops/c1_receiver.py、mcs/ops/c1_records.py、mcs/ops/c1_contract.py、mcs/ops/export_schema.py、tests/ops/ext_fixtures.py、tests/ops/test_ext_contract_c0.py、tests/ops/test_c1_withdraw_receive.py

#### 依頼と返信のcanonical追従を合成評価で確認

- evaluation/canonical_request_eval.pyは既存のcanonical validator・legacy projection・G6評価器を実行するオフライン支援で、旧仕様の項目欠落を成功扱いしない。
- 容量の14日間・日平均6.5時間・job LLM時間p90 900秒・週slot_busy 1回以下を架空入力で検査し、観測不足は不明とする。計測する実時間はローカルの合成検証・投影・採点だけ。
- canonicalへの依頼詳細追加はLoop同一性、監査対象と世代改版の判断が必要なまま。人承認のreceiptや昇格tokenは生成しない。
- 根拠: evaluation/canonical_request_eval.py、evaluation/canonical_request_cases.json、tests/semantic/test_canonical_request_evaluation.py

#### canonicalの依頼項目をローカル候補で保持

- request_details=Trueでproject_v2_doc_legacyとproject_v2_factsを呼ぶと、候補のto/from/due_text/kind/conditionとassignee_text/time_textを生成する。既定呼出しは旧出力と保存済みLoop同一性を維持する。
- 同一fact IDの重複chunkでは追加項目を保持し、帰属や分類の矛盾は項目単位のunknownにする。片方だけがunknownの場合は未確定として扱い、引用に裏付けられた既知の値を残す。一度矛盾した項目は後続の重複でも復活させない。正式依頼や返信の自動結合・臨床workflowの変更はしない。
- semantic_v4.stage_request_followingは現行thread・投稿revision・本文hash・引用を検証し、既存v4_stageのPENDINGに旧/新doc hashと候補を保存する。canonical_projection・semantic_facts_v4・Loop状態・正式依頼を更新しない。
- 追加要件は既定extract-specと別の候補世代として扱います。request_to/request_from/due_text/conditionの原文引用とrequest_kind=request|question|self_plan|unknownを保持し、共有仕様の統合・公開世代の切替・Loop採用は親とオーナーの判断に従います。
- 既存readerの旧破損fixtureはg1_artifacts_msg_updだけを一時的に外して不正project行を植え、insert guardと元のreader防御の検証を維持する。
- 根拠: mcs/semantic/semantic_facts.py、mcs/semantic/semantic_extraction.py、mcs/semantic/semantic_projection.py、mcs/semantic/semantic_v4.py、tests/semantic/test_canonical_request_following.py、tests/semantic/test_canonical_projection.py

#### 依頼追従の人手レビュー待ち合成集合を追加

- 既存の抽出46件・completeness12件・canonical例示12件・練習1件を調べ、200件以上の関連人手キューがないため独立したpending形式を作成した。既存資産と既存schemaは編集しない。
- sourcesとsynthetic_proposalを別ファイルに保持し、manifestのSHA-256で固定する。case_idと架空account/project/thread、split=testとsource fingerprintをexport時にも維持する。
- ASCII数字・話者名・空白による差を除いて原文の重複を検出し、220原文と220対象文の独立性を検証する。意味的な多様性や合成宣言の真正性を機械判定だけで保証しない。
- 完全一致引用で支持された提案値は宛先20・依頼者23・期限原文22・条件49件。false_doneリスク65件、文脈側も引用する提案12件を含む。これらはAIの合成提案であり人手goldではない。
- exportは原文優先の固定混合順JSONLを全件出力し、focus・提案・署名・モデル出力を付けない。human_verified_labelsとhuman_receipt、model_outputs、telemetryは空のままにする。export-proposalsは別の合成提案ファイルとして出力する。
- 本人レビュー後のreceipt、方式別実候補出力・盲検比較・fact/claim/Loop対応・lifecycle・要求/token/latency・calibration・capacityは別工程で必要。非医療の創作会話だけで既存G6全指標の分母が揃うとは主張しない。
- 根拠: evaluation/request_following_heldout_sources_a.json、evaluation/request_following_heldout_sources_b.json、evaluation/request_following_heldout_proposals_a.json、evaluation/request_following_heldout_proposals_b.json、evaluation/request_following_review_manifest.json、evaluation/request_following_review.py、evaluation/request_following_review_protocol.md、tests/semantic/test_canonical_human_review_assets.py

#### カード経路と復元照合で保留した通知にも理由を記録

- 保留理由は notify_outbox の progress.hold_reason に安定コードだけを保存し、既存の受領記録は保持する。
- 追加コードは payload_invalid、resend_exhausted、dispatch_failed、restore_hold、restore_unlinked。処理例外は既存の internal_failure を使う。
- 根拠: mcs/core/ledger.py、mcs/notify/notify_cards.py、mcs/notify/notify_flush.py、mcs/notify/notify_reconcile.py、mcs/ingest/run_check.py

#### 薬剤の表記正規化と根拠付き検査候補を追加

- 薬剤の正規化は表記注釈だけで、同一成分・製品ID・代替薬・用法用量を推定せず、既存の薬剤集約キーを維持します。
- 検査値は数値と定性結果を区別し、単位換算・基準値判定・相対日付補完・投稿日時の測定日代用を行いません。
- quote_supportedは原文に裏付けられたAI抽出を示し、人による確認を意味しません。
- 測定日と未確認状態が異なる検査結果はchunk結合時に別候補として保持します。
- 承認済み薬剤辞書の導出artifact・統計注釈、v1ルール抽出・rollup・外部投影の拡張はこの限定実装の対象外です。
- 検査値の本人以外判定で「大丈夫」「工夫」などの語に含まれる「夫」を家族と誤判定しないよう修正しました。家族を示す語を含む引用は従来どおり未確認のまま残します。
- 根拠: mcs/extract/clinical_values.py、mcs/extract/v4/extract_llm.py、mcs/views/structured_view.py

#### 変換cohortの投稿参照を整合

- 根拠: mcs/semantic/semantic_v4.py、tests/semantic/test_semantic_v4.py

#### 本人宛メンションとしおりの横断読取りを追加

- MCSAdapter.fetch_cross_list(dataset) は mentioned/bookmarked のみを扱い、正規化済みMessageと未読flagの有無を分離したCrossListEntryを返します。未返却の未読flagはNoneで、Messageの既定falseを未読保持の証拠にしません。
- 既定は5ページ・20行/ページ・合計100行・25秒。許容上限は10ページ・20行/ページ・200行・120秒。継続には同じ正のserver timestampが必要です。終端の初回1ページだけtimestamp未返却を許容します。
- 公開クライアントOLVJNCFG・FUWQG6Z4・KBHQOTFVと既存正規化器を根拠にしています。no_extend_session=1を送り、increment_countは送りません。keep_read_statusはしおりの1件再取得でのみ根拠があり、横断一覧には推測で付けません。GET成功は既読・セッション非変更の実証ではありません。
- mentionedはunread_onlyの明示boolで取得集合を分離します。bookmarkedに未確認のunread filterは送れません。401はsession_expired、403等は分類用probeを行わずHTTP状態付きのhttp_errorとし、再試行やsleepはしません。
- project.idを正本として投稿IDと組み合わせ、project_id・parent_message・parent_idの矛盾と既存保存投稿のscope矛盾を拒否します。失敗時は作業中の部分集合を破棄し、以前の完全取得artifactを保持します。
- cross_list_v1 artifactには正規化ID、本文取得状態、投稿時刻、未読flagまたはNone、既存normalizerで抽出したmetadataだけを保存します。本文・氏名・添付URLは保持せず、message_metadataのcapture/shadow、本文hash、semantic current、未読snapshot/floorsへ書き込みません。
- 閲覧は取得ゲートと独立した明示公開ゲートが必要です。失敗後と期限超過は以前の完全集合を過去の観測として返し、現在の未読・未対応・正式完了を断定しません。
- 合成HTTP開封境界で実adapter・worker実行・JSON解析・成果物保存・snapshot読取りを検証します。認証済みMCS、実データ、秘密値、LLM、Jevは使用しません。
- 根拠: mcs/ingest/cross_lists.py、mcs/ingest/mcs_adapter.py、mcs/views/cross_lists_view.py、tests/ingest/test_cross_lists.py、tests/views/test_cross_lists_view.py

#### 時間切れの抽出と集計を次回へ継続

- CLIの期限なし処理は従来どおりです。既存のchunk単位commitを維持します。
- watchdogは固着時に非ゼロ終了し、次回は未完了runをcrashedにします。通常終了時は解除します。個別SQL・抽出一行の中では協調中断しません。
- 全体期限を越えたstageの後は追加作業を次回へ残し、終了記録とsnapshot公開を続けます。healthに所要秒数・超過秒数・最も遅いstageを記録し、期限超過をdegradedとして表示します。
- 根拠: mcs/ingest/run_check.py、mcs/extract/v1/extract.py、mcs/extract/rollup.py、tests/extract/test_derive_deadline.py、tests/ingest/test_run_watchdog.py、deployment/scripts/mcs_check.sh、deployment/scripts/mcs_deep.sh、mcs/ingest/health_watch.py

#### 薬剤の表層名を保った成分候補注釈

- mcs-drug-map/1を標準ライブラリだけで容量・件数制限付きで読み込みます。製品YJ/SSKコードや曖昧な類似名を成分の確定照合には使いません。
- med_refを現行source artifactのID・本文hash・抽出内容hash・辞書ID/SHA・resolver版に束縛し、変更時は旧注釈を置き換えます。
- rollupの再構築版を4に更新し、統計には表層名別の従来値と独立した分母付き成分候補内訳を追加します。外部機械exportのallowlistは変更しません。
- 候補注釈の置換・削除はrollupの再構築判定に含め、期限で再構築が後回しになった患者にも古い候補を残しません。初回は既存rollupを一度だけ再構築します。内容が変わらない患者は1回の再構築で再判定対象から外れます。辞書照合が期限で打ち切られた場合は状態をpartialとして返し、実行結果のerrorsにdrug_map: partialを記録します（辞書未使用時はunavailableのまま）。
- 根拠: mcs/extract/drug_map.py、mcs/extract/rollup.py、mcs/views/mcs_stats.py、mcs/ops/brain_export.py、mcs/ingest/run_check.py、mcs/ops/mcs_setup.py、tests/ingest/test_drug_map_pipeline.py

#### 認証付き暗号化バックアップと隔離復元訓練を追加

- gzip圧縮したDBをOS OpenSSL AES-256-CBC、PBKDF2-SHA256 600000回で暗号化。別ドメインで導出するHMAC-SHA256鍵により、形式ヘッダー・manifest・IVを導出するsalt・暗号文全体を認証する。
- 単一の認証済みbundleを0600でtmp、fsync、rename、ディレクトリfsync、read-backの順に公開。復号前に独立receiptとHMACを検証し、部分コピー・誤鍵・切詰め・改ざん・旧世代への差し替えを拒否する。
- 既存valid_mcs_db、file_sha256、publish_tmp、restore_pendingを再利用し、integrity_check、foreign_key_check、schema・件数・安全な集計の一致を確認。原本は変更せず、設定・認証情報ファイル・添付本体は収集しない。
- 機密性はローカルアカウント・OS・OpenSSL・鍵とreceiptの保管を信頼する。端末侵害、媒体消失、鍵紛失、時計の不正、平文scratchの物理回復や安全消去は保証しない。
- cron・health・実機復元・自動prune・外部サービスはこの機構に含めず、オーナーゲート付きロードマップ#1全体の完了とは区別する。
- 根拠: mcs/ops/mcs_backup.py、tests/ops/test_mcs_backup.py

#### 取得状態の理由コードをそのまま表示

- parent_body_incomplete・forbidden・download_empty・disk_full・mark_result_unknown・bad_snapshot_ts・bootstrap_error・response_too_large を表示語彙へ追加しました。
- どこからも記録されない body_incomplete を語彙から外しました。未知の値は従来どおり other_error、未記録は not_recorded です。
- 根拠: mcs/views/mcs_view.py、tests/views/test_status_fetch_reason.py

#### グループ相談の取得種別と公開状態を保護

- 汎用fetch_metadataもconsultationsではproject_type=groupを必須にし、既存fetch_group_consultationsから確認済み種別を渡す。公開クライアント根拠はmcs-api-surveyのgroup条件とstampsの共通GET条件。
- 既存page/per_page/include_paginate_totals、server timestamp固定、重複ID・件数・終端検証、上限、no_extend_session=1、no-proxy/no-redirectと最小allowlistを再利用する。
- 明示syncは各GET前と保存前にinventoryを再確認し、scope_changedはrowsを残さない失敗として既存artifactへ耐久保存する。partialも途中行を公開せず、直前の完全取得だけをhistoricalとして区別する。
- group readerはsnapshot inventory、artifact scope・任意project_type、bool complete、取得時刻、成功とエラーの矛盾を検証する。患者関連付けは常にunknownで、include_chatの臨床比較経路は変更しない。
- 合成adapter→実HTTP worker→偽remote opener→実artifact capture→publish_snapshot→LedgerReader/Viewを検証し、相談本文・返信本文・氏名・写真・連絡先や推測patientを保存・表示しない。
- 取得側tests/ingest/test_group_consultations.pyと読取り側tests/views/test_group_consultations_view.pyを別名で保持する。pyproject.tomlの既定prepend modeを変更せず、通常runnerで同名moduleの衝突を避ける。
- 実APIのgroup利用数、権限/404の原因、非空/空一覧、更新中完全性、未読保持とsession非延長、患者との安全な関連付けは未確認として維持する。group応答にtype echoや相談詳細・返信経路の保証を仮定しない。
- 根拠: mcs/ingest/project_metadata.py、mcs/ingest/mcs_adapter.py、mcs/views/project_metadata_view.py、tests/ingest/test_group_consultations.py、tests/views/test_group_consultations_view.py

#### 集計exportの手渡しとreceipt照合を手動CLIで操作

- reconcileは --envelope-id / --all / --receipts のいずれかを使い、receiptのstrict parserと既存journalへの束縛を維持します。
- healthはwriter・sink constructor・lock・audit更新を使いません。journalの既定上限は1000項目、ファイルは64KiBで、途中打切りや破損は完全な件数として扱いません。
- 本文・患者別coverage・分割/profile・数値正規化・link-hints・引数なしconfig導線・撤回指示書wire・実受信側の動作は残る契約・実装前提です。
- 根拠: mcs/ops/ext_contract.py、tests/ops/test_ext_contract_cli.py

#### 時間切れで次回へ延期した処理をhealthに記録

- run_check の health 生成で result の deferred_stages を run.deferred_stages へ複写する。health_watch は run を既存どおりそのまま表示する。
- 時間切れで延期した段は errors に延期した段名をまとめた1件 deadline_deferred:段名,段名 を追加し、runs.status を partial として記録する。延期のあった実行がバックアップの source_last_successful_run で成功扱いにならない。
- 根拠: mcs/ingest/run_check.py

#### 稼働状態の理由と直近の正常判定を表示

- 既存のoverall・通知・取得完全性・滞留の判定は変更しない。
- 状態理由には固定コードだけを使用し、本文・秘密・actor等を追加しない。
- 設定読込み前に実行が失敗した場合、health の backup は disabled ではなく state=unknown・reasons=config_not_loaded とし、state_reasons に backup_not_verified を出す。
- 根拠: mcs/ingest/run_check.py、tests/ingest/test_run_check_stages.py

#### 健康監視の状態ファイルと警告行に非正常の理由を表示

- run_check が記録した state_reasons、last_ok_at、notify.held_reasons、各キューの oldest_age_s を健康監視が状態ファイルへ写す。
- 警告行は reasons と last_ok_at を追加し、コードと数値だけを出力する。
- 根拠: mcs/ingest/health_watch.py、tests/ingest/test_health_watch.py

#### インストーラーがLINE WORKSの導入手順を案内

- standalone形態では独立hostがLINE WORKSアダプターを起動するため、別の常駐サービスを併用しないよう案内する。
- 根拠: install.sh

#### 職種間の構造的な返信時間を明示選択で確認

- 職種は投稿に保存された明示情報、または同一project内のactor IDが一意に一致するケアチームから取得します。氏名・施設名・本文による推定はしません。
- 複数職種は一つの職種集合として数え、欠損・未知の職種を既知の職種に割り振りません。
- 無効・負の時刻差、返信取得不足を時間集計から除外し、隠したセルの職種名・件数・時間値も出力しません。
- 職種不明ペア数と職種の取得元内訳は、小集団方針が未指定の場合と非表示セルがある場合に出力しません。非表示セルがある場合、有効ペア数から表示セル件数を引いた残り（職種不明と非表示セルの合計）がmin_pairs未満になるなら職種セルを全て非表示にし、差し引きで隠したセルの件数を特定できないようにします。
- 既存シグナル・候補・統計の閾値は変更しません。n=20をプライバシー閾値には使いません。
- 根拠: mcs/core/mcs_queries.py、mcs/views/mcs_stats.py

#### 取得できない理由を保持して状態表示

- 生の例外文やAPI応答ではなく、固定の理由コードだけを永続化する。
- 通信の一時障害は既存の再試行上限、認証失敗はattempt非消費を維持する。返信・履歴jobは4xx（408/425/429を除く）と応答の構造不整合を初回でfailedにし、known_gapsとして表示する。新しい取込依頼や再要求で同じjobは再開する。
- 成功・進捗・再開で古い理由を消し、failed jobは検証未完了のknown_gapsとして表示する。
- 編集・削除検出の再巡回と取込依頼で壊れたjobの失敗にも理由コードを記録する。再巡回は4xxや構造不整合でも初回で打ち切らず、巡回位置と完了回数を保持して従来の再試行上限まで続ける。
- 根拠: mcs/core/ledger.py、mcs/ingest/job_ops.py、mcs/views/mcs_view.py

#### DB整合性を件数だけで監査

- LedgerReaderの読取り専用接続を再利用し、単一読取りトランザクションとSQLite VMステップ上限で監査する。
- 未取得の親投稿、キューの未取得投稿ID、投稿を持たない集計artifact、project未指定の旧artifactを誤って違反にしない。
- 合成SQLiteだけで理由別件数、旧世代、破損、CLIとDBハッシュの保持を検証する。
- 根拠: mcs/core/ledger_audit.py、tests/core/test_ledger_audit.py

#### 派生データと添付の投稿参照をDBで保護

- healthとsnapshotのstatusは関係別のmode・初期化時existing_count・累積shadow_countを表示します。記録済みの違反件数があればhealthをdegradedにし、新たな完全性監査の結果と混同しません。旧snapshotで表が無ければ不明です。
- ledger_relation_guardsに関係別のmode・初期化時existing_count・累積shadow_countだけを保持します。shadow_countはコミットされた不正INSERT・参照列UPDATEの件数であり、ロールバックされた書込みは含みません。
- artifactsのmessage_idがNULLなら免除し、project_idがNULLなら投稿の存在だけを検査します。attachmentsのmessage_idはNULLも不存在として検査します。
- foreign_keys=OFFの接続にも作用します。既存行の削除・修復・表再構築、スキーマ版、AUTOINCREMENT高水位、FTS、その他の外部キー契約は変更しません。
- 実機の監査・観測期間・負荷は未検証です。enforce済みの関係も既存違反件数またはshadow観測件数があれば再初期化時にshadowへ戻します。ゼロ件のshadow観測は観測期間の完了を証明しません。
- 根拠: mcs/core/ledger.py、tests/core/test_ledger_guards.py

#### 導入・更新・初期設定・診断を共通コマンドに集約

- PATH上のPythonではなく導入時の実際のinterpreterとcheckoutをlauncherに固定する。
- ~/.local/bin/mcsに別ツールや別checkoutのlauncherがある場合、preflight・--dry-runで阻害として表示し、installは段階1より前に停止する。
- 更新bootstrapを再利用し、planではfetchせず、阻害時は終了コード1を返す。
- doctorの共有出力は分類・件数・runtime種別・バージョンに限定し、秘密・本文・ID・生応答を除外する。
- mcs setup checkで阻害要因と修正手順を表示し、doctorの案内もこのコマンドを示す。
- 根拠: install.sh、scripts/mcs、mcs/ops/mcs_cli.py、mcs/ops/mcs_setup.py

#### 本文を残さない編集・削除の観測履歴

- full/deleted間でhashが変わった場合だけ連番で記録する。
- 旧hashがNULLの行は履歴対象外とし、snippetで完全本文を上書きしない。
- snapshotにはhash履歴だけが含まれ、外部messageの論理キーとpayload契約は変更しない。
- 連番と直前hashは挿入文の中で最新の履歴行から決め、書込みが重なっても観測した編集を取りこぼさず、同じ編集を二重に記録しない。
- 根拠: mcs/core/ledger.py、tests/core/test_message_revisions.py

#### 新しい読取データをsnapshotから明示閲覧

- 観測値には--item-idが必要です。group相談の患者関連付けや臨床的完了は推定しません。
- 根拠: mcs/views/mcs_view.py、tests/views/test_project_metadata_view.py

#### 通知の送信余白と復元後の配送保留を統一

- 送信下限は30秒、既存の通信上限180秒を維持。各chunkと添付拒否後のtext-only再試行で再確認する。
- send_budget_insufficientとrestore_pendingをflush結果で区別し、結果不明と復元テキストの安定理由を既存progressに保存する。health.notify.held_reasonsは既知理由の件数だけを返し、不明値や自由文は出力しない。スキーマ変更は行わない。
- restore_text_unverifiedはrestore_reconcile.jsonにも記録し、ops.card_resolveの権限をtextへ拡張しない。
- session_expired・session_recovered・run_failed・update_noticeは復元中も既存経路で配送する。at-least-onceへの変更と既存heldの解除方針は未確定のため採用しない。
- 復元時点より後に作られたtext通知は隔離せず、復元照合が完了するまで送信を待機して自動で再開する。
- 復元中に待機する本文通知は再試行回数を消費せず60秒ずつ次回取得を後ろへずらし、待機中の本文通知が10件以上あっても後続のalertが取得枠から押し出されないようにする。
- 送信開始後に結果が確認できない通知は、種類を問わず本文の再生成前に保留する。元のシグナルや添付が閉じていても、患者がアーカイブ済みでも破棄せず、結果不明の記録を残す。
- コマンド結果ファイル(cmd_results)の定期削除は名前順ではなく古い順に対象を選び、7日を過ぎたファイルが削除されずに残り続けないようにする。
- 根拠: mcs/notify/notify_flush.py、mcs/notify/notify_reconcile.py、mcs/notify/notify_cards.py

#### 公式医薬品マスターのオフライン変換を追加

- 指定された公式menuとR08rec3.pdfの222/223頁を読み取り、42列・cp932・numeric前ゼロ省略・列ごとの文字/byte幅を照合した。現行downloadMenu/yFileと2026-03-31までのR07_y.zipを混同しない。初版converterはレビュー済みlayout R08rec3-medicine-42とedition 20260930だけを受け付け、未確認の将来版は拒否する。
- pin schemaはmcs-official-drug-master-pin/1。schema/layout/edition/member/as_of/SHA-256を照合し、source上限32MiB、CSV上限32MiB、100000 rows、単一CSV memberのZIPを扱う。展開せず読み、別member・path traversal・暗号化・symlink member・未知圧縮・不正cp932・42列外・型/幅/有効桁数違反を拒否する。
- 指定PDFは変更区分と0/99999999等の意味を説明しないため、status_policyのcandidate_change_values/absent_date_values/confirmed_byをoperator宣言として保持する。未確認change/date値・期限終了/境界・将来日・不明medicine identityを件数で報告し、機械がactiveや廃止なしを推測しない。
- 薬価12桁をYJ成分同一性へ変換せず、medicine/drug_price/general_nameの原値をsource_codesに保持する。一般名コード/textの整合が壊れたsourceは拒否し、別コードの同一aliasは複数候補のままにする。かなが最大20文字なら切詰めの可能性があるためaliasから外す。
- 新しいmcs-drug-map/2はproduct/general_name identityとraw source codesを表し、旧/1のingredient/classと区別する。既存clinical_values/drug_mapのfoldを再利用し、/2は名前・基本名・支持される完全かなのexact matchingだけとする。stemから製品/一般名処方を推定しない。候補表示も成分と誤称しない。
- terms_checked_onと実確認のterms_recordがない場合はoutputを保留し、approved_byは明示された場合だけ保持する。承認省略の辞書は既存loaderでunapprovedのまま。辞書entry/alias/byte上限を超えた場合は黙って縮小せず、medicine_codesによる明示curationが必要であることをreportする。
- private stagingを0600で作り、既存loaderとoutput hashを照合してからhard linkで新規destinationへ公開する。既存ファイル・symlink・共有/他owner directoryは上書きせず、失敗時の一時ファイルを除く。dry-runはaliases/rowsを表示せず、原本・config・DBを変更しない。
- fixturesは全て創作した42列cp932データであり、実マスターの行や匿名化行を使わない。実CLI、ZIP/CSV pin、旧/未来版拒否、unknown/ended identity、Unicode・衝突・kana上限、loader/annotation、0600/owner/umask/no overwrite、failure不変を検証する。
- 残るoperator判断は実source file/hashの確認、利用条件・転送/保存許可・承認record、変更/番兵値の根拠、採用するmedicine_codes subsetと辞書上限内のcuration。実マスター全件の変換・権利確認・本番有効化の受入とはしない。
- 根拠: mcs/ops/import_drug_master.py、mcs/extract/drug_map.py、tests/ops/test_import_drug_master.py、tests/extract/test_drug_map.py

#### 取得不能な返信・履歴jobを初回で失敗扱いに

- job_retryが理由コードの分類（unavailable・structural）を見て即時failedにする。
- 認証失敗はattemptを消費しない従来動作を維持する。
- 根拠: mcs/core/ledger.py、tests/core/test_job_reasons.py

#### 自分の投稿で医師の「見ました」未観測人数を表示

- 対象は自分の投稿だけで、既存の押下者件数表示と同じ範囲。
- 名簿IDと押下者IDが一致し、押下者の記録職種にも医師が含まれる場合だけ観測済みと数える。
- 空のケアチームは0人として既知、名簿の期限切れは履歴であり現在の根拠にしない。
- 職種不明のケアチームメンバーがいる場合は人数を出さず不明とする。
- 根拠: mcs/views/project_metadata_view.py、mcs/views/mcs_view.py、tests/views/test_physician_viewed.py

#### ケアチームと構造化情報の読取り専用取得を追加

- 既存artifact APIを使い、新規migration・既読化・抽出・通知・外部exportを追加しない。
- 公開クライアントのGET契約のみ実装し、写真・連絡先・相談本文を保存しない。
- 観測値はscalar/max/min/left/rightと観測時刻、確認済み定義の単位を保持。本文との一致は未照合と明示する。
- 相談詳細・回答本文、患者関連付け、薬剤用法用量はこのメタデータ契約に含まれない。
- 根拠: mcs/ingest/project_metadata.py、mcs/views/project_metadata_view.py、mcs/ingest/mcs_adapter.py、tests/ingest/test_project_metadata.py

#### 外部集計受領結果の誤認と重複送付を防止

- 参照LocalSinkはmcs-ext-receipt/1のacceptedとdelete結果を発行し、受領拒否を成功ackとして扱わない。
- HandoffSinkは自己ackを発行せず、JSON・NDJSON・ディレクトリからの手動receipt取込で照合する。終端ではローカルのstagingコピーを除去し、journalと監査を保持する。
- semantic・検知器・統計presetとexport allowlistの整合を合成テストで検証し、本文・氏名・未知の臨床項目を送付しない。
- 根拠: mcs/ops/ext_contract.py、mcs/ops/export_schema.py、tests/ops/test_receipt_contract.py

#### 再照合の完全巡回を記録

- 根拠: mcs/ingest/job_ops.py、tests/ingest/test_job_drain.py

#### 復旧用Pythonを明示して独立配置を維持

- 選択先は現在のrepoとupdaterの更新対象treeの外に限定する。環境変数による復旧用パスの暗黙取得はしない。
- Programが指定されたplistではProgramArgumentsへフォールバックせず、その実行先を検証する。
- servicesの修復は同一利用者の私有復旧ディレクトリ、repo_path、専用Labelと引数を確認し、既知の待機状態とupdate/runの排他ロックを要求する。
- 停止中の既存ジョブは停止状態を維持する。稼働状態が不明・実行中・所有権不明ならplistを変更しない。再起動失敗時に古い危険な実行先へ戻さない。
- 日本語の公開設定APIは recovery_python、initと初回installerのフラグは --recovery-python。既存のmode保存とatomicな0600 config/plist発行を維持する。
- 未指定で配備済みの復旧ジョブが安全でない場合、update planはmerge前にrecovery_runtimeを阻害要因として示し、notesに手順を出す。applyはmergeせずに停止する。
- 所有済みで停止中のジョブと明示した安全な選択との不一致はmerge前に止めず、merge後のservicesが修復する。post-mergeの子プロセスは、親が保持するupdate/runロックを、journalのshaと一致するhandshakeで確認した場合だけ受け入れる。
- backupまたはwatchdog_grace_sが不正な設定では、setup check・setup services・update plan/applyがtracebackで落ちず、config: ... を阻害要因として報告する。
- initはrecovery_pythonの検証をKeychainと.envへの書込みより前に行う。wizardで値が変わった場合は再検証し、config.jsonを書かない。
- Python 3.11-3.13が未導入でHomebrewが使える新しいMacでは、install.sh --preflight/--dry-runが復旧用Pythonの確認を阻害要因にせず、stage 6で確認する旨の警告にする。--recovery-python指定時は従来どおり阻害要因とする。
- 根拠: mcs/ops/mcs_setup.py、mcs/ops/mcs_update.py、install.sh、deployment/launchagents/org.mcs.recovery.plist

#### 読取り専用の修復計画と実施観測の記録を追加

- ledger_auditとLedgerReaderを再利用し、旧列の欠如を不明として扱います。floorは解除しません。
- recordはパイプライン・DB・MCSを実行せず、監査メタデータだけを追記・fsyncします。既存receiptの参照は可能ですが、新規receiptや修復権限は生成しません。
- 未確認の前提、段階の飛越し、改変された計画・スナップショット、バックアップ検証の失敗では進行を拒否します。finalizeは未完了・保留も明示し、既存ファイルを上書きしません。
- 根拠: mcs/ops/mcs_repair.py、tests/ops/test_mcs_repair.py

#### 保存・認可・配送復旧と解析の共通処理を整理

- 既存atomic_write・loads_dict・file_sha256・canonical・payload_hashを再利用し、0600、一時ファイルの排他的作成、保存バイト、承認用ハッシュを保持する。
- modal・confirm・followupの期限切れ削除と保存を共通化し、ロックと不正期限の拒否を保持する。
- ユーザー・チャット認可を共通化し、project ID・ユーザー・チャット・project scopeの判定順を保持する。
- 返信取得は既存のページ取得処理を再利用し、全件取得APIは途中エラーと上限超過を引き続き例外として報告する。
- 関係判定の比較順序と分類を保ち、itertoolsで組合せを順次走査する。
- 独立復旧ツール、no-clobber command公開、接続先固有の認証・receipt・再送抑止・互換importは維持する。
- 根拠: adapters/common/registry.py、adapters/common/worker.py、hermes_plugin/__init__.py、mcs/ingest/mcs_adapter.py、mcs/ops/brain_export.py、mcs/ops/ext_contract.py、mcs/ops/mcs_refstats.py、mcs/ops/mcs_update.py、mcs/semantic/semantic_assessment.py、mcs/semantic/semantic_bench.py、mcs/semantic/semantic_blind.py、mcs/semantic/semantic_evaluation.py、mcs/semantic/semantic_relations.py、mcs/views/message_metadata.py

#### 依頼追従の抽出を独立した候補世代で実行

- 既存_chunk_generation(model, prompt)とcached chunksの厳密な世代一致を再利用する。旧と候補のキャッシュを双方向に混用せず、coverage retryの既存schema分離も維持する。
- 候補promptと同じbase promptをtargeted repairに使い、修復成功・未実行・失敗でも候補generationを返す。従来repairの戻り値とpromptは既定flagでは維持する。repairは既存の明示呼出しであり、新しい自動repairや監査承認の権限は追加しない。
- extract_request_followingは現行threadから投稿を選び、既存transportへ注入callbackを渡す。抽出途中のprefixは同じ候補世代から再利用し、全chunk取得後だけ既存stage_request_followingを呼ぶ。抽出世代とdoc hashは既存v4_stage/PENDINGにも記録する。
- current/public fact artifact・Loop・正式依頼・収集状態・設定を更新しない。投稿revision・本文hash・引用・現行threadの既存staging検証と親のcohort/receipt変更を保持する。
- 実モデル/API/Jev/private dataは使用せず、完全合成callbackからvalidation・cache・repair・stagingまでを検証する。syntheticの成功をモデル品質・人手G6・calibration・本番受入と扱わない。
- 根拠: mcs/semantic/semantic_llm.py、mcs/semantic/semantic_extraction.py、mcs/semantic/semantic_v4.py、tests/semantic/test_request_following_generation.py

#### 本人宛の返信・UI観測不足を読取り専用で一覧

- 既存のself_sender_id・mentions_self・response_observationを再利用します。役割や押下者の別名を追加しません。helperにはメンション観測時刻と返信の時刻・actor不明コンテキストだけを最小追加します。
- 本人UI操作はUIの観測のみです。臨床的な非応答・完了・未読を推定せず、既知の返信時刻・観測時刻を維持します。
- 最大200件の候補をページ単位で走査し、cursorをsnapshot世代・project scope・鮮度条件に束縛します。非本人宛・不明候補を飛ばしたページでも走査位置を維持します。
- 一覧には氏名・actor ID・本文・自由文・本文hashを含めません。message/project参照と固定machine値だけを返し、元のメタデータ・台帳を変更しません。
- 完全合成の公開snapshotで本人宛true/false/unknown、返信併存、未来・時刻不明・actor不明、取消・部分観測、同時刻の本文revision、鮮度、削除・archive・scope・cursorを検証します。新GET・既読化・通知・収集変更・実API・LLM・Jevは実行しません。
- 根拠: #25、mcs/views/response_observation_view.py、mcs/views/message_metadata.py、tests/views/test_response_observation_view.py

#### 返信と本人スタンプの観測を独立表示

- 本人スタンプはUI操作の根拠のみです。未読は推定せず、観測時刻を押下時刻として扱いません。
- 氏名・押下者IDは観測集計に含めません。合成snapshot・カード表示・取消・失敗・期限・返信併存を回帰検証します。
- 実APIの追加受入・実機配備は本変更では実施しません。
- スレッドの返信件数が保存済み返信より多い場合は、未取得の返信が本人のものである可能性があるため返信状態を不明とする。
- 根拠: #22、#25、mcs/views/message_metadata.py、mcs/views/metadata_report.py、tests/views/test_response_observation.py

#### 実際の起動対象PythonとSQLiteの診断を補完

- 選択runtimeを-I付きのbounded stdlib subprocessで検査します。SDKクライアントをimportせず、discord.py・slack-bolt・slack-sdkの配布metadataだけを取得します。metadataの版表示はSDK接続・機能互換の実機受入ではありません。
- recoveryはProgramまたはProgramArgumentsの実行対象を読み、診断コマンドや固定のsystem Pythonと同じだと仮定しません。未配備はnot_checked、不正plist・metadata不明・影響版はblockedです。
- サービスの起動・再起動、DB・秘密・Keychainへのアクセスをdoctorの既定診断に追加しません。
- recovery未配備は従来どおり未確認・警告の扱いであり、任意watchdogを暗黙に選択しません。配備済みplistが存在するが不正・壊れたリンク・metadata不明なら変更を拒否します。installerの既存--no-recoveryによる明示skipは維持します。
- 対話式installerではEOFと同時に届いた入力を空へ戻さず、入力済みのstandalone選択を保持します。selectorでpromptを待つ既存テストを変更せず検証します。
- 実際の常駐プロセス、配備機械のSDK版・通信・SQLiteベンダーバックポートは合成テストでは未検証です。#9全体の実機受入は未完了です。
- 根拠: mcs/ops/mcs_setup.py、mcs/ops/mcs_update.py、install.sh、tests/ops/test_runtime_compatibility.py

#### シグナルの解消理由と再発・抑制を内部で計測

- signal_v1にlifecycleとresolutionを追加し、signal_feedback_v1に却下済み候補の評価回数と同一根拠抑制数を記録する。
- signal_feedbackは開いたepisodeを期間コホートとしてas_ofまで追跡し、分子・分母とWilson95区間を返す。再open率は観測期間で打ち切られた値であり、独立試行や精度・再現率とは扱わない。
- 解消は保存された根拠に対する検出器の除外条件で説明し、説明できない原因はunclassifiedとする。actor、氏名、自由文、根拠IDは集計に含めない。
- signal_feedback_v1は評価ごとに1行追加せず、案件・種別・日本時間の日ごとに1行へ合算する。合計回数と初回観測時刻は従来どおり。日内の時刻でas_of・since・untilを指定し、その日の行が境界をまたぐ場合は件数を推定せず集計から除き、statusをpartialとして該当行数を返す。
- 閲覧とbrain exportが表示する承認済み閾値ポリシーは、実際に適用する閾値と同じ厳格な来歴条件（文字列のcommand_idとactor）で判定する。条件を満たさない行は表示上も未承認として扱う。
- 根拠: mcs/ops/mcs_signals.py、mcs/ops/mcs_operations.py、mcs/views/mcs_stats.py

#### 構造化薬歴・観測値とチャット候補の任意併記

- 構造化側の最新試行と最後のcomplete artifact、チャット側のmessage ID・content hash・選択artifact ID/kind・投稿時刻を別々に示します。
- chat_comparison=side_by_side_unconfirmedでもmaster item IDとチャット表層名を結び付けません。care_team・group相談の表示と氏名の保存/表示条件を維持します。
- 欠落・古い取得・失敗・途中取得・不正shape・表示上限を正常な不存在へ置き換えず、absence_confirmed=falseを保持します。候補の測定日と投稿日時、構造化値の時点・単位を混ぜません。
- 根拠: mcs/views/project_metadata_view.py、tests/views/test_project_metadata_comparison.py

#### サマリーの範囲と事後の緊急度表示を整合

- 本人スタンプはUI操作の観測のみです。臨床的完了・未対応・未読を推定せず、既知の観測時刻と返信時刻を維持します。
- CLIのmine指定も対話サマリーと同じ名前必須条件で検査し、読取り接続を閉じます。
- 要約比較で過剰に深いJSONを安定した読取りエラーに変換します。人による採用・監査・公開ゲートを弱めません。
- 完全合成のsnapshot、4描画経路、本人専用コマンド、ページ単位確認、長文、空結果、緊急度の事後更新を回帰検証します。実サービス・LLM・Jev・SDK接続は行いません。
- 根拠: #12、#13、#25、#31、#32、mcs/notify/notify_digest.py、mcs/notify/notify_render.py、mcs/views/summary_review.py、tests/notify/test_summary_rendering_regressions.py

#### 削除済み返信を含むスレッドが未取得のまま表示され続ける問題を修正

- mcs_queries.incomplete_reply_rootsとmcs_repairのREPLY_GAPの判定をbody_stateがfullまたはdeletedの返信件数に揃えました。
- バックアップ点検のincomplete_reply_roots指標も修復計画と同じREPLY_GAPの判定を使い、削除済み返信を取得済みとして数えます。
- 根拠: mcs/core/mcs_queries.py、mcs/ops/mcs_repair.py、tests/ingest/test_thread_tombstone_gap.py、mcs/ops/mcs_backup.py、tests/ops/test_backup_reply_gap.py

#### 対応版ごとの更新経路を再実行可能な合成資産に追加

- 26経路はv1.0.0〜1.0.9のHermesから1.0.11/1.0.12へ20経路、1.0.10の両runtimeから両版へ4経路、1.0.11の両runtimeから1.0.12へ2経路です。公開記録とローカルタグのcommit・更新入口・installer差分を根拠にしています。
- Git操作・環境・常駐サービス・インストーラー・通知を合成境界とし、実plan/precheck_tag/apply/postcheck/backup/rollback/復旧同意とSQLite migrationを通します。履歴取得・Git commit・破壊的Git操作・実サービスをテスト実行時に使いません。
- 元のupgrade_routes.pyとupgrade-routes-2.logはrepoに資産化されておらず、隔離一時領域の絶対パスも記録されていません。過去の成功申告と今回の現行契約による再分類を区別し、旧target updater全体の再実行とは主張しません。
- schema0〜4/6の原DDL、実Gitの操作成否、サービス協調停止とSDK導入副作用・実機配備は未検証としてmanifestに残します。既存の13版とpre-release schema5の根拠は変更しません。
- 根拠: tests/fixtures/schema_upgrade/update-paths.json、tests/ops/test_supported_update_paths.py

#### 緊急度の独立した再確認候補を記録・通知

- 新しいledger列は追加せず、既存artifact・outbox・初回通知の配送証拠・世代付き確認記録を使います。初回通常表示の記録がない旧通知から通常表示を推定しません。
- 初回text表示の判定元はopt-in時だけ既存artifactへ記録します。interactive初報のE1判定には、親が初回intent封印時の同じtransactionからcapture_initialを呼ぶ接続が必要です。配送証拠のない部分受理を初回完了として扱いません。
- 結果不明・部分送信は既存保持経路で扱い、取消で送信証拠を消したり自動再送したりしません。復元保留と下限送信予算を維持し、日をまたぐ結果不明の試行も現在日の上限へ算入します。
- カードで担当中、または期限内の保留にされた投稿は再通知せず、送信直前にも同じ条件で取り消します。期限切れの保留は停止条件にしません。
- 本人スタンプは停止条件に加えません。記録不足を臨床的な未対応・完了と同一視せず、本人ID不明を否定証拠にしません。
- 完全合成のDB・送信stubでE1/E2、現在LLM高・routine・失効・本文hash不一致・ルール併存、重複・取消・ページ確認・結果不明・復元・予算を検証します。実サービス・LLM・Jev・SDK接続・MCSへのPOSTは行いません。
- mcs_setup.py checkがurgency_escalationを既知の設定として扱い、modeがon/shadowなのにroom_cooldown_min未指定や型不正で無効になる設定をエラーとして報告します。既定・判定・送信の挙動は変わりません。
- 根拠: #12、mcs/notify/notify_urgent.py、mcs/notify/notify_flush.py、mcs/notify/notify_cards.py、mcs/ingest/run_check.py、tests/notify/test_notify_urgent.py、mcs/ops/mcs_setup.py

#### 否定・過去・将来予定の緊急語を区別

- 同じ文の否定と現在の至急指示を分け、明示的な時点の変更を追跡する。
- 旧世代の保存結果が置き換わり、共通の緊急度表示から誤ったhighが消えることを合成DBで検証する。
- 「昨日から」「昨日より」「明日までに」等の継続・期限表現と「ただちに」は至急を打ち消さない。過去・将来の時点語は緊急語を直接修飾する場合だけ打ち消し、「昨日の採血で…、至急ご連絡ください」「明日の訪問前に至急」は現在の至急として扱う。
- 時点語の直後でも同じ文節に依頼表現（ください・お願い等）があれば現在の至急として扱い、予定や報告だけを打ち消す。「すぐに搬送」「緊急で搬送」のように隣接する緊急語は一つの句として否定・完了を判定する。
- 「搬送の必要はありません」「救急要請なし」を否定として扱う。
- 世代更新時は旧ルール結果を先に一括削除せず、再抽出した行ごとに同じトランザクションで置き換える。期限で中断しても未処理の行は旧結果を保持し、緊急度や処方シグナルが一時的に欠落しない。
- 根拠: mcs/extract/v1/extract.py、mcs/views/structured_view.py、tests/extract/test_extraction_review.py

#### 標準配置の日次バックアップを診断できるよう修正

- 合成DBをdata/snapshotsへ配置した診断回帰と、記録先の同一・逆包含の拒否回帰を追加。
- 根拠: mcs/ops/mcs_backup.py、tests/ops/test_backup_preflight.py

#### 参照受信CLIから保持期限後の本文を削除

- 既存expireを接続し、期限ちょうどの削除・receipt保持・冪等再実行を合成回帰で確認。
- 根拠: mcs/ops/ext_contract.py、tests/ops/test_c1_withdraw_receive.py

#### 数字だけのDiscord IDを設定するとカード操作が動かない問題を修正

- hermes_pluginの_settingsと_interactive_settingsは、application_id・guild_id・channel_idを既存の_id_textで正規化する。0以上の整数と空でない文字列を受け付け、boolは受け付けない。
- 根拠: hermes_plugin/__init__.py、hermes_plugin/card_workers.py

#### 常駐ジョブのログを所有者だけが読める権限で作成

- deployment/launchagents の全雛形に Umask 077 を追加した。
- 未対応: _cron_plist（mcs/ops/mcs_setup.py）と mcs_standalone/service.py の生成 plist。
- 根拠: deployment/launchagents/

#### LINE WORKSで確定後の受付に失敗した時も返信するよう修正

- 受付ファイルが存在する（公開済みの可能性がある）場合は、確定時の公開でOSErrorまたはValueErrorが起きると固定文のDMを送ってから元の例外を再送出し、二重受付を防ぐため確認の処理中状態と結果通知の追跡は従来どおり残す。DM送信自体の失敗で元の例外を置き換えない。
- 公開失敗後にcmd_intへ受付ファイルが存在しない場合（容量超過のValueErrorや置換前のOSError）は確認の処理中状態を解除して再確定を許可し、結果通知の追跡は再確定で上書きされるまで残す。
- 根拠: adapters/lineworks/actions.py

#### LINE WORKSの個別回答がテキスト通知の送信中に消える問題を修正

- Botの個別DM送信は、送信前のローカルロック競合(sender_busy)に限り0.5秒間隔で最大10回まで待って再送し、それ以外の失敗は従来どおり再送しない。
- 約5秒の再送でもロックが空かず、その個別回答をまだ1通も送っていない場合は結果通知の追跡を戻し、次の巡回で再送する。一部を送った後の失敗は重複送信を避けるため再送しない。
- 根拠: adapters/lineworks/actions.py

#### 失敗し続ける本文の夜間自動再試行が上限で止まるよう修正

- run_pendingの失敗記録を_failに統一し、夜間再試行の回数(auto_retry)を新しいエラー行へ引き継ぐ。
- 根拠: mcs/extract/v4/extract_llm.py

#### 独立実行で別の保管場所を指定したときのバックアップ先を修正

- 保守処理と既定offのLLM受付制御DBの保存先を、MCS_ROOTで選ばれた保管場所に合わせた。
- 根拠: mcs/core/maintenance.py、mcs/core/local_llm.py

#### 薬の変更件数から「〜は出来ない」という可否の記述を除外

- st_med_change_burdenとst_med_change_followupでmed_capability_evidenceに該当する薬剤言及を変更から除外し、DEFINITION_VERSIONを2026-10-05に更新。
- 根拠: mcs/views/mcs_stats.py

#### スタンプ影確認の対象選定が履歴の増加で極端に遅くなる問題を修正

- metadata_watch_targetsで、各キーの最新signal_v1の根拠投稿を投稿ごとではなく1回だけ集計するよう変更。
- 根拠: mcs/core/ledger.py、tests/core/test_metadata_watch.py

#### 依頼の確認・プレビューで失敗理由を具体的に表示

- /mcs と Slack・LINE WORKS のコマンド共通の振り分けで、英小文字の理由コードだけを返し、パスや入力値を含む例外は従来どおり operation_failed にまとめる。
- 根拠: hermes_plugin/__init__.py

#### Hermes連携の初期設定でproject IDが文字列として保存される問題を修正

- _apply_plugin_integrationのproject_idsはカンマ区切りと一覧リテラルのどちらでも整数の一覧に変換する。数字以外・0以下の値があれば書き込まずに失敗として扱う。ユーザー・チャンネル・ロールIDは従来どおり文字列の一覧。
- 根拠: mcs/ops/mcs_setup.py

#### /mcs の状況要約が操作カード未起動の環境で失敗する問題を修正

- plugin がファイル位置から読み込まれた場合も、要約で使う共通 adapter を読み込めるようリポジトリ直下を import 経路へ加える。
- 根拠: hermes_plugin/__init__.py

#### 復旧ジョブの通知で設定ファイルの安全確認を統一

- 復旧通知の設定読込を他の復旧処理と同じ検査付きの読込に統一し、到達しない重複した独立実行の送信経路を削除した。
- 根拠: deployment/recovery/mcs_recover.py

#### アーカイブ済みの部屋で処方期間のシグナルが残る問題を修正

- rx_period_expiryとrx_period_lapsedの検出でアーカイブ済みの部屋を除外し、既存のopenはproject_archivedとして解消。med_period_artifactsは統計と共有のため変更なし。
- 根拠: mcs/ops/mcs_signals.py、tests/ops/test_mcs_signals.py

#### Slackで確定した操作の結果が届かないことがある不具合を修正

- Slackの確定操作の結果通知は旧カードのボタンtokenに依存せず、送信元scope・許可ユーザー・操作者・project_id・route_epoch・interactive/transportフラグを送付時に再確認する（LINE WORKSと同じ判定）。
- 更新前に保存された確定待ち（project_id・route_epochなし）は従来どおりボタンtokenで判定し、再起動時点の結果通知を失わない。
- 根拠: adapters/slack/actions.py

#### Slackの表示名取得が一時的に失敗しても、あとで名前を取り直すよう修正

- SlackCardAdapter.display_nameのusers.info失敗を900秒の負キャッシュにし、成功した名前だけを無期限に保持する。
- 根拠: adapters/slack/delivery.py

#### 投稿が多い患者のスタンプ再取得で監視対象選択の負荷を低減

- 完全合成1患者・5投稿/スレッド・全capture反応・取得上限4件で、1万投稿の候補検索は3回中央値14.25秒から0.041秒へ短縮。同じ4件を選択することを確認。共有予算25秒は変更しない。
- 押下者の氏名・所属追加と全投稿監視拡大に合わせて、旧契約テストを整合。写真・連絡先非取得、完全walk・部分失敗、signal患者範囲と最新世代の検査は維持。
- 根拠: mcs/core/ledger.py、tests/ingest/test_reaction_actors.py、tests/core/test_metadata_watch.py、tests/ingest/test_acquisition_contracts.py

#### 独立実行でカード操作の連続処理が30秒待つ問題を修正

- cmd_intジョブが終了コード0で終わった場合の再起動間隔を10秒とし、失敗時と、MCSへ接続するcmdジョブ・抽出workerは従来どおり30秒を維持する。
- cmd_intジョブが終了コード0で終わった後、起動時に無かった新しい依頼ファイルが届いた場合は再起動間隔を待たずに起動する。失敗後と、同意保留などで同じファイルが残る場合は従来の間隔を維持する。MCSへ接続するcmdジョブは対象外とする。
- 定期ジョブ（mcs_check・mcs_health・mcs_deepなど）は前回の実行が予定時刻の直前30秒以内に終わった場合も、その予定時刻に起動する。これまではworker用の再起動間隔によりその回が飛ばされ、未読収集が5分遅れることがあった。
- 根拠: mcs_standalone/runtime.py、tests/adapters/standalone/test_runtime.py

#### 独立実行の初期設定・確認で失敗理由のコードを表示するよう修正

- mcs_standalone initのconnector_settings・validate_credentialsと、checkのtransports・LINE WORKS確認が出す固定コードのValueError/ClientErrorだけをConfigErrorに変換して表示する。それ以外の例外は従来どおり型名のみ。
- checkと起動時の確認も接続処理と同じconnector_settingsで操作者・ロールの許可設定を検証する。10進数でないロールIDや前後に空白のあるユーザーIDは、これまで確認をOKで通過して接続後に失敗し再起動を繰り返していたが、確認の段階でstandalone_grants_invalidとして停止する。
- 根拠: mcs_standalone/__main__.py

#### 更新承認で現在の版を特定できないときは承認を受け付けないよう修正

- 承認の書込みトランザクション内でgitを再実行せず、事前解決できなかったbase_shaはtarget_shaと同様に拒否する。
- 根拠: mcs/ops/mcs_operations.py

#### 最新タグより先の版で更新通知が誤って届く問題を修正

- cmd_checkは取得後にgit merge-base --is-ancestorでタグがHEADに含まれるか確認し、終了コード0のときだけ通知と自動適用を省く。判定できない場合は従来どおり。
- 根拠: mcs/ops/mcs_update.py

#### 更新前検査でDBの版を読めないときに誤った版変更を報告しないよう修正

- precheck_tagはsqlite3の読取り失敗時にcur_verを0とせずschema_version_unknownを返し、schema_bump判定を行わない。
- 根拠: mcs/ops/mcs_update.py

#### 緊急度の再確認通知を初回表示と確認可能な経路へ整合

- 初報の配送証拠からsealedカード経路とtext経路を区別し、送信直前の再検査にも同じE2条件を適用します。
- 未送信のtext初報は描画と初回緊急度の証拠記録を同じwriter transactionで実施します。送信処理はtransactionの外で実行します。
- 合成DBと送信stubでtextのE2抑止・E1維持、カードE2の時刻境界、別接続の並行抽出と初回表示の整合を検証します。
- 根拠: mcs/notify/notify_urgent.py、mcs/notify/notify_flush.py、tests/notify/test_notify_urgent.py、docs/roadmap/signals-stats.md

#### 通知先を切り替えた後も旧通知先の未決着で稼働状態が劣化のまま残る問題を修正

- notify.interactiveで選ばれていない配送先のgranted/unknown attemptと、その配送先だけに封印された保留イベントをlive件数から除く。
- 根拠: mcs/notify/notify_cards.py、mcs/ingest/run_check.py

#### スタンプを押した人の氏名をSlackに表示し、記録を無期限に保持

- 押下者一覧の氏名と所属を保存し、完全取得で見えなくなった押下はremoved_atを記録する。再押下でremoved_atを消し、observed_atを更新する。prune処理は削除した。
- 根拠: mcs/ingest/mcs_adapter.py、mcs/core/ledger.py、mcs/views/message_metadata.py、mcs/notify/notify_render.py

#### MCSスタンプを絵文字で表示し、要約・スタンプ・本文の順に整理

- 自分の投稿は本人分を除いた件数、未知の種別は❔で表示する。未取得・再取得失敗は集計行にも明示する。
- 根拠: mcs/views/message_metadata.py、mcs/notify/notify_render.py

#### スタンプ再取得の反映と押下者取得を既定offで追加

- publish時は成功した再取得だけを同じtransactionでcaptureへ書き、失敗・保存済みshadow値は反映しない。
- 押下者は完全取得時だけcurrentを置換し、最大4件/tick、期限24h、失敗後6時間待機。
- 根拠: mcs/core/ledger.py、mcs/ingest/run_check.py、mcs/ingest/mcs_adapter.py

#### スタンプの再取得をスレッドの動きに合わせた間隔で先頭・返信の全投稿へ拡大

- 間隔: 2時間未満10分・1日未満30分・3日未満2時間・7日未満6時間・30日未満24時間。未取得の投稿は活発なスレッドから順に処理する。押下者は直近7日に動きのあったスレッドの全投稿を対象にする。
- 根拠: mcs/core/ledger.py、mcs/ingest/run_check.py

#### スレッドへの書込みを1投稿ずつ分離

- _body_groupsは全メッセージに独自のpost keyを与え、legacyのまとめ投稿だけ既存メンバーを保持する。
- 根拠: mcs/notify/notify_cards.py

</details>

## [1.0.12] — 2026-10-04

**全通知先のコマンド操作とスタンプ判定の修正**

Hermesあり・なしのDiscord・Slack・LINE WORKSで閲覧と人承認操作を共通化し、スタンプの取得失敗・取消・差分を正しく扱います。Slackのコマンド登録と更新後の再起動が必要です。

### 新機能

- **Slack・LINE WORKSでも全機能のコマンド操作に対応**
  Hermesあり・なしのDiscord、Slack、LINE WORKSで共通の閲覧・依頼管理・運用承認を利用できます。QC・統計・シグナル等の閲覧と全13種の運用操作を既存のpreview/confirmとreceipt経路に接続し、送信元・患者権限・参照の再検証を保ちます。

### 不具合修正

- **スタンプの取消・取得失敗を正しく判定**
  取得に失敗したスタンプを有効な対応として扱わず、解消済みの依頼も監視期間内は取消を再確認します。公開を無効にしたshadow取得でも押した人を取得でき、件数ゼロの表現違いによる誤差分を抑えます。

### 更新時の注意

- メタデータ取得・公開の既定値、取得上限、既読化と人承認の条件は変更しません。変更コードを稼働workerへ反映してください。

- Slackはアプリに /mcs と commands 権限を登録して再インストールしてください。Hermes gateway、独立Slack/DiscordまたはLINE WORKSアダプターの該当プロセスを更新後に再起動してください。LINE WORKSは許可された本人トークで mcs に続けてJSON を使用します。既存の通知・取得・承認条件の既定値は変更しません。

### 技術詳細

<details>
<summary>技術詳細・根拠を表示</summary>

#### スタンプの取消・取得失敗を正しく判定

- 不正な保存フィールドを無効として読み取り、新しいshadow取得の失敗時には本人スタンプを応答根拠に使わない。不正なshadowをcaptureへ昇格しない。
- 本人スタンプで解消した未応答候補を監視期間内の再取得対象に残し、取消の公開後に再評価する。間隔・失敗時backoff・取得予算は維持する。
- 押下者取得と鮮度判定は最新の正常なcaptureまたはshadow観測を使い、公開設定とは独立させる。差分比較では種別省略の件数をゼロとする。
- 根拠: mcs/core/ledger.py、mcs/ops/mcs_signals.py、mcs/views/message_metadata.py、mcs/views/metadata_report.py

#### Slack・LINE WORKSでも全機能のコマンド操作に対応

- 既存のDiscordコマンド検証をSlack slash commandとLINE WORKSの本人1:1入力でも使い、全18閲覧kindとstatus・全13運用actionを共有する。
- 本人のpreview/confirm、最新参照とhashの照合、患者権限、送信元scope、cmd inboxとreceiptを維持する。全体集計はsnapshot内の全患者に権限がある場合だけ許可する。
- LINE WORKSはHermesにnative接続がないため、Hermesあり・なしで同じ独立アダプターを使用する。全6構成の合成テストと固定SDK統合を検証し、実サービスへの配備・送信は未実施。
- 根拠: adapters/common/commands.py、adapters/slack/actions.py、adapters/lineworks/actions.py、hermes_plugin/__init__.py

</details>

## [1.0.11] — 2026-10-03

**MCSスタンプの観測表示と絞込みサマリー・更新手順**

本人スタンプの観測表示とID判定を追加し、日次サマリー・絞込み・本人向け呼出しを共通化します。移行先版のコードで更新でき、DB更新は人承認付き、新しい再取得は既定で無効です。

> 更新前の確認：HermesモードはGateway、独立モードはSlackアダプターを更新後に再起動してください。Slackアプリのslash command設定は従来の追加手順が必要です。取得範囲・既読化・モデル・患者名の既定は変更しません。

### 新機能

- **MCSスタンプと投稿メタ情報を分離して保存**
  既存の収集応答に含まれるスタンプ件数・本人反応・メンション・しおり・ピン留めを保存します。未取得と0件を区別し、不正なメタ情報があっても本文収集を継続します。

- **MCSスタンプの本人反応と観測時刻を表示**
  カード脚注と未確認一覧で本人のスタンプを観測日時とともに確認でき、同患者の反応済みカードは一覧の末尾に表示します。取得未完了と0件を区別し、自投稿は送信者IDで「自分」と表示します。日次ダイジェストには対象期間に観測した本人反応の投稿数を追加します。

- **自分の投稿へのMCSスタンプと自分宛で応答未観測の投稿を表示**
  カード脚注で自分の投稿には他者からのスタンプ件数を表示します。日次サマリーと📊に「自分宛で応答未観測」と、再取得の反映を有効にした環境では「反応が観測されていない自分の投稿」を追加します。氏名は出しません。

- **スタンプ再取得の反映と押下者取得を既定offで追加**
  監視対象のスタンプ再取得が成功した値を表示へ反映する設定と、自分の投稿の押下者をID・種別・職種だけ取得する設定を追加します。どちらも既定offで、氏名・アイコン・施設名は保存しません。

- **本人の承知・完了スタンプを依頼の応答に数える設定を既定offで追加**
  有効にすると、依頼投稿そのものに本人の承知・完了スタンプが観測された薬剤師宛依頼を未応答候補から外します。見ました・他者のスタンプ・未取得は数えません。

- **スタンプ再取得の照合レポートを追加**
  通常収集と再取得のスタンプ値の一致・差・失敗理由・経過時間と、監視対象の未処理件数を件数だけで表示する読取り専用のレポートを追加します。投稿・患者IDや本文は出しません。

- **依頼候補の返信状況・メンション・しおり・スタンプ取得状況を表示**
  患者サマリーに依頼候補の返信状況を、閲覧CLIにメンション・しおり・ピン留めの有無と自分の投稿の押下者集計を表示します。日次サマリーの取得状況にスタンプ未取得の件数を、未確認一覧の見出しにMCSで本人反応ありの件数を出します。

- **要約サマリーを本人専用でいつでも表示し、対象患者を絞り込めるように**
  通知カードの「📊 サマリー」から、押した本人だけに要約サマリーを表示できます。Slackは見出し付きのカード、Discordは見出し付きの表示、LINE WORKSはDMで届き、all・mine（担当・記録上）・station:施設名・project:ID・days:1-7で対象と期間を絞れます。朝の日次ダイジェストは要約行・要対応・患者別新着・取得状況の順に見やすく整理し、空の節を省きます。

- **日次サマリーをカードで配信し、各チャットのコマンドから呼出し**
  カード通知が有効な環境では、朝の日次サマリーをSlack・Discord・LINE WORKSのカード形式で配送先チャンネルへ送ります。Discordの/mcs（op: summary）、Slackの/mcs-summary、LINE WORKSのBot DM「サマリー」から呼び出せます。SlackとLINE WORKSは本人への回答、Discordは公開範囲が未確認のため患者名を表示しません。

- **過去版から移行先版のコードで安全に更新する手順を追加**
  v1.0.3以降の導入は、移行先タグから取り出した起動役(scripts/mcs_upgrade.py)で更新計画の確認と適用ができます。install.shや独立モードSDKが変わる版も、操作者が--reinstallを指定すれば同じロック・バックアップ・巻戻し付きの経路で再導入まで行います。AIエージェント用のdocs/guides/UPGRADE_AGENT.mdが計画確認から完了報告までを案内します。

### 不具合修正

- **スタンプ脚注付きカードの文字数上限を維持**
  投稿や担当・確認・依頼の情報が多いカードでも、見出しとスタンプ脚注を含めて送信上限内にページを分けます。脚注の追加でカード全体が送信保留になる問題を修正し、投稿は別ページと本文表示から確認できます。

- **混雑日の日次サマリーカードが送られない問題を修正**
  新着の多い日に一覧を畳んだ日次サマリーが送信側の文字数検査で拒否され、その日のカードが届かない問題を修正します。畳み込みの文字数を送信側と同じ数え方で測ります。

- **本人の投稿と返信をIDで識別**
  自局名簿の本人フラグから本人IDを解決し、プロフィールのID欠落を補います。返信状態は氏名ではなく送信者IDで判定し、同名の別人や改名した本人を混同しません。識別できない投稿は返信済みと推測しません。 CLIの自局投稿判定も名簿の送信者IDで行い、IDや名簿が不明なら不明と表示します。

- **LINE WORKSで本文のないDMが不明な処理として残る問題を修正**
  画像・スタンプや空白だけのDMで例外が起き、不明な処理件数として残っていました。従来どおり何もせずに終了します。

- **サマリーの絞込みと集計期間を維持**
  本人スタンプの件数にも患者範囲の絞込みを適用します。定期サマリーで明示した日数が集計内容と記録に反映され、取得中にスナップショットが消えても回答処理が中断しません。

- **MCSスタンプの観測時刻が収集のたびに更新される問題を修正**
  未読投稿の再取得で値が変わらなくても観測時刻が更新され、カードが5分ごとに編集され、日次サマリーが同じ本人反応を毎日数え直していました。値が変わったときだけ観測時刻を更新します。

- **不整合な診断応答を確認成功にしない**
  投稿の未読診断でページ終端の矛盾を成功にせず、利用数診断では空の一覧と後続ページ・総件数の矛盾を調査不能として残します。未読保持と登録なしを誤って確認済みにしません。

- **利用件数の調査不能を完了や登録0件と区別**
  利用数診断で、一覧ページの不整合や医療ルームの患者ID欠損を調査完了にしません。薬歴・観測項目の調査対象を解決できない場合は不明として残し、相談のグループ単位集計と区別します。

- **大文字Pythonで起動した抽出workerの更新前検出を修正**
  HomebrewのPython実行名でも抽出workerを検出し、更新時に旧workerを見逃す問題を修正します。対象のスクリプト名を厳密に照合し、無関係なプロセスは停止対象にしません。

- **中断した再導入を手動完了した後に更新が止まり続ける問題を修正**
  install.sh再実行中に中断した更新を、手順どおり手動で完了しても更新計画が「再導入未完了」で止まり続けていました。完了を記録するreinstall-doneを追加し、15分ごとの復旧処理も未完了の再導入を適用済みにしません。

- **セキュリティ：Slackサマリーの返答を既存の通信制限に統一**
  Slackのサマリーは既存の接続済みクライアントから本人宛の非公開メッセージとして返します。別のWebhook接続を作らず、固定接続先・リダイレクト禁止・プロキシ禁止・再試行禁止の制限を維持します。認可外やチャンネル不明の入力には返答しません。

- **更新の中止時に旧版のままDBが新しいschemaへ上がる問題を修正**
  起動役(scripts/mcs_upgrade.py)で更新が中止・復旧不能になったとき、中止通知の投入で移行先版がDBを先に移行し、旧版の収集・抽出が起動できなくなる問題を修正します。通知は稼働中の版のコードで投入します。

### 更新時の注意

- 追加設定は不要です。カード表示の反映には、利用中のHermes gatewayまたは独立アダプターの再起動が必要です。通知先・確認・担当・依頼の承認条件は変更しません。今回の作業では再起動していません。

- 追加操作は不要です。カード表示の反映には、利用中のHermes gatewayまたは独立アダプターの再起動が必要です。

- 追加設定は不要です。既存プロフィールのID補完は自局名簿の次回取得時に行い、保存済みの患者集約は次回の集約処理で再構築します。シグナルの自局・職種設定と応答判定は変更しません。

- LINE WORKS独立アダプターの再起動で反映されます。

- 追加操作は不要です。daily_digest.scopeの日数指定時は指定期間を毎回集計するため期間が重複し得ます。日数未指定時は従来どおり前回の配信終端から集計します。

- DBは起動時にschema 8へ加法移行します。schemaを上げるため自動更新は適用せず、人承認付きの更新経路を使います。更新前バックアップを保持してください。メタ情報専用の再取得は既定で無効です。契約と未読保持を実証した後、metadata_shadow=trueで定期shadow、または --metadata-shadow で手動shadowを実行します（最大5件・最大25秒・末尾30秒確保、成功後30分・失敗後6時間）。Hermes pluginはgateway再起動、独立モードはhost再起動で反映します。既存の既読化条件・抽出モデル・通知先・人承認条件は変えません。

- 追加操作は不要です。既に保存済みの観測時刻は次に値が変わるまで最後の更新時刻のまま残ります。

- 追加設定は不要です。不整合な応答は失敗または未確認として再確認してください。MCS実画面・実APIの最終確認はユーザーが担当します。

- 追加設定は不要です。診断結果が不完全な場合は、失敗または未確認として再確認してください。この修正はローカルの合成テストで検証し、MCS実画面・実APIの最終確認はユーザーが担当します。

- Hermesでカード表示を更新する場合はgatewayの再起動が必要です。独立アダプターも起動中のプロセスを再起動してください。日次ダイジェストは既存の有効化設定に従い、既定では無効のままです。観測日時は押下時刻ではなく、スタンプから業務確認・担当引受・完了を自動実行しません。旧snapshotでは未取得と表示します。

- 設定変更は不要です。更新処理を実行するコードの反映後から適用します。稼働サービスの再起動は今回実施していません。

- 復旧処理(deployment/recovery)の変更は、install.shの再実行を伴う更新で反映されます。中断時の手順はdocs/guides/UPGRADE_AGENT.mdのreinstall_incompleteを参照してください。

- HermesモードはGateway、独立モードはSlackアダプターを更新後に再起動してください。Slackアプリのslash command設定は従来の追加手順が必要です。取得範囲・既読化・モデル・患者名の既定は変更しません。

- 追加設定は不要です。カード表示の反映には、利用中のHermes gatewayまたは独立アダプターの再起動が必要です。「反応が観測されていない自分の投稿」はmetadata_refresh_publish=trueのときだけ出ます。確認状態・依頼台帳は変わりません。

- 既定では動作は変わりません。metadata_refresh_publishは未読保持の実証とmcs/views/metadata_report.pyでの照合後、metadata_actorsは押下者の表示・保持の判断後に有効化します。どちらもmetadata_shadow=trueが前提です。DBはschema 8のまま押下者用の表を加法で追加します。

- 既定では動作は変わりません。signals.self_reaction_responseはオーナー判断(#22-D5)後に有効化します。切り替えると既存候補のresolve・再openが起き得ます。依頼台帳・割当・確認状態は変わりません。

- 追加設定は不要です。python3 mcs/views/metadata_report.pyで実行し、DBは読取り専用で開きます。不一致0は未読保持の証明になりません。

- 追加設定は不要です。表示の反映にはHermes gatewayまたは独立アダプターの再起動が必要です。未取得と0件・なしは区別して表示し、確認状態は変えません。

- Hermes連携のSlack・Discordは更新後にHermes Gatewayを再起動してください。LINE WORKSは独立アダプターを再起動してください。再起動前の旧アダプターでは📊ボタンが正しく動作しません（Slackは「操作できません」、Discord・LINE WORKSは結果の無い応答になります）。日次ダイジェストの対象は任意のdaily_digest.scopeで絞れます（既定は全患者、mineは指定不可）。通知先・送信時刻・患者名の既定（include_names=false）・既読化・人承認・取得範囲・モデルは変更しません。

- Hermes連携のSlack・Discordは更新後にHermes Gatewayを、LINE WORKSは独立アダプターを再起動してください。Slackの/mcs-summaryを使う場合はSlackアプリにslash commandとcommands scopeを追加して再インストールし、plugin settingsのsnapshotを設定します（導入ガイド付録B）。日次サマリーの送信先はカード通知が有効ならカード用の配送先、無効なら従来どおりnotify_targetです。DBはnotification_renders.intent_event_id列を追加します（加法）。この版より前へ巻き戻すと、未封印のカード版日次サマリーはその日の分が送られず、封印済みで未送の分は送信されても完了扱いにならず通知キューに残ります（翌日分の投入後は手動で整理してください）。Discordの/mcsのサマリーは返答の公開範囲が実機で未確認のため患者名を出しません。既読化・人承認・取得範囲・モデル・患者名の既定（include_names=false）は変更しません。

- 追加操作は不要です。v1.0.11以降への更新でUPGRADE_AGENT.mdの手順が使えます。v1.0.0〜1.0.2からは同書の手動経路を使います。自動更新とSlack等の承認経路はinstall.sh変更を含む版を従来どおり適用しません。独立モードの外部適用は未対応で、host経由の更新を使います。既読化・人承認・取得範囲・モデルの既定値は変更しません。

- 追加操作は不要です。起動役は移行先タグのコードを使うため、この修正を含む版を移行先にしたときから有効です。

### 技術詳細

<details>
<summary>技術詳細・根拠を表示</summary>

#### スタンプ脚注付きカードの文字数上限を維持

- 見出し・脚注・ページ位置の表示を含め、既存の4000文字検証を維持したままカードを組み立てる。
- 旧版で送信できた上限近傍の合成カードと、ページを通した表示対象の維持を回帰検証する。
- 根拠: mcs/notify/notify_render.py、tests/notify/test_metadata_display.py

#### 混雑日の日次サマリーカードが送られない問題を修正

- fit_partsが可視文字数と送信側spec検証の計上(項目ごとの加算)の大きい方で折り畳む。
- 根拠: mcs/notify/notify_render.py

#### 本人の投稿と返信をIDで識別

- 数値文字列と整数の送信者IDを正の64ビット整数に正規化し、不正値を不明扱いにする。
- 名簿の本人IDが複数ある場合は氏名やプロフィールで推測せず不明扱いにする。
- プロフィール補完は追記式で、既存の所属・職種の既定値を維持する。
- 根拠: mcs/ops/mcs_signals.py、mcs/extract/rollup.py、mcs/views/mcs_view.py

#### LINE WORKSで本文のないDMが不明な処理として残る問題を修正

- サマリー語の判定で空の入力を空文字として扱う。
- 根拠: adapters/lineworks/actions.py

#### サマリーの絞込みと集計期間を維持

- captureのみを本人スタンプの集計に使い、shadowは非公開のまま保持する。
- スナップショットの更新日時は読取り開始時に取得し、ファイル操作失敗を固定エラーに変換する。
- 根拠: mcs/notify/notify_digest.py、adapters/common/summary.py

#### MCSスタンプと投稿メタ情報を分離して保存

- message_metadataを本文hashと独立して保存。shadow観測は通常表示へ出さない。
- 監視集合は自分のルート投稿7日分・未確認カード・現在openの薬剤師宛候補。
- 氏名・押下者一覧・新しいMCS POST・外部export契約は追加しない。
- 実API確認は合成テストと別の公開ゲート。
- 通常tick/deepの定期設定を型検証し、shadowをsemantic処理後に実行する。専用deadlineを設定・復元し、healthにIDなしの状態・予算・延期理由を記録する。
- 返信投稿は親投稿ID付きの既存thread APIを使って正確に1件取得する。ルート投稿用経路へ返信IDを送って422になる失敗を防ぐ。
- 契約確認用の診断は、生の対象未読を取得前後で独立観測し、不完全・不明・初回の型不正を成功にしない。押下者は上限付き2断面の集合を値を出さず比較し、利用件数は患者データとgroup相談を分ける。最終MCS実画面検証はユーザーが担当する。
- 根拠: mcs/ingest/mcs_adapter.py、mcs/core/ledger.py、mcs/ingest/run_check.py、scripts/development/probe_message_metadata.py

#### MCSスタンプの観測時刻が収集のたびに更新される問題を修正

- message_metadataの各項目のobserved_atは値が前回と同じ場合に保持する。
- 根拠: mcs/core/ledger.py

#### 不整合な診断応答を確認成功にしない

- 総ページ数が2ページ目から返る場合も、未読一覧の終端ページとの一致を要求する。初回の空一覧と総ページ数0は保持する。
- 空datasetの任意paginateを検証し、has_nextや件数・ページの矛盾をunknownへ集計する。paginate欠落の互換性と非空先頭ページによる登録あり判定を保持する。
- 投稿診断と利用数診断の終了コード0の意味を受入票で分け、全22-A完了とは扱わない。
- 根拠: scripts/development/probe_message_metadata.py、tests/ingest/test_metadata_probe_observation.py、tests/ingest/test_metadata_probe_usage.py、docs/development/ACCEPTANCE_1.0.11.md

#### 利用件数の調査不能を完了や登録0件と区別

- ページ番号・ページ容量・重複ルーム・終端の整合を検証し、取得できていない対象を完全集計から除外して隠さない。
- medicalルームのkarte欠損・不正値を患者0件の成功にせず、解決不能な対象数と不完全状態を返す。
- 診断出力に患者ID・ルームID・本文・認証情報を追加しない。
- 根拠: scripts/development/probe_message_metadata.py、tests/ingest/test_metadata_probe_usage.py

#### MCSスタンプの本人反応と観測時刻を表示

- captureの反応値だけをカード・CLI・日次ダイジェストに投影する。CLIはcaptureの経過時間と遅延説明、別キーでshadowの取得状態・日時・理由だけを返す。
- CLIの投稿表示に種別別件数・本人フラグ・観測時刻・取得状態を追加し、外部exportとバージョン付きread_modelの契約は維持する。shadowの反応値は公開しない。
- 日次ダイジェストはreactionsの観測日時を半開区間で集計し、操作イベント数・押下時刻として解釈しない。
- 既存の本文配送とsource fingerprintは維持し、未知のスタンプをメンション構文として外部表示しない。
- 根拠: mcs/views/message_metadata.py、mcs/views/mcs_view.py、mcs/notify/notify_render.py、mcs/notify/notify_views.py、mcs/notify/notify_digest.py

#### 大文字Pythonで起動した抽出workerの更新前検出を修正

- pgrepのPOSIX EREで[Pp]ythonを許可。実子プロセスの正・負例で検証。
- 根拠: mcs/ops/mcs_update.py、tests/ops/test_mcs_update.py

#### 中断した再導入を手動完了した後に更新が止まり続ける問題を修正

- mcs_update.py reinstall-doneはHEADが再導入した版のときだけ完了を記録し、journalが残っていればrecoverで後処理を完了する。
- mcs_recover.pyは未完了のreinstallをescalateする。
- 根拠: mcs/ops/mcs_update.py、deployment/recovery/mcs_recover.py、docs/guides/UPGRADE_AGENT.md

#### Slackサマリーの返答を既存の通信制限に統一

- Boltのrespondは別AsyncWebhookClientを生成するため使わず、既存の_say/chat.postEphemeralを再利用する。
- 実SDKのSocket Modeでslash commandを配送し、response_urlを参照せず本人宛に返す合成回帰を追加。
- 根拠: adapters/slack/actions.py、integration/test_standalone_slack_sdk.py

#### 自分の投稿へのMCSスタンプと自分宛で応答未観測の投稿を表示

- own_post_reaction_textは本人分を除いた他者件数を表示し、count 0の種別を0で補わない。
- 自分宛で応答未観測は未対応の依頼候補シグナルと本人宛メンションから作り、本人ID不明時はメンション行を出さない。
- 根拠: mcs/views/message_metadata.py、mcs/notify/notify_render.py、mcs/notify/notify_digest.py

#### スタンプ再取得の反映と押下者取得を既定offで追加

- publish時は成功した再取得だけを同じtransactionでcaptureへ書き、失敗・保存済みshadow値は反映しない。
- 押下者は完全取得時だけcurrentを置換し、最大2件/tick、期限24h、失敗後6時間待機。
- 根拠: mcs/core/ledger.py、mcs/ingest/run_check.py、mcs/ingest/mcs_adapter.py

#### 本人の承知・完了スタンプを依頼の応答に数える設定を既定offで追加

- evidenceは変えず、既存の自局投稿による応答判定はそのまま。
- 根拠: mcs/ops/mcs_signals.py

#### スタンプ再取得の照合レポートを追加

- captureとshadowのchecked_atの前後を分類し、captureが新しいだけの差を区別する。
- 根拠: mcs/views/metadata_report.py

#### 依頼候補の返信状況・メンション・しおり・スタンプ取得状況を表示

- 押下者集計は職種×種別の件数と本人の有無だけで、氏名・押下者IDは出さない。
- 根拠: mcs/notify/notify_views.py、mcs/views/mcs_view.py、mcs/notify/notify_digest.py

#### 要約サマリーを本人専用でいつでも表示し、対象患者を絞り込めるように

- notify_digest.buildが共通表示モデル(parts)を組み、notify_render.fit_partsが上限を超える一覧を「…他N件」に畳み、parts_textが各チャットの書式(discord/slack/plain)に変換する。
- 📊はview actionとして既存のcmd_int→cmd_results経路で本人へ返し、receiptには本文・partsを保存しない。プロジェクト範囲はrunner側で必ず適用する。
- Slackの本人専用返答はslack.cards.render_partsで同じpartsからBlock Kitを組む。
- mineは押した人の表示名と未完了タスクの担当者欄の照合（📋と同じ規則）で、正式な担当割当ではない。
- 根拠: mcs/notify/notify_digest.py、mcs/notify/notify_render.py、mcs/notify/notify_cards.py、adapters/common/text.py、adapters/slack/cards.py

#### 日次サマリーをカードで配信し、各チャットのコマンドから呼出し

- 日次サマリーはカード行を持たないop=noticeのrenderとして発行し、受領がdeliveredなら通知キューをaccepted、実際の送信失敗はMAX_RESENDまで再発行、begin拒否は数えない。
- カード機能をオフにした時点で未送と証明できる日次サマリーだけをテキスト経路へ戻し、日次オフ・翌日分の投入後は送らずsuppressedにする。送れなかったnoticeの本文はgcで消す。
- LINE WORKSは1000字に畳み、超える分は既存のdisplay#分割で封入する。
- コマンドは公開snapshotを読取り専用で開き、各入口の静的プロジェクト範囲を必ず適用する。範囲が空なら表示しない。mineはname:名前で照合する。
- 根拠: mcs/notify/notify_cards.py、mcs/notify/notify_transport.py、mcs/notify/notify_digest.py、mcs/core/ledger.py、adapters/common/summary.py、adapters/slack/actions.py、adapters/lineworks/actions.py、hermes_plugin/__init__.py

#### 過去版から移行先版のコードで安全に更新する手順を追加

- mcs_update.py planが既存の検査結果を経路・阻害・再導入・再起動・各版の更新時の注意に分類して出力する。
- apply --reinstallはCLIからだけ受け付け、merge後のpost-merge内でinstall.sh --no-servicesを実行してからサービスを再同期する。中断復旧の再開時も再実行する。
- repo rootをmcs_util.REPOへ一元化し、MCS_UPDATE_REPOで一時展開した移行先コードを実checkoutへ向ける。
- v1.0.2〜1.0.7の常駐ai.mcs.extract-drainer-rtが配置されていれば更新時に停止する。
- 根拠: scripts/mcs_upgrade.py、mcs/ops/mcs_update.py、mcs/core/mcs_util.py、docs/guides/UPGRADE_AGENT.md

#### 更新の中止時に旧版のままDBが新しいschemaへ上がる問題を修正

- 起動役の一時コピーから実行中は、live treeのledgerを子プロセスで使ってupdate_noticeを投入する。
- 根拠: mcs/ops/mcs_update.py

</details>

## [1.0.10] — 2026-10-02

**Hermesなしの独立稼働を追加し、Slack通知と運用の安定性を改善**

独立モードで収集・通知・人承認操作・定期実行・更新・復旧を利用できます。Hermesの既定経路を維持し、Slack通知の読込み障害と独立モードの通知先・停止・切替えを修正しました。利用中の接続には更新後の再起動が必要です。

### 新機能

- **Hermesなしでも通知・操作・定期実行を使える独立モードを追加**
  スタンドアローンモードで収集・保存・検索・抽出に加え、Slack・Discord・LINE WORKSへの通知、対話カード、人承認操作、監視、更新・復旧を実行できます。既存の設定はHermesモードを維持し、同じ配送・承認・既読化の安全条件を使います。

### 改善

- **独立接続のコード配置を整理**
  Slack・Discordの独立接続コードを接続先別にまとめます。既存のCLI・旧import入口・送信結果・通知先と承認条件は維持します。

- **独立モードのCLIと送信再試行を統合**
  独立モードのrun/send/checkに、単一hostの定期実行・安全な更新再起動と既存のSlack・Discord配送を統合しました。受理されていない送信だけを最大5回まで再試行し、成否不明は自動再送しません。既定のHermes接続は維持します。

### 不具合修正

- **Hermes連携でSlackの新着通知が停止する問題を修正**
  Hermes GatewayがMCSプラグインを読み込む際のエラーを解消し、未送信のSlack通知を既存の配送処理で送信できるようにします。

- **独立稼働の通知先と停止・切替え処理を修正**
  指定した保存先の通知設定を一貫して使用し、明示した別チャンネルへのシステム通知と通知なしでの収集起動を可能にします。ログ障害時も子プロセスと接続を回収し、LinuxでHermesへ戻す際は独立サービスの停止を確認します。

### 更新時の注意

- Hermes連携でSlack通知を使う環境は更新後にHermes Gatewayを再起動してください。通知先・認証情報・稼働モードの変更は不要です。

- Hermes連携の標準保存先では設定変更は不要です。独立稼働の修正反映にはホストの再起動が必要です。システム通知は明示したnotify_system_targetだけを使用し、Slackのworkspace/applicationおよびDiscordのguildの照合を維持します。Linuxの切替えで停止が確認できない場合は成功扱いせずサービス定義を保持します。

- 設定変更・DB移行・サービス再登録は不要です。Hermesモードの接続経路は変更しません。

- 独立モードを使う場合はinstall.sh --mode standaloneで固定SDKを導入し、init、check、servicesを実行します。旧worktree版の個別calendar agentは停止し、同じBotを使うHermes接続・単体LINE WORKSサービスとの重複を解消してください。私有JSONを優先し、既存のroot配下.env（0600）も互換入力として使えます。稼働コードの更新後は選択モードのサービス再起動が必要です。install.shやSDK固定版が変わるタグは自動更新せず再導入してください。モデル・取得範囲・既読化・人承認条件・解析の既定値は変更しません。

- 従来のHermes運用は追加設定不要です。独立モードを使う場合はinstall.sh --mode standaloneを実行し、runtime_mode=standalone、操作ユーザー・プロジェクト範囲と専用資格情報を設定してcheckを実行してください。既存経路から切り替える際は同じBotのHermes接続、手動登録の定期ジョブや単体LINE WORKSサービスとの重複を解消します。更新後は独立hostの処理終了後に再起動します。独立モード非対応の旧タグへのロールバックは事前にHermesモードへ移行してください。モデル・抽出範囲・既読化・承認条件・解析の既定値は変更しません。

### 技術詳細

<details>
<summary>技術詳細・根拠を表示</summary>

#### Hermes連携でSlackの新着通知が停止する問題を修正

- 構成整理で共有配送モジュールを直接importしたため、Hermesのプラグイン読込時にリポジトリルートを解決できなくなっていた。既存の互換入口を再利用する。
- リポジトリルートをsys.pathに含めない独立PythonプロセスでSlackのhandler factoryを実行する回帰テストを追加する。
- 根拠: hermes_plugin/card_workers.py、8b124c6d6067fb939b15904db212778ab905e94a

#### 独立稼働の通知先と停止・切替え処理を修正

- 通知処理の設定パスをMCS_ROOTに対応する共通パスへ統一する。
- ログを開く失敗も捕捉し、終了処理では新規ジョブ起動をせず、状態ファイルの書込み失敗が子の回収を妨げないようにする。
- 送信用scopeは明示した通知先からチャンネルだけを解決し、認証主体・権限と既存の配送不確実性の扱いを維持する。
- localプレースホルダーを通知なしの構成として扱う。
- systemdの独立サービスをdisable --nowし、非稼働を確認してから定義を削除する。
- 根拠: mcs/notify/notify_flush.py、mcs_standalone/、adapters/slack/standalone.py、adapters/discord/standalone.py、mcs/ops/mcs_setup.py

#### 独立接続のコード配置を整理

- 旧ランタイム実装をadapters/slack・adapters/discord配下のruntime_compat.pyへ移し、mcs_standaloneの旧モジュール名は同じモジュール実体への互換入口として保持。
- SDKとasyncioの静的許可を移動先の個別ファイルへ引き継ぎ、通常adapterの許可は拡張しない。
- 文書生成のimport探索を_mcs_pathへ統一し、隣接する生成ヘルパーのパスを明示。リポジトリ外・隔離Pythonで生成内容を確認する回帰テストを追加。
- 根拠: adapters/slack/runtime_compat.py、adapters/discord/runtime_compat.py、mcs_standalone/slack_runtime.py、mcs_standalone/discord_runtime.py、scripts/development/update_readme.py

#### 独立モードのCLIと送信再試行を統合

- 公式SDKと推移依存を独立venvへ固定して導入する。
- 単一hostが既存6定期ジョブ、cmd/cmd_int、抽出worker2本と接続を所有する。
- scope・資格情報・添付pin・no-redirect/no-proxyを検証し、更新markerとgeneration付き再起動要求で処理中の強制終了を避ける。
- 既存の私有JSONまたは.envのみから実行用の資格情報を読み、環境やHermes profileへfallbackしない。
- 独立モード選択中はHermes pluginのMCS処理を起動しない。
- 根拠: mcs_standalone/、mcs/core/mcs_runtime.py、mcs/notify/notify_flush.py、mcs/ops/mcs_setup.py、mcs/ops/mcs_update.py、deployment/recovery/mcs_recover.py、install.sh

#### Hermesなしでも通知・操作・定期実行を使える独立モードを追加

- 独立venvへ公式接続SDKを導入し、収集・解析コアの標準ライブラリのみという条件を維持する。
- 単一hostが既存6定期ジョブ、cmd/cmd_int取込、抽出worker2本と接続を所有し、更新marker・heartbeat・子PID検証・再起動要求で更新と協調する。
- setupとrepo外復旧watchdogが選択モードのインタプリタ、scope、資格情報、常駐状態、所有ジョブを扱う。
- 根拠: mcs/core/mcs_runtime.py、mcs_standalone/、adapters/discord/standalone.py、adapters/slack/standalone.py、mcs/notify/notify_flush.py、mcs/ops/mcs_setup.py、mcs/ops/mcs_update.py、deployment/recovery/mcs_recover.py、docs/guides/STANDALONE.md

</details>

## [1.0.9] — 2026-10-01

**LINE WORKS接続を追加し、導入・表示・収集・更新の安定性を改善**

LINE WORKSへ要約・原文・添付を配信し、本人との1:1トークで入力・確認・確定できます。Slack・Discordの公式接続を維持し、導入手順と画面例を充実させ、収集・解析・通知・出力・更新時の異常処理を改善しました。更新後は利用中の接続プロセスを再起動してください。LINE WORKSの新規導入は専用の接続ガイドで進めます。

### 新機能

- **LINE WORKSに要約・原文・添付と本人確認付き操作を配信**
  LINE WORKSのトークルームへSlackと同じ要約・原文・送信対象の添付を配信できます。許可ユーザーだけが操作でき、入力・プレビュー・本人による確定は1:1トークで行います。Slack・Discordは引き続きHermes公式接続を使います。

### 改善

- **LINE WORKSの画面例と接続別の案内をREADMEへ追加**
  LINE WORKSの連続投稿と本人との1:1入力・確定を、架空の名前・投稿を使った2画面で確認できます。Discordと同じ折りたたみ表示で、通知先・データの行き先・スレッドの説明も3接続に合わせました。

- **READMEを目的別に読みやすくし、リリース時の更新を定着**
  Discord/Slackの画面例、目的別の使い方、FAQを開閉できるREADMEへ更新しました。最新の変更はCHANGELOGから自動生成し、毎回のリリースで機能説明・画面例・導入・安全・導線を見直す記録を検査します。

- **Slackを推奨とするREADMEへ更新し、7画面の表示例を追加**
  先頭の画面と導入導線をSlackへ変更しました。通知とスレッド、確認・担当、操作メニュー、タスク入力・確定・一覧、患者サマリーを、実装に基づく架空の名前・投稿を使った画像で紹介します。

- **接続共通コード・開発ツール・文書・テストの配置を整理**
  文書をガイド・開発資料・仕様から探せるようにし、開発ツールと接続先別テストをまとめました。Slack・Discord・LINE WORKSの共通配送コードをアダプター配下へ集約し、既存のimport入口は同じ実装を参照します。

- **初回導入の案内を設定と最終確認まで一本化**
  通常導入はインストーラーと設定ウィザードで完了し、重複していたサービス再登録とチェックを更新・復旧時だけ案内します。AI向け手順は未決事項だけを確認し、Chromeの起動確認を収集直前に移しました。スタンドアロンで抽出を使う場合の常駐worker配置も補いました。

### 不具合修正

- **不正な患者集約からの出力で既存ファイルを失わないよう修正**
  患者集約のJSONや表示に必要な構造が壊れている場合は、エクスポートをエラーとして停止し、既存の出力を保持します。不正な集約を「患者がいなくなった」と扱って患者ページを削除したり、一部だけ新しい出力に置き換えたりしません。

- **LINE WORKS追加後も既存接続とテキスト通知の動作を維持**
  Slack・Discordの新旧import入口で再送防止・再接続の状態を共有し、移動後も同じ接続処理が動きます。LINE WORKSの操作をoffにしてもテキスト通知を継続でき、別通知先として指定したLINE WORKSにも配信できます。

- **LINE WORKSの導入・操作結果・更新適用を安定化**
  通常のcheckoutから本体と共通の設定でLINE WORKSを導入できます。再クリックで古い閲覧結果を返さず、ローカル発行失敗後も同じ操作を再試行でき、カード更新後も本人の確定結果を受け取れます。添付の内容と処理開始の永続化を確認し、無効化・期限切れ時の入力処理を停止します。

- **通知の不正ファイルとSlack履歴確認失敗を安全に処理**
  深すぎる不正JSONの結果ファイルで通知の処理が中断せず、不正な表示ファイルは隔離します。Slackの既存返信履歴を確認できない場合は本文や添付を追加投稿せず、不明な配送結果として記録して重複を防ぎます。

- **長文解析の再試行で残り時間を再確認**
  長文出力が必要な解析でJSON形式が拒否された場合、残り時間が400秒未満なら追加のモデル呼び出しを止めます。長文出力の記録を保持し、後続の十分な時間を確保した試行で再処理します。

- **macOSで更新時の残存解析プロセスを検出**
  macOSのpgrepでも認識できる検索式に変更し、更新時に残った解析プロセスを見つけられるようにします。対象スクリプト名を正確に照合し、別のプログラムやコマンド中の文字列を誤検出しないようにします。

- **長文解析の拒否判定と形式切替を維持**
  形式拒否後に再送時間が不足したHTTP400は、通常の再試行ではなく要確認の終端結果として扱います。形式拒否の記録を共有し、別workerや再起動後にも600秒間は制約なしの形式を選びます。

- **不正な抽出結果で閲覧・集計出力が停止する問題を修正**
  保存された事実・関係の配列やIDの型が不正な場合は、読み取りモデルでunknownとして扱います。入れ子の内容が集計出力へ混入せず、同じ本文に対応する利用可能な過去の結果があればその結果を保持します。

- **一覧取得に失敗したcronジョブの削除を防止**
  更新・復旧時にcronジョブ一覧の取得が失敗した場合、残された出力を根拠にジョブを削除しません。確認できなかった状態は従来どおり復旧の問題として報告します。

- **特殊文字を含むDBパスの更新・復旧判定を修正**
  DBやバックアップのパスに #・?・% などが含まれていても、指定したファイルのスキーマ・承認記録・復元による影響を確認します。別のファイルを参照して復旧を完了扱いにする問題を防ぎます。

- **リリース本文の同期で下書きのタグを保持し、失敗を検出**
  GitHub Releaseのタイトル・本文を更新するとき、下書きと既存タグの紐付けを保持します。同期エラーをCIの失敗として検出し、更新後もタグ・公開状態が変わっていないことを確認します。

- **所見の短縮表示でも否定・推測・家族・予定の条件を保持**
  所見の否定・推測・家族・予定を短縮表示でも保持し、解釈条件を本文より前に確認できます。

- **取得ジョブとAPI応答の異常JSONによる収集停止を修正**
  取得ジョブやAPI応答の異常JSONで全体の収集が止まらず、異常な患者応答は未完了として既読化を抑止します。

- **診断DBパスとLINE WORKSの破損処理記録を安全に処理**
  診断が特殊文字を含む正しいDBを読みます。LINE WORKSの破損した処理記録は成否不明として再実行を抑止しながら後続入力を処理します。

- **意味解析の未送信修復と壊れた保存記録への耐性を改善**
  要約の修復が予算不足やモデル受付待ちで送信されなかった場合、修復回数を消費せず次回再開します。同じ処理で確認済みの投稿は保存され、実行済み修復の再実行は引き続き防ぎます。壊れた意味解析の保存記録があっても状態表示や評価用の配送段階確認を中断しません。

- **不正な却下理由の記録でアラート一覧が停止する問題を修正**
  保存済みの却下理由区分が不正な型や未知の値でも、アラート一覧は停止せず未分類として数えます。自由なメモを理由区分として集計出力に含めません。

- **監視とローカル解析が不正な入力で停止する問題を修正**
  監視・解析のJSONが深すぎる場合や監視状態の文字コードが壊れた場合も、失敗を判定して処理を継続します。別ポートに設定したローカルAIサーバーの稼働・形式確認も設定先に統一し、対話用の形式確認を見送った際に待機状態が残ってバックログを止める問題を修正します。 大きな対象指定でも選択件数の上限より先に対象を絞るため、許可された古い投稿が処理から取り残されません。

- **型の壊れた抽出結果でも構造化表示を継続**
  抽出結果の配列・文字列・区分値の型が壊れていても、通知の構造化表示を停止せず、読める項目を表示します。読み取れない症状・薬剤・依頼からルール抽出へ戻して、除外された記載を復活させることを防ぎます。

- **未確認の検査値を候補として表示**
  抽出時に未確認とされた検査値は、通知の「検査候補（未確認）」行に表示します。「検査」行と分けることで、未確認フラグのない項目と混同することを防ぎます。

### 更新時の注意

- 追加設定やDB移行は不要です。不正な集約がある場合はpatient_rollup_invalidを返します。集約を再生成してからエクスポートを再実行してください。

- 利用する場合だけnotify.interactive=lineworksとBot・ドメイン・部屋・許可ユーザー・MCSプロジェクト範囲を設定し、端末でpython -m lineworks_adapter initとcheckを実行してください。Botの管理者登録、公開HTTPS Callbackと独立プロセスの起動が必要です。常駐サービス候補はserviceで生成できますが自動登録・起動はしません。既存Slack/Discordのフォルダ移動を反映する場合は対象Hermes gatewayの再起動が必要です。既定の通知先・モデル・取得範囲・既読化・人承認条件は変更しません。

- Slack/Discordを利用する場合は対象Hermes gatewayを再起動してください。LINE WORKS利用時は独立プロセスへ更新を反映してください。offは従来通りカード・人承認操作を停止し、テキスト通知を維持します。全通知を止める実行には--no-notifyを使います。接続scope・認証設定・既読化・理由・本人確定・モデル・取得範囲の既定値は変更しません。

- LINE WORKS利用時は更新後にpython -m lineworks_adapter checkを実行し、独立プロセスを再起動してください。設定・dataの既定保存先は本体と同じ~/.mcsです。旧実装でcheckout側へ認証情報を作った場合は、本体の保存先を確認して所有者のみ読める権限で配置を揃えてください。サービス候補の配置先・起動パスが変わる場合はserviceで再生成します。Slack/Discordのadapter変更は既存のgateway再起動判定に含めます。既定通知先・モデル・取得範囲・既読化・人承認条件は変更しません。

- pluginの変更を適用するにはHermes gatewayの再起動が必要です。Slackの履歴取得に失敗した配送は、原本と通知先を確認して既存の配送不明の解決手順で対応してください。モデル・取得範囲・通知先・既読化・人承認条件・既定設定の変更はありません。

- 通常のコード更新手順で反映します。モデル・タイムアウトの既定値・設定変更・データ移行は不要です。

- 通常のコード更新手順で反映します。設定変更・データ移行は不要です。検出できない場合に更新を中止する既存の安全動作を維持します。

- 通常の検証済みコード更新で反映します。設定変更・データ移行は不要です。形式拒否の共有記録は書込不能やlock競合時には保存を省略し、既存の処理を維持します。

- 追加操作やDB移行は不要です。不正な結果の状態はcurrentからunknownに変わります。

- 設定変更は不要です。画像は現行実装に基づく架空データの説明図で、実画面のキャプチャではありません。通知・取得範囲・既読化・人承認・理由・receiptの実行条件は変更しません。

- 利用者の追加設定・データ移行は不要です。リリース準備ではdocs/development/readme-review.jsonの5項目をソースと照合して新しいversionへ更新してください。

- 利用者の設定・データ移行は不要です。Slackを導入する場合は導入ガイドの接続・許可ユーザー設定を確認してください。画面例の更新では生成スクリプトでSVG・PNGを一緒に再生成します。

- 通常のコード更新に加え、配備済みの独立復旧ツールへ反映するには install.sh の再実行が必要です。設定変更は不要です。成功した一覧に対する所有範囲の確認と復旧条件は変わりません。

- 通常のコード更新に加え、配備済みの独立復旧ツールへ反映するには install.sh の再実行が必要です。DBの移行や設定変更は不要です。復元には従来どおり対象バックアップと損失報告に一致する人の承認が必要です。

- 利用者の追加設定は不要です。未公開の下書きでタグの紐付けが失われている場合は、対象を確認し、承認されたリリース作業で下書きを作り直します。公開済みのタグ・公開状態・添付資産は変更しません。

- 次回の表示から反映されます。追加操作は不要です。DB schema、モデル、取得範囲、通知先、既定値、既読化・人承認・理由・receiptの条件は変更しません。

- 次回の収集実行から反映されます。DB schema、モデル、取得範囲、通知先、既定値、既読化・人承認・理由・receiptの条件は変更しません。破損した取得ジョブは既存のinvalid_payload分類で隔離され、再取得は既存の操作手順を使います。

- 診断は次回実行から反映されます。稼働中のLINE WORKS独立プロセスは更新後に再起動してください。DB schema、モデル、取得範囲、通知先、既定値、既読化・人承認・理由・receiptの条件は変更しません。破損したLINE処理記録は成否不明のまま保持し、自動再実行しません。

- 開発用Pythonコマンドはscripts/development/配下、文書はdocs/guides・docs/development・docs/specsの新パスを使用してください。makeの既存ターゲット、scripts/run_tests.sh、install.sh、python -m lineworks_adapterの入口はそのままです。稼働中のHermes gatewayとLINE WORKS独立アダプターには更新後の再起動が必要です。共通コードの変更時は両方の再起動案内を表示します。DB schema、モデル、取得範囲、通知先、既定値、既読化・人承認・理由・receiptの条件は変更しません。

- 追加操作・設定変更は不要です。既存の監査・公開条件、既読化・通知承認条件、抽出範囲・モデルの既定値は変更しません。

- 既存の設定・通知先・モデル・収集範囲・既読化条件は変わりません。初回はinstall.shの後にinitを実行してください。--no-servicesを指定した場合や配置差分がある場合は従来どおりservicesとcheckを実行します。実機への適用やサービス再起動はこの変更だけでは行われません。

- 追加操作やDB移行は不要です。既存の正しい理由区分と、人承認の条件は変わりません。

- 追加操作・設定変更は不要です。常駐抽出ワーカーと監視は次回起動時から修正が反映されます。既定のモデル・ポート・通知・取得範囲・既読化・人承認条件は変更しません。

- 追加の設定変更は不要です。抽出モデル・取得範囲・通知先・既読化・人承認条件・既定設定は変更しません。型が壊れた抽出項目は表示せず、原本本文は既存の経路で確認できます。

- 追加の設定変更や再抽出は不要です。保存済みの未確認フラグを表示時に使用します。フラグがない旧形式の表示と、合計6件の表示上限は維持します。

### 技術詳細

<details>
<summary>技術詳細・根拠を表示</summary>

#### 不正な患者集約からの出力で既存ファイルを失わないよう修正

- 最新の患者集約をすべて読み取り・描画してから、出力ファイルの更新と保持期限の整理を行う。
- JSONオブジェクトの読取りは既存loads_dictを再利用する。
- 根拠: mcs/ops/brain_export.py、tests/ops/test_brain_export.py

#### LINE WORKSに要約・原文・添付と本人確認付き操作を配信

- 各接続実装をadapters/slack・discord・lineworksへ整理し、既存importとLINE WORKS CLIの入口を維持します。Hermes Agent本体は変更しません。
- 公式JWT RS256認証・Bot送信・添付upload・生HTTP本文のHMAC署名検証を実装し、配送grant/journal/receiptを共通化します。本文・表示末尾・追加ボタンは封印済み配送パーツで分割し、配信前に整合と範囲を確認します。
- 編集・削除・履歴照合・スレッド指定がない公式Bot APIに合わせて更新を新規投稿し、旧ボタンを無効化します。HTTP 201は受理であり閲覧を示しません。
- 応答喪失・処理中クラッシュは不明として保持し、自動再送しません。429の後はプロセス間でも60秒の待機を共有します。Callbackは公式仕様で再送されないため、statusで不明件数を確認します。
- 導入手順とAI向け手順に、秘密値を会話やargvへ出さない入力、許可範囲設定、ローカル診断、常駐候補生成、実接続の確認範囲を記載します。
- 根拠: adapters/lineworks/、adapters/README.md、mcs/notify/notify_cards.py、mcs/notify/notify_transport.py、mcs/notify/notify_flush.py、mcs/ops/mcs_setup.py、docs/guides/LINEWORKS.md

#### LINE WORKS追加後も既存接続とテキスト通知の動作を維持

- canonical adaptersとhermes_pluginの互換入口を同一moduleへaliasし、DiscordのContextVar再送抑止とSlackの常駐worker引継ぎ状態を共有します。SDK importは関数内のまま、Hermes本体の変更や独自Slack/Discord認証はありません。
- LINE WORKSのtext send/checkは接続設定と対話設定を分けて検証します。off又は他の対話接続を使う場合も、明示した有効なLINE設定とexactdestinationが必須です。LINE Callback・操作・サービス起動はactive LINE必須を維持します。
- 変更記録チェックにadapters/とlineworks_adapter/の実行時ファイルを含め、移動先だけの変更も日本語記録なしで通しません。
- 独立daemonの待機タイムアウトはasyncio.TimeoutErrorで扱い、Python 3.10の例外別名でも通常の待機で終了しないようにします。CIの例外型許可はLINEの起動入口だけに限定します。
- 導入手順のHermes依存の説明をSlack/Discordへ限定し、独立LINE接続とoffの意味を明確にします。
- 根拠: hermes_plugin/mcs_slack/__init__.py、hermes_plugin/mcs_discord/__init__.py、adapters/lineworks/config.py、adapters/lineworks/__main__.py、scripts/development/release_notes.py、ci/gates.py、docs/dev-records/lineworks-impact-20261001.md

#### LINE WORKSの導入・操作結果・更新適用を安定化

- ソースcheckoutと設定/dataを分離し、LINE CLI既定保存先・本体診断・常駐候補を揃えます。既存認証設定はinitで上書きせず、診断・保護した退避・再入力の手順を示します。
- Slack/Discordの移動先を旧worker検知・更新影響・永続化したplugin_changedへ含めます。LINE WORKS変更は独立プロセスの診断・再起動として案内し、Hermes Agent本体は変更しません。
- 新しい閲覧には新しい応答IDを使い、処理中の明示再クリックだけ同じIDで冪等発行します。人承認の結果追跡はコマンド発行前に永続化し、現在の本人・配送範囲・route epoch・対象プロジェクトを再確認します。
- Callbackの処理開始renameをディレクトリfsyncで永続化します。設定の無効化で旧serverを終了し、20分超の未処理入力は実行前に内容を削除して不明記録を残します。
- LINEテキスト配送の添付はledgerのSHAを送信直前まで保持し、途中のファイル置換や未保管ファイルを子プロセス起動前に拒否します。曖昧な実送信は自動再送しません。
- 根拠: adapters/lineworks/、mcs/notify/notify_flush.py、mcs/ops/mcs_setup.py、mcs/ops/mcs_update.py、docs/guides/LINEWORKS.md、docs/dev-records/lineworks-review-20261001.md

#### 通知の不正ファイルとSlack履歴確認失敗を安全に処理

- 共通の結果読取りと表示ファイル走査でRecursionErrorを不正JSONとして扱います。
- Slackの本文照合・既存返信の更新・添付照合が使う履歴読取りで、通信失敗または不正応答を空の履歴へ変換しません。
- 根拠: adapters/common/paths.py、adapters/common/worker.py、adapters/slack/delivery.py

#### 長文解析の再試行で残り時間を再確認

- 形式拒否前の呼び出しは送信済みとして扱い、未送信の延期とは区別します。
- 残り399秒で追加送信を抑止し、400秒では制約なしの形式で再試行する境界を合成テストで検証します。
- 根拠: #2、mcs/semantic/semantic.py、tests/semantic/test_semantic_llm_retry.py

#### macOSで更新時の残存解析プロセスを検出

- POSIX EREの文字クラスを使用し、Pythonインタープリターとextract_llm.pyまたはsemantic_drain.pyの完全な名前を照合します。
- 同じユーザーIDへの制限・自プロセスの除外・pgrepエラー時の中止を維持します。
- 実際のpgrepと完全合成の子プロセスで対象2種と非対象5種を検証します。
- 根拠: #4、mcs/ops/mcs_update.py、tests/ops/test_mcs_update.py

#### 長文解析の拒否判定と形式切替を維持

- admission無効時のfallback経路のみが対象で、admission有効時の経路は変更しません。
- HTTP404/422は従来の再試行可能な分類を維持し、残り400秒の再送境界とHTTP400の要確認分類を合成テストで検証します。
- endpoint/modelのハッシュと拒否時刻だけを保存し、本文・患者情報・応答本文・認証情報は記録しません。
- 600秒期限・500件保持・atomic publish・非blocking lockを使い、保存時間も再送予算から差し引きます。
- 根拠: #6、#2、mcs/semantic/semantic.py、tests/semantic/test_semantic_llm_retry.py

#### 不正な抽出結果で閲覧・集計出力が停止する問題を修正

- canonical_projectionとsemantic_facts_v4の読み取り時に配列・識別子・根拠IDの形を検査する。
- 構造化表示と閲覧CLIのJSONオブジェクト読み取りは既存loads_dictに統一する。
- 深すぎるJSONを含む閲覧履歴は空のmetaまたはnullのcontentとして扱い、他の履歴を表示する。
- 根拠: mcs/views/read_model.py、mcs/views/structured_view.py、mcs/views/mcs_view.py

#### LINE WORKSの画面例と接続別の案内をREADMEへ追加

- LINE WORKSのSVG/PNG・生成元・hash記録を追加し、CIで同期と画像形式を検証します。
- 利用者ガイド・README運用規則・文書索引・送信先表・全体フローを現行接続方式へ整合します。
- 根拠: README.md、docs/screenshots/lineworks-gallery/、scripts/development/generate_lineworks_gallery.py、docs/guides/USER_GUIDE.md

#### READMEを目的別に読みやすくし、リリース時の更新を定着

- release_notes.py buildでCHANGELOGとREADMEの最新変更を同時に更新します。
- PR・main・tagで見直し記録とリンクを検査し、tagとREADMEのversion不一致を拒否します。
- 根拠: README.md、scripts/development/readme_release.py、docs/development/README_MAINTENANCE.md

#### Slackを推奨とするREADMEへ更新し、7画面の表示例を追加

- 画像は実画面のキャプチャではなく、実患者・実投稿・匿名化データを使わない説明図です。
- リリース時のREADME見直しにSlack優先と画面例のソース照合を追加し、CIで7組のSVG・PNG・ハッシュ記録を検査します。
- 根拠: README.md、scripts/development/generate_slack_gallery.py、docs/screenshots/slack-gallery/README.md

#### 一覧取得に失敗したcronジョブの削除を防止

- mcs_updateと独立復旧ツールの両方で、cron listが成功した場合だけジョブ除去の候補を判定する。
- 一覧取得の失敗報告、復元承認の待機条件、独立復旧ツールの世代隔離を維持する。
- 根拠: deployment/recovery/mcs_recover.py、mcs/ops/mcs_update.py

#### 特殊文字を含むDBパスの更新・復旧判定を修正

- SQLiteの読み取り用URIを実ファイルのパスからエンコードし、URI区切り文字やエスケープ文字が参照先を変えないようにする。
- 更新時のスキーマ事前確認、承認キュー、復旧時のスキーマ・損失報告・復元承認の参照先を一致させる。
- 独立復旧ツールの世代隔離、読み取り専用モード、復元承認と待機中の停止条件を維持する。
- 根拠: deployment/recovery/mcs_recover.py、mcs/ops/mcs_update.py

#### リリース本文の同期で下書きのタグを保持し、失敗を検出

- 競合確認済みのtag_nameをPATCHへ同じ値で含め、応答と再取得の両方でReleaseの識別・公開状態を検査します。
- workflowのteeを除去し、同期コマンドの失敗を成功として扱わないようにしました。
- 根拠: scripts/development/sync_release_notes.py、.github/workflows/release-notes.yml、tests/release/test_sync_release_notes.py、docs/development/RELEASE_NOTES.md

#### 所見の短縮表示でも否定・推測・家族・予定の条件を保持

- canonical所見の対象・極性・確度・状態・時点等を短縮した本文より前へ表示し、保存形式と公開条件を変えずに資格情報を保持します。
- 根拠: mcs/views/structured_view.py、tests/views/test_structured_view.py

#### 取得ジョブとAPI応答の異常JSONによる収集停止を修正

- 取得ジョブの4解析箇所とLedger.job_payloadに既存loads_dictを再利用し、深いJSON・巨大整数を既存の失敗分類へ戻して後続ジョブの処理を続けます。
- HTTP200応答の不正UTF8・深いJSON・巨大整数を固定文言のSchemaErrorへ分類し、患者単位で未完了を記録して既読化を抑止します。既存の非JSONログイン復旧は維持します。
- 根拠: mcs/core/ledger.py、mcs/ingest/job_ops.py、mcs/ingest/mcs_adapter.py

#### 診断DBパスとLINE WORKSの破損処理記録を安全に処理

- 診断用SQLite URIを既存Path.resolve().as_uri方式へ統一し、#・?・%を含むパスでも正しいDBを読取り専用で開きます。
- LINE Callbackの完了記録を容量制限付きで読み、不正形式・読取り失敗をunknownとして保管と状態表示に共通利用します。後続入力を止めず、同じCallbackを再実行しません。
- 安全文書・AI導入手順・文書索引・生成Wikiの正本briefを3接続先の構成に整合させます。生成Wiki本文、Hermes Agent本体、実データ・配備は変更しません。
- 根拠: mcs/ops/mcs_setup.py、adapters/lineworks/server.py、adapters/lineworks/__main__.py、docs/guides/SETUP_AGENT.md、SECURITY.md

#### 接続共通コード・開発ツール・文書・テストの配置を整理

- transport中立の配送7モジュールをadapters/commonへ移し、hermes_plugin.mcs_deliveryの互換importは同一moduleと共有状態を維持します。
- 共通コードの変更をgatewayの診断・更新時の永続restart判定とLINE WORKSの更新影響案内へ含めます。
- 開発ツール6本とCCO候補・承認記録・説明3本を責務別に移動し、既存CCO記録の内容と適用状態を保持します。
- 文書13本と接続関連テスト18本を整理し、generator・CI・integration・相対リンク・import探索を新配置へ追従させます。
- 過去の検証hash台帳・Release原本・生成OpenWiki本文と実データを変更しません。
- 根拠: adapters/common/、hermes_plugin/mcs_delivery/__init__.py、mcs/ops/mcs_setup.py、mcs/ops/mcs_update.py、scripts/development/、tests/adapters/、docs/README.md、deployment/cco/

#### 意味解析の未送信修復と壊れた保存記録への耐性を改善

- 送信前と証明できる停止時だけ要約修復の予約を解除し、実行済み・成否不明の予約を維持する。
- 状態表示の集計は共通JSONオブジェクト読取を使い、配列やスカラーのmetaを未判定として扱う。
- 保存ページのfact ID型を読取境界で検証し、壊れた段階を未観測として保持する。
- 根拠: mcs/semantic/semantic.py、mcs/semantic/semantic_drain.py、mcs/semantic/semantic_lifecycle.py

#### 初回導入の案内を設定と最終確認まで一本化

- 導入診断のローカルLLM通信を既存の上限付きHTTP処理に統一し、proxy・redirectを許可しない。
- 壊れたサービスmanifestを共通JSON loaderで扱い、UTF-8不正や深いJSONで診断・再同期が停止しない。
- gateway同期に失敗したinitは、後続チェックが成功しても非0で終了する。
- Path Bで既存plistを標準ライブラリで描画する合成検証を追加し、slot 0/2とowner-only権限を保持する。
- 根拠: install.sh、mcs/ops/mcs_setup.py、docs/guides/INSTALLATION.md、docs/guides/SETUP_AGENT.md

#### 不正な却下理由の記録でアラート一覧が停止する問題を修正

- 人承認コマンドと集計で同じDISMISS_REASON_CODESを参照する。
- 根拠: mcs/ops/mcs_signals.py

#### 監視とローカル解析が不正な入力で停止する問題を修正

- JSON辞書の読取りと形式確認に既存の共有処理を再利用する。
- 送信前に見送ったRT形式確認の待機permitだけを解放し、送信済み処理の不確実性は保持する。
- 30,000件を超える抽出対象はSQLite json_eachでSQL内絞込みを保ち、LIMIT後の絞込みを廃止する。
- 根拠: mcs/core/mcs_util.py、mcs/core/local_llm.py、mcs/ingest/health_watch.py、mcs/extract/v4/extract_llm.py

#### 型の壊れた抽出結果でも構造化表示を継続

- 構造化表示の配列反復と区分辞書参照を共通の型検証に統一します。
- 未指定または正しい空配列の場合のルール補完は維持し、型不正や読めない選択済み項目は除外契約を維持して補完しません。
- 根拠: mcs/views/structured_view.py、tests/views/test_structured_view.py

#### 未確認の検査値を候補として表示

- 依頼や薬剤と共通の未確認判定を検査表示にも使用し、フラグがfalse以外の値なら候補として扱います。
- 患者集約は検査項目のフラグを保持しており、保存形式・抽出処理・数値や根拠の再検証は変更しません。
- 根拠: mcs/views/structured_view.py、tests/views/test_structured_view.py、tests/extract/test_rollup_canonical.py

</details>

## [1.0.8] — 2026-10-01

**依頼の期限・条件と返信状況を見やすくし、患者サマリーを連携**

通知カードで「依頼」「確認」「予定」を区別し、返信から進捗・完了・取消の状態を保持します。MCSの連携サマリー取得と、収集・解析・通知の安定化も加えました。

> 更新前の確認：更新後に`hermes gateway restart`と`mcs_setup.py services`が必要です。実行パスはその版の導入ガイドを確認してください。

### 新機能

- **依頼と返信の情報を拡充**
  依頼の種類・条件・期限表現・依頼者を本文の完全一致引用に基づいて保持します。返信を承知・対応意思・進捗・回答・完了・取消に分類し、スレッド単位の状態と矛盾を記録します。

- **MCSの連携サマリーを取得**
  新着投稿を保存した患者を対象に、通知後に最大12件／実行で読み取り専用取得します。患者サマリー・日次ダイジェスト・抽出の参照に利用し、取得時に投稿・既読化は行いません。対象投稿より新しいサマリーは抽出に使いません。

- **新着返信をすべて通知する設定**
  `notify_all_replies`を有効にすると、リアルタイム経路で保存した新しい返信を未読・経過時間の条件によらず通知します。既定オフで、一括履歴取り込みと`--jobs-only`は対象外です。

- **送信保留を確認**
  送信失敗が続く通知を上限到達で保留し、無限再試行を止めます。日次ダイジェストの「送信保留」で確認できます。

- **失敗した解析を定期的に再試行**
  再試行上限に達した抽出・意味解析ジョブを、6時間ごとの定期処理で上限付きに復帰させます。

- **解析の運用状況を計測**
  `semantic_observe.py --days`で対象期間を指定できます。依頼の条件・期限・返信を評価対象に加え、ベンチに混入していたリークケースを除外しました。

### 改善

- **新着処理と過去履歴の解析を分離**
  LLMを新着・対話用1枠と過去履歴用2枠に分けます。新着が混み合うと過去履歴側の1枠が処理を譲ります。

- **長い出力や形式不正への対処を強化**
  解析の呼び出し回数・エラー予算・再試行に上限を設け、JSON形式を要求します。生成の打ち切りやJevの失敗を診断可能な形で記録します。

- **依頼とサマリーの表示を調整**
  条件を最大60字で表示し、確認・予定を未解決依頼と区別します。連携サマリーは最新の取得結果を読み、実際に内容が変わった場合だけダイジェストの更新件数に数えます。

### 不具合修正

- **返信の追加投稿を抑制**
  返信は1投稿ずつスレッドへ送り、過去履歴経由で後から届いた返信は既存の投稿を更新します。

- **収集の欠測・途中配備への対処**
  取得状況の確認、既読処理後の再確認、上限到達や連携サマリー由来の停止状態、4xx応答、配備途中のコード変更の扱いを修正しました。

- **ログ肥大化・メモリ増加・再試行の衝突を修正**
  現行ログをローテーション対象にし、常駐処理のメモを呼び出しごとにリセットします。失敗ジョブの復帰が処理中のdrainerと衝突して誤報する問題も修正しました。

### 動作・設定の変更

- **収集は昼夜とも5分間隔**
  旧運用の夜間20分間隔と、夜間だけ解析レーンを保持する機構は廃止されています。

- **LLMの同時処理枠は3枠**
  2枠から3枠へ変更し、1枠あたりのコンテキストは16,384です。

- **解析キャッシュの識別を変更**
  モデルとプロンプト版をキャッシュキーに含め、モデル変更後に古い中間結果を誤用しないようにします。手編集された非整数の長出力設定は無視します。

### 更新時の注意

- 更新後に`hermes gateway restart`と`mcs_setup.py services`が必要です。実行パスはその版の導入ガイドを確認してください。

- `notify_all_replies`は既定オフです。すべての新着返信通知を利用する場合に有効化してください。

- 返信から抽出した「完了」と、人がタスク操作で確認した完了は区別してください。連携サマリー取得の失敗では投稿収集を止めません。

### 技術詳細

<details>
<summary>内部仕様・根拠・公開当時の詳細を表示</summary>

ローカル LLM のスロット再構成と構造化抽出項目の拡張、MCS 連携サマリーの
読み取り専用取り込み、スレッド配送の精度向上、そして収集・解析・通知の
各層で見つかった不具合の修正です。

配備時の注意: `deployment/` と `hermes_plugin/` の変更を含むため、
配備後に `hermes gateway restart` と `mcs_setup.py services` が必要です。
llama-server の同時スロット数が 2 → 3（`-np 3`、1枠あたりコンテキスト
16384）に変わっています。

#### 追加

- **構造化抽出の依頼・返信項目（#20）** — 依頼 `requests` に
  `kind`（依頼 / 確認 / 自分の予定）・`condition`（発動条件）・
  `due_text`（本文の期限表現そのまま）・`from`（依頼者）を追加。
  いずれも対象本文の完全一致引用で根拠付けし、引用できない値は
  採用しません。スレッドへの返信は `reply` で分類
  （承知 / 対応意思 / 進捗報告 / 回答 / 完了報告 / 取消）し、
  rollup の `recent_requests` にスレッド単位の `reply_state`
  （進捗・完了・取消の最新状態と矛盾フラグ）を持ちます。
  カードの `依頼:` 行は「予定:」「確認依頼:」の接頭辞と
  `(期限:…)` `(条件:…)` を表示し、確認・予定を未解決依頼の
  集計に混ぜません
- **連携サマリー（#21）** — MCS の患者連携サマリーを読み取り専用で
  取得します。新着投稿を保存した患者だけを対象に、通知の後・1回の
  実行あたり最大12件まで取得（POST・既読化は一切しない）。失敗は
  実行結果に記録するだけで収集自体は止めません。内容は
  `karte_summary` artifact として保持し、`🧾 患者サマリー`の行、
  日次ダイジェストの更新件数、extract_llm の参照コンテキスト
  （対象投稿より新しいサマリーは使わない）で利用します
- **リアルタイム経路の返信通知**（`notify_all_replies`、既定オフ）—
  オンにすると、リアルタイム経路で新しく保存した返信を未読・経過
  時間のゲートなしですべて通知します（一括履歴取込・`--jobs-only`
  は対象外。通知済みの行は二度送りません）。カード配送は返信ごとに
  1投稿ずつスレッドへ送り、バックログ経由で後から到着した返信は
  既存の返信投稿を書き換えます（投稿を増やしません）
- **送信エラーが続くカードの隔離** — 配送が繰り返し失敗する送信要求は
  試行回数の上限で保留（`outbox_hold`）にし、無限リトライを止めます。
  保留分は日次ダイジェストの「送信保留」行で確認できます
- **失敗ジョブの夜間 bounded retry** — 再試行回数を使い切った抽出・
  意味解析ジョブを、6時間ごとの cron（`mcs_llm_catchup.sh`）で
  上限付きに復帰させます
- **意味解析の運用計測** — `semantic_observe.py --days` で期間を
  絞った観測と、補間パーセンタイル `llm_s_p90`（容量ゲート用）を
  追加。bench は依頼の `kind`/`condition`/`due_text` とスレッド返信を
  採点対象にし、混入していたリーク cases を除去しました

#### 修正・改善

- **LLM スロットの再構成** — llama-server を固定3枠化し、新着・対話を
  専用のリアルタイム枠（slot 1）、2枠を24時間常駐のバックログ
  drainer（slot 0 / slot 2）に分離。slot 2 の backlog lane は
  リアルタイム要求が混むと一時停止して譲ります（elastic）。
  旧方式の夜間レーン保持（lane hold）機構は撤廃しました
- **意味解析の堅牢化** — 1ジョブの多パス実行に上限を設け、出力を
  JSON 制約付きで要求、Jev 呼出しの失敗を診断可能な形で記録。
  `max_tokens` 打ち切り（生成暴走）は反復制御つきで1回だけ再試行、
  length stop の再試行は長い天井へ段階的に上げます。本文単位の
  エラー予算・QC 呼出しの共有・形式不正リトライの上限も追加
- **収集の境界条件** — F-5（取得欠測）のフォールバック coverage と
  ack 後再確認、cap 済み・karte 由来の wedge 処理、4xx 応答のみの
  経路 A、配備途中の `code_changed` ガードを整備
- **表示の整合** — 依頼の条件は保持した条件をそのまま表示
  （最大60字）、確認依頼も薬剤師宛依頼として集計、ダイジェストは
  サマリーが実際に変わったときだけ更新件数に数えます。`🧾` の
  連携サマリー行は rollup を待たず最新 artifact を読みます
- **運用まわり** — 退役した `drainer-rt` 名ではなく現行
  `extract_drain_2.log` をログ回転の対象に修正（無回転で肥大化して
  いた）。`--revive-failed` の単独実行は run lock を取らないため、
  定期 retry が実行中の drain と衝突しても誤って失敗報告しません。
  常駐 drainer の batch パスが呼出しごとの整合メモを蓄積していた
  のをリセットするよう修正（長期稼働でのメモリ増加を抑止）。
  2026-09-30 の adversarial review で挙がった欠陥5件を修正

#### 動作が変わるもの

- **収集は常時5分間隔** — 夜間間引きはありません（旧運用の
  「夜間20分」記述は廃止済みの機構です）
- **チャンクキャッシュはモデル・プロンプト単位** — 意味解析の中間
  キャッシュがモデル変更をまたいで誤用されないよう、キーにモデルと
  プロンプト版を含めます
- **非整数の長出力 mark は無視** — `semantic_runaway.json` など
  手編集された値が整数でない場合、起動時に落ちず無視します

全差分：[v1.0.7 → v1.0.8](https://github.com/yusuketakuma/hermes-mcs/compare/v1.0.7...v1.0.8)

</details>

## [1.0.7] — 2026-09-29

**通知の重複を解消し、カード操作・タスク管理・導入を改善**

Discord／Slackの本文・添付の二重投稿を修正しました。通知カードからの確認・担当・タスク操作、朝の日次ダイジェスト、導入前チェックを追加しています。

> 更新前の確認：`hermes gateway restart`と`mcs_setup.py services`が必要です。再起動までは新しいカード操作・形式が反映されません。

### 新機能

- **通知カードからタスクを管理**
  確認・担当の切り替え、タスク作成・完了、患者サマリー、抽出の誤り報告、MCSへのリンクを利用できます。未完了タスクと期限切れもカードに表示します。

- **自分のタスク・未確認一覧・患者内検索**
  操作した本人だけに一覧を表示します。未確認一覧は直近7日の確認記録を参照し、作業完了とは区別します。検索は取得済み投稿のみが対象で、project範囲外は表示しません。

- **タスクの期限リマインド**
  有効なカードに紐づく未完了タスクの期限当日と期限切れ後に各1回通知します。8〜21時、最大3件／実行で、`notify.interactive`がオフなら送信しません。初回有効化時の既存期限切れは通知しません。

- **朝の日次ダイジェスト**
  新着・取得未完了・確認候補・未完了／期限切れタスクの件数を1日1回送ります。既定オフで本文は含めず、患者名は`daily_digest.include_names: true`のときだけ追加します。

- **抽出した緊急度の出所を表示**
  AI抽出の高緊急度と、機械照合で緊急語を含む場合を分けて表示します。機械照合は否定表現にも反応し得ます。

- **導入前チェックと計画表示**
  `install.sh --preflight`は前提条件と直し方を確認し、`--dry-run`は変更予定も表示します。どちらも書き込みません。`mcs_setup.py doctor`で実行環境とサービス状態を診断できます。

- **認可・担当者選択・報告を拡充**
  Discordロールでカード操作を許可でき、自局スタッフ一覧から担当者を選べます。候補の却下理由を選択式にし、誤り報告と却下理由を一覧・集計できます。

### 改善

- **Slackのカードをモバイルで操作しやすく**
  主要ボタンを「確認」「担当」に絞り、その他の操作を選択メニューへまとめます。MCSリンクは文字リンクです。Discordの配置は変わりません。

- **確認者・担当者を名前で表示**
  フッターでメンション通知を鳴らさず、名前を表示します。Slackで名前を取得できない場合は「メンバー」と表示します。

- **中断した導入を再実行可能に**
  失敗した段階で停止し、再実行でclone・環境構築・モデルダウンロードを続行します。`--force-repo`で導入元checkoutを明示的に切り替えられます。

- **診断と処理の無駄を削減**
  収集の遅れと停止を区別し、ディスク容量・サービス・cronのずれを確認します。不要な再生成・取得・起動時走査を減らし、利用者向け説明をUSER_GUIDEへ分離しました。

### 不具合修正

- **本文と添付の二重送信を修正**
  抽出結果が後から届いても既存本文を更新し、同じ添付は既存投稿を再利用します。配信直後の更新競合にも対処しました。

- **正常な収集への異常通知を修正**
  大きい患者の履歴確認待ちを常時エラー扱いせず、停止と区別します。5分ごとの同じhealth通知を抑制しました。

- **ディスク不足とLLM異常終了への対処**
  添付取得の空き容量ガードとバックアップ省略を加え、処理中スロットの指定に起因するllama-serverの異常終了に対処しました。

- **設定・サービス導入の失敗を明示**
  破損config、実行用Python不足、root実行、不正引数、launchd再読み込み失敗の扱いを修正しました。

### 動作・設定の変更

- **確認は現在表示中の内容への確認**
  新しい返信などで同じページの内容が変わると未確認へ戻ります。確認取消と担当解除の操作も記録します。

- **カードの一部ボタンを変更**
  `⏸ 保留`と常時表示の`📋 タスク`を廃止し、`📝 依頼作成`を`📝 タスク作成`へ改名しました。古いカードには互換対応を残します。

- **導入は失敗時停止・root実行禁止**
  新規作成ファイルは所有者のみ読み書き可能にします。任意の`MCS_MODEL_SHA256`でモデル取得後のチェックサム照合ができます。

### 更新時の注意

- `hermes gateway restart`と`mcs_setup.py services`が必要です。再起動までは新しいカード操作・形式が反映されません。

- 導入はservices用venvのPythonを使用してください。`mcs_setup.py check`が必要条件と修正する順番を表示します。

- 日次ダイジェストは既定オフです。患者名の掲載も別設定で既定オフです。

### 技術詳細

<details>
<summary>内部仕様・根拠・公開当時の詳細を表示</summary>

v1.0.6 以降の運用で見つかった不具合の修正と、カード操作・導入まわりの
全面的な改善です。Discord/Slack のスレッド本文・添付の二重投稿の解消、
収集 health の誤判定・ディスク・ローカル LLM（llama-server）の異常終了への
対策、1.0.5 以降のコードのリファクタリングと処理の無駄の削減、
`install.sh` / `mcs_setup` の不具合修正と事前チェック、README の分割、
そして通知カードのボタン（確認・担当の切替、タスク作成・完了、患者サマリー、
抽出の誤り報告、自分のタスク・未確認一覧・患者内検索など）と
朝の日次ダイジェストを加えました。`hermes_plugin/` と
`deployment/` の変更を含むため、配備後に `hermes gateway restart` と
`mcs_setup.py services` が必要です。

#### 修正・改善（運用で見つかったもの）

- **スレッド本文の二重投稿** — 抽出結果が後から届いてチャンクの文面が変わると
  2通目を投稿していた。前回の投稿 id（`prior_remote_id`）を計画に載せ、
  Discord は edit・Slack は `chat.update` で書き換える。カード配信直後に
  更新が発行される競合も、本文パートの配信確定まで更新を待たせて解消
- **添付の二重送信** — 更新のたびに全添付を再アップロードしていた。
  sha256 が同じ添付は既存の投稿を再利用する
- **収集 health の常時 degraded** — 大きい患者の coverage 認証待ち（lag）を
  エラー扱いしていた。lag は情報表示（`coverage_lagging`）にし、
  history_head の停止だけを異常とする。health watch の5分毎の再通知を止めた
- **ディスク** — 添付ダウンロードに空き容量ガード（ENOSPC で attempts を
  消費しない）、空き不足時は日次バックアップを省略、health に空き容量。
  MCS 用 Chrome の端末内 AI モデル取得を無効化
- **llama-server の異常終了** — 処理中の slot への id_slot 指定が
  llama.cpp の prompt cache 差し替えで `GGML_ASSERT` を起こしていた。
  lend-rt drainer は処理中の slot を指定しない。同梱テンプレートは
  `--cache-ram 0`
- **抽出の無駄** — バッチ抽出（採用率46%）を既定オフ、書込みロック待ちで
  捨てていた結果を同じ run 内で書き直す、tick の抽出段は slot が埋まって
  いればスキップ
- **signals 通知オフ時の digest カードの宙づり** を解消、registry の
  スレッド間競合をロックで直列化、配備済み cron wrapper のずれを
  `mcs_setup.py check` で検出
- **1.0.5 以降のリファクタリングと無駄の削減** — Discord/Slack の重複処理の
  共通化、rollup の不要な再生成・self_profile の毎回取得・Ledger 起動時の
  全走査の削除など（7領域・64項目）
- **README の分割** — README を概要に絞り（708→約120行）、利用者向けの
  詳細を `docs/USER_GUIDE.md` に移した

- **Slack カードの操作をコンパクトに** — モバイルで各ボタンが横幅いっぱいに並ぶため、
  Slack ではボタンを「確認」「担当」の2つに絞り、ほかの操作は1つの選択メニュー
  （`mcs:menu`、値は同じトークン）に、MCS リンクは文字リンクにした。Discord は変更なし。

新規導入を専門知識なしで進められるよう、`install.sh` に読取り専用の
事前チェックと計画表示を加え、途中で止まった導入が再実行で収束する
ようにした。`mcs_setup` は定期ジョブの実行基盤まで検証し、直す順番を
示す。導入文書（INSTALLATION・SETUP_AGENT・README）を実装に合わせて
更新した。

#### 追加

- **`./install.sh --preflight`（別名 `--check-only`）** — 前提条件を
  読取り専用で確認し `OK`/`WARN`/`NG` と直し方（`fix:`）を表示。
  NG があれば exit 1。何も書き込まない
- **`./install.sh --dry-run`** — preflight に加え、各ステージが作成・
  変更するものを表示。何も書き込まない
- **`./install.sh --force-repo`** — 別の checkout から導入済みの環境
  （plugin symlink・`~/.mcs-recovery/repo_path`・services）をこの
  checkout に切り替える。指定しなければ停止する
- **`mcs_setup.py doctor`** — インタプリタ・`hermes` の解決先（launchd
  PATH 含む）・repo・各 launchd agent のロード状態を表示してから
  `check` を実行
- **`MCS_MODEL_SHA256`**（任意）— 設定するとモデル DL 後に sha256 を照合
- 導入完了時に `Installed. Summary:` と、次に実行するコマンド（venv
  インタプリタのフルパス付き）を表示
- **通知カードのボタン（Discord / Slack 共通）** —
  `☐ 確認`⇄`✅ 確認済み`・`👤 担当する`⇄`👤 担当中` を押し直しで取消・
  担当解除（別の人が押すと担当を引き継ぎ）。フッターの確認者・担当者は
  名前表示（通知は鳴らない）。`📝 タスク作成`（タスク内容・担当者・期限・任意の理由 — 空なら
  「通知カードからタスク作成」を記録し、確認画面に必ず表示。
  抽出済みの依頼を下書きに、MCS の自局スタッフ一覧から担当者を選択）、
  未完了タスクがあるときだけ `☑ タスク完了`、`🧾 患者サマリー`（暫定集約・
  履歴の取得状況つき）、`⚠ 抽出の誤りを報告`（その投稿の抽出を1回だけ
  再実行）、`🔗 MCSで開く`。カードのフッターに未完了タスク（3件まで）と
  `⚠ 期限切れ`・`⚠ 誤り報告あり` を表示
- **期限リマインド** — 有効なスレッドカードの投稿に紐づく未完了タスク
  （`📝` 作成・`/mcs` 登録の両方）の期限当日と期限切れ後に各1回、
  `notify_target` へ患者名・タスク内容・担当者名を含むテキストを投稿
  （8〜21時、1回の実行で最大3件、`notify.interactive` がオフなら送らない）。
  期限を変更すると新しい期限で再度リマインドする。初回有効化時に既に期限
  切れのタスクは送らずに記録だけする（baseline）
- **緊急度の表示** — `📋 構造化` に `緊急度: 高（AI抽出）` /
  `緊急語を含む（機械照合）` を出所付きで表示（ルール照合は否定表現にも
  反応しうる）。緊急語を含む投稿の既存カードは1回だけ更新される（通知なし）
- **Discord のロールでカード操作を許可** — plugin 設定
  `allowed_role_ids` と `mcs_setup.py init --plugin-role-ids`
- **自局スタッフ一覧の取得** — `/users/self` の所属局について
  `GET /stations/{id}/staffs` を自己プロフィールと同じ頻度で読み、
  `station_staff_v1` artifact に保存（変化時のみ追記。失敗は run 結果の
  `station_staff: "failed: <型>"` に残すだけで、errors・health には
  影響しない）
- **カードの4行目（view・押した本人にだけ表示）** — `📋 自分のタスク`
  （押した人の表示名が担当者欄と一致する未完了タスクを全患者分、期限切れ
  を先頭に）、`🗂 未確認一覧`（直近7日に更新され今の内容に確認が付いて
  いないカードを患者ごと・古い順に。担当中で未確認のものに印、MCS と
  カードへのリンク、15件超は「他N件」。確認記録の有無であり作業の完了
  ではない）、`🔎 この患者を検索`（キーワードで取得済み投稿を検索し、
  日時・職種・抜粋を10件まで。未取得の範囲は検索されないと明記）。
  plugin の project 範囲外の項目は表示しない。部品数が Discord の上限に
  近いカードでは入る分だけ表示
- **朝の日次ダイジェスト**（`daily_digest: {enabled, hour_jst}`、既定オフ）—
  1日1回、新着件数（職種別）・緊急度が高い投稿の ID・取得未完了の
  ルームと理由コード（0件でも表示）・open（未解決）の確認候補の型別件数
  （依頼・期限系を除く。`signals.notify` 時のみ）・未完了/期限切れタスク数を
  `notify_target` にテキストで送る。本文は含めない。患者名は
  `daily_digest.include_names: true` のときだけ一覧の ID に添える（既定オフ）。
  `mcs_setup.py init` の設定項目に追加
- **却下理由の選択式化** — `🚫 却下` は理由（誤検知 / 対応済み / 重複 /
  対象外 / その他）を選び、任意でメモ。`ops.signal_dismiss` に任意の
  `reason_code` を追加し dismissed 行に `dismiss_reason_code` として保存
  （未指定の旧コマンドもそのまま受理）。型別・理由コード別の件数は
  `mcs_view.py signals` の `dismissals`、⚠ 報告の一覧は
  `mcs_view.py qc --project N` の `extract_feedback`

#### 動作が変わるもの

- **`⏸ 保留` ボタンと常時表示の `📋 タスク` ボタンを廃止**。既に投稿
  済みのカードの `⏸ 保留` は「廃止」の案内を返してカードを最新の表示に
  更新する（保留中の状態は従来どおり自動で解除される）。旧 `📋 タスク`
  は一覧を返す。`📝 依頼作成` は `📝 タスク作成` に改名
- **`✅ 確認` の意味** — 今表示している内容（同じ世代・同じページ）に
  対する確認として数え、新しい返信などで内容が変わると未確認に戻る。
  取消した確認は `notification_acknowledgements.withdrawn_at` に残る
  （列追加は起動時の冪等 migration）
- **Slack のカード操作で許可外のユーザーが押すと「権限がありません。」を
  本人に返す**（従来は無応答）
- **Slack カードのフッターはメンション構文を使わない** — 確認者・担当者は
  `users.info` の表示名（`users:read`。取得できなければ「メンバー」）の
  文字列で、再投稿でも通知は鳴らない。📋 の照合と 📝 の担当者既定値も
  同じ表示名を使う
- **plugin の更新後は `hermes gateway restart` が必要** — project を持つ
  thread/signal カードはほぼすべて `🔗 MCSで開く`（リンクボタン）や人名表示を
  含むため、再起動まで旧 worker に保留される（再起動後に配送）。確認者・
  担当者のいる既存カードはフッター形式の変更で1回だけ再描画される（通知なし）。
  4行目のボタン（📋 / 🗂 / 🔎）は再起動まで正しく動かない（旧 worker は
  一覧を表示できない）。却下フォームは旧 worker では従来の自由記述のまま
- **cmd_int の notification envelope に任意の `input`**（`name` / `query` /
  `projects`）
  を追加。runner 側の検証は `bad_input` で拒否する
- **install.sh はステージ失敗で停止する** — brew 以外も含め全ステージが
  失敗時に非 0 で止まり、後続ステージを実行しない。再実行すると中断した
  clone/checkout・venv・pip install・モデル DL（`.part` から再開）が
  続きから収束する
- **install.sh は root / sudo 実行を拒否**、`-` で始まる未知の引数は
  exit 2
- **`hermes` が PATH にあっても `~/.hermes/hermes-agent/venv` を作る** —
  services が全ジョブをこのインタプリタで起動するため
- 新規作成ファイルは所有者のみ読み書き可（`umask 077`）
- llama-server plist の内容変更を検出し、サーバ応答中は再読込を保留して
  コマンドを案内、無応答なら再読込する
- **`mcs_setup.py check` の検証追加** — services 用インタプリタ
  （`~/.hermes/hermes-agent/venv/bin/python`）、launchd PATH での
  `hermes` 解決、復旧 watchdog `org.mcs.recovery`（導入・repo 版との差分・
  `repo_path`・ロード状態）、llama-server agent のロード状態。末尾に
  `blockers (N) — fix in this order:` を表示
- **`mcs_setup.py init` は壊れた `config.json` で停止** — `--yes` のときだけ
  `config.json.corrupt-<日時>` へ退避して既定値から続行
- **`mcs_setup.py services` は services 用インタプリタが無ければ何も
  描画せず exit 1**
- `init --help` に非対話実行の例を表示
- `llamacpp_restart_if_idle.sh` は llama-server の launchd agent が
  ロードされていなければ skip して exit 0、`kickstart` 失敗時は非 0

#### 文書

- INSTALLATION.md 冒頭に「最短手順」、§7 に preflight / install.sh /
  check のメッセージ別対処表。Path B の `python3` を `python3.13` に、
  Path A の後続コマンドを venv インタプリタに統一（新規 Mac の
  `python3` は 3.9 系で `mcs_setup` が動かない）。checkout の移動と
  `~/.mcs-recovery/repo_path` を追記（§A-7）
- ジョブ一覧表を `deployment/launchagents/README.md` に一本化

全差分：[v1.0.6 → v1.0.7](https://github.com/yusuketakuma/hermes-mcs/compare/v1.0.6...v1.0.7)

</details>

## [1.0.6] — 2026-09-29

**未確認情報の扱いを統一し、更新・復旧・配送を安定化**

未確認の薬・症状・依頼を確認済みとして扱う問題を修正しました。Discord／Slackの操作判定、通知の配送、更新途中の復旧と復元同意待ちの維持を強化しています。

> 更新前の確認：`hermes gateway restart`が必要です。

### 改善

- **確定・取消の操作判定を統一**
  Discord／Slackで本人の認可と期限を同じ仕組みで確認します。操作範囲外になった下書きも、本人による取消は可能です。Slackの拒否理由は本人だけへ表示します。

- **サービス起動を実状態で確認**
  起動コマンドの成功だけでなくlaunchdへの登録状態を確認します。個々の操作と再起動全体に待ち時間の上限を設けます。

- **同じ障害通知を繰り返さない**
  状態変化や6時間経過で再通知し、正常状態が続く間は通知しません。回復時は1回通知します。

- **配送と内部処理を効率化**
  配送記録を差分読みへ変更し、共通処理とテスト用ヘルパーを整理しました。

### 不具合修正

- **未確認情報が確認済みに混ざる問題**
  未確認フラグの欠落または文字どおりのfalseのみを確認済み扱いに統一します。`0`・`null`・文字列などを誤って確認済みの根拠にしません。

- **復元同意待ちが失われる問題**
  gitや実行状態が不明でも復元の同意待ちを維持します。通常の復旧で同意を無効化せず、他の更新処理の記録を勝手に巻き戻しません。

- **配送の競合と古い表示を修正**
  配送の巻き戻り・拒否後の再発行・世代違いの表示・通知オフ時の処理を修正し、古いworker向けの未対応形式を保留します。

- **意味解析と訪問・検査値の誤読を修正**
  古い解析結果や破損JSONの扱いを修正しました。酸素流量・不整脈の誤読を防ぎ、完了訪問の報告を調整します。

- **収集と更新・バックアップの失敗処理**
  再ログイン予算、403応答、収集ロック競合を整理しました。更新の期限・再実行・保存の確実性、プロセス確認失敗の扱いを修正します。

### 動作・設定の変更

- **解析の確認状態は最新の監査だけを見る**
  古い合格結果まで遡って確認済みとしません。正本候補への昇格には既存のG6評価基準を適用します。

- **抽出ルールと評価形式を更新**
  抽出ルール版を6へ更新し、評価schemaを`semantic-evaluation/v3`へ変更します。通常の経路で再抽出され、rollupの再計算版は変わりません。

### 更新時の注意

- `hermes gateway restart`が必要です。

- 独立watchdogの再配置が必要です。checkoutで`./install.sh --no-brew --no-llm --no-plugin --no-services`を実行してください。自動更新では`~/.mcs-recovery`を更新しません。

- 旧watchdogは`mcs_recover.py.prev`へ退避されます。

### 技術詳細

<details>
<summary>内部仕様・根拠・公開当時の詳細を表示</summary>

v1.0.5 の独立レビュー指摘への修正（3 wave）と、その後の全体
リファクタリング・未解決事項の解消。未確認フラグの fail-closed
統一、Discord/Slack の確定・取消規則の一本化、配送 journal の
差分読み、そして更新・復旧経路（mcs_update / 独立 watchdog）が
launchctl・git・pgrep の異常や restore 同意待ちの最中でも
drainer を止めたまま放置せず、同意を無効化しないよう強化した
（v1.0.5 から 109 コミット・199 ファイル）。`hermes_plugin/` と
`deployment/recovery/` に変更があるため、gateway 再起動と
watchdog の再配置が必要。

#### 動作が変わるもの

- **未確認フラグ（`unverified`）を fail-closed に統一** — 依頼・
  症状・薬の各項目は、フラグが欠落または literal false の時だけ
  確認済みとして扱う。従来は signals の SQL が JSON true だけを
  除外し、症状・薬の判定は truthiness だったため、`0`・`null`・
  `"true"` などが確認済みの根拠として候補化されていた
  （`ITEM_CONFIRMED_SQL` / `item_unverified`）。未確認の依頼は
  rollup・brain export・表示で確認済みと混ざらない
- **確定・取消の判定を registry に一本化** — Discord と Slack が
  `Registry.take_confirm` で一度に判定する。本人の認可は両ボタン
  で確認し、project scope は「確定」だけを制限（scope 外になった
  preview も本人は取り消せる）。確認の TTL が lookup と take の間
  で切れた場合に、古い payload のままコマンドを投入していた
  問題も解消
- **Slack の拒否応答を Discord と同じ文言に** — 期限切れ・他人の
  クリック・チャンネル不一致・カード token 消失・scope 外の確定に、
  クリックした本人だけへ ephemeral で返す（未検証の送信元は
  従来どおり無応答）
- **launchd bootstrap は `launchctl print` で検証して成功とする** —
  setup・update・独立 watchdog・install.sh の全経路で、exit 0
  だけを成功扱いにしない（`mcs_util.launchd_bootstrap`）
- **更新経路の launchctl 待ち時間に上限** — 1 呼出し 10 秒
  （`T_LAUNCHCTL`）、再起動全体 120 秒（`RESTART_BUDGET_S`）。
  launchd が固まっても post-merge 子プロセスの 600 秒上限内
  （最悪約 524 秒）に収まり、正常な更新が kill → rollback されない
- **restore 同意待ち（consent hold）はあらゆる escalate で維持** —
  git 失敗・MERGE_HEAD 残存・判別不能な repo 状態などでも、hold 中は
  drainer を止めたまま marker と受領記録を残し
  `restore_consent_blocked` を報告する（outbox に書かないので
  承認対象の loss report は変わらない）。journal に記録のない hold
  も restore marker から検出して記録し直す
- **escalate 通知は条件ごとに 1 回** — 同じ journal・同じ原因では
  再通知せず、状態変化時または 6 時間経過で再通知。hold 外の
  再 escalate は drainer を bounce せず、止まっているものだけ起動する
- **lifecycle の verified 判定を公開 gate と同じ規則に** — 最新の
  fact audit だけを見る（`semantic_v4.fact_audit_verdict`）。古い
  PASS まで遡って verified と数えることはない
- **health watch** — ok→ok では通知せず、回復時に 1 回だけ通知。
  stale アラートは stale の根拠で重複排除する
- **canonical 昇格を出荷済み G6 基準で gate** — install は失敗した
  stage で停止し、cron の収束を検証する

#### 修正した問題

- **配送（hermes_plugin）** — tick ごとの journal 全件走査を
  差分読みに置換（1 tick の読み取りが約 1MB で増え続けていたのを
  一定に）し、保持中の view が後の refresh で書き換わる潜在不具合を
  修正。巻き戻った配送の fence、変化した source 上で拒否された
  begin のカード即時再発行、配送済み本文の破棄、signal 通知 off
  時の render claim、旧世代 worker が知らない feature を持つ spec
  の保留、Discord 作成 POST の single-shot 化（429 後の SDK
  再試行は許可）
- **semantic** — stale な canonical 行の再 projection、
  fact_source 往復後の projection 復活、壊れた artifact JSON /
  stage metadata への耐性、open obligation 上で PASS を公開しない、
  LLM 要求が未送信なら job を defer、LLM 保留中の Jev 予算消費の
  抑止、送信時 chunking に対する部分 receipt の証明
- **抽出** — 完了訪問を「予定通り」として報告、O2 流量・不整脈を
  vitals や訪問として誤読しない（`RULE_VERSION` 6）、イベント
  手がかりのある本文を低信号 prefilter から除外、根拠のない依頼
  本文の長さ制限、backoff 比較の時計を書込み側と統一
- **収集** — run 単位の relogin 予算を run 境界でも適用、有効
  session 下の 403 は要求単位の拒否として扱う、drain lock 競合中も
  未読 tick を継続
- **運用** — 更新復旧が system Python・任意の checkout から収束、
  install.sh の再実行耐性、バックアップの fsync、consent hold の
  順序、不要 cron job の削除検証、`apply()` が他の run の journal
  をロックなしで rollback しない、pgrep 失敗を「残存なし」と
  扱わない、`restart_agents` の pid 待ちが全体期限を上書きしない
- **表示** — 表示中の世代と食い違う view 統計、未確認の依頼を
  確認済みとして描画しない

#### 内部構造・開発者向け

- **重複実装の共通化** — fact binding・projection 現行判定・
  object meta ガード・scope lock 待ち・restore marker / dir fsync・
  relogin・publish・criteria hash などを 1 か所に集約（挙動不変）
- **テスト共通 helper（testkit）** — semantic・views・notify・
  plugin・ops の testkit を新設し、テストモジュール間の import を
  0 に。`test_extract_llm_v2.py` を `test_extract_llm.py` に改名
- **docs/ROADMAP.md** — 機能候補 v2（PR #1）

#### アップグレード時の注意

- **`hermes gateway restart` が必要** — `hermes_plugin/` を変更
- **独立 watchdog の再配置が必要** — 自動更新は
  `~/.mcs-recovery` を更新しない。repo の checkout で
  `./install.sh --no-brew --no-llm --no-plugin --no-services`
  を実行する（旧版は `mcs_recover.py.prev` に退避される）
- **ルール抽出の再実行** — `RULE_VERSION` 6 への bump により
  通常の stale 経路で再抽出される
- **semantic evaluation の schema が v3 に** —
  `semantic-evaluation/v3`
- **rollup は再計算しない** — `PERIOD_CHECK_VERSION` は 2 の
  まま（変更点は producer が書かない非 bool フラグにしか影響
  しないため）

全差分：[v1.0.5 → v1.0.6](https://github.com/yusuketakuma/hermes-mcs/compare/v1.0.5...v1.0.6)

</details>

## [1.0.5] — 2026-09-28

**本文に根拠のある抽出を強化し、後から届く解析結果をカードへ反映**

イベントの根拠確認、不足した抽出の再試行、ルール抽出とLLM抽出の統合を改善しました。カードの自動更新と、実際に動いているサービス・滞留の診断を強化しています。

> 更新前の確認：`mcs_setup.py services`を再実行し、新しいdrainerパスでサービスを再設定してください。

### 新機能

- **実稼働と処理待ちを診断**
  `mcs_setup.py check`でlaunchdの登録・起動状態と、台帳の解析ジョブ滞留を確認します。GUIの実行基盤を確認できない場合は警告に留めます。

- **解析結果を正本候補へ昇格する準備状況**
  `semantic --status`に評価・公開・取得状況を表示します。読み取り専用で、モデルを呼び出さずに確認できます。

- **抽出ベンチを拡充**
  否定・口語の訪問・家族・複数計測の合成ケースを加え、項目別採点と処理時間を記録します。異なるコーパス間の単純比較を拒否します。

### 改善

- **イベントの抽出に本文の裏付けを要求**
  転倒・入退院・訪問・検査などは本文に手がかりがある場合だけ採用します。口語の表現にも対応を広げました。

- **不足した抽出を1回だけやり直す**
  300字以上の単一チャンクで実項目が2件以下の結果を、条件に応じて1回再抽出します。再試行済みや除外条件を記録し、繰り返し続けません。

- **ルール抽出とLLM抽出を項目ごとに統合**
  LLMの部分的な結果がルールで拾ったイベントや計測値を隠さないようにします。イベントを併合し、計測値は項目ごとにLLMを優先します。

- **品質監査と緊急度の判断を調整**
  本文の裏付け不足が多い項目から監査する順番へ変更しました。臨床トリガーと過去の報告を区別する指示も改善します。

### 不具合修正

- **後から解析が届いてもカードが更新されない問題**
  新しい解析結果の準備状態を検出し、既存カードを再描画します。

- **口語表現の抽出漏れと監査の取り違え**
  症状や看取りに関する語彙を拡充しました。修復対象の投稿・患者・世代を再確認し、別対象や別時刻の値が混ざらないようにします。

- **診断で消えない警告と滞留の誤報**
  gatewayが再生成するキャッシュを更新警告の対象から外し、正式なhealth設定を検証します。処理停止と意図的に遅い処理を分け、shadow運用の正当な状態を誤報しません。

### 動作・設定の変更

- **抽出エンジンの実行パスを整理**
  現行LLM抽出を`mcs/extract/v4/`へ移動しました。旧世代はgit履歴を参照し、既存artifactとflat importは維持します。

- **health設定を正式に検証**
  値の型・範囲が不正な場合、`check`がエラーとして報告します。

### 更新時の注意

- `mcs_setup.py services`を再実行し、新しいdrainerパスでサービスを再設定してください。

- この版では`hermes_plugin/`を変更していないため、`hermes gateway restart`は不要です。

- 抽出schema・artifact kind・extract_versionは維持します。

### 技術詳細

<details>
<summary>内部仕様・根拠・公開当時の詳細を表示</summary>

抽出パイプラインの精度改善（本文中の裏付けを必須化したイベント
抽出、不足抽出への1回限りの修復、ルール検出のフィールド単位
統合）と、`mcs/extract/` の推論エンジン世代別フォルダ化
（v1〜v4）。運用面では `check` が plist の存在だけでなく
launchd の実稼働とキュー健全性を診断し、通知カードは抽出
artifact の後着を検知して自動で再描画する（v1.0.4 から
12 コミット・39 ファイル）。`hermes_plugin/` に変更はないため
gateway 再起動は不要。drainer のパス更新のため `mcs_setup.py
services` の reconcile が必要。

#### 動作が変わるもの

- **`mcs/extract/` を推論エンジン世代でフォルダ分割** —
  `v1/extract.py`（ルール抽出エンジン）、`v4/extract_llm.py`・
  `extract_bench.py`（現行 LLM 抽出レーンとベンチ）。v2/v3 は
  in-place 置換で退役済みのため git 履歴を指す README のみを配置
  （バージョン付き artifact は従来どおり読める）。`rollup.py` は
  v1+v4 を横断するため `extract/` 直下に維持。`_mcs_path` が
  ネストしたモジュールディレクトリを再帰登録するため
  `import extract_llm` 等の flat import は不変。drainer plist・
  夜間 catchup wrapper・各種ドキュメントを新パスに追随
- **離散イベント抽出に本文中の裏付けを要求** — eol・転倒・
  入院・退院・移乗・検査・訪問の各イベントは対象本文に
  手がかり表現がある場合のみ採用し、根拠のないイベントは破棄
  して修復パスへ回す。口語表現（倒れ/移り/処置/伺い 等）も
  手がかり語彙に追加。実測で events は QC の NO_MATCH 最大
  分類（約7割）だった
- **不足抽出に1回限りの修復を付与** — 300字以上の単一チャンク
  本文で実フィールド ≤2 しか出ない artifact は内容形状から
  thin を再判定して一度だけ再抽出する（`meta.thin` 未記録の
  過去 artifact も backfill なしで対象になる。prefilter 由来の
  記録は除外）。再抽出でも thin/失敗なら `meta.thin_retried`
  を付けてループさせず settle。複数チャンク本文は nudge 免除
  だが thin 記録は残す
- **ルール(v1)検出を LLM 出力へフィールド単位で統合** — LLM の
  部分的なイベント一覧が v1 限定の検出（medication・
  adherence・media_ref、約400件の eol）を、部分的な vitals
  dict が v1 限定の計測値（約520件）を隠していた。イベントは
  union、vitals はキー単位で LLM 優先のマージに変更。フィールド
  ごとの優先規則をモジュール docstring に契約として記録し
  `notify_flush`・カード描画とのドリフトを防ぐ
- **QC 監査の質問順を実測 NO_MATCH 順に** — events→vitals→
  meds→symptoms→labs。従来は共有質問予算が meds/symptoms で
  尽き、最大の NO_MATCH 源だった events・vitals に到達し
  なかった
- **urgency 判定の根拠を prompt で明示** — 「緊急」「至急」の
  文言がなくても臨床トリガーがあれば high とし、過去形の
  報告は routine に留める

#### 修正した問題

- **抽出 artifact の後着でカードが薄い表示のまま残る問題** —
  スレッドカードの `_source_fp` が message 行だけを fingerprint
  していたため、描画から数分〜数時間後に extract_llm artifact
  が到着しても世代がずれず v1-only の表示のままだった。
  `structured_view.fact_ready_ids` が `latest_fact_artifact` と
  同一の現行 fact 判定をバッチ共有し、fingerprint にメッセージ
  単位の readiness を持たせて、artifact 到着時に
  `source_generation` を更新する
- **ルール抽出の語彙不足** — 実運用の口語表現（「お熱があって」
  「意識がない」「お亡くなり」）を症状・eol 手がかりに追加
  （意識系は decline に限定した精密マッチ）。RULE_VERSION bump
  により通常の stale 経路で再抽出される
- **`check` の恒久警告を解消** — `hermes_plugin/**/__pycache__`
  は gateway が plugin 読込のたびに再生成するため stale-gateway
  警告が絶対に消えない問題を、source ファイルの mtime のみを
  比較する方式に修正。`health` config ブロックは
  `health_watch.py` が消費する正規設定なのに「unknown config
  key」警告が出続けていたため、読み取り契約に沿う
  validator（tick_interval_s は (0,86400] の有限数、
  max_missed_runs は int [0,100]、night_thinning は bool）を追加
- **backlog 警告の誤報を整理** — 意図的に遅い backfill（Jev
  日次予算＋レーン公平性）で pending>1d 警告が常時発火して
  いた。24時間完了ゼロ＋滞留あり＝真の stall と、3日超の
  滞留拡大＝lag に区別。shadow モードで
  `canonical_projection` が無いのは正当なので、fact_source が
  canonical の時だけ error 相当のシグナルにした
- **修復・監査の正確性** — thin 再抽出は pin された artifact を
  message/project/hash scope で再検証し、保持した臨床主張が
  後退しない修復のみ採用。QC 監査は vital-keyed ドメインを
  実測順に処理し、表示層は選択済み LLM 結果を優先して他対象・
  他時刻のルール値を混ぜない

#### 新しい運用機能

- **`check` が launchd の実稼働を検査** — plist の存在確認だけ
  では 2026-09-28 の障害を数日間検出できなかったため、
  `launchctl print gui/UID/LABEL` で installed-but-unloaded を
  error に（headless 等で GUI domain に届かない場合は
  warning）。台帳を read-only で読み、extract_qc/semantic ジョブ
  の1日超滞留・extract_llm backlog>500・semantic 有効で
  canonical_projection ゼロ（publish 未実行）も警告する
- **`semantic --status` に canonical_readiness** —
  `fact_source=canonical` 昇格の判断材料（v2 shadow の保有量・
  完了 coverage・audit/publish 通過数）を read-only・モデル
  呼出しなしで表示し、昇格可否を質問前に確認できる
- **ベンチコーパス拡充と計測の精密化** — 否定・口語訪問・家族・
  複数計測ケースを追加。フィールド単位採点・ケース別の
  call/repair 回数・p50/p95 時間・コーパス hash を記録し、
  異なるコーパス間の素朴な比較は拒否する

#### 内部構造・開発者向け

- **README のモック刷新** — Discord カード・Discord カード+
  コンパニオンスレッド・Slack カード・Slack スレッドパネルの
  4種に番号凡例・送信者行・構造化フィールド・操作ボタン・
  配送状態・確認後状態を記した完全合成の mock を冒頭に配置

#### アップグレード時の注意

- **drainer の実行パスが `mcs/extract/v4/extract_llm.py` に
  移動** — `mcs_setup.py services` の reconcile で plist を
  再描画・再起動する。旧パスを参照する実行経路・cron
  wrapper・ドキュメントは残っていない
- **`hermes_plugin/` は無変更** — `hermes gateway restart` は
  不要
- **config の `health` ブロックが正式な検証対象に** — 不正値は
  `check` で error になる（従来は未知キー警告のみ）
- **抽出 schema/artifact kind は不変** — `extract_llm` kind・
  `extract_version=4` を維持し、既存の台帳行・QC・v4
  publication 経路への影響はない

全差分：[v1.0.4 → v1.0.5](https://github.com/yusuketakuma/hermes-mcs/compare/v1.0.4...v1.0.5)

</details>

## [1.0.4] — 2026-09-28

**部分導入とSlack設定に対応し、患者識別・保存・復旧を強化**

既存環境を保持しながら必要な部分だけ導入できるようにしました。Slackの設定、ローカルLLMの接続先設定、セッション回復と安全性・正確性の修正をまとめています。

> 更新前の確認：`hermes_plugin/`の変更を反映するため`hermes gateway restart`が必要です。

### 新機能

- **必要な部分だけインストール**
  `--no-brew`・`--no-llm`・`--no-plugin`・`--no-services`・`--no-recovery`で各段階を省略できます。既存のモデルサービス・設定・checkoutを検出して再利用します。

- **Slackを設定ウィザードで選択**
  `notify.interactive=slack`を選び、Slackの接続範囲を検証できます。既存tokenとallowlistは保持し、秘密値はコマンド引数へ出しません。

- **ローカルLLMの接続先とモデルを設定**
  `local_llm.url`と`local_llm.model`を指定できます。接続先はloopbackのHTTPだけを許可し、外部へ患者情報を送る設定にはしません。

- **更新状態と外部出力の診断を拡充**
  古いgateway、残留する通知先設定、LLMの接続状態を確認します。外部出力の共有schemaで自由記述の混入・未承認送信・結果不明時の重複を防ぎます。

### 改善

- **初回セットアップを進めやすく**
  設定済みの対話通知とHermes CLIがあれば、init完了時にgatewayの導入・起動を同期します。外部コマンドには待ち時間上限を設けます。

- **実行中のセッション失効から回復を試行**
  各収集段階でセッションが失効したら、その実行内で自動ログインを1回試します。成功時は再開し、失敗時は原因を報告します。

- **SlackとDiscordの配送処理を共通化**
  同じカード・スレッド・添付の配送基盤を共有します。読み取り時のDB問い合わせをまとめ、意味解析の出力・処理予算を拡大しました。

- **コード・CI・導入文書を整理**
  7領域とplugin・testsを整理し、契約テストを追加しました。CI依存を固定し、導入・エージェント用手順を整備します。

### 不具合修正

- **別患者の情報が混ざる境界を修正**
  保存・集約・通知・閲覧で患者の不一致を拒否し、操作主体・配送先・現在の世代を確認します。添付や取得状況の範囲漏れにも対処しました。

- **検査値・抽出世代の取り違えを修正**
  長文末尾の血圧や検査値の欠落、破損した途中記録、古い結果による新世代の上書きを修正します。

- **意味評価と保存・復元の整合を修正**
  原文・根拠・評価分母の対応を整理し、復元承認を内容にも結び付けます。不明な保存状態のまま復旧を継続しません。

- **配送直前と復旧処理の安全性**
  停止・復元保留・患者範囲を配送直前に確認し、検証した同じ添付データを送ります。復旧時に正規cronを誤削除する問題も修正しました。

- **新規環境のPython選択と秘密値保存を修正**
  Hermes venvを優先し、代替Pythonを3.10以上に制限します。引用処理・設定検証・Keychain登録失敗時の既存値復元を改善します。

### 動作・設定の変更

- **患者情報ファイルの権限を統一**
  台帳・バックアップ・snapshotは0600、関連ディレクトリは0700に揃えます。

### 更新時の注意

- `hermes_plugin/`の変更を反映するため`hermes gateway restart`が必要です。

- `mcs_setup.py services`を再実行するとcron／launchdの差分を再設定します。

- 既存token・allowlist・scopeを保持します。導入コマンドはその版のvenv Pythonの案内に従ってください。

### 技術詳細

<details>
<summary>内部仕様・根拠・公開当時の詳細を表示</summary>

追跡ファイル284件の全リポジトリレビュー（dev-records
`refactor-20260927`）にもとづく安定性・正確性の改善、部分導入済み
環境への柔軟なインストール、Slack の設定ウィザード対応、ローカル
LLM エンドポイントの設定可能化。通知・収集の既存設定はそのまま
使える。`hermes_plugin/` の変更を反映するには `hermes gateway
restart` が必要（`check` が起動中 gateway より plugin が新しい
場合に警告するようになった）。

#### 動作が変わるもの

- **`install.sh` がステージ単位でスキップ可能に** — `--no-brew`・
  `--no-llm`・`--no-plugin`・`--no-services`・`--no-recovery` で
  各段を省略でき、既にローカルLLM稼働中・Discord/Slack 導入済み・
  依存を自前管理、といった部分導入環境で必要な段だけを実行
  できる。フラグなしでも `:8080` の OpenAI 互換応答・既存
  LaunchAgent・導入済み plugin/cron/checkout を検出して保持・
  再利用する（`-h` でヘルプ）
- **Slack を設定ウィザードの第一級 transport に** —
  `notify.interactive=slack` が `init` で選択可能になり、
  `notify.slack` scope（profile・application_id・team_id・
  channel_id）を検証する。plugin 側には `slack_*` キーと
  `slack_adapter_enabled` を書き込み、`SLACK_BOT_TOKEN`/
  `SLACK_APP_TOKEN` を serving profile の `.env` へ格納する
  （既存 token・allowlist は上書きしない。`*_TOKEN` は全て
  stdin 経由で argv/`ps` に出さない）。Discord と同じ
  `mcs-discord-commands` plugin が Slack のカードも処理する
- **ローカルLLMの endpoint/model を config で指定可能に** —
  `local_llm.url`・`local_llm.model`（既定は従来どおり
  `http://127.0.0.1:8080/v1/chat/completions`・`Qwen3.5-9B`）。
  url は loopback の http のみ受け付け、PHI が外部へ送られない
  制約を維持する。extract/semantic の各呼出しは実行時に解決済み
  endpoint/model を使い、artifact metadata にも解決済み model を
  記録する。`check` は設定済み endpoint の `/v1/models`・`/slots`
  を probe し、slot 不足は error、slots 非対応は warning に留める
- **`init` 完了時に gateway を同期** — `interactive` 設定済みかつ
  Hermes CLI があれば `hermes gateway install`/`start` を init 内で
  実行し、初回導入（services→init の順）で gateway 未登録のまま
  残らないようにした
- **`install.sh` の python 解決を修正** — `brew install
  python@3.13` は無印 `python3` を PATH に出さないため、新規
  環境では `/usr/bin/python3`（3.9 系）に解決されサービス登録が
  全て失敗し得た。Hermes venv python を優先し、フォールバックも
  3.10 以上に限定。`mcs_setup.py` にバージョンガードを追加し、
  案内文・ドキュメントのコマンド例を venv python に統一
- **セットアップの外部コマンドに timeout** — `security`・
  `launchctl`・`hermes cron` など `subprocess.run` 全14箇所を
  共通 `_run()` 経由にし既定 timeout を設定。Keychain プロンプト
  や launchd の応答なしで check/reconcile が無限に停止しなくなった
- **セッション失効時の in-run 自動再ログイン** — `SessionExpired` が
  unread/backfill/self_probe/discovery/reply/history/trickle/reconcile
  の各ステージで発生した時点で `auto_login` を1回試行し、成功すれば
  その run のまま再開する（従来は jobs_only・backfill・probe 系の
  失効は run を即 abort し、復旧は次回 tick まで持ち越しだった）。
  結果は通知される: 復旧→`session_recovered`、失敗→従来どおり
  `session_expired`（detail に `auto_login=<state>`）。
  `session_expired` は kind 単位1時間 throttle、`session_recovered`
  は既出の失効アラートを解消する通知なので throttle しない。
  試行は run log の `relogin_attempts` に残る（auto_login 自体の
  例外は `detail` に repr で記録）
- **PHI ファイルのパーミション統一** — `ledger.db`(WAL/SHM 含む)、
  日次・preupdate バックアップ、公開スナップショット、
  `data/`・`data/cmd`・`chrome-profile`・`data/attachments`・
  `data/backups`・`data/snapshots` の各ディレクトリを
  0600/0700 に統一（従来は `data/` の 0700 に依存し、
  ファイル自体は umask 任せだった）

#### 修正した問題

- **患者識別の不一致を各層で拒否** — 保存・集約・通知・閲覧で
  患者の不一致を拒否し、receipt の操作主体・配送先 scope・現行
  世代を確認。read model の患者 scope 付き attachment 取得と
  coverage 漏洩を修正し、projection→read の欠落（canonical
  relation type・engine/project/error 行・削除元 fact の開示）を
  修復
- **抽出の取り違えを修正** — 長文結合時の検査値欠落、本文末尾の
  血圧の取り違え、破損 checkpoint、古い抽出による新世代上書きを
  修正
- **意味評価の分母と対応を修正** — 根拠 span・原文世代・修復後
  fact の対応、未評価分を含む評価分母、監査と必須表示の整合
- **保存・復元の境界を修正** — snapshot を非公開の固有一時ファイル
  から公開し、復元承認を行内容 hash にも結び付け、WAL と中断
  journal が不明な状態での継続を防止
- **配送直前チェック** — 配送直前の停止・復元保留・患者 scope を
  確認し、添付は検証した同じ bytes を配送。不確実な送信の自動
  再試行を抑制
- **実行管理の修正** — LLM 枠の所有権と世代、resident の設定
  再読込、昼夜の収集予定に沿った health 判定を修正
- **read model の N+1 を解消** — メッセージごとに発行していた
  artifacts クエリを500件チャンクのバッチ `IN` クエリに集約
- **`mcs_recover.py` の既知 cron 一覧を更新** — `mcs_health.sh` が
  欠落しており、旧 manifest 経由の復旧時に正規 cron を stale と
  誤認して削除し得た。一覧に追加し、CRON_JOBS との一致を assert
  する回帰テストを追加
- **導入・秘密情報** — shell/XML の引用、設定値の型・範囲検証、
  秘密値の保存形式、Keychain 登録失敗時の既存値復元を改善

#### 新しい運用機能

- **外部 export の共有 schema** — `mcs/ops/export_schema.py` を
  新設し、producer と受け口で閉じた schema を共有。自由記述の
  混入・未承認の送信・結果不明時の重複実行を防止
- **`check` の診断強化** — 起動中 gateway より `hermes_plugin/`
  が新しい場合に restart を促す stale-plugin 警告、非アクティブ側
  transport の残留 scope 警告、Slack scope 検証、設定済み LLM
  endpoint の probe を追加
- **semantic LLM の出力上限・予算を拡大** — `llm_chat` の出力上限を
  4096 token に、v2 抽出向けに呼出し回数上限と job budget を拡大
- **Slack カードの出力を Discord と同一に** — transport-neutral の
  delivery 基盤で両 transport が同一のカード・スレッド・添付経路を
  共有
- **extract レーンの prefilter と共通 drain ゲート** — extract/semantic
  間で安定性ゲートを共通化し、v4 詳細フィールドを render に反映

#### 内部構造・開発者向け

- **core/ingest/notify/extract/semantic/views/ops の7領域と
  plugin/tests を全面リファクタ** — 長いパイプライン関数を名前付き
  phase helper に分割し、領域ごとの契約テストを追加（flat import
  契約は維持）。変更は dev-records `refactor-20260927` に記録
- **CI のサプライチェーン強化** — `actions/checkout`・
  `actions/setup-python` を commit SHA でピン（v7.0.1/v7.0.0）、
  ruff 0.6.9→0.16.8・pytest 8.3.3→9.1.1・CI Python 3.13 に更新
- **ドキュメント整備** — `docs/INSTALLATION.md`（addon/standalone
  経路・部分導入フラグ）と `docs/SETUP_AGENT.md`（エージェント
  実行用 runbook）を追加し、README を現行動作に同期

#### アップグレード時の注意

- **`hermes_plugin/` を更新したら `hermes gateway restart` が必要**
  — 再起動なしでは新形式 spec を旧 worker が処理し、カードのみ
  届き本文・添付が欠落する。`check` がこの状態を警告する
- **`mcs_setup.py services` を再実行すると cron/launchd を
  reconcile する** — `mcs_health.sh` 等の差分があれば追加・更新
  される（削除は stale 判定のみ）
- **既存の token・allowlist・scope 設定は保持される** — init の
  再実行・部分導入フラグともに既設定値を上書きしない

全差分：[v1.0.3 → v1.0.4](https://github.com/yusuketakuma/hermes-mcs/compare/v1.0.3...v1.0.4)

</details>

## [1.0.3] — 2026-09-26

**通知をカードと専用スレッドへ刷新し、対話操作と解析基盤を追加**

Discord／Slackのカードから操作でき、本文・添付は専用スレッドへ配送する方式へ移行しました。解析の自己修復、収集経路の更新、監視と中断した更新の復旧も追加しています。

> 更新前の確認：`hermes_plugin/`を更新したら`hermes gateway restart`が必要です。再起動しないと本文・添付が欠落する場合があります。

### 新機能

- **通知カードから確認・担当・依頼操作**
  確認・担当・保留・依頼作成・本文表示を操作できます。スタッフ一覧、操作範囲とスレッド内の認可、カードタスク管理も加えました。

- **本文・添付用の専用スレッド**
  `notify.card_thread`により本文全文と対象添付を専用スレッドへ送ります。initウィザードでは既定オンです。利用不可の場合は本人のみへの本文表示に切り替えます。

- **Slackのカード配送と自己更新**
  Discordと共通の配送基盤をSlackへ展開し、更新確認・適用・復旧の処理を追加しました。

- **意味解析の自己修復と評価後の公開**
  段階別の記録を持つv4解析パイプラインを追加します。合格した成果のみ公開し、正本候補への昇格には人手ラベルによるG6評価を要求します。

- **自投稿・先に読まれた投稿の取り込み**
  `self_posts=true`で各患者の最新投稿を確認し、未保存なら上限付き履歴取得と新着通知を行います。取り切れないIDを記録して無限取得を抑止します。

- **稼働監視と更新中断からの復旧**
  収集状態の存在・形式・新しさを監視し、中断した更新適用を復旧します。外部知識ストア向けの出力契約も追加しました。

### 改善

- **配送の途中から再開**
  カード・スレッド・本文・添付を個別に記録し、worker再起動後に中断点から再開します。配送の完了・未送信・保留を追跡できます。

- **LLMの利用枠と抽出品質を調整**
  背景処理と新着処理を調停する枠管理を導入し、解析予算を300秒へ変更しました。バッチ抽出、1回限りの修復・再抽出、計測値ラベルの照合を強化します。

- **レビュー候補とカード表示を整理**
  同じ投稿の候補通知を併合し、否定文や取得状況を考慮します。送信者の時刻・職種・組織と複数ページの位置を表示します。

- **開発構成と利用者向け文書を整理**
  実行モジュール・配送基盤・テストを領域別へ分割し、flat importを維持します。文書と画像は完全合成例へ置き換え、再発防止のCI検査を拡充しました。

### 不具合修正

- **スレッドだけ作られて本文・添付が届かない問題**
  workerの世代ずれ、既存スレッドの再作成、配送結果の処理順を修正しました。

- **古い未読の新着通知と取得不完全の誤報**
  履歴由来や古い投稿の通知を抑制し、実際の取得遅れだけを報告します。止まった配送要求の救済も拡充します。

- **削除投稿の漏れと計測値の誤認**
  削除された本文・抜粋を根拠から除外し、脈を血糖値として扱うなどの誤りと世代ずれを修正しました。

### 動作・設定の変更

- **MCSの収集・既読化の経路を変更**
  旧エンドポイントから現行経路へ移行しました。取得中に到着した投稿の扱いを含め、snapshot時刻を使う既読化の条件を維持します。

- **定期実行で自動既読化を有効化**
  当時の承認によりcronとコマンド監視へ`--mark-read`を追加しました。snapshot timestamp必須の安全条件は維持します。

- **当時の夜間収集は20分間隔**
  この版では22〜06時を20分間隔へ変更しました。この運用は後に廃止され、v1.0.8時点では昼夜とも5分間隔です。

### 更新時の注意

- `hermes_plugin/`を更新したら`hermes gateway restart`が必要です。再起動しないと本文・添付が欠落する場合があります。

- この版のsemantic v4はshadow運用です。canonicalへの昇格には人手ラベルに基づくG6評価が必要です。

- この版の自動ログインはbest-effortで、当時の本番ログではフォーム投入からtoken検証までの経路は未実証です。

### 技術詳細

<details>
<summary>内部仕様・根拠・公開当時の詳細を表示</summary>

Discord 通知を「カード＋コンパニオンスレッド」の対話型モデルへ刷新
（本文・添付は専用スレッドへ耐久化配送）、semantic v4 自己修復
パイプラインと canonical 読取経路の追加、retired エンドポイント
からの移行、モジュール第一層分割など基盤の大規模な更新。通知・
収集の既存設定はそのまま使えるが、`hermes_plugin/` の変更を反映
するには Hermes gateway の再起動が必要（後述）。

#### 動作が変わるもの

- **本文・添付はカードのコンパニオンスレッドへ配送** —
  `notify.card_thread`（`init` ウィザード既定でオン）で、患者
  スレッドごとのカードが
  `💬 患者名 — MM-DD` の専用スレッドを立て、表示対象の本文全文と
  対象添付（画像・PDF 等）をリアルタイムでスレッド内へ投稿する。
  カード本体は送信者行・構造化要約・操作ボタンのみで肥大化しない。
  本文は必要に応じて複数 chunk に分割される。スレッドを持てない
  カード（オフ・作成失敗・削除済み）では `📄 本文表示` が残り、
  押した本人のみに ephemeral 表示する従来動作がフォールバックと
  して維持される
- **通知配送はジャーナル化 parts** — card → thread → 本文 chunk →
  添付を個別の durable part として記録し、worker 再起動・中断から
  中断点で再開する。配送済み本文は remote match で重複投稿しない。
  配送状態は render rollup（none/pending/complete/incomplete）と
  part state（pending/delivered/not_sent/held）で追跡できる
- **未読チェックの夜間間引き** — 投稿実績（全期間で新着のほぼ
  全てが 07-21 時）に基づき、22-06 時は :00/:20/:40 のみ実行する
  20分間隔に間引き。MCS セッションの失効上限 30 分を下回る設計で
  bearer を維持し、auto_login を夜間のクリティカルパスに置かない。
  あわせて `health.max_missed_runs=4`（freshness deadline
  15→25分）で夜間 cadence の stale 誤検知を防止
- **MCS 取得経路を現行エンドポイントへ移行** — `GET /projects/unread`
  と `POST /projects/{id}/mark_as_read` がサーバ側で 403 に退場した
  ため、未読列挙は `/projects?include_meta=1` の `is_unread`
  フィルタに、既読化は messages GET 後の `oldest_unread_message`
  確認に変更。`paginate.timestamp` は per-request 時計となり、
  `snapshot_ts` は walk 内の最小値を採用（walk 途中到着の投稿が
  既読化対象から漏れない不変条件を維持）
- **`auto_login` はフォーム操作の前に live session を回復する** —
  従来は login フォーム駆動のみだったため、token 失効とログアウトを
  区別できず `manual_required` を繰り返し報告していた。`_recover_session()`
  を最優先で試行し、fresh localStorage token + `check_session` で
  資格情報を注入せず回復する経路を追加。login フォームが見つから
  ない場合もログイン中アプリのリダイレクトと解釈して session 検証を
  優先する。dead-session の `session_expired` アラートは throttle 済み
- **ローカルLLM呼出しを admission broker 経由に** — slot 容量を
  上限管理するブローカを導入し、バックグラウンド大量処理と
  リアルタイム経路が取り合いにならないよう調停
- **セマンティック抽出の LLM 呼出し予算を 300 秒に拡大** — v2 fact
  抽出の実測時間に合わせた上限引上げ
- **定期実行で自動既読化が有効** — 2026-09-23 の明示承認により
  `mcs_check.sh`（cron tick）と `local.mcs-cmd`（cmd WatchPaths）に
  `--mark-read` を付与。自動 ACK なし方針から、snapshot timestamp
  必須の安全ゲートを保ったまま自動既読化へ移行
- **レビュー候補シグナルを再設計** — 自己抑制・新検知器5種・
  通知階層化。同一投稿由来の `med_change_no_followup` 通知を1通に
  併合、否定文・digest 救済・coverage ゲートの扱いを修正、自己
  identity は `/users/self` から自動取得

#### 修正した問題（通知・配送）

- **空スレッドになる配送漏れを修復** — 複合要因:
  (a) 長寿命 gateway が parts manifest 導入前の旧 plugin コードを
  保持し、runner が発行した新形式 spec を旧 worker が処理して
  thread だけ作成・本文と添付が未配送になる version skew。
  `hermes_plugin/` 変更時の gateway 再起動必須を運用文書に明記
  (b) 再開された sealed create spec が既存 thread に対して
  `create_thread` を呼ぶと Discord が `http_400`（既に thread
  あり）を返し、definitive reject として part `not_sent`・
  capability scope の負キャッシュ・`thread_state='failed'` が
  自己永続化していた。既存 thread（`msg.thread` → 起点
  message.id と同一 snowflake の thread を `fetch_channel` で解決）
  を bind して `delivered` として継続。message 個別の 4xx では
  scope capability を負キャッシュしない（401/403 のみ対象）
  (c) worker 死亡直後の reconcile receipt が `transport_begin` より
  先に drain され `unknown_attempt` 棄却 → granted 孤児化。
  drain を begins → receipts → others の順に修正
- **`hermes_plugin/` 変更は gateway 再起動が必要** — gateway は
  plugin を起動時に import する長寿命プロセスのため、再起動なし
  では新旧 spec の世代ずれが起きる（2026-09 実機事案）
- **stale unread の通知抑制** — 古い未読や backfill 由来の投稿が
  新着として通知されないよう age cutoff を適用
- **hold rescue の拡張** — stranded intent 全体への rescue と
  atomic hold
- **coverage_incomplete の誤検知を修正** — 実際の lag がある場合
  のみ報告
- **削除されたメッセージ由来の data leak を修正** — tombstone/
  snippet 本文を evidence から除外し、追加の表記ゆれを検知対象に
- **vital ラベルの誤認を修正** — 脈を血糖値として扱う誤り、
  free-text 欄の guard、抽出 context の世代ずれ
- **表示上の改善** — カード送信者行に記入時刻・職種・組織を表示、
  カード面を Container で包みカードとして描画、複数ページカードに
  ページ位置/件数を表示、apply-time re-render と drain-side sweep

#### 新しい運用機能

- **interactive card pipeline** — sealed intent → 不変 render spec
  → parts manifest の配送基盤。カード上のボタンから ✅確認・
  👤担当・⏸保留・📝依頼作成・📄本文表示を操作。card task 管理・
  staff directory・動的 project scope・コンパニオンスレッド内
  クリックの認可・scope lock の適切な解放を含む
- **Slack カード配送** — Discord と同じ card transport を Slack に
  展開（transport-neutral 層で共有）。自己更新ライフサイクル
  （update check → apply → recover）も実装
- **semantic v4 パイプライン + canonical 読取** — s0_prep 〜
  s8_publish の stage receipt を持つ自己修復型推論パイプライン。
  PASS の成果のみが `semantic_facts_v4` read model として公開され、
  `fact_source=canonical` 昇格は人手ラベルに基づく評価レポート
  （G6 基準）を必須とする fail-closed ゲートで制御。PENDING/
  NEEDS_REVIEW/STALE の中間状態と `v4_diagnostic`・cohort 退役・
  repair 予約を持つ。canonical 読取は `semantic_facts_v2` を正本と
  する経路
- **自投稿・他者先読み投稿の取り込み** — `self_posts=true` で、
  各患者の `messages/latest` を tick ごとに probe し、未保管の
  最新 id があれば bounded 履歴取得して新着通知。取り切れない id
  は `probe_mid` に記録して再取得ループを抑止
- **ingest health watch + lifecycle recovery** — `health.json` の
  契約ベース監視（presence・parseability・freshness）を
  `mcs_health.sh` cron で常駐化、`mcs_recover.py --if-stale` が
  中断した update apply を復旧
- **governed external export contract** — 外部知識ストア向け出力の
  契約化
- **抽出 v3 の品質・処理量強化** — batch 推論（既定4件集約）、
  検証 drop 時の1回限り repair 再問、llama.cpp timings を artifact
  meta に集計、vitals の本文ラベル照合による誤キー自動修正、
  Jev QC フィードバックによる1回限りの再抽出

#### 内部構造・開発者向け

- **`mcs/` を第一層サブディレクトリに再編** — `core/`・`ingest/`・
  `notify/`・`extract/`・`semantic/`・`views/`・`ops/` に分割。
  エントリポイントは `import _mcs_path` の2行ブートストラップで
  flat import（`import ledger` 等）を維持するため import 文の
  変更は不要
- **`mcs_delivery/` 配送基盤を分離** — Discord/Slack 共有の
  transport-neutral 層（paths・journal・registry・envelopes・spec・
  text・worker）
- **テストツリーを領域別に再配置** — `tests/` も `core/`・`ingest/`・
  `notify/`・`extract/`・`semantic/`・`views/`・`ops/`・`plugin/`・
  `meta/` に整理。実LLM・実Jev endpoint はテスト transport 境界で
  遮断され、テストは一時 DB + スタブのみを使う
- **README をユーザー向けに分割** — `docs/DEVELOPMENT.md`（開発・
  運用リファレンス、generated ブロックはこちらへ移転）、稼働
  システム構成図・カード/スレッド図の SVG 追加。`update_readme.py`
  は複数対象ファイルを処理する構成に
- **表示例は完全合成のみ** — 実患者・実投稿由来の記録を
  sanitized 版に差し替え、ドキュメントの画像も合成ケースで描画
- **CI gates 強化** — incident 由来の静的ゲート（`ci/gates.py`・
  `ci/mine_gates.py`）を追加・拡充し、発生した問題の再発を gate で
  防止

#### 注意

- **`hermes_plugin/` を更新したら `hermes gateway restart` が必要**
  — 長寿命 gateway が起動時にコードを読むため。再起動しないと
  新形式 spec を旧 worker が処理し、カードのみ届いてスレッド本文・
  添付が欠落する（本リリースの修正前事案）
- **auto_login は best-effort のフォールバック** — 本番ログで
  `auto_login=ok` の記録はなく、フォーム投入→token 検証のフル
  経路は未実証。夜間間引きがこの依存を避ける設計（20分 < 30分）
- **semantic v4 は shadow 運用中** — canonical 昇格には人手ラベル
  による G6 評価レポートが必須。現状は `semantic.mode=shadow` で
  `semantic_facts_v4` は PASS 成果のみ発行される読み取り面

全差分：[v1.0.2 → v1.0.3](https://github.com/yusuketakuma/hermes-mcs/compare/v1.0.2...v1.0.3)

</details>

## [1.0.2] — 2026-09-23

**収集と抽出の安全性を修復し、通知先切り替え・品質確認を追加**

削除・編集・添付撤回の反映、履歴の取得継続、根拠と原文の対応を強化しました。通知経路の統一、品質監査の閲覧、外部向け要約出力と抽出の並列処理を追加しています。

### 新機能

- **抽出品質の結果を閲覧**
  `mcs_view qc`で評価済み・未評価・判定別・要注意投稿を確認できます。NO_MATCHは「本文に裏付けがない」であり、事実が存在しないことは意味しません。古い世代の判定は集計から除外します。

- **患者要約をMarkdownへ出力**
  `brain_export`で公開snapshotから要約を生成します。本文を含めませんが患者情報を含むため、同期先の権限管理が必要です。

- **意味抽出と日次の品質・運用確認**
  semantic-facts v2、監査、分割・常駐処理と、履歴／現在の世代を区別した集計・観測を追加しました。

- **Hermesの導入を自動化**
  未導入なら固定commitから環境構築とpluginリンクまで進めます。Keychainから.envへの同期ツールも追加しました。

### 改善

- **通知経路をHermesの送信処理へ統一**
  通知先を`notify_target`で指定し、Discord／Slackを切り替えられます。配送結果記録・分割再開を維持し、拡張子のない添付の表示も調整しました。

- **確認候補の通知を具体化**
  患者名と最新根拠投稿の抜粋、タイムラインを開く案内を表示します。通知の保存記録はIDと固定文だけで、本文は送信時に台帳から読みます。

- **抽出を並列実行可能に**
  `extract_llm --all --workers N`で並列化できます。DB操作は専有スレッドで直列化し、形式検出を処理開始前に1回行います。

- **文書・構成・CI検査を整理**
  利用者向け説明と取得できない範囲を明記しました。モジュールを領域別に整理し、標準ライブラリのみの依存と合成テストを維持します。

### 不具合修正

- **通知文が添付コマンドとして解釈される問題**
  本文や抽出結果に含まれる`MEDIA:`などの制御構文を無効化します。

- **削除・編集・添付撤回と未取得投稿の扱い**
  MCS側の現行状態を反映し、一覧の返信抜粋しか取得していない親投稿を既読対象へ混ぜません。

- **履歴の上限到達・再試行ループを修正**
  継続位置を保存して残りを取得し、失敗後も既取得分を保持します。通常エラーは8回で見える失敗にし、セッション切れは試行回数を消費しません。

- **通信と取得期限の適用を修正**
  通信処理を子プロセスへ隔離し、期限超過を終了・回収します。WebSocket違反を拒否し、初期取り込みの期限を実際の取得へ伝えます。

- **根拠・原文・患者・解析世代の不一致を修正**
  現在使用できる解析結果の判定を統一し、無効・形式不正・本文不一致を採用しません。品質監査も参照元の抽出世代に結び付けます。

- **同じ文面の別事実と空項目を修正**
  属性や時刻が異なる事実は重複として消さず、未解決として残します。空白だけの薬名・症状を除外し、ベンチには依頼項目も追加しました。

### 動作・設定の変更

- **品質監査は直近60日の投稿が対象**
  3日から60日へ拡大しました。対象外の未処理ジョブを回収し、過去の監査結果は閲覧可能なまま残します。

- **LLMの背景・新着スロットを入れ替え**
  2枠を維持し、通信上のid_slotは背景0・リアルタイム1へ変更しました。`--lend-rt`は新着枠が空いているときだけ借用します。

- **Keychain読取失敗時は.envへ切り替え**
  `~/.mcs/.env`の`MCS_PASSWORD`へフォールバックします。平文のためFileVaultと物理セキュリティが前提です。

### 更新時の注意

- 通知先設定を`discord_channel_id`から`notify_target`へ移行してください。移行済み環境は変更不要です。

- LLMスロットの通信上の割り当てはv1.0.1と逆です。独自指定がある場合は確認してください。

- その他の既存の運用操作・設定は継続して利用できます。外部へ要約を同期する場合は出力先の権限を確認してください。

### 技術詳細

<details>
<summary>内部仕様・根拠・公開当時の詳細を表示</summary>

全体レビュー（46ファイル・実測）に基づく安全性・収集完全性・抽出
正確性の大規模修復と、canonical semantic-facts v2・通知経路の
一本化・抽出並列化・運用導線の追加。既存の運用操作・設定はそのまま
使える。通知の宛先設定だけ `discord_channel_id` から `notify_target`
へ移行している（済みの環境は変更不要）。

#### 動作が変わるもの

- **通知の配送経路を `hermes send` に一本化** — Discord API への
  独自実装（urllib・multipart・429 再送）を全廃し、Hermes の
  送信コマンドに委譲。config は `notify_target`（例
  `"discord:1550…"`）を参照し、`"slack:#mcs"` への変更だけで
  配送先を切り替えられる。outbox・receipt・分割再開などの配送
  保証機構は維持。拡張子のない添付には原本名の拡張子を持つ
  hardlink 別名を付けて送る（Discord で画像として表示されるため）
- **レビュー候補シグナルの通知が具体的になった** — 患者名と
  根拠となった最新の投稿の抜粋、末尾に `/mcs timeline` で確認する
  導線を表示。患者行が欠損・投稿が削除されている場合は従来の
  基本文で送る。通知の保存データは従来通り ID と固定文のみで、
  本文は送信時に台帳から解決する
- **抽出結果の QC 監査の対象が「直近60日以内の投稿」に拡大** —
  従来の3日窓から変更。窓を超えた未処理の監査ジョブは遅れて
  実行せず完了扱いで回収し、対象外の記録の過去の監査結果は
  そのまま閲覧できる。`mcs_view qc` の集計が対象内件数
  （eligible）・未実施（pending）・対象外（out_of_scope）を
  分けて報告する
- **ローカルLLMのスロット割当を再編** — llama.cpp は `-np 2` の
  2スロット構成のままだが、論理名を「slot 1 = 背景 / slot 2 =
  リアルタイム」に整理し、wire `id_slot` は 0-based（背景が `0`、
  RT が `1`）。**v1.0.1 と wire の割当が入れ替わっている**
  （旧: id_slot 0=RT・1=背景）。抽出などの背景処理は既定で
  slot 1 に固定し、対話系のために slot 2 を空ける。例外は
  `extract_llm --lend-rt`（全呼出し前に `/slots` を照会し、RT
  スロットが空いている時だけ借用）と `MCS_LLM_SLOT=<N>`（プロセス
  単位のオーバーライド。夜間 semantic drain が slot 2 を使う）。
  形式検出 probe も同じ決定点を通るため、オーバーライドや
  `--slot`/`--lend-rt` が probe 通信にも正しく効く
- **Keychain がロック中でも収集を継続できる** — `auto_login` が
  Keychain 読取に失敗した場合、`~/.mcs/.env` の `MCS_PASSWORD` に
  フォールバックする（`mcs_setup init` が Keychain と併記する。
  平文のため FileVault・物理セキュリティ前提）。再ログイン結果は
  run status と通知の detail に出る
- **`extract_llm` が並列実行できる** — `run_pending` の LLM 呼出しを
  ワーカー並列化（`--all --workers N`、既定はサーバスロット数）。
  DB 読み書きは呼出しスレッド専有で直列化し、形式検出は fan-out 前に
  1回だけ行う。実測で約100件/時→約294件/時
- **夜間の semantic/QC 一括処理** — `semantic_drain --drain` と
  launchd 常駐 drainer（`ai.mcs.extract-drainer[-rt]`、shard 分割）を
  追加。`deployment/scripts/` に定期実行スクリプトの正本を集約

#### 修正した問題（安全性・収集完全性）

- **通知テキストが配送コマンドの制御構文として解釈されないよう
  無害化** — 投稿本文や抽出結果に紛れた `MEDIA:` 等のディレクティブ
  形式が `hermes send` のファイル添付機能を起動しないよう、通知
  組立ての入力境界で無効化
- **削除・編集・添付撤回が現行ビューに反映される** — MCS 側で
  消えた投稿・更新された本文・取り下げられた添付が、保存済み
  記録の現行状態として正しく扱われる
- **履歴収集がページ上限で止まらない** — 上限に達した取得は永続
  cursor と継続ジョブで残りを引き継ぐ。途中失敗しても既取得分を
  保持したまま再開できる
- **返信スニペットの親投稿が既読化されない** — 一覧の抜粋表示だけで
  本文未取得の投稿が既読対象に混入しない
- **履歴ジョブの沈黙ループを解消** — walk 途中のエラーが attempt を
  消費せず 300 秒間隔で無限に再試行し続けた経路を、通常の再試行
  会計に統一（8回で可視な失敗へ）。セッション切れは従来通り
  attempt を消費しない
- **WebSocket プロトコル検証** — CDP 接続を websockets 依存から
  標準ライブラリの RFC 6455 実装に置き換え、マスクされたサーバ
  フレーム等のプロトコル違反を拒否
- **ワーカー分離と絶対期限** — `mcs_transport` で API 取得・添付・
  Chrome 入出力を子プロセスに隔離し、絶対時刻の期限を伝播。
  期限超過は kill+回収し、通信は loopback と許可 origin に限定
- **`init_data` の `--deadline` が実際に効く** — 列挙開始前に
  アダプタへ期限を束縛するよう修正（従来は指定が伝播しなかった）

#### 修正した問題（抽出・意味処理の正確性）

- **canonical projection が読み取り経路を素通りしない** — 無効化・
  形式不正・期限切れの projection が旧来の抽出結果を覆い隠したり、
  逆に新しい壊れた projection が有効な旧 projection を隠したり
  しないよう、「現在使用可能な projection」の判定を
  `current_projection_pred`/`current_projection_id` に単一ソース化。
  統計・シグナル・QC・rollup・drain・依頼候補の全読取経路が同じ
  判定を使う。projection 判定は JSON の object 型・エラーフラグ・
  本文 hash 一致・非無効化・同一 project を全て確認する
- **canonical 有効化の前提欠陥（C01–C07）を修復** — projection 境界の
  message_id 型正規化、target 別の v2_doc 隔離、coverage 不完全時の
  有界再試行、評価済み監査のみ再利用、`finish_reason=length` の
  中途生成の拒否、現行版 projection の一意化と rollup 連動、
  care_event の型付きゲート
- **QC 監査と抽出の世代ずれを防止** — QC の realtime 窓判定を
  source_artifact_id への世代束縛に変更し、監査実行時に抽出が
  置き換わっていた場合のずれを排除
- **抽出の証跡（evidence）を本文・出所に束縛** — semantic-facts の
  evidence が参照元 message・revision と一致し、atom/body の
  範囲内に収まることを検証。本文に無い根拠や出所の違う根拠は
  採用しない
- **同じ文面でも内容が違えば「重複」にしない** — 事実の関係判定で
  EXACT_DUPLICATE は臨床属性・時間属性が完全に一致する場合のみ。
  文面が同じでも属性が異なるものは UNRESOLVED として残し、
  mandatory render でも fact_id 単位で保持（同文面の別事実は
  主語・時刻・ID 注記つきで両方表示）
- **空白だけの薬名・症状を捨てる** — 抽出結果の検証で空白のみの
  `meds.name`/`symptoms.text` を棄却し、棄却数を `_items_dropped`
  に計上
- **抽出ベンチの採点に依頼（requests）を追加** — 期待・禁止の
  フィールド適合率・再現率に requests が含まれ、精度退行を
  検出できる
- **semantic_shadow_e2e を単独実行できる** — `_mcs_path`
  ブートストラップを追加し、リポジトリ外からの実行でも flat
  import が解決される

#### 新しい運用機能

- **`mcs_view qc`** — extract_qc の集計（評価済み/未評価/判定別/
  urgency 不一致/未処理）と要注意メッセージ一覧、`--message-id` で
  項目別判定の全履歴を表示。判定値の日本語凡例つき（NO_MATCH は
  「事実が存在しない」ではなく「本文に裏付けがない」の意味）。
  本文が変わって古くなった QC 行は集計・一覧から除外
- **`brain_export`** — 公開 snapshot から患者 rollup 由来の要約を
  外部知識ストア向け Markdown として生成（read-only・atomic・冪等。
  message 本文は含めないが PHI を含むため同期先の権限管理は運用側）
- **`semantic-facts v2`** — canonical 抽出・義務レンダリング・
  semantic QC・v2→legacy projection・coverage 監査。`--shard`/
  `--slot`/`--lend-rt`・常駐 poll・oldest_first 対応
- **`semantic_metrics` / `semantic_observe`** — 履歴と現行世代の品質を
  分離した集計と、read-only snapshot を使う日次確認コマンド
- **install.sh が hermes-agent を自動導入** — 未導入なら pin 済み
  commit を clone・venv 構築・plugin リンクまで実施。pin 不一致は
  警告のみ。`keychain_to_env.py` で Keychain→.env の同期も可能

#### 内部構造・開発者向け

- **`mcs/` を第一層サブディレクトリに再編**（44ファイル） —
  `core/`（ledger・local_llm・maintenance・init_data・mcs_util）・
  `ingest/`（adapter・transport・job_ops・notifier・run_check）・
  `extract/`・`semantic/`・`ops/`。`import ledger` 等の flat import は
  `_mcs_path` ブートストラップで維持
- **CI ゲート強化** — `ci/gates.py` に失敗履歴由来の静的ゲート8本
  （stdlib 限定・platform 直 API 禁止・plugin sandbox・snapshot
  read-only 契約・writer flock・fail-closed 通知・install pin・
  records 健全性）。`ci/mine_gates.py` が dev-record の FIX-/AUDIT- ID
  とゲートのカバレッジを照合
- **依存は標準ライブラリのみを維持** — websockets 依存を除去。
  テストは全て一時 DB + 合成 fixture（実 MCS・Discord・Keychain・
  ローカルLLM・Jev へアクセスしない）
- **検証** — pytest 886 件全通過・ruff clean・CI ゲート 8/8・
  mine_gates・README 生成物 drift なし

#### Docs

- README を非エンジニア向けに全面改訂 — 平易な導入部・
  「MCS データで何が追えるのか」「蓄積・集計できるデータの全体像」
  「まだ取れないもの」の明示、図を SVG 化、用語を「チャットルーム」
  に統一、QC 対象範囲（直近60日）を明記
- `docs/semantic-facts-v2-rollout.md`・監査記録（AUDIT-J03・
  review-20260923）・launchagents の手順書を追加・更新

全差分：[v1.0.1 → v1.0.2](https://github.com/yusuketakuma/hermes-mcs/compare/v1.0.1...v1.0.2)

</details>

## [1.0.1] — 2026-09-22

**薬・依頼・長文の抽出を改善し、品質監査と統計の回帰確認を追加**

患者本人と家族の薬、服用中・中止・予定、否定の区別を加えました。本文の根拠を保持し、長文や返信の文脈も抽出に使います。

### 新機能

- **抽出結果の品質監査**
  `semantic.extract_qc: "annotate"`でJevの裏付け確認を別記録へ注記します。任意・既定オフで、抽出結果自体を変更・抑制せず、既存予算・停止条件を維持します。

- **合成ケースで抽出精度を計測**
  抽出ベンチで14件の合成ケースを項目別に採点できます。ローカルLLMが必要で、オフライン確認には`--mock-ok`を使います。

- **承認済み統計との比較**
  人が承認した統計を基準に、後日の結果を再計算して突き合わせます。コード変更による結果差と、データ変更による差を区別し、承認は既存の人確認コマンド経路で行います。

### 改善

- **誰の・どの状態の薬かを区別**
  患者本人と家族、服用中・中止済み・計画中、否定言及を分け、家族や中止済みの薬が現在の薬へ混ざるのを防ぎます。

- **本文の根拠と依頼者・期限を保持**
  抽出項目に本文中の根拠を記録し、本文にない根拠の項目は破棄します。依頼には依頼者と期限を付けます。

- **長文と返信の文脈へ対応**
  3,000字を超える本文も全文を抽出対象にし、親投稿と直近返信を参照します。

- **サーバに応じた出力方式と文書を整理**
  JSON schema強制の対応状況に応じて出力方式を切り替えます。READMEへデータ活用範囲を追加し、図とAGENTS.mdも整理しました。

### 不具合修正

- **長文の一部失敗を完了扱いする問題**
  分割抽出の一部が失敗した場合はメッセージ全体を失敗として再試行し、不完全な成功結果を残しません。

### 動作・設定の変更

- **旧抽出結果は新しい抽出の成功時に置換**
  旧v1抽出はv2成功時に自動的に置き換わります。失敗履歴は保持します。

### 更新時の注意

- 既存の運用操作・設定は継続でき、手動移行は不要です。

- 品質監査を使う場合だけ`semantic.extract_qc: "annotate"`を設定してください。既定はオフです。

### 技術詳細

<details>
<summary>内部仕様・根拠・公開当時の詳細を表示</summary>

構造化抽出（extract_llm）の精度向上と、統計の回帰確認ワークフロー。
既存の運用操作・設定はそのまま使え、移行作業は不要。

#### 抽出結果が変わるもの

- **薬の抽出に「誰の・どの状態の薬か」が付く** — 各項目が
  患者本人の薬か・家族など他人の薬か（`subject`）、現在服用中か
  中止済みか計画中か（`status`）、否定言及か（`negated`）を区別する。
  その結果、通知・患者ロールアップ・薬関連統計で「家族の薬」や
  「中止した薬」が現在の薬として表示・集計されなくなる。
- **抽出の各項目に本文中の根拠箇所（`evidence`）が記録される** —
  通知や台帳の確認時に「どの記述から拾ったか」を照合できる。
  本文に無い根拠をでっち上げた項目は自動で捨てられる。
- **依頼の抽出に「誰からの依頼か」「期限」が付く**
- **3000字を超える長文も全文が抽出対象になる** — 以前は先頭しか
  見ていなかったため、後半に書かれた薬・症状・依頼も拾う。
- **スレッドの親投稿・直近返信を文脈として参照する** —
  「はい、大丈夫です」のような返信単体では意味が取れない投稿の
  抽出精度が上がる。

#### 動作が変わるもの

- **ローカルLLMサーバの能力に応じて出力方式を自動選択** — JSON
  スキーマ強制に対応したサーバならそれを使い、非対応なら従来方式へ
  自動で降格・復帰する。設定変更は不要。
- **途中で失敗した抽出を成功扱いしない** — 長文の分割処理の一部が
  失敗した場合、そのメッセージ全体を失敗として再試行する
  （以前は中途半端な結果が「完了」として残り得た）。
- **v1 の旧抽出は v2 が成功した時点で自動的に置き換わる** —
  手動の移行操作は不要。抽出失敗の履歴は残る。

#### 新しい運用機能

- **抽出結果の QC 検証（任意・既定 OFF）** — config に
  `semantic.extract_qc: "annotate"` を設定すると、抽出済み項目を
  Jev が裏付け確認し、判定を別 artifact に注記する。抽出結果自体は
  変更・抑制されない。日次予算・一時停止など既存のガードは全て
  そのまま効く。
- **抽出精度ベンチ** — `mcs/extract_bench.py` で合成ケース14件に
  対するフィールド別の適合率・再現率を計測できる（要ローカルLLM
  サーバ。オフライン検証は `--mock-ok`）。
- **統計の承認済み参照セット検証** — `mcs/mcs_refstats.py` で
  「人が確認して承認した統計結果」を基準として保存し、後日の
  snapshot に対して `verify` で再計算・突合できる。統計コードの
  変更が結果を変えていないか（regression）、データが変わったか
  （drift）を区別して報告する。承認は既存の `--confirm-human`
  コマンド経路のみ。

#### Docs

- README に「MCS データで何が追えるのか」節を追加、図を mermaid
  から SVG に差し替え
- AGENTS.md を最小限の指示に整理

全差分：[v1.0.0 → v1.0.1](https://github.com/yusuketakuma/hermes-mcs/compare/v1.0.0...v1.0.1)

</details>

## [1.0.0] — 2026-09-21

**hermes-mcsとして独立し、収集・検索・解析・通知の基盤を集約**

mcs-adapterを独立リポジトリに整理した最初のリリースです。以下の機能は、この版で新規開発したものに限らず、既存機能を集約した内容を含みます。

### 新機能

- **MCSの未読収集とDiscord通知**
  この版では15分間隔で未読を収集し、構造化結果と原文を2段階で通知します。

- **全履歴の保存と検索**
  履歴取得はページ位置から中断再開でき、全文検索・日本語の空白を無視した部分一致・患者タイムラインを利用できます。

- **構造化抽出と患者単位の集約**
  ルールとローカルLLM（Qwen3.5-9B）で抽出し、患者別の集約・読み取り専用統計・6種類のレビュー候補を提供します。

- **人による承認と候補の却下**
  依頼管理は`--confirm-human`・理由・操作記録の経路で実行します。候補の却下、承認済み閾値ポリシー、既定7日の通知クールダウンを備えます。

- **導入設定と環境チェック**
  `mcs_setup.py`でKeychain・config・.envの初期設定と診断ができます。DiscordコマンドpluginとHermes統合テストを維持します。

### 改善

- **実行コードとテストを分離**
  `adapter/`を`mcs/`へ移し、テストを`tests/`へ分離しました。開発記録を集約し、launchdテンプレートを配置します。

- **開発の検査と文書生成を統一**
  GitHub Actionsのlint・test・文書同期・hygieneを備え、ruff・pytest設定とMakefileをまとめます。ライセンス・安全性・エージェント向け文書も追加しました。

### 動作・設定の変更

- **コード配置と独立リポジトリへ移行**
  旧`adapter/`配置から`mcs/`へ変更しています。旧パスを参照する独自スクリプトは、この版の配置と照合してください。

### 更新時の注意

- 初回の独立リリースです。既存mcs-adapterから移す場合は、実行パスと導入設定を確認してください。

- この版の収集間隔は15分です。後続版の運用間隔と区別してください。

### 技術詳細

<details>
<summary>内部仕様・根拠・公開当時の詳細を表示</summary>

`mcs-adapter` から `hermes-mcs` として独立リポジトリ化し、内部構造を
シンプル化した最初のリリース。

#### Layout
- `adapter/` → `mcs/`（実行モジュール37本）、テスト49本を `tests/` へ分離
- `hermes_plugin/`（/mcs Discord コマンド）、`integration/`（hermes E2E）を維持
- `docs/dev-records/` に開発記録を集約、`deployment/launchagents/` に
  launchd plist テンプレート3種を追加

#### Features (既存機能の集約)
- 15分間隔の MCS 未読収集 + Discord 通知（構造化→原文の2段投稿）
- 全履歴アーカイブ（ページカーソルで中断再開）
- FTS5 全文検索 + 日本語空白無視の部分一致、患者タイムライン
- 構造化抽出: ルール `extract_v1` + ローカルLLM `extract_llm`(Qwen3.5-9B)
- 患者ロールアップ、読み取り専用統計、レビュー候補シグナル6種
- 人承認の依頼管理（`--confirm-human`+`reason`+receipt）
- 人による却下（`signal_dismiss`）と承認済み閾値ポリシー（`signal_policy`）
- 通知クールダウン（既定7日）、deadline 部分実行の安全側 resolve 抑制

#### Infrastructure
- `mcs/mcs_setup.py` — init（Keychain/config/.env プロビジョニング）+ check
- `scripts/update_readme.py` — README モジュール表の自動生成
- GitHub Actions: lint-test / readme-sync / hygiene
- `pyproject.toml` で ruff・pytest 設定を一元化、`Makefile` 追加
- `LICENSE`（proprietary）・`SECURITY.md`・`AGENTS.md` 新設

</details>
