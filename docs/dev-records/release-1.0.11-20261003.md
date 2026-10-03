# 1.0.11 リリース準備・計画レビュー

2026-10-03。対象はローカル実装・整理・検証・リリース文書の準備。
専用worktree: `codex/release-1.0.11-20261003`（基点 `9ea015d`）。
元mainのユーザー差分（ROADMAPと新規計画3件）は保持した。
push・PR・tag・公開・gateway再起動は行っていない。
意図した配備はしていないが、専用worktreeへ移る前に定期処理が編集を読み、
原本DBのschema markerを8へ進めた事象があった。下記復旧記録を参照。

最新指示により、MCSの実画面検証は最後にユーザーが担当し、エージェントの操作は禁止。本人宛メンションも作成しない。今回エージェントは公式画面・投稿・スタンプ・しおり・ピンを操作していない。追加実機確認は終了し、[最終受入票](../development/ACCEPTANCE_1.0.11.md)に原全確認を保持する。

## 計画と実装の照合

ROADMAPと詳細計画10件を全件確認し、次を修正した。
- 第1層は件数と本人フラグだけで、医師の押下は判定できない。
- F-7の本人ID/返信ID修正を1.0.11へ揃え、本人と自局、所属/職種による既存signal契約を分けた。
- メンションは宛先であり返信の根拠ではない。スタンプから正式完了・担当を変更しない。
- 旧詳細計画のF-1/#13/#20/#21実装済み範囲と旧節参照を現行正本へ合わせた。
- C1の合成開発と本番投入を分け、#8移行/#4修復実施/#10レビューの依存を維持した。
- CD-9の職種だけで自局を断定する矛盾は未合意の訂正提案とし、既存wire/CD決定を変更しなかった。
- F-6の大文字Python検出を1.0.11で修正。既読化は手動だけでなく明示flag付き定期実行にも適用されると文書を修正した。

#1〜#29の実装済み・候補・未決・未割当・保留はROADMAPの全件表で追跡する。
後続版の押下者・ケアチーム・構造化薬歴・相談・MCSへのPOST・外部契約改訂は実装していない。
担当判断・実態調査・実APIゲートを省略して後続機能を先行実装しない。

## 1.0.11の実装範囲

既存GETの付随metadataだけをcaptureとして保存・表示する。
本文hash・通知source generation・semantic・既読化・coverage・外部exportの契約は維持。
加法schema 8、旧snapshot読み取り、バックアップ/復旧の検証を追加した。
unknown種別は保存し、表示時は「未知」とする。欠落/不正/0件/観測日時を区別する。
本人IDは名簿is_selfで一意解決、曖昧時は不明扱い。reply_stateは氏名で判定しない。
CLIは名簿IDによる自局判定をbool/nullで返し、既存signalの所属/職種による応答候補と区別する。

第1層metadata refreshは定期tick/deep runの`metadata_shadow: true`と手動
`--metadata-shadow`の共通経路を実装し、既定off、実機適用なし。
#22-D1のローカル実装設計値として5件/回・最大25秒・全体期限余白30秒・
成功後30分/失敗後6時間を選んだ。本文収集・配送・semanticの後に保存だけ行う。
7日分の本人root・現行世代未確認カード・最新open薬剤師宛候補を、古い観測から選ぶ。
cap超過をdue/deferredへ開示し、欠落応答でもchecked_atを記録して飢餓を防ぐ。
認証切れ・期限切れでは後続optional GETを停止し、全体deadlineを復元する。
shadowの反応値をカード/digestへ出さず、CLIにはcaptureの鮮度とshadow状態/日時だけを返す。
healthは件数・予算・延期理由に限定して患者/投稿IDを含めない。
未読保持を含む実API確認後にshadowを有効化・照合する。
既定offを理由に、原1.0.11の第1層shadow受入を第0層だけへ縮小しない。

## 原1.0.11要求と未達の追跡

開始時ROADMAP §5とstamps §5の原要求を再照合した。後続版のAPI先行確認も
22-Aの一括確認に保持し、機能実装の版割当を理由に確認を省略しない。
2026-10-03の最新ユーザー判断「元の全確認を1.0.11に残す」に従い、
押下者49/50/51/100/101件の合成境界確認も含め、未達を延期・省略しない。
当初の専用投稿限定と通常患者調査の別承認待ちは、ユーザーの追加指示で解消している。

| 原要求 | ローカル実装・確認した証拠 | 残る受入 |
|---|---|---|
| 22-A: 投稿オブジェクト・本人フラグ・GET非変更・再取得・返信/自投稿・session | 履歴include_meta有/無、1件/一括、押下者、本人positive/negative ID照合、本人/他者×root/返信4区分、no_extend指定受理と期限切れ処理を確認 | 未読true保持、viewed自動付与、count 0省略規則等は未実証。権限不足の実応答は判定不能。実時間のsession非延長効果は原session行への追加必須条件にしない |
| 22-A: 横断取得・押下者paging・更新検出・#26/#27件数の先行確認 | 下記および[API調査 §7](../roadmap/mcs-api-survey.md#7-22-a実確認と残る受入2026-10-03)に全項目を記録。静的な実2ページ取得・集合不変は確認済み | 非空横断応答・更新中paging・updated対照・相談利用有無は未確定。後続機能の実装とは区別 |
| F-7: 本人/自局ID・profile補完・reply_state | 同名別人とID不明を区別する回帰、CLI本人/自局判定、名簿唯一IDと押下者一覧によるpositive/negative対照一致 | 最新差分の最終検証。既存signalの所属/職種契約は維持 |
| #22第0層: 加法保存・hash独立・0件/未取得・鮮度 | schema8/capture、旧snapshot、未知/不正値・再起動・復旧・CLI観測鮮度の回帰 | 実API省略規則の確定と最新差分の最終検証 |
| #22第1層: 監視集合更新・bounded・shadow・状態/鮮度/予算 | 定期/手動共通経路、D1設計値、CLI独立shadow状態、safe health、semantic不変・選択境界・周期・deadline回帰 | 未読保持の実証後の実shadow更新・照合。未適用・未実行を完了扱いにしない |
| #23解析 | mentions/しおり/ピンの最小ID/boolを保存。履歴のreactions/mentions listを確認 | しおり/ピンの存在時の型。表示/横断取り込みの実装は1.0.12 |
| 活用: 脚注・未確認一覧・自投稿・digest | captureだけを投影、反応済みカードを同患者末尾へ、自投稿ID表示、観測窓集計 | 最新差分での配送回帰・Hermes/独立同一表示の最終確認 |
| 合成fixture・changes・README/画面例・更新手順・F-6/F-8 | 合成回帰・変更記録・画面例・人承認schema更新手順、Python検出・定期既読化文書の修正 | 最終runner/SDK/gates/同期とexact SHAのCIを記録。公開・配備は別工程 |
| 共通: 収集/抽出/既読化/Slack配送/人承認の回帰なし | hash・coverage・source generation・外部exportを維持する実装と回帰 | 実API・合成・固定SDK・GitHub CI・配備の証拠を混同しない |

「第1層候補のみ」「限定した公開なら原1.0.11完了」という定義は採らない。
未達表の受入と原計画の全確認票を維持し、実装済み・調査済み・未実証を分ける。

## 実API確認（合成テストと別工程）

ユーザーは読み取り確認・任意の対象選定・通常患者ルームの読取りを許可し、専用テスト投稿限定を解除した。
セッション期限切れ後、ユーザー指示により既存auto_loginで復旧（result=ok）。
実cacheExpired応答・既存復旧経路と、合成の期限切れ後optional GET打切りを確認した。
本文・氏名・秘密値・患者/投稿ID・生応答はこの記録やfixtureへ保存していない。
認証キャッシュ更新は既存復旧経路内で行い、POST投稿/押下/取消/明示既読化は行っていない。

1件再取得はtarget ID一致。`reactions[]`のtype/count/self_reactedはstr/int/bool、
`mentions[]`は空配列。is_bookmarked/is_pinnedはこの応答ではキー無し。
正規化エラー無し。keep_read_status=1/no_extend_session=1を送った。
ルーム詳細は既読判定キーを返さないため、`/projects`一覧のis_unreadを前後比較した。
観測は既読false→falseで不変。`/projects`全102ルームのinventoryはcomplete、現時点の未読は0。
3分間・15秒間隔のbounded観測を2回（各12回、計24回）行ったが、未読対象は見つからなかった。
**未読true→true保持は未実証**。実時間のsession非延長効果も未実証だが、
原session行の指定受理/期限切れの扱いとは区別し、新しい必須条件にしない。既読falseの不変だけを
第1層有効化の根拠にしない。第0層は既存収集応答だけを使い、新GETは既定off。

追加確認の事実（キー・型・集計・比較結論だけを記録）:

- 履歴include_meta有/無ともreactions/mentionsはlist。is_bookmarked/is_pinnedはキー無し。
- 一括messagesはkeep_read_status付き400。指定なし＋include_oldest_unread_thread_message_id=1で200、targetを返した。ID数上限・未読保持は未実証。
- 押下者一覧はkeep_read_status付き400。指定なし・初回timestamp省略では200、全種類2行・has_next=false、viewed別経路1行でmessage.reactionsの件数と一致した。timestamp空文字を送ると200空となり不整合。初回はtimestampを省略し、継続は有効なserver timestampだけを使う。空文字を完全断面としない。
- inventoryで既読と確認した既存rootでper_page=1も受理された。全種類user_reactionsは2walk各2ページ/2行、viewed種類は2walk各1ページ/1行。各walkは第1ページのmessage.reactionsと件数一致、paginate.timestampは固定の正整数、終端has_next=false、完全集合は不変。行のcreated_at/updated_at/reacted_at/timestampキー、meta message.id/project_idはこの応答に無い。ID echoは存在時だけ検査する。全GETはextend_session=False経路でno_extend_session=1を送信、POSTなし。値/ID/生応答は保存していない。更新中比較・未読true保持・期限効果の証明とは区別する。
- 同じ押下者walkではmultiple_kinds_per_actor_observed=false。1人複数種別の可否を断定する結果ではなく、このサンプルで未観測。初回探索では投稿flag欠落を既読とせず対象を確定できなかった。その後、既読ルームの本人root候補12件目でreactions=[]を確認し、両押下者GETを各2walk取得。全walkが0行/1ページ・件数・終端・timestamp固定・集合不変で整合した。未読保持の証明とはしない。
- bounded履歴2ルーム（per_page20）内のself_reacted=true投稿を使い、完全押下者一覧has_next=false・user.id int・同種別を名簿の唯一is_self IDと照合し一致した。陰性例も一致。実押下者/患者ID・本文をrepoへ残していない。
- mentioned/bookmarkedはincrement_countなし・no_extend_session=1で両GET200、messages空。非空応答・未読true保持・session副作用は未実証。
- 返信投稿をroot用の1件経路で取ると422になることを実測。既存thread経路にparent IDとmessage_id/per_page=1/keep_read_status=1/no_extend_session=1を指定するとexact返信1件200、reactions/mentions正規化エラー無し。adapter・監視target・stage・probeへ親IDを渡し、root2引数互換を維持した。関連311テスト成功。
- 本人root/他者root/本人返信/他者返信の4区分を既存対象で比較し、すべてtarget/author/parent一致、reactions/mentionsあり、optional errors=[]。既読ルームであることはinventoryで確認したが、投稿単位flagはexact応答に無い。返信はparent-specific経路、no_extend_session=1、POSTなし。実対象ID・本文等は出力保存していない。4区分の不足は解消したが、権限不足の実応答は判定不能である。
- 最新root exact GETはinclude_meta 0/1ともis_unread/is_read/read_at/has_unread_messages/has_unread_responsesキー無し。既存adapterのnorm is_unread=falseは欠落に対する互換既定値であり、raw投稿の既読false実測や未読保持の証拠として使わない。対象投稿の真の未読判定は親が点検中であり、診断は不明を成功にしない。
- projects/statusはafter=projects.paginate.timestampで200、projects.updatedはbool。押下/編集によるupdated対照は未実証。
- 臨床100ルームのinventoryからkarte ID重複を除く93患者を集計。medication_periodsは登録0/不明0、observation_itemsも登録0/不明0、両complete=true。consultationsは全100GET404、93患者不明、complete=false。相談登録0とは断定しない。

viewed自動付与・count 0種別省略・更新中paging・権限比較の
未実証は[確認票](../roadmap/stamps.md)へ残す。POSTによる押下・取消の実測はしていない。

### 全確認の手動実験と不足条件

[stampsの手順M0〜M8と全確認対応表](../roadmap/stamps.md#22-aの手動実験手順未実施の対照を含む)へ
原確認票の10項目と#26/#27件数を対応させた。これは未実施の実験設計であり、
画面操作の承認や成功結果ではない。最終実画面検証はユーザー担当であり、エージェントは禁止。追加のエージェント実機確認は終了した。
公式UIの投稿・スタンプ押下・取消・しおり/ピン変更は未承認、別アカウントの参加者はいない。
読み取り確認の通常患者ルーム許可は、これらの書込み許可を含まない。
[承認用の具体操作票](../roadmap/stamps.md#本人だけの公式ui操作として承認に出す具体案)は、
患者なし・本人のみ参加する専用ルームの可否確認、固定合成本文のroot/reply各1件、
本人のviewed/accepted押下・取消・再押下、本人宛メンション・しおり・権限のあるピン、
status前後比較に限定した。ルーム作成可否は未確認。他人の参加/通知が必要なら止める。
この操作票は検討時の案を保持したものであり、最新ではユーザー本人が操作する。本人宛メンションは作成せず、エージェントは画面を操作しない。
非公開APIの直接POST・自動押下機能・他人の反応変更・削除・明示既読化はこの案へ含めていない。

| 原確認の対照 | 本人アカウントで進められる方法 | まだ必要な条件 |
|---|---|---|
| count 0省略・本人フラグ・種類別一覧 | 患者情報なし専用投稿で本人未押下→押下→本人取消を逐次比較。他者の反応は変更しない | 対象/種類/戻す状態を含む公式UI操作の承認。現在のpositive/negative ID照合済み結果は保持 |
| 更新中paging・timestamp | 実際の継続ページの間に本人押下/取消を一度行い、完全性・断面・再取得条件を比較 | 静的なper_page=1の実2ページは確認済み。本人が操作を承認された専用対象と継続条件が必要 |
| 未読・viewed非変更、横断未読 | 対象画面を開かず、各GET前後の対象投稿とルームの未読trueを別々に比較 | 相手の自然な新着/既存送信者の協力と初回viewedの独立baseline。現在未読0。本人投稿では代替不可 |
| root/返信・本人/他者・認可結果 | 正しいroot/thread経路の4区分は確認済み。既存の権限差があれば本人だけで追加比較できる | 権限不足の実応答は判定不能。422/404を認可拒否と断定せず、特定403や権限変更を追加必須にしない |
| no_extend_session受理・期限切れの扱い | 複数経路の指定付き200、実cacheExpired→recovery、合成打切りを確認済み | 実時間の非延長効果は追加の未実証事項。原session行の必須completionと区別する |
| 非空mentioned/bookmarked、しおり/ピンの型 | 既存非空対象を読むか、承認後に本人しおり/本人宛メンション/権限のあるピンを専用投稿へ設定 | UI設定の承認と非空対象。他者からの未読メンションは別の受信対照が必要 |
| statusの押下更新 | UIを開いた後をbaselineに、本人押下/取消と無操作を比較 | UI承認と別投稿/編集等の更新が混ざらない対照 |
| 両押下者GETの49/50/51/100/101件 | 完全合成HTTPスタブで診断の終端・集計を検証 | 診断/metadata関連402件の合成回帰が成功。静的な実0行/2ページの成功と、押下取消を伴う更新中の未実測を区別する |
| #26/#27利用件数 | 全取得範囲と重複除外を維持し、薬歴/観測項目のcomplete結果を再利用 | 相談全404の経路/対象/権限等の原因と登録有無は未確定。93患者不明を0へ変更しない |

初回GETのviewed自動付与は2回目との一致だけでは証明できない。
小さいpage sizeでの実継続確認と、49/50/51/100/101件の合成境界確認は別の証拠として残す。
公式UIの押下取消は本人だけで代替できる項目があるが、新規受信未読には本人以外の投稿が必要。
更新中pagingには本人が操作できる専用対象の継続条件がまだ必要。全確認と実shadow照合の未達を保持し、
第1層既定off・参加者不在を理由に1.0.11の成功条件を縮小しない。

## 更新と公開の条件

schema 7→8のため自動適用は止まる。人承認付き更新と更新前バックアップが必要。
反映には選択モードのgateway/独立host/独立adapter再起動が必要だが、今回は配備しない。
生成CHANGELOGとREADMEの5項目レビューを一致させ、Release本文は同版からexportする。
公開時はcommit/pushの明示許可とexact SHAの必須CI、tag workflow下書き/本文一致確認が必要。

## 検証

最新の実装・診断・合成fixture・生成文書で一式を再検証した。
- **4214 passed、4 skipped、26 subtests passed、exit 0**、250.83秒。
  ログ: `/tmp/mcs-release-1.0.11-pytest-20261003-pass7.log`。
  最終レビューのカード上限と診断完了判定の3件を修正した差分を含む。
  skipはSDK不足の4対象で、修正後の固定SDK環境で別途確認した。
- その後、確認スクリプトはprojectの未読保持だけでなく対象投稿の未読が不変であることも要求し、
  既読/不明の対象を未読保持の成功にしない合成回帰を追加した。独立した生の未読観測・ページ取得・患者/group件数の先行402件、完了判定修正後の関連449件の合成回帰が成功した。最新の一式にも全件を含めて成功した。
- ruff（CI pin 0.16.10）の全対象、8/8安全ゲート、incident coverage、
  README/release生成・版/記録/リンク、export、SVG/PNG/hash、diff checkは成功。
- native SDKの接続/表示経路は下記固定環境で最終レビュー修正後も再検証した。
  stdlibのmetadata GET/DB/CLI/設定・共通カード予算を修正し、SDKアダプターのコード・依存・契約は変更していない。
  GitHub exact SHA CI・実配送・配備は未実施として区別する。

先行差分の検証履歴（成功・失敗を保持）:

- レビュー前のpass6一式: **4166 passed、4 skipped、26 subtests passed、exit 0**、248.00秒。
  ログ`/tmp/mcs-release-1.0.11-pytest-20261003-pass6.log`を保持。全体成功後の独立レビューで、
  上限近傍と不完全inventoryの追加回帰が不足していた3件を確認し、修正してpass7へ含めた。

- 返信再取得修正時の一式: **3822 passed、4 skipped、26 subtests passed**、266.85秒。ログ `/tmp/mcs-release-1.0.11-pytest-20261003-pass4.log`。

- pass5一式: **4014 passed、1 failed、4 skipped、26 subtests passed**。追加テストにより生成されたDEVELOPMENTのテスト表が古くなった。正規generatorで再生成し、該当metaとmetadata関連297件が成功した。失敗ログ `/tmp/mcs-release-1.0.11-pytest-20261003-pass5.log` は保持。

- 初回の一式検証: **3784 passed、4 skipped、26 subtests passed、exit 0**、254.16秒。
  skipはSDK不足の4対象で、下記固定SDK環境では別途すべて検証した。
  追加後の結果は上記の最新検証へ記録した。
- 初回はschema 7固定期待値2箇所で停止。既存のbackfill検証を維持したまま、
  期待する現行writer版をSCHEMA_VERSIONへ合わせた。再実行で一式成功。
- ruffはCI固定版0.16.10・CI全対象成功。shellcheck成功。
- 安全ゲート8/8、incident coverage、README生成drift・版/記録/リンク、
  release_notes check/export、git diff --check成功。
- Slack7組・LINE WORKS2組のSVG/PNG/hash整合検査成功。
  Slack02は本人スタンプ観測の合成例を追加し、日本語欠け・重なりを視覚確認した。
- 既存mainのユーザー差分は元bytesへ戻し、初期計画を持つ別worktreeと照合した。
  今回の実装・変更記録・リリース文書は専用worktreeだけにある。
- 成功ログ: `/tmp/mcs-release-1.0.11-pytest-20261003-pass2.log`。
  最初の失敗ログも保持し、未実施のGitHub CI・実配送を成功扱いにしない。

## 原本DBのschema marker事象

専用worktreeへ移る前の初期編集を定期処理が読み込み、原本DBがschema 8へ進んだ。
元コードを復元した後はschema 7を使うため、版番号の不一致で収集を開始できない。
metadataテーブルは0行で、行の消失は確認していない。
版番号だけを7へ戻し、追加テーブル・全データ・履歴を保持する復旧を準備した。
排他lockと現DBのバックアップ・quick_check・行数不変を必須とする。
ユーザーが版番号だけを戻す復旧を明示承認した。排他lock下で版番号7へ復旧し、
patients/messages/artifacts/attachments/read_marks/runsの行数不変、quick_check=okを確認した。
追加テーブルは保持、内容0行、削除なし。復旧前backupは原本rootのdata/backupsへ0600で保存した。
復旧後の既存定期実行をread-onlyで確認し、schema 7・run status=okを確認した。
手動の追加収集・通知・既読化・サービス再起動は行っていない。

## SDK検証結果

- standalone exact pinsの3対象: Python 3.13.16、23 passed、failed/skipなし、exit 0。
- Hermes固定ref `fd50a275e2616118c48fe07e7e1c878782b15ccd` の4対象:
  Python 3.11.14、14 passed、failed/skipなし、exit 0。
  discord.py 2.7.1・aiohttp 3.14.3・slack-bolt 1.30.0・slack-sdk 3.44.1。
- 空の環境・一時HOME・network/Keychain/MCS workerガードで実施。
  既存Hermes/venvは更新せず、固定ソースを一時archive、SDK不足は一時uv環境へ補完した。
- HermesはCIのPython 3.13と異なる。GitHub CI・実配送・配備の成功証明ではない。
- ログ: `/tmp/mcs-sdk-isolated-20261003.gIUfgT/{hermes,standalone}.log`。
- 最終レビューの3件修正後も、同じ固定ref/依存とガードでHermes14件・独立23件を
  再検証し、双方skipなし・exit 0。旧一時interpreterの消失による初回終了127を保持し、
  キャッシュの同じ23/18 packageからオフラインで一時venvを再生成した。
  新成功ログは同ディレクトリの`hermes-review-fixes-offline.log`と
  `standalone-review-fixes-offline.log`。本番venv・設定・サービスは変更していない。

## 最終診断の局所修正

- exact GETの投稿単位未読flagは欠落し得るため、正規化のFalseを証拠にしない。既存のkeep_read_status付き未読経路で、fresh timestampと生のtarget boolを取得前後に観測する。対象欠落/画面上限/不完全はunknown、重複・ID不一致・矛盾は安全なエラーで止める。初回メタの不正も保持し、既知の状態変化・不明から追加経路を呼ばない。
- 押下者診断は最大10ページ、初回timestamp省略、継続はserver timestamp固定。2完全walkの集合不変をID値を出さず比較する。未完了・同件数交代・途中429/401/404を消失/取消/0件にしない。
- 相談の公開UIはgroup対象だったため、診断の患者ルームGETを訂正した。usage report v2は薬歴/観測項目を患者単位、相談をgroup単位に分ける。group型不明や失敗を0として完了させず、患者数との関連付けは未知を維持する。サーバのgroup限定・404理由は追加実測していない。
- これらはローカルの合成検証のみ。最新ユーザー指示後に実API・公式画面で試行していない。

## ローカル準備の完了と最終受入の分担

最終レビューで3件を確認し、専用worktreeで修正した。
- スタンプ脚注を加えたカードの文字数: 旧版3846文字・新版4029文字の合成例で、
  共通4000文字検証による送信保留を再現した。全ページの見出し・脚注・ページ位置を
  算定し、必要時にだけ本文予算を下げて既存ページ分割へ渡す。投稿集合・順序・
  source generationと本文表示を維持し、長い単一項目は従来の明示省略導線を使う。
- 利用数診断の一覧: 逆行ページや重複ルームを終端フラグだけで完了にしていた。
  ページ番号・容量・重複・空の非終端と、返却されたtotalの型・不変・終端/実件数を検証する。
  矛盾時はdataset GET前にschema errorで停止する。
- 利用数診断の患者ID: medicalのkarte欠損/null/boolを対象から除外し、患者0件・
  complete=trueにしていた。ID不明の医療ルーム数を別のaggregateで残し、
  未知typeも含め患者datasetを未完了にする。患者件数へ換算せず、IDや本文を出力しない。

追加レビュー時の合成再現はカード1件・診断5件が失敗した。修正後は元のカード再現と
診断5件が成功し、診断の不正ページは安全なSchemaErrorを期待する契約に合わせた。
患者ID欠損4件のFalse要求はそのまま維持した。関連診断449件とカード表示・notify_cardsの
回帰、ruff・diff checkも成功した。元の失敗ログは`/tmp/mcs-probe-review-681e174g/reproduction.log`、
診断再現の成功ログは同ディレクトリの`fixed.log`へ保持する。
これらの修正はMCS実画面・実APIで試しておらず、同版の変更記録とREADME確認へ反映した。

実装・計画訂正・既存回帰・生成文書・Release本文のローカル準備を完了した。必須のMCS実画面/実shadowの全受入はユーザー担当として未完了を維持し、公開可能の最終判定は保留する。既定offを根拠に元受入を削らず、未達を成功にしない。push・PR・tag・公開・配備・再起動は行っていない。

原本DBは先行作業でread-onlyのschemaメタデータだけを照合し、marker7のまま、保留message_metadataの列が最終schema8の列に一致することを確認した。その確認では原本を変更せずLedgerを起動していない。既存mainのユーザー4差分を保持し、今回のレビュー修正も専用worktree内だけにある。レビュー修正では原本DBの確認や操作を追加していない。

## マージ前監査の3件の修正

- 未読一覧の終端では返却総ページ数と現在ページの一致を要求する。初ページで総数が省略され、2ページ目から小さすぎる総数が返る場合も、本人/他者のroot・返信の未読保持を成功にしない。正当な初回空一覧・総ページ数0は維持する。
- 空datasetにpaginateがある場合は、後続あり・正の総件数・不正な型/ページ/総ページ数をunknownへ集計する。paginate欠落との互換、非空先頭ページだけの登録あり確認を維持し、後続GETは追加しない。
- 受入票では通常の投稿診断とusage-countsの終了コードを分けた。usage-countsの0は全datasetの調査完了であり、未読保持や全22-Aの証明ではない。

今回追加64ケースを含む関連4ファイルは **513 passed、exit 0**。既存の
`run_tests.sh` を一時HOME・空の認証環境で実行し、さらにmacOSのファイル/ネットワーク
sandboxで本番データ読取りとネットワークを禁止した。書込みは容量512MiBの一時ボリューム
内のみ、CPU・プロセス数・ファイルサイズ・wall-clockを制限し、メモリは親から監視した。
macOSのRLIMIT_AS/DATA/RSS設定は利用できず、メモリ監視はOSの強制上限ではない。
これは通常の不具合修正のプロジェクト回帰であり、security-auditの隔離要件を満たす
追加セキュリティ実行監査とは扱わない。先行一式4214件とHermes14件・独立23件は、
今回変更していない経路の成功結果として再利用する。

ログは `/tmp/mcs-release-1.0.11-final-audit-fixes-20261003.log` に保持する。
今回もMCS実API・実画面・原本DB・Keychainは使用していない。
変更記録と未公開1.0.11本文は既存generatorで再生成し、1.0.10以前の本文を維持した。
全MCS受入はユーザー担当の未完了として保持し、公開・merge・配備の判定には使わない。

追記: リリース・CIスクリプトの局所回帰は **68 passed、26 subtests passed、exit 0**。
最初の2回は隔離環境がmacOSのgit起動用CommandLineToolsを読めず、
合成git initだけが失敗した（各1 failed、67 passed）。既存toolchainへの読取りと
そのgitを選ぶPATHを修正し、テスト・期待値・安全ゲートを変更せず成功した。
全ログは `/tmp/mcs-final-audit-fixes-validation-20261003/`、成功ログは
`/tmp/mcs-release-1.0.11-final-release-checks-20261003.log` に保持した。
ruff、8/8安全ゲート、incident coverage、README生成/版/記録/リンク、
release check/export、diff checkが成功。新しいRelease本文は既存の
`/tmp/mcs-release-1.0.11-notes.md` とtitle出力へ同期した。
