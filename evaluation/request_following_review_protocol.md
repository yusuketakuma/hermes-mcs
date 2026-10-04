# 依頼追従 held-out 人手レビュー待ちキュー

この集合は完全に創作した事務・調整会話220件です。実投稿、匿名化投稿、実患者、
モデル出力は含みません。原文は2つのsourcesファイル、AIの合成提案は別の
proposalsファイルです。提案は正解でも本人確認済みラベルでもありません。
全件pending、promotion_eligible=false、人手検証済み件数は0です。

## 固定集合と操作

```sh
python3 evaluation/request_following_review.py validate
python3 evaluation/request_following_review.py export
python3 evaluation/request_following_review.py export-proposals
```

validateは件数・独立した原文・focus coverage・完全一致引用・pending状態とmanifestの
ファイルhashを検証します。意味的な多様性や合成宣言の真正性を機械だけで証明する
ものではありません。exportは原文優先のJSONL評価票です。提案とfocusは表示せず、
順序はsource fingerprintによる固定混合順です。全220件を出し、黙って件数を切りません。
export-proposalsは別保管用の合成提案JSONLで、欠けた任意項目をunknownとして明示します。
原文評価前にこのファイルを評価者へ渡さないでください。

manifestのhashとcase_idはこのheld-out集合の固定点です。case_idから生成する架空の
account/project/thread ID、split=test、source fingerprintを評価票へ保持します。
原文・話者・時刻・splitを評価中に差し替えたり、校正・開発用へ同じthreadを移したり
しないでください。修正が必要なら旧集合を保持し、新しい集合として扱います。
機械hashを更新しただけで既存人手ラベルを新集合へ引き継がないでください。

## 本人による source-first 注釈

1. 文脈を含む全messagesとtarget_message_idを読みます。source_timeがある場合は
   対象会話の創作上の時点です。ない場合はunknownで、日付を補いません。
2. 対象行為を分けて記載します。依頼・質問・自己予定、引用された旧依頼、転送者、
   元の依頼者、回答・受領・進捗・完了の対象範囲を区別します。
3. 宛先・依頼者・期限原文・条件原文と種別を記入し、各項目へmessage_idと完全一致
   quoteを付けます。候補値が不明ならunknownとし、話者名や医学知識から補いません。
   提案中のrequest_kind=noneは「新しい依頼を生成しない」の評価用区分で、
   公開canonical契約へ追加した値ではありません。
4. 返信が言及する元依頼の項目を評価する際は、context側のquoteと行為を明示します。
   request_from等が記載されても、新しい依頼が存在することを意味しません。
   部分完了・準備・申請受理・同意を依頼全体の完了に変換しないでください。
5. 実際の人手記入が終わった評価票だけへ、本人がhuman_verified_labelsを記入します。
   human_receiptも本人が採番・記入者・実記入日時を記載します。このツールは署名、
   完了日時、receiptを生成せず、本人の記入を代行しません。
6. 原文評価後に限って合成提案と比較し、不一致・判断不能・見落とした行為を記録します。
   提案のコピーを本人ラベルと呼ばないでください。返却票はsources/proposalsや
   固定manifestとは別の管理対象であり、このpending資産validatorは完了票を採点しません。

## 既存G6への接続は未完了

このqueue/worksheet形式はpending状態を表すための別形式です。
semantic evaluationの既存schema、g6-criteria-v1.json、min_human_labels>=200、
calibration、Loop同一性や公開世代のゲートを変更していません。
220件の原文や合成提案があることは、人手200件や校正合格を満たしません。

既存G6へ投入するには、承認された方式別モデル出力と完全なevaluation_candidate、
同じ固定bundle、claims/facts/loopsの対応、fact lifecycle観測、要求数/token/latency等の
実測を別工程で準備します。既存annotation-guideとsemantic_blindの盲検・本人記入・
unblind/join経路で本人receiptを結合してください。欠けた出力や実測を後から創作しません。
この非医療会話集合だけで薬剤などG6全指標の分母が揃うとは主張しません。
分母0・人手不足・未計測は既存gateの保留理由として残します。

残る条件は、本人レビュー、実候補モデルの品質比較・校正、既存全指標とlifecycle、
実capacity/予算/通知優先順の確認、オーナーの昇格判断です。
本資産の作成・exportはモデル実行、配備、設定変更、enforce有効化の承認ではありません。
