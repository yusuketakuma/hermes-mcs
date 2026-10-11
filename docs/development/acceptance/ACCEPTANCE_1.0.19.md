# 1.0.19 リリース受入票

2026-10-11（日本時間）。前公開版はv1.0.18（`14c33dd204e1355e8b951117715a9940fc25bfb4`）。候補は`feat/v1019-compatible-checkout`（base `14c33dd`）。本票はローカル監査・合成受入の根拠と制約を記録し、公開や本番の安定稼働をこの票だけで完了扱いしない。

## 対象

| 変更 | 内容 | 独立レビュー |
|---|---|---|
| 確認済みHermes checkoutの保持 | `scripts/mcs_upgrade.py plan|apply --keep-hermes-checkout <40桁commit>`。Python移行中、HEADが指定commitと一致し、追跡ファイルの変更がなく、`pyproject.toml`の`[project].requires-python`が実行するPython 3.14の完全なバージョンを許すcheckoutだけを動かさずに保持し、venvだけを作り直す。更新処理とinstall.shは同じ判定スクリプト（`mcs/ops/requires_python_admits.py`）を使い、固定commitの規則より先に判定する。指定なし・不一致・変更あり・3.14非対応は従来どおり事前確認で止まる | root技術受入（3指摘の修正後。最終patchのsha256 `e495712f…`とWT差分の一致、installer 19 passed・updater/launcher 97 passed、shellcheck） |
| 更新元v1.0.18の追加 | origins/update-paths。v1.0.18 exact tagの`Ledger._init`だけを空の合成DBで実行する抽出器で、DDLがv1.0.17と同一（schema 9）のためshape-10を共有。target v1.0.19 | Claude実施、更新経路テストで確認 |

1.0.18の本番適用は、本番のHermes checkoutが独自ブランチ上にあり移行の安全条件で止まるため未実施だった。本版はその条件を広げず、運用者が確認したcommitを明示した場合だけ保持を許す。

## 公式医薬品masterの当該リリース確認

2026-10-11T06:05Zに[公式menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)と固定URLの[yFile](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile)をno-proxy/no-redirect、1応答32MiB・全体90秒の上限で新しく取得した。

| 検査 | 結果 |
|---|---|
| menu | 令和8年度版の医薬品マスターは19,272件・最終更新令和8年9月30日（令和8年4月1日から適用）。HTMLのhashは1.0.18時点と異なるが、差分はセッションIDだけでタグを除いた本文は一致 |
| ZIP | 1,157,440 bytes・SHA256 `820f173981b1601e718ef110ffba55fbea1c04d07abb6a4297b4281db0eecccf`。同梱と一致 |
| member/CSV | `y_20260930.csv`・SHA256 `ac1e7ab8db086e31b117fe2df2563d500859de9220a80ccf9b746b62b1d5a73c`・19,272行。同梱と一致 |
| 仕様・利用条件 | R08rec3・R08rec1・厚労省利用条件ページを再取得し、hashが1.0.18時点と一致 |
| 同梱検証 | `check_drug_master_bundle.py` ok、`conversion_held=terms_unconfirmed`（意図した保留） |

更新はなく、20260930版を継続同梱する。辞書の作成・有効化は運用者の操作であり本リリースの範囲外。

## 新規導入と全過去版更新

[origins](../../../tests/fixtures/schema_upgrade/origins.json)と[update paths](../../../tests/fixtures/schema_upgrade/update-paths.json)を正本とする。更新元は全19公開版、Hermes19経路とstandalone9経路で、targetはv1.0.19。v1.0.19はDDL・SCHEMA_VERSIONを変更しない。v1.0.18からの更新は実行環境の移行を伴わない。1.0.17以前からのPython 3.14移行と中断時の復旧は1.0.18の受入範囲（[1.0.18受入票](ACCEPTANCE_1.0.18.md)）を維持し、本版は保持経路の判定を追加した。新規導入の経路（install.sh・初期設定）は保持経路以外を変更しない。

## 検証の区別

候補（Python 3.14.8と3.13、完全合成）での実績:

- install・Python移行の対象テスト（installer・config matrix・移行）を3.13と3.14.8の両方で成功。`tests/ops`・`tests/meta`一式を3.14.8で成功。更新経路・schema更新のテスト一式成功。
- 静的検査: ruff 0.17.0全対象、安全ゲート10/10、incident網羅、`sh -n install.sh`、shellcheck、README・生成文書・リリースノートの検査。
- リモートの最終SHAでの必須CI、push・PR・tag・公開・本番配備は別の段階で確認し、この票の時点では完了扱いしない。

実データ・患者名・秘密値を公開記録へ含めない。本番の保持経路での実適用・長期安定稼働を合成成功から推定しない。
