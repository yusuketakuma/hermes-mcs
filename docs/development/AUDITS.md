# 監査記録（1.0.14〜1.0.15）

1.0.14・1.0.15の監査記録（JSON・README、計59ファイル・約11MB）は、
リポジトリを軽くするため作業ツリーから外しました。内容はgit履歴に残っています。

削除直前のcommitは `3213eb4` です。取り出すときは次のようにします。

```bash
git show 3213eb4:docs/development/audits/1.0.15/README.md
git restore --source=3213eb4 -- docs/development/audits   # まとめて作業ツリーへ戻す
```

コード・CI・テスト・リリース検査はこれらのファイルを参照していません。
CHANGELOGや過去の受入記録に書かれたパスは、上のcommitで読める履歴上の場所です。

## 含まれていたファイル

- `docs/development/audits/1.0.14/README.md`
- `docs/development/audits/1.0.14/full-maintenance-20261005/PLAN.md`
- `docs/development/audits/1.0.14/full-maintenance-20261005/findings.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/generated-classification.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/inventory.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/original-state-preservation.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/parent-bootstrap-evidence.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/parent-ci-evidence.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/parent-development-document-evidence.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/parent-lifecycle-evidence.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/parent-run-records-evidence.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/parent-semantic-send-gate-evidence.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/parent-semantic-utc-evidence.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/parent-stamp-ui-evidence.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/parent-static-svg-evidence.json`
- `docs/development/audits/1.0.14/full-maintenance-20261005/parent-views-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-core-docs7-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-core-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-core-packet2-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-core-packet3-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-core-packet4-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-core-packet5-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-core-packet6-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-drug-json-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-ledger-audit-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-notify-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-notify-packet2-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-notify-packet3-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-notify-packet4-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-notify-packet5-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-notify-packet6-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-semantic-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-semantic-packet2-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-semantic-packet3-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-semantic-packet4-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-semantic-packet5-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-semantic-packet6-evidence.json`
- `docs/development/audits/1.0.14/maintenance-20261005/maintenance-stamps-evidence.json`
- `docs/development/audits/1.0.15/README.md`
- `docs/development/audits/1.0.15/baseline/stability-audit-1.0.15.json`
- `docs/development/audits/1.0.15/baseline/stability-audit-adapters-1.0.15.json`
- `docs/development/audits/1.0.15/baseline/stability-audit-core-1.0.15.json`
- `docs/development/audits/1.0.15/baseline/stability-audit-evaluation-1.0.15.json`
- `docs/development/audits/1.0.15/baseline/stability-audit-extract-1.0.15.json`
- `docs/development/audits/1.0.15/baseline/stability-audit-ingest-1.0.15.json`
- `docs/development/audits/1.0.15/baseline/stability-audit-operations-1.0.15.json`
- `docs/development/audits/1.0.15/baseline/stability-audit-ops-1.0.15.json`
- `docs/development/audits/1.0.15/baseline/stability-audit-views-1.0.15.json`
- `docs/development/audits/1.0.15/mobile-notification-preview-1.0.15.json`
- `docs/development/audits/1.0.15/round2/stability-audit-round2-1.0.15.json`
- `docs/development/audits/1.0.15/round2/stability-audit-round2-adapters-1.0.15.json`
- `docs/development/audits/1.0.15/round2/stability-audit-round2-core-1.0.15.json`
- `docs/development/audits/1.0.15/round2/stability-audit-round2-extract-ingest-1.0.15.json`
- `docs/development/audits/1.0.15/round2/stability-audit-round2-views-ops-1.0.15.json`
- `docs/development/audits/1.0.15/round3/stability-audit-round3-1.0.15.json`
- `docs/development/audits/1.0.15/round3/stability-audit-round3-adapters-1.0.15.json`
- `docs/development/audits/1.0.15/round3/stability-audit-round3-core-1.0.15.json`
- `docs/development/audits/1.0.15/round3/stability-audit-round3-extract-ingest-1.0.15.json`
- `docs/development/audits/1.0.15/round3/stability-audit-round3-views-ops-1.0.15.json`
