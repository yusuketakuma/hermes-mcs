# #20 チャットからのタスク候補抽出・v4 能力強化

作成日: 2026-09-30。状態: 順序 1〜6 を `wip/roadmap20-ultracode` に実装済み（20-D/E は未着手、設定変更は無し）。ユーザー要望は「チャット文面からタスク化する能力を、構造化データ・LLM 処理の強化を前提に引き上げる」。実データ・実 LLM による現状精度や性能は未計測。

2026-10-03照合: 20-A〜Cの依頼種別・条件・返信種別・rollupの`reply_state`はv1.0.10基準のコードに存在する。
上のworktree名と以下の設定・実測は当時の記録。設定変更・enforce/canonicalの切替完了を宣言しない。
現在の順序・版割当は[ROADMAP](../ROADMAP.md)を正とする。1.0.11のF-7で`reply_state`を送信者ID比較に直し、
ID不明時は別人と断定しない。表示追加は1.0.12の#25で、G6・calibration・20-Dの未決ゲートは維持する。

改訂: 2026-09-30 第4版。第4版は実装判断（decision list）に合わせた行単位の訂正のみ（QC の走査範囲、kind 語彙、evidence 必須化、due_text 表示、rollup キー名、20-C の thread 単位・閲覧のみ、semantic_loops 判定文の保留、20-D の前提、容量ゲートの算出元、signals の kind ガード）。第3版は 第2版（同日）で利用者に届く経路を先に強化する順序へ組み替え、第3版で **shadow/off の機能は on にする** オーナー方針（2026-09-30）を取り込んだ。コード上のゲート（enforce の calibration、canonical の G6 token）は迂回せず満たして通す。初版・第2版の本文は git 履歴を参照。読み取り側の新項目（kind/condition/due_text/reply_state）は extract_llm 経路のみに出る — `canonical_projection` が投影する request_pending は to/from/action/due/unverified だけを持ち、投影が有効な投稿では #20 以前と同じ表示・集計になる。

## 目的と境界

「誰から誰への、何を、いつまでに、どの条件で行う依頼か」を根拠付きで抽出し、返信による受諾・途中経過・回答・完了報告・取消を区別できるようにする。

hermes-mcs は抽出と候補提示を担当する。正式な担当割当・期限確定・タスク作成・完了承認の正本は zaitaku-calender 側。既存 `mcs_requests` 台帳の凍結方針を維持し、自動登録・自動完了・新しい外部送信先を本計画に含めない。既存の外部送信（Jev API への本文送信）は shadow で既に行われており、on 化で送信範囲・payload は広げない。

明示依頼だけでなく、「確認いただけますか」「次回訪問で残薬を確認します」のような間接依頼・自己の実施予定も別種の候補として扱う。「食欲が低下した」から「医師へ連絡する」を生成するような臨床知識からの新規行動の推論は対象外。条件付き依頼は条件未充足のまま保持し、実行指示に昇格しない。不明な担当者・期限は不明のまま表示し、捏造しない。

## 稼働構成と「on」の意味

`config.json` の enum/bool 値のみ確認（2026-09-30。認証情報・患者データ・原本 DB には触れていない）。

| 設定 | 現在 | on にすると | コード上のゲート・注意 |
|---|---|---|---|
| `semantic.mode` | shadow | `enforce`: Jev 判定を通知に使う。監査 PASS の要約が `semantic_notice` として Discord/Slack へ配送され、degraded notice も出る（`semantic_drain.py:1022`、`semantic_send_gate.py`、`notify_flush.py:320`） | enforce は `threshold_mode: calibrated` + `calibration_version` 必須（`semantic_policy.py:112`）。`calibration_version` は policy fingerprint に入るため、切替で現行の解析結果が全て非 current になり再評価の波が起きる（日次予算 2000 で有界） |
| `semantic.summary_mode` | shadow | `assist`: 生成された要約候補を `summary_review` で人が比較・採用できる（`publication_mode == "assist"` が採用条件）。`enforce`: 上記の配送 | assist は設定のみ。`mcs_setup.py init` の choice は off/shadow/enforce なので `--set semantic.summary_mode=assist` で入れる |
| `semantic.loop_mode` | shadow | drain は `!= off` しか見ない。Loop 候補は shadow でも生成され、`read kind:"loops"` で閲覧・`loop_artifact_id` + `loop_match_confirmed` で人が採用できる（`hermes_plugin/README.md`） | assist は設定のみ。enforce は calibration 必須 |
| `semantic.fact_source` | shadow | `canonical`: カード・rollup・export が `canonical_projection` を読む | `fact-source canonical --gate-evidence report.json` のみ。G6（`g6-criteria-v1.json`、人手ラベル 200 件以上、gate PASS）の評価レポートが token になる。**20-D の項目追従前に切り替えるとカードの「依頼:」行が to=不明・from なしに退行する** |
| `semantic.threshold_mode` / `calibration_version` | shadow_only / 未設定 | `calibrated` + 版文字列 | `semantic_bench.py calibrate` が shadow 期間の `semantic_audit.claim_audit` から match_threshold の what-if を出す（読み取り専用、実 DB、オーナー実行） |
| `semantic.extract_qc` | annotate | 最大値（off/annotate のみ） | 変更なし |
| `daily_digest` / `self_posts` / `signals.notify` / `notify.interactive` | on | — | 変更なし |
| 20-B/C で足す新項目 | — | 設定フラグを設けず、配備で即有効 | `EXTRACT_VERSION` は上げない |

`update.mode`（自己更新、未設定=off）は本計画の範囲外。

## 第2版レビューの所見（要約）

1. 利用者が Discord/Slack カードと患者 rollup で見る「依頼:」行は **extract_llm v4 の `requests` だけ**（`structured_view._request_lines`、`rollup.recent_requests`）。canonical v2 と Loop 候補は shadow 出力。初版は 20-B〜D を canonical 経路の上に組み立てていたため、G6 を通すまで利用者に何も届かない構成だった。
2. `assignee_text` はどの経路も生成しない（legacy `_FACT_PROMPT` にも v2 `_FACT_V2_PROMPT` にも宛先・依頼者の項目がなく、`semantic_loops` が読むだけ）。canonical の `project_v2_doc_legacy` が to を「不明」にするのはその帰結。
3. extract_llm には to/from/action/due/due_text（evidence はスキーマにあるがプロンプトの requests 行には無く、全依頼が unverified=候補扱いになっていた。第4版で訂正）、投稿日時基準の相対日付規則、参照専用スレッド文脈（root + 直近 3 返信）、v1 ルール候補、Jev QC の 1 回再抽出、bench と合成 cases が既にある。足りないのは依頼の種別・条件・複数依頼の分割規則、返信の種別、それを採点する評価ケース。
4. 初版の `request_detail` 派生 artifact（独自 repair・cache・検証 receipt・superseded 管理）は必要性が未実証の新機構なので保留。canonical 側は `validate_fact` が既知キー以外を捨てるため、省略時 `unknown` の加法改版で旧 doc 互換のまま足せる。
5. 返信関係の唯一の経路は `semantic_loops` の Jev 評価（40 ペア/deadline）。カード経路には返信の種別すらない。

初版の安全条件（根拠束縛・不明の保持・捏造禁止・自動登録なし・外部送信 payload の不変・G6 を下げない）は全て維持する。

## 現行コードから確認した基盤

| 領域 | 現状 | 本計画での扱い |
|---|---|---|
| カードに出る依頼 | `extract_llm.py` `requests`: to（職種 enum）/from/action（#20 以前はプロンプト 15 字、現在 30 字。保存 60 字）/due/due_text。evidence はプロンプトで要求されておらず、全件 unverified（依頼候補（未確認））で表示されていた | 種別・条件を足し、evidence をプロンプトで必須にし、複数依頼の分割と除外規則を明示（20-B） |
| 文脈 | `_thread_context`: root + 直近 3 返信を参照専用で注入。evidence は対象本文に限定 | 変更しない。返信種別の判定材料に使う（20-C） |
| 品質 | `extract_qc`（Jev、annotate）の走査は `_QC_SECTIONS`（vitals/meds/symptoms/labs/events）のみ。**`requests` は監査対象外**で 1 回の再抽出も他 section の所見で起きる | 新項目（kind/condition/reply）は Jev に届かない。`requests` を `_QC_SECTIONS` に加えるのは送信 payload の拡張なので別承認。in-call repair と thin retry では `reply` を落とさない（QC 再抽出は従来どおり置換） |
| 評価 | `extract_cases.json` + `extract_bench._match_request`（action 部分一致 + to/from/due/evidence）。`semantic_evaluation` + G6 + `semantic_blind` の人手ラベル手順（`evaluation/annotation-guide.md`） | 合成ケースと照合項目を追加（20-A）。人手ラベルに依頼・返信の項目を含め、G6 と共用（20-E） |
| rollup | `recent_requests` に kind(=to)/ctx(=action)/flag を 15 件まで | 新キー `req_kind`/`condition`/`due_text`/`reply_state`（`reply_conflict`）を追加。`kind` は to のまま、既存キー不変 |
| canonical v2 | `request_pending` fact は statement/workflow_status/event_time/根拠のみ | 加法改版で extract_llm と同じ項目を持たせる（20-D）。canonical 切替の前提 |
| Loop 候補・返信関係 | 世代束縛・候補同一性・Jev 判定（同じ薬剤・行動・期間の一致を要求）。閲覧と人手採用の経路あり | 変更なし。判定文の修正は保留へ（下記） |
| 要約の採用・配送 | `summary_review`（assist）、`semantic_notice`（enforce）、`semantic_observe` の日次観測 | 20-E で on 化。新規実装なし |

根拠ファイル: `mcs/extract/v4/{extract_llm,extract_bench}.py`、`mcs/extract/rollup.py`、`mcs/views/{structured_view,summary_review,mcs_view}.py`、`mcs/semantic/{semantic_llm,semantic_facts,semantic_extraction,semantic_projection,semantic_loops,semantic_drain,semantic_policy,semantic_send_gate,semantic_bench,semantic_blind,semantic_observe}.py`、`mcs/notify/notify_flush.py`、`mcs/ops/{request_loops,mcs_setup}.py`、`evaluation/`、`docs/specs/{semantic-facts-v2-rollout,semantic-evaluation}.md`。

## 進め方

```
今すぐ   20-E1 assist 化（設定のみ）+ semantic_observe の日次観測開始
   ↓
20-A 評価ケース → 20-B requests 項目 → 20-C 返信種別      ← 利用者に届く経路。設定不要
   ↓
20-E2 calibration → enforce（semantic_notice 配送開始）    ← 20-A〜C と並行可
   ↓
20-D canonical の項目追従                                  ← canonical 切替の前提
   ↓
20-E3 人手ラベル 200 件 → G6 → fact-source canonical
```

A〜C は extract_llm と読み側だけを触り、shadow/enforce の canonical・Loop には影響しない。モデル変更・fine-tuning・全投稿への追加推論は、A〜C の実測で残る誤りの種類を見てから別計画で比較する。

### 20-E1 assist 化と観測開始（設定のみ・今すぐ）

- `python3 mcs/ops/mcs_setup.py init --set semantic.summary_mode=assist --set semantic.loop_mode=assist` を実行（オーナー実行。`check` が fail-closed で検証）。`semantic.mode` は shadow のまま（enforce は E2）。
- 効果: 以後の要約候補が `summary_review` で採用可能になり、Loop 候補は従来通り閲覧・人手採用できる。通知・外部送信・Jev 予算は変わらない。
- `python3 mcs/semantic/semantic_observe.py --json` を日次で追記し、PASS 率・failed 数・Jev 使用量・pending 消化率を E2 の判断材料にする。
- **完了条件**: 設定反映と `check` 合格、観測ログの開始。

### 20-A 依頼の評価ケースと基準測定（S）

- `evaluation/extract_cases.json` に依頼向けの完全合成ケースを約 15 件追加（実装は 17 件）。層: 一投稿の複数依頼、間接依頼、自己予定、条件付き、相対期限、否定・取消、引用転載された旧依頼、家族への依頼、依頼なしの報告・挨拶、返信（受諾・途中経過・回答・完了報告・「ありがとうございます」だけ）。
- `extract_bench._match_request` に `kind`・`condition`・`due_text` の照合と、`reply` の照合を追加（expected にある項目だけ照合する現行の流儀）。
- 現行プロンプトでローカル LLM の基準を測る（承認済み範囲。mock 成功は精度にしない）。
- 人手ラベル（20-E3）の記入項目に、依頼の to/from/kind/condition/due_text と返信種別を加える。G6 の facts/loops ラベルと同じ worksheet で一度に記入し、実データでの依頼精度を G6 と同じ 200 件で測る。
- **完了条件**: ケース・照合・基準 JSON が揃う。実モデル未評価なら「未測定」のまま。

### 20-B extract_llm `requests` の項目強化（S〜M）

- スキーマに任意キーを追加: `kind`: `request|question|self_plan`（3 値。explicit/indirect の区別は読み手が使わないので統合）、`condition`: 条件の原文 or null（60 字上限、対象本文に完全一致で見つからなければキーごと落とす）。`action` はプロンプト側を 30 字以内に緩め、保存上限 60 字はそのまま。`evidence` をプロンプトの requests 行に加えて必須にする（現状は要求しておらず全件 unverified）。to/from/due/due_text は現行のまま。`from` は本文に依頼者が明記された場合のみ、無ければ null。
- プロンプト規則: 一投稿の別行動は別項目。挨拶・既に完了した報告・単なる出来事は requests にしない。「〜してください」「〜していただけますか」はいずれも `request`、「〜します／次回確認します」は `self_plan`、答えを求める問いは `question`。条件付きは `condition` に原文。参考コンテキストや引用転載の旧依頼を対象投稿の依頼にしない。few-shot は既存例 2 に `kind` を付け、複数依頼と条件付きの合成例を 1 件追加。
- `_Validator.requests` に `kind` の enum 検査（不正・欠落はキーを省くだけで項目は残す）と `condition` の 60 字上限 + 本文照合。`response_format` の schema にも追加。`extract_qc` は `requests` を走査しないので新項目は Jev に送られない（上表「品質」）。
- `EXTRACT_VERSION` は上げない（新着から適用。旧 artifact は `kind` 無し＝表示は従来通り）。全量再抽出は #15-B・#4 の再生成窓に合わせる場合だけ。
- 読み側: `structured_view._request_lines` の既存「依頼:」行の中で項目ごとに `self_plan` は「予定:」、`question` は「確認依頼:」を前置し（別行にしない）、`due` が null なら `due_text` を `(期限:…)` に出し、`condition` は `(条件:…)` を 20 字で付記（action 30 字・3 件上限は据え置き）。`rollup.recent_requests` に新キー `req_kind`/`condition`/`due_text` を運ぶ（`kind`=to など既存キー不変、`PERIOD_CHECK_VERSION` 据え置き）。`mcs_signals` の `_pharmacist_request`/`_rx_request_visibility` は `kind` が `self_plan`/`question` の項目を除外する（薬剤師自身の予定・確認質問で `pharmacist_request_unanswered`/`rx_request_visibility` を鳴らさない。`kind` 無しの旧 artifact は従来通り）。
- **完了条件**: 合成 test で複数依頼の分割・kind・condition の正解率と誤候補数を報告。既存 med/symptom/negation 指標が下がらない。通知文の上限内。

### 20-C 返信の種別（S〜M）

- extract_llm に `reply` を追加（対象投稿が返信で、先行投稿の依頼・質問に応答している場合のみ）: `{"kind": "ack|intent|progress|answer|done|cancel", "evidence": 対象本文の引用}`。応答先の特定は文脈を参照してよいが、出力は種別と対象本文の根拠だけ。
- 規則: 「承知しました」= ack、「対応します」= intent、「一部確認しました」= progress、質問への答え = answer、「提出しました」= done、「不要になりました」= cancel。「ありがとうございます」だけは reply にしない。answer は question への応答であり、実施依頼を閉じない。
- 導出: `rollup.build_rollup` でスレッド単位（root = `parent_id` or `message_id`）に、依頼より後（`posted_at_ts` が大きい。NULL は 0 扱いで「後」にならない）かつ送信者が依頼者と異なる投稿の `reply.kind` を段階順（ack < intent < progress < answer < done < cancel）で最強のものに集約し、そのスレッドの LLM 依頼行すべてに `reply_state`（kind 文字列）として付ける。done の後に cancel があれば `reply_conflict: true`。依頼単位の対応付けはしない（`ponytail:` thread 単位が上限。依頼単位は Loop 経路）。**閲覧のみ**（`#20-D2` 初期案どおり）: カード・通知・digest には出さず、`mcs_requests` への書込みも状態遷移もしない。「完了」の語はこのキーから描画しない。正式完了ではない。
- `semantic_loops` の判定文修正は保留（下記「保留」）。
- **完了条件**: 合成 test で ack/answer/done の混同行列（`extract_bench` の `reply_confusion`）を報告し、誤 done は 0 件。root 投稿（文脈なし）では `reply` を出さない。既存の Loop テストが通る。

### 20-E2 calibration → enforce（設定 + オーナー判断）

- 前提観測（20-E1 のログ）: 現行世代の監査 PASS 率が安定、failed job 0、Jev 日次使用量が予算の余裕を持つ、pending 消化率 ≥ 新着率。閾値の数値はオーナーが観測値を見て決める（`#20-D4`）。
- `python3 mcs/semantic/semantic_bench.py calibrate`（実 DB 読み取り専用、オーナー実行）で claim 信頼度分布と match_threshold の what-if を確認し、必要なら `match_threshold`/`nomatch_threshold` を決める。
- 設定: `--set semantic.threshold_mode=calibrated --set semantic.calibration_version="2026-MM-DD.1"` → `check` 合格 → `--set semantic.mode=enforce --set semantic.summary_mode=enforce --set semantic.loop_mode=enforce`。
- 効果: 監査 PASS の要約が `semantic_notice` として配送され、degraded notice が出る。生の新着通知は semantic 処理を待たない（既存設計）。`calibration_version` 変更で現行結果が非 current になり再評価が走るため、切替直後の Jev 使用量と滞留を観測する。
- 戻し: `semantic.mode=shadow` に戻すだけで配送が止まる（`semantic_gate` が deferred/freeze）。
- **完了条件**: 切替後 1 週間の `semantic_observe` で failed 0・予算内・通知の誤配なし。誤りがあれば shadow へ戻して原因を記録する。

### 20-D canonical 並走の項目追従（M。canonical 切替の前提）

- `_FACT_V2_PROMPT` の `request_pending` に任意項目 `request_to`・`request_from`・`due_text`・`condition`・`request_kind` を追加。`validate_fact` は省略時 `unknown` で受理（旧 doc 互換）。`_normalise_facts_v2` で運び、追加項目が evidence_quote に含まれない場合は fact を消さず項目だけ `unknown` に落とす。
- `project_v2_facts` で `assignee_text`/`time_text` を、`project_v2_doc_legacy` で to/from/due_text/kind/condition を投影する。Loop 候補と canonical 由来の「依頼:」行が extract_llm と同じ項目を持つ。
- cache 無効化は `SCHEMA_VERSION_V2` の bump だけで行う（policy fingerprint には入れない。fingerprint 変更は現行結果を全て非 current にして再評価の波を起こす）。
- 前提（着手前に決める）: (1) `semantic_loops._candidate_identity` は `assignee_text`/`due_text` を identity に含むため、投影で値が入ると同じ open item が別候補として重複する — identity の扱いを先に直す。(2) `semantic_audit._fact_audit_target` は statement + kind/polarity/epistemic/workflow/event_time/subject（medication_event は action）だけを監査文にする — 新項目を監査対象に含めるか（Jev への送信文が変わる）含めないか（監査されない項目になる）を決める。(3) 20-A の基準測定と 20-B/C の本番 1 週間で kind enum を実データで確かめてから。
- 監査 S0〜S8・repair 一世代一回・PASS-only 公開は変えない。Jev への state.context に新項目は送らない。
- **完了条件**: shadow 比較で extract_llm と canonical の requests 項目一致率を報告し、退行がないことを 20-E3 の切替条件に入れる。

### 20-E3 人手ラベル → G6 → canonical（オーナー作業 + 設定）

- 対象: held-out test 200 件以上（患者・スレッドは dev/calibration と重複させない。`docs/specs/semantic-evaluation.md` の split 規則）。分割と期間はオーナーが決める（`#20-D4`）。
- 手順（既存ツールのみ）: 承認済み snapshot から `semantic_blind.py --snapshot … --generate-local-baseline` で worksheet を作る → `evaluation/annotation-guide.md` に沿って本人が記入（facts / claims / loops に加え、20-A の依頼・返信項目）→ `--unblind-key` → `--evaluation-records … --method audited` で結合 → `semantic_evaluation.py --criteria evaluation/g6-criteria-v1.json` → report。
- G6 PASS かつ 20-D 完了なら `python3 mcs/ops/mcs_setup.py fact-source canonical --gate-evidence report.json`。G6 不合格なら基準を下げず、不合格項目を 20-B〜D の次版で直して次の固定集合で再評価する。
- 同じ 200 件から依頼の適合率・再現率・項目正解率・返信の混同行列を報告し、合成ケースの結果と並記する。
- **完了条件**: token が pin され、カードの「依頼:」行が canonical 由来でも to/from/due_text を保つ。`docs/specs/semantic-facts-v2-rollout.md` の戻し手順（`fact-source shadow`）を確認済み。

### 保留（A〜E の実測で必要と分かった場合だけ）

`request_detail` 派生 artifact と専用 repair・検証 receipt、prefix 評価 harness と合成 100 スレッド、Jev 判定の対象照合の全面改訂、既存 40 ペア上限の変更、モデル変更・fine-tuning。

`semantic_loops` の判定文（「同じ薬剤・行動・期間」→「同じ対象・行動・期間」）の 1 行修正も保留。理由: 判定文は policy fingerprint にも `seen_pairs` にも入らないため、修正後は旧文で出た判定と新文で出た判定が世代 marker なしに混在する。loop_mode は shadow で利用者への影響がなく、薬剤以外の誤判定も未観測。E1 の Loop 観測で薬剤以外の取りこぼしが見えた場合だけ、meta に世代 marker を付けた単独 commit で行う。

## 容量ゲート（2026-09-30 追加）

本機は M4 Mac mini 16 GB、Qwen3.5-9B Q4 常駐 6 GB。実測: 新着は平均 15 件/日（最多 33）、extract_llm は 17 秒/件、semantic（shadow: legacy + v2 + 要約）は中央値 393 秒/件・上位 10% 1,058 秒・平均 2.5 pass。生成速度は単独 12.8 t/s、2 slot 同時 3〜5 t/s。日中（drainer 窓 20-07 の外）に使えるのは 13 時間。

| 段 | 見積もり | 判定 |
|---|---|---|
| 20-A/B/C | 出力 1〜2 割増、1 日数分 | 制約なし |
| 20-D/E2 | 平均日 1.6〜3.6 時間（日中の 1〜3 割）。canonical 化で legacy 抽出が消え 2〜3 割減 | 可能 |
| 20-E3 新着 | 同上 | 可能 |
| 20-E3 履歴 | 17,978 件 × 393 秒 ≈ 78 日分の LLM 時間 | 不可。新着から・bounded cohort のみ |

**切替条件（E2・E3 共通）**: 直近 2 週間で (1) semantic の LLM 時間が 1 日平均で日中の 50%（6.5 時間）以下、(2) job の LLM 時間 p90 が 900 秒以下、(3) tick の semantic 実行が realtime slot 混雑で skip される頻度が週 1 回以下。算出元: (1) は `semantic_drain_run` artifact の `job_metrics.llm_s` の合計（`semantic_observe --json --days 14` の `recent_drain.llm_s`。`created_at` で 14 日窓）、(2) は同 `llm_s_p90`（job ごとの `job_metrics.llm_s` の線形補間 percentile — `semantic_evaluation._percentile`。線形補間なので ceil-rank と一致せず上下どちらにもずれうる）、(3) は tick ログの JSON から `"skipped": "slot_busy"` を数える（`grep -c '"skipped": "slot_busy"' data/run.log`。旧 starvation-hold 機構 `semantic_lane.hold_until` は 3 スロット再編で廃止 — realtime lane は busy 時に即 skip し常駐 worker が backlog を継続する）。**オーナー注意**: `maintenance.rotate_log` は >5MB で 1 世代（`.1`）しか残さず日数保証がないため、2 週間の読み出し前にログの保持期間を確認する。超えた場合は切替を進めず、下記の削減策を先に入れる。

**削減策（効果順）**: canonical 切替で legacy `_FACT_PROMPT` を落とす（-25%）→ v2 抽出に `response_format` の JSON schema を使い冗長な出力を削る → 長文だけ job 予算を上げる → モデル軽量化（最後）。検討して見送り（2026-09-30）: preflight「全カテゴリ absent」の chunk で v2 生成を省く案は、了解文の生成が元々短く（約 20 秒、1 日 1.5 分相当）節約が小さい一方、Jev の誤判定で fact を取りこぼす経路になるため採用しない。

## 評価と昇格

| 観点 | 報告・判定 |
|---|---|
| 依頼の検出 | 種別別の適合率・再現率。陰性例で出た誤候補を分母に入れる。合成開発目標は固定コーパスで誤り 0 件（件数併記。95% 目標は実装判断で置換）。実データは 20-E3 の 200 件で報告 |
| 項目 | to/from/action/due_text/condition の項目別正解率、不明を不明のまま保つ率。担当・期限・行動の捏造 0、根拠なし項目 0 |
| 返信 | ack/intent/progress/answer/done/cancel の混同行列。誤 done 0、「ありがとう」だけの done 0 |
| 既存機能 | 薬剤・否定・時制の固定コーパスで非劣化。G6 基準は下げない |
| on 化 | E2: 切替後の failed 0・予算内・誤配なし。E3: G6 PASS + 20-D 完了。いずれも戻し手順を先に確認 |
| 運用 | 抽出の token・p50/p95・失敗率、Jev 日次使用量、最古 job age を前後で比較し、収集・通知の予算を維持 |

実装時は `scripts/run_tests.sh tests/extract/ tests/semantic/ tests/views/` と CI 範囲の ruff、README 生成 drift、`ci/gates.py`、`ci/mine_gates.py --check` を維持する。テストは一時 DB・stub・完全合成のみ。設定変更・実 DB の calibrate・snapshot からの worksheet 生成はオーナー実行。

## 残る判断・依存

- `#20-D1`: 初期対象業務と評価ラベル。初期案は薬剤関連の依頼・確認質問・残薬確認・書類提出・連絡に限定。20-A の前。
- `#20-D2`: `reply_state` をカード（通知）にも出すか、rollup と閲覧だけにするか。初期案は閲覧のみで、20-C はその前提で実装する（カードへ出すなら別判断）。
- `#20-D3`: E2（enforce）と E3（canonical）の切替時期。観測値と G6 レポートに基づく。
- `#20-D4`: 人手ラベル 200 件の分割・期間・記入者と、E2 の前提観測の閾値。E1 の観測開始後。
- #11 の薬剤正規化は照合精度の補助で、必須依存にしない。#16 OCR・処方薬剤歴は後続入力候補。C1 の `mcs-read-model/1` allowlist は変えない。Q2「semantic 層を常時動かす」は E2/E3 で満たす。
- 規模: E1 設定のみ、20-A S、20-B S〜M、20-C S〜M、E2 設定 + 観測 1 週間、20-D M、E3 は人手ラベル期間に依存。

## 今回の調査範囲と未検証事項

extract_llm の prompt・validator・文脈・QC・bench、structured_view と rollup の依頼表示、canonical の prompt・validator・normalise・projection、Loop 候補と閲覧・採用資格、semantic policy と drain の mode/fact_source 分岐、send gate と notify の enforce ゲート、summary_review の assist 条件、calibrate・blind・observe・fact-source の各 CLI を実ソースと docs で確認した。稼働構成は `config.json` の enum/bool 値だけを読んだ。実データの依頼頻度・実モデル推論・通知の実送信・設定変更は未実施。文書のみの変更。
