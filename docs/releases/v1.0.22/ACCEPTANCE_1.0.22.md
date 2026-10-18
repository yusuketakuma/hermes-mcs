# 1.0.22 リリース受入票

## 対象と公開条件

- 比較元: v1.0.21（`802d45c9a50034a735e6138648f44b49722509ae`、本番反映済み）。
- 変更: 変更記録7件（`installer-updater-agent-entry`、`retired-extraction-import-roots`、セキュリティ修正5件 `security-*`）。CHANGELOG 1.0.22（2026-10-16）は生成器で作成。
- 範囲: 公開（commit・PR・merge・tag・Release）。本番反映・稼働サービスの再起動は含まない。
- DB schema・設定形式・通知・既読化・承認条件・モデルの変更なし。

## 公式医薬品マスター

2026-10-16 13:54（JST、`checked_at` 2026-10-16T04:54:57Z）に[公式menu](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/)と[yFile](https://shinryohoshu.mhlw.go.jp/shinryohoshu/downloadMenu/yFile)を新しく取得した。no-proxy/no-redirect、45秒・32MiB上限で、menu・yFile・利用条件ページはいずれもHTTP200。

| 項目 | 確認値 |
|---|---|
| menu | 令和8年度版（令和8年6月1日から適用）医薬品マスター 19,286件、最終更新 令和8年10月13日 |
| ZIP | 1,158,682 bytes / SHA256 `c3efc8aa772b9bf54cfa992e8df3bc82480f86f35bbeb52522d0997144140fb5`（manifestと一致） |
| CSV | `y_20261013.csv`、6,083,884 bytes / SHA256 `6fa8f08ca4341ac530e5f4081d3fd3bbebbfcdd278c0d0ec0f172a0c1af018d1`、19,286行（manifestと一致） |
| 利用条件 | 厚生労働省の利用条件ページは引き続き公共データ利用規約（第1.0版）準拠を明記 |
| 同梱検査 | `check_drug_master_bundle.py` は `status: ok`・終了0、`activation: false`・人承認必須 |

更新はなく、20261013版をそのまま同梱する。私有辞書の採用・承認・有効化は行わない。

## 全体監査（1.0.21の全体監査を基準にした差分監査）

1.0.21の全体監査（[1.0.21受入票](../v1.0.21/ACCEPTANCE_1.0.21.md#文書全体監査の扱い)）を基準に、今回の差分と共有経路を8領域で確認した。差分以外の追跡ファイルは v1.0.21 と同一。

| 領域 | 判定と根拠 |
|---|---|
| コードの無駄・リファクタ | 確認済み・改善不要。追加は既存の `_read_control_json`・`publish`・`plan_line` を再利用し、新しい抽象化なし |
| フォルダ・ファイル整理 | 確認済み・改善不要。旧 `mcs/extract/v2`・`v3` は追跡READMEだけで、配布する旧実装なし。`_mcs_path.py` の除外で残存ファイルを読込まない |
| 冗長な処理・性能 | 確認済み・改善不要。追加はinstall.shの1ファイル書込み、planの1ファイル読取り、import探索の1比較のみ（性能値は測定していない） |
| 正しさ・互換性・保存 | 指摘あり→修正済み。更新ツールの再導入が移行・修復用のopt-outを利用者の記録として残す問題を自己レビューで発見し、`MCS_UPDATE_REINSTALL` で記録を変えないよう修正・回帰追加。planの出力は項目追加のみで既存項目は不変 |
| セキュリティ・プライバシー | 確認済み・改善不要。記録はopt-outだけで秘密値を含まない。読取りは `O_NOFOLLOW`・通常ファイル限定、既知のopt-out以外を含む記録は不採用。AI向け案内は秘密値の端末入力と依頼範囲の限定を維持 |
| 依存・CI・供給経路 | 確認済み・改善不要。依存・lock・CI workflow・Hermes固定commit（`f36c1da0`）の変更なし |
| テスト・build・配布物 | 下記「検証」と「導入・更新」を参照 |
| 文書・画面・運用・release資産 | 確認済み。README 5項目は `readme-review.json`（1.0.22）に記録。画面の変更なし |

## 導入・更新

- 新規導入: `tests/meta/test_install_sh.py`・`test_install_sh_standalone.py`・`test_install_local.py` の隔離HOME・stub環境で、全工程・再実行・opt-out記録・別版の導入先での停止を確認。実機では作業ツリーから `install.sh --preflight`・`--dry-run`（読取りのみ）で各工程の解決を確認した。
- 過去版からの更新: [更新経路manifest](../../../tests/fixtures/schema_upgrade/update-paths.json)の移行先をv1.0.22とし、更新元にv1.0.21を追加した（Codex担当）。v1.0.21はtag `802d45c9` のschema-only抽出（実module・DB・serviceは実行しない）でschema 9・全56定義がv1.0.20と同一のため、既存のshape-11を共有する。更新元22公開版（v1.0.0〜v1.0.21）・現行34経路（手動3・移行先bootstrap 19・独立host 12）。過去26経路・既存32経路・全11件のSQL fixtureはbytes単位で保持。Python 3.14.8で更新経路・schema更新・release origin・upgrade系の429件が成功。合成回帰であり、旧版の更新ツール本体の再実行・実機配備は含まない。

## Claude Security スキャン

Claude Security（plugin 0.12.0）で `~/.mcs`（`802d45c9`、v1.0.21）全体を medium・攻撃面重視でスキャンした（2026-10-16 05:09 UTC）。全16の最上位ディレクトリを調査または理由付きで除外、調査担当41体すべて完了、検証パネルを通過した5件で検証状態は verified。報告書は本番 checkout 内の `.gitignore` 付き `CLAUDE-SECURITY-20261016-050955/`（追跡外）。コードを読んで導いた指摘で、攻撃の実行はしていない。

| 指摘 | 重大度 | 対応 |
|---|---|---|
| F1 自動ログイン用ChromeのCDP（127.0.0.1）を同じMacの別アカウントが操作できる（CWE-306） | MEDIUM | ログイン可能な別アカウントがあるMacでは自動ログインがCDP付きChromeを起動せず `manual_required:multi_user_host` で止まる。`--remote-debugging-pipe` への置換は常駐の仲介プロセスが必要なため次版以降の課題 |
| F2 規則抽出の正規表現の3乗のバックトラッキング（CWE-1333） | MEDIUM | `_MED_TOKEN`・`_DOSE`・処方照会の節走査の繰り返しと数値境界を制限。旧パターンは入力2倍で約8倍の時間、新パターンは攻撃規模で0.002秒（実測） |
| F3 Hermesのテキスト通知にメンション・偽装リンクが混入（CWE-116） | LOW | `_compose_body` の送信境界で `<`・`>`・`@` の全角化と `](` の分断（Codex） |
| F4 要約の limitation 主張・自由記述の制約が監査を素通り（CWE-1427） | LOW | limitation も監査対象、空の主張は未評価、制約はコードが生成した定型文だけを表示、単行化、POLICY_VERSION 2026-10-16.1 で旧PASSを失効（Codex） |
| F5 統計 overview がプロジェクト範囲外の集計を返す（CWE-862） | LOW | 部屋数・状態・保存期間を指定プロジェクトに限定 |

## CodeRabbit

CLI 0.9.0、`--agent --deep --fresh --uncommitted --include-untracked --base-commit 802d45c9`。

導入・更新の修正だけの段階（16ファイル）:

| 回 | 結果 |
|---|---|
| 1 | 終了0、`review_completed`・`completed`、16/16ファイル、指摘1（`repo_path` が無い環境のcheckout特定）→採用、READMEからUPGRADE_AGENT §1へ案内 |
| 2 | 終了0、`review_completed`・`completed`、16/16ファイル、指摘0 |

セキュリティ修正を含むリリース差分全体:

| 回 | 結果 |
|---|---|
| 1 | 終了0、`review_completed`・`completed`、49/49ファイル、指摘7。採用6（stats回帰の日時、UID上限の撤廃、用量の数値境界、dry-runの文言、§1のcheckout探索、要約制約の生成元分離＝Codex）。不採用1（31文字を超える薬剤名を不一致にする案は、空白のない日本語の文で薬剤を取りこぼすため末尾一致を維持） |
| 2 | 終了0、`review_completed`・`completed`、51/51ファイル、指摘5。採用3（小数点直後の用量境界、`--no-recovery` の規則の一本化、変更記録の記載）。不採用2（長い薬剤名は1回目と同じ理由、UID下限の撤廃は macOS の root〈UID 0、/bin/sh〉を数えてすべての Mac で自動ログインが止まるため。下限未満のアカウントはすでに全権を持つ） |

自動レビューは上限の2回で終了し、2回目の採用分はテストで確認した。

## 検証

最終候補の作業ツリーで、Python 3.14.8 の `scripts/run_tests.sh` 全体が終了0（統合テストを含む。専用SDKの検証枠2件はskipで、成功扱いにしない）。`update_readme.py --check`・`readme_release.py --check`・`release_notes.py check`・`check_drug_master_bundle.py`・`ci/gates.py`・`ci/mine_gates.py --check`・`generate_slack_gallery.py --check`・ruff 0.17.0・`git diff --check` はすべて成功。初回の全体実行で見つかった統合テスト2件の失敗（要約の合成stubが空の主張を前提にしていた）は、製品コード・安全ゲートを変えずにテストの合成データを直して解消した（Codex）。

実モデルでの抽出・要約の評価、実機での更新・本番反映、長時間稼働は未検証。
