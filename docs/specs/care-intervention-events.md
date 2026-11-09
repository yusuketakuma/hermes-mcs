# 介護・医療介入イベントの記録契約

職種×事業所×判明したサービス単位の開始・中断・再開・終了などの記録を、原文と参加者情報へ結び、患者コンテキストの全体像・経過タイムライン・訪問前ノートから辿れるようにする。公開推論エンジン 1.0 は維持する。新しい状態 DB・所属マスタ・外部検索・依存・queue/cache・既定課金は作らず、既存 record と read projection を再利用する。

## 1. 固定 detail schema

既存 `event_records` の各項目へ optional の `care_detail` を 1 件まで追加する。legacy `EVENT_KINDS`（11 種）は変更しない。粗分類に適合しない detail は `kind: "other"` で運び、既存 `_DIRECT` cue だけに依存させない。

| フィールド | 契約 |
|---|---|
| `event_type` | 下記 17 種のいずれか。列挙正本は `clinical_events.CARE_DETAIL_TYPES` |
| `workflow_status` | 既存 semantic 軸 `semantic_facts.WORKFLOW_STATUSES` を共用。イベント自身の `status`（current/past/planned/conditional/unknown）とは別軸。二重に食い違う場合は `unknown` |
| `date_text` | 原文の日付表現をそのまま保持。正規化・補完しない |
| `service_actor` | 明示された提供者・実施者の原文候補。名簿 ID・hash・verified は LLM が出さない。結合はコード側が行う（§3） |

`verify_care_detail` は `clinical_events` の既存検証経路へ集約する。`event_type` の採用には `CARE_DETAIL_CUES` の種別ごとの手がかり語彙が evidence quote 内に出ることを必須とし、語彙共起だけではイベントとしない：契約開始は提供開始と区別し（`_DETAIL_VETOES` で介入系 cue 内の「契約」を拒否）、短期入所は施設入所と区別し、共同指導は退院時に限定し、DNAR 単独は ACP・方針変更の根拠にしない。名詞 cue は自分のイベント述語に束縛される必要がある（`_detail_event_target`：cue の bind 助詞の後はイベント名詞・助詞・話題 marker だけを辿る spine（`_DETAIL_SPINE`）の内部で述語を照合し、句または別行為の目的語を跨がない：会議の開催・実施、指導の受領・実施、話合い・方針の確認等）。同じ文にあるだけの汎用述語や、資料作成・説明・計画の述語では採用しない。述語を持つ動詞 cue は、サービス語と動詞の間の gap（`_GAP`）が文末・イベント完結語・別行為の目的語を跨げないため 1 match = 1 実例となり、「訪問看護について資料作成を開始」のような別行為の述語へは結び付かない。`date_text`/`service_actor` は cue を含む文の内部に逐語で出て、かつ文内の全 detail cue のうち最近接する述語が当該 `event_type` のものである場合だけ採用する（`_attribute_bound`）。採用にはさらに `_detail_shared_instance` が要求される：検証済み anchor（kind=other は detail cue 自身、legacy kind は `_DIRECT` の一致）の文が predicate 束縛済みの同種実例をちょうど 1 つだけ持ち、明示された全属性がその同じ文の内部でのみ束縛されること。別の文の同種実例へだけ結び付く日付・主体の組合せ（交差組）は証明できないため detail を採用しない。属性の無い detail は anchor の実例をそのまま記述する。同一文に同種イベントが複数ある場合はどの実例の属性か証明できないため採用しない。別イベントの述語へ結び付く日付・主体は採用せず、「から/より」で出典・紹介元として示された組織・人物は `service_actor` にしない。`workflow_status` は record/fact の status・negated・workflow_status と家族（予定系/実施済み系）が食い違う場合 `unknown` へ落とし、予定・否定の記録と肯定実施済みを区別する。`kind:"other"` の record は `_DIRECT` に依存せず detail cue を anchor に同じ軸検証を受ける。本人/否定/予定/条件/引用/訂正/伝聞/対象サービスの確認は既存の span・極性・主体・status 検証が担う。detail の検証に旧 `_isolated_assertion`（単一文・全体が安全な場合だけ許可）は使わず、単文・複数文とも全文文脈を検証した場合だけ受理する。複数文では訂正・否定・主体の前後文脈を省略しない。timeline 側は全イベント共通で `_isolated_assertion` を `clinical_events.context_clean` へ置き換える。検査は 2 層：quote が触れる文と前後 1 文ずつの局所文脈に不確実・引用符・局所の訂正/伝聞がないこと（引用・伝聞の近接確認）、かつ bounded body 全体（`_BODY_LIMIT` まで）に訂正・取り消し・第三者枠・伝聞 marker がなく（`_GLOBAL_CONTEXT` 全文検査）、同一主体・同一相の対立極性言及が本文の候補走査全体にないことを要求する。候補走査が `_EVENT_CANDIDATE_LIMIT` を超えた場合は fail-closed とする（`kind:"other"` は detail cue の service head が一致する言及のみ比較）。detail 競合は request 詳細と同様に保持し、同 fact ID の first/last wins・3 件目で復活は禁止。

## 2. event_type 17 種と語彙対応表

検索用の同義語と、採用に必要な文脈検証は分ける。表の語彙は一次資料で確認した区別の根拠であり、個々の投稿の事実・法的同意・報酬算定を保証しない。資料中の個人名・事例は fixture へ転用せず完全合成とする。

| event_type | 意味 | 採用の文脈条件（区別） | 一次資料 |
|---|---|---|---|
| `intervention_start` | 継続的なサービス・支援の開始 | 「契約開始」「単発の訪問実施」「予定」と区別。開始の肯定・実施済み記述のみ | S1, S2, S6 |
| `intervention_pause` | 支援の中断（一時休止） | 単発訪問の中止・キャンセルと区別。LIFE の「利用中断」に相当。30 日等の加算提出条件は状態遷移へ転用しない | S6, S7 問14 |
| `intervention_resume` | 中断後の再開 | 本人・対象サービスが明示された実施済み「再開」の肯定は、中断記録が未取得・不在でもイベントとして保持する。再開の根拠と中断履歴・現在継続状態の証明は分離し、未知履歴から現在継続や過去の中断日時を補完しない。予定・否定・第三者の再開は区別 | S6, S7 問14 |
| `intervention_end` | 支援の終了 | 「終了したときの理由分類」（軽快/施設等/医療機関/死亡/その他）は理由であり「入院なら全支援終了」の含意ではない。契約終了・会議・入所・死亡から自動生成しない | S3, S6 |
| `service_contract_start` | サービス契約の開始（契約手続） | 説明・同意・契約成立とサービス提供開始は別イベント。チャット記録を法的有効性の確定として扱わない。S7 問24 は提供開始前の説明同意の根拠に限定し「契約開始そのもの」の根拠を過大に担わせない | S7 問24, S10 |
| `pre_discharge_conference` | 退院前カンファレンス・退院前会議 | 「退院」と「退院前の会議」を区別。会議・情報共有の開催記録であり、退院実施や退院時共同指導の実施と自動同一視しない。S11 は語彙・場面の根拠に限定し、2024 年の算定値・条件を現行判断へ移植しない | S1 第4表, S11 |
| `discharge_joint_guidance` | 退院時共同指導 | 退院時共同指導と初回訪問を区別。指導の実施記録 | S2 |
| `care_team_conference` | サービス担当者会議 | S1 第4表「サービス担当者会議の要点」の構成（日時/所属・職種/検討/結論/残課題）を参考。「担当者会議」一般も同型だが、開催記録と介入状態を混同しない | S1 第4表 |
| `care_plan_change` | ケアプラン・計画の変更 | S1 第5表「居宅介護支援経過」の計画変更記録。変更の決定と実施を区別 | S1 第5表 |
| `monitoring` | モニタリング（計画の評価・見直し） | S1 第5表「居宅介護支援経過」に含まれるモニタリング・目標達成・調整の記録。第5表自体を「モニタリング」とは呼ばない。算定要件の充足を断定しない | S1 第5表 |
| `care_need_certification_change` | 介護度・要介護認定の変更・区分変更 | 認定/区分欄の記録。申請中・更新予定と認定結果を区別 | S1 認定/区分欄 |
| `respite` | レスパイト（介護者の休息目的の短期利用） | 死亡・全支援終了ではない。医療型/生活介護型を同一視しない。期間限定の入所で長期入所と区別 | S4, S5 |
| `facility_entry` | 施設への入所（長期含む） | 医療機関への入院（legacy `admission`）と介護施設への入所を区別。短期入所（`respite`）と区別 | S4 |
| `facility_exit` | 施設からの退所 | `facility_entry` と対。退所理由の推測をしない | S4 |
| `death` | 死亡の記録 | 看取り・終末期（legacy `eol`）と死亡を区別。ACP 言及から死亡を導かない。S3 の終了理由分類の一つ | S3, S8 |
| `acp_discussion` | ACP・人生会議等の話合いの記録 | 本人/家族/多職種の話合い・意向の変化・都度の記録共有。ACP 言及から DNAR・死亡・治療中止は導かない | S8 |
| `care_policy_change` | 療養方針・治療方針の変更 | 意向・方針の変更記録。方針変更と実施済み治療変更を区別 | S8 |

### 誤採用の禁止

- generic な「開始」「終了」（薬剤の開始/中止、事業所の休業、会議の開始/終了）は患者への介入イベントへ採用しない。
- 「契約開始」と「訪問・サービス開始」（`service_contract_start` と `intervention_start`/legacy `visit`）を混同しない。
- 看取り・終末期（legacy `eol`）と死亡（`death`）を混同しない。
- 単発訪問の中止・キャンセルと継続介入の中断（`intervention_pause`）を混同しない。
- 入院（医療機関、legacy `admission`）・退院・転院・訪問・受診等の legacy 種別は保持し、同じ timeline へ載せる。

### 一次資料一覧（2026-11-09 TinyFish 確認済み）

- S1 居宅サービス計画書標準様式・記載要領 https://www.mhlw.go.jp/content/000764680.pdf：第4表=サービス担当者会議の要点、第5表=居宅介護支援経過（モニタリング・目標達成・調整・計画変更と時系列・情報源を含む）、認定/区分欄
- S2 2026 訪問看護留意事項 https://www.mhlw.go.jp/content/12400000/001686845.pdf：利用開始、退院時共同指導と初回訪問の区別、複数事業所の連携
- S3 2026 訪問看護請求記載要領 https://www.mhlw.go.jp/content/12400000/001754612.pdf：訪問終了の状況（軽快/施設等/医療機関/死亡/その他）と死亡年月日等を別記
- S4 短期入所生活介護 https://www.kaigokensaku.mhlw.go.jp/publish/group12.html / 短期入所療養介護 https://www.kaigokensaku.mhlw.go.jp/publish/group13.html：期間限定の入所、医療/生活介護の区別
- S5 医療型ショートステイガイド冒頭 https://www.mhlw.go.jp/content/12200000/000654272.pdf：レスパイト（介護者の休息）、地域移行/緊急時支援
- S6 LIFE 説明会 https://www.mhlw.go.jp/content/12301000/001470350.pdf：「利用中断」「再開」「終了」の区別。30 日等は対象加算の提出条件
- S7 2021 介護 Q&A vol952 問14/24 https://www.mhlw.go.jp/content/000763822.pdf：リハ利用の中断後再開、提供開始前の説明同意
- S8 ACP 決定プロセス解説 https://www.mhlw.go.jp/file/06-Seisakujouhou-10800000-Iseikyoku/0000197722.pdf：本人/家族/多職種の話合い、意向の変化、都度の記録共有
- S9 居宅療養管理指導概況 https://www.mhlw.go.jp/content/12300000/001123921.pdf：医師/歯科医師/薬剤師/栄養/歯科衛生等、職種とサービスの区別
- S10 介護予防サービス解釈通知（2026-03-13 改正） https://www.mhlw.go.jp/content/001673837.pdf：説明/同意/契約成立とサービス提供の区別
- S11 令和6年度診療報酬改定の概要【同時改定】 https://www.mhlw.go.jp/content/12400000/001252073.pdf：退院前カンファレンス・退院前カンファレンスにおける情報共有の語彙と場面。2024 年の算定値・条件は現行判断へ移植しない

## 3. 参加者と発言の結合

- 同 project の `sender_id`+`type` を care_team 名簿の 1 行へ結ぶ。名前一致・チャット参加だけを帰属根拠にしない。
- 発言者リンクと event のサービス提供者を分離する。明示提供者は述語との結合で検証し、「A から B へ移行して B 開始」で A にしない。
- 真の自己参照（当薬局等）のみ roster を提供者候補に使う。引用の「当院」・第三者から聞いた訪問看護 A・家族の出来事は、投稿者の事業所へ転用しない。
- 名簿は `get_project_metadata(..., care_team_as_of=None)` の既存 selector 拡張で取得する。cutoff 以前の latest attempt と latest complete を別々に選択し、failed/malformed/nonfinite を飛ばして旧 complete を確定へ戻さない。省略 caller は旧挙動。行数/bytes/deadline を維持する。
- roster は取得時点の観測で在籍期間の完全証明ではない。回顧投稿へ現在所属を逆投影しない。後日名簿は `observation_link_only` として表示可。
- 組織 ID がない現状では根拠付き施設ラベルを使う。同名・改称・カンマ複数所属を勝手に統合/分割しない。row 証拠 artifact・行 hash・観測時刻を局所束縛し、無関係な別メンバー変更で全件失効させない。

## 4. canonical 証拠と患者 context 統合

- `care_event_sources` の canonical 一律 `unconfirmed` を単に解除しない。借用 read snapshot 内で原文/revision/source_binding/患者 scope/権限/現契約/QC/bundle fp/保存 doc hash/audit PASS/投影一致/provider 証拠を既存共通経路で確認する。成功 fixture と各 1 要素損傷 negative を必須とする。
- canonical が存在するのに良好な旧 local へ fallback しない。新 detail へ旧 audit を流用せず、内部 contract/hash は候補固定時に正直に更新し、public 1.0 は不変。
- 読取経路は `semantic_extraction.rebuild_facts_v2_cached`（no model/preflight dispatch・writes・TX ownership の既存関数）、`semantic_publication.reproject_doc`、`_member_bundle`、`_projection_provenance` を再利用し、新しい resolver を乱立させない。実 call graph と synthetic no-write/no-call で証明する。
- `views.patient_context_view`/`notify_views` の既存「経過タイムライン」を拡張し、care_events 別表示と同じ原資料 projection を共有する。「患者さんの全体像」に事業所別『記録上の介入状況』と timeline 導線、previsit にも関連直近イベントを出す。timeline は既存背景/OCR 資料等を落とさず source identity で重複整理する。元 MCS リンクは既存 `project_url`/投稿参照 helper を使い、未実証 deep link を発明しない。本人専用・スコープ・全 transport 制限は維持する。
- 閲覧は LLM 呼出・概況 4 回枠消費・DB 書込をしない。概要生成で利用する場合は同じ検証済み事実を既存 source/facts 依存に含め、表示のための読取と推論採用を混同しない。取得済み有限範囲・未解析・日付未確認を明示し、上限を理由に全履歴と称さない。既存ページング/検索を活用し古いイベントへの到達経路を残す。
- event 日時と posted 日時は別。event 時刻で順序が証明できる場合だけ最終記録状態を導出する。日付不明・同日順序不明・遡及訂正は `unknown`/競合とし、投稿順勝ちにしない。履歴未完時は「最後に確認できた開始記録」等とし、現在継続を断定しない。

## 5. 状態の読取時導出

状態は患者×職種×事業所×判明サービスから読取時に導出し、永続状態表は作らない。原文で肯定・実施済みの開始/中断/再開/終了だけを反映する。契約・会議・入院・レスパイト・入所・死亡・参加者退出から自動で介入終了等を生成しない。未知サービスの終了は既知全サービスへ適用せず、関係し得る scope の不確実性に反映する。最後の根拠は保持する。訂正・削除・権限失効・ABA で古い根拠の現在採用を拒否し、paid unknown/履歴は保持する。

## 6. readonly 証拠 callgraph

閲覧・投影は書込みも LLM 呼出もしない。既存の読取経路は次のとおり。

```
patient_context_view.care_events(db, project_id, cfg)
  └─ SAVEPOINT care_event_view で読取範囲を囲み RELEASE（借用中の外側 TX は確定・巻戻ししない）
      └─ care_event_sources.collect(db, project_id, cfg=cfg)
          ├─ db.in_transaction 必須（借用 snapshot）・SELECT のみ
          ├─ 患者 scope/権限/fetch_state/履歴完全性の確認
          ├─ messages 本文 hash 再照合・本文上限・preflight 上限
          ├─ canonical artifact → _canonical_events（§4 の証拠連鎖を全て証明した場合のみ投影）
          │   └─ _projection_provenance（producer complete・repair replay・Jev done）
          └─ extract_llm artifact → care_event_timeline.project
              ├─ clinical_events.validate_record（span 一意・極性・主体・status）
              │   └─ verify_care_detail（CARE_DETAIL_CUES の quote 内照合・veto/述語必須・
              │       文内最近接述語への属性束縛・workflow 整合）
              │       └─ kind=other は detail cue を anchor に同一軸検証（§1）
              ├─ clinical_events.context_clean（quote が触れる文＋前後 1 文の局所検査と
              │   bounded body 全体の訂正/伝聞/第三者枠 marker・対立極性言及を検査。
              │   旧 _isolated_assertion を置換）
              └─ event_day / posted_at の分離・date_uncertain
                  （care_detail があれば kind を問わず検証済 date_text を
                  `_attribute_bound` と同規則で束縛確認し `written_day` の
                  ymd のみで導出。legacy の dated-event 導出と矛盾する
                  場合は日付を確定しない。年なし・相対表現・unknown は
                  日付を補完しない）
      └─ quote は検証済み span からのみ再構成（本文を返さない）
```

canonical 側の既存読取関数の実際の呼出関係と責務は次のとおり。

```
semantic_publication.reproject_doc(db, mid, meta)
  └─ current_artifact(semantic_facts_v2) で保存 doc を選択
      ├─ doc_hash 一致・coverage complete・published_quantity_findings
      └─ fact_audit_verdict(semantic_facts_audit) == PASS を確認して doc を返す
         （rebuild_facts_v2_cached も _member_bundle も呼ばない）

semantic_publication._projection_provenance(..., v2_doc, content)
  └─ 条件成立時のみ semantic_extraction.rebuild_facts_v2_cached(member, ...)
      （model/preflight dispatch・writes・TX ownership なし。member は引数で受ける）

semantic_store.thread_bundle + fingerprint + 対象 member 照合（_member_bundle 相当）
  └─ スレッド束縛と member 同定だけを行う。doc hash・audit PASS の検証関数ではない
```

新しい reader は上記の既存関数をこの責務どおりに組み合わせる計画であり、新しい resolver は作らない。`care_team_as_of` の名簿取得は本段階では未接続であり、§3 の設計上の拡張点に留まる。

`get_project_metadata` は「Reads ledger or snapshot only」で attempt/complete artifact を SELECT のみで読む。`care_team_as_of` 拡張も同じ SELECT 経路に留める。

## 7. 検証範囲

合成 batch の節目: 17 種各 positive を record→project の実経路で検証（subject/polarity/status/detail 添付を確認）＋予定/否定/家族/引用/単語衝突・資料/説明/計画の述語衝突 negative（会議資料作成≠会議開催・サービス資料作成開始≠提供開始を含む）、同種 2 実例の正しい属性組と交差組・実例を跨ぐ quote（validate_record/canonical_record/timeline 往復で detail 非添付を確認）、legacy kind への bound date_text による event_day（kind=other 以外でも適用・年なしは補完しない）、複数文/訂正/第三者提供者、全投影往復、旧形式、detail 競合順序不変、roster 後日/失敗/破損/複数/ID type、canonical 各証明損傷＋正常＋ABA、日付不明/遅延投稿/同日競合/他事業所非波及、context 全体像/timeline/MCS リンク/3 transport 表示/権限失効、no-write/no-LLM/quota 不消費/上限 partial。実 DB・API・LLM・実データは使わない。
