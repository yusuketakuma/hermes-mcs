# 全リポジトリ調査・安定性改善・導入改善（2026-10-01）

開始時の追跡対象390ファイルをすべて調査対象へ割り当て、コード・テスト・配備資産・資料・合成画面を確認した。
過去レビューと同一hashの確認結果は再利用し、変更されたものは対応する差分と実ソース・呼出元を確認した。
新規・未確認の実装は全文を確認した。資料は現行契約・履歴・計画・生成物を区別し、生成物は生成源・参照・形式を検証した。
対象ごとの方法・最終hash・結果は[確認一覧](stability-20261001-progress.json)に記録する。

## 導入改善

- 通常の初回導入は `install.sh` → `mcs_setup.py init`。既存のinitが設定・gateway同期・最終チェックを行うため、重複したservices/checkを初回必須の案内から外した。
- [README](../../README.md)にAIへの依頼文を追加。[AI手順](../SETUP_AGENT.md)は現在のcheckout・既存回答と承認を再利用し、未決事項だけをまとめて確認する。
- ChromeのCDP準備を依存導入後・収集前へ移した。秘密情報をコマンド例へ直書きせず、端末で入力する経路を示した。
- [通常ガイド](../INSTALLATION.md)の通知なし構成に、既存の抽出worker2件の配置手順を補った。文書中のレンダラーは完全合成のパス・HOMEで検証した。
- 導入診断のローカルLLM確認を既存の上限付き通信へ統一し、壊れたmanifestやgateway同期失敗を適切に報告する。

## 安定性改善

| 問題 | 結果 | 回帰の根拠 |
|---|---|---|
| 深いJSON・不正UTF-8・保存結果の型不正 | 共通loaderを再利用し、壊れた項目をunknown・未観測・隔離として扱う。読める履歴や表示は維持 | core/ingest/plugin/semantic/viewsの合成回帰 |
| 対話用の形式確認を見送っても待機状態が残る | 未送信の待機permitを解放してバックログの進行を妨げない | `test_llm_admission.py` |
| 設定先と解析の稼働・形式確認先が不一致 | 生成・models・slots・形式確認で同じ既存resolverを使う | `test_extract_review_regressions.py` |
| 大規模な許可対象を件数制限の後に絞る | SQLiteの既存JSON機能で絞り込みをLIMITより前に置く | 同上、30,001件の合成対象 |
| 未送信の要約修復が一度限りの枠を消費 | 確実に未送信の予約だけ返して再開する。送信済み・成否不明の予約と確認済みの兄弟投稿は保持 | `test_semantic_budget_retry.py` / `test_semantic_llm_retry.py` |
| Slack履歴の確認失敗を空履歴と扱う | 本文・添付を再投稿せず配送結果をunknownに保つ | `test_mcs_slack.py` |
| 不正な患者集約から患者ページを削除・出力を部分更新 | 全患者ページを先に読み取り・描画し、不正なら既存出力を保持してエラーを返す | `test_brain_export.py`、更新前後の全ファイルバイト比較 |
| 未確認検査値を確認された検査値と同じ行に表示 | 共通の未確認判定で「検査候補（未確認）」へ分離。合計6件上限と旧形式は維持 | `test_structured_view.py` / `test_rollup_canonical.py` |
| 特殊文字を含むSQLiteパスが別DBを参照 | updater/recoveryの読取りURIを既存Path.as_uriへ統一。損失計測・receipt・復元の参照先を一致させる | `test_mcs_update.py` / `test_mcs_recover.py`、別schemaの合成decoy DBを含む |
| 失敗したcron一覧の出力からジョブを除去 | 成功した一覧だけを除去判定に使い、検証不能の報告を保持 | 同上、成功・失敗両側 |
| 不正な却下理由が集計を停止・混入する | 既存の理由コード一覧を使い、未知・型不正をunclassifiedにまとめる | `test_qc_view.py` |

独立復旧ツールは旧世代のimport故障時にも動く必要があるため、updaterとの意図的な世代隔離を維持した。
共通loader・通信・resolver・enumを再利用し、不要なstate loader・定数を削除した。新しい本番依存は追加していない。

## 検証

ソース編集を固定した後、共通の隔離runnerで実行した。

| 検証 | 結果 |
|---|---|
| `scripts/run_tests.sh` | 3,269 passed、1 skipped、19 subtests passed（234.27秒） |
| CIと同範囲のruff | All checks passed |
| `python3 ci/gates.py` | 8/8 pass |
| `python3 ci/mine_gates.py --check` | 未追跡の欠陥incidentなし |
| `python3 scripts/update_readme.py` / `--check` | 生成ブロックは最新・check成功 |
| `python3 scripts/release_notes.py check` | 成功 |
| `python3 scripts/readme_release.py --check` | 成功 |
| `python3 scripts/generate_slack_gallery.py --check` | 合成7画面のSVG・PNG・hash整合成功 |
| `shellcheck install.sh` / `bash -n install.sh` | 成功 |
| `git diff --check` | 成功 |

skipはdiscord.pyのserialization検証（依存不在）。Hermes実SDK向けintegrationは
対象checkout・依存がないため収集されていない。その後の[固定版・実SDKの隔離検証](hermes-sdk-20261001.md)で14件すべて成功した。GitHub CI自体は未実行。
途中に並列編集でcode stampが変わった領域テストは最終成功と混同せず、確認一覧に経緯を保持した。

## 未実施・制約

- 実MCS・Discord/Slack・Keychain・原本DB・実ローカルLLM・実Jevを使う検証、本番適用、gateway再起動は行っていない。
- Hermes実SDK統合は固定版ソースと実SDKで隔離検証済み。Pythonはローカル3.11.15、CIは3.13のため同一環境とはしない。実モデルの臨床品質・性能や人間ラベル評価は未実施。
- plugin反映にはgateway再起動、repo外の凍結復旧ツール反映にはinstall.sh再実行が必要。今回は実施していない。
- 生成OpenWikiには外部Jev経路の省略、Slack設定の欠落、古い既読化境界、root絶対リンクが残る。直接編集禁止に従って保持し、[生成briefと次回更新手順](openwiki-update-path-20261001.md)を追加した。fallbackでもsnapshot timestampは必須であり、省略するのはunreadフィルタである。生成結果の照合は未実施。現行の安全境界は[SECURITY](../../SECURITY.md)と実ソースを正本とする。
- ロードマップと過去配備記録は基準日当時の資料。将来機能を今回自動実装したり、現在の稼働状態の証拠として使ったりしていない。

## 探索範囲

`jg`は安全確認した追跡Python/shellソースのコピーに対し、core/ingest/extract、semantic、ops/views、setup/update/recovery/tooling、通知表示と配送の関連領域を検索した。
結果は実ソースとrgの参照で裏付けた。data・config・env・認証情報・患者データは読取り・検索対象外。
検索結果を網羅性・正しさ・テスト成功の証明にはせず、390件の棚卸しと領域ごとの調査を別に記録した。
