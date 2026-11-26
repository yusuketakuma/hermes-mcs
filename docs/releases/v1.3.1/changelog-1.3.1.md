## [1.3.1] — 2026-11-26

**要約の再開・薬剤照合・通知の監視を修正**

クラウドで修復した患者要約は、再監査の途中で処理が止まっても、入力と成功した予約を照合できれば続きから処理できます。通知の監視はワーカーの起動記録と本文・添付の配送記録を個別に確認します。結果不明の部品は保留し、手動で配送先と照合できます。起動状態の照会が時間切れになったときは、その理由を表示します。

処方に「カリウム 4、分2」のような単位のない記載がある場合、服薬量を検査値として確定せず、数値を候補として残します。採血の数値に「異常なし」などの所見だけが続く場合は、元の記載を根拠に検査値を確認します。短い引用の後に「未満なら連絡」などの条件が続く場合も、元の投稿を確認し、その基準値を実測値から除きます。薬剤照合に時間が残っていなければ、次回の収集に持ち越します。

AnthropicとOpenAIを合わせた費用上限は、設定を省略した場合、1日80ドル・1か月350ドルです。すでに指定した上限は引き継ぎます。

OCRを導入済みのMacでは、更新時に専用環境のライブラリを固定版に揃えます。Jevを使う設定では、保存前の確認画面で送信される可能性のある対象を確認できます。

![通知ワーカーの起動と本文・添付の配送記録を確認する流れ](https://github.com/yusuketakuma/hermes-mcs/releases/download/v1.3.1/notification-observation.png)

図は通知監視の仕組みを示します。ワーカーの起動時刻だけでは個々の部品の送信完了は分かりません。

v1.2.1から更新する場合は、在宅ケア台帳や医薬品検索の変更も適用されます。詳細は[在宅ケアと医薬品検索の変更一覧](https://github.com/yusuketakuma/hermes-mcs/blob/v1.3.0/docs/releases/v1.3.0/changelog-1.3.0.md)にまとめています。

### 新機能

- **v3 consent wireをcredential storeとpin fileへ接続**
  v3自動更新のconsent確認経路が、enrollment済みの端末credentialとpin配布fileから署名wireを組み立てられるようになりました。enrollment未完了・pin未配置では従来どおりfail-closedで利用できません。

- **v3 contract digestを検証済みCompatibilityから解決**
  v3自動更新のattempt_keyで使うcontract digestを、中央が署名配布する検証済みCompatibility metadataの値から解決するようになりました。digestが取れないtargetはdownload前にfail-closedし、繰り返しdownloadを防ぎます。enrollment・pin未配置の端末では従来どおり先へ進みません。

- **v3 owner tick driverへwire接続を追加**
  v3自動更新の定期tickと同意CLIが、enrollment済みcredentialとpin配置のある端末で中央への署名通信(offer照会・consent確定/撤回など)を行えるようになりました。未enrollment・pin未配置では従来どおりfail-closedで通信しません。

- **v3自動更新のpin配布形式を追加**
  利用者の動作は変わりません。v3自動更新で使う公開鍵pinの配布形式(mcs.auto-v3-pins.v1)と読込み部品を追加し、pinはrelease control rootのauto-v3-pins.jsonだけから読み、networkから取得・更新しません。

### 改善

- **クラウドAPIの合計費用上限の既定値を1日80ドル・1か月350ドルに変更**
  AnthropicとOpenAIの合計費用上限は、設定を省略した場合、1日25ドルから80ドル、1か月75ドルから350ドルに変わります。

### 不具合修正

- **通知の配送記録とworkerの起動を個別に確認**
  待機中の通知とworkerの起動を個別に調べ、配送を確認できなかった理由を監視警報に表示します。

- **OCR警報の受付対象を一覧で確認**
  OCR警報に、受付のやり直しが必要な場合と次の照合を待つ場合を分けて表示します。読取り専用の一覧で未受付の対象を確認できます。

- **ログイン復帰に失敗したときの原因が実行記録に残るようになりました**
  ログインの復帰に失敗した場合、実行記録でChrome起動時の例外の種類、プロファイルに残ったロック、キーチェーンのロックを確認できます。予備の認証情報でログインできた場合にも、検出したキーチェーンのロックを記録します。

- **電解質の服薬量や連絡基準を検査の実測値と誤認する問題を修正**
  単位のない電解質の数値に服薬の回数や飲み方だけが続く場合は、未確認の候補として残します。検査の数値がどのようなときに連絡するかを示す基準であれば、短い引用に条件が含まれていなくても、元の投稿を確認して実測値から除きます。

- **通知操作後の台帳コピー中にも患者コンテキストを保存可能に**
  通知操作後に台帳の読み取り用コピーを作る際、コピーと検証の間は共有ロックを解放します。この間も患者コンテキストや抽出結果を保存できます。

- **コマンド処理が数秒おきに走るたび収集ロックを長く握る問題を修正**
  通知の復旧確認を未処理の部品から始め、正常に確認した後は60秒間隔を空けます。通知コマンドと収集が同じロックを取り合う時間を減らし、時計が戻った場合は次の確認を実行します。

- **通知の配送記録の読取りが時間切れでも通知ワーカーの起動を確認**
  配送記録の読取りが時間切れになった場合は、その理由を表示し、残り時間で通知ワーカーの起動を確認します。送信中の通知で送信試行の記録がない場合や、その時刻に不備がある場合は、ほかの通知が届いても配送状況を確認できないことを警報に表示します。

- **薬剤照合の持ち越しを進捗停止として警報する問題を修正**
  定期収集で薬剤照合を始める時点で処理時間が残っていない場合、次回への持ち越しとして記録します。処理時間があったのに照合位置が進まなかった場合は、引き続き進捗停止を報告します。

- **添付OCRで使うパッケージの版を固定**
  添付OCRを選んで導入したMacでは、新規導入と更新で同じ版の依存パッケージを使い、必要なパッケージが欠けている場合は導入を停止します。

- **リリース下書きの対象をタグのコミットに固定**
  新しいGitHub Releaseの下書きには、タグが指すコミットSHAを設定します。公開処理がタグCIから必要な成果物を取得できない場合は、その名前を表示して停止します。

- **投稿の更新後も過去の抽出記録を確認して処理を再開**
  本文が同じまま投稿者などの情報が更新されたとき、旧版が保存した入力記録のために抽出が繰り返し先送りされる場合がありました。既知の3つの旧版の記録を照合し、以前の投稿状態の記録だと確認できた場合は、現在の入力で抽出を進めます。

- **収集がロック不足で止まっているとき健康レポートが原因を示すようになりました**
  収集が繰り返し実行ロックを取得できなかった場合、健康レポートに拒否の回数と継続時間が表示され、収集開始前の停滞を確認できます。

- **保存前確認の外部Jev追加解析表示を実効条件へ修正**
  semantic.modeが明示offの設定では外部Jev追加解析を無効と表示し、mode未設定でsummary/loopの既定がassistの場合は、Jev key登録時に送信され得る条件付き表示のままにしました。

- **クラウドで修復した要約が中断後に反映されない問題を修正**
  意味解析の要約をクラウドで品質修復したあと、検証が中断（時間切れ・送信不可）されると、その後いくら処理を進めても支払い済みの修復が要約へ反映されない問題を修正しました。スレッドの最初のメッセージの特定を構造的な親子関係で行うようにしたもので、単発の記録など最初のメッセージ自体が処理対象の場合に起きていました。

- **要約の生成元をクラウドの決済記録で確認**
  中断した要約の処理を再開するとき、クラウドへの要求が失敗していても、その後の再試行が成功して決済された記録があれば、要約の生成元を確認できます。

### 更新時の注意

- 更新後の監視から反映されます。追加操作は不要です。

- 受付のやり直しを案内された場合は、通知の復旧手順に沿って対象と計画を確認し、操作者が受付を行ってください。

- 更新後に Hermes gateway（独立実行では mcs_standalone）の再起動が必要です。設定の変更や追加の操作は不要です。keychain のロック解除や設定変更は引き続き行われず、ロックの検出は記録だけに使われます。

- 追加操作は不要です。wireが実際に使われるには、SDC enrollment完了とauto-v3-pins.jsonの配置が必要です。配置しない限り従来どおりconsent確認はunavailableを返します。

- 追加操作は不要です。digest解決にはwire経由のmetadata検証が必要で、release_trust(attestation)配線が未完了の端末では従来どおりfail-closedのままです。

- 追加操作は不要です。wireはSDC enrollment完了とauto-v3-pins.jsonの配置がある場合だけ組み立てられ、無い場合は従来どおりunavailableのままです。

- 現行の動作は変わりません。consent wire実factoryとenrollmentが接続されるまで、この部品を呼ぶ製品経路はありません。配備時は管理者または署名済みrelease closureがauto-v3-pins.jsonを配置します。

- 設定変更は不要です。

- 更新前から合計上限や提供元別の上限を設定している場合は、その値を引き継ぎます。以前の既定値を維持したい場合は、更新前に cloud-llm budget --daily-usd 25 --monthly-usd 75 で指定してください。

- 更新後の通知コマンド処理から適用されます。設定変更は不要です。

- 追加操作は不要です。既存のDBには起動時に小さな記録テーブルが自動で作られます。

- HermesでSlackまたはDiscordに通知している環境が対象です。追加の操作は不要です。

- 追加操作は不要です。更新後の定期収集から適用されます。

- OCRを追加済みの環境は通常の更新時に専用環境のパッケージを入れ直します。OCRの有効化は従来どおり設定が必要です。

- 新しく作成する下書きに適用されます。追加操作は不要です。

- 更新後の抽出ワーカーから適用されます。抽出に関わるソースが変わるため、以前の抽出結果を保持した自動保留は再評価の対象になります。

- 更新後に Hermes gateway（独立実行では mcs_standalone）の再起動が必要です。設定の変更や追加の操作は不要です。

- 追加操作は不要です。保存前確認の表示が実際の有効条件へ合わせられます。

- 適用後の最初の処理から有効です。反映されなかった修復は次の再検証で支払い済みの結果がそのまま使われ、追加のクラウド送信は発生しません。

- 追加操作は不要です。更新後に要約の生成元を再確認する際に適用されます。

### 技術詳細

<details>
<summary>技術詳細・根拠を表示</summary>

#### 通知の配送記録とworkerの起動を個別に確認

- 配送の進捗は停滞判定に使う直近30分の記録を確認します。
- 根拠: mcs/ingest/health_delivery.py、mcs/ingest/health_watch.py

#### OCR警報の受付対象を一覧で確認

- 一覧は添付256件ずつ確認できます。受付証跡を照合できない場合はエラーを返します。
- 受付後も本文を回収できなかった記録は残ります。
- 根拠: mcs/ops/ocr_failure_ack.py、docs/guides/NOTIFICATION_RECOVERY.md

#### ログイン復帰に失敗したときの原因が実行記録に残るようになりました

- Chrome の起動に失敗した場合は例外の種類を記録します。例外本文やプロファイルのパスは記録しません。
- _ensure_chrome は、CDP が落ちているのに停止対象の Chrome プロセスが見つからず、かつプロファイルに SingletonLock がある場合（プロセス一覧で帰属できない保持者または stale lock）を chrome_singleton_lock として記録する。この状態では再起動が吸収されて CDP が立ち上がらない可能性が高い。
- _login_password は keychain のロックを検出した時点で keychain_locked を記録する。.env フォールバックでログインできた場合も残る。
- これらの事実は試行ごとに初期化され、値があるときだけ実行記録に載る。従来の記録（healed_tabs・chrome_restarted）と同じ扱い。
- 根拠: mcs/ingest/mcs_authentication.py、mcs/ingest/mcs_adapter.py、mcs/ingest/collection_stages.py、tests/ingest/test_adapter_runtime_auth.py、tests/ingest/test_run_check_session_recovery.py

#### v3 consent wireをcredential storeとpin fileへ接続

- credentialは使用毎にconnector storeから解決し、pinはnetworkから取得しない。
- release_metadataのattestation trustは未配線のまま。
- confirm応答の保存はcompare-and-swapで行い、送信中のauto-disable撤回をconfirmで上書きしない。pin fileの不備はwire構築時にfail-closedする。
- 根拠: mcs/ops/update_cli.py、mcs/ops/release_auto_trust.py

#### v3 contract digestを検証済みCompatibilityから解決

- 端末はconfig/data契約文書からdigestを計算せず、配備値としても配置しない。
- release_metadataの戻り値にconfig/data contract sha256を追加し、build_depsがwire注入時にdigest解決を自動構成する。
- digest解決をartifact fetch前へ移し、stage不能なtargetの反復downloadを防ぐ。
- 根拠: mcs/ops/release_auto_wire.py、mcs/ops/release_auto_deps.py、mcs/ops/release_auto_owner.py

#### v3 owner tick driverへwire接続を追加

- wire factoryをrelease_auto_wire_factoryへ集約し、consent CLIとowner tick driverが共有する。
- wire構築の失敗はtickを停止させずunavailable wireとしてfail-closedする。
- auto-disableでもwireが組み立てられる場合は中央consent-revokeを送る。
- 根拠: mcs/ops/release_auto_wire_factory.py、mcs/ops/release_auto_driver.py、mcs/ops/update_cli.py

#### v3自動更新のpin配布形式を追加

- 閉じたschemaで検査し、symlink・16KiB超・未知keyを拒否する。
- pinの用途と期限の評価は既存の署名検証側が担う。
- 根拠: mcs/ops/release_auto_trust.py、docs/specs/auto-update-v2.md

#### 電解質の服薬量や連絡基準を検査の実測値と誤認する問題を修正

- 読点の後ろが別の検査項目や薬名を明記した処方の場合は、手前の検査値を従来どおり判定します。
- 「採血 カリウム 4.5」に「異常なし」「経過観察」「次回再検」のいずれかの所見だけが続く場合は、4.5を検査値として扱います。「ドライシロップ1包」など剤形と用量だけが続く場合は、元の数値を未確認の候補に残します。
- 採血の値が読点で並ぶ場合は、血清ナトリウムや補正カルシウムなど、後ろの検査項目まで確認します。同じ文の一連の数値に「未満なら連絡」などの条件がかかる場合は、実測値として確定しません。
- 根拠の引用が数値だけで終わっていても、元の投稿でその後に続く服薬指示や連絡条件を確認します。すでに記録した数値を表示するときにも同じ照合を行います。
- 根拠: mcs/extract/clinical/clinical_values.py、tests/extract/clinical/test_electrolyte_prescription_lines.py、mcs/extract/llm/extract_validation.py、mcs/patients/measurement_sources.py、tests/extract/clinical/test_lab_names_gen12.py、tests/views/test_structured_view.py

#### クラウドAPIの合計費用上限の既定値を1日80ドル・1か月350ドルに変更

- 日・月のどちらかを省略した場合は、その項目だけに新しい既定値を使います。明示した0や上限解除（null）も引き継ぎます。
- 根拠: mcs/llm/cloud_policy.py、mcs/ops/cloud_cli.py、docs/guides/ANTHROPIC.md

#### 通知操作後の台帳コピー中にも患者コンテキストを保存可能に

- コピーの公開前にロックを取り直して台帳と更新状態を確認します。処理中に加わった通知の変更は、次のコピー作成の対象として残します。
- 通知操作の結果をすぐに反映するときは、コピーを始めるために必要な残り時間を450秒から300秒に短縮します。ロック待ちが長引いても、読み取り用コピーを更新しやすくなります。
- 根拠: mcs/ingest/run_check.py、mcs/core/maintenance.py、mcs/core/snapshots.py

#### コマンド処理が数秒おきに走るたび収集ロックを長く握る問題を修正

- recover の走査は delivered 行ごとの相関 EXISTS ではなく pending 部品の駆動に変更（結果は同じ）。
- recover は60秒に1回まで。最終実行時刻は ledger の notify_recovery_state に記録し、再起動後も間隔が維持される。独立実行でも、最後の通知コマンド処理から60秒経過したら、キューが空でも復旧処理を起動する。
- backlog_stalls には checkpoint が渡され、SQLite の外側の処理（ファイル走査・worker 診断）でも観測上限で停止する。
- 根拠: mcs/notify/notify_spool.py、mcs/core/ledger_schema.py、mcs/ingest/health_report.py

#### 通知の配送記録の読取りが時間切れでも通知ワーカーの起動を確認

- 通知ワーカーの起動記録が見つからない場合は、診断結果に witness_missing と記録します。
- 根拠: mcs/ingest/health_recovery.py、mcs/ingest/health_delivery.py、mcs/ingest/health_watch.py

#### 薬剤照合の持ち越しを進捗停止として警報する問題を修正

- 収集全体の制限時間とスナップショット検証のために確保する時間は従来どおりです。
- 根拠: mcs/ingest/run_check.py、tests/ingest/test_drug_map_pipeline.py

#### 添付OCRで使うパッケージの版を固定

- macOS・Python 3.13の任意OCR環境で、固定したYomitoku本体と推移依存を導入し、依存関係を検査します。
- 根拠: deployment/requirements-ocr.lock、scripts/setup_yomitoku.py

#### リリース下書きの対象をタグのコミットに固定

- 手動実行では、指定したIDから正式な受入票を取得する経路が未実装であることを表示して停止します。
- 根拠: .github/workflows/release-notes.yml、.github/workflows/release-publish.yml

#### 投稿の更新後も過去の抽出記録を確認して処理を再開

- 以前の試行の本文や回答は流用しません。記録が一致しない場合は先送りし、課金済みの送信に関する照合も継続します。
- 根拠: mcs/extract/llm/extraction_context.py、mcs/extract/llm/extract_llm_context.py

#### 収集がロック不足で止まっているとき健康レポートが原因を示すようになりました

- writer_lock に run_lock_refusals.json の記録・消去・読取を追加。収集（run_check）がロック待ちに失敗すると1件記録し、取得成功で消去する。30分を超える間隔が空くと連鎖は新しい事象として数え直す。
- health_watch は、連鎖が2件以上・15分以上の幅・直近15分以内の拒否を含む場合に report.run_lock_starved（回数・幅・最終拒否からの経過）を載せ、state_reasons に run_lock_starved を追加する。状態記録が古い場合も、現在のロック拒否と過去の記録を区別して表示する。
- 単発の拒否や古い連鎖では表示しない。抽出ワーカーの単一writeロックは対象外で、収集経路の拒否だけを数える。
- 根拠: mcs/core/writer_lock.py、mcs/ingest/run_check.py、mcs/ingest/health_watch.py、tests/ingest/test_run_lock_starvation.py

#### 保存前確認の外部Jev追加解析表示を実効条件へ修正

- 外部Jev追加解析は、summary/loop/extract_qcのいずれかが有効で日次要求上限がある場合に条件付きで表示します。
- semantic.modeを明示offにした場合は無効と表示します。mode未設定でkey不在のために現在offでも、key登録でassist既定が有効になり得る場合は送信可能性を隠しません。
- 根拠: mcs/ops/setup/setup_wizard.py、tests/ops/test_setup_save_confirmation.py

#### クラウドで修復した要約が中断後に反映されない問題を修正

- 原因は支払い済み修復の検証がスレッドの最初のメッセージを選択用の役割名で探していたことで、最初のメッセージが処理対象のとき役割名が上書きされて検証が必ず失敗していました。
- 検証は構造的な親子関係（parent_id）で特定する方式に変わり、選択の対象指定に影響されなくなりました。
- 根拠: mcs/semantic/semantic_summary_stages.py、tests/semantic/test_semantic_budget_retry.py

#### 要約の生成元をクラウドの決済記録で確認

- 要約の対象と予約を照合し、同じ予約の成功と決済を確認します。結果の重複や失敗の記録だけでは、生成元を確定しません。
- 予約のメタデータが配列や数値などで保存されていても、生成元の確認中に処理が停止する問題を修正しました。
- 根拠: mcs/semantic/semantic_summary_stages.py、tests/semantic/test_semantic_cloud.py、tests/semantic/test_semantic_budget_retry.py

</details>
