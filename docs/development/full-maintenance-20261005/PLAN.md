# 全域監査・修復 checkpoint — 2026-10-05

仕様正本は添付pasted-text-1.txt。対象はhermes-mcs 1 repoの全workspace/package。
B=main 4167697（既存未追跡8/recoverは調査後、明示された条件付き削除承認に従って復元可能な私有コピーを残して削除。別worktree既存変更は保持）。
初回Pは4167697のclean worktree。後続packetの固定P/W・専用refは各evidenceを正本とする。Iはaudit/full-maintenance-20261005専用候補。
前goal turnはprogress（main統合・修復・検証）。既存baseline 6780 passは全域coverageではない。

## 所有と継続順

| owner | 専用worktree/branch | 現packet | 次packet候補 |
|---|---|---|---|
| root | full-maintenance-20261005 / audit/full-maintenance-20261005 | 共有台帳・親統合・文書境界 | 固定Iの全体/SDK/隔離配布物検証 → fresh-context独立レビュー |
| maintenance_core | maintenance-hermes-fixture-20261005 / audit/maintenance-ledger-audit-20261005 | 薬剤JSONとledger監査のPython3.10互換性修復・正式証拠を受入済み | 担当完了 |
| maintenance_notify | maintenance-notify-20261005 / audit/maintenance-notify-packet6-20261005 | 文書259件の正式受入済み | 固定Iの実SDK・配布物の隔離起動検証 |
| maintenance_semantic | maintenance-stamps-20261005 / audit/maintenance-stamps-20261005 | スタンプ実装・20基準と両Python各385件を受入済み | 担当完了（thread上限により最終全件は親が両runtimeを担当） |

ユーザー指定はGPT-6.1-sol ultra。実効モデル名は子のAPI応答から確認できないため、要求設定と実測を混同しない。再帰spawnなし、1 worktree/1 writer。共有ledger/schemaはcore owner。
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

初期inventory: tracked 814 + original untracked 2。最新の正式ledger証拠受入後はtracked 911、台帳913件（削除2件を含む）、REVIEWED888・EXCLUDED25・UNSEEN/PARTIAL0。固定runtime I=1cf0505で3.10全件7391成功、3.13全件7390成功/性能1失敗のあと既存100/1000件単独control2成功（30秒基準不変・1000件6.5117秒）、短い必須チェック9件成功。3.13全量exit1は保存し、未変更の成功caseを再利用したaggregate受入とする。最終record HEADでのSDK・隔離配布物・B→H全byte/mode再現・fresh-context最終レビューの実結果は、下記の外部受入正本へ保存してから完了とする。
8/recoverは本文を外部出力せず確認し、見出しだけの残留作業出力と判断。2件33 bytesの削除と復元元は私有 receipt に記録。
各source packet、文書259件、歴史20文書、旧run記録9件、スタンプ氏名表示を親の専用Iへ統合した。旧PASS/途中状態は原文に保持し、現在の全域受入へ転用しない。開始後にmainへ外部追加されたc26a009（テスト時Gateway実操作の拒否）は、元checkoutを変更せず専用Iへ取り込んだ。詳細はoriginal-state-preservation.json。

## 追加された受入条件

1. MCSサーバーの負荷対策として、MCS定期アクセスを5分から10分へ変更。変更理由を利用者向けガイド・仕様・変更記録に明記する。取得jobとhealth欠測判定・Hermes/standalone登録を整合させ、LLM timeout等の無関係な300秒は維持する。
2. install/update/recoveryの1.0.13実経路を修復・検証する。旧版からの移行、途中失敗・再試行、service ownership、独立Python/SQLite検査を維持し、合成・隔離fixtureで証明する。
3. Slack/Discord/LINE WORKSの表示を全件追跡。長文/分割、添付、ボタン、metadata、escaping、権限scope、stale/更新/receiptを合成で検証する。実サービス表示や実機配備を未実施のまま成功扱いしない。
4. スタンプ件数のラベルを「MCS」から「スタンプ」へ統一し、各投稿で押した人の氏名と本人識別を表示する。未取得・古い一覧・再取得失敗・氏名不明・長い一覧の省略を区別し、後から取得した氏名も既配信本文へ反映する。既定offの取得条件・7日対象・1回最大4投稿と予算、人承認・正式タスク完了の境界を維持する。
5. ponytailの最新版への更新は公式の管理経路で4.11.0へ完了。private receiptを保存済み。現在のskill catalogは旧版のため、作業中のCodexを中断せず完了後の再起動で反映する。

親は共有台帳・直列統合・README/ガイド・説明図の正本と生成を所有する。sourceの修復は受入済みpacketを再利用し、追加stampの変更と依存だけ再確認する。全件の正式受入と固定Iの全体検証・実SDK検証・配布物の隔離起動・fresh-context独立レビューは引き続き必須。

## 最終受入の正本

runtime source I=1cf0505eb5c9155d19afa3e3ba218f2640096d20。後続commitは正式証拠と台帳だけで、実行source/test/tool/configの全byte/mode不変を外部manifestで拘束する。旧全件のFAILとpacket-localのpending/PASSは履歴として保持し、最終Iの成功へ書き換えない。

最終受入receipt: `/Users/yusuke/.herdr/evidence/mcs/final-I-20261005-1cf0505/final-acceptance.json`。source/record一致: 同directoryの`final-source-record-equivalence.json`。全ログ・JUnit・SDK・archive50・完全B→H patch/replay・非実装fresh reviewは実行後にSHA/bytesとcommitで結ぶ。最終receiptを読む前にCOMPLETEとは扱わない。元main/実設定/サービス・患者情報を使用した検証や配備は範囲外。
