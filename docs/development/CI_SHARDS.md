# Linux CI の独立 runner 分割

Linux の既存全対象 tests/ + integration/ を lifecycle（meta/ops）、patients、残りの3群に分ける。
各群は別の GitHub-hosted runner と scripts/run_tests.sh の一時HOME/認証環境隔離を使う。
SQLite、一時ファイル、ポート、子プロセスは runner 間で共有しない。テストや安全ゲートは削除しない。
macOS の対象、Hermes/standalone SDK、incident/hygiene は変更しない。

required check `lint-test` は3群の完了を待ち、全群 success の場合だけ成功できる。
失敗・キャンセル・skip は成功に読み替えない。matrix fail-fast は無効で、失敗があっても他群を完走する。
ruff、医薬品master検査、生成文書検査も従来どおり lint-test に残す。
保護設定、公開権限、マージ判定の緩和はしない。artifact 名は群ごとに独立し7日保持する。

## 根拠と測定

変更前 run 43875319646 / job 133326732945:
- lifecycle: 8895 passed, 12 skipped, 15 subtests / 1446.34秒
- rest: 29838 passed, 8 skipped, 139 subtests / 3183.44秒
- uv/Python cache は既に hit。rest の slowest 30 に patients の複数テストが含まれる。

これは変更前の観測値であり、分割後の短縮率は未測定。新しいCIで各群の時間・合計runner時間・
収集件数・skip理由を比較する。追加runnerの起動、依存準備、収集により合計計算量は増え得る。
標準hosted runnerを維持し、有料larger runner/外部サービスは追加しない。

## 検証・戻し方

test_ci_pytest_groups.py は全テストファイルが1群だけに所属することと群選択・失敗伝播を検査する。
CI変更時には全対象と各群の collect-only node ID の和集合/重複も比較する。
旧Linux同時実行から独立runnerへの変更なので、速度は実際のhosted CIで確認する。
問題があればこの変更PRをrevertし、元の2 subprocess + lint-test一体実行へ戻す。

実装時のローカルPython 3.12収集は38752件（lifecycle 8916、patients 2950、rest 26886）、
1480ファイルで重複・欠落なし。node IDのCounter比較も全38752件が一致（重複0/欠落0/追加0）。hosted Python 3.14/macOSでの条件付き収集は新CIで別途確認する。
local CI代替runnerも8job構成へ更新し、3群を直列実行して今回の成功証跡だけを集約する。
検査の先送りというtradeoff: lint/生成文書検査はLinux群完了後に走るため、そこでの失敗検知は従来より遅い。
