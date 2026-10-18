# 全ワークツリー集約の照合台帳 — 1.0.23

観測時刻: `2026-10-17T02:20:04.747395+00:00`。比較対象HEAD: `5d71c1a0d7e51d8da1aec2bd6006626b68a26ac6`。

登録 126 件、HEADが対象HEADの祖先（同一含む）67 件、非祖先 59 件、prunable 2 件。未コミット変更または未追跡ファイルがあるものは 21 件（release作業中を含む）。

初回の読み取り専用棚卸し後、全126件の内容を照合し、未集約だった合成テスト5ファイルをリリース候補へ取り込んだ。追加33件と後継契約215件のテストが成功した。以下の初回候補一覧は経緯として保持し、末尾の最終処置・証跡を現状とする。元ワークツリー、所有印、稼働中作業、prunable登録は変更・削除していない。

## 方法と判定境界

- `git worktree list --porcelain` 全登録を対象とし、`GIT_OPTIONAL_LOCKS=0`でstatusを取得。ignored/private runtime dataは対象外。
- `merge-base --is-ancestor`、`git cherry`で祖先・パッチ同等を区別。独自非mergeコミットの変更パスと、tracked dirty/untracked全パスをJSONに保持。
- 現releaseの作業ファイルと一致、または`git apply --reverse --check`成功を内容取り込みの根拠として記録。失敗は後続編集でも起きるため「未統合」とは断定しない。
- 比較対象は上記HEADに加え、並行作業中のrelease未コミット内容を含む。最終commit時には候補の再照合が必要。
- reviewのscope base、固定レビュー用snapshot、migration用root基準を製品コミットと混同しない。合成基準をそのままmergeしない。
- 認証情報・実データ・差分本文は記録せず、パス・SHA・判定のみを保存。

詳細JSON: `~/.local/state/mcs-quality/v1.0.23/worktree-inventory.json`。`WT/`は`~/.mcs-wt/`、`CLAUDE/`は`~/.mcs/.claude/worktrees/`、`STATE/`は`~/.local/state/`、`LIVE`は`~/.mcs`。

## 優先する未統合候補

次の5ファイルは現releaseに同名パスがなく、release履歴にも同名パスがない。同名テスト関数も現tests内にない。旧テストをそのまま移す前に現API・合成fixture隔離・同等既存テストを確認する必要がある。

- `tests/meta/test_recovery_failure_cleanup.py` — `WT/v9-rc-20261009-integration`
- `tests/ops/test_repair_log_cleanup.py` — `WT/v9-rc-20261009-integration`, `WT/v9-rc-20261009-security`
- `tests/ops/test_setup_retirement_verification.py` — `WT/v9-rc-20261009-integration`, `WT/v9-rc-20261009-security`
- `tests/ops/test_update_failure_cleanup.py` — `WT/v9-rc-20261009-integration`, `WT/v9-rc-20261009-security`
- `tests/ops/test_update_service_stop_verification.py` — `WT/v9-rc-20261009-integration`, `WT/v9-rc-20261009-security`

これらはFDの失敗時cleanup、サービス不在の検証、providerエラーを停止済みとみなさないことの回帰候補。元WTには触れず、親が安全性と現APIを照合して採否・移植を扱う。

## 全登録の履歴と作業状態

| ワークツリー | HEAD | 祖先 | cherry同等 / 独自 | tracked / untracked | 状態 |
|---|---|---|---|---|---|
| `LIVE` | `f8c6c20d4f7fd841d5d66adfbf4b9675371a9339` | はい | 0 / 0 | 4 / 1 | 存在 |
| `TMP/wt-3213` | `3213eb41b869967e21ad20f4445c2366e5e7b72b` | はい | 0 / 0 | 0 / 0 | prunable・削除しない |
| `TMP/wt-combined2` | `9b10124a51d63f302ddba38f76b3268faf27b889` | いいえ | 0 / 3 | 0 / 0 | prunable・削除しない |
| `STATE/mcs-hotfix-summary-20261009/baseline` | `be0a392afc48fac9cb1d51e361a0e951cf01f720` | はい | 0 / 0 | 0 / 1 | 存在 |
| `STATE/mcs-quality/v1.0.21/blockers-20261014/mcs-review` | `0201dcd4f69b77f60d70d91ec2154a4821ec1c83` | いいえ | 0 / 6 | 0 / 0 | 存在 |
| `STATE/mcs-quality/v1.0.21/post-merge-fix-review-20261014/coderabbit-review` | `5af5fcb564b9116238099584c00a32f21c971de0` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `STATE/mcs-quality/v1.0.21/post-merge-review-20261014/coderabbit/01-core-ingest-semantic` | `bebd497c4d7856d58862033533e49ee87ebe0011` | いいえ | 0 / 2 | 0 / 0 | 存在 |
| `STATE/mcs-quality/v1.0.21/post-merge-review-20261014/coderabbit/02-delivery-notify` | `0f1eb46f82fe21e760b2d010e1a9680e8f8accff` | いいえ | 0 / 2 | 0 / 0 | 存在 |
| `STATE/mcs-quality/v1.0.21/post-merge-review-20261014/coderabbit/03-adapter-tests` | `18cfe1163419bf48c43f339d43b3541a2b8c2624` | いいえ | 0 / 2 | 0 / 0 | 存在 |
| `STATE/mcs-quality/v1.0.21/post-merge-review-20261014/coderabbit/04-operations-supply` | `8ddd93a3e678a493c0bb6d481f3e2b107aeb32e1` | いいえ | 0 / 2 | 0 / 0 | 存在 |
| `STATE/mcs-quality/v1.0.21/post-merge-review-20261014/coderabbit/05-docs-assets` | `ce797ce4422da73792c1539c307b0efe6721761a` | いいえ | 0 / 2 | 0 / 0 | 存在 |
| `WT/audit-v1.0.20-claude` | `7d746824f60481f059b277d0763fa7fc169dc847` | いいえ | 0 / 12 | 0 / 1 | 存在 |
| `WT/chore-coderabbit-drafts` | `785d036f070352aac25660fbc54de3598a4f5c3e` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/dev-team-base` | `f8c6c20d4f7fd841d5d66adfbf4b9675371a9339` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/docs-v1.0.21-aggregate` | `4148150c5433f6bdb9da74584121f3ed07f82637` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/docs-v1.0.21-final` | `ef1d6d4d0cdee0b38ca59625ec5d058c22007d91` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/docs-v1.0.21-late-aggregate` | `b5f457f45e7b53f88b632b475dc7ba44fd54bde9` | いいえ | 0 / 14 | 0 / 0 | 存在 |
| `WT/feat-bundled-drug-master` | `b7361b699dd1dea2a8900a51a4446fb3810224f4` | いいえ | 0 / 1 | 0 / 0 | 存在 |
| `WT/feat-desktop-local-bootstrap` | `be5ab0240a605a34dad815a777ab4b9b852759b9` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `WT/feat-hermes-release-pin` | `7687a1f95aae7e9d73c5d48a7997c73866578c29` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `WT/feat-python314-runtime-migration` | `a76a2f1fd41c15fe30f12307e569fd4e79c56233` | いいえ | 1 / 2 | 0 / 0 | 存在 |
| `WT/feat-v1.0.21-claude-ops` | `8bcada00bb7d8b29bf3fea8b74ce88af44a97208` | いいえ | 0 / 8 | 0 / 0 | 存在 |
| `WT/feat-v1.0.21-operations` | `7a4e0c60d9257867f8a31e1c087849e6cf1d17be` | はい | 0 / 0 | 141 / 69 | 存在 |
| `WT/feat-v1.0.21-pharmacy-integration` | `92e0b7ac3d612339e948d8c22badec2e4f45229c` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/feat-v1019-compatible-checkout` | `25f434f0005ac9661f08cc7302d7002ec2572e93` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-admitted-chat-wait-s` | `f713ab95ab6f9726aeca2c79084c8e16025ca31b` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-backup-deadline-slack` | `e714b9d23947f2de48c184f17933ac029b0d4f3a` | いいえ | 0 / 1 | 0 / 0 | 存在 |
| `WT/fix-daily-backup-deadline` | `2732dc8f9bb7c6cf0155648faa0c97c54278ccf6` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-installer-config-matrix` | `4a6e3e41e229176f79520b168068667c5d24851e` | いいえ | 1 / 1 | 0 / 0 | 存在 |
| `WT/fix-installer-updater-agent-entry` | `4c5e1fd362d089616fe989943b6cb53bdc3c0d2b` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-llm-down-since-recovery` | `4b02859aebb8924ca99c37179d9e098b885a1ba4` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-llm-health-ordering` | `bbd5eb58e82051f531113d5c33a4e3ddba147e43` | いいえ | 0 / 1 | 0 / 0 | 存在 |
| `WT/fix-main-local-disposition` | `685a1b768c1e3ab010cb5243d2c87d610b4ed313` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `WT/fix-retired-extraction-import-roots` | `802d45c9a50034a735e6138648f44b49722509ae` | はい | 0 / 0 | 2 / 2 | 存在 |
| `WT/fix-revoke-withdraw-first` | `638662310d76958bc81406a9a2f03c81b3b14fe5` | いいえ | 0 / 1 | 0 / 0 | 存在 |
| `WT/fix-same-pass-revoke` | `b09e5228c250c10fb6cbd8e15e8f3e9ed8ffc204` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-slack-profile-worker-20261017` | `4237548bf089a6bde5445c32f77163f611be1ecf` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-snapshot-read-snapshot` | `f26b3eb2cdde1a656816504afe79fa830419505a` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-standalone-log-fields` | `cea1f082ea39a0d5be8af0a08f38c8c84cc5cbd8` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-v1.0.21-checkpoint-unsent` | `55a986cf7edb7b35db2dc6729b37394d883e5301` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-v1.0.21-late-quality` | `4cf731c2d49a27d84fe465fa2ff0bb01b2974be4` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-v1.0.21-post-merge-review` | `1dc65b0364dee31a08f7114f0bc3ade63b4eacda` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-v1.0.21-recover-efficiency` | `8a9e9177d5b10c5fd01d231851c67eb0fb2f6443` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-v1.0.21-recover-ops` | `8561dd082f2f15323229ebb89f0e465dc55ceeed` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/fix-v1022-update-chunk-plan` | `958614690d097f7dd05e1a4215b26d95da4b316a` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hermes-v0217-pin` | `7a4e0c60d9257867f8a31e1c087849e6cf1d17be` | はい | 0 / 0 | 6 / 1 | 存在 |
| `WT/hotfix-backlog-commit-reserve` | `c4efb119be7421c13c0b393ffccb4736d5b7b11b` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-backlog-qc-generation-order` | `1813c186780dd7c967528adc40e918cf0dcad304` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-jev-answer-reask` | `73d9d40729568ca258af2ccfde16c7fff28efb9a` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-jev-claim-batch` | `408a2e49ef714459a7af42e5c3390a3f622f6b4b` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-jev-partial-answers` | `04750807cff75199b5e527caad56ac46ad9a2f89` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-jev-prob-codes` | `cdaf69bf6a8ce2b3ebcd547a5b8d8caa7a2c2c0f` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-limitations-item-type` | `c9ea41d301fb3b9e593ab21e40765a1e01b26140` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-loop-rejection-integration` | `88c6c739d732b8476017d420f50cdd155368d1ad` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-loop-rejection-view` | `128f5c8190a7f42151ff16ae97ca203a6d922cec` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-loop-relation-reask` | `518d0bfe99e0df8df08118bf1b36fecf24c3ab3e` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-loop-relation-rejected` | `0a8be9a77493212a2e92976ef344cf326b6ca2eb` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-maintenance-lifecycle` | `2a95ca3464c0d16d7c8e2269877d348f76e74232` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-mcs-health-launchd` | `1188bb7b1fe81ddb1e79e2005e1ac614f0ecd8ad` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-rollup-input-signature` | `4c5872580f6a1d695129c4c783d87d26acd68eec` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-rollup-reobserve` | `40bf4fd679f1ae0ffa68cf8ade23a3b28c871ac8` | はい | 0 / 0 | 7 / 0 | 存在 |
| `WT/hotfix-run-lock-diag` | `8ee567224a4c8a05d71f163a4a2288170001ba57` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-semantic-backlog` | `28171054b62aaef4293487932096a33953ad2a0f` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-semantic-cap600` | `f82503b761b2cc0a96aa6c972ff5a36cfb92e1a6` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-shadow-v2-drop-reasons` | `057f4bdccd921cfb97a149fd23a98a118893f509` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-shadow-v2-failure-detail` | `2ddf3d430371f78bd123c07dcac2017f5ac9ab18` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-shadow-v2-null-context` | `750cd25704e4999846e70558a7dfce9c58958a93` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-shadow-v2-rerun` | `e60918950c1509d92a474918bf255d525932130d` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-shadow-v2-timing` | `b97aef64ff21d1247806d0188214e9add6854fb9` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-snapshot-hard-deadline` | `b4c18a1488bffec44f37af36f1383d53207aba85` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-snapshot-validation-deadline` | `a84330c1aedd2f9860f2c76822390434294863e9` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-summary-format-length-stop` | `40bf4fd679f1ae0ffa68cf8ade23a3b28c871ac8` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-summary-format-repair` | `7cfb9dbf9a913b61af131627d3980d0ae40f6e3c` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-summary-limitations-string` | `ab769ba9f8ccef36009b06c228ed7b0face472bb` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-summary-quality` | `079e5a0093ed33ea7acb4e23d6a154d059aeb51e` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/hotfix-summary-validation-codes` | `a410ab367d87ff52348585ae1b2292e45748ab60` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/integrate-all-worktrees-20261014` | `d87d6f39378560078637a2312e71b72e9705958d` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/integrate-remote-main` | `c20f56fef08ecc72b1b606d7ae9003fe33bc866e` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/pharmacy-chat-coverage-w1` | `5b8a81bda92646131b340fbbf04414063c847f80` | はい | 0 / 0 | 0 / 5 | 存在 |
| `WT/quality-v9-efficiency` | `18d77f9b0783b6c5fdf0f5eca59554abb3bb13fd` | いいえ | 1 / 0 | 21 / 6 | 存在 |
| `WT/quality-v9-integration` | `746ca0d6fde61fc1f6ec3aa5146f48815132d837` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/quality-v9-stability` | `180e074dafdc21699993f0402874ed07eda1b236` | いいえ | 1 / 0 | 7 / 2 | 存在 |
| `WT/quality-v9-structure` | `c543f65b7efde1717a0d2d6793cc32c8f85ad992` | いいえ | 1 / 0 | 7 / 2 | 存在 |
| `WT/refactor-release-1.0.18-delta` | `6b8a2f9f8affee2eaba3f695e230fe9fd6b4406d` | いいえ | 3 / 0 | 0 / 0 | 存在 |
| `WT/refactor-v1.0.20-debt` | `3213eb41b869967e21ad20f4445c2366e5e7b72b` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/release-1.0.18-candidate` | `14c33dd204e1355e8b951117715a9940fc25bfb4` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/release-1.0.18-prep` | `e897c095623d15f255e4d5319198f8ebdf5e5b0c` | いいえ | 2 / 0 | 0 / 0 | 存在 |
| `WT/release-v1.0.20-main` | `bbca0e09484f9115e88660ba764a7a79071d88e3` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/release-v1.0.20-prep` | `57c4a40088cf8b08a6e9fc57b7d007223fcde4ff` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/release-v1.0.23` | `5d71c1a0d7e51d8da1aec2bd6006626b68a26ac6` | はい | 0 / 0 | 74 / 32 | 存在 |
| `WT/release-v15-schema` | `7ae6ee47210608759bd14b7b2ed86aa73606b1c2` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/review-highs` | `aad1947bce751aaec5cabaae970265d4c76b0212` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/review-v1.0.21-20261014` | `f0646a3f651fddebf89ead4dc67206418b4c583c` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/v1.0.17-stability` | `e18b54b1f521bea39fc0608b963ecf950e4dbb07` | はい | 0 / 0 | 0 / 0 | 存在 |
| `WT/v9-rc-20261009-efficiency` | `079e5a0093ed33ea7acb4e23d6a154d059aeb51e` | はい | 0 / 0 | 53 / 15 | 存在 |
| `WT/v9-rc-20261009-integration` | `079e5a0093ed33ea7acb4e23d6a154d059aeb51e` | はい | 0 / 0 | 110 / 45 | 存在 |
| `WT/v9-rc-20261009-security` | `079e5a0093ed33ea7acb4e23d6a154d059aeb51e` | はい | 0 / 0 | 30 / 19 | 存在 |
| `WT/v9-rc-20261009-structure` | `079e5a0093ed33ea7acb4e23d6a154d059aeb51e` | はい | 0 / 0 | 25 / 9 | 存在 |
| `CLAUDE/wf_04c119de-b76-1` | `e61715bd9953b7ba5ff50f9ab79088441ea0bd9d` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-10` | `369bf08236b23d2ca5a703592d6129354c953ce0` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-11` | `cebaae6880f64943ec6e272c59988ed7704287d7` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-12` | `c04ac78b02f0d5093fb7ccf4cea7e11c32647dff` | いいえ | 1 / 0 | 0 / 1 | 存在 |
| `CLAUDE/wf_04c119de-b76-13` | `a754ac4375da34438601f8f114cdb7a18c5b7d0a` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-14` | `93c1ca344625d44cb6b9138d16cfb6f2ffab78f2` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-2` | `ecb090c168b612f26fcfb68772edc917c42fca11` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-3` | `c0e69c18138e7e9be30b92e73a81dd50b767addd` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-4` | `076179980c18deaed6a927c877301ed0017016e4` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-5` | `b00e0db6b512faf8a530a2be272cb8bfe43b426a` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-6` | `c4d180d96ddc20c8a106d0ab043027361d4a1944` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-7` | `419ded6e7c15e43a099240194d00e79c94b0778d` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-8` | `e67b3b966426ec5c6ace41aa124ac252ae83a649` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_04c119de-b76-9` | `a3e187750593534a74ee9421d80eef9f6a086ddb` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_872d3f72-871-1` | `d3007709e5404eb50126b771cfe22f8664ceba22` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_872d3f72-871-2` | `53bf3f01109c3576800e26f227f67ecf7f1a41bc` | いいえ | 1 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_872d3f72-871-3` | `a828b978e86ff5935964139b64a50b653f5dd02d` | いいえ | 2 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_b0df567d-353-1` | `794d15108efa6f275b442f5b4b2fad15c7815877` | いいえ | 0 / 5 | 0 / 0 | 存在 |
| `CLAUDE/wf_b0df567d-353-2` | `2c5c200b490f5c39d6846659c95032294843b729` | いいえ | 0 / 4 | 0 / 0 | 存在 |
| `CLAUDE/wf_b0df567d-353-3` | `970f6272a487f9f15c806f552a8a207e6eed7147` | いいえ | 0 / 2 | 0 / 1 | 存在 |
| `CLAUDE/wf_eee116be-215-1` | `dbb5c833ba5357657667e54e6f6621d3e5d46426` | いいえ | 12 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_eee116be-215-2` | `d08a25e7a84421a57aae2764a127ef24f93fa65f` | いいえ | 15 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_eee116be-215-3` | `ab65731223b56e4fa3533f5c55634996c12e3655` | いいえ | 25 / 0 | 4 / 0 | 存在 |
| `CLAUDE/wf_eee116be-215-4` | `7510d3855f29d468851b4cad76233a165d0d0cc9` | いいえ | 17 / 0 | 0 / 1 | 存在 |
| `CLAUDE/wf_eee116be-215-5` | `b42c5e9a15bacb2a6204cddcaefa10fd89014c3c` | いいえ | 8 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_eee116be-215-6` | `702648c95b57f6c98d211105c0efa00a091b0f0d` | いいえ | 14 / 0 | 0 / 1 | 存在 |
| `CLAUDE/wf_eee116be-215-7` | `2523a2cb7fc3d2de035ee6e6296c2c854aaa49eb` | いいえ | 5 / 0 | 0 / 0 | 存在 |
| `CLAUDE/wf_eee116be-215-8` | `382fa0ea727c5b0d40ff513ad4de8d454b9ffeee` | いいえ | 9 / 0 | 0 / 0 | 存在 |

## 独自SHAの区分

パッチ同等でない68 SHA（WT間の重複を除く）。以下の残差件数は必要な意味照合の件数で、機能欠落の件数ではない。各パス・親SHAは詳細JSONに記録。

| SHA | 区分 | 変更パス / 残差 |
|---|---|---|
| `0201dcd4f69b77f60d70d91ec2154a4821ec1c83` | product_commit_compare_residual | 5 / 1 |
| `07d1522b6675b030b92580b3d6619ad5bf2995d3` | synthetic_baseline_do_not_merge | 170 / 22 |
| `0a0f6a221192d4931e23924d8b1a40dcdd040830` | product_commit_compare_residual | 3 / 1 |
| `0b9deac87ff46fbcb7e26d98c3ac48fdaaf79fd5` | product_commit_compare_residual | 1 / 1 |
| `0f1eb46f82fe21e760b2d010e1a9680e8f8accff` | review_scope_snapshot_do_not_merge_whole | 45 / 1 |
| `0f71635308015d4c09fb2970414eef61201177d2` | product_commit_compare_residual | 9 / 2 |
| `113f09a4391e5242e2b6b4fb5795d04578b73ff5` | product_commit_compare_residual | 1 / 1 |
| `18cfe1163419bf48c43f339d43b3541a2b8c2624` | review_scope_snapshot_do_not_merge_whole | 46 / 0 |
| `1d890c624af5aae4509dc47ebf83999190fb986b` | product_commit_compare_residual | 4 / 2 |
| `200a193e6afcbffd946115125788589016991ea7` | synthetic_baseline_do_not_merge | 11 / 5 |
| `207cf2caea226cc164117e601ca85c88ba46c8d2` | synthetic_baseline_do_not_merge | 179 / 22 |
| `20eeb126ce973cd367f720087ff7ab71ac62621f` | product_commit_compare_residual | 5 / 5 |
| `2ae15ea3d8edb7659819dd8fa8dfdba39e3c391b` | product_commit_compare_residual | 1 / 1 |
| `2c5c200b490f5c39d6846659c95032294843b729` | product_commit_compare_residual | 1 / 1 |
| `306ee8a069bfc749d1e2d0870a367fc38b595a56` | review_candidate_snapshot_compare_residual_only | 73 / 31 |
| `30ac16f5e12a0e98647e113535f75f2dc49cab5b` | synthetic_baseline_do_not_merge | 167 / 19 |
| `33c82a18b7358c576f178b064835d469b3047fce` | product_commit_compare_residual | 6 / 5 |
| `35e8b169f5160fe9a41d15200d98137eaec81575` | product_commit_compare_residual | 5 / 3 |
| `41d7b6bdb97d5c8440c0385f40b486c44cd05bc3` | product_commit_compare_residual | 7 / 2 |
| `42378fc33089181111da3f999fb7acac8a791083` | product_commit_compare_residual | 1 / 1 |
| `434828fb7427dbc1f823b94074c897d0acc81901` | product_commit_compare_residual | 7 / 7 |
| `48cbefe9c850c3b0b08efb73a3e571ff5ff928c8` | product_commit_compare_residual | 6 / 5 |
| `4a6a28b6d8ef6ea6c81384b42c64b5db166fa7df` | synthetic_baseline_do_not_merge | 165 / 31 |
| `4bd4697f9155076e5e3748723e2ba9c3f16dc843` | product_commit_compare_residual | 3 / 1 |
| `5b61ecc7a251cea8e6e5aa3de1b6802ae87dcfd9` | product_commit_compare_residual | 1 / 1 |
| `5bc9183965cf23ebb5f04ad190adcd25a95383ab` | product_commit_compare_residual | 1 / 1 |
| `5c8ddcfc7c791bc1c85b5d1036e5960aa1743807` | review_candidate_snapshot_compare_residual_only | 9 / 4 |
| `638662310d76958bc81406a9a2f03c81b3b14fe5` | product_commit_compare_residual | 3 / 1 |
| `6ae01cf2f14beac8312d11c6d65785cc29e08053` | product_commit_compare_residual | 1 / 1 |
| `6b621958d540f429dd1ca450c852a20a9b2e03e7` | product_commit_compare_residual | 6 / 5 |
| `6d8c7cef78aa2363f826de40a3a5f7e50df372f6` | product_commit_compare_residual | 1 / 1 |
| `793f2af51373741ea7420a591a54a16453f9104d` | product_commit_compare_residual | 1 / 1 |
| `794d15108efa6f275b442f5b4b2fad15c7815877` | product_commit_compare_residual | 1 / 1 |
| `7bdfc72a2fbaaf2c8a7348b122bfe8cc3002ddf6` | product_commit_compare_residual | 1 / 1 |
| `7d746824f60481f059b277d0763fa7fc169dc847` | product_commit_compare_residual | 1 / 1 |
| `7df928ca3da32cc1a07f13094066a4c6cd29fa0a` | product_commit_compare_residual | 2 / 0 |
| `817bde8c6777564c36318295be8f5c5538c23896` | product_commit_compare_residual | 2 / 0 |
| `86093c231b476054cd9173b4eb87d30fef536c47` | product_commit_compare_residual | 5 / 2 |
| `88620eb4dca423940096b68e2a18fd5e90fe7a2f` | review_candidate_snapshot_compare_residual_only | 37 / 6 |
| `89b448473ea741d971cb92de52570419d530274e` | product_commit_compare_residual | 6 / 5 |
| `8bcada00bb7d8b29bf3fea8b74ce88af44a97208` | product_commit_compare_residual | 2 / 2 |
| `8ddd93a3e678a493c0bb6d481f3e2b107aeb32e1` | review_scope_snapshot_do_not_merge_whole | 32 / 9 |
| `9677e026724bd1ae6dd8a5a3fab96530fd30b757` | review_candidate_snapshot_compare_residual_only | 151 / 34 |
| `970f6272a487f9f15c806f552a8a207e6eed7147` | product_commit_compare_residual | 7 / 2 |
| `9a952d1aa0ad6b0092ad5482d91b7a7219ec1fee` | product_commit_compare_residual | 3 / 3 |
| `9c5cb379d731b9f961e1a221d151da86f7247ca4` | synthetic_baseline_do_not_merge | 166 / 30 |
| `9fdb868860340069ca87ac1316a043a68035eb25` | product_commit_compare_residual | 1 / 1 |
| `a504e9bbc52f30d5aef4c335666dd9a4529ef143` | product_commit_compare_residual | 4 / 4 |
| `a8d9750ebe0a877d096062d88aee643f117cc940` | product_commit_compare_residual | 1 / 1 |
| `b33950bc12492ecee0b58211be44d351183b7344` | product_commit_compare_residual | 2 / 0 |
| `b5f457f45e7b53f88b632b475dc7ba44fd54bde9` | product_commit_compare_residual | 1 / 0 |
| `b6a38c51a3ad74684acf1bd4994d5825d5692352` | product_commit_compare_residual | 1 / 1 |
| `b7361b699dd1dea2a8900a51a4446fb3810224f4` | product_commit_compare_residual | 8 / 6 |
| `bb44863bae463d985bd77f181c52a2c21a76dcd9` | product_commit_compare_residual | 1 / 1 |
| `bbd5eb58e82051f531113d5c33a4e3ddba147e43` | product_commit_compare_residual | 3 / 2 |
| `bebd497c4d7856d58862033533e49ee87ebe0011` | review_scope_snapshot_do_not_merge_whole | 41 / 9 |
| `c8ee583a13fab8a183225403bd42f61158f1a992` | product_commit_compare_residual | 4 / 3 |
| `ce797ce4422da73792c1539c307b0efe6721761a` | review_scope_snapshot_do_not_merge_whole | 44 / 12 |
| `d1697aeac426cc176ffc480c07c097e6893e1cad` | product_commit_compare_residual | 11 / 5 |
| `dcd420638fc4e28403af7af2c7a0a861aed010b2` | product_commit_compare_residual | 5 / 3 |
| `e150a75527e907089e6ce4c7be01e55149de4ee1` | product_commit_compare_residual | 1 / 1 |
| `e449ecb865d4203e2a56de93c277efba02a95d90` | product_commit_compare_residual | 5 / 2 |
| `e714b9d23947f2de48c184f17933ac029b0d4f3a` | product_commit_compare_residual | 4 / 1 |
| `eaacfe9f376e1f49a96170564ed26b3bdc3c4736` | review_candidate_snapshot_compare_residual_only | 1 / 1 |
| `eaf053ba2347f1aeb4f64752364728394372fd71` | product_commit_compare_residual | 4 / 2 |
| `f1c2d7c902f68f7d862a3747702f35b4996e8fc7` | product_commit_compare_residual | 5 / 3 |
| `f79dc6ff619c17d53b8d7dd038045cda6c0364cd` | product_commit_compare_residual | 1 / 1 |
| `faba2e2aac2f2885b6dad55fbabb39936588a9dc` | product_commit_compare_residual | 2 / 2 |

## 元WTの変更パス判定

release自身の作業中ファイルは親の実装台帳で追跡する。以下はそれ以外の全dirty/untrackedパス。archive一致や逆パッチ成功を新規変更として重ねて適用しない。

### `LIVE`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `docs/guides/INSTALLATION.md` | 現release一致 |
| ` M` | `docs/guides/UPGRADE_AGENT.md` | 逆パッチ検査成功 |
| ` M` | `hermes_plugin/card_workers.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/slack/test_mcs_slack.py` | 後続変更との意味照合が必要 |
| `??` | `changes/slack-profile-worker.json` | 現release一致 |

### `STATE/mcs-hotfix-summary-20261009/baseline`

| 状態 | パス | 判定 |
|---|---|---|
| `??` | `tests/extract/test_rule_summary_sentence_context.py` | 同名あり・内容差 |

### `WT/audit-v1.0.20-claude`

| 状態 | パス | 判定 |
|---|---|---|
| `??` | `.owner.lock` | 所有印・保持 |

### `WT/feat-v1.0.21-operations`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `.github/workflows/ci.yml` | 現release一致 |
| ` M` | `CHANGELOG.md` | 後続変更との意味照合が必要 |
| ` M` | `README.md` | 後続変更との意味照合が必要 |
| ` M` | `adapters/common/journal.py` | 現release一致 |
| ` M` | `adapters/common/text.py` | 逆パッチ検査成功 |
| ` D` | `changes/plugin-event-logger-repo-root.json` | 逆パッチ検査成功 |
| ` M` | `deployment/recovery/mcs_recover.py` | 逆パッチ検査成功 |
| ` M` | `docs/development/DEVELOPMENT.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/development/RELEASE_NOTES.md` | 現release一致 |
| ` M` | `docs/development/readme-review.json` | 後続変更との意味照合が必要 |
| ` M` | `docs/guides/INSTALLATION.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/guides/UPGRADE_AGENT.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/guides/USER_GUIDE.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/specs/official-drug-master.md` | 現release一致 |
| ` M` | `install.sh` | 逆パッチ検査成功 |
| ` M` | `mcs/core/local_llm.py` | 逆パッチ検査成功 |
| ` M` | `mcs/core/maintenance.py` | 現release一致 |
| ` M` | `mcs/core/mcs_queries.py` | 逆パッチ検査成功 |
| ` M` | `mcs/extract/drug_map.py` | 逆パッチ検査成功 |
| ` M` | `mcs/extract/patient_context.py` | 逆パッチ検査成功 |
| ` M` | `mcs/extract/v1/extract.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ingest/health_watch.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ingest/mcs_adapter.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ingest/run_check.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/notify/notify_cards.py` | 逆パッチ検査成功 |
| ` M` | `mcs/notify/notify_render.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ops/import_drug_master.py` | 現release一致 |
| ` M` | `mcs/ops/mcs_drug.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/ops/mcs_operations.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ops/mcs_repair.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ops/mcs_setup.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/ops/mcs_update.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/ops/semantic_observe.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_drain.py` | 逆パッチ検査成功 |
| ` M` | `mcs/semantic/semantic_jev.py` | 現release一致 |
| ` M` | `mcs/semantic/semantic_metrics.py` | 現release一致 |
| ` M` | `mcs/views/mcs_stats.py` | 逆パッチ検査成功 |
| ` M` | `mcs/views/mcs_view.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/views/read_model.py` | 後続変更との意味照合が必要 |
| ` M` | `resources/drug-master/README.md` | 現release一致 |
| ` M` | `tests/adapters/common/test_answer_literal_text.py` | 現release一致 |
| ` M` | `tests/adapters/common/test_deep_local_state.py` | 現release一致 |
| ` M` | `tests/adapters/common/test_worker_reconcile_integrity.py` | 現release一致 |
| ` M` | `tests/adapters/discord/discord_delivery_testkit.py` | 現release一致 |
| ` M` | `tests/adapters/discord/discord_testkit.py` | 現release一致 |
| ` M` | `tests/adapters/discord/test_discord_alert_thread_notice.py` | 現release一致 |
| ` M` | `tests/adapters/discord/test_discord_confirm_followup.py` | 現release一致 |
| ` M` | `tests/adapters/discord/test_discord_edit_only_parts.py` | 現release一致 |
| ` M` | `tests/adapters/discord/test_followup_current_role.py` | 現release一致 |
| ` M` | `tests/adapters/discord/test_mcs_discord.py` | 逆パッチ検査成功 |
| ` M` | `tests/adapters/discord/test_mcs_discord_delivery.py` | 現release一致 |
| ` M` | `tests/adapters/discord/test_stab_journal_no_backup_horizon.py` | 現release一致 |
| ` M` | `tests/adapters/discord/test_task_answer_budget.py` | 現release一致 |
| ` M` | `tests/adapters/lineworks/test_drug_navigation.py` | 現release一致 |
| ` M` | `tests/adapters/lineworks/test_lineworks_actions_review.py` | 現release一致 |
| ` M` | `tests/adapters/lineworks/test_lineworks_adapter.py` | 逆パッチ検査成功 |
| ` M` | `tests/adapters/lineworks/test_lineworks_client.py` | 逆パッチ検査成功 |
| ` M` | `tests/adapters/lineworks/test_lineworks_plain_views.py` | 現release一致 |
| ` M` | `tests/adapters/lineworks/test_lineworks_runtime.py` | 現release一致 |
| ` M` | `tests/adapters/lineworks/test_lineworks_silent_text.py` | 現release一致 |
| ` M` | `tests/adapters/lineworks/test_registry_delta.py` | 逆パッチ検査成功 |
| ` M` | `tests/adapters/lineworks/test_revoked_card_pending_parts.py` | 現release一致 |
| ` M` | `tests/adapters/lineworks/test_same_pass_revoke.py` | 現release一致 |
| ` M` | `tests/adapters/lineworks/test_shutdown_cleanup.py` | 現release一致 |
| ` M` | `tests/adapters/lineworks/test_stab_callback_receipt_ttl.py` | 現release一致 |
| ` M` | `tests/adapters/lineworks/test_stab_token_expiry.py` | 現release一致 |
| ` M` | `tests/adapters/slack/slack_testkit.py` | 現release一致 |
| ` M` | `tests/adapters/slack/test_mcs_slack.py` | 逆パッチ検査成功 |
| ` M` | `tests/adapters/slack/test_mcs_slack_actions.py` | 逆パッチ検査成功 |
| ` M` | `tests/adapters/slack/test_queued_view_scope_narrowed.py` | 現release一致 |
| ` M` | `tests/adapters/slack/test_rate_retry_authority.py` | 逆パッチ検査成功 |
| ` M` | `tests/adapters/slack/test_signal_thread_root_regression.py` | 現release一致 |
| ` M` | `tests/adapters/slack/test_slack_delayed_reply_flow.py` | 逆パッチ検査成功 |
| ` M` | `tests/adapters/slack/test_slack_edit_only_parts.py` | 現release一致 |
| ` M` | `tests/adapters/slack/test_slack_human_receipt.py` | 現release一致 |
| ` M` | `tests/adapters/slack/test_slack_late_reply_broadcast.py` | 現release一致 |
| ` M` | `tests/adapters/standalone/test_native_commands.py` | 現release一致 |
| ` M` | `tests/adapters/standalone/test_standalone_error_codes.py` | 現release一致 |
| ` M` | `tests/adapters/standalone/test_standalone_runtime.py` | 逆パッチ検査成功 |
| ` M` | `tests/core/test_llm_admission.py` | 逆パッチ検査成功 |
| ` M` | `tests/core/test_stab_db_maintenance.py` | 現release一致 |
| ` M` | `tests/extract/test_drug_map.py` | 現release一致 |
| ` M` | `tests/extract/test_patient_context.py` | 現release一致 |
| ` M` | `tests/fixtures/schema_upgrade/origins.json` | 後続変更との意味照合が必要 |
| ` M` | `tests/fixtures/schema_upgrade/update-paths.json` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_drug_map_pipeline.py` | 現release一致 |
| ` M` | `tests/ingest/test_run_check_stages.py` | 逆パッチ検査成功 |
| ` M` | `tests/ingest/test_stab_worker_reap.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_thread_read.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/meta/test_drug_master_bundle.py` | 現release一致 |
| ` M` | `tests/meta/test_install_runtime_migration.py` | 現release一致 |
| ` M` | `tests/meta/test_install_sh.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/notify_testkit.py` | 現release一致 |
| ` M` | `tests/notify/test_alert_thread_hotfix.py` | 現release一致 |
| ` M` | `tests/notify/test_card_buttons.py` | 現release一致 |
| ` M` | `tests/notify/test_card_drug_views.py` | 現release一致 |
| ` M` | `tests/notify/test_card_layout.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_gc_mixed_parts.py` | 現release一致 |
| ` M` | `tests/notify/test_hold_event_progress.py` | 現release一致 |
| ` M` | `tests/notify/test_layout1_urgency_label.py` | 現release一致 |
| ` M` | `tests/notify/test_notification_reliability.py` | 現release一致 |
| ` M` | `tests/notify/test_notify_cards.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_cards_regressions.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_command_dependencies.py` | 現release一致 |
| ` M` | `tests/notify/test_notify_digest.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_flush.py` | 現release一致 |
| ` M` | `tests/notify/test_notify_lineworks.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_lineworks_flush.py` | 現release一致 |
| ` M` | `tests/notify/test_notify_parts.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_notify_progress_render.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_reconcile.py` | 現release一致 |
| ` M` | `tests/notify/test_notify_render.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_slack.py` | 現release一致 |
| ` M` | `tests/notify/test_notify_standalone_flush.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_summary_notice.py` | 現release一致 |
| ` M` | `tests/notify/test_notify_urgent.py` | 現release一致 |
| ` M` | `tests/notify/test_post_actions.py` | 現release一致 |
| ` M` | `tests/notify/test_rebind_revoked_card.py` | 現release一致 |
| ` M` | `tests/notify/test_reconcile_unreadable_text_progress.py` | 現release一致 |
| ` M` | `tests/notify/test_restore_journal_shape.py` | 現release一致 |
| ` M` | `tests/notify/test_signal_fallback_idempotency.py` | 現release一致 |
| ` M` | `tests/notify/test_signal_notify_off.py` | 現release一致 |
| ` M` | `tests/notify/test_signal_thread_hotfix.py` | 現release一致 |
| ` M` | `tests/notify/test_stamps_display.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_summary_rendering_regressions.py` | 現release一致 |
| ` M` | `tests/notify/test_task_reminder_send_gate.py` | 現release一致 |
| ` M` | `tests/notify/test_urgent_payload_invalid.py` | 現release一致 |
| ` M` | `tests/notify/test_urgent_route_regression.py` | 現release一致 |
| ` M` | `tests/ops/ops_testkit.py` | 現release一致 |
| ` M` | `tests/ops/test_import_drug_master.py` | 現release一致 |
| ` M` | `tests/ops/test_jev_value_input_boundary.py` | 現release一致 |
| ` M` | `tests/ops/test_mcs_drug_bundled.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ops/test_mcs_repair.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ops/test_mcs_setup_doctor.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ops/test_mcs_update.py` | 逆パッチ検査成功 |
| ` M` | `tests/release/test_sync_release_notes.py` | 現release一致 |
| ` M` | `tests/semantic/semantic_testkit.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_semantic_fairness.py` | 現release一致 |
| ` M` | `tests/semantic/test_semantic_metrics.py` | 現release一致 |
| ` M` | `tests/semantic/test_semantic_observe.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/views/test_qc_view.py` | 後続変更との意味照合が必要 |
| `??` | `changes/archive/1.0.21/backup-marker-reader-unification.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/drug-ref-source-validation.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/health-status-reader-boundary.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/hermes-companion-supply.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/hermes-v0217-pin.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/jev-response-json-boundary.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/llm-admission-payload-preflight.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/plugin-event-logger-repo-root.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/private-output-directory-durability.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/qc-view-item-boundary.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/queue-age-invalid-timestamps.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/recovery-installer-control-file-boundaries.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/refstat-rollback-directory-durability.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/setup-input-file-boundaries.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/signal-suppression-window-validation.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/thread-read-partial-evidence.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/updater-control-file-boundaries.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/v1021-claude-delivery-journal-fd-cleanup.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/v1021-claude-delivery-task-title-literal.json` | 同名あり・内容差 |
| `??` | `changes/archive/1.0.21/v1021-drain-observation.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/v1021-drug-map-config.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/v1021-drug-map-failure.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/v1021-drug-master-20261013.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/v1021-health-integration.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/v1021-master-update-safety.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/v1021-notify-wait-reasons.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/v1021-operational-progress.json` | 同名あり・内容差 |
| `??` | `changes/archive/1.0.21/v1021-operational-status.json` | 現release一致 |
| `??` | `changes/archive/1.0.21/v1021-pending-diagnosis-correction.json` | 現release一致 |
| `??` | `docs/development/acceptance/ACCEPTANCE_1.0.21.md` | 同名あり・内容差 |
| `??` | `docs/development/plans/RELEASE_1.0.21.md` | 同名あり・内容差 |
| `??` | `docs/releases/v1.0.21/README.md` | 現release一致 |
| `??` | `docs/releases/v1.0.21/assets.json` | 同名あり・内容差 |
| `??` | `docs/releases/v1.0.21/operations-check.png` | 現release一致 |
| `??` | `docs/releases/v1.0.21/operations-check.svg` | 現release一致 |
| `??` | `mcs/ops/operational_progress.py` | 現release一致 |
| `??` | `mcs/ops/operational_status.py` | 現release一致 |
| `??` | `resources/drug-master/20261013/manifest.json` | 現release一致 |
| `??` | `resources/drug-master/20261013/y_20261013.zip` | 現release一致 |
| `??` | `tests/adapters/common/test_journal_append_cleanup.py` | 現release一致 |
| `??` | `tests/adapters/lineworks/lineworks_client_testkit.py` | 現release一致 |
| `??` | `tests/adapters/lineworks/lineworks_testkit.py` | 現release一致 |
| `??` | `tests/adapters/slack/slack_actions_testkit.py` | 現release一致 |
| `??` | `tests/adapters/slack/test_slack_session_boundary.py` | 現release一致 |
| `??` | `tests/adapters/standalone/standalone_testkit.py` | 現release一致 |
| `??` | `tests/adapters/standalone/test_standalone_credential_boundary.py` | 現release一致 |
| `??` | `tests/core/test_extraction_eligibility.py` | 現release一致 |
| `??` | `tests/core/test_local_backup_status.py` | 現release一致 |
| `??` | `tests/fixtures/schema_upgrade/shape-11.sql` | 現release一致 |
| `??` | `tests/ingest/test_health_watch_file_boundary.py` | 現release一致 |
| `??` | `tests/ingest/test_health_watch_wait_reasons.py` | 同名あり・内容差 |
| `??` | `tests/ingest/test_operational_health.py` | 現release一致 |
| `??` | `tests/meta/test_install_config_reader.py` | 現release一致 |
| `??` | `tests/meta/test_recovery_local_state_reads.py` | 現release一致 |
| `??` | `tests/notify/notify_lineworks_testkit.py` | 現release一致 |
| `??` | `tests/notify/notify_reconcile_testkit.py` | 現release一致 |
| `??` | `tests/notify/notify_urgent_testkit.py` | 現release一致 |
| `??` | `tests/notify/test_pending_wait_reasons.py` | 現release一致 |
| `??` | `tests/ops/test_mcs_setup_drug_map.py` | 現release一致 |
| `??` | `tests/ops/test_observe_queue_timestamps.py` | 現release一致 |
| `??` | `tests/ops/test_operational_progress.py` | 現release一致 |
| `??` | `tests/ops/test_operational_status.py` | 現release一致 |
| `??` | `tests/ops/test_private_output_durability.py` | 同名あり・内容差 |
| `??` | `tests/ops/test_refstat_rollback_durability.py` | 現release一致 |
| `??` | `tests/ops/test_restart_runtime_boundary.py` | 現release一致 |
| `??` | `tests/ops/test_setup_evidence_input.py` | 現release一致 |
| `??` | `tests/ops/test_update_file_boundaries.py` | 現release一致 |
| `??` | `tests/semantic/test_jev_json_boundary.py` | 現release一致 |
| `??` | `tests/views/test_mcs_stats_suppression_boundary.py` | 現release一致 |

### `WT/fix-retired-extraction-import-roots`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `docs/development/DEVELOPMENT.md` | 後続変更との意味照合が必要 |
| ` M` | `mcs/_mcs_path.py` | 現release一致 |
| `??` | `changes/retired-extraction-import-roots.json` | archive一致 |
| `??` | `tests/meta/test_mcs_path_retired_extraction.py` | 現release一致 |

### `WT/hermes-v0217-pin`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `.github/workflows/ci.yml` | 後続変更との意味照合が必要 |
| ` M` | `docs/guides/INSTALLATION.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/guides/UPGRADE_AGENT.md` | 後続変更との意味照合が必要 |
| ` M` | `install.sh` | 後続変更との意味照合が必要 |
| ` M` | `mcs/ops/mcs_update.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/meta/test_install_runtime_migration.py` | 現release一致 |
| `??` | `changes/hermes-v0217-pin.json` | 同名なし |

### `WT/hotfix-rollup-reobserve`

| 状態 | パス | 判定 |
|---|---|---|
| ` A` | `changes/rollup-reobserve-throttle.json` | 後続変更との意味照合が必要 |
| ` M` | `docs/development/DEVELOPMENT.md` | 後続変更との意味照合が必要 |
| ` M` | `mcs/extract/rollup.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/ops/mcs_repair.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/extract/test_bugfix_r2_5_rollup_dirty.py` | 後続変更との意味照合が必要 |
| ` A` | `tests/extract/test_rollup_reobserve.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ops/test_mcs_repair.py` | 後続変更との意味照合が必要 |

### `WT/pharmacy-chat-coverage-w1`

| 状態 | パス | 判定 |
|---|---|---|
| `??` | `docs/specs/pharmacy-visit-information.md` | 同名あり・内容差 |
| `??` | `mcs/extract/eval/pattern_cards.py` | 同名あり・内容差 |
| `??` | `mcs/extract/pharmacy_cues.py` | 同名あり・内容差 |
| `??` | `tests/extract/test_pattern_cards.py` | 同名あり・内容差 |
| `??` | `tests/extract/test_pharmacy_cues.py` | 同名あり・内容差 |

### `WT/quality-v9-efficiency`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `mcs/extract/rollup.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_audit.py` | 逆パッチ検査成功 |
| ` M` | `mcs/semantic/semantic_drain.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_projection.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_runtime.py` | 逆パッチ検査成功 |
| ` M` | `mcs/semantic/semantic_store.py` | 逆パッチ検査成功 |
| ` M` | `mcs/semantic/semantic_v4.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/views/read_model.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/extract/test_drug_map.py` | 逆パッチ検査成功 |
| ` M` | `tests/extract/test_extract_llm.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/extract/test_extract_packet4_regressions.py` | 逆パッチ検査成功 |
| ` M` | `tests/extract/test_rollup_canonical.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_canonical_projection.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_canonical_request_following.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/semantic/test_semantic_drain_scan_bounds.py` | 現release一致 |
| ` M` | `tests/semantic/test_semantic_persisted_boundaries.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_semantic_v4.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/views/test_mcs_stats.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/views/test_project_metadata_comparison.py` | 現release一致 |
| ` M` | `tests/views/test_read_model.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/views/test_structured_view.py` | 逆パッチ検査成功 |
| `??` | `changes/semantic-daily-reservation-budget-20261008.json` | archive一致 |
| `??` | `changes/semantic-invalidation-cadence-20261008.json` | archive一致 |
| `??` | `changes/semantic-published-quantity-20261008.json` | archive一致 |
| `??` | `tests/extract/test_rollup_projection_version.py` | 同名あり・内容差 |
| `??` | `tests/semantic/test_fact_quantity_publication.py` | 現release一致 |
| `??` | `tests/semantic/test_semantic_daily_cap.py` | 同名あり・内容差 |

### `WT/quality-v9-stability`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `mcs/notify/notify_cmds.py` | 逆パッチ検査成功 |
| ` M` | `tests/adapters/discord/test_mcs_discord.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_card_buttons.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_card_delivery_and_view_regressions.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_drain_int_sweep.py` | 現release一致 |
| ` M` | `tests/notify/test_notify_cards.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_urgent.py` | 後続変更との意味照合が必要 |
| `??` | `changes/notification-receipt-window-20261008.json` | archive一致 |
| `??` | `tests/notify/test_notify_command_dependencies.py` | 同名あり・内容差 |

### `WT/quality-v9-structure`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `deployment/recovery/mcs_recover.py` | 逆パッチ検査成功 |
| ` M` | `mcs/core/mcs_queries.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/extract/test_rollup_canonical.py` | 逆パッチ検査成功 |
| ` M` | `tests/meta/test_gateway_restart_identity.py` | 現release一致 |
| ` M` | `tests/ops/test_c1_records.py` | 現release一致 |
| ` M` | `tests/ops/test_mcs_features.py` | 現release一致 |
| ` M` | `tests/ops/test_mcs_signals.py` | 後続変更との意味照合が必要 |
| `??` | `changes/hermes-runtime-bootstrap-identity-20261008.json` | archive一致 |
| `??` | `tests/core/test_projection_version_gate.py` | 現release一致 |

### `WT/v9-rc-20261009-efficiency`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `docs/guides/UPGRADE_AGENT.md` | 後続変更との意味照合が必要 |
| ` M` | `evaluation/canonical_request_eval.py` | 逆パッチ検査成功 |
| ` M` | `mcs/core/ledger.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/core/local_llm.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/core/mcs_queries.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/core/mcs_util.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ingest/health_watch.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ingest/mcs_adapter.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ingest/run_check.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/notify/notify_cards.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/notify/notify_digest.py` | 逆パッチ検査成功 |
| ` M` | `mcs/notify/notify_flush.py` | 逆パッチ検査成功 |
| ` M` | `mcs/notify/notify_render.py` | 逆パッチ検査成功 |
| ` M` | `mcs/notify/notify_urgent.py` | 逆パッチ検査成功 |
| ` M` | `mcs/notify/notify_views.py` | 逆パッチ検査成功 |
| ` M` | `mcs/semantic/semantic_bench.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_render.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/views/mcs_stats.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/views/structured_view.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/core/test_core_misc.py` | 逆パッチ検査成功 |
| ` M` | `tests/core/test_job_reasons.py` | 現release一致 |
| ` M` | `tests/core/test_llm_admission.py` | 逆パッチ検査成功 |
| ` M` | `tests/core/test_maintenance_retention_cleanup.py` | 逆パッチ検査成功 |
| ` M` | `tests/core/test_mcs_util.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/core/test_message_revisions.py` | 現release一致 |
| ` M` | `tests/core/test_thread_reply_pairs.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_adapter_wire.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_bugfix_r2_1_init_reauth_reached.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_bugfix_r3_0_init_deeper_since.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_health_stalls.py` | 逆パッチ検査成功 |
| ` M` | `tests/ingest/test_health_watch.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_job_drain.py` | 逆パッチ検査成功 |
| ` M` | `tests/ingest/test_reaction_actors.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_reply_jobs_cmd_quarantine_housekeeping.py` | 逆パッチ検査成功 |
| ` M` | `tests/ingest/test_run_check_stages.py` | 逆パッチ検査成功 |
| ` M` | `tests/ingest/test_semantic_ingest_revisions.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_thread_read.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_thread_read_reply_failed.py` | 現release一致 |
| ` M` | `tests/ingest/test_unread_notify_paths.py` | 現release一致 |
| ` M` | `tests/notify/test_bugfix_r1_4_notify_cards.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_metadata_display.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_digest.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_notify_render.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_notify_standalone_flush.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_notify_urgent.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_views_search_fold.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_summary_unreadable_karte_summary.py` | 現release一致 |
| ` M` | `tests/semantic/test_canonical_request_evaluation.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_semantic_bench.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_semantic_degraded_coverage.py` | 現release一致 |
| ` M` | `tests/views/test_clinical_summary_source_scope.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/views/test_interaction_latency.py` | 現release一致 |
| ` M` | `tests/views/test_view_corrupt_state.py` | 現release一致 |
| `??` | `changes/main-analysis-stored-json.json` | archive一致 |
| `??` | `changes/main-evaluation-legacy-json.json` | archive一致 |
| `??` | `changes/main-urgent-policy-number-boundary.json` | archive一致 |
| `??` | `changes/v9-eff-admission-cancellation-cleanup.json` | 同名なし |
| `??` | `changes/v9-eff-attachment-seal-retention.json` | 同名なし |
| `??` | `changes/v9-eff-card-source-retention.json` | 同名なし |
| `??` | `changes/v9-eff-coverage-query.json` | 同名なし |
| `??` | `changes/v9-eff-digest-scope-integer-boundary.json` | 同名なし |
| `??` | `changes/v9-eff-health-history-copy.json` | 同名なし |
| `??` | `changes/v9-eff-ledger-lookups.json` | 同名なし |
| `??` | `changes/v9-eff-medication-presence.json` | 同名なし |
| `??` | `changes/v9-eff-search-retention.json` | 同名なし |
| `??` | `changes/v9-eff-source-preserving-chunks.json` | 同名なし |
| `??` | `changes/v9-eff-thread-acquisition-scope.json` | 同名なし |
| `??` | `changes/v9-eff-websocket-initialization-cleanup.json` | 同名なし |

### `WT/v9-rc-20261009-integration`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `CHANGELOG.md` | 逆パッチ検査成功 |
| ` M` | `README.md` | 後続変更との意味照合が必要 |
| ` M` | `adapters/common/journal.py` | 後続変更との意味照合が必要 |
| ` M` | `adapters/lineworks/client.py` | 逆パッチ検査成功 |
| ` M` | `adapters/lineworks/delivery.py` | 逆パッチ検査成功 |
| ` M` | `adapters/slack/standalone.py` | 逆パッチ検査成功 |
| ` M` | `adapters/slack/tasks.py` | 逆パッチ検査成功 |
| ` M` | `deployment/README.md` | 後続変更との意味照合が必要 |
| ` M` | `deployment/recovery/mcs_recover.py` | 逆パッチ検査成功 |
| ` M` | `docs/development/DEVELOPMENT.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/guides/BACKUP.md` | 逆パッチ検査成功 |
| ` M` | `docs/guides/INSTALLATION.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/guides/LINEWORKS.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/guides/SETUP_AGENT.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/guides/STANDALONE.md` | 逆パッチ検査成功 |
| ` M` | `docs/guides/UPGRADE_AGENT.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/guides/USER_GUIDE.md` | 逆パッチ検査成功 |
| ` M` | `docs/roadmap/digest.md` | 現release一致 |
| ` M` | `docs/roadmap/karte-summary.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/screenshots/discord-card.svg` | 現release一致 |
| ` M` | `docs/screenshots/discord-thread.svg` | 現release一致 |
| ` M` | `docs/screenshots/signals-cli.svg` | 現release一致 |
| ` M` | `docs/screenshots/view-status.svg` | 現release一致 |
| ` M` | `docs/specs/lifecycle-spec.md` | 後続変更との意味照合が必要 |
| ` M` | `docs/specs/semantic-evaluation.md` | 逆パッチ検査成功 |
| ` M` | `docs/specs/semantic-facts-v2-rollout.md` | 逆パッチ検査成功 |
| ` M` | `evaluation/canonical_request_eval.py` | 逆パッチ検査成功 |
| ` M` | `hermes_plugin/README.md` | 逆パッチ検査成功 |
| ` M` | `integration/test_standalone_sdk.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/core/ledger.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/core/local_llm.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/core/mcs_queries.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/core/mcs_util.py` | 逆パッチ検査成功 |
| ` M` | `mcs/extract/clinical_values.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/extract/drug_map.py` | 逆パッチ検査成功 |
| ` M` | `mcs/extract/v1/extract.py` | 逆パッチ検査成功 |
| ` M` | `mcs/extract/v4/extract_llm.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/ingest/health_watch.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ingest/mcs_adapter.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ingest/run_check.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/notify/notify_cards.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/notify/notify_digest.py` | 逆パッチ検査成功 |
| ` M` | `mcs/notify/notify_flush.py` | 逆パッチ検査成功 |
| ` M` | `mcs/notify/notify_render.py` | 逆パッチ検査成功 |
| ` M` | `mcs/notify/notify_urgent.py` | 逆パッチ検査成功 |
| ` M` | `mcs/notify/notify_views.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ops/mcs_repair.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ops/mcs_setup.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/ops/mcs_signals.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ops/mcs_update.py` | 逆パッチ検査成功 |
| ` M` | `mcs/semantic/semantic.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_assessment.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_bench.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_drain.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_extraction.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_loops.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_render.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_v4.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/views/mcs_stats.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/views/structured_view.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs_standalone/config.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/common/test_journal_phase_recovery.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/lineworks/test_bugfix_r3_15_part_cooldown.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/lineworks/test_lineworks_client.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/slack/test_slack_alert_thread_notice.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/standalone/test_standalone_runtime.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/core/test_core_misc.py` | 逆パッチ検査成功 |
| ` M` | `tests/core/test_job_reasons.py` | 現release一致 |
| ` M` | `tests/core/test_llm_admission.py` | 逆パッチ検査成功 |
| ` M` | `tests/core/test_maintenance_retention_cleanup.py` | 逆パッチ検査成功 |
| ` M` | `tests/core/test_mcs_util.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/core/test_message_revisions.py` | 現release一致 |
| ` M` | `tests/core/test_thread_reply_pairs.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/extract/test_bp_pair_guard.py` | 現release一致 |
| ` M` | `tests/extract/test_clinical_chunk_planning.py` | 逆パッチ検査成功 |
| ` M` | `tests/extract/test_detail_clinical_extraction.py` | 逆パッチ検査成功 |
| ` M` | `tests/fixtures/schema_upgrade/origins.json` | 後続変更との意味照合が必要 |
| ` M` | `tests/fixtures/schema_upgrade/update-paths.json` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_adapter_wire.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_bugfix_r2_1_init_reauth_reached.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_bugfix_r3_0_init_deeper_since.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_health_stalls.py` | 逆パッチ検査成功 |
| ` M` | `tests/ingest/test_health_watch.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_job_drain.py` | 逆パッチ検査成功 |
| ` M` | `tests/ingest/test_reaction_actors.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_reply_jobs_cmd_quarantine_housekeeping.py` | 逆パッチ検査成功 |
| ` M` | `tests/ingest/test_run_check_stages.py` | 逆パッチ検査成功 |
| ` M` | `tests/ingest/test_semantic_ingest_revisions.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_thread_read.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ingest/test_thread_read_reply_failed.py` | 現release一致 |
| ` M` | `tests/ingest/test_unread_notify_paths.py` | 現release一致 |
| ` M` | `tests/meta/test_mcs_recover.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_bugfix_r1_4_notify_cards.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_metadata_display.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_digest.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_notify_render.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_notify_standalone_flush.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_notify_urgent.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_views_search_fold.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_summary_unreadable_karte_summary.py` | 現release一致 |
| ` M` | `tests/ops/test_mcs_setup.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ops/test_mcs_setup_standalone.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ops/test_mcs_update.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_canonical_request_evaluation.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_semantic_bench.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_semantic_degraded_coverage.py` | 現release一致 |
| ` M` | `tests/views/test_clinical_summary_source_scope.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/views/test_drug_candidates.py` | 現release一致 |
| ` M` | `tests/views/test_interaction_latency.py` | 現release一致 |
| ` M` | `tests/views/test_view_corrupt_state.py` | 現release一致 |
| `??` | `changes/hotfix-semantic-backlog-budget.json` | 同名なし |
| `??` | `changes/main-analysis-stored-json.json` | archive一致 |
| `??` | `changes/main-evaluation-legacy-json.json` | archive一致 |
| `??` | `changes/main-urgent-policy-number-boundary.json` | archive一致 |
| `??` | `changes/v10-recovery-owned-resources.json` | 同名なし |
| `??` | `changes/v9-eff-admission-cancellation-cleanup.json` | 同名なし |
| `??` | `changes/v9-eff-attachment-seal-retention.json` | 同名なし |
| `??` | `changes/v9-eff-card-source-retention.json` | 同名なし |
| `??` | `changes/v9-eff-coverage-query.json` | 同名なし |
| `??` | `changes/v9-eff-digest-scope-integer-boundary.json` | 同名なし |
| `??` | `changes/v9-eff-health-history-copy.json` | 同名なし |
| `??` | `changes/v9-eff-ledger-lookups.json` | 同名なし |
| `??` | `changes/v9-eff-medication-presence.json` | 同名なし |
| `??` | `changes/v9-eff-search-retention.json` | 同名なし |
| `??` | `changes/v9-eff-source-preserving-chunks.json` | 同名なし |
| `??` | `changes/v9-eff-thread-acquisition-scope.json` | 同名なし |
| `??` | `changes/v9-eff-websocket-initialization-cleanup.json` | 同名なし |
| `??` | `changes/v9-stability-journal-failed-inspection.json` | 同名なし |
| `??` | `changes/v9-stability-lineworks-numeric-state.json` | archive一致 |
| `??` | `changes/v9-stability-repair-log-cleanup.json` | 同名なし |
| `??` | `changes/v9-stability-service-retirement-proof.json` | 同名なし |
| `??` | `changes/v9-stability-signal-response-queries.json` | archive一致 |
| `??` | `changes/v9-stability-slack-history-boundary.json` | 同名なし |
| `??` | `changes/v9-stability-slack-start-ownership.json` | archive一致 |
| `??` | `changes/v9-stability-standalone-job-signal-status.json` | 同名なし |
| `??` | `changes/v9-stability-standalone-private-input.json` | 同名なし |
| `??` | `changes/v9-stability-update-failed-resource-cleanup.json` | 同名なし |
| `??` | `changes/v9-structure-artifact-kinds.json` | 同名なし |
| `??` | `changes/v9-structure-completeness-quality.json` | 同名なし |
| `??` | `changes/v9-structure-stats-ref-progress.json` | 同名なし |
| `??` | `changes/v9-structure-vital-source.json` | 同名なし |
| `??` | `docs/screenshots/discord-card.png` | 同名あり・内容差 |
| `??` | `docs/screenshots/discord-thread.png` | 同名あり・内容差 |
| `??` | `docs/screenshots/signals-cli.png` | 同名あり・内容差 |
| `??` | `docs/screenshots/view-status.png` | 同名あり・内容差 |
| `??` | `tests/adapters/slack/test_start_failure_retention.py` | 現release一致 |
| `??` | `tests/extract/test_completeness_quality.py` | 同名あり・内容差 |
| `??` | `tests/meta/test_recovery_failure_cleanup.py` | 同名なし |
| `??` | `tests/ops/test_repair_log_cleanup.py` | 同名なし |
| `??` | `tests/ops/test_setup_retirement_verification.py` | 同名なし |
| `??` | `tests/ops/test_signal_response_queries.py` | 現release一致 |
| `??` | `tests/ops/test_update_failure_cleanup.py` | 同名なし |
| `??` | `tests/ops/test_update_service_stop_verification.py` | 同名なし |
| `??` | `tests/semantic/test_coverage_completeness_repair.py` | 同名あり・内容差 |
| `??` | `tests/semantic/test_semantic_backlog_budget.py` | 同名あり・内容差 |

### `WT/v9-rc-20261009-security`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `adapters/common/journal.py` | 後続変更との意味照合が必要 |
| ` M` | `adapters/lineworks/client.py` | 逆パッチ検査成功 |
| ` M` | `adapters/lineworks/delivery.py` | 逆パッチ検査成功 |
| ` M` | `adapters/slack/standalone.py` | 逆パッチ検査成功 |
| ` M` | `adapters/slack/tasks.py` | 逆パッチ検査成功 |
| ` M` | `docs/guides/UPGRADE_AGENT.md` | 後続変更との意味照合が必要 |
| ` M` | `evaluation/canonical_request_eval.py` | 逆パッチ検査成功 |
| ` M` | `mcs/notify/notify_urgent.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ops/mcs_repair.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ops/mcs_setup.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/ops/mcs_signals.py` | 逆パッチ検査成功 |
| ` M` | `mcs/ops/mcs_update.py` | 逆パッチ検査成功 |
| ` M` | `mcs/semantic/semantic_bench.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_render.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/views/mcs_stats.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs_standalone/config.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/common/test_journal_phase_recovery.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/lineworks/test_bugfix_r3_15_part_cooldown.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/lineworks/test_lineworks_client.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/slack/test_slack_alert_thread_notice.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/adapters/standalone/test_standalone_runtime.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/notify/test_notify_urgent.py` | 逆パッチ検査成功 |
| ` M` | `tests/ops/test_mcs_setup.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ops/test_mcs_setup_standalone.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/ops/test_mcs_update.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_canonical_request_evaluation.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_semantic_bench.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_semantic_degraded_coverage.py` | 現release一致 |
| ` M` | `tests/views/test_interaction_latency.py` | 現release一致 |
| ` M` | `tests/views/test_view_corrupt_state.py` | 現release一致 |
| `??` | `changes/main-analysis-stored-json.json` | archive一致 |
| `??` | `changes/main-evaluation-legacy-json.json` | archive一致 |
| `??` | `changes/main-urgent-policy-number-boundary.json` | archive一致 |
| `??` | `changes/v9-stability-journal-failed-inspection.json` | 同名なし |
| `??` | `changes/v9-stability-lineworks-numeric-state.json` | archive一致 |
| `??` | `changes/v9-stability-repair-log-cleanup.json` | 同名なし |
| `??` | `changes/v9-stability-service-retirement-proof.json` | 同名なし |
| `??` | `changes/v9-stability-signal-response-queries.json` | archive一致 |
| `??` | `changes/v9-stability-slack-history-boundary.json` | 同名なし |
| `??` | `changes/v9-stability-slack-start-ownership.json` | archive一致 |
| `??` | `changes/v9-stability-standalone-job-signal-status.json` | 同名なし |
| `??` | `changes/v9-stability-standalone-private-input.json` | 同名なし |
| `??` | `changes/v9-stability-update-failed-resource-cleanup.json` | 同名なし |
| `??` | `tests/adapters/slack/test_start_failure_retention.py` | 現release一致 |
| `??` | `tests/ops/test_repair_log_cleanup.py` | 同名なし |
| `??` | `tests/ops/test_setup_retirement_verification.py` | 同名なし |
| `??` | `tests/ops/test_signal_response_queries.py` | 現release一致 |
| `??` | `tests/ops/test_update_failure_cleanup.py` | 同名なし |
| `??` | `tests/ops/test_update_service_stop_verification.py` | 同名なし |

### `WT/v9-rc-20261009-structure`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `docs/guides/UPGRADE_AGENT.md` | 後続変更との意味照合が必要 |
| ` M` | `evaluation/canonical_request_eval.py` | 逆パッチ検査成功 |
| ` M` | `mcs/extract/clinical_values.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/extract/drug_map.py` | 逆パッチ検査成功 |
| ` M` | `mcs/extract/v1/extract.py` | 逆パッチ検査成功 |
| ` M` | `mcs/extract/v4/extract_llm.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/notify/notify_urgent.py` | 逆パッチ検査成功 |
| ` M` | `mcs/semantic/semantic_assessment.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_bench.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_drain.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_extraction.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_loops.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_render.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/semantic/semantic_v4.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/views/mcs_stats.py` | 後続変更との意味照合が必要 |
| ` M` | `tests/extract/test_bp_pair_guard.py` | 現release一致 |
| ` M` | `tests/extract/test_clinical_chunk_planning.py` | 逆パッチ検査成功 |
| ` M` | `tests/extract/test_detail_clinical_extraction.py` | 逆パッチ検査成功 |
| ` M` | `tests/notify/test_notify_urgent.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_canonical_request_evaluation.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_semantic_bench.py` | 逆パッチ検査成功 |
| ` M` | `tests/semantic/test_semantic_degraded_coverage.py` | 現release一致 |
| ` M` | `tests/views/test_drug_candidates.py` | 現release一致 |
| ` M` | `tests/views/test_interaction_latency.py` | 現release一致 |
| ` M` | `tests/views/test_view_corrupt_state.py` | 現release一致 |
| `??` | `changes/main-analysis-stored-json.json` | archive一致 |
| `??` | `changes/main-evaluation-legacy-json.json` | archive一致 |
| `??` | `changes/main-urgent-policy-number-boundary.json` | archive一致 |
| `??` | `changes/v9-structure-artifact-kinds.json` | 同名なし |
| `??` | `changes/v9-structure-completeness-quality.json` | 同名なし |
| `??` | `changes/v9-structure-stats-ref-progress.json` | 同名なし |
| `??` | `changes/v9-structure-vital-source.json` | 同名なし |
| `??` | `tests/extract/test_completeness_quality.py` | 同名あり・内容差 |
| `??` | `tests/semantic/test_coverage_completeness_repair.py` | 同名あり・内容差 |

### `CLAUDE/wf_04c119de-b76-12`

| 状態 | パス | 判定 |
|---|---|---|
| `??` | `.owner.lock` | 所有印・保持 |

### `CLAUDE/wf_b0df567d-353-3`

| 状態 | パス | 判定 |
|---|---|---|
| `??` | `.owner.lock` | 所有印・保持 |

### `CLAUDE/wf_eee116be-215-3`

| 状態 | パス | 判定 |
|---|---|---|
| ` M` | `mcs/extract/patient_context.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/extract/rollup.py` | 逆パッチ検査成功 |
| ` M` | `mcs/extract/v1/extract.py` | 後続変更との意味照合が必要 |
| ` M` | `mcs/ops/brain_export.py` | 逆パッチ検査成功 |

### `CLAUDE/wf_eee116be-215-4`

| 状態 | パス | 判定 |
|---|---|---|
| `??` | `.owner.lock` | 所有印・保持 |

### `CLAUDE/wf_eee116be-215-6`

| 状態 | パス | 判定 |
|---|---|---|
| `??` | `.owner.lock` | 所有印・保持 |


## 追加回帰テストの集約結果

上記5ファイルは初回観測では未統合だった。親の追加指示により元WTを読取り専用のまま、releaseへ同一bytesで新規追加した。全fixtureは合成、書込みはtmp_path、launchctl・プロセスsignal・サービス入口はstubであることを確認。元2 WTの所有ロックなし、観測時のcwdプロセス一致0。これらは稼働中でないことの限定的な観測であり、元WTの変更許可とはしない。

公式 `scripts/run_tests.sh` で **33 passed in 0.26s**。ruff、`git diff --check`成功。現コード修正は不要だった。追加後に元ファイルとreleaseファイルのSHA256がreceiptと一致し、対象HEADと登録126件が変わっていないことを確認した。

証跡: `~/.local/state/mcs-quality/v1.0.23/worktree-test-integration.json`（パス・SHA）、`worktree-test-result.json`、`worktree-tests.log`。台帳本文とinventory JSONは初回観測を保持し、この節をその後の処置とする。未意味照合の残差は全件統合済みとは扱わない。

## 最終の意味照合結果

初回の逆パッチ失敗を未統合と断定せず、release到達可能blob、変更AST、同じ関数の到達履歴、現呼出先と後継テストまで照合した。元WTの製品変更として未統合のまま残す候補はない。唯一今回追加が必要だった未追跡5テストは上記のとおり新規集約した。旧コード・旧pin・生成文書・過去のリリースノートを現版へ重ねて戻すことはしない。

- operations WT210パス: 既統合179、取り込み後の後続変更31、未統合0、未確認0。`operations-worktree-disposition.json`に元blob・取り込みcommit・後続置換commit・意味照合を記録。
- claude-ops8 SHA/32 path pairs: 19残差を独立照合、未統合0。`claude-ops-commit-disposition.json`。
- structure/integrationの抽出・semantic15関数: 14は統合後強化、1は未使用wrapperを直利用経路へ置換。未統合0。`semantic-worktree-disposition.json`。
- 他のdirtyパスとunique68 SHAも`worktree-final-disposition.json`で一度ずつ処置を記録。履歴blobは234/342の初期commit残差で一致し、残りは関数・後継契約で照合。

「後続文書へ置換」は古い文書の全文章を今回再掲したという意味ではない。旧releaseの生成物や合成レビュー用baselineは元WTに保持し、現releaseの正本を古い版で上書きしない。所有印・ignored私有runtime dataは集約対象外で保持する。

### 主な後続置換の根拠

| 元変更 | 現在の根拠・処置 |
|---|---|
| `old_pin` | 後続置換; `install.sh:HERMES_PIN`, `mcs/ops/mcs_update.py:HERMES_PIN/HERMES_PREVIOUS_PINS`, `.github/workflows/ci.yml` |
| `rollup` | 後続置換; `b00ef9d3`, `mcs/extract/rollup.py:_message_source_signature/dirty_projects`, `mcs/ops/mcs_repair.py:plan`, `tests/extract/test_rollup_dirty.py` |
| `pharmacy` | 後続置換; `mcs/extract/pharmacy_cues.py:renal_hepatic`, `mcs/extract/eval/pattern_cards.py:NAME_SOURCES/_clinical_name/_generic_names`, `tests/extract/test_pattern_cards.py`, `tests/extract/test_pharmacy_cues.py` |
| `core_efficiency` | 既統合・後続強化; `mcs/core/ledger.py:replace_reaction_actors/_semantic_member/_semantic_thread_fingerprint/stored_reply_ids`, `mcs/core/local_llm.py:admitted_chat/admitted_probe_format`, `mcs/core/mcs_queries.py:REPLY_GAP/incomplete_reply_roots/coverage_gaps`, `mcs/ingest/run_check.py:stage_thread_read` |
| `security` | 既統合・後続強化; `adapters/common/journal.py:append`, `mcs/ops/mcs_setup.py:_TIMEOUT_PL/_retire_agents/_retire_previous_runtime/_agent_absent`, `mcs_standalone/config.py:_private_bytes`, `tests/adapters/common/test_journal_append_cleanup.py`, `tests/ops/test_setup_retirement_verification.py` |
| `test_refresh` | 後続テストへ置換; `tests/core/test_mcs_util.py:test_long_line_keeps_boundaries_without_repeated_suffix_copies`, `tests/core/test_thread_reply_pairs.py:test_coverage_gaps_preserves_counts_with_constant_query_count`, `tests/ingest/test_health_watch.py:test_unknown_archive_copies_only_retained_history`, `tests/ingest/test_thread_read.py:test_foreign_patient_reply_does_not_stand_in_for_an_unstored_one`, `tests/notify/test_notify_digest.py:test_scope_integer_decoder_failure_is_usage_error_before_io`, `tests/notify/test_notify_render.py:test_card_list_omits_original_body_but_body_view_keeps_it`, `tests/notify/test_notify_standalone_flush.py:test_direct_sender_seal_bounds_reads_and_preserves_contract`, `tests/notify/test_notify_views_search_fold.py:test_search_counts_all_matches_without_fetching_all_bodies`, `tests/views/test_clinical_summary_source_scope.py:test_medication_presence_reuses_scope_without_dictionary_reads` |
| `context_cleanup` | 既統合・後続表現変更; `mcs/extract/patient_context.py:LABELS/_CATEGORY`, `mcs/extract/v1/extract.py:main` |
| `baseline_test` | 後続強化; `tests/extract/test_rule_summary_sentence_context.py` |
| `semantic` | 既統合・後続強化; `semantic-worktree-disposition.json`, `c3a53c88`, `f72afdcb` |
| `shared_ops` | 既統合・後続強化; `claude-ops-commit-disposition.json` |
| `fsync` | 既統合・後続強化; `mcs/ops/ext_contract.py:fsync_dir`, `mcs/ops/mcs_operations.py:fsync_dir`, `mcs/ops/mcs_requests.py:fsync_dir`, `mcs/ops/mcs_update.py:fsync_dir`, `mcs/core/mcs_util.py` |
| `retry425` | 既統合; `mcs/core/mcs_util.py:RETRY_LATER_4XX`, `mcs/ingest/mcs_adapter.py:download_attachment` |
| `endpoint_order` | 既統合; `mcs/extract/v4/extract_llm.py:_CALL_NOTES.transport_at/run_pending._observe` |
| `notify_old` | 既統合・後続テスト契約変更; `mcs/notify/notify_cards.py`, `tests/notify/test_task_reminder_send_gate.py`, `tests/adapters/lineworks/test_same_pass_revoke.py`, `tests/adapters/discord/test_mcs_discord_delivery.py`, `tests/ops/test_v1020_latest_policy.py` |

### 全登録の最終処置

| ワークツリー | 処置 |
|---|---|
| `LIVE` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `/private/tmp/claude-501/-Users-yusuke--mcs/c618caa7-929f-4a8a-afcb-e31daaa53e00/scratchpad/wt-3213` | 履歴照合済み・prunable登録保持（削除なし） |
| `/private/tmp/claude-501/-Users-yusuke--mcs/c618caa7-929f-4a8a-afcb-e31daaa53e00/scratchpad/wt-combined2` | 履歴照合済み・prunable登録保持（削除なし） |
| `STATE/mcs-hotfix-summary-20261009/baseline` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `STATE/mcs-quality/v1.0.21/blockers-20261014/mcs-review` | レビューsnapshot・変更blob照合済み、基準履歴は統合対象外 |
| `STATE/mcs-quality/v1.0.21/post-merge-fix-review-20261014/coderabbit-review` | 全独自パッチ同等・再適用不要 |
| `STATE/mcs-quality/v1.0.21/post-merge-review-20261014/coderabbit/01-core-ingest-semantic` | レビューsnapshot・変更blob照合済み、基準履歴は統合対象外 |
| `STATE/mcs-quality/v1.0.21/post-merge-review-20261014/coderabbit/02-delivery-notify` | レビューsnapshot・変更blob照合済み、基準履歴は統合対象外 |
| `STATE/mcs-quality/v1.0.21/post-merge-review-20261014/coderabbit/03-adapter-tests` | レビューsnapshot・変更blob照合済み、基準履歴は統合対象外 |
| `STATE/mcs-quality/v1.0.21/post-merge-review-20261014/coderabbit/04-operations-supply` | レビューsnapshot・変更blob照合済み、基準履歴は統合対象外 |
| `STATE/mcs-quality/v1.0.21/post-merge-review-20261014/coderabbit/05-docs-assets` | レビューsnapshot・変更blob照合済み、基準履歴は統合対象外 |
| `WT/audit-v1.0.20-claude` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/chore-coderabbit-drafts` | HEAD祖先・既統合 |
| `WT/dev-team-base` | HEAD祖先・既統合 |
| `WT/docs-v1.0.21-aggregate` | HEAD祖先・既統合 |
| `WT/docs-v1.0.21-final` | HEAD祖先・既統合 |
| `WT/docs-v1.0.21-late-aggregate` | 独自SHAの現内容/履歴/後続契約を照合済み・再適用不要 |
| `WT/feat-bundled-drug-master` | 独自SHAの現内容/履歴/後続契約を照合済み・再適用不要 |
| `WT/feat-desktop-local-bootstrap` | 全独自パッチ同等・再適用不要 |
| `WT/feat-hermes-release-pin` | 全独自パッチ同等・再適用不要 |
| `WT/feat-python314-runtime-migration` | 独自SHAの現内容/履歴/後続契約を照合済み・再適用不要 |
| `WT/feat-v1.0.21-claude-ops` | 独自SHAの現内容/履歴/後続契約を照合済み・再適用不要 |
| `WT/feat-v1.0.21-operations` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/feat-v1.0.21-pharmacy-integration` | HEAD祖先・既統合 |
| `WT/feat-v1019-compatible-checkout` | HEAD祖先・既統合 |
| `WT/fix-admitted-chat-wait-s` | HEAD祖先・既統合 |
| `WT/fix-backup-deadline-slack` | 独自SHAの現内容/履歴/後続契約を照合済み・再適用不要 |
| `WT/fix-daily-backup-deadline` | HEAD祖先・既統合 |
| `WT/fix-installer-config-matrix` | 独自SHAの現内容/履歴/後続契約を照合済み・再適用不要 |
| `WT/fix-installer-updater-agent-entry` | HEAD祖先・既統合 |
| `WT/fix-llm-down-since-recovery` | HEAD祖先・既統合 |
| `WT/fix-llm-health-ordering` | 独自SHAの現内容/履歴/後続契約を照合済み・再適用不要 |
| `WT/fix-main-local-disposition` | 全独自パッチ同等・再適用不要 |
| `WT/fix-retired-extraction-import-roots` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/fix-revoke-withdraw-first` | 独自SHAの現内容/履歴/後続契約を照合済み・再適用不要 |
| `WT/fix-same-pass-revoke` | HEAD祖先・既統合 |
| `WT/fix-slack-profile-worker-20261017` | HEAD祖先・既統合 |
| `WT/fix-snapshot-read-snapshot` | HEAD祖先・既統合 |
| `WT/fix-standalone-log-fields` | HEAD祖先・既統合 |
| `WT/fix-v1.0.21-checkpoint-unsent` | HEAD祖先・既統合 |
| `WT/fix-v1.0.21-late-quality` | HEAD祖先・既統合 |
| `WT/fix-v1.0.21-post-merge-review` | HEAD祖先・既統合 |
| `WT/fix-v1.0.21-recover-efficiency` | HEAD祖先・既統合 |
| `WT/fix-v1.0.21-recover-ops` | HEAD祖先・既統合 |
| `WT/fix-v1022-update-chunk-plan` | HEAD祖先・既統合 |
| `WT/hermes-v0217-pin` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/hotfix-backlog-commit-reserve` | HEAD祖先・既統合 |
| `WT/hotfix-backlog-qc-generation-order` | HEAD祖先・既統合 |
| `WT/hotfix-jev-answer-reask` | HEAD祖先・既統合 |
| `WT/hotfix-jev-claim-batch` | 全独自パッチ同等・再適用不要 |
| `WT/hotfix-jev-partial-answers` | HEAD祖先・既統合 |
| `WT/hotfix-jev-prob-codes` | HEAD祖先・既統合 |
| `WT/hotfix-limitations-item-type` | HEAD祖先・既統合 |
| `WT/hotfix-loop-rejection-integration` | HEAD祖先・既統合 |
| `WT/hotfix-loop-rejection-view` | 全独自パッチ同等・再適用不要 |
| `WT/hotfix-loop-relation-reask` | HEAD祖先・既統合 |
| `WT/hotfix-loop-relation-rejected` | 全独自パッチ同等・再適用不要 |
| `WT/hotfix-maintenance-lifecycle` | 全独自パッチ同等・再適用不要 |
| `WT/hotfix-mcs-health-launchd` | HEAD祖先・既統合 |
| `WT/hotfix-rollup-input-signature` | HEAD祖先・既統合 |
| `WT/hotfix-rollup-reobserve` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/hotfix-run-lock-diag` | HEAD祖先・既統合 |
| `WT/hotfix-semantic-backlog` | HEAD祖先・既統合 |
| `WT/hotfix-semantic-cap600` | 全独自パッチ同等・再適用不要 |
| `WT/hotfix-shadow-v2-drop-reasons` | HEAD祖先・既統合 |
| `WT/hotfix-shadow-v2-failure-detail` | HEAD祖先・既統合 |
| `WT/hotfix-shadow-v2-null-context` | HEAD祖先・既統合 |
| `WT/hotfix-shadow-v2-rerun` | HEAD祖先・既統合 |
| `WT/hotfix-shadow-v2-timing` | HEAD祖先・既統合 |
| `WT/hotfix-snapshot-hard-deadline` | 全独自パッチ同等・再適用不要 |
| `WT/hotfix-snapshot-validation-deadline` | HEAD祖先・既統合 |
| `WT/hotfix-summary-format-length-stop` | HEAD祖先・既統合 |
| `WT/hotfix-summary-format-repair` | HEAD祖先・既統合 |
| `WT/hotfix-summary-limitations-string` | HEAD祖先・既統合 |
| `WT/hotfix-summary-quality` | HEAD祖先・既統合 |
| `WT/hotfix-summary-validation-codes` | HEAD祖先・既統合 |
| `WT/integrate-all-worktrees-20261014` | HEAD祖先・既統合 |
| `WT/integrate-remote-main` | HEAD祖先・既統合 |
| `WT/pharmacy-chat-coverage-w1` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/quality-v9-efficiency` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/quality-v9-integration` | HEAD祖先・既統合 |
| `WT/quality-v9-stability` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/quality-v9-structure` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/refactor-release-1.0.18-delta` | 全独自パッチ同等・再適用不要 |
| `WT/refactor-v1.0.20-debt` | HEAD祖先・既統合 |
| `WT/release-1.0.18-candidate` | HEAD祖先・既統合 |
| `WT/release-1.0.18-prep` | 全独自パッチ同等・再適用不要 |
| `WT/release-v1.0.20-main` | HEAD祖先・既統合 |
| `WT/release-v1.0.20-prep` | HEAD祖先・既統合 |
| `WT/release-v1.0.23` | 集約先・並行実装中。最終commit/必須検証は親が担当 |
| `WT/release-v15-schema` | HEAD祖先・既統合 |
| `WT/review-highs` | HEAD祖先・既統合 |
| `WT/review-v1.0.21-20261014` | HEAD祖先・既統合 |
| `WT/v1.0.17-stability` | HEAD祖先・既統合 |
| `WT/v9-rc-20261009-efficiency` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/v9-rc-20261009-integration` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/v9-rc-20261009-security` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `WT/v9-rc-20261009-structure` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `CLAUDE/wf_04c119de-b76-1` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-10` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-11` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-12` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `CLAUDE/wf_04c119de-b76-13` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-14` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-2` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-3` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-4` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-5` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-6` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-7` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-8` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_04c119de-b76-9` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_872d3f72-871-1` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_872d3f72-871-2` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_872d3f72-871-3` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_b0df567d-353-1` | 独自SHAの現内容/履歴/後続契約を照合済み・再適用不要 |
| `CLAUDE/wf_b0df567d-353-2` | 独自SHAの現内容/履歴/後続契約を照合済み・再適用不要 |
| `CLAUDE/wf_b0df567d-353-3` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `CLAUDE/wf_eee116be-215-1` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_eee116be-215-2` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_eee116be-215-3` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `CLAUDE/wf_eee116be-215-4` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `CLAUDE/wf_eee116be-215-5` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_eee116be-215-6` | 全dirty/untrackedパス処置済み（詳細JSON）。元WT・所有印を保持 |
| `CLAUDE/wf_eee116be-215-7` | 全独自パッチ同等・再適用不要 |
| `CLAUDE/wf_eee116be-215-8` | 全独自パッチ同等・再適用不要 |

詳細JSONには705件のdirty/untrackedパス観測（release作業中106件を含む）と68 unique SHAの処置がある。記録はパス・SHA・判定のみで、diff本文や実データを保存していない。

後継契約の選択回帰7ファイルは **215 passed in 4.40s**（`worktree-successor-tests-corrected.log`）。初回は旧名`test_rollup_reobserve.py`を指定して収集エラー・0実行となったため、現後継`test_rollup_dirty.py`に修正して実行した。初回失敗ログ`worktree-successor-tests.log`も保持する。

### cherry対象外のmerge履歴

`git cherry`が列挙しない非祖先mergeは別に16 SHAを全件確認した。全16件で`git show --remerge-diff --name-only`は差分なし（exit0）。親commitに追加された独自の解消パッチはなく、親の処置を継承する。上記68件は非merge独自SHAであり、merge16件を含めた確認数は84 SHA。

| merge SHA | 処置 |
|---|---|
| `04411a252583590bcab1cfc6f3a2c1b7d5536256` | remerge差分なし・親変更を照合済み |
| `10a4768bbd0d377324830acc50a86d8604d2b435` | remerge差分なし・親変更を照合済み |
| `14cb85927a0b40b78c186c009e6980e457c3a023` | remerge差分なし・親変更を照合済み |
| `2a864684b9906e5622892c8d154461d29c8ae043` | remerge差分なし・親変更を照合済み |
| `37ca79eea0df6b847caf5b377bfebca1e29d9933` | remerge差分なし・親変更を照合済み |
| `596501141bffd16d08cf1d13af2ca02e833a4b8a` | remerge差分なし・親変更を照合済み |
| `7250857d9d39f8015a7ba3082fa8ae3a3992a368` | remerge差分なし・親変更を照合済み |
| `85609a631ecf18fe69d7ccb447e0be09666c9314` | remerge差分なし・親変更を照合済み |
| `90f1983f9f3aeae6c8b27adddaeafe79a4c99a3f` | remerge差分なし・親変更を照合済み |
| `9b10124a51d63f302ddba38f76b3268faf27b889` | remerge差分なし・親変更を照合済み |
| `9d38762a1e2165465862a818156e2d4f5ea85cee` | remerge差分なし・親変更を照合済み |
| `b8288f87c952141b300d3dc21c13f95a82bf90c4` | remerge差分なし・親変更を照合済み |
| `c4318e00e6497fd3803d2007a34ec3e99c207cca` | remerge差分なし・親変更を照合済み |
| `d19ac3a278e0eb423bfed054e8b207591059172b` | remerge差分なし・親変更を照合済み |
| `dcff5678e820a0b91aa96bb4a15d85e7e20314f0` | remerge差分なし・親変更を照合済み |
| `fd20248e1c66c25fe054449e380b5db8755b6e14` | remerge差分なし・親変更を照合済み |

最終再確認ではrelease HEADと全126元HEADに変化なし。`worktree-final-receipt.json`に最終証跡ファイルのSHA256を固定した。独立照合票内のinventory_sha256は途中時点のinventoryを指し、追記後の最終inventoryとは異なる。元blob・統合commitによる証明と最終receiptを併用する。
