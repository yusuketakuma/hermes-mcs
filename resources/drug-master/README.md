# システム版と一緒に配布する公式医薬品マスター

`20261013/y_20261013.zip` は、[厚生労働省・診療報酬情報提供サービス](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)の令和8年度医薬品マスターの公開ZIPです。2026-10-13に、現在の公開版が20261013版・19,286行であることを確認しました。ZIP内の行やバイト列は加工せず、同梱用のファイル名だけ変更しています。出典：厚生労働省 診療報酬情報提供サービス（同ページ）。manifestと検証ツールはhermes-mcsが作成しました。

`20260930/` は1.0.16〜1.0.20で同梱した旧版の原本とmanifestで、履歴として残しています。検証ツールと `mcs drug install-bundled` が使うのは、converterが固定する版（現在は20261013）のフォルダだけです。旧版から作った私有辞書は、`mcs drug status` で同梱版と異なると表示されます。作り直すかどうかは運用者が判断します。

この公開配布資産はテストfixtureではありません。テストは完全合成の別ZIPを使います。患者データ・実投稿・認証情報・操作者情報は含めません。

[厚労省の利用条件](https://www.mhlw.go.jp/chosakuken/index.html)は、特記のない情報を公共データ利用規約第1.0版（PDL1.0）の条件で利用できるとし、出典表示と、加工時には加工の表示を求めます。個別の条件や第三者権利の例外も利用者が確認してください。manifestの `reference_checked_on` は参照した日付で、運用者の利用承認ではありません。

## 同梱と有効化

リポジトリのcheckoutを利用する既存の導入・更新経路で、このZIPとmanifestもシステム版と一緒に配布されます。同梱だけでは私有設定への反映・有効化を自動で行いません。

`mcs drug install-bundled --terms-checked-on 日付 --confirmed-by 確認者 [--approved-by 承認者]` は、このmanifestから版・SHA・member・layoutを読み、運用者が入力した利用条件の確認日と変更区分・日付番兵の確認者でpinを組み立て、既存converterで全行を変換して `data/drug-map/` に新規0600の私有辞書を作ります。`--approved-by` を省略した辞書は参考の照会・検索専用です。設定・DB・通信は変更せず、使う場合は表示された `mcs_setup.py init --set 'drug_map=...'` を運用者が実行します。`mcs drug status` で同梱版と設定中の辞書の対応を確認できます。使い方は[利用ガイド](../../docs/guides/USER_GUIDE.md#同梱マスターを辞書として使う)を参照してください。

運用者の `approved_by`・`terms_checked_on`・`confirmed_by` を同梱manifestへ記録しません。辞書の有効化は既存の人承認と明示的な私有設定の経路を使います。未承認の辞書を照会・比較しても実運用で承認済みにはなりません。

作成する辞書には作成基準日と、候補集合が次に変わり得る日を記録します。原本の更新日と各行の対象日は異なる場合があり、辞書は日付だけで自動更新されません。`mcs drug status`で再作成の必要性を確認してください。基準日がない旧辞書は引き続き読み込めますが、日付の確認が必要です。新旧比較・旧辞書への戻し・中断時の扱いは[初回更新の防御と運用手順](../../docs/specs/official-drug-master.md#初回更新で追加した防御と確認範囲)を参照してください。

## 無通信の検証

リポジトリ直下で次を実行します。

```sh
python3 scripts/development/check_drug_master_bundle.py
```

明示する場合は `--manifest /absolute/path/to/manifest.json` を指定できます。検証は読取り専用です。容量・SHA・安全なZIP member・CSV hash・全行の既知形式・出典を照合し、件数と固定の状態だけを出力します。公式行を表示せず、接続・私有pin作成・承認・辞書作成・設定変更は行いません。

既存converterも、承認・status policyを渡さず全19,286行の入力形式を検証します。結果の `conversion_held=terms_unconfirmed` は意図した保留です。候補生成の有効化や臨床精度の検証を意味しません。現在の入力・状態仕様は[R08rec3・222–223頁](https://shinryohoshu.mhlw.go.jp/shinryohoshu/file/spec/R08rec3.pdf)と[R08rec1](https://shinryohoshu.mhlw.go.jp/shinryohoshu/file/spec/R08rec1.pdf)を参照します。

## 毎回のリリース準備

1. **各リリースで公式サイトを新しく確認**し、現在適用される医薬品マスターの版・更新日・件数・配布先と、利用条件・両仕様書を照合します。前回の日付や記憶だけで最新版と扱いません。
2. 公開ZIPを固定公式URLから、リダイレクト・proxyなし、32MiB上限・期限付きで取得し、取得バイト列のSHA・ZIP member・版を既存manifestと比較します。SHAが変わっていなければ、同じ原本を継続同梱できます。
3. 版やSHAが変わった場合は、CSV形式と変更区分・日付番兵・一般名/製品identity・利用条件を一次仕様でレビューします。未知の版やlayoutを自動受理せず、必要な場合はconverterの版/layoutと合成テストを更新します。
4. レビューした公式原本と出典・hash・件数・加工表示のmanifestを、今回のシステム版の変更として追加・更新します。過去版の資料や操作者の私有pinを付随作業で消しません。
5. 無通信のbundle検証、converterの全行report-only検証、関連する完全合成テストを通し、CHANGELOGに同梱masterの版と必要な運用者操作を記録します。必要な公開承認は既存のリリース経路に従います。

この手順は、masterを版更新に含めるための準備です。本番有効化や外部公開の権限を追加しません。
