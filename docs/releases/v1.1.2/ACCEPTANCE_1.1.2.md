# v1.1.2 受入準備票 — 未公開

この票は v1.1.2 の変更記録と説明資料を現在の候補へ準備した作業の記録です。候補全体の合格、本番適用、公開の完了を示すものではありません。

## 今回の対象

- 既存の88件へ今回の5件を加え、`changes/archive/1.1.2/` に全93件を収録。既存88件のファイルはバイト単位で保持し、今回の作成回数の説明だけ「全資料の処理完了を初めて反映する更新」へ限定した。
- 前回は空の部分概況と走査位置競合の2件を加え、全95件へ追補した。既存93件と追加2件の変更記録はバイト単位で保持した。
- 今回は検証済み投稿の部分作成の修正1件を加え、全96件へ集約した。既存95件と追加1件の変更記録はバイト単位で保持した。
- 個別の履歴抽出・意味解析への新規クラウド送信は停止済みで、新規送信は患者コンテキスト（作成・更新・品質修復）専用という現在の仕様に文言を合わせた。停止前の予約と課金済み回答の回収経路は存続。
- 説明図のSVG・PNGを現在の仕様で再生成し、描画結果を確認。記録は `visual-source.json`。
- CHANGELOG の v1.1.2 節を生成。README のリリース案内を同じバージョンへ更新。

## 検証済み（この作業の範囲）

- 変更記録の形式：既存の形式検証でarchive全96件を照合し、`release_notes.py check` でCHANGELOGの構造・refs・技術詳細を確認。旧95件と追加1件の内容を保持した。
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

- Jevアイコンの統合後、Discordの画像部品だけを該当する表示ファイルで許可し、動的なSDK参照と他のファイルでの使用を拒否する境界を維持した。固定Discord SDKを使う関連20fileは545 passed・0 failed・0 skipped・exit 0。証跡は `jev-icon-codex/gate-fixed-batch.xml` とlogに保存。利用上限に伴う引継ぎ後の追加修正はCodexの自己確認であり、独立レビューとは区別する。

- Jev表示修正の候補 `e4b2f651` は本番へのコード適用記録と実SHAが一致し、applying=null・stages=[]・clean、直接起動の診断終了0、通知worker起動済みを照合した。更新wrapperの終了1は後続のSlack権限不足によるもので、コード適用の失敗とは区別する。
- 運用者のSlack権限変更後、正式な `mcs setup slack-channels` を1回実行し、2026-11-07 11:44 UTCに終了0。運用ログ先の作成証跡・操作者の招待確認・設定保存・後処理完了を照合し、患者情報のテキスト先とカード先が変わらず、運用先と分離されていることを確認した。全運用通知種別の宛先と送信profileの導出も一致。確認のための外部投稿は行っておらず、実投稿の到達は未観測。
- OCR失敗の固定段階診断 `a9275e32` は関連2fileの合成検証113件が成功・終了0。画像の寸法上限を読込み前に適用し、制御例外と既存の終了コードを維持する。log/XMLは私有の `ocr-failure-phases/final-batch.*` に保存。実機の内部失敗と出力形式拒否の原因は未確定で、診断追加による解消を主張しない。この追補時点で当該修正は本番未適用。

- OCR固定段階診断を含む `d621f96d` は2026-11-07 11:55 UTCに通常の更新wrapper全体が終了0。コードの適用記録と実SHA、applying=null・stages=[]・clean、直接起動の診断終了0、通知worker起動済み、MCSLOGの設定後処理完了を照合した。旧OCR失敗の根因や解消は未確認のまま保持する。
- 実設定でクラウド動作モードを省略しているため、新しいカードの進捗が常に未確認へ落ちる経路を確認。`4e30d084` は既存の提供元登録確認を共通の設定解決へ渡し、元設定とprofileを保持した表示用設定だけを使う。関連10fileは337 passed・0 failed・0 skipped・終了0、log/XMLは私有の `context-progress-defaults/final-batch.*` に保存。入力待ち0%と実読取りの10%、追加の秘密鍵読取り拒否、予約0、既存の凍結維持を検証した。初回7失敗は、意図した仕様変更による旧期待値6件と、閉鎖済み個別経路の不要な設定解決1件を修正して再検証した。この追補時点で当該修正は本番未適用。

## 患者コンテキスト・検索・昼夜3枠の統合追補

- 当初の文書基準は `b0b5f59b000e447ab0b4184b9b4e97eaa23c35b4`、2026-11-08の最終再検証候補は `23f0890b60661592534e623b3f4c2efa986b570f`。部分的な概況の作成、1患者・日本時間1日4回／原則3時間、同じ患者の原資料検索、昼夜の3枠配分と患者コンテキスト用の子処理を含む。CHANGELOGの対象日付は2026-11-08とし、過去の検証・配備日時と図の来歴は保持した。
- 検索の関連検証は547 passed、回数管理の限定検証は31 passed（親工程から受領した成功結果）。部分作成の初回149 passed / 11 failedは、旧期待値5件と署名・状態互換の退行6件を修復したとの親報告がある。後続の統合検証と再検証は下表を参照する。
- ローカル処理の初回18fileは `a26c7cd7` で628 passed / 1 failed・終了1。残った復旧用資産の世代不一致は親の配布資産更新の対象とした。患者コンテキストの子処理とローカル代替の最終3fileは `102b1c78` で227 passed・終了0、source_stamp_changed=[]。最後のRT枠解放競合は旧製品で1件失敗を再現し、`439492ec` の関連25件が成功・終了0、source_stamp_changed=[]。log/XMLは私有の `local-llm-schedule/` に保存し、この文書作業では再実行していない。

親工程が固定候補で実行した統合検証と再検証は次のとおり。文書担当は私有の `context-integrated/` に保存されたlog・JUnit・終了結果を照合し、pytestを再実行していない。

| 対象SHA・検証範囲 | 成功 | 失敗 | 終了コード | 実行時間 | ソース変更 |
| --- | ---: | ---: | ---: | ---: | --- |
| `b0b5f59b`・初回62file | 1075 | 95 | 1 | 905.49秒 | `source_stamp_changed=[]` |
| `2c6ab9c2`・失敗後の280ケース | 271 | 9 | 1 | 241.97秒 | `source_stamp_changed=[]` |
| `d01a239e`・残った9ケース | 9 | 0 | 0 | 16.79秒 | `source_stamp_changed=[]` |
| `d01a239e`・最終63file | 1163 | 13 | 1 | 867.65秒 | `source_stamp_changed=[]` |
| `23f0890b`・失敗4file全体と追加対照1file | 125 | 0 | 0 | 119.11秒 | `source_stamp_changed=[]` |

- 製品修正 `fc19b9af` とテスト整合 `2c49e065`・`58610d46`・`6d2dca2b`・`f7178cdf` を統合した。`resolution-current.json` は初回の重複しない失敗95件すべてについて、`failed-recheck.xml` と `last-nine.xml` での最新結果がPASSであることを記録している。これはこの2回の再検証時点の結果であり、後続の検証やGitHub CIの成功を意味しない。
- 最終63fileの13失敗は、いずれも初回に空のA段階を実行する旧テスト前提が原因だった。実際の失敗箇所を照合し、`cab8dd0e`・`d69f74ab` で実B/C後のA検証と初回B検証へ整合した。元の原子性・保留・ロック上限・出典隔離のassertを保持し、dry-runはB/Aの両方を検証する。`final-five.json/xml` は関連5file全体の125 passed・失敗0・skip 0を記録している。両実行とも前後の作業ツリーはcleanで、ソース変更はなかった。
- 実行済みの成功結果と、失敗修正後の関連範囲の再検証を合わせ、今回追加した機能のローカル検証を受け入れた。単一の全体suiteでの一括成功や、GitHub CI全ジョブの成功とは記録しない。
- 今回の独立レビューはユーザーの明示指定によりCodexの別担当が実施した。親工程が受領した結果では、`fc19b9af` の資料集合・空入力・結果不明の保存条件・書込みロック外の取得・原資料の照合、ローカル処理の `102b1c78..439492ec` は受入。前者の関連37件の成功証跡も照合済み。検索 `ca95383d..d7022cc8` は500文字の実アクション入力、薬剤検索100文字、添付の続き取得を独立照合し、関連234件の成功証跡とソース一致を確認した。
- テスト整合 `2c49e065`・`58610d46`・`6d2dca2b`・`f7178cdf` の独立レビューで不足していた、既存の事実だけでB段階の走査が成功する場合の負の対照を `45c5c5d1` で追加した。現行製品は2件成功・終了0（pytest出力3.32秒）。隔離した旧条件の対照ではbackfill判定だけを常にTrueへ戻し、最後の再試行状態の比較で想定どおり1件失敗・終了1（同2.01秒）となった。証跡は私有の `known-backfill-retry/RESULTS.json` とlog/XML。追加テストも上記125件に含む。他のassertと `6d2dca2b`・`f7178cdf` は既存レビューで受入済み。最後のfixture修正 `cab8dd0e` は親担当、`d69f74ab` は別担当による独立レビューで受け入れた。
- `2c6ab9c2` の静的検証はruff 0.17.3全範囲・ci/gates 10/10・mine・README生成差分・Slack画面集・source manifestの6checkが終了0。記録は `static-fixed-results.json`。配布証跡 `01f0c881` は524fileすべてのGit blobとmodeが一致し、公式digestは `3e2fe45afa5311befa0a54dbf0ffd07cf8b7091056eafed9f775939f5d7bd1f9`。これは `b0b5f59b` 時点のdigest `8da97247` から修正を反映した値で、独立照合も受入済み。
- 医薬品マスターのoffline照合は19,286行・edition 20261013で同梱版と一致し、activation=false。同じ1.1.2の公式更新確認（2026-11-07）は既存証拠を再利用した。文書担当による外部確認・本番辞書の有効化は行っていない。
- 正式更新planの結果を親工程から受領した。候補 `d01a239e`・本番 `d621f96d` を対象として終了0、routeはapply、blockersは空。gateway再起動が計画され、`recovery_tool_changed=true`、既存の `--no-brew`・`--no-llm` は保持される。これは更新計画の確認であり、本番適用の成功ではない。
- 前項の計画確認時点では本番は `d621f96d` のままで、今回の部分作成・検索・回数管理・昼夜3枠の新機能は未適用。実機の処理速度、全履歴を完了する時間、医学的な精度向上は未測定で、本番適用・自然実行の観測・公開は未完了。

## 空の概況・走査位置競合の修正追補

- 文書の基準は `b8150c3118ae5272c9d26092ce9f23dfecce1e29`。空の部分概況の修正 `82186278` と走査位置競合の修正 `b8150c31` を含む。既存93件に変更記録2件を加えて95件とし、既存の変更記録と過去の検証・適用時点の説明を保持した。図の処理の流れに変更はなく、図に件数表記もないため、SVG・PNG・来歴ファイルは再生成していない。
- 未反映・未取得が残り、確認済みの範囲が空本文・削除済み投稿だけの場合はAの作成候補から除外する。B/Cと有用な事実からの部分作成は継続する。全資料が空本文・削除済み投稿だけで過去の品質不合格がなければ、モデル通信・クラウド処理単位・生成枠を使わない。本文のある資料からの抽出項目が0件の場合や過去の品質不合格には、必要な品質確認を維持する。過去に消費した生成枠・会計・結果不明の履歴を自動で取り消さない。
- 親工程から、旧製品で最小2ケースが失敗し、修正後の限定検証は28 passed / 0 failed / 0 skipped・終了0・33.43秒・`source_stamp_changed=[]` との結果を受領した。証跡は私有の `context-empty-overview/`。文書担当はpytestを再実行していない。
- 走査位置の競合は、最新のbookmarkの形式を検証し、走査位置以外の全内容とメタ情報が一致する場合だけ処理を続ける。最新位置を保持し、cursor・units・原資料・品質確認・処理世代・書込み権限と6:3:1の配分は維持する。親工程から、旧 `c9d93baa` は2 failed / 6 passed、修正後は8 passed / 0 failed・終了0・7.44秒、同じテスト内容で処理中のソース変更なしとの結果を受領した。証跡は私有の `context-runtime-followup/`。
- 親工程から、`be539f2a` の生成表・配布証跡を別のCodex担当がGit objectから独立照合し、524件すべてのpath・hash・modeとtopが一致したとの報告を受領した。公式digestは `ab3f618ee9bf8565bf4905407e441da917b7392c5857da7519e2eda6f276a4a7` と再現し、生成表は335 modules / 1379 test files。full ruffは終了0、ci/gatesは10/10、mineとrelease owner checkも終了0。走査位置競合の製品差分13行も独立レビューで受け入れられ、旧・新の検証対象hashは2/2でcommit blobと一致した。
- 前回追補で実行中だった `be539f2acc212b072e3777a9cf46d47997371ffc` の66file統合検証は、1199 passed / 0 failed / 0 skipped・終了0で完了した。pytest出力の実行時間は1019.41秒、実行driverは1020.44秒。実行前後のSHAは同じ `be539f2a` で、前後の作業ツリーはclean、`source_stamp_changed=[]`。文書担当は私有の `context-runtime-followup/batch-final.log`・XML・JSONの終端を照合し、pytestは再実行していない。追加2修正を含む66fileの成功であり、過去の全体suite約1万件やGitHub CI全ジョブの成功とは記録しない。
- 親工程から、本番 `bcf3343d` の実機適用は終了0、checkout外の復旧ツールの同期は `./install.sh --no-brew --no-llm --no-services` で終了0、設定・opt-out・復旧用plistのハッシュは不変との報告を受領した。直接起動のsetup checkは終了0、doctorではサービス正常とcard worker起動を観測し、gatewayは `supervisor_restart_verified`。gatewayが読み込んだSHAはunknownのまま区別する。
- 同じ実機観測で常駐処理3本の稼働と、`/slots` の全3枠がprocessingになった瞬間を確認したとの報告を受領した。持続的な枠利用、処理速度、本番の回復や医学的な品質向上の証拠にはしない。その `bcf3343d` 観測時点では追加2修正は本番未適用で、適用後の自然実行・運用受入と公開は未完了だった。

## 検証済み投稿の部分作成の追補

- 文書の基準は `f9b9b9199e9ae2a25ed7326f4ccc24fd1fdc1325`。製品修正 `49bc0a85` とテスト強化 `f5789370` は親工程の独立レビューで受け入れられた。既存95件へ1件を加えて全96件とし、過去の変更記録・技術詳細・検証・適用時点の説明を保持した。図の処理の流れに変更はなく、SVG・PNG・来歴ファイルは保持した。
- 要約が有効な非空投稿だけを、既存の `reprocess_target_state` が返す `current` または `current_skipped` の証拠で判定する。省略の証拠を監査合格に置き換えず、要約OFF・空本文・削除済み投稿・取込み保留・抽出QC・原資料対応の条件を維持した。canonicalをlegacyで代用せず、新しい原資料との不一致も拒否する。

文書担当は私有の `context-semantic-member/` にあるJSON・XML・logの終端を照合した。次表の時間は実行driverの値で、各実行中の対象ファイルhashは前後同一、`source_stamp_changed=[]`。実行時HEADと未commitの修正・追加テストを、確定コミットと区別する。

| 証跡・実行範囲 | 実行時の状態 | 成功 | 失敗 | skip | 終了コード | driver時間 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `red.*`・旧条件の2ケース | `5988a774`＋追加テスト | 0 | 2 | 0 | 1 | 3.85秒 |
| `green-initial.*`・関連7file | `5988a774`＋修正差分 | 162 | 1 | 0 | 1 | 156.34秒 |
| `recheck.*`・失敗項目と強化対照 | `5988a774`＋修正差分 | 13 | 0 | 0 | 0 | 12.76秒 |
| `overview-positive.*`・B→C→A | `49bc0a85`＋追加テスト | 2 | 0 | 0 | 0 | 4.15秒 |

- 初回に残った1失敗は、スレッドの待機だけで投稿を拒否する旧期待値だった。当該テストを、実際の `current` 証明による採用、監査失効による拒否、新着追加による拒否へ変更して対照を強化した。修正版の製品ファイルは修正後の3実行と候補でhashが一致し、陽性対照のテストも `f5789370` と一致する。文書担当によるpytest再実行はない。
- B→C→Aの完全合成の陽性対照はlegacy・shadowの2ケース。非空の概況、採用資料1件・未反映1件・全体のcomplete=false・生成枠1回・他投稿の未完了・cloud予約0件を検証した。ほかの投稿の未完了を全体完了へ読み替えない。以前の66file・1199件成功は `be539f2a` 時点の証拠として保持し、今回の修正後に同じ範囲を再実行したとは記録しない。
- 親工程が生成した配布証跡は524path、公式digestは `cbfcb65c77c3a76f86142270966297d8dcff61e3165ffbd1ee571096098422ea`、生成表は335 modules / 1380 test files。文書担当はsource manifestとDEVELOPMENTを変更していない。
- 親工程から、本番 `5988a774` の適用は111.86秒・終了0・clean、設定と明示OFFの変更0、更新journalに未完了記録なし、直接起動のsetup checkは終了0、gateway再起動とcard worker起動を観測したとの報告を受領した。今回追加した投稿単位の判定修正は本番未適用で、適用後の自然実行・運用受入と公開は未完了。
- 実機の3患者標本では、採用可能な本文付き資料数は0で、資料のQC・意味解析・OCRの待機が残るとの親報告を受領した。旧empty-repairの1例は旧policyのmarkerにリンクした保存候補が0件で、同じ入力の保存済み修復結果として回収できないため保留を維持する。markerを取り消したり、新しいrepairを送ったりしていない。OCR表の交差は曖昧なgridを拒否する必要があるという分類にとどめ、個別10件の全原因や医学的な精度は未検証。本番回復の成功とは扱わない。

## 最終整理とCI後の再検証の追補

- 文書のソース基準は `18608038a7fc02a454c319dbe45c90d32400b4e0`。既存96件のarchiveをバイト単位で保持し、保守用の未使用処理と到達しない分岐の整理1件を加えて全97件へ集約した。処理の流れは変わらないため、SVG・PNG・visual-sourceの3資産は保持し、再描画していない。
- 未使用の復旧manifest読取wrapper、通知のwire名比較helper、ログ列挙helper2件と到達しないanchor分岐を削除した。公開CLI、旧更新互換、移行・rollback・witness、旧クラウド予約・課金済み回答・結果不明の回収と保留は保持する。
- 現行説明との不一致はplist件数9→10・Hermes用5→6、`patient_state_context`から`extraction_context`へのコードリンク、台帳schema 12と削除済み本文状態、個別の新規生成はlocal・保存済みcloud結果は既存の保護条件で回収する説明の4系統を訂正した。台帳の説明訂正によるDB移行はない。

旧GitHub CI run `42399969431`（ソース `77ac031b`）は失敗した実行として保持する。restは27691成功・91失敗・8skipと102 subtests、lifecycleは7983成功・1失敗・12skipと15 subtests、macは6694成功・1失敗と15 subtestsだった。hygieneはshellcheckで失敗した。これらの失敗と、後続の修正後の検証を分けて記録する。

| 修正後の検証範囲 | 実行時の状態と確定修正 | 成功 | 失敗 | skip | 終了コード |
| --- | --- | ---: | ---: | ---: | ---: |
| サービス構成 | `7485495b`＋test修正、後に`758cd3d3`へ確定 | 38 | 0 | 0 | 0 |
| 常駐処理と待機 | `758cd3d3`＋fixture修正、後に`12a75558`へ確定 | 112 | 0 | 0 | 0 |
| 認証とCDP peer照合 | `81e5e9d4` | 41 | 0 | 0 | 0 |
| 通知の関連16file | `74aac63d` | 409 | 3 | 0 | 1 |
| 通知の失敗3node再検証 | `d6f944e3` | 3 | 0 | 0 | 0 |
| 患者情報・品質・再試行の関連4file | 親工程の試験差分、後に`3fd0442b`へ確定 | 156 | 1 | 0 | 1 |
| 患者情報の失敗1node再検証 | 修正版を`3fd0442b`へ確定 | 1 | 0 | 0 | 0 |
| 最終整理とinstallerの合同9file | `18608038`固定 | 429 | 0 | 0 | 0 |

- 文書担当は完全合成のXML・JSONと失敗対応表を読み、サービス38件・常駐112件・認証41件、通知409件と失敗3件の再検証、患者関連156件と失敗1件の再検証を区別した。同じ対象とソースの成功結果を再利用しており、件数を重複なく合計した全体suiteの結果ではない。
- 認証41件は公式runnerの隔離下で実行したが、実際のPythonは指定のstandalone SDK venvではなく `.mcs/.venv/bin/python` だった。実環境はPython 3.14.10・pytest 9.2.0・SQLite 3.54.1。SDKに依存しない対象であることとpytest固定版の一致を親工程が確認して採用した。他の実行環境と取り違えない。
- 患者関連4fileの調整はテスト側だけで、製品処理の変更はない。品質確認へ渡す引数、部分監査済み集合から全体集合への拡大、実writerによる原文編集後の拒否、Linuxの共有time.sleepへのスタブ影響を、現在の契約へ合わせた。残った1失敗はAの読取り回数の旧期待値で、当該nodeを修正して再検証した。
- `failure-resolution-current.json` は旧CIのunique failed 92件を最新PASSへ全件対応付け、未対応0件とした。LinuxとmacOSで異なるUIDは同じparam index・URL・peer ID・相対UIDで厳密に対応付け、表示上のescape差を分けた。意図した契約名の変更も記録している。元の失敗数から再検証件数を単純に差し引いた集計ではない。
- 親工程は配布証跡524pathの全bytes一致、pathの集合不変、digest `b9ed9df613bea35f544313c0c644b14d9cb101d7f7d897f38fdaa5b410398a83` を確認した。更新対象はprepare_recovery・install.sh・ledger・maintenance・extract_llm・notify_transportの6entryで、DEVELOPMENT生成差分はない。文書担当はこれらのsource manifest・生成表を編集していない。
- 最終の整理とinstallerの合同batchは、`18608038`固定の9fileで429件成功・失敗0・skip 0・終了0だった。pytest出力の時間は319.96秒、driverは320.3856秒。文書担当は`final-cleanup-batch/batch.json`・XML・logの終端を照合し、実行前後のSHA一致と親工程のclean報告を確認した。対象はinstaller・install companions・recovery bundle・installer config boundary・upgrader・maintenanceの2file・Discord group settlement・card part resolveで、文書担当によるpytest再実行はない。
- 親工程から、全範囲ruff、gates 10/10、mine、README、gallery、配布ソース524path、同梱masterのoffline検査の7項目と、CI同範囲のshellcheckがすべて終了0との報告を受領した。これらをGitHub CI全ジョブの実測成功とは扱わない。
- GitHubのartifact storage quotaは未解消。既存Release下書きの再利用は`6baa4654`（親統合`f40e6136`）の合成unittest18件が成功し、本文同期のCASと公開状態の保持を維持するが、lookupからsyncまでの原子性を保証するものではなく、tag差替前の実機での下書き再取得は未確認である。今回の文書追補では本番の再適用・自然実行・tag差替・公開を確認していない。

## 実機観測後の診断と常駐候補上限の追補

- 製品ソースは `9bfae5add8f6c35505e23747d5912c645ee203bb` に固定した。既存97件のarchiveをバイト単位で保持し、OCR監視の診断読取り、患者情報処理の診断と古いwriter世代の子終了、閉鎖route時の常駐候補上限の3件を加えて全100件へ集約した。SVG・PNG・visual-sourceの3資産と過去の受入記録は保持し、図を再描画していない。
- OCR監視はwriter診断3項目がすべてない旧形式を受け入れ、追加時は3項目の型・固定語彙・対応関係を検証する。正常な記録の誤判定を修正した一方、欠落・未知・型違い・不一致の拒否は維持する。
- 患者情報処理のreason・status・scanned・laneとwriter拒否を固定語彙と型・範囲を確認した件数・時間で診断する。既存のwriter確認が拒否した子処理は更新検出の経路で終了し、推論・予約・保存へ進まない。合成で再現した世代不一致の待機ループの修正であり、実環境の待機原因がこの条件だけとは断定しない。
- 個別reextract routeが閉じたときは、意味解析も巡回する常駐処理のローカル抽出候補を1件に制限する。限定legacy-open機構の上限、物理slot3枠、昼夜配分、モデル、最大900秒の推論予算、worker数、優先順位、archived・CAS・課金済み回答・結果不明・carryの保護は維持する。1件自体が長い場合や保存待ちを即座に解消する保証、速度向上率の測定はない。

| 合成検証の範囲 | ソースと根拠 | 結果 |
| --- | --- | --- |
| OCR監視reader | 親工程の`779e`修正と再現・検証報告 | 旧版で2件RED、修正版の関連62件成功 |
| 患者情報処理の固定診断 | 親工程の`41fc`修正と検証報告 | 関連80件成功 |
| 古いwriter世代の終了 | 親工程の`00a`修正と検証報告 | 旧版の1nodeがRED、修正版の関連5fileは96件成功・終了0 |
| 閉鎖route時の候補上限の旧版対照 | `06ff`の合成対照 | 閉鎖時は12と期待1の不一致で失敗、限定openの対照は成功 |
| 候補上限と常駐処理の関連3file | `9bfae5ad`固定、`resident-closed-route-limit/green.*` | 162件成功・失敗0・error 0・skip 0・終了0 |

- 文書担当は最終3fileのJSON・XML・logの終端を照合した。対象はsemantic cloud・carry backpressure・anthropic runtime paid lifecycle。driverは140.56秒で、前後のSHAは同じ `9bfae5ad`、親工程の作業ツリーはclean、1897fileの`source_stamp_changed=[]`だった。health 62件・guard 96件と今回162件の関連成功を再利用しており、重複なく合計した単一の全体suiteやGitHub CI全ジョブの成功とは記録しない。文書担当によるpytest再実行はない。
- 親工程の同じソースに対する全範囲ruff・gates 10/10・mine・配布ソース524path・READMEの静的5項目はすべて終了0で、`postdeploy-static/results.json`の記録を照合した。配布証跡のdigestは `f69b782589716225cedfc55ffff2b363b4137c5827ecfd3ac5a532808733412d`。親工程は全実bytes一致とhealth_report・cloud_backlog・residentの3entryだけの変更、DEVELOPMENT生成差分なしを確認した。文書担当はsource manifestと生成表を編集していない。
- 親工程の限定標本の集計では、採用条件を満たす本文付き資料がない状態で、意味解析の証拠、原文抽出、OCR後の解析と添付OCRの待機が残っていた。最終抽出件数が増えない観測区間にも部分結果の保存と走査位置の更新があり、自然実行はcrashではなくrollupの期限による先送りだったとの報告を受領した。実標本の内容・識別子・具体的件数はここへ転載せず、私有の集計と公開文書を分ける。医学的精度、実速度改善、全体の復旧を確認したとは扱わない。
- この追補時点の本番は `66066697` のファイル適用済みで、check終了0・services healthyとの親報告を受領した。gatewayの稼働中処理を保護した安全な再起動は待機のままtimeoutとなり、今回の3修正は本番未適用だった。クラウド新規POSTがない観測と、旧予約・課金済み回答・結果不明の保持も区別する。後続の実機適用・自然実行・tag・公開の結果は親工程の私有receiptで扱い、この時点の未確認を成功へ書き換えない。

## Jev再試行待機中の共有ロック解放の追補

- 文書の基準は `5b0535bb3926c239969337871809182490eca89c`、製品修正は `5e9b615a630ede746bb4fb785eac2250b944f957`。親工程の独立レビューで指摘なしとして受け入れられた。既存100件のarchiveをバイト単位で保持し、Jevの再試行待機中の共有ロック解放1件を加えて全101件へ集約した。図の流れは変わらず、SVG・PNG・visual-sourceの3資産は保持した。
- 共有ロックを使うsemantic処理で、HTTP待機の既存の解放・再取得経路をRetry-Afterと通信失敗後のbackoffへ再利用した。既存のtransportがない呼出しの待機は変更していない。待機時間、試行上限、送信前の予約、送信結果不明の扱い、送信前のソース・設定・期限の再確認は維持する。現在のpoll待機の原因がこのロック保持であるかは未確定で、コードと合成で確定した欠陥への修正として区別する。

| 合成検証 | 根拠と実行結果 | 終了コード |
| --- | --- | ---: |
| 旧製品の再現と対照6件 | 4件失敗・2件成功 | 1 |
| 修正版の関連134case | 132件成功・新fixtureの2件失敗 | 1 |
| fixture整合後の失敗2node再検証 | 2件成功 | 0 |

- 文書担当は `jev-retry-wait-unlock/acceptance.json` を照合した。初回の2失敗は試験fixtureの不足によるもので、一時dataディレクトリを用意し、既存writer_lockの状態 `held=True/updated=False` に合わせて修正した。製品は最初の関連batch後に変更せず、batchと再検証の製品bytesは同一で、各実行中のsource固定と最終commitの再検証ソース一致を確認したとの親記録を受領した。
- 同じ製品ソースの既存132件成功と失敗2件の再検証成功を併用する。単一の実行で134件すべてが成功した結果や、全体suite・GitHub CI全ジョブの成功とは記録しない。親工程のruff 0.17.3・diffcheckとfragmentのYomiは成功。文書担当によるpytest再実行はない。
- 親工程は配布証跡524pathとtopの不変、semantic_jev.pyの1entryだけの変更、全実bytes一致を確認した。digestは `8177ed809ad82173c164822e1c601207ce20b45a9505be3132ddf956ca3acca5`。文書担当はsource manifest・製品ソース・テスト・DEVELOPMENTを編集していない。
- 親工程の同じ `5b0535bb` に対するci/gates.py 10/10・mine・release_owner_sourceのcheckはすべて終了0で、前後SHA同一・cleanの記録を `jev-retry-wait-unlock/final-static.json` で照合した。検査は再実行していない。
- この追補時点の本番は `ea03b52c` のファイル適用済みで、終了0、設定・導入の省略指定のhash保持、check終了0・services healthyとの親報告を受領した。gatewayは実行中の処理の自然終了後に `supervisor_restart_verified` となり、doctorの`restart_required=false`を観測した一方、loaded revisionはunknownとして区別した。強制中断はしていない。過去の未適用・再起動待機の記録は当時の状態として保持する。
- 親工程の自然実行ではok=true・snapshot=trueで、記録されたエラーはrollup_scan・rollup_rebuildのdeadline_deferredだけだったとの集計報告を受領した。一方、healthはdegradedでOCR・通知保留・semantic_backlog_stalledが残り、解除されていない。自然実行の一部の成功を、全体の復旧や現在のpoll待機の根因特定へ読み替えない。
- 新しいJev修正の本番適用、現在のpoll待機の根因特定、医学的精度・実速度改善・全体の復旧は、この追補では未確認。実データの具体的件数・内容・PID等を公開本文へ追加していない。後続の適用・自然観測・tag・公開結果は親工程の私有receiptで扱い、文書の自己SHA更新や再適用を繰り返さない。

## 通知カードの完了集計の追補

- 文書の基準は `d71ad78552082676b9d2a6a66a463d24210495b5`、製品修正は `81818f0cc11bcfd1a46d6a332445d69965e1bb6f`。親工程の独立レビューで指摘なしとして受け入れられた。既存101件を保持し、通知カードの完了集計1件を加えて全102件へ集約した。既存の図3資産と過去の受入記録は変更していない。
- 最新配送を求めるSQLの `WITH latest AS` に `MATERIALIZED` だけを加え、部品集計と最終集計で同じ集合を共有する。既存SQLiteの対応範囲、schema・index・データ移行、監視時間制限、送信結果不明の判定、保存済み履歴と自動再送の条件は変更しない。
- 文書担当は `notification-completion-query/RESULTS.json` を照合した。旧製品の合成1nodeで `completion_counts/time_limit` を再現して失敗し、修正内容を固定した関連4fileは75件成功・失敗0・error 0・skip 0・終了0だった。実行前後のHEADは `6da7169f` で、`source_stamp_changed=[]`。試験の時計はVM操作数を使う代替であり、実速度の測定ではない。親工程が受け入れた最終製品commitと、この実行記録を区別する。
- 親工程のruff 0.17.3・AST・JSON・fragmentのYomiは成功。既存の関連成功と今回の限定検証を再利用し、単一の全体suiteやGitHub CI全ジョブの成功とは扱わない。文書担当によるpytest再実行はない。
- 親工程の単一の実機読取り計測では、SQL変更前後の結果digestが一致し、監視全体も既存の時間制限内に完了したとの報告を受領した。常時の完了や速度向上率を保証する結果ではなく、医学的精度や全体の復旧を確認したものでもない。実データの内容や具体的件数はここへ転載しない。
- 親工程は配布証跡524pathとtopの不変、notify_scope.pyの1entryだけの変更、全実hash一致を確認した。digestは `bf71a9e720c09098feb0d0872c09955cbadb07ef02352358371104106b70d167`。335modules・1381testsの生成表は親工程が更新済みで、文書担当は製品・テスト・source manifest・DEVELOPMENT・図を編集していない。
- この追補時点の本番は引き続き `ea03b52c`。Jevの再試行待機中の共有ロック解放と今回の通知集計修正は、ともに本番未適用として区別する。後続の適用・自然観測・tag・公開結果は親工程の私有receiptで扱い、過去時点の未確認を成功へ書き換えない。

## 通知集計の参照順序と要約候補保留後の巡回の追補

- 文書の基準は `35458d341c216c0806ba388540a12bc719524bec`。通知修正 `e6fdac1083ff71898911bf4a67dd9b8f8a51f765` と意味解析修正 `7bd74e8e2be12aadc5a3b5ee1157cc027fb3b4f0` は、親工程の独立レビューで受け入れられた。既存102件へ新2件を加えて全104件へ集約し、旧archive・図3資産・過去の受入記録は変更していない。
- 通知の完了集計は最新配送の集合を一度だけ求める先行修正を維持し、`CROSS JOIN` で最新配送から部品を参照する順序を固定する。既存indexを使い、集計条件・監視時間制限・送信結果不明の扱い・保存形式・配送履歴・自動再送条件は変更しない。
- 意味解析は `empty_repair_candidate_binding` による再保留だけで、Jev送信・モデル呼出し・新しい段階の保存がすべてない場合に限り処理枠を返し、既存の選択・走査上限・期限内で別の候補へ進む。同じ実行で確認済みのjobは再選択しない。初回送信や保存の進行があれば枠を消費し、保留の解除、有料処理の再送、試行回数や間隔の変更は行わない。

| 合成検証 | 実行結果 | 終了コード |
| --- | --- | ---: |
| 旧d8の通知集計1node | 1件失敗 | 1 |
| 通知の修正後の関連4file | 76件成功・失敗0・error 0・skip 0 | 0 |
| 旧d8の意味解析1node | 1件失敗 | 1 |
| 意味解析の関連174case | 172件成功・新fixtureの2件失敗 | 1 |
| 意味解析fixture整合後の失敗2node再確認 | 2件成功 | 0 |

- 文書担当は `notification-completion-join/RESULTS.json` と `semantic-binding-refill/acceptance.json` を照合した。通知の検証は実行中とcommit後のsource変更なし。VM操作数を使う合成の制限時間回帰であり、すべての実機監視が時間内に終わる保証ではない。
- 意味解析の2失敗は、新fixtureで実artifactのmessage-relation guardに必要な合成messagesが不足していたため、その2件だけにmessagesを保存して再確認した。製品・期待値は変えておらず、batchと再確認の製品bytesは同一、各実行中のsource固定と最終sourceの再確認時との一致が記録されている。新しい巡回試験はprocessorの結果を注入する一方、実際の候補選択・queue・実行状態の遷移・会計・走査上限を通し、実binding・paid・sourceの既存回帰も同じbatchで実行した。
- 通知76件成功と、意味解析172件成功およびfixture整合後2件成功をそれぞれ再利用する。単一の174件全成功や、全体suite・GitHub CI全ジョブの成功とは扱わない。元担当のruff 0.17.3・AST・JSON・diffcheck・Yomiは成功。文書担当によるpytest再実行はない。
- 親工程は配布証跡524pathとtopの不変、notify_scope・semantic_drainの2entryだけの変更、全実hash一致とDEVELOPMENTの生成差分なしを確認した。digestは `b4de7be126cb6d9b8055db34a63b67022ecf9c9291a3da80a6428a615cb824d3`。文書担当は製品・テスト・source manifest・DEVELOPMENT・図を編集していない。
- 新2修正はこの追補時点で本番未適用・実機未確認。既存d8の観測で残った未解決事項を成功へ読み替えず、現在のpoll待機の根因、医学的精度、常時の速度改善、全体の復旧は未確認として保持する。実データの件数・識別子・PID・ログ原文はここへ転載せず、後続の適用・自然観測・公開結果は親工程の私有receiptで扱う。

## 収集開始時の固定診断と通知完了集計の最大版取得の追補

- 文書の基準は `ea8120e6d976675ef1fb07d3cd732643e0ef01ef`。収集開始時の修正 `51a82ba673b2bd7cc898556902d9c44bbd8c5679` と通知修正 `b8e19a9e10748dcd84b584ec139b12c0b05c9d70` は親工程の独立レビューで受け入れられた。既存104件へ新2件を加えて全106件へ集約し、旧archive・図3資産・過去の受入記録は変更していない。
- 収集開始時には既存の待機処理が観測した分類済みの `writer_reason` と `writer_stage` だけを `lock_held` の診断へ加える。成功時と理由が未分類の場合の出力、ロックの取得順序・待機時間・予算・更新とコード世代の検査・収集範囲・既読化の条件は維持する。終了コード3の説明も別のholderだけでなく更新・コード世代・開始期限を含む実装へ合わせた。定期処理と重なる収集開始時の待機の観測だけで、背景holderが原因だとは断定しない。
- 通知集計は従来と同じ全配送版からカードごとの最大版を一度まとめて求め、既存indexでその版の配送記録を参照する。操作・状態・通知先で最大版を先に絞らず、不明な送信結果、部品記録なし、所要時間の集計方法、2秒の観測上限を維持する。保存形式・indexの追加、データの書換えや再送は行わない。

| 合成検証 | 実行結果 | 終了コード |
| --- | --- | ---: |
| 旧1aの収集開始時の診断1node | 固定診断の欠落で1件失敗 | 1 |
| 収集開始時の修正後の関連4file | 129件成功・失敗0 | 0 |
| 旧1aの通知集計1node | completion_counts/time_limitで1件失敗 | 1 |
| 通知修正後の関連4file | 211件成功・失敗0 | 0 |

- 文書担当は `collection-entry-refusal/RESULTS.json` と `notification-completion-heads/acceptance.json` を照合した。各実行中のsource固定、収集開始時のcommit後のsource変更なし、通知の最終sourceと成功時の一致が記録されている。通知の試験時計はSQLite VM操作数を使う代替で、実速度の測定ではない。元担当のruff 0.17.3・AST・JSON・diffcheck・Yomiの成功と既存の関連成功を再利用し、単一の全体suiteやGitHub CI全ジョブの成功とは扱わない。文書担当によるpytest再実行はない。
- 親工程は配布証跡524pathとtopの不変、run_check・notify_scopeの2entryだけの変更、全実hash一致とDEVELOPMENTの生成差分なしを確認した。digestは `21bbebf2b0afc5c8b0ee94cf311b6d64b635022ca20cc101cc4bb6f459698328`。文書担当は製品・テスト・source manifest・DEVELOPMENT・図を編集していない。
- 前版 `1a83267f` の自然観測では、要約候補の純粋な再保留後に同じpassの独立jobが完了し、品質確認・結果保存と自然収集完了へ進み、freshhealthからsemantic stallが消失したとの親供給の集計を受領した。一方、自然実行の通知完了集計は再び時間切れとなり未解決で、公開保留を維持する。この追補の新2修正は本番未適用・実機未確認。患者コンテキスト完成、自然集計の正常化、医学的精度、常時の速度改善を確認したとは扱わない。実データの件数・識別子・PID・ログ原文は転載せず、後続の適用と自然観測・公開結果は親工程の私有receiptで扱う。

## MCSLOGの結果別案内と保留・OCR診断の追補

- 文書の基準は `75d01a6deff6bca70e8280baf06755eb30dde2b8`。既存106件へ更新結果、通知保留、OCR失敗、監視アラートの4件を加え、全110件へ集約した。旧106件のarchiveとSVG・PNG・visual-sourceの3資産、過去の受入記録は保持した。
- 更新の適用完了は対応不要と案内し、中止・復旧・再起動要求の失敗・結果不明を固定コードで区別する。適用完了から収集・通知の正常稼働や全サービスの再起動完了は推測しない。旧queuedも既知の形式だけを読み、例外文・リリース本文・任意のURL・患者情報を表示しない。既に配信した通知は書き換えない。
- 通知保留は編集先・添付・配送所有関係・本文長の既存理由コードを保持する。OCR失敗は既存の観測範囲で原因別件数と再試行上限到達を記録し、旧形式との互換、型・件数上限・鮮度と部分観測の判定を維持する。これらは診断の改善であり、既存のOCR失敗、未採用結果、配送unknownや本人照合待ちを解消しない。原資料の品質確認と配送先・記録の本人照合・承認は引き続き必要とする。
- 監視アラートは問題・影響・対処・照会コードを整理し、共有用診断と環境に合う完全なコマンドを案内する。要約通知が遅れ得ること、未採用のOCR結果と全理由横断の再試行上限到達を分けて表示する。ディスク警告の重複を避け、不正な型の理由もアラートを止めずunknownとして扱う。診断条件・再送・辞書の自動承認・安全ゲートは変更しない。
- 文書担当は `mcslog-message-design/combined-result.json` と `static-results.json` の終端を照合した。指定の隔離Pythonと公式runnerによる合同25fileは、`75d01a6d` 固定で1633件成功・失敗0・error 0・skip 0・終了0、記録上の経過は約362.53秒だった。`source_stamp_changed=[]`、終了HEADは同じ `75d01a6d`、終了時のstatusは空。過去の成功結果とは別の今回の限定実測として記録し、単一の全体suiteやGitHub CI全ジョブの成功とは扱わない。文書担当によるpytest再実行はない。
- 同じsourceのfullruff・gates 10/10・mine・readme・sourceのcheckはすべて終了0。gatesは約83.40秒、mineは約3.80秒だった。既存の成功結果を再利用し、同じ検査を文書担当から再実行していない。
- 親工程は公式の連携履歴でDevinの最終独立レビューreceiptを確認した。固定source75と追加の診断境界修正、統合テストの静的確認、配布証跡524pathのhash・top・digestに反例やP1/P2指摘はなく、受入済み。レビュー本人はpytestを実行しておらず、動的根拠は上記のRootの1633件成功と分ける。私有の会話全文は転載していない。
- 親工程は配布証跡524pathとtopの不変、全実hash一致を確認した。digestは `cdbda84e8a44a45a5d03dfd573ea8101ddf814d73766006a55b600c25f049057`。335modules・1383testsのDEVELOPMENTは親工程が生成済みで、文書担当は製品・テスト・source manifest・DEVELOPMENT・COLLECTION_DIAGNOSTICS本文・図を編集していない。
- このsourceと新4件の文書はこの追補時点で本番未適用、Release下書きは未公開で、自然観測は未確認。OCR失敗と配送unknownが解消した、患者コンテキストが完成した、医学的精度や全体の復旧を確認したとは扱わない。実データの件数・識別子・PID・ログ原文は転載せず、過去時点の未確認を成功へ書き換えない。

## 配送照合と導入・更新の追補

- 文書の基準は `d85759a8400ed0373cbfad70018b04f0ceb7305c`。既存110件へ配送記録の重複キー拒否・任意の読取り上限・復旧対象一覧、対話初期設定、導入依存の検証、導入済み単独実行環境の対話更新の6件を加え、全116件へ集約した。旧110件のarchive、SVG・PNG・visual-sourceの3資産と過去の受入記録を保持する。
- 配送記録の破損・未完了・読取り上限超過を未送信の証拠にせず、部分証拠を採用しない。復旧対象の一覧と事前確認は読取りのみで、unknownの自動解除・再送・状態変更は行わない。journal_checkのpassedは少なくとも1件のproofにjournalとの矛盾がないことを示す。配送結果・worker停止・配送先identity・人承認の証明とは別で、必要な外部証拠と適用時の再照合を省略しない。
- 初期設定は既存の質問・保存前承認・中止と再開を保ち、通知形式を先に選び、現在の選択で質問がある設定段階だけを表示する。導入済み環境では新たなcloneを作らず、既存の公開コマンドと案内を使う。個別投稿・添付OCR・意味解析への新規クラウド送信の停止、旧課金済み・結果不明の保護は維持する。
- 対応する導入済みupdaterを確認できる単独実行環境では、mcs update planで計画を見て、mcs updateの対話で更新を選べる。旧updater・外部から取り出したツール・未公開候補はこの経路の対象外として保留する。実行方式・以前の導入設定・更新前後のSHAの照合、実行モードの競合検知、停止・再起動と復旧保留の条件を維持する。完了後の診断・通知応答と実収集の再開は分けて確認する。
- 親工程は、固定SDK13種を控えから空の仮想環境へ導入し、実importが成功したこと、実行モードが変わる2窓の修正と独立レビュー受入、配送journalの限定したRED/GREEN成功を供給した。これは新しいMacで依存導入から通知・常駐まで完了した証拠ではない。親工程の合同45対象はこの追補時点で実行中で、終端・成功は未確認。文書担当はpytestを起動していない。
- 親工程は同sourceのruff・gates 10/10・mine・readme・manifestの検査終了0を供給した。release_notes検査は新worklistのsummary長で失敗したため、文書担当は当該summaryを短縮し、一覧対象・件数上限・ページ表示・固定コードの条件をdetailsへ移した。限定した読取り専用CLIの照合も完了し、proofごとのpassed/blockedと、別に必要な外部確認4条件を親工程が確認した。これらの操作で保留解除は行っていない。
- 親工程は配布証跡524pathとtopの不変、全実hash一致を確認し、335modules・1386testfilesの生成表を更新した。文書担当はsource・tests・manifest・DEVELOPMENTを編集せず、同じ検査を再実行していない。
- この追補時点の本番は70e4ae5eのままで、今回の候補はローカルのみ。新規Macへの全導入、候補の本番適用・自然観測は未実施で、既存のRelease下書きは未公開。配送unknownやOCR失敗の解除、医学的精度、患者コンテキストの完成、全体suite・GitHub CI全ジョブの成功は記録しない。過去の観測・未確認の記録は当時の状態のまま保持する。

## 配送journalのadapter非依存読取りの追補

- 製品・metadata・後続テストの基準は `8b75024482cbd9adfc8cb2abc4a461edffb79765`。既存116件のうち配送記録の重複キー拒否の技術詳細だけを訂正した。本体は既存のunique_pairsを使って重複キーを拒否し、adapterがない本体だけの環境でも実際のjournal読取りと復元後の照合まで実行できる。同じparser実装を使う、scan時にadapterを読み込むという前の説明は採用しない。新たな変更記録は追加せず、旧110件と図3資産、過去の受入を保持する。
- 親工程は旧d857の本体と新しい同一テストでImportErrorの2件失敗を確認した。製品修正049cf32eでは、読取り上限と復元後照合の関連28件が成功・終了0、約8.03秒で、対象hashも固定されていた。独立Codexの受入も親工程が確認した。module importだけの確認と、実際に配送記録を読む経路の確認を区別する。
- 元d857の合同45対象は2733件中2732件成功・1件失敗・skip 0、約1387.30秒で終了した。実行前後のsourceは同じで、source_stamp_changed=[]、statusはcleanだった。失敗はfakeFDを拒否した後、合成環境に更新前のbaseがなく、復旧を正しく保留したことによる旧期待だった。test-onlyの324fa35aは保留markerと失敗、更新前・更新先と設定の保持、再起動0を確認するよう修正し、製品の保護条件は変えていない。失敗を残した元batchを全件成功とは扱わない。
- この節の初稿時点では後続16fileは実行中だった。その後、文書担当は親工程のdelivery-reconciliation/followup-result.jsonの終端を照合した。8b750244固定の16fileは709件成功・失敗0・error 0・skip 0・終了0、約609.895秒で完了した。source_stamp_changed=[]、終了HEADは同じ8b750244、終了時のstatusはcleanだった。元batchのfakeFD fixture修正と本体だけでの実journal読取り・復元後照合を含む関連成功として記録し、元45対象の2732件成功・1件失敗は当時の結果として保持する。全体suiteやGitHub CI全ジョブの成功とは扱わず、文書担当はpytestを追加起動していない。
- 親工程は配布証跡524pathとtopの不変、全実hash一致を確認した。変更はnotify_journalの1entryだけで、digestは `ac19bae4f3665e7343e7a6867ce86f20c0ccbefc6c987c5ba02fb7d978de03ec`。文書担当はsource・tests・manifest・DEVELOPMENTを手編集していない。本番70e4ae5e、新しいMacでの全導入未実施、候補未適用、既存Release下書き未公開と、unknownの自動解除・再送を行わない条件は引き続き保持する。

## 導入・更新の段階記録の追補

- 文書の基準は `9910acbcfc7c8eb1f5de1ef796b1583f06842ef6`。既存116件へinstaller-interruption-progressの1件を加え、全117件へ集約した。新fragmentのsummaryは156字で、原文のbytesを保ってarchiveした。既存116件のhashと旧110件のbytes、SVG・PNG・visual-sourceの3資産、過去の受入記録を保持する。
- 記録が示すのは最後に開始した段階とソフト準備完了だけで、全導入完了や実稼働の証拠ではない。記録と時刻だけで実行中・中断とは断定せず、記録が残っていることと、実行中なら完了を待つことを案内する。初回のbrew・runtimeを含む厳密なprefix検査前はメモリ上の段階だけを使い、記録ファイルは検査通過後から書く。更新処理が起動した導入はinstall.shの再実行へ誘導せず、mcs recoverの確認を案内する。
- shellは先頭4KiBのprefixを固定形式と既知fieldで確認する。Pythonのmcs setup checkは全体4KiB以内のregular・非symlinkのJSONをno-followで読み、形と既知値を検証する。両者の読取り範囲を混同しない。読めない・不正な記録の内容や例外文は出力せず、記録先がディレクトリやリンクなら既存の対象を保持し、記録失敗だけで導入を止めない。
- 親工程の9910acbc固定のinstaller・初期設定の関連21fileは、この追補時点で実行中で、終端・成功は未確認。前の8b750244固定の後続16file・709件成功は当時の結果として保持し、今回再実行した結果や全体suite・GitHub CI全ジョブの成功とは扱わない。文書担当はpytestを起動していない。
- 本番は70e4ae5eのまま。親工程の限定した読取りではdeadline_deferredとstall_check_failed:OperationalErrorを観測したが、独立した要約・薬剤集約の失敗は観測されず、postprocessは同じstallを二重に分類した表示だった。後続の自然実行ではstall・stage_errors・overshootは観測されず、期限による先送りが残った。警報分類は原因調査中で、実装修正と新しい変更記録は未確定。導入記録の追加で運用が回復した、患者コンテキストが完成した、医学的精度を確認したとは扱わない。新しいMacでの全導入、候補の本番適用・自然観測、公開は未実施で、既存Release下書きは未公開。unknownの自動解除・再送は行わない。
- source・tests・manifest・生成DEVELOPMENTは親工程の所有のまま、文書担当は手編集・再生成していない。今回の文書集約で旧110件の条件や図の流れは変わらず、件数表示のない図は再描画していない。

## 停滞チェックの観測診断の追補

- 文書の基準は `a2acf03f11e46625e4da00c9560217e96cf29512`。既存117件へmcslog-stall-observationの1件を加え、全118件へ集約した。新fragmentは原文のbytesを保ってarchiveし、既存117件のhash、旧110件のbytes、SVG・PNG・visual-sourceの3資産と過去の受入を保持する。
- 停滞チェックの観測失敗は固定語彙のstall_check診断へ記録し、result.errorsへ追加して収集・要約等の失敗として二重計上しない。status・reason・observationと、取得できた場合のSQLite主コード（下位8bit）を記録し、例外本文は保存しない。既存の停滞理由・degraded・閾値とlast_ok_atの更新条件は維持する。期限超過の説明は収集後の処理を含む実行全体へ、最終正常の説明は全監視項目が正常だった最終記録へ限定し、値と更新条件は変えない。
- 親工程は旧6cと新しい同一テストで、観測失敗をresult.errorsへ追加しない条件が1件失敗することを確認した。終了1、約1.835秒で、旧製品のbytesは不変だった。その後、文書担当はdelivery-reconciliation/health-final-result.jsonの終端を照合した。a2acf03f固定の健康監視関連15fileは546件成功・失敗0・error 0・skip 0・終了0、約132.866268秒で完了した。source_stamp_changed=[]、終了HEADは同じa2acf03f、終了時のstatusはcleanだった。文書担当はpytestを起動していない。
- 文書担当はdelivery-reconciliation/installer-final-result.jsonの終端を照合した。9910acbc固定のinstaller・初期設定関連21fileは859件成功・失敗0・error 0・skip 0・終了0、約553.7024秒で完了した。実行前後のHEADは同じ9910acbcで、source_stamp_changed=[]、終了時のstatusはcleanだった。前の8b固定709件成功と元45対象の2732件成功・1件失敗は当時の結果として保持する。今回の15fileや全体suite・GitHub CI全ジョブの成功とは扱わない。
- 過去のSQLite観測失敗は例外型しか記録がなく、直接原因は未確定。今回の分類や今後得られるSQLite主コードから、過去の原因を補って断定しない。前のpostprocess二重表示と後続の自然実行で停滞警報等を観測しなかった結果は、親工程の限定観測として保持する。新候補の本番適用・自然観測は未実施で、本番は70e4ae5eのまま。期限による先送りの解消、患者コンテキストの完成、医学的精度や運用回復を確認した結果にはしない。
- 親工程の同sourceのfullruff・gates 10/10・mine・readme・ownerの検査はすべて終了0。Devinはmergeの親関係と製品bytes、a2acのmetadataのみの差分、配布証跡524pathsとtopの不変、変更がhealthの2entryだけで不一致0、公式digestの再現を独立した読取りで確認し、親工程が受け入れた。本人はpytestを実行しておらず、動的根拠は上記の親工程による546件成功と分ける。追加の全体suiteやGitHub CIの成功とは扱わない。
- 新しいMacでの全導入、公開は未実施で、既存Release下書きは未公開。unknownの自動解除・再送は行わない。文書担当はsource・tests・manifest・生成DEVELOPMENTを手編集・再生成せず、図も再描画していない。本番適用と自然観測は次工程の予定で、この記録の時点では未実施。

## snapshot検証時間の確保の追補

- 文書の基準は `f67a5447c35bdd96ad2b7e14042b9419e240ca18`。既存118件へsnapshot-size-reserveの1件を加え、全119件へ集約した。新fragmentは原文のbytesを保ってarchiveし、既存118件のhash、旧110件のbytes、SVG・PNG・visual-sourceの3資産、過去の受入を保持する。
- 派生処理が使っていたDBサイズに応じたsnapshot検証時間と固定余白を残す期限を共通化し、意味解析の開始可否・LLM枠の確認・実行に同じ期限を使う。作業窓が60秒以下なら開始せず、枠の確認中に期限へ達した場合とDB情報を取得できない場合も新しい解析を始めない。推論の再処理の走査は1回5秒を上限とし、行数上限・再開位置・確定処理と行途中で中断しない条件を維持する。事前処理の秒数を記録し、その他の上限・カーソル・トランザクション、snapshotの検証・原子的な公開・最後の正常snapshotの保持は変えない。
- 親工程は旧5eの製品と新しい同一テストで、解析をdispatchしない条件が1件失敗することを確認した。ee15固定の関連13fileは439件成功・失敗0・skip 0・終了0、約295.846秒で完了し、source_stamp_changed=[]、終了時はcleanだった。製品bytesがee15と同じ最終f67では、比較を厳密化した1nodeも終了0。これらは別の実行として記録し、単一の全体suiteやGitHub CI全ジョブの成功とは扱わない。文書担当はpytestを起動していない。
- この追補時点の本番は5e4efe70で、snapshot更新失敗は親工程の実機観測として候補の合成検証と分ける。意味解析の期限とsnapshot検証時間を調整した候補が、過去の実機失敗を解消した証拠にはしない。f67の先行適用と自然観測は次工程の予定で、現時点では未実施。患者コンテキスト完成、医学的精度や運用回復も確認済みとは扱わない。
- 別途の自動復旧作業は未統合で、この119件の候補には含めていない。既存unknownの自動解除・再送は行わない。新しいMacでの全導入と公開は未実施で、既存Release下書きは未公開。文書担当はsource・tests・manifest・生成DEVELOPMENTを手編集・再生成せず、図も再描画していない。

## 自動復帰と限定解析集計の追補

- 文書の基準は統合merge `0cfec1af`（production source は c165b01e と byte 一致、生成 owner metadata を除く）。main `3e9a1010` を通常mergeで祖先へ取り込み、競合は `mcs/ingest/health_recovery.py` の1件のみで c165 blob を採用した。旧 `_retire_previous_restart` 方式の test は c165 の世代別 request 機構と非互換のため除外し、正式 archive fragment を保持して loose 重複を除いた。
- 自動復帰は、standalone 稼働ホストの停滞・子処理失敗・接続断と、Hermes 共有 gateway の配送 worker 起動失敗（`worker_start_failed`・コード版/PID/開始時刻の一致）を対象に、update.lock 取得後に要求ID・対象世代・回数を `data/auto_recovery.json` へ保存し、既存の終了待ち・再起動機構へ委ねる。結果不明・破損・in-flight の記録は上書きせず自動再送しない。gateway 側は対象PID・要求ID・更新ロックを既存復旧ツールへ渡し、世代交代でも結果不明を保留する。手動停止・更新・復元待ちは自動解除しない。
- 限定解析（`admitted_ids`/`shard` 指定・`cloud_only`）の `run_pending` は末尾の全履歴残件集計を省き、`left`・`left_estimate`・経過時間を null とする。対象限定なしの正確な残件集計と期限付き概算キャッシュは維持する。
- 既存119件へ `auto-recovery`・`scoped-extraction-backlog-count` の2件を加え全121件へ集約した。新fragmentは原文のbytesを保って archive し、既存119件の hash・過去の受入・図3資産を保持する。生成 DEVELOPMENT と release owner manifest を再生成した。
- Root runner 88523 は c165 関連20 file・729 passed・0 failed・0 skipped・rc0（556.667s・`source_stamp_changed=[]`）。別途保存の 489pass・853delta166pass・98cd9pass は部分結果であり全体 suite/CI の成功とは記録しない。文書担当は packaging 検証のための pytest を起動していない（下記の診断 test 実行は test 担当工程として別記）。
- `tests/patients/test_private_context_basis_diagnostic.py` は当初、参照する scenario 関数名の不一致を現行名へ合わせ（`aad05038` の改名に追随、assert は不変）、公式 runner で1 file 実行したが `baseline_pass_still_valid` の assert で失敗した。最小 probe の実測で、publication は `source_scope=[1]` の scoped identity で QUALITY_PASS を発行・保存するのに対し、診断 test が全体 scope（`source_keys` 未指定）で identity を再導出するため audit_key が一致せず 0 件になることを確認した（保存scope版は `meta.quality_audit_key`・保存PASS audit_keyと一致し `quality_passed`=true）。原因は製品契約ではなく test 側の fixture 不整合（scope 未透過）で、製品契約は自己整合している。Codex レビュー受入のもと、各 step の保存 `meta.source_scope` で snapshot/read proof を別に構築し `_quality_capture`・`_quality_input`・`_all_facts` へ scope を透過する最小 test-only 修正を実装し、baseline 採用時に再構成 `audit_key==meta.quality_audit_key` かつ `_quality_passed`=true を先行 assert、baseline/final の `source_scope==[1]` を assert とした。既存 assert は全て保持。診断 test と元 scenario の2 file batch を公式 runner で実行し 2 passed・失敗0・rc0（17.09s、実行中の source 変更なし。証跡は私有audit `final-package-07-r5/`）。一時 probe は削除済み。
- この追補時点の本番は f67a5447 適用済みで auto/scoped は未適用。初回 snapshot timeout 9355 の後、9357–9362 で6回 publish（9358/9362 は jobs-only、他 full は overshoot 0）。rollup deadline 先送りは残る。unknown 配送2件・OCR 表対応10件は保護 hold で未解消。Oracle browser GPT-6+Pro は UI 選択 verified だが backend 独立同定は不可。MCS 側の保守 gate が遅延 restart の結果不明を世代交代後も hold することを確認（Hermes native の保証ではない。native の遅延作用は PID だけでは除外できないため保留を維持する）。患者医療精度・新規 Mac 実接続導入・運用復旧は未検証。公式 master は 2026-11-08T06:41Z 確認で現行 20261013・19286件・同梱同版・offline checker rc0。公式 downloadMenu/yFile を stdlib urllib（no-proxy/no-redirect・32MiB上限・timeout60・1回）で 2026-11-08T07:10Z に取得し、ZIP 1,158,682 bytes・SHA-256 `c3efc8aa…`、member `y_20261013.csv` 6,083,884 bytes・SHA-256 `6fa8f08c…`、19,286行・全行42項目が候補 manifest と完全一致した（非機密JSON証跡は私有audit `drug-master-yfile-20261108/verify.json`）。新たな無断外部サービス・有料 fallback は追加していない。
- 公開前の最終受入根拠はこの制約を正直に記載する。新しい Mac での全導入と公開は未実施で、既存 Release 下書きは未公開。既存 unknown の自動解除・再送は行わない。

## OCR予算延長とLINE WORKS診断の追補

- 文書の基準は候補 `a6327313`。既存121件へ `ocr-local-inference-budget`・`lineworks-doctor-remedy` の2件を加え全123件へ集約した。新fragmentは原文のbytesを保って archive し、既存121件の hash・過去の受入・図3資産を保持する。`changes/` 直下の loose fragment は0件。
- OCRローカル推論の期限は `mcs/ops/mcs_ocr.py` の `LOCAL_CALL_S`=120→300 のみ。`_drain` が `attachment_inference.run_pending(stage='local')` へ渡す deadline は `min(ジョブ残り-5秒, 現在+300秒)`。callee 既定120秒・checkpoint contract identity・抽出HTTP300秒・semantic 600秒・共通local_llm既定90秒・原本OCR timeout_seconds既定120秒・DRAIN_BUDGET_S=900・終端5秒・slot優先・試行上限・paid/unknown・原文QC は全て不変。残り時間が raw_budget 以上 raw_budget+LOCAL_CALL_S 未満ではローカル段を後回しにして原本OCRを優先する既存判定を維持する。
- 検証は関連3fileの40件成功・失敗0・skip 0・終了0（証跡 `ocr-budget-r4/`）。実装の `_drain`→実 `run_pending` を通す合成検証で、80秒×2chunk のページが新300秒では1pass完了・chunk再送なし・ready、旧120秒では途中leafを保存して deferred になることを RED/GREEN で実測した（fake clockとcheckpoint seam・実モデル呼出なし）。処理速度の改善や実機でのタイムアウト解消を実測したものではない。
- LINE WORKS診断の修正は `mcs_setup.py` が削除済み `health_watch._LINEWORKS_ACTION` を import して ImportError になっていた問題を、既存 `health_watch._lineworks_actions()`（`_reason_rows` と同じ helper・`_lw_restart()` 内包）へ接続して解消した。古い定数や互換wrapperは復活させていない。対処文は監視アラートと同一で、darwin=launchctl kickstart・linux=systemctl --user restart・その他=両案内。辞書にない理由には再起動文を出さない。関連3file99件成功・失敗0・終了0（証跡 `lineworks-doctor-11/`）。remote CI run42433639313 の同 node 失敗（release系 `70e4ae5e` でも再現）を本修正で解消する見込みだが、CI の再実行成功は未確認。
- remote CI run42433639313 は Codex 独立確認で、失敗2job（lint-test/macos）の pytest 失敗 node は既知の LINE WORKS ImportError のみで追加 node・collection error・internal error・ログ欠落なし、残4jobは success と確定した。両 job の pytest diagnostics step は CreateArtifact の storage quota 超過で、これは製品の成否とは別のインフラ制限として保持する。quota 超過を成功・失敗のいずれにも数えず、候補 HEAD での CI 再実行は未実施。
- 親工程が既存 mcs.log 末尾8MiBを bounded read-only で再読し、08観測中の failed1 行に first_calls1/repair_calls1/length_stops0・repair_issues.structure1 を確認した。初回出力の構造検証不合格から修復呼出へ進んだことは確定するが、最終出力の詳細と timeout 因果はこの集約に無く未確定のままとする。「LLM呼出なし」「時間不足」とは断定せず、採用0・原因不明を正常化もしない。品質不適合は既存 gate で不採用となる正しい処理で、gate 緩和・試行数増・再送は行わない。
- この追補時点の本番は `2bda672d` 適用済みで、OCR予算・LINE WORKS診断の2修正は未適用。自然観測・公開・新しいMacでの全導入・既存unknownの解除・再送は行っていない。単一の全体suiteやGitHub CI全ジョブの成功とは扱わない。

## 公開前検査の修正追補

- 文書の基準は候補 `a669adb9` に本修正を積んだ作業tree。既存123件へ `release-candidate-checks` の1件を加え全124件へ集約した。新fragmentは原文のbytesを保って archive し、既存123件の hash・過去の受入・図3資産を保持する。`changes/` 直下の loose fragment は0件。
- 修正は3件。`install.sh` の導入段階検証2箇所と `deployment/install/runtime.sh` の完了記録呼出で、シェル予約語 `done` を引用符付きへ直した（`[ "$_ipf_stage" = "done" ]`・`_install_progress "done" complete`）。意味・型検査・構造は不変で、`runtime.sh` を正本として `INSTALL_RUNTIME_SHA256` pin を再生成した。CI hygiene の SC1010 はこの引用符で解消する。
- `scripts/development/sync_release_notes.py` は、書込み後の識別再照合で対象に由来する日時情報 `created_at` の変化も変更として拒否していた。CI run42465260744 の draft job は PATCH による本文更新の後に識別照合で fail-closed 停止した（書込み自体は成功・本文は一致済み）が、旧 `created_at` を保存していないため実際に変化したfieldは未特定である。`created_at` はReleaseの作成日時ではなく対象に由来する日時metadataのため、PATCH応答と再取得の2か所の比較からだけ外し、IDENTITY定義・型検証・書込み前スナップショット比較・他6項目・本文一致の照合はすべて維持する。合成テストは `created_at` のみの正常な変化を受理して再書込みしないこと（draft=False/published_atあり と draft=True/published_at=None の両形状）、書込み前のずれ・欠落・不正型・他項目の変化・本文不一致を引き続き拒否することを固定し、旧コードで RED・修正後 GREEN を確認した。実CIでの再現可否は、更新後の次の必要な同期で確認する。
- `tests/meta/test_install_log_sink.py` のサービス段階ハーネスは `install.sh` と `runtime.sh` から該当区間を切り出して実行するが、導入中断記録の追加で生まれた `_install_progress` 呼出を未定義のまま残していた。既存の出力関数と同じ形の no-op スタブ `_install_progress() { :; }` をハーネスへ補い、CI macos で失敗した5件を解消した。製品コードと各assert（サービス有無・bytes・helper・private・symlink不変・exit）は一切変更していない。進捗writer自体は既存の導入進捗テストで実検証を維持する。
- 検証：focused の `test_install_log_sink.py` 5件・`test_sync_release_notes.py` 関連は全件成功・終了0、ruff All checks passed、shellcheck/bad substitution なし、bash -n 両ファイル OK、fragment lint error0。RED 証拠は CI run42465260774（hygiene SC1010・macos `_install_progress: command not found` 5件）と run42465260744（PATCH後の識別照合 fail-closed・変化field未特定）を再利用する。ローカル再現でも同一内容の失敗を確認済み。
- 節目batch（tests/release＋install系meta 9file＋test_install_log_sink.py の対象コマンド）は終了0で完了した。`-q` 実行のため明示的な件数・JUnitは無く、進捗表示のドット計測で約297件・失敗表示0（証跡 `release-check-fixes-15/boundary-batch.log`）。このbatchは文書編集と並行した作業treeでの実行であり、最終SHAの全対象source不変の証明とはしない。文書/manifestを読む tests/release 3fileと更新した sync file の最終tree再確認は49件成功・終了0（`docs-readers-retest.log`）。候補CIの全job再実行・push・tag差替え・Release公開・本番適用は未実施。この追補だけで合格とは扱わない。

## 図表主体の文書整備追補

- 文書の基準は公開前検査の修正を積んだ作業tree（`be04f8eb` 以降の文書変更を含む）。既存124件の変更記録・archive・hashは変更せず、README手書き領域の構成見直しと説明図3枚の追加だけを行った。製品コード・既定値・検証条件・anchor・AI導入route判定・外部送信表・安全境界は保持する。
- 追加した図は `data-processing-v112`（全体処理）・`patient-find-v112`（患者検索）・`setup-steps-v112`（導入4段階）の3枚。いずれも hermes-mcs の Diagram Design profile（warm paper・MCS green・390px幅）で作成した合成説明図で、HTML原稿は `docs/releases/v1.1.2/*-v112.html` としてrepoへ保存し self_check を通過後、`export_svg.py` でSVGを書き出し、macOS qlmanage + ImageMagick のwrapper/crop経路でPNGを生成した。訂正後の寸法は data-processing 390x660・patient-find 390x640・setup-steps 390x640。初回生成PNGはwrapperへXML prologを含めたため一部がXML parser error表示となり、Rootの実見で検出して wrapper から prolog を除き nested `<svg>` へwidth/heightを明示して全3枚を再生成した。実画面・測定結果・患者データを含まず、来歴・ハッシュは `visual-source.json` の artifacts へ登録した（HTML原稿3件も含む11エントリ）。
- READMEでは data-processing を処理節の先頭図、patient-find を患者情報節、setup-steps をクイックスタート節へ配置し、既存 cloud-continuity は補足detailsへ畳んだ。図の意味は Root 指摘に従い訂正済み：data-processing は収集元をMCSとし、通知・検索は患者コンテキスト完成を待たない並行枝、Jevは別経路の外部監査。patient-find はMCSの返信を通知カードで確認する場面から開始。setup-steps は install_progress.json を「最後に開始した段階の記録」とし自動再開を主張せず、setup check 合格と接続受入を区別する。既存の画面画像11点（slack-gallery 含む）は全て保持し、07-patient-summary.png の参照も復元した。CHANGELOG・版README・Release本文には同3図を v1.1.2 の release download URL で追加した。PNGの upload と外部表示は未実施で、URLは既存 cloud-continuity と同じ確定パターンの予定参照であり、upload・表示の検証済みを意味しない。
- 検証：`update_readme.py --check`・`release_notes.py check` は終了0。訂正後のPNGは prolog 除去済みwrapperから再生成し、XMLとしてwell-formedなSVGと、MCS-greenアクセント画素・warm-paper背景を含む実描画であることを画素統計で確認した（白画素率97%台・green系0.12〜0.21%）。細部の目視確認はモデルの画像表示制約により未実施で、Rootの独立確認へ委ねる。文書を読む tests/release 系の再確認は公開前検査追補の49件成功を基準とし、今回の文書変更が読む範囲は同一file群のため重複全量実行は行っていない。単一の全体suite・候補CI全job成功・公開・本番適用は未実施。
- r8/r9 追補：3図の aria-labelledby/id を図slug固有（`<slug>-title`/`<slug>-desc`）へ修正してSVGを再exportし、PNGはXML prologを含まないflatな `<g transform>` wrapperをqlmanageで描画してcropする経路で再生成した（nested `<svg>` はqlmanageで配置がずれるため不使用）。寸法・内容は前項どおり。CHANGELOG [1.1.2] の可視部は目的別へ再構成し、冒頭の画像は cloud-continuity 1枚に限定、3図は新機能節内の関連項目の直後へ分散配置した。これにより README 生成ブロックは最新更新欄へ新図を重複輸入せず cloud-continuity のみとなり、手書き領域の3図配置と重複しない。更新時の注意は 既定値・外部送信・人承認・移行・必要な再起動・回数/対象の変更 の6区分の表へ集約し、領域別の更新時の扱いを表で保持、個別の実装詳細は技術詳細124件へ委ねる構成とした。Slack通知先・通知カード互換・検索・その他の不変条件・復旧手順・更新コマンド条件・auto_recovery・OCR期限の個別条件は引き続き可視の箇条書きで保持する。主要表は5行へ拡張した。release_notes.py check・update_readme.py・readme_release.py の各checkは終了0。

## この資料では確認していない事項

- GitHub CI全ジョブの実測成功。ローカルの必須検査と失敗項目の再検証とは区別する。
- 最終公開SHAの固定と、今回の投稿単位の判定修正の本番適用・運用受入。
- OCR後のローカル解析と患者コンテキスト作成の完了、既存の通知保留の回復。
- GitHub 上での画像表示、Release本文・asset・tag・公開。画像の来歴とハッシュは保持し、外部表示は未検証。
- Git への push・tag・Release 公開（ユーザーの公開指示は受領済みだが、運用受入待ちで未実施）。

最終リリースSHAは未固定です。`visual-source.json` は図の生成時の記録を保持しており、今回の検証候補の受入や公開を意味しません。
