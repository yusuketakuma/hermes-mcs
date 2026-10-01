# スタンドアローンモードの計画・検証記録

## 目的と互換性

Hermes Agent をインストールしない環境で、収集・保存・検索・抽出・通知・
カード操作・人承認・`/mcs`・定期実行・更新・復旧を現在と同じ契約で動かす。
`runtime_mode` が未指定または `hermes` の既存環境は従来の経路を一切変えない。
上流 Hermes Agent のコードは変更しない。

開始点は `c5a7716`。作業は `feature/standalone-mode` の別 worktree で行い、
稼働中の `~/.mcs` には適用しない（merge = 配備）。

## Hermes 依存の棚卸し

| 依存 | Hermes モード | スタンドアローン |
|---|---|---|
| テキスト通知 | `notify_flush` → `hermes send` | `python -m mcs_standalone send`（LINE WORKS と同じ封印 JSON・添付 pin） |
| カード配送・操作 | gateway が plugin の `Supervisor` に SDK client を渡す | `mcs_standalone run` が SDK client を作り同じ `Supervisor` を起動 |
| `/mcs` (Discord) | Hermes の native command → `hermes_plugin` handler | 同じ handler を discord.py app command として登録（単一 upsert） |
| 定期ジョブ6件 | `hermes cron`（`--deliver local`） | launchd カレンダー agent `ai.mcs.cron.*`（同じ wrapper・同じ時刻） |
| drainer・watcher | launchd（Hermes venv の python） | 同じ launchd agent、python のみ `~/.mcs/venv` |
| 更新後の再起動 | `kickstart -k ai.hermes.gateway` | `kickstart -k ai.mcs.standalone` |
| 復旧 watchdog | Hermes python・`hermes send`・cron list | 独立 venv・独立 send・cron は agent 管理なので対象外 |
| 導入 | Hermes clone/venv/plugin link | `install.sh --mode standalone`: 独立 venv と固定版 SDK のみ |

## 設計判断

- 常駐プロセスは Discord/Slack 接続（`ai.mcs.standalone`）だけにする。定期ジョブは
  launchd に任せる。launchd は同一ジョブを重複起動しないため hermes cron と同じ
  非重複性を持ち、更新処理の quiesce・drainer 再起動・agent 所有権管理は既存経路を
  そのまま使える。更新処理は常駐プロセスの子ではないため自己 kill の問題も無い。
  （Codex 初稿の「単一ホストが全ジョブを所有し、更新 marker と再 exec で協調」案は
  更新・復旧に独自の協調プロトコルが必要になるため採らない。）
- 対象 OS は既存と同じ macOS(launchd)。
- 設定は `~/.mcs/config.json` の `notify.<transport>` に許可ユーザー・プロジェクト等を
  置く（LINE WORKS と同じ形）。認証値は `~/.mcs/.env` の `DISCORD_BOT_TOKEN` /
  `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` だけを読み、`~/.hermes/.env` へは fallback しない。
- SDK は `deployment/requirements-standalone.txt` の固定版を独立 venv にだけ入れる。
  コア（`mcs/`）は標準ライブラリのみを維持し、SDK import は `adapters/*/standalone.py`
  の関数内に限定する。
- 二重消費の防止: 配送は既存 registry の scope lock が1 worker に限定する。加えて
  `flags/notify.json` に `runtime_mode` を出し、Hermes plugin の card worker は
  standalone 選択時に起動しない。
- 送信結果の契約: exit 0=配送済み、2=送信前の設定/入力エラー（outbox_hold）、
  その他=成否不明（自動再送しない）。`hermes send` と同じ扱い。
- Hermes の AI チャット機能（エージェントとの会話）は MCS の機能ではないため対象外。
  Discord のメッセージ本文による `/mcs` 入力も対象外（native slash command で同等操作可、
  特権 intent を要求しないため）。

## 受入条件

| 条件 | 検証 |
|---|---|
| Hermes 未導入で全機能 | 合成 fixture: 送信 CLI、connector の Supervisor 起動・停止、services の agent 生成、update/recover の分岐 |
| Hermes モード不変 | 既存テスト全件、mode 未指定時の argv・services・update が従来と一致 |
| Discord/Slack 同等性 | 実 SDK（固定版）をオフラインで使い、送信・添付・`/mcs` 登録 payload・本人/scope 照合を stub 通信で検証 |
| 安全ゲート | 送信先は設定チャンネルのみ、添付は pin 照合、proxy 不使用、秘密値をログ/argv に出さない、不明時は再送しない |
| 品質 | ruff、README 生成 check、gates、changes 記録 |

実 Discord/Slack/MCS/LLM/Keychain・稼働サービスには接続しない。実機での
接続確認は未実施として報告する。

## 段階の記録

- 計画: 本文書。
- 計画レビュー（独立レビュー1回、実ソース照合）: 次を計画に追加した。
  - モード切替時の重複実行防止: standalone の `services` は所有済み Hermes cron を削除し、
    hermes の `services` は `ai.mcs.cron.*` と `ai.mcs.standalone` を退役させる。
    所有権判定（`mcs_setup._sync_agents`・`mcs_update._reconcile_membership`）の接頭辞を拡張。
  - `runtime_mode` を `CONFIG_RULES` に追加（未登録だと precheck が設定を拒否）。
  - `~/.hermes` 固定値（scripts・python・logs・gateway label）を update / recover /
    llama 再起動スクリプトでもモード別に解決する。
  - 復旧通知・gateway 再起動は `mcs_recover.py` にも同じ分岐を入れる。
  - 送信 CLI が Hermes の暗黙機能（メンション無効、1回だけの POST、添付上限）を担う。
  - scope key は `notify.<transport>.profile` 等から作られ、両モードで同値になる
    （card spec と同じ値を使うため journal の継続性も保たれる）。主防御は
    `flags/notify.json` の `runtime_mode` と既存の scope lock。
  - launchd の calendar は `*/n` を持たないため cron 式を dict 列へ展開する。
- 実装: `mcs_standalone/`（run・send・check、Discord/Slack runtime、Host）、
  `mcs/core/mcs_runtime.py`、`notify_flush` の送信経路と終了コード 75、
  `mcs_setup`（設定検証・ウィザード・`services` の calendar agent／接続 agent／
  Hermes cron 退役・check・doctor）、`mcs_update`・`mcs_recover` の分岐、
  `install.sh --mode standalone`（Codex 初稿を取り込み、置き場所を修正）、
  Hermes plugin の stand-down、文書・CI job `standalone-sdk`。
  Codex 初稿の「単一ホストが全ジョブを所有」部分（status/restart request・
  更新協調）は採用せず、launchd に任せた。
- テスト: 合成 fixture の単体テスト（setup・notify・lifecycle・runtime）と、
  固定版 SDK を使うオフライン統合テスト（HTTP セッションだけを偽装）。
  統合テストで Bolt の引数誤りと Supervisor 前提ディレクトリの扱いを検出し修正した。
- レビュー（独立レビュー1回）: 10件を確認し、次を修正した。Discord の添付容量超過（413）を
  終了コード2にして本文だけ再送、standalone 中は Hermes plugin が `/mcs` も登録しない、
  `services` が flags を再公開、修復案内に `--mode standalone`、standalone では
  `requirements-standalone.txt` の変更も自動更新の対象外、送信 CLI を独立 venv の
  Python で起動し SDK 欠如は再試行扱い、未対応の送信先を設定検証で拒否、接続の異常終了を
  非0で終了して launchd（`SuccessfulExit=false`）に再起動させる、Hermes cron の退役は
  launchd の定期ジョブ登録が成功してから行う。
  未対応: 同じ Bot を Hermes gateway と同時接続した場合の振り分け（運用で gateway 側の
  接続を外す手順を文書化）。
- コード整理: 接続 `run` の引数を両 transport で統一、戻り値注釈を修正、
  `release_notes.py` の変更記録必須範囲に `mcs_standalone/` を追加。
- フォルダ・リポジトリ整理: 新規ディレクトリを AGENTS.md・lint 範囲・CI・README・導入ガイド・
  launchd README に反映。テスト実行で生じた `__DATA__/`・`.venv/`・`uv.lock` を削除。
  `openwiki/` は自動生成のため手編集していない。
- 未検証: 実 Discord/Slack・実 MCS・実機 launchd への接続（オフライン検証のみ）。
  CI の pinned Hermes での `hermes-integration` は未実行（ローカル Hermes では14件成功）。
- 追加要求（2026-10-02）: インストーラーで方式を選べるようにした（`--mode`、既存
  `config.json` の継承、初回の対話プロンプト。非対話は従来どおり hermes）。
  その他の検討として、新ログのローテーション、Hermes gateway 併存時の check 警告、
  `~/.hermes/.env` のトークンを確認付きで引き継ぐ init を追加した。
- 2回目の独立レビュー: 9件中8件を修正。calendar agent に `AbandonProcessGroup`
  （収集で起動する Chrome を tick 終了時に落とさない）、hermes へ戻すときの gateway
  再起動と Hermes CLI 不在時の `ai.mcs.cron.*` 保持、所有 Hermes cron 記録の保持、
  モデルの再ダウンロード回避、更新処理が自分の job を reload して自滅しない
  （`XPC_SERVICE_NAME`）、復旧通知を独立送信で system target へ、モード不一致時の
  接続プロセスの正常終了。llama ログの移動は変更記録に明記済み。
  Socket Mode の再接続失敗の監視は Hermes と同じく SDK の自動再接続に任せた。
- 網羅監査（ultracode、2026-10-02）: 7観点の並列検出と指摘ごとの反証検証
  （37エージェント）で30件中26件が確定（重複除き約22件）。主な修正:
  Hermes が plugin を読み込む条件（repo root が sys.path に無い）で `register()` が
  `adapters` を import できず `/mcs` とカードが消える退行（flags を直接読む方式に変更、
  Hermes の loader 相当の回帰テストを追加し、旧実装で失敗することを確認）、
  standalone の更新操作が `~/.hermes/scripts` の wrapper を探して拒否される問題
  （`wrapper_path`）、Slack 本文のエスケープ（`<!channel>` 等を無効化）と太字変換・
  全イベント ack・本文送信後の添付失敗を配送済みとする、Discord の添付拒否・容量超過・
  フォーラム投稿・`/mcs` の不要な再登録、launchd job の実行上限（3600秒、SIGTERM も
  ジョブのプロセス群へ転送）、updater 自身の job の再読込を updater 終了後に行う
  （`MCS_JOB_PID`）、Hermes へ戻すときは Hermes cron の確定後に launchd を退役、
  未設定のトークンは再試行扱い、standalone 導入前へのロールバック拒否、
  既存環境での install.sh 再実行では方式を尋ねない、モデルの相互再利用、文書
  （SECURITY.md・INSTALLATION.md・lifecycle-spec・STANDALONE.md・変更記録）。
  修正後に指摘ごとの独立検証（11エージェント）を行い、残った4件と完了度の指摘を追加修正。
  却下: Discord のスレッド指定先（文書化済みの制限）、Socket Mode の死活監視（SDK が再接続）。
- レビュー指摘の追加修正（2026-10-02、5件すべて非ブロッキングだったが解消）:
  - `notify_flush` の送信失敗リトライに5回上限を追加。75（受理なし確実）を返す
    恒久的失敗（revoked token・削除済みチャンネル等）が無限に再試行されていた問題を、
    汎用パスと同じ天井で送信保留へ移行するようにした。未受理の receipt を持つ
    digest/new_messages のメンバーは従来どおり救済キューへ移る。
  - `requirements-standalone.txt` を直接依存+解決済み推移的依存の完全固定にした
    （`audioop-lts` は py>=3.13、`typing-extensions` は py<3.13 の条件付きピン）。
    `_sdk_problem` のピン検証はマーカー付き行をスキップする（条件付き依存は
    全環境に導入されるとは限らないため）。
  - Slack `_mrkdwn` で `**bold**` 以外の単独 `*` を全角 `＊` に変換し、本文中の
    偶発的なペアが Slack の太字として解釈されないようにした。
  - `init` の `--plugin-*` フラグは standalone では `notify.<transport>` のスコープへ
    移すが、スコープが無い場合（と Hermes profile 指定）は警告するようにした。
  - `mcs_recover._notify` の送信が 60 秒を超えた場合、子プロセスを kill して回収する
    ようにした（watchdog 配下に残留子プロセスを残さない）。
