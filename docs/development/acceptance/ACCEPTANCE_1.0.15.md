# 1.0.15 リリース受入票

2026-10-06（日本時間）。前公開版は `v1.0.14`、リリース文書開始前のソースは
`baa4f6de21c6ee86d2c733b17c277353abd707cb`（main、全worktree統合済み）。
以下は公開準備時点のローカル受入と既存証拠の照合結果。最終リリースcommitのCI・tag・公開結果はこの記録の後に確認する。

## 全体監査と再利用範囲

基準は [round3全体監査](../audits/1.0.15/round3/stability-audit-round3-1.0.15.json)、
[round2監査](../audits/1.0.15/round2/stability-audit-round2-1.0.15.json)、
[1.0.14全体inventory](../audits/1.0.14/full-maintenance-20261005/inventory.json)。
`git archive baa4f6d` の追跡1226ファイルをメモリ内で照合し、round3の全1143対象について
SHA256一致1020・変更123・欠落0を確認した。相対symlinkはrepo内の対象内容で照合した。
他の既存監査JSONにも現SHA一致を探し、再利用できる現物は同じ1020件だった。

変更123件の内訳は実行コード36、配備・build3、文書・画像36、契約・変更記録28、
過去変更記録2、検証資産18。round3対象外83件は変更記録40、監査JSON等文書20、テスト23。
変更・新規対象をhash一致扱いにせず、変更記録・共有経路・今回の合成回帰で再確認する。
監査JSON自体の再帰的self-hashは対象外、生成OpenWikiは生成元・用途と既存制約を再利用する。

| 全8領域 | 対象と状態・根拠 |
|---|---|
| コードの無駄・リファクタ | core/extract/semantic/notify/views、adapters、standaloneの既存全域証拠を1020件のhash一致範囲で再利用。変更36実行ソースのうちadapters/plugin/standaloneの14件は現hashと旧hashの全差分・共有呼出経路を別担当が精査し阻害事項なし。残る22実行ソース（親14・通知/更新担当8）も差分と共有呼出元・既存回帰資産を確認し、配備3資産を含む全39件の現精査を完了。行数だけの分割・推測的削除なし |
| フォルダ・ファイル整理 | 全1226追跡対象とround3 inventoryを照合。acceptance/plans/auditsの移動・互換導線は既存記録、flat importは生成・CIゲートで確認。歴史migration・互換入口を保持 |
| 冗長な処理・性能 | journalは一括・flat行走査と同時刻の既存順序、不正timestampの配送別隔離を両merge stageから精査。関連29テスト成功。索引・token delta・抽出走査は既存合成回帰を再利用。実患者数での速度改善率は未測定 |
| 正しさ・互換性・保存 | schema9を維持。旧版DDL・原全列・添付・FTS・更新/再実行/中断/rollback/復元同意の永続matrixを今回220テストで検証。現共有経路の変更精査も完了 |
| セキュリティ・プライバシー | 全1226追跡対象のsecret-patternと保護対象パスgateは検出0（親の同タスク結果）。no-proxy/no-redirect、承認・reason・receipt、添付保存先制限を合成回帰で確認。患者snapshotは維持。保存済み抜粋の取得失敗済み返信だけは既読化を妨げない条件をREADME・SECURITY・利用者ガイドへ明記。実認証・実データ操作なし |
| 依存・CI・供給経路 | コアstdlib、standalone14固定pin、Hermes固定refとCI/installの整合を親が確認。install.sh/pinsはv1.0.14から不変。2026-10-06にGitHub Global Security Advisoriesで14pin全照会成功・該当なし。GitHub収録情報だけであり、未知/未収録脆弱性の不存在は主張しない |
| テスト・build・配布物 | baa4f6dの全量隔離runner exit0を再利用。追加旧版fixtureのmatrix220件成功。必須static gates、README生成整合、Slack7組・LINE WORKS2組のgallery整合は同タスクで成功。SDK専用CIと最終SHAのCIは未確認 |
| 文書・画面・運用・release資産 | 前版以後の利用者変更とREADME5領域は別担当が実ソースと照合。画面例は完全合成図、実端末の通知previewは未確認。CHANGELOG/export/画像のrelease生成検証・最終diffはリリース文書完成後に親が確認する（公開準備時点の工程） |

現adapters監査はjournal破損tail隔離、deltaのfsync/旧base識別、transport/workspace scope、
添付保存先、429限定再試行、不確かな送信のunknown保持、文字表示、起動/停止の例外境界を確認。
本タスクで配送workerと配備3資産も現本文を照合した。実SDK/本番通知成功とは区別する。

変更した実行・配備39ファイルの現精査対象は以下（新規全体監査を省略した一覧ではない）。

- `adapters/common/{journal,paths,registry,text,worker}.py`、`adapters/discord/{delivery,tasks}.py`、
  `adapters/lineworks/{actions,delivery}.py`、`adapters/slack/{actions,delivery,tasks}.py`。
- `mcs/core/{ledger,llm_admission,maintenance,mcs_util}.py`、`mcs/extract/drug_map.py`、
  `mcs/extract/v4/extract_llm.py`、`mcs/ingest/{health_watch,job_ops,run_check}.py`、
  `mcs/notify/{notify_cards,notify_cmds,notify_flush,notify_render,notify_transport,notify_views}.py`、
  `mcs/ops/{mcs_setup,mcs_update}.py`、`mcs/semantic/{semantic_drain,semantic_loops,semantic_v4}.py`、
  `mcs/views/{mcs_stats,mcs_view}.py`。
- `hermes_plugin/card_workers.py`、`mcs_standalone/runtime.py`、
  `deployment/{launchagents/ai.mcs.llamaserver.plist,recovery/mcs_recover.py,scripts/mcs_check.sh}`。

依存照会の正本は [GitHub Global Security Advisories API](https://docs.github.com/en/rest/security-advisories/global-advisories#list-global-security-advisories)。
親が `ecosystem=pip&affects=package@version` で現pinを照会した結果を記録する。

## 新規導入と全過去版からの更新

新規導入はhash一致の `install.sh`、`tests/meta/test_install_sh.py`、
`tests/meta/test_install_sh_standalone.py` をbaa4f6dの全量成功から再利用。
隔離HOME・stubでHermes/standalone、前提診断・導入・初期設定・起動/サービス描画・
再実行・途中失敗を確認する。brew/SDKの実導入、OS全組合せ、外部認証・実配送は未実施。
`integration/test_mcs_recovery_narrative.py` の合成取込→保存→解析→配送、部分失敗と
承認待ち復元も今回全量成功に含む。checkoutの成功を実機新規導入成功へ置き換えない。

今回、既存 [update-paths.json](../../../tests/fixtures/schema_upgrade/update-paths.json) の
targetをv1.0.15へ更新し、直近v1.0.14の永続fixture・2構成を追加した。
更新元は全15公開版v1.0.0〜14、Hermes15構成＋standalone5構成の全20経路。

| 更新元 | runtime/実入口 | 判定と証拠 |
|---|---|---|
| v1.0.0〜2 | Hermes、手動merge/reinstall | 自動更新対象外。planの拒否、合成の手動更新・migration・バックアップ/同意付き復元を成功確認 |
| v1.0.3〜9 | Hermes、対象版bootstrap | 自動対象。実plan/apply/再実行/バックアップ/migration/rollbackと再導入成功・失敗を合成で確認 |
| v1.0.10〜14 | Hermes、対象版bootstrap | 同上。現schema9のrollbackは更新後保存記録保持も検証 |
| v1.0.10〜14 | standalone、host経由 | 外部applyは `standalone_external_apply` で拒否。host前提と共通updaterの合成apply/再実行/rollbackを確認。実host協調停止は未確認 |

[origins.json](../../../tests/fixtures/schema_upgrade/origins.json) に各tagのsource/dependency SHAを保持。
v1.0.14はexact tag `3d9e324bd8fdf493213a4df506d62ccca8edf24a` の
Ledger schema用5メソッドだけをASTで取り出し、同tagの2依存SCHEMA定数と空in-memory DBで
初期化した。実DBを読まずに [shape-8.sql](../../../tests/fixtures/schema_upgrade/shape-8.sql) を作成。
既存shape-7との差分は `thread_read_marks` の新tableだけで、旧全sqlite_master定義は
空白正規化後に一致。schema9同値だけでv1.0.13 fixtureをv1.0.14の代用にはしていない。
同tableの完全合成行も原全列保持・更新/rollback検証へ追加した。

旧26経路の歴史記録は変更せず、当時runner/旧updater全実行の再現とは区別する。
schema0〜4/6の完全原DDL未確認は既存originの制約を保持。
現matrixはGit object/merge/reset、installer process、service supervision、通知をstub化し、
SQLite・backup・locks・journal・migration・復元同意/receiptは実コードを使う。
実Git更新・依存導入副作用・実host・外部サービスの全経路受入は成功扱いしない。

## 実施した検証と残る条件

- baa4f6d: `MCS_TEST_PYTHON=…/.venv/bin/python scripts/run_tests.sh -q` はexit0。
  pyprojectと二重quietで件数が表示されなかったため、件数・skip数は捏造しない。
  統合前の7685件成功記録を最終統合版の件数へ転用しない。
- 追加fixture後: 同隔離runnerで `tests/ops/test_supported_update_paths.py`
  `tests/core/test_supported_schema_upgrade.py -o addopts= -q` は **220 passed / 9.96秒**。
  全20経路と15公開版・既存pre-release schema5を追跡する。
- 親の `make check` はexit0、static gates10/10とincident mining成功。
  Slack7組・LINE WORKS2組のgallery `--check` もexit0。
- テストは一時HOME・合成DB/fixture・stubのみ。実MCS/LLM/Jev/Slack/Discord/LINE WORKS、
  原本DB・Keychain・認証情報にはアクセスせず、サービスの操作や送信をしていない。
- 最終release文書・fixture差分・README記録の整合、対象commit exact SHAの必須CI、
  固定SDK/Hermes lane、tag workflow、公開本文/asset表示の確認は親の最終工程で追記する。
  この受入票だけで公開条件完了とはしない。

## 今回の監査指摘と解消

- 長い投稿情報と長い添付名を合わせると通知文の300文字制限で拡張子が欠ける境界を合成チェックで再現。通知文の残枠で名前だけを省略し、既存の16文字以内の拡張子・投稿5項目・実際の添付名を保持するよう修正した。`713fd65`、関連152テスト成功（2.13秒）。この1ファイルの新差分はbaa4f6dの全量成功と区別し、最終CIで全量を確認する。
- 未リリースのrollback変更記録がhelper先頭の検証まで主張していたため、実装どおり明示rollbackのjournal前検証へ訂正。apply失敗時の従来復旧経路を保持。古い監査参照2件も移動先へ整合した。実行挙動の追加変更ではない。
- 原107変更記録の対応・summary/upgrade/details/refsを技術詳細とarchiveに保持し、READMEの5領域と更新条件を照合する。
- 通知比較図はv1.0.14の固定Slack通知文と現通知文をソースで照合した完全架空の説明図。1180px・390pxを目視し、文字欠けなし。固定画像commit `ad11541b5ccc87bb8fe3881b8660c00794c8a4c3` のPNGを認証済みGitHub Contents APIで取得し、ローカルSHA256と一致。repoは非公開なので匿名raw URLは404となる。Release本文には権限を持つ利用者向けの図へのリンクを置く。ブラウザーが利用不能で、GitHub Release実画面の表示は未確認。

現ソース精査の識別（SHA256、通知caption修正後）:

```text
f97b6cfd1debcc9c876021eff9b6576588f5be890c88b4f1e995ed1cab32dc04 mcs/core/ledger.py
3650135f2de3769469bc9d869ee992d7369d22405ab1ba5cf26357ab57be1edb mcs/core/llm_admission.py
1b206700d9f98b573742d1c36058fa0c3431de61750ef4fab2efa6fe75b9ccfa mcs/core/maintenance.py
42e46d4ef7f2ab952a2a723284cfe986c8c76f7677f3c43377c95d48c8730560 mcs/core/mcs_util.py
ad99d50fd9e95ea93b41b388f34804f48962c48217a124c6d8c80513455c528b mcs/extract/drug_map.py
0d028fdbe05e48997b15cc8e68ca58c0272718264d278d4f464c1617e4f13abe mcs/extract/v4/extract_llm.py
89d3771eae8d139f59e6e75959c4d786a01d3265aee1f1f92cb763a289c47015 mcs/ingest/health_watch.py
78f0320bb5459e94f0d704d335e4292f94386dff929a64f568a91689d3e59d5c mcs/ingest/job_ops.py
72f7e50d6deb54f728ea10220ac21cc5944868eeee28abfa88bbc8531dd8b285 mcs/ingest/run_check.py
2d222c9e66df78f233918a727fb4057b4721a69681b60cd4d15771b1dc30b8eb mcs/semantic/semantic_drain.py
91152253c6023b4de4c33e5e5ff47b4bbfe8aa7609c5ec511fd62c4ae46472a6 mcs/semantic/semantic_loops.py
0dfae0520bcb0a7314c84e93bd0341932de6dd61aa5a55844c7b98d0e8460278 mcs/semantic/semantic_v4.py
3d010e47b48a1f952e548479e4113d482940c9a8901837698e7878913c1bef2d mcs/views/mcs_stats.py
1170c58ca514dc857ba186afb6ef09609534fd878bbc4c0cb5d3dd8e122a5391 mcs/views/mcs_view.py
55e3326eb1fcda144cf03a0c65f4dcc2c101a9af244bc43d0ac199bb6447c020 mcs/notify/notify_cards.py
6dffc4da036c91f2fb7829a17e413e33a626d55f58ad4f266cff973da98001f5 mcs/notify/notify_cmds.py
f73dae53d3a39c0fff82d84c77a274cb484e5b56155d086aa1326ac2375ae1c6 mcs/notify/notify_flush.py
decaf81da7afa7148254804ede88e30e86b78c575604f4dbb5dd6d8d83612082 mcs/notify/notify_render.py
fc9802c6ad5a02a922a21f7281275ec2059449541187f0f73d8ac692bab2bcad mcs/notify/notify_transport.py
73228eef96faac831064d81dcb2a477a349abb57cac72094b2647f7a473eed16 mcs/notify/notify_views.py
741ab4ea3f8197eeb608517dcc6a3c4e73d0b0b9f8993399fc539d4c1e126943 mcs/ops/mcs_setup.py
a61fa6fe381f1267cf7536bd1a27f613d382f11e58c46810bdf66fd6f85a5e8e mcs/ops/mcs_update.py
```

リリース文書・新規導入・合成取込の追加検証結果は公開前の既存runnerで確認し、最終SHAのCIはタグ前に確認する。公開後の最終SHA・CI run・tag/Release本文一致の実行記録は、公開したReleaseと同タスクの完了報告を参照する。

公開準備の最終ローカル結果: releaseノート/READMEのschema・生成・全参照リンク、static gates10/10・incident mining、両gallery確認が成功。既存隔離runnerのrelease・新規導入（Hermes/standalone）・合成復旧narrativeは105 passed / 26 subtests passed（132.09秒）。変更記録107件をarchiveへ移し、CHANGELOG本文は107見出しと107個別更新条件を保持。実装・SDKの最終全量は公開候補commitのCIで別途確認する。

## 最終CIで見つかった検証資産の修正

候補 `9c8bca967905e7932082e709ccd052c252c0ffa9` のCIは、hygiene・incident-gates・readme-sync・standalone SDK（25 passed）が成功し、Hermesと全量は次の2件で失敗した。タグ・Releaseはまだ作成していない段階の記録。

- Discordの取消競合テストは50回のevent-loop yieldだけでpublish開始を証明していなかった。合成20msのflag-read遅延で同じ失敗を再現し、publishへ入ったEventを待つ同期へ修正。取消・二重確定・一意コマンド・confirmation消費の期待値を保持。確定関連テストと独立10回の隔離反復が成功。製品コードの変更はない。
- Hermes Slack実SDKの添付fixtureがworker保存先の外にあったため、正しい保存先制限でupload前に拒否された。worker root内のattachmentsへ完全合成ファイルを配置し、upload3段階・bytes・thread・retry・journalの期待値は保持。保存先制限の関連3テスト成功。ローカルに対象SDKが揃わず実SDK再実行は未実施のため、修正後exact SHAの固定CIで確認する。

同候補の[CIログ](https://github.com/yusuketakuma/hermes-mcs/actions/runs/37438142045)は失敗記録として保持し、最終成功へ読み替えない。
