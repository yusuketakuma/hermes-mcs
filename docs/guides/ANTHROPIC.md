# Jev・Anthropicの設定と利用状況

このMCSの `.env` にAPIキーを登録すると、設定で無効化していないサービスを自動で使用します。
キーの登録前に、対象の投稿本文・患者情報を外部サービスへ送ることと、利用料金を確認してください。
本文や患者名を自動で匿名化する機能はありません。

## キーと自動有効化

| サービス | `.env` の変数名 | mode未指定でキーがある場合 | 処理対象 |
|---|---|---|---|
| Jev | `TYPESAFE_API_KEY` | `semantic.mode=assist` | 既存の意味チェック |
| Anthropic | `MCS_ANTHROPIC_API_KEY` | `cloud_llm.mode=on` | 患者文脈の更新と旧版投稿の再抽出 |

キーはMCSの導入先にある `.env` に保存し、所有者だけが読み書きできる権限 `0600` にしてください。
通常ファイル・所有者・権限・サイズを確認して読み取ります。プロセス環境やHermesのキーは流用しません。
キーの値をチャット、診断資料、Gitへ含めないでください。

modeと対象患者の設定を両方省略した場合、自動有効化されたサービスは全対象を処理します。
明示した `mode=off`、対象患者、除外設定、低い利用上限は維持します。
既存設定に `off` や予算 `0` がある場合、キーの追加だけでは有効になりません。
キーがない場合は外部へ送信せず、Anthropic対象の処理には既存のローカル経路を使います。

Jevの自動有効化は `summary_mode`、`loop_mode`、`extract_qc`、canonicalへの切替を変更しません。
`enforce` や公開判定に必要な評価・承認も従来どおりです。

## 件数・費用・対象を制限する

Jevは日本時間の1日500要求が上限です。再試行、モデル一覧の取得、実通信する評価CLIも同じ上限へ数えます。
`semantic.daily_request_budget` に500未満を設定すれば、その値を使用します。`0` は送信停止です。
500を超える設定でも実効上限は500です。評価CLIが通常のmodeを迂回する場合も、この件数上限を迂回しません。

Anthropicには `cloud_llm.daily_request_budget` と `monthly_cost_limit_usd` を設定できます。
どちらも未指定の場合、件数・月額の上限はありません。`max_concurrency` の既定値4はプロセスごとの上限です。
`usd_jpy` は円換算の試算に使う任意の固定レートで、自動更新しません。

`config.json` の該当部分を編集する例です。既存設定全体をこの断片で置き換えないでください。

```json
{
  "semantic": {"mode": "off"},
  "cloud_llm": {
    "mode": "off",
    "project_ids": [],
    "routes": ["patient_context", "reextract"],
    "daily_request_budget": 0,
    "monthly_cost_limit_usd": 0
  }
}
```

手動で有効なmodeを指定する場合は、対象も明示してください。`project_ids` は患者のプロジェクトID一覧、
`null` は全対象、`[]` は対象なしです。Anthropicの `exclude_project_ids` は指定した患者を除外します。
`routes` では `patient_context` と `reextract` を選べます。モデルは `claude-haiku-5-5` に固定しています。

| Anthropicのmode | 動作 |
|---|---|
| `off` | Anthropicへ送信しない |
| `dry_run` | 送信せず、処理候補を記録する |
| `shadow` | Anthropicへ送信して比較する。ローカルの結果を正本にする |
| `on` | 検証を通ったAnthropicの結果を使用する |

外部処理が使えない場合も、ローカル処理の受付条件や期限は維持します。
未処理の対象を完了扱いにはしません。

## 送信せずに試算・照会する

導入先で次のコマンドを実行できます。保存済み記録を読み取り、APIへは送信しません。

```bash
python3 mcs/ops/mcs_setup.py cloud-llm plan
python3 mcs/ops/mcs_setup.py cloud-llm estimate --days 14
python3 mcs/ops/mcs_setup.py cloud-llm log --days 14
python3 mcs/ops/mcs_setup.py cloud-llm compare --days 14
```

`--since YYYY-MM-DD` は日本時間の日付で絞ります。`log` だけは `--project` でも絞れます。
文字数はトークン数ではありません。履歴が不足する場合、費用の試算には未確定の項目が残ります。
`compare` は保存済みのローカル結果との一致を集計するもので、独立した正解との精度評価ではありません。

AnthropicはHTTPの試行前に件数と費用を予約します。結果不明の要求には予約額を残すため、
表示額は実際の請求額と一致するとは限りません。月額上限に近い場合は保守的な予約によって送信が先に止まります。
料金表の確認日は2026-10-26、ローカルでの再確認期限は2026-11-25です。期限切れ・未知の料金条件では送信を止めます。
Anthropicの利用記録は62日、Jevの利用記録は7日保持します。

## 停止・再開する

Anthropic全体を停止するときは、理由を付けて次の操作を行います。

```bash
python3 mcs/ops/mcs_setup.py cloud-llm stop --confirm-human --reason '外部処理を一時停止'
python3 mcs/ops/mcs_setup.py cloud-llm resume --confirm-human --reason '設定と利用状況を確認して再開'
```

返された `command_id` を控え、同じ操作を再試行するときは `--command-id` で指定します。
認証・支払い・料金確認の問題による停止も、原因を解消してから明示的に再開します。
再開時には現在の設定、キーの存在、料金表、保存済み利用記録を再確認します。Jevの停止は `semantic.mode=off` または日次予算 `0` で指定します。

## 更新時の注意

患者文脈の処理は `patient_context_refresh.py`、更新計画は `patient_context_plan.py` が所有します。
終日処理と日本時間03:30の作業日切替は維持し、保存データと設定の `patient_context_nightly` は互換性のため残しています。
実装・設定・モデルを変えると候補の識別値が変わり、旧候補や保留中の承認記録の再確認、再抽出が必要になることがあります。

合成テストで確認するのは振り分け、上限、保存、停止・再開の制御です。
実APIでの速度・生成品質と本番での受入は別に確認します。
処理の所有者と公開条件は[推論経路](../development/LLM_RUNTIME.md)と[患者文脈の仕様](../specs/patient-context.md)を参照してください。
