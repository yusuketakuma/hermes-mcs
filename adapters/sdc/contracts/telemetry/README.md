# sdc.device-telemetry.v1 — 端末テレメトリ契約

Mac側MCSが収集を許可した固定項目を、既存のdevice認証済み経路でCSDCへ送る。
心拍(`sdc.heartbeat.v1`)・棚卸(`sdc.inventory.*`)・固定診断(`sdc.diagnostics.*`)とは別契約。

## 経路

`POST /v2/device/telemetry` — `x-sdc-device`/`x-sdc-key-id`署名認証、`TELEMETRY_ENABLED==='true'`のときだけ受理(既定OFF)。

## Envelope

```json
{
  "schema_version": "sdc.device-telemetry.v1",
  "report_id": "uuid",
  "sequence": 1,
  "observed_at": 1760000000000,
  "sections": { "<section>": { "schema_version": "...", "...": "..." } }
}
```

- `sequence`: 端末ごとの単調増加を推奨するが中央は順序を強制しない(再送・順不同を許容)。
- `observed_at`: 端末側の観測時刻(epoch ms)。受信時刻(`received_at`)はサーバーが記録し、表示でも区別する。
- `sections`: 1〜8個。各値は下記セクション契約の自己完結object。

## セクション(各々が独立したschema_versionを持つ)

| 名前 | schema_version | 内容 |
|---|---|---|
| `hardware` | `sdc.telemetry.hardware.v1` | `chip` `model` `memory_bytes` `os_name` `os_version` `disk_total_bytes` `disk_free_bytes`(全て任意・1項目以上必須) |
| `operations` | `sdc.telemetry.operations.v1` | `uptime_seconds` `boot_id` `services[]`(name/state、最大16) |
| `logs` | `sdc.telemetry.logs.v1` | `window_seconds`(必須,≤86400) `entries[]`(at/level/source/message、最大50) `dropped` |
| `mcs` | `sdc.telemetry.mcs.v1` | `health_overall` `run_status` `state_reasons[]`(最大16) `backup_state` `last_ok_age_seconds`(全て任意・1項目以上必須。health.jsonの固定値だけを写す) |

## 拡張規則(送信対象が変化しても対応できる構造)

- **新しい情報種別 = 新しいセクション名**。中央は未知のセクション名を拒否するため、追加時は本契約へセクションを追記し、中央の`SECTIONS`レジストリ(`fleet/telemetry.js`)へvalidatorを1つ足す。保存はJSONなのでmigration不要。
- **既存セクション内の項目追加・削除・型変更 = そのセクションのschema_version更新**。中央は旧版を受理しない(fail-closed)ため、送信側は対応版だけを送る。
- 送信側(MCS)は**セクションごとに独立したcollectorモジュール**を持ち、収集→無害化→envelope組立を分離する。validatorと対応関係が1対1で追える構造にする。

## 個人情報・機微情報の禁止(送信側の義務)

患者情報・氏名・連絡先・メールアドレス・ホスト名・シリアル値・MACアドレス・ディスクUUID・ファイルパス・URL・環境変数・秘密値・生のコマンド出力・患者/業務データを一切含めない。`logs.entries[].message`は512文字以内の要約のみとし、送信前に無害化する。中央はemail・`/Users/`・`/home/`・`~/`を含むmessageを拒否するが、**これは多層防御であり完全なPII検出ではない**。禁止事項の遵守は送信側の責務。

## 上限・保持

- sectionsのJSONは16KiB以内、entries 50件・services 16件、各文字列は契約の上限内。
- 同一`report_id`+同一bodyの再送は`duplicate`、同一`report_id`+異なるbodyは`409 TELEMETRY_CONFLICT`。
- 端末ごと1日288件まで。本文は受信から7日または端末ごと最新200件の早い方で削除。
- 表示はConsole(管理 `/devices/<id>/telemetry`、テナント `/tenants/<t>/<i>/devices/<id>/telemetry`)で最新1件の内容と受信履歴20件。全値をescapeして表示する。
