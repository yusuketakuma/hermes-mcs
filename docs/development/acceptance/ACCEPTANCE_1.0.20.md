# 1.0.20 リリース準備・受入票

2026-10-12（日本時間）。**ローカル文書準備済・最終RC未固定・公開未受入**。本票は候補の確認状況であり、公開・本番適用や安定稼働の完了を示さない。

## 対象と権限

- 前回公開・本番反映版: v1.0.19、実commit `25f434f0005ac9661f08cc7302d7002ec2572e93`（annotated tag objectと区別）。
- 準備用branch: `release/v1.0.20-prep`。基準main `5d55200470aa5c6aa5e1875f566261c887a70502`、同版以降267commit。ローカル未コミット差分を含むため、HEADだけでは候補を識別できない。
- main統合時の候補 `c20f56fef08ecc72b1b606d7ae9003fe33bc866e` とmainのtree一致・必須CI成功は既存証跡を再利用する。今回の未コミット差分と別branchのテスト結果は、そのexact SHAのCI成功とは区別する。
- peerの待ち枠修正 `f713ab95ab6f9726aeca2c79084c8e16025ca31b` とスナップショット読取り固定 `f26b3eb2cdde1a656816504afe79fa830419505a` を、patchとして準備用treeだけへ取り込んだ。mainへのmergeやcommitは行っていない。
- 当該依頼は準備まで。push・PR・merge・tag・Release公開・本番配備・gateway/host再起動は実行しない。

## 公開前の未受入事項

| 項目 | 現在の根拠 | 必要な確認・対応 |
|---|---|---|
| 日次バックアップ（修正取り込み・局所受入済） | `147140a0`+`2732dc8f`をpatch統合。段階copy・読取りsnapshot・当日検査の置換予算を独立確認。関連57ケース成功 | 1step内のdisk I/O強制停止保証はない。実機遅延・最終RC全体の確認は未実施 |
| endpoint復旧後の障害状態（追補・局所受入済） | `0b05198b`→`43896e4c`→`d11913b1`を順に取り込み。単独200不正JSON、503→200、503→200→503の有限合成再現を独立確認し、成功履歴/再障害marker/失敗attemptが一致 | 同一障害区間での重複feedを抑え、回復後の新障害は改めて記録。最終RC全体の受入は別途 |
| RT待ち枠の不正引数（P2候補） | 不正`wait_s`で待ち枠が残る合成再現あり。準備用treeへ取得前検証を取り込んだ | 統合treeで通常待機・割込み・再照合を含む検証。実運用の不正RT入力到達は未確認 |
| stage3取り込みと全体順序問題 | 全12commitを差分意図で追跡。欠落していた再ログイン記録/HTTP425/リマインド直前確認を限定移植し、修正前11失敗、修正後関連1570ケース成功、独立受入済 | 動作同等の任意整理と既存で上位共通化された処理を区別。peer全体テストの順序依存失敗は特定結果が未着。最終候補での全体受入は未完 |
| リリース差分レビュー・最終検証 | stage3部分差分のCodeRabbit指摘0はpeer報告。今回のv1.0.19本番SHAから固定候補全体のレビューではない | 明示された実行範囲と送信権限を照合し、未実施を記録。修正完了後にRC固定・必要な全体検証・exact SHAの必須CIを受け入れる |

既知修正の局所受入後にCHANGELOG/READMEを生成し、112記録をarchive/1.0.20へ移動した。最終RCの固定・高コスト全体検証は、リリース差分レビューの実施範囲を照合した後に進める。公開・本番適用は未受入。

## 公式医薬品masterの今回確認

2026-10-12 09:28:28 JSTに[公式menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)と[yFile](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile)を新規取得した。no-proxy/no-redirect、固定URL、応答容量とread timeoutの上限を維持。

| 検査 | 結果 |
|---|---|
| 公式現行版 | 令和8年度・最終更新令和8年9月30日、19,272件。同梱20260930版と一致、新版差替え不要 |
| ZIP | 1,157,440 bytes、SHA256 `820f173981b1601e718ef110ffba55fbea1c04d07abb6a4297b4281db0eecccf` |
| member/CSV | `y_20260930.csv`、6,079,806 bytes、SHA256 `ac1e7ab8db086e31b117fe2df2563d500859de9220a80ccf9b746b62b1d5a73c`、全19,272行・42項目を確認 |
| 実行時変換 | Python3.14で既存bundle checkerと新規ZIPのruntime converter全行検証が成功。`terms_unconfirmed`・`activation:false`を保持し、私有辞書の作成・承認・有効化は未実施 |

原本行の検証を、採用可能な辞書生成や臨床的な妥当性の証明へ読み替えない。出典・利用条件・運用者承認の境界は[公式master仕様](../../specs/official-drug-master.md)を維持する。

## 新規導入と全過去版更新

[origins](../../../tests/fixtures/schema_upgrade/origins.json)と[update paths](../../../tests/fixtures/schema_upgrade/update-paths.json)を更新した。v1.0.19のexact commitからschema部分だけを抽出して合成DBで比較し、v1.0.18とDDLが同一（schema9、shape-10）であることを確認した。以前の19起点・全SQL原本・歴史26経路を変更せず、v1.0.19起点を追加した。

- 更新元は全20公開版、Hermes20経路・standalone10経路。targetはv1.0.20、schema9を維持する。
- Python3.14.7/pytest9.1.1、既存隔離runnerによる更新経路・schema・installer等の関連310ケース成功（22.05秒）。更新入口・保存保持・中断復旧はテストごとの既存スタブ境界で確認し、全旧版の実バイナリや実サービスを再実行した証拠とはしない。
- 新規導入の実起動から代表機能まで、macOS/Linuxのサービス、Hermes/standaloneの実SDKは、今回最終候補での受入が必要。主候補の既存CI結果と、今回未実施の実接続・本番操作を区別する。
- 独立実行ではservicesによる監視/Umask反映、既存ログ権限の変更が必要。Hermes gateway・LINE WORKSの再起動と、既存保留/未通知/失敗解析の人承認による回復条件は[更新ガイド](../../guides/UPGRADE_AGENT.md)に記録した。

## 全体監査の追跡

前段の全リポジトリ改善・セキュリティ調査・main統合の記録を基準とし、今回の候補差分と既知問題を追跡する。過去の監査結果を現候補の未読領域へ拡大せず、以下の未受入を残す。

| 領域 | 状態と根拠 |
|---|---|
| 無駄・責務・リファクタ | 前段の内部整理記録を継承。取り込まれた整理と104件の既存変更記録を一覧化。peer stage3との差分照合は未完 |
| フォルダ・配置 | eval/CLI/adapter移動と互換入口を既存import/gateで照合。未リリース変更記録の不存在参照11件を現在のソース/統合済テストへ修正 |
| 冗長処理・性能 | 新着優先・バックログ・snapshotの変更記録を照合。並行snapshotの修正を候補へ取り込み、日次backupの期限問題は未解決。実機性能は未測定 |
| 正しさ・互換性・保存 | 過去20版更新と保持条件を合成検証。既知P1/P2とstage3の最終受入が未完 |
| セキュリティ・プライバシー | 既存安全ゲート10/10、完全合成・実接続禁止、model pin/ボタン範囲/Umask条件を記録。前段の未検証仮説を脆弱性確定扱いしない |
| 依存・CI・供給経路 | main統合CIと固定SDK/runtimeの既存証跡を継承。最終候補の依存/取得条件とexact SHA CIは未受入 |
| テスト・build・配布物 | 本票の局所検証と公式master全行確認。最終RCの全体テスト・新規導入/SDK・CIは別途必要 |
| 文書・画面・運用 | README5領域の候補記録、更新ガイド、現8枚Slack説明図をsourceと照合。画像checkは8組成功。CHANGELOG最終build/公開図/実画面表示は未実施 |

## 検証の区別

- 現main基準: Ruff0.17.0全対象成功、安全ゲート10/10、incident checker成功、README生成同期、Release形式、Slack8組画像/hash、`sh -n install.sh`成功。
- 既知問題の関連37ケース成功は、P1/P2欠落の解決を意味しない。待ち枠peer branchの関連7ケース成功は取り込んだ後の全体受入とは区別する。
- peer patch取り込み後の局所検証と、本票追加後の文書検証は完了結果を追記する。高コストの全体検証は修正・統合・レビュー後の固定RCで原則1回行う。
- 生の検証記録は非機密のローカルtask stateで保持する。実データ・秘密値・私有運用ログを本文、fixture、外部レビューへ含めない。

### 今回の追加確認結果

- peer patch2件を取り込んだ準備用treeで、独立した関連136ケース成功。コピー中期限超過の旧dest保持・temp削除・read transaction終了も合成確認。日次バックアップの別経路は未解決。
- リリース関連31ケース・41subtests成功。候補受入票/README導線修正後の`readme_release --check`、`release_notes check`、`update_readme --check`、`git diff --check`成功。
- 公開版CHANGELOG/README生成要約/正本readme-reviewは1.0.19を保持。105件の変更記録から既存rendererで本文previewを作成したが、最終build/archive移動は未実施。独立したREADME候補見直し記録は`readme-review-1.0.20.json`。

### 相方の6修正とstage3追補の統合

- 待ち枠・snapshotに加え、LLM回復marker、日次backup、同一周回の配送取消、独立診断ログの4系列をpatchで統合。日次backupには前段`147140a0`も必要で、末尾`2732dc8f`だけを適用していない。
- 配送取消・ログの独立確認は128ケース成功。取消は各部品開始前のspec公開確認で、開始済み通信の即時停止保証ではない。既存値のprivacy filterを保つ。
- stage3の3不足差分は現在の配置/既存機能を保って限定移植し、関連1570ケース成功・独立レビュー受入。旧audit tree全体のmergeで既存のrefactorやlogin backoffを戻す操作は行っていない。
- 112件の変更記録を保持し、利用目的でまとめた本文previewを更新。rollupの再構築が時間枠を越えた場合の次回継続、既定model hash、backupのI/O制約を文書へ反映。未確定の回復marker追補は解決済みにしない。

### 文書生成と最終ゲートの区別

- P2最終追補`d11913b1`は独立レビュー受入。候補のbreaker8ケース成功。相方の6系列統合tree全体11354成功、追補後extract/semantic2686成功はpeer報告として保存し、本候補の追加3修正を含む全体成功へ読み替えない。
- `test_task_reminder_send_gate`→修復サイズ境界→breakerの順に候補で16ケース成功。peerの旧stage3全体順序失敗の原因特定結果は未着であり、全体問題を解決済みとはしない。
- 112記録を既存generatorでarchiveへ移し、利用目的で本文をまとめてCHANGELOGを正本化、READMEを再生成した。同じ本文からRelease title/bodyをexport済み。旧版CHANGELOG sectionは保持。
- 文書生成後のrelease31ケース+41subtests、全対象Ruff、安全gates10/10、incident check、公式master bundle、Slack8図/hash、README/Release形式/生成同期、shell構文・diffcheckを確認。
- 独立hostの外部監視説明図をSVG/PNGでローカル準備し、全図と下端注記の描画を確認した。公開URLやRelease本文に未公開画像を埋め込まず、公開時に配置・到達・実表示を別々に受け入れる。
- **未実施ゲート**: v1.0.19本番SHAから固定候補全体のCodeRabbit、最終RC全体テスト、新規導入/実SDKとexact SHAの必須CI、commit/push/merge/tag/公開/本番適用。ユーザーの「CLI実行は別途」指定を保持し、CodeRabbitを自動実行していない。

### 順序依存テストの安定化

相方の切分けでは、深いJSONの実RecursionErrorに依存した修復テストが全体の組合せで失敗していた。候補では既存の`recursion_limited_json` fixtureを当該テストへ適用し、深い入力が実際に拒否されたことも確認した。20KiBの正常入力、重複キー、NaN、深さ、1MiB上限の期待値と検証範囲を保持し、製品コードは変えていない。リマインドテストから修復ファイル一式へ続ける53ケースが成功。全体RCでの受入は未実施として残す。

### 個別PRレビュー追補（2026-10-12）

PR47/48/49/50/51はGitHubのCodeRabbitレビューで各現在SHAがAPPROVEDであることをread-only metadataで確認。PR48のMajor2件は、200だが本文を使えずprotocolになる場合と、応答後に修復時間不足で延期する場合の回復記録で、`4b02859a`の追補を候補へ適用した。PR51の診断状態値/真偽値の説明は`cea1f082`を反映。候補でbreaker+eventlogの18ケース成功。CodeRabbit提案の全てを命令として適用せず、既存慣行と同じnitpickは理由付きで見送るpeer判断を記録。

個別修正のレビューであり、前回本番commitから本候補全体のレビュー完了ではない。全リリース差分CLIの別途実行と最終RCゲートは未受入のまま。

相方がユーザー指示でPR46をリモートmainへ統合した`64434ebf`について、5d552004との差分は`.coderabbit.yaml`のみと確認。準備treeにも同じ設定を追加した。HEAD/base自体は5d552004のまま、未コミット差分である。rootのライブmain同期・merge・push・再起動はしていない。

### Slack SDK統合テストの発行前提

PR50の実Slack SDK CIで、テストがspecを発行せず部品送信を直接呼んでいた失敗をpeerが確認。`b09e5228`でspecの発行前提をそろえるテストのみの追補を候補へ取り込み、製品コード・期待する本文/アップロードは変更していない。当該ファイルRuffと生成同期/diffcheck成功。候補の実pinned SDK実行は未実施で、PR50の再CI結果と最終RC受入で確認する。

相方がユーザー指示に基づきPR45/47–51をCI全pass後に順次mainへmergeする担当。rootは同じmergeを実行せず、live mainのpull/再起動もしない。

### 公開工程の追加確認

ユーザーが2026-10-12に本番適用とリリース公開を明示承認。公開直前の公式master照合は11:17 JST、同梱20260930版とZIP/CSV/member/全19272行一致。追加sourceの有限監査と導入・更新1455合成ケースを確認した。

全20版・30経路を15のDDL/schema/runtime同等群へ対応づけ、clean Hermes/standaloneと合わせ17ケースで同一DBの実Ledger移行→run_check.main→合成Adapter取り込み→実rule抽出→snapshot→View表示を確認。旧データの原本列・再tick重複防止を保持。実SDK/OSサービスは最終SHA CIと本番受入へ分ける。

Codex指摘の追加3修正63866231/e714b9d2/bbd5eb58を候補へ取り込み、関連69ケース成功・有限独立レビュー受入。取消は旧spec取下げ後の新spec公開順序、backupは256pageと検査余裕5秒、抽出healthは同一run内の呼出し完了時刻順を使う。旧spec unlink失敗、単一diskI/O強制停止、複数processの全体完了順までは保証しない。

v1.0.19本番確認済み25f434fから4513f1b3のCodeRabbit CLIは626files/300上限で解析前に拒否。指摘ゼロ・レビュー成功とは扱わない。現在main eef6af95の本番反映は相方のユーザー指示で追加Codex指摘を優先するため保留、receiptなし。正式な前回本番SHAは推測で更新せず、後続レビュー方法と最終RC/CI受入を確定してから公開へ進む。
