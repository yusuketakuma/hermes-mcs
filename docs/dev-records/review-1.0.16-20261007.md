# 1.0.16全体レビュー（2026-10-07）

対象は `develop/1.0.16` のmainからの差分と未commit変更。ローカル実装と合成検証であり、push・リリース・配備・実サービス再起動はしていない。

## 全件修正済みの指摘（同日追補）

以下はレビュー時に確認した修正前の挙動。オーナーの「全て修正」により3件とも修正・合成検証を完了した。

### P1: canonical projectionがあると緊急再通知が抜ける（修正済み）

`mcs/notify/notify_urgent.py:_current` は `current_fact_pred` でprojectionを優先してからurgencyを読む。projectionにurgencyが無い場合、hash-current LLMのhighは候補から除外される。一方 `structured_view.message_urgency` は同LLMへfallbackするため、画面のhighと再通知判断が一致しない。canonical_projection / semantic_facts_v4を使った完全合成2ケースで再現。既存test_notify_urgentはprojection自身へurgencyを入れているためこの欠落を捉えない。根拠: `notify_urgent.py:_current` / `structured_view.py:message_urgency`。

修正後は候補選択でも現行LLMを除外せず、表示と同じselectedfact verdict／LLM fallbackを選ぶ。根拠artifact ID・時刻を保持し、E1/E2／送信直前のhash・scope・削除・保管状態・receipt条件を維持。対象82件と近隣284件、統合通知suiteで確認。

### P2: 所有gateway判定が非Hermes実行ファイルを受け入れる（修正済み）

`deployment/recovery/mcs_recover.py:_gateway_restart_run` はProgramArgumentsにgatewayがあるだけでnative Hermesとみなす。固定Label/plistパスを持つ `/bin/echo gateway` でも起動要求とsupervisor_restart_verifiedへ進むことを、subprocessを完全にスタブ化した合成ケースで再現。固定ラベルの設定が別サービスへ変わった場合に所有者確認が不足する。実機にこの条件があるとは確認していない。根拠: 同ファイル `_gateway_restart_run`。

修正後は既知のHermes console／venv Pythonの正規位置・module・gateway run引数とJXA内部コマンドを非実行で確認する。明示custom HERMES_HOME／WorkingDirectoryに束縛した標準形状は維持し、未知形状は失敗報告。Program上書き・echo・別module・混在コマンドを拒否し、PID証拠・restore holdを維持。45合成ケースと凍結子のPython3.9実行、統合復旧suiteで確認。

### P2: 旧タグの手動下書き作成が新bundle検証で失敗する（修正済み）

`.github/workflows/release-notes.yml` は指定タグのcheckout後に新しいcheck_drug_master_bundle.pyを無条件実行する。既存v1.0.15にはこのファイルがなく、旧タグのworkflow_dispatch下書き経路が停止する。git内の既存タグのtreeを読取り確認した。外部Actionsの実行はしていない。根拠: 同workflowのrequested tag準備・draft検証段。

修正後はcheckerが存在すれば版にかかわらず検証を要求。欠落互換はv1.0.0〜15かつbundle領域無しだけに限定し、新版・bundle有り・切れたsymlink・checker失敗はexport前に停止する。従来の生成器を持つv1.0.15の旧経路を維持し、公開権限・正本・CIpinを変更しない。

## 今回修正したレビュー指摘

### Slack / LINE WORKSのPNGと生成元が一致しない

開始時はgallery生成器の--checkで既存SVG変更のPNG未反映を再確認した。今回の本人限定患者まとめの背景欄を説明図に反映し、Slack7画面・LINE WORKS2画面のSVG／PNGを生成器とローカルQuick Look／既存ImageMagickで同期。全9画面の文字切れ・重なり・説明を目視確認し、両生成器の--checkが成功。完全合成の説明図であり、実画面・実SDKの検証ではない。

### 長文chunkの通常判定

長文chunkのroutine判定がany-routineだったため、他chunkのurgencyが欠けても通常と確定していた。既存の仕様に合わせall-routineへ修正し、ルールの高判定を欠落chunkが抑止しない合成回帰を追加した。

## 全患者の既存機能拡充

- ルール／ローカルLLMの既存抽出に16分類patient_contextを追加し、原文値・引用・主体・出典を保持。
- 既存連携サマリーGETをdeep runで24時間経過後にbounded refreshし、最後の成功内容を保持。
- rollup／本人限定の患者まとめ／evidence／timeline／read_modelを拡充。共有本文には詳細を追加せず、aggregateでは原文を返さない。
- DB schema・人承認・既読化・既定LLM世代／token数・依存は維持。

ローカルsnapshotの全文取得済み17,653投稿を読取り評価し、明示情報がある186投稿から378項目を抽出、一意な原文引用378項目を照合した。患者名・原文・値は報告・fixture・外部へ出していない。これは分類の正しさ、自由文の網羅性、LLM精度を測定した値ではない。昨日の詳細投稿候補の書式も同じローカル読取りで確認した。

## 検証・制約

確認領域は収集/core、extract/semantic、薬剤master/辞書/統計、通知と3adapter、views/export、ops/installer/updater/recovery、standalone/deployment、CI・文書。実SDK・実API・実LLM抽出精度・実画面・実負荷は未検証。過去タグの配布物を使った全経路・外部CIの検証を今回のローカル成功から推定しない。

- `scripts/run_tests.sh tests/`（隔離Python、cache無効）: 8,619 passed、3 skipped、26 subtests passed。スキップはaiohttp未導入のSlack runtime互換1モジュール、discord未導入のDiscord runtime互換／実SDKテスト2モジュール。実SDK成功とは扱わない。
- 初回一式で捕捉した二重JSON decodeを共有rollupで解消。新しい背景欄の追加に伴う表示テストは対象の節だけを検査するよう整合し、callback付きrollbackのスタブも新しい内部契約へ整合した。編集途中に検知したcode_changed・旧karte_id読取り・生成文書driftの失敗はコード固定後に再確認し、最終一式が成功した。
- 最終の本人向け文言の関連87件、連携サマリー取得／構造化／detail公開／復旧統合を含む99件も成功。
- CI範囲ruff、ci/gates.py 10/10、mine_gates、生成README／release記録／リンク、git diff --check: 成功。
- Slack7画面・LINE WORKS2画面のgalleryチェックと完全合成画像の目視確認: 成功。

上記P1／P2の未修正3件は同日追補で全件修正済み。実DBに構造化結果を書き込む実機適用はしておらず、実LLMの抽出精度・網羅性は未評価。今回の実データ利用はローカルsnapshotの読取りだけ。

### 全件修正後の最終検証

- 通知／release全域: 836 passed、34 subtests passed。
- gateway／独立復旧／Python3.9／updater／rollback／standalone／復旧統合: 348 passed。
- 計1,184件成功。変更していない領域は直前のテスト一式8,619成功・3SDK skipを再利用。
- CI範囲ruff、gates10/10、mine_gates、変更記録、生成README、README release同期／リンク、diff checkが成功。
- 実サービス再起動・GitHub draft作成・外部公開・本番データ変更は未実施。

追加の収集候補は `docs/specs/patient-context.md` の提案表を参照。既存の薬剤・検査／観測値・背景・依頼の詳細属性を拡充する方針で、追加収集や新API接続を今回実施した記録ではない。

## 全情報拡充の最終受入（同日追補）

ユーザーの「全てを拾えるようにアップデート」に対応し、指定5情報と共通provenanceを19分類・27原文属性で既存の保存／再処理／読取り／本人向け要約へ接続した。ルール12・LLM6／詳細2・投影3へ更新。旧結果の通常再処理、nested quote／subject／audit拘束、実length時のlayout細分化・checkpoint再開、見出しなしの情報を語彙で除外しないprefilter、canonical v3/v4の同じ属性処理を実装した。

登録薬剤・観測項目・値の巡回はclinical_metadataの明示選択で有効化（既定false）。12GET／25秒のbudgetとdeadlineを維持し、同timestampの大量値は非公開stageで次deepへ再開。終端と総数が整合するまでcompleteとして保存しない。最大250項目／1項目1万行、容量超過は未完を明示。破損／TS／総数／mapping／定義差分は旧stageを再利用せず、1項目失敗で他の項目や患者を止めない。原文・属性・履歴と未分類メモ原文はdetailへ保持し、aggregateは具体値や引用を含めない。

- tests/一式: **8,808 passed、3 skipped、34 subtests passed**。SDK依存未導入のスキップは従来の3モジュールで、実SDK成功とは扱わない。
- 一式開始後に追加した自由文メモのdetail限定保持を含む最終reader14件も成功。
- 取得／staging／getter:300行全取得、1万行境界、容量10001、全250項目巡回、失敗／normal hold／2患者公平性／checksum・policy・定義pin・TS／対象差分を合成確認。
- 出力length停滞／旧世代失敗budget／途中JSON／paid checkpoint再開／parallel独立layout／全文coverage、nested属性の全key、family／other／past／planned／condition、v4 PASS公開と監査／doc_hashを合成確認。
- CI範囲ruff、gates10/10、mine_gates、生成README／release記録／README同期／リンク、diff check、Slack／LINE WORKS gallery整合は成功。追加した患者まとめの完全合成説明図は目視確認した。

実API・実LLMの精度／網羅性・実機設定変更・配備・本番データ変更は未実施。対象項目を対応したことと、実モデルがあらゆる表現から100%検出できることを同一視しない。未記載情報や未知API項目を推測で補わず、保存済み原文と未取得・未確認・未分類の状態を保持する。

## 詳細投稿の実測で判明した再処理の修正

読み取り専用snapshotの対象投稿1件（本文1,805文字）を私有一時DBに取り込んで原文/hash一致とルール抽出7項目を確認した。実MCSへの登録欄GET2件は成功・登録0行だった。登録欄の空をチャットに医療情報がないことと扱わない。ローカルLLM初回は14呼出・length3回・修復6回、881.3秒で保留となり、全文の構造化結果を確定保存できなかった。患者名・原文・実値を文書・fixture・外部サービスへ出していない。

原因は1区間のlengthごとに全文を小さいlayoutへ変更して、完了区間まで再推論する処理。失敗区間だけを再分割し、初期chunkと子pathに束縛した分割履歴・完了結果を既存非公開checkpointへ保存するよう修正した。各pieceと修復へ渡す候補も当該区間に限定し、小区間に全文thin修復を促さない。新しい処理revisionで旧global-hints checkpointを無効化する。全文完了条件・MAX1600・TIMEOUT300・deadline・lease・admission・失敗予算・人承認は維持した。

- extract全域: **959 passed**。完了済み区間（空結果を含む）の同run/中断後再利用、局所再分割、修復length、piece候補、child thin抑止、1文字限界、旧契約無効化、破損と不正metadataを合成確認。
- CI範囲ruff・gates10/10・生成README・変更記録・README release同期・差分検査は成功。未変更領域は直前の一式8,808成功・SDK3skipを再利用。
- 修正後の同一投稿の実LLM試行は下記追補。実試行はローカル専用、一時DB保存だけで、本番DB変更・既読化・通知・配備は実施しない。

修正直後の単発試行は12呼出・length3回・839.9秒で保留となった。完了した前半や後半の区間を再処理せず保持したが、最終区間前に既存の呼出時間下限に達した。これは単発予算内の全文完了の証明ではない。実workerと同じsource-bound checkpointを保存して次の900秒sliceへ再開する追加試行で、最終保存と読み戻しを確認する。

### 修正後の実データ再開・保存確認

同じ対象投稿を私有一時DBに取り込み、実際のローカルLLMと既存checkpoint保存／読取りを使って確認した。初回900秒sliceは843.8秒・12呼出で呼出時間下限に達して保留。分割履歴と完了済み5区間を保存した。次sliceで5区間を再推論せず再利用し、残りだけを3呼出・90.7秒で処理して全文完了した。全体934.5秒、MAX1600／TIMEOUT300／各slice900秒は変更していない。単発予算内の完了や速度改善率を主張する結果ではない。

一時DBに最終artifactを保存し、読み戻しJSONとの一致を確認。patient_context21項目、meds6項目、requests1項目、points3項目、背景の詳細属性2件が保存された。原文・hash一致と既存の引用検証は確認したが、分類の正しさや100%の検出率を人手採点した評価ではない。元のルール結果も一時DBに保持した。

一時DBは検証終了時に自動削除し、本番DB変更・既読化・通知・実機設定変更・gateway再起動・配備は行っていない。本文・名前・識別子・実値は報告・fixture・外部送信へ含めない。
