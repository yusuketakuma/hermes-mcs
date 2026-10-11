# 1.0.18 リリース受入票

2026-10-11（日本時間）。前公開版はv1.0.17（`be0a392afc48fac9cb1d51e361a0e951cf01f720`）。統合候補は`release/1.0.18-candidate`（base `1188bb7`）。本票はローカル監査・合成受入の根拠と制約を記録し、公開や本番の安定稼働をこの票だけで完了扱いしない。

## 対象と統合

v1.0.17以降のmain（意味解析の滞留修正・Jev聞き直し・要約形式修復・shadow v2診断・ルール20・launchdヘルス監視）と、2026-10-11に各自の独立レビューで技術受入した次の変更を統合した。

| 変更 | 内容 | 独立レビュー |
|---|---|---|
| 公開用snapshotの検証期限 | 公開候補だけを別プロセスで検証し、30秒で停止、前回公開を保持 | Claude受入（既存publishテストの実子経路・競合時のpartial記録を確認） |
| サービス再設定の所有プロセス停止 | 更新・復旧の`mcs_setup.py services`を既存`_run_owned`へ統一 | Claude受入（自己reload helperは独立sessionで対象外を確認） |
| 保存JSON読取り・設定数値の境界 | `loads_dict`/`positive`の共有、巨大数値の拒否 | Claude受入（mainの未commit15件のうち14件とバイト同一を確認） |
| 同梱masterからの私有辞書作成 | `mcs drug install-bundled` / `status` | root受入（shell引用・manifest検証の2指摘を修正後） |
| 更新元v1.0.17の追加・記録の整合 | origins/update-paths、未リリース記録6件の文言修正 | root統合時に確認 |

未リリース記録は34件。独立監査で29件をsourceと照合し、既定値変更の誤記・上限値・表示先・置換済み経路の6件を修正、重複した確率合計の記録1件を統合した。CHANGELOGは34件の技術根拠を折りたたみに保持し、本文を利用目的ごとに編集した。

## 公式医薬品masterの当該リリース確認

2026-10-11T00:25Zに[公式menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)と固定URLの[yFile](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile)をno-proxy/no-redirect、1応答32MiB・全体90秒の上限で新しく取得した。

| 検査 | 結果 |
|---|---|
| menu | 令和8年度版の医薬品マスターは19,272件・最終更新令和8年9月30日（令和8年4月1日から適用） |
| ZIP | 1,157,440 bytes・SHA256 `820f173981b1601e718ef110ffba55fbea1c04d07abb6a4297b4281db0eecccf`。同梱と一致 |
| member/CSV | `y_20260930.csv`・SHA256 `ac1e7ab8db086e31b117fe2df2563d500859de9220a80ccf9b746b62b1d5a73c`・19,272行。同梱と一致 |
| 仕様・利用条件 | R08rec3・R08rec1・厚労省利用条件ページを再取得し、hashが前回確認と一致 |
| 同梱検証 | `check_drug_master_bundle.py` ok、`conversion_held=terms_unconfirmed`（意図した保留） |

更新はなく、20260930版を継続同梱する。今回追加した`install-bundled`は同梱原本の全行を実行時converterで変換できることを一時HOMEで確認した（12,210 entries、変更区分3の1,022行は変更年月日が実行日より後のため候補外）。運用者の`terms_checked_on`・`confirmed_by`・`approved_by`は入力必須または任意入力で、manifestや同梱物から作成しない。`mcs drug status`は版の識別（dict_idに含む原本SHA）の比較で、同梱物の完全な検証は`check_drug_master_bundle.py`が担う。本番の辞書作成・config有効化・既存投稿の再照合は運用者の操作であり、本リリースの範囲外。

## 新規導入と全過去版更新

[origins](../../../tests/fixtures/schema_upgrade/origins.json)と[update paths](../../../tests/fixtures/schema_upgrade/update-paths.json)を正本とする。v1.0.17 exact tag（`be0a392`）から空の合成DBへ`Ledger._init`だけを実行する抽出器で、同じ抽出器がv1.0.16からshape-10をバイト一致で再現すること、v1.0.17のDDLがshape-10と同一（schema 9）であることを確認し、v1.0.17はshape-10を共有する。更新元は全18公開版、Hermes18経路とstandalone8経路で、targetはv1.0.18。v1.0.18はDDL・SCHEMA_VERSIONを変更しない。v0〜2の手動条件、standaloneのhost起動条件、schema 0〜4/6の原DDLが証明されていない既存の制約を保持する。

新規導入では`mcs_setup.py services`がHermesモードで`local.mcs-health`を追加配置し、standaloneでは配置しない。サービス一覧は`mcs_setup`・`mcs_update`・`mcs_recover`で一致することを合成テストで確認する。実brew導入・実認証・実MCS/LLM/Jev・実host停止は合成成功から推定しない。

## 検証の区別

各変更の対象テスト・関連一式は各レビューで実行済み（snapshot 774件、lifecycle 321件、main-local 214件、drug 212件、更新元fixture、ruff・gates・README/notes check）。統合候補の最終source SHAでのtests/integration一式、CI必須チェック（lint-test、hygiene、incident-gates、standalone-sdk、hermes-integration、readme-sync、Release Notes Check）は公開前に別途確認し、この票の時点では完了扱いしない。

本番は2026-10-11時点でmain `1188bb7`稼働。1.0.18の本番適用・push・tag・Release公開は別の明示承認で行う。実データ・患者名・秘密値を公開記録へ含めない。臨床精度・長期安定稼働を合成成功から推定しない。
