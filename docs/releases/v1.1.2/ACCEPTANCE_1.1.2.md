# v1.1.2 受入準備票 — 未公開

この票は v1.1.2 の変更記録と説明資料を現在の候補へ準備した作業の記録です。候補全体の合格、本番適用、公開の完了を示すものではありません。

## 今回の対象

- `changes/` 直下78件（後続mergeで2件追加）の記述を現行実装へ整合させ、`release_notes.py build` で `changes/archive/1.1.2/` へ移動。
- 個別の履歴抽出・意味解析への新規クラウド送信は停止済みで、新規送信は患者コンテキスト（作成・更新・品質修復）専用という現在の仕様に文言を合わせた。停止前の予約と課金済み回答の回収経路は存続。
- 説明図のSVG・PNGを現在の仕様で再生成し、描画結果を確認。記録は `visual-source.json`。
- CHANGELOG の v1.1.2 節を生成。README のリリース案内を同じバージョンへ更新。

## 検証済み（この作業の範囲）

- 変更記録の形式：`release_notes.py check` が全78件を受理（構造・refs・技術詳細の要件）。
- 変更記録の内容：閉鎖済み個別クラウド経路・休眠回収・Slack通知先分離など、現在の実装との静的照合。
- 図版：PNGを描画して文字欠落・重なり・余白を目視確認。細い表示幅（390px想定）で可読。
- README 生成ブロック：`update_readme.py` による再生成との一致（driftなし）。

## 上位工程が保持する受入記録（未再実行）

- 全体 suite：9633 passed + 91 subtests passed / 25 fail / SDK専用 2 skip（実測 `2220b03808c5bc1068d77b3bed6a4b1601eb3bef`・exit 1・source_stamp_changed=[]、ルート供給値）。
- 105-file batch（`cb0` 固定・実終了）：exit 1・3016 passed / 24 failed / 0 skip / 2693.81秒・source_stamp_changed=[]・clean。元25 failは全解消（semantic HTTP含む）。当該node `test_view_reads_only_quoted_bodies_without_reextracting_history` と追加5件（marked厳格照合・consumer write失効・rollback・SQLite失敗・制御例外伝播）が実logで PASSED。
- 既知24 failの末端traceは全て修正 `6129d37a` の原因と整合し、`f7503e5a` 固定の13file統合batch（24failの3file＋通常JOIN境界＋readiness witness/cursor/read境界＋updater2file＋update candidate＋schema/update origin3file）で全解消を確認（exit 0・1072 passed / 0 failed / 0 skip・約602秒・source_stamp_changed=[]・clean、ルート供給値）。lint 0・gates 10/10・mine 0・manifest digest `9726b4c1` 一致もルート確定。
- SDK別lane42件（standalone13・Hermes29）：実行SHAは `f11384da`。SDK対象bytesは `f113..cb0` で不変とルート確認済みで再利用可（旧 `b963` を実行SHAと記録しない）。
- 失敗nodeの最終照合：過去suffix65・combined25・followup24の失敗nodeを最新JUnit3本とrename対応で照合し、108 unique failed nodesすべて最新PASSに対応・未対応0（証跡 `observed-failure-resolution-current.json`、ルート供給値）。単一の全体suite全pass・CI成功とは記録せず、ユーザー指示（2026-10-31）の失敗修正＋影響範囲再検証による合格判断の根拠として使用可能。製品・テスト・資産は `6806843a` と `f7503e5a` で差分0をルート確認済み。
- 公式医薬品マスター：`drug-master-fresh-check.json`（2026-11-07）で同梱 bundle と一致確認済み（`matches_bundle: true`、出典・hash・行数記録済み）。有効化は要オペレーター承認で未実施。

## この資料では確認していない事項

- 必須ゲート・CI全ジョブの成功（既知24 failは13file統合batchで解消確認済み）。
- 最終候補SHAの固定と、統合済みソースに対する source manifest の検証。
- 本番適用・実機稼働・適用後のエラー観測。
- GitHub 上での画像表示、Release本文・asset・tag・公開。画像はリポジトリ内相対参照で保持し、外部表示は未検証。
- Git への push・tag・Release 公開（権限範囲外として未実施）。

最終リリースSHAは未固定です。観測時のHEADと未コミット変更は `visual-source.json` に記録しますが、リリース候補の固定や受入を意味しません。
