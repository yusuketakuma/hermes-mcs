# 全域監査・修復 checkpoint — 2026-10-05

仕様正本は添付pasted-text-1.txt。対象はhermes-mcs 1 repoの全workspace/package。
B=main 4167697（既存未追跡8/recoverは調査後、明示された条件付き削除承認に従って復元可能な私有コピーを残して削除。別worktree既存変更は保持）。
P=各writerとも4167697のclean worktree。Iはaudit/full-maintenance-20261005専用候補。
前goal turnはprogress（main統合・修復・検証）。既存baseline 6780 passは全域coverageではない。

## 所有と継続順

| owner | 専用worktree/branch | 現packet | 次packet候補 |
|---|---|---|---|
| root | full-maintenance-20261005 / audit/full-maintenance-20261005 | CI・検証入口・台帳・親統合 | script/配布物 → 残docs/records/非機密dot state |
| maintenance_core | maintenance-core-20261005 / audit/maintenance-core-20261005 | mcs/core + tests/core全文 | ingest + tests/ingest → ops取得/ledger消費境界 |
| maintenance_notify | maintenance-notify-20261005 / audit/maintenance-notify-20261005 | urgent/flush/digest + 直接tests全文 | cards/render/transport/views → adapters/standalone/plugin |
| maintenance_semantic | maintenance-semantic-20261005 / audit/maintenance-semantic-20261005 | semantic/llm/runtime + 直接12tests全文 | remaining semantic → extract/evaluation/clinical contracts |

子はGPT-6.1-sol ultra、再帰spawnなし、1 worktree/1 writer。共有ledger/schemaはcore owner。
親は必要境界をreadonlyで照合し、検証済みcommit/patchを順次直列統合。
担当外の実装は根拠と依存を返してから所有を更新。成功testの再実行は変更/失敗/必須検証に限る。

## 受入と指標

inventoryの各pathに入力SHA・module・区分・UNSEEN/PARTIAL/REVIEWED/EXCLUDEDと根拠を残す。
全文読取だけではREVIEWEDにしない。C01..C12/X01..X08の適用性・実経路/consumer・異常系を照合。
確定欠陥は修正前に失敗する合成回帰で証明し、FIXと挙動保持のREFACTORを区別する。
改善指標: 誤配送/誤公開/旧schema破壊/無期限停止を防ぐ期待挙動、または同条件合成計測での負荷低減。
悪化防止: API/CLI/schema/import互換、PHI/secret非使用、人承認/reason/receipt、安全通信・予算を維持。

全814 baseline tracked path、新規/削除/rename、関係untracked/dot stateを最後に集合照合する。
生成物/binary/vendor/実データは理由付き分類し、生成元/配布内容を確認。run recordsは別区分。
新しいaudit台帳/報告自体はrun recordsとしてJSON解析と根拠整合を確認し、自己参照hashを完了証拠にしない。
全領域後に固定Iを実装非担当fresh-context独立reviewへ渡す。必要な全体/SDK/隔離配布物検証を行う。
COMPLETEは全適用source対象REVIEWED、確定修復/有用保守解消、統合/配布物/独立review/保全の証拠が揃った時だけ。
外部/push/PR/保護branch直接変更/deploy・実データ/secret取得・恒久指示変更は禁止。

## 現状

初期inventory: tracked 814 + original untracked 2。全確認は未完。
8/recoverは本文を外部出力せず確認し、見出しだけの残留作業出力と判断。2件33 bytesの削除と復元元は私有 receipt に記録。
親のCI consumer監査、子3packetの確認・再現・最小修正を継続中。

## 追加された受入条件

1. MCS定期アクセスを5分から10分へ変更。取得jobとhealth欠測判定・Hermes/standalone登録を整合させ、LLM timeout等の無関係な300秒は維持する。
2. install/update/recoveryの1.0.13実経路を修復・検証する。旧版からの移行、途中失敗・再試行、service ownership、独立Python/SQLite検査を維持し、合成・隔離fixtureで証明する。
3. Slack/Discord/LINE WORKSの表示を全件追跡。長文/分割、添付、ボタン、metadata、escaping、権限scope、stale/更新/receiptを合成で検証する。実サービス表示や実機配備を未実施のまま成功扱いしない。

親はmcs_setup/installer/updater/recoveryとdeployment schedulingを所有。core次packetはingest/healthの10分化、notify次packetは全チャネル表示。仕様全域の残確認も継続する。
