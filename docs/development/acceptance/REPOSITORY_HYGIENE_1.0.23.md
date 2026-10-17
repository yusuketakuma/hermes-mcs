# 1.0.23 リポジトリ整理の確認記録

2026-10-17。対象はリリース候補の作業ツリー。実データ・実サービス・LLMへ接続せず、ファイル一覧、コード、呼出元、CLI、CI、互換契約を読取りで確認した。並行実装中のため件数は調査時点の値であり、最終リリースの固定manifestではない。

## 結論

削除・移動が妥当と判断できるコードは今回の静的調査では見つからなかった。参照が少ない評価モジュール、互換入口、過去のfixture・公開master・変更記録には用途がある。行数・旧版名・定期処理から呼ばれないことだけを削除理由にしない。

文書の整理候補は `docs/development/DEVELOPMENT.md` 冒頭の現行案内。1.0.13の開発計画を現在の案内としているため、現行の `docs/ROADMAP.md` と版別受入記録へ案内し、旧計画は履歴として残す。親担当へ連絡済みで、この調査担当は共有文書を編集していない。自動生成のモジュール表も親担当が実装確定後に更新する。

## 全領域の追跡

「確認済み」は下記の範囲を読取りで確認したという意味であり、全関数の正しさや未実行経路の成功を意味しない。「改善不要」は削除・配置変更を支持する根拠がないもの。「制約あり」は除外・担当分離があるもの。

| 領域 | 状態 | 確認した根拠・扱い |
|---|---|---|
| ルートのREADME・AGENTS・SECURITY・LICENSE・CHANGELOG | 確認済み | 指示、利用入口、ライセンス、履歴という別用途。READMEとCHANGELOGの生成契約を保持 |
| `pyproject.toml`・`Makefile`・`conftest.py` | 確認済み | lint/test対象、隔離runner、socket遮断、SDK依存テストの区別を確認 |
| `.gitignore`・`.coderabbit.yaml`・`.diagram-design` | 制約あり | tracked設定として存在確認。ignore対象はローカル実行資産を含むため一括削除しない。レビュー・図生成設定は今回変更しない |
| `.github/`・`ci/` | 確認済み | CIのlint/pytest/生成物/bundle/static gates/macOS/pinned Hermes SDK経路、release notes同期、incident記録依存を確認。OpenWiki workflowは下記除外 |
| `mcs/core/` | 改善不要 | ledgerと共有安全処理は呼出元あり。履歴schema/migrationの整理削除はしない |
| `mcs/ingest/` | 制約あり | 定期処理・worker・job・health入口を参照調査。並行実装所有ファイルの編集なし |
| `mcs/extract/` | 改善不要 | v1/v4と世代横断処理、offline評価の役割を区別。旧v2/v3はbootstrapの除外契約を維持 |
| `mcs/semantic/` | 確認済み | facade・動的candidate import・offline評価を確認。新規候補評価実装は別担当の受入検証を参照 |
| `mcs/notify/` | 制約あり | `present_`+transportの動的importを確認。表示・配送変更は並行担当の所有 |
| `mcs/views/` | 確認済み | view/CLI呼出元、薬剤timeline等の新規経路を確認。配置移動の根拠なし |
| `mcs/ops/` | 制約あり | 共通CLIと独立CLIを区別。install/update/setup/recoveryの変更は別担当。repairの独立CLIを保持 |
| `adapters/` | 制約あり | Slack/Discord/LINE WORKS/commonの接続境界と互換scopeを確認。並行実装の削除・移動なし |
| `hermes_plugin/` | 改善不要 | plugin入口と歴史的re-exportは明示的互換surface。未使用import扱いで削除しない |
| `lineworks_adapter/` | 改善不要 | `__main__`からadapterのmainへ転送する公開CLI互換入口 |
| `mcs_standalone/` | 改善不要 | 独立host/CLIとサービス設定の参照あり。Hermes入口とは分離を維持 |
| `scripts/` | 制約あり | CLI wrapper、公式test runner、文書/画像生成、release/bundle/SDKチェック用途を確認。installer/updaterコードの実装評価は別担当 |
| `deployment/`・`install.sh` | 制約あり | plist、wrapper、復旧watchdog、候補隔離設定の配備用途を確認。実機配置なし。installer/updater/recovery担当へ分離 |
| `tests/`・`integration/` | 改善不要 | 合成fixture、過去schema/update経路、SDK専用laneの役割を保持。ローカルSDK未導入を削除根拠にしない |
| `evaluation/` | 確認済み | 固定criteria、合成練習資産、人手評価未実施の区別を確認。新criteriaは別担当が文書と整合 |
| `resources/drug-master/` | 改善不要 | 現行20261013と旧20260930の原本・manifest。READMEに旧版保持が明記される。最新公式版の確認は本調査の証拠に含めない |
| `changes/` | 改善不要 | `release_notes.py`がarchiveを履歴根拠として使用。旧記録は削除しない |
| `docs/guides/`・`docs/specs/` | 確認済み | 相対ファイルリンク、入口と仕様の区別を確認。各仕様の全動作検証は本整理の対象外 |
| `docs/development/`・`docs/ROADMAP.md` | 確認済み | 現行案内の改善候補1件。過去plans/acceptance/auditは履歴として維持 |
| `docs/dev-records/` | 改善不要 | `ci/mine_gates.py`がincident根拠として読む。保存記録の削除なし |
| `docs/screenshots/` | 確認済み | SVG/PNG/manifestと生成スクリプトの関係を確認。画像改訂は親・表示担当が所有 |
| `openwiki/`・OpenWiki workflow | 未確認 | ユーザー指示により本文を参照しない。削除・変更もしない |
| ignoredな私有data・設定・環境・cache | 制約あり | 実データ非使用。内容を読まず、共有作業環境をcleanup対象にしない |

## 静的な未参照候補を削除しなかった根拠

runtimeのPython 166ファイルをASTで読取り、構文解析失敗・空ファイル・150文字超の完全同一実装は0件。`mcs/`のflat import対象で同名モジュールの衝突も0件だった。これは似た実装や関数単位の重複の不存在を証明するものではない。

runtime側のモジュール名参照が薄い次の8件は、テストと文書・独立入口を追加照合した。

| モジュール | 保持理由 |
|---|---|
| `extract/eval/audit_cohort.py` | `__main__`を持つ固定cohortのoffline CLI。専用テストあり |
| `extract/eval/pattern_cards.py` | `__main__`を持つ評価CLI。専用テストあり |
| `extract/eval/extract_bench.py` | `__main__`を持つbenchmark。複数の抽出/scoring回帰テストと文書から参照 |
| `ops/mcs_repair.py` | `__main__`を持つ復旧CLI。repair/sidecar/公開安定性等の回帰テストあり |
| `semantic/eval/semantic_v4_cohort.py` | 専用テストとsemantic-evaluation仕様に保留条件が明示される。予算強制・cleanup復旧証拠なしの処理を止める役割がある |
| `semantic/eval/semantic_workflow.py` | 合成ケースで確認手順数を比較する評価ライブラリ。専用テストあり。実際の臨床効果を未検証とする契約を保持 |
| `semantic/eval/semantic_snapshot.py` | 今回追加の独立CLI。sealed candidateテストから使用。並行担当の生成文書更新待ち |
| `semantic/eval/semantic_bench.py` | `__main__`を持つbenchmark。専用テストとroot pathテストから参照 |

`mcs/_mcs_path.py` は再帰的なflat import登録を担う。`semantic_candidate.py` は登録された名前を `importlib.import_module` で読み、`notify_render.py` はtransport名からpresentation moduleを読む。単純なimport grepだけでこれらを不要扱いしない。`hermes_plugin/__init__.py` と `semantic.py` のre-exportは互換入口・monkeypatch点を維持する意図が明記されている。

## 検証と残る限界

- trackedファイル一覧と新規実装の一覧を照合した。並行差分をreset/restore/stash/cleanしていない。
- tracked Markdownの相対ファイルリンクを検査し、対象内の欠落を0件確認。OpenWiki、変更archive、incident履歴を除外し、URL・anchor・コード内の文字列・絶対パスは検査対象外。Markdownの完全parserではないため全リンクの保証とはしない。
- この調査で実行コードを変更していないため、成功済みの薬剤・schema更新テストを繰り返していない。実サービス・SDK・実機の経路は本調査では実行していない。
- 関数単位の到達可能性、全設定組合せ、全文書の記述の意味的正しさは未確認。全領域の一覧化と削除候補の絞込みを行ったもので、不要コードが絶対に存在しないとの結論ではない。
- コード削除・移動0件。文書案内の修正は親担当へ引継ぎ、共有所有権を守る。
