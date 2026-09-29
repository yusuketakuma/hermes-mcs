# Changelog

## [1.0.6] — 2026-09-29

v1.0.5 の独立レビュー指摘への修正（3 wave）と、その後の全体
リファクタリング・未解決事項の解消。未確認フラグの fail-closed
統一、Discord/Slack の確定・取消規則の一本化、配送 journal の
差分読み、そして更新・復旧経路（mcs_update / 独立 watchdog）が
launchctl・git・pgrep の異常や restore 同意待ちの最中でも
drainer を止めたまま放置せず、同意を無効化しないよう強化した
（v1.0.5 から 109 コミット・199 ファイル）。`hermes_plugin/` と
`deployment/recovery/` に変更があるため、gateway 再起動と
watchdog の再配置が必要。

### 動作が変わるもの

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

### 修正した問題

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

### 内部構造・開発者向け

- **重複実装の共通化** — fact binding・projection 現行判定・
  object meta ガード・scope lock 待ち・restore marker / dir fsync・
  relogin・publish・criteria hash などを 1 か所に集約（挙動不変）
- **テスト共通 helper（testkit）** — semantic・views・notify・
  plugin・ops の testkit を新設し、テストモジュール間の import を
  0 に。`test_extract_llm_v2.py` を `test_extract_llm.py` に改名
- **docs/ROADMAP.md** — 機能候補 v2（PR #1）

### アップグレード時の注意

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

## [1.0.5] — 2026-09-28

抽出パイプラインの精度改善（本文中の裏付けを必須化したイベント
抽出、不足抽出への1回限りの修復、ルール検出のフィールド単位
統合）と、`mcs/extract/` の推論エンジン世代別フォルダ化
（v1〜v4）。運用面では `check` が plist の存在だけでなく
launchd の実稼働とキュー健全性を診断し、通知カードは抽出
artifact の後着を検知して自動で再描画する（v1.0.4 から
12 コミット・39 ファイル）。`hermes_plugin/` に変更はないため
gateway 再起動は不要。drainer のパス更新のため `mcs_setup.py
services` の reconcile が必要。

### 動作が変わるもの

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

### 修正した問題

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

### 新しい運用機能

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

### 内部構造・開発者向け

- **README のモック刷新** — Discord カード・Discord カード+
  コンパニオンスレッド・Slack カード・Slack スレッドパネルの
  4種に番号凡例・送信者行・構造化フィールド・操作ボタン・
  配送状態・確認後状態を記した完全合成の mock を冒頭に配置

### アップグレード時の注意

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

## [1.0.4] — 2026-09-28

追跡ファイル284件の全リポジトリレビュー（dev-records
`refactor-20260927`）にもとづく安定性・正確性の改善、部分導入済み
環境への柔軟なインストール、Slack の設定ウィザード対応、ローカル
LLM エンドポイントの設定可能化。通知・収集の既存設定はそのまま
使える。`hermes_plugin/` の変更を反映するには `hermes gateway
restart` が必要（`check` が起動中 gateway より plugin が新しい
場合に警告するようになった）。

### 動作が変わるもの

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

### 修正した問題

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

### 新しい運用機能

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

### 内部構造・開発者向け

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

### アップグレード時の注意

- **`hermes_plugin/` を更新したら `hermes gateway restart` が必要**
  — 再起動なしでは新形式 spec を旧 worker が処理し、カードのみ
  届き本文・添付が欠落する。`check` がこの状態を警告する
- **`mcs_setup.py services` を再実行すると cron/launchd を
  reconcile する** — `mcs_health.sh` 等の差分があれば追加・更新
  される（削除は stale 判定のみ）
- **既存の token・allowlist・scope 設定は保持される** — init の
  再実行・部分導入フラグともに既設定値を上書きしない

## [1.0.3] — 2026-09-26

Discord 通知を「カード＋コンパニオンスレッド」の対話型モデルへ刷新
（本文・添付は専用スレッドへ耐久化配送）、semantic v4 自己修復
パイプラインと canonical 読取経路の追加、retired エンドポイント
からの移行、モジュール第一層分割など基盤の大規模な更新。通知・
収集の既存設定はそのまま使えるが、`hermes_plugin/` の変更を反映
するには Hermes gateway の再起動が必要（後述）。

### 動作が変わるもの

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

### 修正した問題（通知・配送）

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

### 新しい運用機能

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

### 内部構造・開発者向け

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

### 注意

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

## [1.0.2] — 2026-09-23

全体レビュー（46ファイル・実測）に基づく安全性・収集完全性・抽出
正確性の大規模修復と、canonical semantic-facts v2・通知経路の
一本化・抽出並列化・運用導線の追加。既存の運用操作・設定はそのまま
使える。通知の宛先設定だけ `discord_channel_id` から `notify_target`
へ移行している（済みの環境は変更不要）。

### 動作が変わるもの

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

### 修正した問題（安全性・収集完全性）

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

### 修正した問題（抽出・意味処理の正確性）

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

### 新しい運用機能

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

### 内部構造・開発者向け

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

### Docs

- README を非エンジニア向けに全面改訂 — 平易な導入部・
  「MCS データで何が追えるのか」「蓄積・集計できるデータの全体像」
  「まだ取れないもの」の明示、図を SVG 化、用語を「チャットルーム」
  に統一、QC 対象範囲（直近60日）を明記
- `docs/semantic-facts-v2-rollout.md`・監査記録（AUDIT-J03・
  review-20260923）・launchagents の手順書を追加・更新

## [1.0.1] — 2026-09-22

構造化抽出（extract_llm）の精度向上と、統計の回帰確認ワークフロー。
既存の運用操作・設定はそのまま使え、移行作業は不要。

### 抽出結果が変わるもの

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

### 動作が変わるもの

- **ローカルLLMサーバの能力に応じて出力方式を自動選択** — JSON
  スキーマ強制に対応したサーバならそれを使い、非対応なら従来方式へ
  自動で降格・復帰する。設定変更は不要。
- **途中で失敗した抽出を成功扱いしない** — 長文の分割処理の一部が
  失敗した場合、そのメッセージ全体を失敗として再試行する
  （以前は中途半端な結果が「完了」として残り得た）。
- **v1 の旧抽出は v2 が成功した時点で自動的に置き換わる** —
  手動の移行操作は不要。抽出失敗の履歴は残る。

### 新しい運用機能

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

### Docs

- README に「MCS データで何が追えるのか」節を追加、図を mermaid
  から SVG に差し替え
- AGENTS.md を最小限の指示に整理

## [1.0.0] — 2026-09-21

`mcs-adapter` から `hermes-mcs` として独立リポジトリ化し、内部構造を
シンプル化した最初のリリース。

### Layout
- `adapter/` → `mcs/`（実行モジュール37本）、テスト49本を `tests/` へ分離
- `hermes_plugin/`（/mcs Discord コマンド）、`integration/`（hermes E2E）を維持
- `docs/dev-records/` に開発記録を集約、`deployment/launchagents/` に
  launchd plist テンプレート3種を追加

### Features (既存機能の集約)
- 15分間隔の MCS 未読収集 + Discord 通知（構造化→原文の2段投稿）
- 全履歴アーカイブ（ページカーソルで中断再開）
- FTS5 全文検索 + 日本語空白無視の部分一致、患者タイムライン
- 構造化抽出: ルール `extract_v1` + ローカルLLM `extract_llm`(Qwen3.5-9B)
- 患者ロールアップ、読み取り専用統計、レビュー候補シグナル6種
- 人承認の依頼管理（`--confirm-human`+`reason`+receipt）
- 人による却下（`signal_dismiss`）と承認済み閾値ポリシー（`signal_policy`）
- 通知クールダウン（既定7日）、deadline 部分実行の安全側 resolve 抑制

### Infrastructure
- `mcs/mcs_setup.py` — init（Keychain/config/.env プロビジョニング）+ check
- `scripts/update_readme.py` — README モジュール表の自動生成
- GitHub Actions: lint-test / readme-sync / hygiene
- `pyproject.toml` で ruff・pytest 設定を一元化、`Makefile` 追加
- `LICENSE`（proprietary）・`SECURITY.md`・`AGENTS.md` 新設
