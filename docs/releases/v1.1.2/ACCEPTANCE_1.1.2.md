# v1.1.2 受入準備票 — 未公開

この票は v1.1.2 の変更記録と説明資料を現在の候補へ準備した作業の記録です。候補全体の合格、本番適用、公開の完了を示すものではありません。

## 今回の対象

- `changes/` 直下76件（後続mergeで3件追加＋恒久fixで7件追加）の記述を現行実装へ整合させ、`release_notes.py build` で `changes/archive/1.1.2/` へ移動。
- 個別の履歴抽出・意味解析への新規クラウド送信は停止済みで、新規送信は患者コンテキスト（作成・更新・品質修復）専用という現在の仕様に文言を合わせた。停止前の予約と課金済み回答の回収経路は存続。
- 説明図のSVG・PNGを現在の仕様で再生成し、描画結果を確認。記録は `visual-source.json`。
- CHANGELOG の v1.1.2 節を生成。README のリリース案内を同じバージョンへ更新。

## 検証済み（この作業の範囲）

- 変更記録の形式：`release_notes.py check` が全86件を受理（構造・refs・技術詳細の要件）。
- 変更記録の内容：閉鎖済み個別クラウド経路・休眠回収・Slack通知先分離など、現在の実装との静的照合。
- 図版：PNGを描画して文字欠落・重なり・余白を目視確認。細い表示幅（390px想定）で可読。
- README 生成ブロック：`update_readme.py` による再生成との一致（driftなし）。

## 上位工程が保持する受入記録（未再実行）

- 全体 suite：9633 passed + 91 subtests passed / 25 fail / SDK専用 2 skip（実測 `2220b03808c5bc1068d77b3bed6a4b1601eb3bef`・exit 1・source_stamp_changed=[]、ルート供給値）。
- 105-file batch（`cb0` 固定・実終了）：exit 1・3016 passed / 24 failed / 0 skip / 2693.81秒・source_stamp_changed=[]・clean。元25 failは全解消（semantic HTTP含む）。当該node `test_view_reads_only_quoted_bodies_without_reextracting_history` と追加5件（marked厳格照合・consumer write失効・rollback・SQLite失敗・制御例外伝播）が実logで PASSED。
- 既知24 failの末端traceは全て修正 `6129d37a` の原因と整合し、`f7503e5a` 固定の13file統合batch（24failの3file＋通常JOIN境界＋readiness witness/cursor/read境界＋updater2file＋update candidate＋schema/update origin3file）で全解消を確認（exit 0・1072 passed / 0 failed / 0 skip・約602秒・source_stamp_changed=[]・clean、ルート供給値）。lint 0・gates 10/10・mine 0・manifest digest `9726b4c1` 一致もルート確定。
- SDK別lane42件（standalone13・Hermes29）：実行SHAは `f11384da`。SDK対象bytesは `f113..cb0` で不変とルート確認済みで再利用可（旧 `b963` を実行SHAと記録しない）。
- 失敗nodeの最終照合：過去suffix65・combined25・followup24の失敗nodeを最新JUnit3本とrename対応で照合し、108 unique failed nodesすべて最新PASSに対応・未対応0（証跡 `observed-failure-resolution-current.json`、ルート供給値）。単一の全体suite全pass・CI成功とは記録せず、ユーザー指示（2026-10-31）の失敗修正＋影響範囲再検証による合格判断の根拠として使用可能。製品・テスト・資産は `6806843a` と `f7503e5a` で差分0をルート確認済み。
- 公式医薬品マスター：`drug-master-fresh-check.json`（2026-11-07）で同梱 bundle と一致確認済み（`matches_bundle: true`、出典・hash・行数記録済み）。有効化は要オペレーター承認で未実施。
- 本番適用中の新たな原因と暫定復旧：`ac2978a8` 適用が `services_failed` exit 1 で postmerge 未完了となり、`mcs/ops/mcs_setup.py` の import 順欠陥（共有部品の読込みが標準起動準備より前）が原因と `_services_reconcile` の直接起動経路で因果一致（ルート供給値）。既存の recover 処理と `_mcs_path` 公式 sys.path を一回限り PYTHONPATH へ渡す補助で暫定復旧し、`current_sha=ac2978a8`・`applying=null`・`last_applied=ac2978a8`・`stages=[]` を実照合（失敗試行は履歴として保持・完了偽装なし）。
- 恒久 fix：commit `6977b195`（同原因の import 順復旧＋直接起動回帰・限定 batch 77 passed / exit 0）。当該ソースは後述の候補へ統合し、本番適用記録を分けて追記。
- 恒久 fix 追加：commit `44f70dc1`（Slackチャンネル準備の保留中に無関係な設定を更新すると再開不能になる問題を修正・限定 batch 115+285+114 passed / exit 0・実行logは端末表示のみでファイル未保存）、`8aca1692`（添付OCRの起動時に保存ロックの解放を待つ・独立レビュー受入・ルート証跡：旧製品+新test赤 1 fail/exit 1・修正版 entry 12 passed/exit 0、`.mcs-audit/20261107/ocr-startup-lock/`）。merge `75c041d6` で統合。当該ソースは後述の候補へ統合し、本番適用記録を分けて追記。

## 本番適用と稼働観測

- `8b98f126` を通常の固定候補アップデーターで適用し、適用終了0・適用記録と実SHAの一致・`applying=null`・直接起動の診断終了0を確認。起動準備の恒久修正 `6977b195` を含む。
- `429b646c` は2026-11-07 09:20 UTCに適用終了0。Slack準備の設定差分回復とOCR起動時のロック待機を含み、適用記録・実SHA・診断を照合。Slack権限の不足は残っており、作成要求は再送していない。
- 保存ロックの拒否理由を既存の固定コードで記録する修正 `6df26c2b` は独立レビュー受入。core・OCR起動経路の21件と共有の拒否診断20件が成功し、ログとJUnitを私有監査領域に保持。配布証跡を生成した `dcd97683` を09:51 UTCに適用終了0、適用記録・実SHA・`applying=null`・直接起動の診断終了0を照合。生成証跡の独立照合も完了。
- OCRの有効化と既存の毎分ジョブを確認。原資料との一致を読取時に検証したOCR完了7資料と空結果3資料を観測した。これは医療情報の読取精度やOCR後の解析完了を保証するものではない。
- クラウド設定を既存の設定編集APIで `routes=[patient_context]` へ明示し、他設定と日次・月次150 USDの予算を保持。実設定のクラウド動作はONで、観測区間の個別投稿向け新規クラウド予約は0件。既存の不明な会計・送信記録は変更していない。
- Slack添付の未完了40計画は、新しいハッシュでは不一致、旧8項目の正確な計算では全件一致し、新2項目は欠落・集合の試行記録も未作成であることを読取りで照合した。互換修正の最終 `63e24ff1` を通常mergeし、重複する追補部分はレビュー済みのblobと同一に解決。本番への適用と配信結果は未確認。保存済み計画や結果不明の記録を書き換えていない。

- 通知互換修正の合成検証は353件すべて成功（`~/.local/state/devin-audit/notify-legacy-filegroup-20261107/final-batch.xml` をルートで読取）。その後の非Slack旧形式拒否テストの非真空化は対象1件を再検証し成功との担当報告。合成DBで旧形式のheld計画を既存の人承認APIへ通し、現行ハッシュへ再封印される回帰も含む。
- OCR診断の段階表示 `7b68e512` は独立レビュー受入。各処理の固定値 `writer_job_stage=local|ocr` を最後の拒否と一緒に記録し、片方の成功で前の拒否を消さず、両方の拒否は後の段階へ帰属させる。OCR起動経路15件は終了0・失敗0・skip0（`phase-entry.xml`）。処理予算・待機時間・モデル・保存と送信の判定は変更しない。

- OCR保存待ち修正 `daabf2cc` を通常mergeで統合。関連2fileは98 passed・exit 0（Devinの端末出力報告、log/XML未保存、親は未再実行）。5秒以下の待機ごとの期限・世代・設定検証、取得後の期限・原資料検証、固定失敗理由だけを保存する条件を静的レビュー。コミット本文に残る「1回のwait」は旧案の説明で、最終実装と変更記録は5秒上限の反復。
- 本番 `f931e6e6` は公式更新が2026-11-07 10:19:24 UTCにexit 0で完了し、直接起動のsetup checkもexit 0。更新後のOCRは保存成功2件と実行失敗1件を観測したが、OCR後の解析と患者コンテキストは未完了。新しい保存待ち修正はまだ本番未適用。
- 通知の旧形式40件は全件でカード本体の配送済みと添付の結果不明をjournalで確認。検証可能になっただけで配送完了とは扱わず、結果不明を自動再送しない。

- Jevロゴ表示の修正を統合。Devinの製品差分をCodexがSHA基準でレビューし、利用上限による単独引継ぎ後に旧期待値と実SDK回帰を整えた。固定Discord SDK 2.7.1とpytest 9.2.0を使う関連18fileは421 passed・0 failed・0 skipped・exit 0。証跡は私有の `jev-icon-codex/final-batch.xml` とlogへ保存。本文に同じヘッダー文字列があっても画像化せず、実際のヘッダー由来だけへ画像を付ける。
- 本番 `874ad3fc` はコード適用の記録が一致し、applying=null・stages=[]・clean、直接起動のsetup checkはexit 0。ただし更新後のSlackチャンネル準備は `slack_setup_scope_required` で未完了のため、更新wrapper全体はexit 1。全体成功とは扱わない。
- 同版で自然実行したOCRジョブは保存8件・空資料2件・実行失敗1件・OCR先送り0件・ローカル解析完了1件・先送り1件。解析結果は現行の読取り側がreadyとして採用し、個別の追加クラウド処理を要求しないことを実測した。スナップショット更新待ちと失敗は未解決で、Jevロゴ修正は本番未適用。

## この資料では確認していない事項

- GitHub CI全ジョブの実測成功。ローカルの必須ゲートと失敗項目の再検証とは区別する。
- 最終候補SHAの固定と、統合済みソースに対する source manifest の検証。
- OCR後のローカル解析と患者コンテキスト作成の完了、既存の通知保留の回復。新しいOCR診断とSlack旧形式互換修正の適用後に実処理で観測する。
- GitHub 上での画像表示、Release本文・asset・tag・公開。画像はリポジトリ内相対参照で保持し、外部表示は未検証。
- Git への push・tag・Release 公開（ユーザーの公開指示は受領済みだが、運用受入待ちで未実施）。

最終リリースSHAは未固定です。観測時のHEADと未コミット変更は `visual-source.json` に記録しますが、リリース候補の固定や受入を意味しません。
