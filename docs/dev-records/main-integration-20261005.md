# mainへの全ブランチ統合 — 2026-10-05

## 範囲と結果

ユーザーの明示指示により、ローカル全枝とfetch済みorigin全枝をmainへ統合した。開始時mainはfb099ee、実装候補はd42320e、ノート候補はd08aa91。push・公開・配備・常駐再起動・実データへの接続は行っていない。未追跡の8とrecoverは保持した。

## 統合方法

- release/1.0.13はfast-forward、release/1.0.13-notesは8833d49でmerge。
- fix/thread-stamp-ux-20261003は595b66dで機能を統合し、現行のscope・shadow公開条件・緊急度・DB安全修正を保持。
- 既存導入済みの残る7枝は541a271で現行コードを維持して祖先履歴を統合。スタンプ表示・actor取得・本人応答は実コード/テスト、docs枝は同等patch、semantic rejectionは317cedfと回帰/同一変更記録で確認。
- 公開済み旧versionのCHANGELOGと既存archiveは保持。1.0.13候補は全変更記録を隔離一時treeに集めrelease_notes.py buildで再生成し、結果をmainへ反映。重複したroot記録は同一archiveを保持して整理した。

## 調査と修正

| 観点 | 確認・対応 |
|---|---|
| 配送 | Slack更新429/その他拒否から新規投稿へfallbackしない。旧まとめ投稿の改ページによる重複と、一部未配信を全件配信済み扱いして結合する問題を回帰で修正。 |
| 表示 | 1投稿1本文、見出し→要約→スタンプ→押下者→本文、絵文字集計、長本文の分割を統合。旧表示helper入口も保持。合成Slack画像02を更新し目視・hash検証。 |
| 取得 | 30日活動thread全投稿のtiered再取得8件、7日活動threadのactor4件を統合。未来日時と削除済み投稿が活動期間を延長しないよう修正。予算・deadline・通信安全・既定offを維持。 |
| 永続化 | actor氏名/所属/取消状態の加法migration。旧snapshotの列欠損時も読取り互換を維持。無期限なのは最新観測状態であり、全操作イベント履歴ではない。 |
| 診断 | 切替前transportの保留は記録を保持しlive障害から分離。自動mergeで消えたsqlite3 importを復元し、backup DB例外のNameErrorを回帰で防止。 |
| 共通契約 | latest signalのproject境界・取消後再監視・shadowとcaptureの分離・未取得と0件の区別・人承認とreceiptを維持。 |
| SDK | pinned Hermes/standalone SDKの隔離統合試験を再実行。実機SDK接続・稼働反映とは別。 |
| 文書 | ROADMAP D1〜D3、stamps、INSTALLATION、READMEと画像例を現行の取得範囲・氏名保存・保持契約へ整合。 |

## 制約

旧legacy本文のterminal not_sent/unknownは記録を保持し、配信済みと成功扱いしない。既配信部品や未知送信を自動再送しない現行receipt契約を維持する。全履歴の復元や実配送の完全性を合成検証だけでは保証しない。
実APIの未読保持・権限/ページング、鍵/NAS/実復元訓練、人手評価とG6、相手側C1受入などはdocs/development/plans/RELEASE_1.0.13.md §6の外部条件を引き続き満たす必要がある。

## 枝別確認

| ref | 確認head | 状態 |
|---|---|---|
| `codex/release-1.0.11-20261003` | `9ea015d` | 統合済み |
| `docs/readme-slim-and-tidy` | `1d46439` | 統合済み |
| `feature/card-buttons` | `239b22c` | 統合済み |
| `feature/card-extras` | `5854b28` | 統合済み |
| `feature/card-fixes` | `46d1eae` | 統合済み |
| `feature/card-refactor` | `b91df23` | 統合済み |
| `feature/digest-names` | `2f6c7b2` | 統合済み |
| `feature/slack-compact-actions` | `9564925` | 統合済み |
| `feature/stamps-1.0.11` | `e3c5eac` | 統合済み |
| `feature/standalone-mode` | `d20efb4` | 統合済み |
| `fix/attachment-no-reupload` | `4dbf8e3` | 統合済み |
| `fix/collection-health-and-disk` | `1ef6eed` | 統合済み |
| `fix/extract-lane-slot-and-waste` | `4bbc318` | 統合済み |
| `fix/intent-route-mismatch-hold` | `5803f7c` | 統合済み |
| `fix/llama-cache-ram-off` | `9b13199` | 統合済み |
| `fix/plugin-freshness-docs` | `597014a` | 統合済み |
| `fix/review-1.0.11-20261003` | `b5b2958` | 統合済み |
| `fix/script-drift-and-drain-observability` | `3b86d60` | 統合済み |
| `fix/signal-render-gate-and-registry-lock` | `0fa8e28` | 統合済み |
| `fix/thread-stamp-ux-20261003` | `431c39e` | 統合済み |
| `fix/wait-parts-before-update` | `2a6adc3` | 統合済み |
| `installer/base` | `9c2213b` | 統合済み |
| `installer/docs` | `5bb0836` | 統合済み |
| `installer/docs-base` | `c21cf9c` | 統合済み |
| `installer/install-sh` | `ae53bf0` | 統合済み |
| `installer/integrate` | `7aa70d1` | 統合済み |
| `installer/mcs-setup` | `da3d6f3` | 統合済み |
| `integrate/countermeasures-20260929` | `ad828ee` | 統合済み |
| `integrate/refactor-post105` | `88d76d1` | 統合済み |
| `integrate/release-1.0.11-20261003` | `f6976e6` | 統合済み |
| `integrate/standalone-and-local-20261002` | `53fd99f` | 統合済み |
| `main` | `541a271` | 統合済み |
| `merge/readme-slim` | `e76037a` | 統合済み |
| `refactor/delivery-post105` | `4380c66` | 統合済み |
| `refactor/extract-views-post105` | `33f19f9` | 統合済み |
| `refactor/ingest-core-post105` | `6aabc6f` | 統合済み |
| `refactor/layout-post105` | `0a1bc97` | 統合済み |
| `refactor/notify-post105` | `47a1f80` | 統合済み |
| `refactor/ops-post105` | `71aa7b1` | 統合済み |
| `refactor/semantic-post105` | `95590cc` | 統合済み |
| `release/1.0.13` | `d42320e` | 統合済み |
| `release/1.0.13-notes` | `d08aa91` | 統合済み |
| `stamps-display-1.0.11` | `0bd2273` | 統合済み |
| `wip/roadmap20-ultracode` | `be56814` | 統合済み |
| `worktree-agent-a363a0cd79c87b953` | `9ea015d` | 統合済み |
| `worktree-agent-a3b001820d775e51a` | `379331d` | 統合済み |
| `worktree-agent-a624f551231c52d84` | `e76037a` | 統合済み |
| `worktree-agent-a6b8abb592fa9537a` | `e76037a` | 統合済み |
| `worktree-agent-a917ba1072aa97e5c` | `b98301c` | 統合済み |
| `worktree-agent-aa50620390c1b7478` | `1f7a403` | 統合済み |
| `worktree-agent-aa55b9af21782ea21` | `e76037a` | 統合済み |
| `worktree-agent-ad589729a976bc50e` | `b98301c` | 統合済み |
| `worktree-agent-adda85f994a54d0f6` | `b98301c` | 統合済み |
| `worktree-agent-af14089c42f774b34` | `e76037a` | 統合済み |
| `worktree-wf_8e813aaf-587-11` | `9b13199` | 統合済み |
| `worktree-wf_8e813aaf-587-15` | `9b13199` | 統合済み |
| `worktree-wf_8e813aaf-587-17` | `9b13199` | 統合済み |
| `worktree-wf_8e813aaf-587-19` | `9b13199` | 統合済み |
| `worktree-wf_8e813aaf-587-20` | `9b13199` | 統合済み |
| `worktree-wf_8e813aaf-587-21` | `9b13199` | 統合済み |
| `worktree-wf_8e813aaf-587-22` | `9b13199` | 統合済み |
| `worktree-wf_c7eb6808-99d-1` | `b2ab605` | 統合済み |
| `worktree-wf_c7eb6808-99d-2` | `b2ab605` | 統合済み |
| `worktree-wf_c7eb6808-99d-3` | `b2ab605` | 統合済み |
| `worktree-wf_c7eb6808-99d-4` | `b2ab605` | 統合済み |
| `worktree-wf_c7eb6808-99d-9` | `b2ab605` | 統合済み |
| `origin/docs/japanese-release-notes-20261001` | `253512b` | 統合済み |
| `origin/docs/readme-release-refresh-20261001` | `d9afc6f` | 統合済み |
| `origin/docs/roadmap-feature-candidates-v2` | `eb21ace` | 統合済み |
| `origin/docs/slack-readme-gallery-20261001` | `f2e9a16` | 統合済み |
| `origin/feature/stamps-1.0.11` | `e3c5eac` | 統合済み |
| `origin/fix/ci-fixture-contracts` | `017be83` | 統合済み |
| `origin/fix/exact-drainer-detection` | `d3a2a69` | 統合済み |
| `origin/fix/semantic-format-rejection-state` | `dc8329b` | 統合済み |
| `origin/fix/semantic-long-format-budget` | `b1b25a1` | 統合済み |
| `origin/main` | `fb099ee` | 統合済み |
| `origin/release/1.0.13` | `7074a85` | 統合済み |
| `origin/release/1.0.13-notes` | `7dfcd66` | 統合済み |

## 最終の負荷確認と契約回帰

押下者selectorの相関MAXが投稿ごとに患者全体を検索することを合成計測で確認。
1患者・5投稿/thread・全capture反応・limit4・3回中央値で、1,000/5,000/10,000投稿は
0.125/3.65/14.25秒。加法thread/time索引後は0.0044/0.021/0.041秒で対象4件は同一。
同じ環境での合成query測定であり、実機性能ではない。起動時の加法index作成だけを追加し、
取得対象・上限・予算・保存データ・schema9を維持した。既存DB再openと実query planを回帰で確認。

最初の全suiteは6774 passed、5 failed、6 skipped、26 subtests passed（672.33秒）。
5件は旧actor返却キー（氏名/所属追加）4件と旧signal監視集合1件の契約期待値で、
意図した機能変更に合わせて更新した。写真/連絡先非取得、部分walk失敗、snapshot timestamp、
latest signal・project境界・未来/削除/archive除外は明示検証を維持する。
更新後の全suite結果は以下の最終検証欄へ記録する。

## 作業ツリーの既存変更

別worktreeも読取り確認した。roadmap20-ultracodeのAGENTS.md OpenWiki blockはmainと完全一致。
旧codex/release-1.0.11-20261003のtracked未コミット34件も、メタ情報取得・本人ID・返信判定・
カード表示・Python検出・設定・回帰・文書・画像を全件照合し、mainに導入済みまたは後続実装で更新済み。
未導入の必要機能/修正は検出しなかった。これら他者の変更と未追跡ファイルは編集・stage・commitしない。

## 最終検証

最終実行コードはe7a8428。全suite再実行は **6780 passed、6 skipped、26 subtests passed、635.61秒**。
失敗した旧契約5件も全件成功した。最終core/ingestは1520 passed、取得契約は61 passed。
SDK専用隔離試験はHermes 16 passed、standalone 25 passed（各skipなし）。
任意SDK環境の全suite内skipを実機検証成功とは扱わない。

ruff 0.16.10のCI全対象、生成README/開発文書、release_notes、readme_release、
安全ゲート10/10、incident coverage、Slack画像7組の整合検証は成功した。
公開済み1.0.12以前のCHANGELOGと既存archiveに改変がないことも確認。
ローカル66枝とorigin12枝は全てmainの祖先となり、未統合headは0。
この追記は検証結果の文書化だけで、実行コード・テスト・設定を変更しない。
