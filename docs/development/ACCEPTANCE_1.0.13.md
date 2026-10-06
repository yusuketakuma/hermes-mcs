# 1.0.13 リリース受入票

2026-10-05。対象は統合HEAD `36ee3e3af5b91dca6ccdff229aa80506049d4d98` と本作業の文書・release記録差分。
ユーザー依頼は全worktree統合、1.0.13公開、本番適用。公開・配備の実施結果は親の後工程で追記する。

## 全体監査とソース同一性

全体監査は `docs/development/audits/1.0.14/full-maintenance-20261005/{PLAN.md,inventory.json,findings.json}`。
最終受入正本は `/Users/yusuke/.herdr/evidence/mcs/final-I-20261005-1cf0505/final-acceptance.json`（SHA256 `b7781cb0776303f861c7f77f1f2b0007b1bf708d198268e7432020a4d807d230`）。
状態COMPLETE、runtime I=`1cf0505eb5c9155d19afa3e3ba218f2640096d20`、record H=`3b58ba78827f8871edc71baaa1749a29311d3ba7`。
統合HEADとHのtree差分ゼロを親が確認した。今回の差分はrelease資産だけで実行ソース・test・依存を変更しない。

| 領域 | 判定・根拠 |
|---|---|
| コードの無駄・リファクタ | 確認済み。inventory/維持した互換入口、全findingと修正packetを再利用 |
| フォルダ・ファイル整理 | 確認済み。tracked911、台帳913件、REVIEWED888/EXCLUDED25、UNSEEN/PARTIAL0 |
| 冗長処理・性能 | 確認済み・制約あり。同一コードの100/1000件単独control成功、実機性能未計測 |
| 正しさ・互換性・保存 | 確認済み。旧版schema/更新経路・同意/復旧/再実行の合成検証 |
| セキュリティ・プライバシー | 確認済み。秘密/PHI不使用、送信/所有者/承認/期限gate維持 |
| 依存・CI・供給経路 | 確認済み。固定SDK・Hermes別lane検証済み。最終release SHAのCIは親担当 |
| テスト・build・配布物 | 確認済み・制約あり。下記の全体/SDK/archive/旧版更新証拠を再利用 |
| 文書・画面・運用・release資産 | 本作業でREADME5領域・131記録を照合。生成/画像/release検証を実施 |

独立レビューは `PASS_WITH_DECLARED_CONSTRAINTS`、technical_acceptance=true、未解決blocking0。
追加監査・レビュー連鎖は行わない。実機・人手・相手側の条件を合成受入で解消したとは扱わない。

## 実行ソースの検証証拠

- Python3.10.22: 全量7391成功・8skip、exit0。
- Python3.13.12: 全量7390成功・性能1失敗・8skip、exit1を保持。同一ソース/基準30秒の100/1000件単独control2成功で当該失敗を補完し、他の未変更成功を再利用。全量greenとは記載しない。
- record Hで固定SDK25成功、実Hermes17成功（Python3.13.16）。実サービス通信はスタブ。
- H archive実起動50件（通常46＋ledger4）、Python3.13.12/3.10.22の出所を各実stdoutで確認、exit0・timeout0・guard違反0。
- B→Hの完全patch/replayで911ファイルのbytes・Git/fs mode一致。証拠99ファイルのhash検証済み。
- source/record同一性とreceiptを再利用。監査の初期失敗ログは上書きしない。
- 本番Python3.11環境でwhole runnerは `hermes_yaml` import不足によりintegration collect失敗（`/tmp/mcs-release-1.0.13-runtime311-tests.log`）。固定Hermes laneの成功とは別結果。親がtests/のみの結果と本番環境調整を追記する。

## 新規導入（更新とは別の条件）

| 経路 | 現在の受入 | 根拠・制約 |
|---|---|---|
| macOS/Hermes新規 | 合成・隔離検証済み | `tests/meta/test_install_sh.py` の一時HOME/スタブでinstall→rerun収束・中断再実行・依存失敗・recovery選択、全体receiptとH archive起動を再利用 |
| macOS/standalone新規 | 合成・隔離検証済み | `tests/meta/test_install_sh_standalone.py` のHermes非導入/preflight/runtime選択、固定SDK lane25件とH archiveを再利用 |
| LINE WORKS独立adapter | 合成契約検証済み | 署名/認可/本人確認/配送失敗の既存テストと全体receipt。実Callback/実認証/外部接続は未確認 |
| 実機の取得・依存インストール・設定・起動・doctor・代表操作 | 未確認（公開準備時点） | SDKインストール境界・launchd・実MCS・通知先の実受入を合成スタブで代用しない。親の本番適用結果を別途記録 |

SDKを実importしたlaneもAPI送信は合成で、患者データを使用していない。
配布物の隔離起動は取得/初期設定/常駐開始を全て実機で行った証明ではない。

## 全過去版からの更新（全16経路）

正本 `tests/fixtures/schema_upgrade/update-paths.json` のcurrent_paths16件を全件追跡。
更新先はv1.0.13（schemaは現行9）。直接更新の合成経路を確認し、段階更新を必須化しない。

| 更新元 | runtime | 入口・条件 | 判定 |
|---|---|---|---|
| v1.0.0 | hermes | 手動merge/reinstall | 合成契約検証済み（実機未確認） |
| v1.0.1 | hermes | 手動merge/reinstall | 合成契約検証済み（実機未確認） |
| v1.0.2 | hermes | 手動merge/reinstall | 合成契約検証済み（実機未確認） |
| v1.0.3 | hermes | 対象版bootstrap→plan/apply | 合成契約検証済み（実機未確認） |
| v1.0.4 | hermes | 対象版bootstrap→plan/apply | 合成契約検証済み（実機未確認） |
| v1.0.5 | hermes | 対象版bootstrap→plan/apply | 合成契約検証済み（実機未確認） |
| v1.0.6 | hermes | 対象版bootstrap→plan/apply | 合成契約検証済み（実機未確認） |
| v1.0.7 | hermes | 対象版bootstrap→plan/apply | 合成契約検証済み（実機未確認） |
| v1.0.8 | hermes | 対象版bootstrap→plan/apply | 合成契約検証済み（実機未確認） |
| v1.0.9 | hermes | 対象版bootstrap→plan/apply | 合成契約検証済み（実機未確認） |
| v1.0.10 | hermes | 対象版bootstrap→plan/apply | 合成契約検証済み（実機未確認） |
| v1.0.11 | hermes | 対象版bootstrap→plan/apply | 合成契約検証済み（実機未確認） |
| v1.0.12 | hermes | 対象版bootstrap→plan/apply | 合成契約検証済み（実機未確認） |
| v1.0.10 | standalone | standalone host経由 | 合成契約検証済み（実機未確認） |
| v1.0.11 | standalone | standalone host経由 | 合成契約検証済み（実機未確認） |
| v1.0.12 | standalone | standalone host経由 | 合成契約検証済み（実機未確認） |

`tests/ops/test_supported_update_paths.py` は実plan/precheck/operator apply・lock/journal・事前backup・post_merge/migration・再apply・束縛同意rollback、reinstall失敗、中断とbootstrap一時tree cleanupを確認。
Git object/merge/reset、環境preflight、service supervision、installer process、通知はスタブ。
手動v1.0.0～1.0.2は自動更新の適格性を宣言せず、手動merge/reinstall/migration/復元の合成経路を確認。
standalone v1.0.10～1.0.12は外部applyがhostを迂回しないことを確認し、実host協調は未確認。

`tests/core/test_supported_schema_upgrade.py` と `origins.json` は各版の原DDL由来の完全合成DB、設定・投稿・依頼・添付保持とbackup/restoreを確認。
同じschemaは来歴hashでまとめる。旧schema0～4/6の原DDL来歴は不足し、創作fixtureで補わない。
履歴26経路は保存済み記録の出所を保持するが、過去updaterの全binary再実行ではない。
macOSの旧watchdogでSQLiteが影響版の場合はmerge前に停止し、checkout外の安全な復旧Python選択または明示no-recoveryが必要。

rollbackは境界に束縛した人承認・reason・receipt・適切なbackup復元を要し、非互換downgradeの無条件安全性を宣言しない。
復元後は通知/配送照合を保留し、未確認実機経路を成功へ置換しない。

## release資産と後工程

未公開候補1.0.13の既存archive88件と未release43件を隔離stagingへ集め、既存release_notes.py buildで131件を再生成。
1.0.12以下のCHANGELOGと既存archive内容は保持。README生成ブロックは手編集せず更新。
README5領域はreadme-review.json、画面の整合はSlack/LINE WORKSの既存checkへ記録。
最終CI・commit/push/tag/Release公開・本番サービス適用・recovery Pythonの選択は親担当で、この票作成時点では未実施。
本番設定のmetadata/urgency/外部取得など任意opt-inは自動で変更しない。

## 文書作業の最終検証

release/readme/生成表/Slack7組/LINE WORKS2組のcheckとdiff --checkはexit0。tests/releaseは22成功＋26 subtests成功、exit0。最初のPython3.14 runnerはpytest未導入でexit1、既存本体venvを明示して再実行。過去CHANGELOG/既存archiveの保持と43件の移動byte一致を確認。Release exportはCHANGELOGから生成済み。
