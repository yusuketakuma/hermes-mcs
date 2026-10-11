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

## 2026-10-11後半の追加（Python 3.14・依存更新・導入/更新/復旧）

ユーザーの追加指示（インストーラー・更新・復旧の見直し、デスクトップ版からの導入、推奨Hermesの更新、Python 3.14以上への固定と依存の最新安定版への更新、前版からの差分の整理）により、次を追加で統合した。各項目は相互の独立レビューで技術受入した。

| 変更 | 内容 | 独立レビュー |
|---|---|---|
| 推奨Hermesの固定先 | `yusuketakuma/hermes-agent` の `6f35b1ed`（ブランチ`mcs-pin/20261011`）。本番で稼働実績のある統合（上流v0.21.6を含むmain f925b01791＋MCS用追加変更）から、ローカル専用の指示ファイル等7ファイルを公開版に戻した統合commit。私的なcommit履歴は公開していない。上流v0.21.6単体はMCSの契約テストが2件失敗（コマンド文脈の受渡し）するため使わない | ユーザー承認のうえ公開、root確認 |
| Python 3.14と依存 | CI全job 3.14、Hermesのdev依存はdependency groupから導入、ruff 0.17.0、aiohttp 3.14.5、multidict 7.1.0、setup-node v7.1.0、wiki更新のNode 24・openwiki 0.7.2・jsdom 30.1.2（mermaidはopenwikiの要求で11.17.2）。PyPI・npm registryで2026-10-11時点の最新を照合。更新した2依存のGitHub Advisory照会は該当0件 | root受入 |
| インストーラーと初期設定 | Python 3.14・新固定先、設定の検証を認証情報の保存前に実行、Slack/Discordのチャンネル設定の引継ぎ、空のモデル一覧を利用可能と扱わない、使用中・既設の実行環境は更新ツール経由以外で置き換えない | Claude受入（阻害3点の修正後） |
| 更新ツールでのPython移行 | 更新処理が保持するrun.lockのFDをinstall.shへ継承し、検証できた場合だけ旧venvを退避して3.14で再構築。過去の固定commit（または新固定commit）で未変更・detachedのHermes checkoutだけを進める。失敗・ロールバックで旧venvとcheckoutを戻し、強制中断はrecoverで旧版へ戻す。旧版の自動更新からは移行せず元の版に戻る | root受入（予備指摘を含む6点の修正後） |
| デスクトップ版からの導入 | `scripts/install_local.py`。恒久的な置き場所を決めてからinstall.shを実行し、ローカル実行を確認できない環境では書き込まない | root受入 |
| 前版からの差分の整理 | 実行経路から使われない補助関数3件の削除と外部評価の判定の共通化。公開互換入口・過去のmigrationは保持 | Claude受入 |

Python 3.14では深い入れ子のJSONを標準ライブラリが読めるため、読取り不能の分岐はテストで旧来の失敗を再現して検証する（本番の検証は弱めていない）。Hermes連携の契約テスト21件・独立実行SDKの契約テスト25件はPython 3.14.8で成功。

本番のHermes checkoutは独自ブランチ（`local/org-build`）上のため、本番へ1.0.18を適用する際は移行の安全条件（固定commit・detached）で止まる。本番の実行環境（Python 3.11のvenv）の移行方法は本番適用時に別途判断する。

## 公式医薬品masterの当該リリース確認

2026-10-11T00:25Zに[公式menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)と固定URLの[yFile](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile)をno-proxy/no-redirect、1応答32MiB・全体90秒の上限で新しく取得した。

| 検査 | 結果 |
|---|---|
| menu | 令和8年度版の医薬品マスターは19,272件・最終更新令和8年9月30日（令和8年4月1日から適用） |
| ZIP | 1,157,440 bytes・SHA256 `820f173981b1601e718ef110ffba55fbea1c04d07abb6a4297b4281db0eecccf`。同梱と一致 |
| member/CSV | `y_20260930.csv`・SHA256 `ac1e7ab8db086e31b117fe2df2563d500859de9220a80ccf9b746b62b1d5a73c`・19,272行。同梱と一致 |
| 仕様・利用条件 | R08rec3・R08rec1・厚労省利用条件ページを再取得し、hashが前回確認と一致 |
| 同梱検証 | `check_drug_master_bundle.py` ok、`conversion_held=terms_unconfirmed`（意図した保留） |

公開作業の直前（2026-10-11T04:38Z）に同じ上限・no-proxy/no-redirectで再取得し、menuの令和8年度版医薬品マスター（19,272件・令和8年9月30日）、ZIP・CSV・R08rec3・R08rec1・利用条件ページのSHA256が上記と一致することを再確認した。候補`30b19fe`での無通信のbundle検証もok（19,272行、`conversion_held=terms_unconfirmed`）。

更新はなく、20260930版を継続同梱する。今回追加した`install-bundled`は同梱原本の全行を実行時converterで変換できることを一時HOMEで確認した（12,210 entries、変更区分3の1,022行は変更年月日が実行日より後のため候補外）。運用者の`terms_checked_on`・`confirmed_by`・`approved_by`は入力必須または任意入力で、manifestや同梱物から作成しない。`mcs drug status`は版の識別（dict_idに含む原本SHA）の比較で、同梱物の完全な検証は`check_drug_master_bundle.py`が担う。本番の辞書作成・config有効化・既存投稿の再照合は運用者の操作であり、本リリースの範囲外。

## 新規導入と全過去版更新

[origins](../../../tests/fixtures/schema_upgrade/origins.json)と[update paths](../../../tests/fixtures/schema_upgrade/update-paths.json)を正本とする。v1.0.17 exact tag（`be0a392`）から空の合成DBへ`Ledger._init`だけを実行する抽出器で、同じ抽出器がv1.0.16からshape-10をバイト一致で再現すること、v1.0.17のDDLがshape-10と同一（schema 9）であることを確認し、v1.0.17はshape-10を共有する。更新元は全18公開版、Hermes18経路とstandalone8経路で、targetはv1.0.18。v1.0.18はDDL・SCHEMA_VERSIONを変更しない。v0〜2の手動条件、standaloneのhost起動条件、schema 0〜4/6の原DDLが証明されていない既存の制約を保持する。

新規導入では`mcs_setup.py services`がHermesモードで`local.mcs-health`を追加配置し、standaloneでは配置しない。サービス一覧は`mcs_setup`・`mcs_update`・`mcs_recover`で一致することを合成テストで確認する。実brew導入・実認証・実MCS/LLM/Jev・実host停止は合成成功から推定しない。

## 検証の区別

最終候補`2f1783a`（統合用ブランチ`release/1.0.18-candidate`）での実績（いずれもPython 3.14.8、完全合成）:

- 全テスト一式: 10,714 passed・2 skipped（SDK専用レーンの対象外）・0 failed（Claude実行、`2f1783a`）。
- root独立の全体実行（Python移行の前の固定ツリー）: 10,666 passed・11 failed・2 skipped。11件は旧版の合成fixture（doctor/CLI/旧recoveryの期待値）で、修正後262 passed。統合後の候補でops・install・CIの2,589 passed、最終の中断復旧の235 passed＋復元承認100 passed。
- SDKレーン: Hermes連携21 passed（6f35b1ed）・独立実行25 passed。静的検査: ruff 0.17.0全対象、安全ゲート10/10、incident網羅、`sh -n install.sh`、README・生成文書・リリースノートの検査、リリース関連テスト28件。
- リモートの最終SHAでの必須CI、push・PR・tag・公開・本番配備は未実施で、別の承認段階とする。



各変更の対象テスト・関連一式は各レビューで実行済み（snapshot 774件、lifecycle 321件、main-local 214件、drug 212件、更新元fixture、ruff・gates・README/notes check）。統合候補の最終source SHAでのtests/integration一式、CI必須チェック（lint-test、hygiene、incident-gates、standalone-sdk、hermes-integration、readme-sync、Release Notes Check）は公開前に別途確認し、この票の時点では完了扱いしない。

本番は2026-10-11時点でmain `1188bb7`稼働。1.0.18の本番適用・push・tag・Release公開は別の明示承認で行う。実データ・患者名・秘密値を公開記録へ含めない。臨床精度・長期安定稼働を合成成功から推定しない。
