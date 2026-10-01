# LINE WORKS追加の集中影響レビュー（2026-10-01）

LINE WORKSの独立実装とアダプター移動が既存機能へ及ぼす影響を追加レビューした。
前回の9項目の修正を重複監査せず、共有状態・既存transport・設定・更新/復旧・導入・
収集/解析caller・CIを重点に確認した。実害を再現した3件と待機例外の互換を修正した。
Hermes Agent本体・実DB・実サービス・実配送・配備・pushは変更していない。

## 確認範囲と判定

| 領域 | 確認内容 | 判定 |
|---|---|---|
| nativeアダプター | 移動前9ファイルとcanonical9ファイル、旧入口、factory、SDK遅延import、ContextVar・worker状態 | 新旧module identity分離を修正 |
| 共通配送 | Registry/envelopes/spec/worker、v1 Discord・v2 Slack・v3 LINEのgrant/receipt、full本文・添付・thread | 追加の具体的回帰なし |
| DB・切替・復旧 | schema、旧inflight移行、scope/epoch切替、factual receipt、restore、unknown hold、operator resolve、GC/recover | DBschema変更なし、合成回帰成功 |
| text outbox | Hermes argv/MEDIA、LINE JSON/pin、notify_system_target、off切替、独立CLI | LINEが対話activeを必須としてtextまで拒否する問題を修正 |
| setup/update/recovery | target別診断、plugin_changedの保存とpostmerge/rollback/recover伝搬、marker、writer/cron/LaunchAgent所有 | 追加回帰なし。既存所有集合を維持 |
| 収集・抽出・意味解析・view | run_checkのflush入口、ledgerの配送schema/添付followup、semantic_send_gate/lifecycleとfingerprint、viewのreceipt scope | 接続先ごとの分岐はnotify側。収集/解析caller・権限・意味解析gateの変更なし |
| 導入・CLI | source/runtime分離、Path A/Bの配送説明、offの意味、待機例外 | 文書整合とasyncio例外互換を修正 |
| CI・release | lint/stdlib/plugin sandbox、SDK、変更記録判定 | 移動先単独のruntime変更が記録必須checkを回避する問題を修正 |

3担当がnative互換・共通transport・運用を読取りレビューし、親が横断callerを確認した。
レビュー後に担当ファイルを限定して実装・合成回帰を追加した。
新しいSlack/Discord BotやREST認証、第三者ライブラリ、独立writerは追加していない。

## 確定した影響と修正

1. **新旧importで再送抑止・再接続の状態が分離**。
   `__path__`転送は同一ファイルを別module名でロードした。混在importではDiscordの
   single-post guardが別ContextVarを見て、合成500/connection reset後の2 POSTを許した。
   Slackの`_LIVE`も分離し、worker引継ぎが外れた。
   互換入口をcanonicalの同一moduleへaliasし、両import順・別host namespace・SDK不在で
   同一性を検証。Discord混在時は1 POSTで抑止し、Slackは旧workerを引き継ぐ。
2. **offや他のactive接続でLINE textを拒否**。
   coreはoffをtext通知継続として扱い、別transportのsystem targetを選択できるが、
   LINE設定readerが対話activeも必須としていた。text send/checkだけ接続検証を分離した。
   実際のnotify設定とLINEの必須scopeを両方検証し、宛先完全一致を維持する。
   run/service/init/status・Callback・人承認はactive LINE必須のまま。
3. **adapterだけのruntime変更が変更記録gateから漏れる**。
   `release_notes.py require_fragment()`へ`adapters/`・`lineworks_adapter/`を追加した。
   各接続のruntime変更は記録なしで拒否し、有効な記録がある場合とREADME-onlyは通る。
4. **Python 3.10の待機例外別名**。
   `asyncio.wait_for()`のタイムアウトを`asyncio.TimeoutError`で処理する。
   builtinと別の例外型を合成して通常のpoll待機でdaemonが終了しないことを確認した。
   CIの例外型許可もLINEの`__main__.py`だけに限定し、native及び他のLINEファイルの
   制限を維持した。Python 3.10本体を実行したという意味ではない。

LINEのAPI・認証・署名・固定URL・許可ユーザー・人承認条件は変更していない。
[公式Callback仕様](https://developers.worksmobile.com/jp/docs/bot-callback)を再照合し、
module cache/aliasの意味は[Python公式import仕様](https://docs.python.org/3.13/reference/import.html)と
実際のimport同一性を根拠にした。Path Bの既存収集・定期実行機構も変更していない。

## 探索と制約

安全確認したPython/shellソース117ファイル（2.4 MiB）の一時コピーへjgを実行した。
対象はcore/ingest/extract/semantic/views/notify/ops、hermes_plugin、3接続adapter、
CLI、scripts、deployment。data・秘密ファイル・実情報・mcs-exports・SDK依存は含めない。
リクエスト上限5で**discovery incomplete、exit 2**となり、factory・native cards/actions等の
候補だけを得た。全領域の網羅確認として扱わず、rg・直接読取り・3担当レビューと
既存回帰でcallerを補完した。実サービス/実テナント・Linuxネイティブsystemdは未検証。

## 検証

- native新旧入口・SDK不在・再送抑止・Slack回帰: **94 passed**。
- 共通notify/contractsの5ファイル: **195件成功・exit 0**（quiet出力の成功記号を集計）。
- 変更前の運用/導入/update/recovery/layout: **444件成功・exit 0**（同集計）。
- LINE text/off/native-active scope・診断・既存runtime: **84 passed**。
- 待機例外・release記録gate: **13 passed、13 subtests passed**。
- 全体回帰: **3491 passed、1 skipped、23 subtests passed**（236.53秒、exit 0）。
  既定環境でSDK不在のDiscord実SDKテストはskipし、以下の固定SDK環境で別途成功。
- 固定Hermes `fd50a275e2616118c48fe07e7e1c878782b15ccd`のSDK連携:
  **4ファイル・14 passed、0 failed**（1.6秒、exit 0）。
  Python 3.11.15、discord.py 2.7.1、slack-sdk 3.44.1、slack-bolt 1.30.0。
  一時Hermesソースと認証・socket隔離を使用し、本体・実サービスは変更していない。
- 全体終了後のCI例外型許可・native拒否・新旧module状態・LINE待機回帰:
  **41 passed**（0.63秒、exit 0）。追加3ケースを含む対象回帰でCIの最終変更を確認。
- `make lint`、`ci/gates.py` **8/8**、`ci/mine_gates.py --check`、
  `update_readme.py --check`、`readme_release.py --check`、`release_notes.py check`、
  `generate_slack_gallery.py --check`、`git diff --check`は成功。

既存差分128パスの開始hashは `/tmp/mcs-lineworks-impact-start-20261001.json`。
開始時差分のうち変更した12パスと新規差分4パスは今回の限定修正・回帰・文書・記録のみ。
残りの開始時差分116パスはhash一致、既存ファイルの消失はない。
一時ログは `/tmp/mcs-lineworks-impact-{core,operations,off,runtime-release,final-target,sdk,lint,full,jg}-20261001.log`。
恒久的な主要結果は本書に保持する。実接続・配送の成功やバグゼロの保証は主張しない。
