# 1.0.23 リリース候補受入票

2026-10-17。作業branch `release/v1.0.23-prep`、基準HEAD `5d71c1a0d7e51d8da1aec2bd6006626b68a26ac6`、前版 `v1.0.22 / f8c6c20d`。未コミット差分を含む実装中の記録であり、公開可ではない。

## 範囲と現在の判定

Fableレビューの1〜32、Slack/Discord表示改善、導入・更新・復旧の改良、全更新内容の反映確認、不要コード・コード配置・文書整理、追加承認D1〜D5（薬剤master活用）とW1（全126WT集約）を追跡する。旧ROADMAPの #1〜#32 とは別番号である。実データ・実サービス・実LLM・本番配備・公開は未実施。実装と合成検証を品質合格や実環境受入に読み替えない。

| レビュー項目 | 成果物・条件 | 状態 |
|---|---|---|
| 1 | 既存レイアウト固定・移行対象の読取診断 | 実装・局所検証済み |
| 2 | rollback時の再投稿防止 | 更新・復旧経路の確認中 |
| 3 | worker所有者と起動証跡のdoctor/health診断 | 実装・局所検証済み |
| 4 | 所有者不明時の拒否・固定SDK属性契約 | 実装済み・固定Hermes 21件とstandalone 12件成功 |
| 5 | LINE WORKS文書・画面の整合 | guide・SVG/PNG2枚・生成check・目視済み |
| 6 | 1.0.23更新手順 | 更新済み・updater最終契約と照合待ち |
| 7 | 1.0.22からの更新fixture | schema/更新364件成功・tick19件は編集静止後 |
| 8 | 名前付き受入ゲートと固定候補 | 本票で追跡中 |
| 9 | 待機理由行列・Discord所有者照合 | 実装・局所検証済み |
| 10 | LINE WORKS要約待機・公開条件・改訂上限 | 実装・局所検証済み |
| 11 | 候補指紋と独立人承認receipt | 実装・局所検証済み |
| 12 | 実データと合成の品質証拠区別 | 実装・局所検証済み |
| 13 | holdout封印・split・blind二重評価 | blind資料作成・二重レビュー・裁定・元候補への結合まで実装、局所検証済み |
| 14 | 分母・Wilson・緊急度/AI優先・A/B基準 | 実装・局所検証済み |
| 15 | 私有snapshotのローカル評価runner | claims/loops・監査/描画/配送証跡の同一候補束縛を実装・局所検証済み。実データ/モデル受入未実施 |
| 16 | 薬剤言及履歴と取得範囲 | 実装・局所検証済み |
| 17 | 別名提案・承認・rollback | 実装・局所検証済み |
| 18 | 自動export禁止・私有表示境界 | 局所検証済み |
| 19 | 復元collect-only・配送holdの可視化 | 実装・局所検証済み |
| 20 | RPO鮮度・年齢/遅延の区別・overrideなし | 実装・文書化済み |
| 21 | offsite/verify/escrow証跡分離 | 実装・局所回帰済み（root313件・別保管鍵CLI1件）。固定候補全体検証は別ゲート |
| 22 | 全profile再起動と稼働証拠 | 診断実装済み・更新経路確認中 |
| 23 | 表示変更の分類・更新注意 | 最終文書整合待ち |
| 24 | clean install・22→23 plan/apply | 更新回帰済み・配布経路確認中 |
| 25 | standalone所有判定と表示 | 実装・局所検証済み |
| 26 | レイアウト変化で保留/上限を解除しない | 実装・局所検証済み |
| 27 | LINE WORKS同一カード/トークン再利用 | 実装・局所検証済み |
| 28 | 要約/スタンプと本文chunk分離 | 新規layout3で実装・旧layout1/2固定 |
| 29 | 要約有界待機・改訂上限・時計だけの再送抑制 | 実装・局所検証済み |
| 30 | 配送進捗中の誤った停滞警報抑制 | 合成300件backlog検証済み |
| 31 | unknown partのplan・承認・矛盾検査・receipt | 実装・API/ローカルCLIの合成検証済み。配送独立指摘も修正・回帰済み、チャットへ操作公開なし |
| 32 | 部品上限の可視化・原文保持 | 実装・局所検証済み |

## 設計上の決定

- 旧layout1/2の指紋と部品名を保持する。新規論理カードだけlayout3で要約・スタンプのヘッダと本文を分離する。旧LINE WORKSカードの実内容が変わる場合は従来形式の本文追加投稿が残る。既存全カードへの一斉移行は行わない。
- LINE WORKSの結果不明の解除は遠隔の未送信を証明しない。操作者の申告と重複リスク受容、対象・manifest・attempt・部品の束縛、receiptを必要とする。他の保留・復元holdを解除しない。
- 復元訓練はcollect-only。配送再開の受入ではない。期限超過backupのoverrideは提供せず、端末交換まで含むRPOを運用時に設定する。
- 評価用合成データはG6昇格証拠にしない。実データ評価と人承認は別工程。監査・配送を観測しないローカルrunに成功値を補わない。

## 必須の最終ゲート

以下は現在差分の固定後に証拠を対応付ける。過去候補の成功やskipを今回の成功に流用しない。

| ゲート | 状態 |
|---|---|
| 全実装要件→コード/テスト/設定/資産/文書/配布物の対応表 | [取り込み対応表](UPDATE_ITEMS_1.0.23.md)を作成。項目12/13/15の補完実装も同期済み |
| インストーラー・アップデーター・リカバリーの実入口・中断/再実行/rollback | 担当検証中 |
| 全126WTの製品差分・未追跡・独自履歴の処置 | [最終照合台帳](WORKTREE_INTEGRATION_1.0.23.md)で完了。未収録5合成テストを追加、元WT保持。最終候補commitとの再照合は別ゲート |
| 全repoの不要コード・配置・文書整理 | [全領域調査](REPOSITORY_HYGIENE_1.0.23.md)。削除根拠のある候補0、古い現行文書導線を修正 |
| `scripts/run_tests.sh` 全体＋関連integration | 編集完了後 |
| 固定Hermes SDK `check_pinned_sdks.sh hermes` とstandalone SDK | Hermes 21件、standalone 12件成功。固定SDKを隔離導入、生サービス通信なし |
| `ruff check` CI全範囲、`ci/gates.py`、`ci/mine_gates.py --check` | 最終差分で未完 |
| `release_notes.py` check/build、`tests/release` | 未完 |
| `update_readme.py --check`、`readme_release.py --check`、1.0.23 READMEレビュー | 未完 |
| Slack/LINE WORKS gallery生成・check・目視 | 統合待ち |
| 公式薬剤masterの新規取得時刻・ZIP SHA・bundle検証 | 2026-10-17T02:11:57Z再取得、同梱20261013 ZIP SHA `c3efc8aa772b9bf54cfa992e8df3bc82480f86f35bbeb52522d0997144140fb5` と一致。bundle 19,286行検証成功、activation=false。importer追加変更後も再検証成功 |
| CodeRabbit固定差分（比較元は前版本番相当SHA、秘密/実データ除外） | round1は131 text files全件、completed/exit0。3指摘のうち2採用、1は既存NULL安全処理により不採用。補完実装と修正後のround2待ち |
| 最終候補のGit差分・未追跡・意図しない変更確認 | 未完 |
| 最終SHAの必須CI | push未承認につき未実施 |

## 証跡の所在

非機密の検証記録は `~/.local/state/mcs-quality/v1.0.23/`。初期調査の結果は [AUDIT_1.0.23.md](AUDIT_1.0.23.md) に履歴として保持する。Oracleには機密を含まない設計要点だけを送り、browser / GPT-6-pro / Pro thinkingで相談した。採用した不変条件は旧指紋固定、旧コードrollback前の防御、権限世代を変更しないトークン再利用、独立承認receipt、原子的な状態変更。回答自体はコード検証の代替にしない。


## 追加承認: 医薬品マスター活用5項目と全ワークツリー集約

2026-10-17、ユーザーが以下の全項目を1.0.23へ明示追加。安全な読取機能と確認候補を提供し、処方正本・臨床同等性・服用中を自動確定しない。D1〜D5は実装・局所合成検証済み。W1は全126登録WTの意味照合と必要な5合成テストの集約が完了した。これらを固定候補のCodeRabbit・全体テスト・固定SDK・最終CIの成功とは扱わない。

| ID | 受入条件 | 実装・局所検証の根拠 |
|---|---|---|
| D1 | 商品名/一般名をまたぐ履歴検索、規格/剤形を保持、曖昧候補を区別 | `DrugMap.related_names`と`timeline --name`。現行pin・単一公式identityのみ横断。`test_drug_master_reference_workflows.py` / `test_drug_reconciliation.py`で規格保持・曖昧・旧ingredientを確認 |
| D2 | 規格/剤形/単位の違いを候補提示、欠測や曖昧を一致としない | `compare_surfaces`と`check-surface`。規格/剤形/単位差はreview、欠測はunknown。同等性確定なし。`test_drug_master_reference_workflows.py` / `test_mcs_drug_master_cli.py` |
| D3 | master版間の名称/廃止/経過措置を全行比較、採用辞書への影響、総件数と省略 | `importer.catalog`・`drug_master_compare.compare`と`master-impact`。廃止済み行も保持、旧辞書code照合・総数/省略・固定SHA拒否を専用test/CLIで検証。旧v1互換・8MiB上限維持 |
| D4 | 開始/中止/増減/変更予定を原文根拠とcode候補で時系列化、人物・時点・予定/実施を区別 | `timeline`のmention/change/drug_groups。`test_drug_reconciliation.py`で本人/家族・過去/予定・否定・動作とepistemicを確認。実施の記載はaffirmed＋performed/done＋asserted/reportedだけ |
| D5 | 取得済み構造化薬歴と投稿を照合、差の確認候補と情報時点/取得漏れを表示 | `reconcile`。構造化薬歴の取得時点・保存期間・失敗後の旧情報を保持。欠測名・単位不足・他者・古い情報・ページ継続を`test_drug_reconciliation.py`で確認 |
| W1 | 全126登録WT、非merge68 SHA・merge16 SHAを追跡。既統合/後続置換/レビュー基準を区別し、未収録5合成testsを取り込み | [全件台帳の最終処置](WORKTREE_INTEGRATION_1.0.23.md#最終の意味照合結果)を確認。追加33件・後継機能215件成功。全126WT・非merge68 SHAとmerge16 SHAを処置済み、最終commitは未実施 |

元WTの変更・履歴・保留・未終了ツールを保持する。旧レビューsnapshotや統合済み変更は重複投入せず、取り込み判定と根拠を記録する。

薬剤機能の契約と操作は[薬剤master活用仕様](../../specs/DRUG_MASTER_WORKFLOWS.md)。公式剤形code（内用/外用/注射等）と薬名中の錠/散等を混同せず、doseを製剤規格へ流用しない。D1〜D5は読取りと確認候補の経路であり、処方の正本・現在服用中・成分同等性・禁忌判定を作らない。私有辞書の自動有効化、全投稿の再LLM抽出、患者別結果の自動export/通知は追加しない。

W1の最終記録は705件のdirty/untrackedパス観測（集約先の作業中106件を含む）を追跡する。operations210件は既統合179・後続置換31、旧Claude ops8 SHA/32 path pairsは残差19を独立照合し、製品変更の未統合候補は残っていない。非祖先merge16件もremerge差分なしを確認し、非merge68件と別に記録した。`worktree-final-receipt.json`が最終証跡のSHA256を固定する。初回inventoryや途中の証跡を最終記録へ置き換えて消さず、最後の処置と組み合わせて読む。


### 項目11の既存context契約の照合

`extract_llm._context_contract()`単独にはモデル名がないが、呼出元`_prepare_jobs()`は`patient_state_context.prepare(model=target[1], contract=...)`へ渡し、`patient_state_context._binding()`がmodel/contract/attempt/enabledを同時に束縛する。従ってmodelの再追加や同じcache機構の新設は行わない。候補評価のfingerprintにはモデルとprompt/schemaを別途含める。


### 拡張後の局所検証と独立指摘

- 薬剤master変換/関連検索/版差分は70件と関連155件、薬剤履歴・構造化照合・CLIは66件成功（対象重複のため合算しない）。親CLI追加4件も成功。
- 独立レビューで推測・不明の薬剤記載が実施記載へ強められる経路を修正。epistemicを保持し、asserted/reportedかつpolarity=affirmed、workflow_status=performed/doneの場合だけ実施記載にする。関連CI/薬剤回帰101件成功。
- worker起動失敗の証跡誤称、復元markerが消えた後の個別hold見落とし、カード配送済み/部品未完の保留件数過少を独立レビューで修正。root関連313件と失敗起動13件、別保管鍵のCLI訓練回帰1件成功（重複あり）。
- literal `:memory:` の接続のみsnapshot gateのファイル読取要件から分離した。可変パス・ファイル名・読取専用URI偽装の拒否は維持し回帰を追加。全10 gateとmine gates成功。
- importer変更後の公式bundle検証成功。Slack8枚、LINE WORKS2枚の生成check成功。実アプリ表示・実配送の成功を意味しない。
- operations WT210件と旧Claude ops8commit/32path、抽出・semantic7file/15関数の残差は既統合/後続置換を確認し、旧挙動の再投入をしない。未収録の合成復旧テスト5filesを追加し33件成功。全126WTの最終照合・処置は別表で完了し、最終候補commitと必須検証は未完了のまま区別する。

- 配送の独立レビューで、card自身のunknownを子部品との矛盾とみなして既存の人承認復旧を拒む回帰を修正。card自身のunknownだけを除外し、配送済みcard/子部品とunknown子部品の矛盾検査を維持。追加12件・既存復旧回帰78件成功。


### 通知復旧CLIと残る最終確認

`mcs_view.py card_part_plan`は公開snapshotからscope/hash/attemptだけを表示し、患者本文や承認済み値を返さない。`control card_part_resolve --confirm-human`は既存のstdin envelope検証とenqueueへ接続する。従来の`card_resolve`もproject入力不要のまま。CLI・既存operations・part復旧の合成20件、ruff、diff check成功。人承認、理由、worker停止、LINE WORKSの重複リスク条件と、チャットtoolへの非公開を維持する。手順は[通知復旧](../../guides/NOTIFICATION_RECOVERY.md)。

配送の独立レビュー3件を修正した。card自身のunknownを子部品の矛盾から除外する回帰12件と既存復旧78件が成功。LINE WORKSはsource_generationが同じ場合だけカード/トークンを再利用し、変更後の新ボタンが操作可能・旧ボタンがstale_sourceとなることを確認した。新旧の部品予算超過holdは直接公開・recoverの両方から除外し64件成功。3修正の専用回帰は27件成功（対象重複のため合算しない）。固定候補の全体検証は別ゲート。

追跡IDはFable 1〜32、D1〜D5、W1の計38件で欠落なし。全体テスト・CodeRabbit固定差分・固定Hermes/standalone SDK・最終Git差分・exact CIは前掲ゲートの未完了状態を維持する。局所検証件数は重複を含み、合算して全体成功件数を作らない。

Doctorの既定DB probeによる既存契約の回帰を修正した。DB読取は既存の明示 `--probe data` に限定し、markerなし・未probeはunknown。関連96件成功。

### 評価経路の補完と最終局所検証

項目12は別保存の私有利用許可receiptをsnapshot/holdout/purposeへ結合し、欠測・不一致を拒否する。項目13は独立A/B資料、二重読み、第三者裁定、元候補へのlabel結合をCLIで実装。項目15は既存の監査・最終claims/loops・描画/配送receiptを同じsource/candidate/policy/generationに束縛して生成時に封印する。人の実在・実際の盲検実施・利用許可の権限は運用確認であり、自動で保証しない。

最終4ファイルの評価回帰125件成功。完全合成200caseのgenerate→scoreで全必須metricの分母と数値閾値を満たせることを確認し、syntheticのG6不合格は維持した。実データ評価・実モデル・外部監査・配送は実行していない。後付け改変と不一致の拒否、CLIの私有出力・汎用エラーも確認した。

通常・外部の両recoverでrestore_incompleteをinterrupted_pre_mergeへ誤分類しない修正は関連335件成功。補助的な保留件数SQL失敗を復元全体の失敗としない修正はbackup102件成功。
