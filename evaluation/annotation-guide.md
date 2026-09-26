# 人手ラベル記入手順 v1

担当：ユーザー本人。方式名を伏せたworksheetを見て記入する。coordinator-keyは記入完了まで開かない。評価前にg6-criteria-v1.jsonと対象集合を固定する。

1. 候補を見る前に原文の対象投稿・親返信・時刻・投稿者を確認し、正解factを列挙する。各factへIDとimportant=true/falseを付ける。未記載と否定、予定/指示と実施、引用と本人の報告を区別する。重要度未記入は不可。
2. A/B/Cそれぞれについて、出力の全主張をIDで列挙し、critical・supported・finalをtrue/falseで記入する。支持できる主張にはcovered_gold_fact_idsを付ける。支持なしの主張に正解factを紐づけない。
3. 薬剤・否定・時制・投稿者関係を正解factと照合する。添付未解析や返信不足は「問題なし」にせず、評価不能理由を残す。本文外の医学知識から支持を補わない。
4. 原文で未解決の項目と解決報告を列挙し、候補Loopの対応・誤解決・欠落を確認する。候補の自動判定を正解へ転記しない。
5. 記入完了後、管理者側で--unblind-keyにより方式を戻す。worksheetのhuman_labels以外は変更しない。自由記述は保存できるが、それだけでは定量評価を実行できない。

品質評価器のlabelにはsource="human"、版、facts、claims、loopsと、記入の証跡receipt（receipt_id/labelled_at/reviewer）が必要。receiptは記入完了時に本人が採番・日付・記入者名を入れ、後から付け替えない。各claimsのIDは対応するcandidate.claimsと一致させ、全claimを評価する。factの重要度は厳密なbooleanである。source="human"は実際に本人が記入した場合だけ指定し、AIが作った合成ラベルはsyntheticとする。receiptは来歴の鍵であり、構造の検査はするが真正性そのものは記入プロセスが担保する。

評価票の各choiceにはc1,c2…の主張IDがある。human_labelsの各A/B/Cに評価器のlabel構造を記入した後、semantic_blindの--evaluation-records/--manifest/--methodで対応する未ラベルrecordへ結合できる。原文指紋・患者・split・主張IDと本文を照合し、既存ラベルは上書きしない。

現時点の制約：正解factは本人が原文から定義する必要がある。固定構造データのfacts/loops/usage/latencyは生成時の観測から準備し、結合処理で補作しない。自由記述だけの評価を定量labelへ自動変換しない。


定量評価に使う評価票は、作成前に各outputsへ`evaluation_candidate`として評価器用candidate全体（facts/claims/loops/status/usage/latency/version）を渡す。claimsのIDと本文は表示主張に一致させる。評価票にはpredictionsとしてfacts/loops/statusも表示されるため、本文だけでなくこの予測も確認する。管理者用キーにはcandidate全体のhashを保存し、結合時に差し替えを拒否する。

従来の本文だけの評価票およびsnapshotから直接作った比較票は定性比較用であり、定量ラベル結合には使えない。定量評価では観測済みcandidateを揃えてから評価票を作り直し、その票に本人が記入する。評価後にfactsや計測値を補作しない。
