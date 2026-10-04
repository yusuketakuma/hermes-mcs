# 1.0.12基準の全リポジトリ・リファクタリング

基準: `fb87584e4d9dc5f9c9b793a999b0eebfe4ffdf31`（公開済み `v1.0.12`）。
依頼: 全フォルダ・全コードを対象に、根拠のある内部整理を実装する。
公開済みタグ・CHANGELOG・GitHub Releaseを変更する作業ではない。

## 対象と確認方法

`git ls-files` で追跡対象を固定した。Python 318、shell 8、計326コードファイル。
Pythonは全件AST解析し、関数・呼出・import・完全一致重複を横断確認した。
実装とツールは本文も確認し、候補の呼出元・保存形式・既存テストを照合した。
テスト176ファイルと統合7ファイルは構造・参照を全件確認し、変更に関係する本文を詳細確認した。
全テスト本文の逐語的な手作業通読、全分岐の動的網羅を主張するものではない。

非公開コードの外部検索は、追跡ソースのみの一時コピーで秘密値パターンと対象を確認して実施した。
JevgrepはHTTP 402で未完了。並列数1の再試行も同じエラーだったため、ローカル調査へ切り替えた。
検索結果を網羅性の証明として扱っていない。

| 対象 | コード数 | 確認結果・対応 |
|---|---:|---|
| `mcs/core/` | 8 | 共有処理・DB・snapshot・lock・LLM制御を確認。既存helperを再利用し、migrationと保存形式を維持。 |
| `mcs/extract/` | 4 | ルール・LLM・集約・benchを確認。臨床的な判定と品質・lease条件は維持。 |
| `mcs/ingest/` | 6 | 返信の全件取得を既存window取得へ統一。途中失敗を全件成功として返さない契約を維持。 |
| `mcs/notify/` | 8 | grant・送信結果不明・generation・復元hold・通知設定を確認。契約が異なる分岐を維持。 |
| `mcs/ops/` | 10 | 保存3箇所と更新用ファイルハッシュを共通化。no-clobberコマンド公開、承認・復旧条件を維持。 |
| `mcs/semantic/` | 26 | JSON判定・評価用hashを共通化。レポート再読込みと関係判定の中間組合せリストを除去。 |
| `mcs/views/` | 7 | metadataの到達不能な再判定を削除。読取り専用・不正値の優先・不明と空の区別を維持。 |
| `mcs/_mcs_path.py` | 1 | flat importの探索順・入口を確認。変更不要。 |
| `adapters/` | 35 | 対話期限・claim参照を共通化。接続先固有の認証・再送制御・scope・SDK境界を維持。 |
| `hermes_plugin/` | 6 | project付きとsystem操作のユーザー・チャット認可を既存helperへ統一。エラー判定順を維持。 |
| `mcs_standalone/` | 8 | supervisor・schedule・設定・互換入口を確認。互換module identityを維持。 |
| `lineworks_adapter/` | 2 | 独立CLIの互換入口を確認。変更不要。 |
| `ci/` | 2 | 依存・sandbox・writer・公開先・incident coverageのゲートを確認。制御の緩和なし。 |
| `deployment/` | 7 | shell6と独立復旧Python1を確認。本体が壊れても動く復旧ツールの意図的重複を維持。 |
| `scripts/` | 11 | テスト隔離・導入/更新・文書生成・gallery・合成bench・明示probeを確認。変更不要。 |
| ルート | 2 | `install.sh` と `conftest.py`。新規導入・環境隔離・SDK依存時の収集条件を確認。 |
| `tests/` | 176 | 構造・参照を全件照合し、既存回帰テストを再利用。レポート一読と返信途中失敗の回帰確認を追加。 |
| `integration/` | 7 | 合成回復と固定SDK/Hermes境界を確認。実サービスへの接続なし。 |

`.github/` のworkflow・Dependabot、plugin manifest、配備候補YAML、requirements、
launchagent7テンプレート、Makefile・pyprojectもソースの入口と照合した。変更不要。
`evaluation/` の6合成資産は構文・評価基準を確認した。
`changes/` は過去の記録を保持し、後続変更記録を追加した。
`docs/`・`openwiki/` は文書・画像・生成索引で実行コードがなく、生成物の直接整理はしない。
README・DEVELOPMENTの生成結果とSlack/LINE WORKSの画像整合を検証した。
既存の未追跡 `8`・`recover`、実データ・秘密ファイル・インストール済み依存は対象外で保持した。

## 実装した整理と維持した契約

- `atomic_write` をexport journal、知識ストア用出力、基準統計captureへ再利用。
  既存のJSONバイト、一時prefix、排他的な0600 staging、rename公開を保持する。
  後二者は共通helperのfile/dir fsyncも使うため、同期失敗は保存エラーとして報告される。
  rename後のdirectory fsync失敗では新ファイルが存在し得る。「失敗なら必ず旧ファイル」とは言わない。
- `file_sha256` の既存aliasで更新内部入口 `_file_sha256` を保持。
  DBバックアップ承認に使うdigestは同じ。独立復旧ツールの本体依存は増やさない。
- `loads_dict` にassessmentの保存JSON読取りを統一。
  壊れたJSON・非object・深すぎるJSONをcacheとして採用しない。
- `canonical` / `payload_hash` に評価criteriaとcandidateのhashを統一。
  UTF-8・sort_keys・separators・allow_nan=Falseの契約を保持。
  worksheet側はNaN許可契約が異なるため、この共通化の対象外。
- modal・confirm・followupの取得時失効処理はprivate helperで共有。
  public methodのRLock、失効時の削除・save、batch中の保存遅延は維持する。
  配送復旧は全claimコピーの代わりに既存の単件参照を使い、その参照にもRLockを付けた。
  全claimを走査する箇所はdetached dictを維持し、二重のlistコピーだけ除去した。
- `_authorize` は既存 `_authorize_system` を再利用。
  project ID → user → chat → project scopeの拒否順を保持する。
- 全件返信取得は `fetch_thread_window` を再利用。
  完了時の重複ID排除・順序、途中エラーの同一例外、上限での `thread_incomplete` を保持する。
- 関係判定は `chain(product(...), combinations(...))` で同じ順序のペアを順次処理。
  判定の総比較数・分類・結果sort・fingerprintは変えない。速度改善率は未測定。
- bench比較は表示時の解析結果を再利用し、同じファイルの再読込みを除去。
  表とdeltaが同じ読取りを使う回帰確認を追加した。
- metadataで先行するinvalid判定の後にある到達不能な再判定だけ削除した。

小さいローカルvalidatorの共有化で層をまたぐ依存を増やす案、行数による巨大module分割、
transportごとの異なるURL/filename/scope/receiptの統合は採用しない。
履歴migration、旧import入口、承認理由・receipt、人確認、既読snapshot timestamp、
redirect/proxy拒否、独立復旧、結果不明時の再送抑止は維持する。

## 検証

- 変更関連の集約: 504 passed（初期の対象ファイル名指定2回は収集前に失敗し、成功件数へ含めない）。
- 全体初回: 4,478 passed、4 skipped、26 subtests passed。
- 追加の返信取得整理後: `tests/ingest/` 902 passed。
- 最終全体: 4,481 passed、4 skipped、26 subtests passed（273.52秒）。
  返信取得の整理と追加回帰3ケースを含む最終コードで実行した。
- 固定standalone SDK: 23 passed。
- 固定Hermes `fd50a275e2616118c48fe07e7e1c878782b15ccd`: 4 files、14 passed。
  隔離環境はgit metadataを持たないarchive。GitHub treeのblob hashを全Pythonと照合し、
  相違はrunnerの一時保存先1行のみで、diffも確認した。
  `scripts/run_tests_parallel.py` の `/var/tmp/hermes-pytest` を専用scratchへ変更したもの。
  Hermes実装は固定版と一致し、テスト選択・assertion・retry条件は変えていない。
- ruff（CIと同範囲）、shellcheck、全318 PythonのAST、shell内Python3ブロック: 成功。
- 静的安全ゲート8/8、incident coverage、release fragment/CHANGELOG形式、
  README同期・review/link、Slack7画像・LINE WORKS2画像の整合: 成功。

テストは一時HOME・合成DB・スタブで実行。実MCS・実通知先・Keychain・原本DB・
実LLM/Jevへ接続していない。SDK/Hermesの導入済み隔離環境を再利用し、依存追加なし。
リファクタリング準備時点ではローカル編集・検証まで。
commit・push・実機反映は後続の明示承認に基づいて扱う。

終了時差分: 実装14ファイル、既存テスト2ファイル、変更記録1ファイル、本文書1ファイル。
実装のみでは53行追加・115行削除（差引62行削減）。テスト追加46行はこの計算へ含めない。
開始時から存在した未追跡 `8`・`recover` を保持し、使い捨て成果物をrepoへ追加していない。
