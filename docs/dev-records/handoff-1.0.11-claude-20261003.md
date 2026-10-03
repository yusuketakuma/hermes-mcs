# 1.0.11 追加開発の引継ぎ（Claude → Codex、2026-10-03）

対象ブランチ: `feature/stamps-1.0.11`（worktree `~/.mcs/.claude/worktrees/stamps-1.0.11`、基点 `9ea015d`）。
Codexのリリース準備worktree `~/.herdr/worktrees/mcs/release-1.0.11-20261003`（`codex/release-1.0.11-20261003`、未コミット47件）
へ統合してもらうための記録。push・PR・merge・配備・実チャット送信はしていない。

## 1. コミット

| commit | 内容 |
|---|---|
| `1aa1d0d` docs | ROADMAP（1.0.x/1.1.x方針、#22〜#32）、`docs/roadmap/{stamps,mcs-api-survey,connector-decisions,upgrade,digest}.md` |
| `c3ea1bd` feat #30 | 過去版からの安全な更新: `scripts/mcs_upgrade.py`、`mcs_update plan`/`apply --reinstall`、`mcs_util.REPO`一元化、`docs/guides/UPGRADE_AGENT.md` |
| `1842976` feat #31/#32 | 要約サマリー: 📊ボタン・Discord `/mcs {"op":"summary"}`・Slack `/mcs-summary`・LINE WORKS DM「サマリー」、絞込み、日次サマリーのカード配信（`op=notice`）、`fit_parts`/`parts_text`/Slack `render_parts` |

詳細設計とレビュー反映は `docs/roadmap/upgrade.md`・`docs/roadmap/digest.md`（§6が第2段階）。
変更記録: `changes/upgrade-agent-20261003.json`・`changes/summary-card-20261003.json`・`changes/summary-phase2-20261003.json`
（リリースbuildで1.0.11のCHANGELOGに含めること）。

## 2. 検証済み・未検証

- 済: 合成データの全テスト `scripts/run_tests.sh`（tests/・integration/）3768 passed・4 skipped、ruff（CIと同範囲）、
  `ci/gates.py` 8/8、`update_readme.py --check`。中間commit `c3ea1bd`単体でも tests/ops・tests/notify が通過。
  各機能とも 計画→独立計画レビュー→実装→独立再レビュー→指摘修正 を実施。
- 未: 実機での更新実行（`mcs_upgrade.py apply`）、Slack/Discord/LINE WORKS実テナントでの表示・送信、
  Hermes同居時の`/mcs`返答の公開範囲（このため`/mcs`のsummaryは患者名を出さない）、Slack slash commandの実テナント登録。

## 3. 統合時の衝突（Codex worktreeの未コミット変更と重なるファイル）

`docs/ROADMAP.md`、`docs/development/DEVELOPMENT.md`、`docs/guides/INSTALLATION.md`、`docs/roadmap/{connector-decisions,mcs-api-survey,stamps}.md`、
`mcs/core/ledger.py`、`mcs/notify/notify_digest.py`、`mcs/notify/notify_render.py`、`mcs/ops/mcs_setup.py`、`mcs/ops/mcs_update.py`。

- **`notify_digest.py`（必ず衝突）**: 本ブランチで全面的に書き直した（`build()`が`parts`を返し、`build_text`/`daily_parts`/`view`/`parse_scope`/CLI）。
  Codex側の`_self_reaction_count`（本人反応の件数）は、`build()`内の`section(...)`で1節として追加する形に移植してほしい
  （例: `section("本人反応", [...])`。患者別の行なら`fold=True`、集計行なら既定の非fold。`ok(pid)`で範囲を必ず適用）。
  これで日次・📊・コマンドの全経路に同じ内容が出る。
- `notify_render.py`: 本ブランチは`display_text`の後に`PARTS_TEXT_BUDGET`・`fit_parts`・`parts_text`を追加しただけ。
- `ledger.py`: 本ブランチは`notification_renders.intent_event_id`の加法列と`_outbox_insert(..., route=None)`/`outbox_add_tx(..., route=None)`のみ。
- `mcs_setup.py`: `REPO_ROOT = REPO`（`mcs_util.REPO`）と`daily_digest.scope`の検証・init質問。
- `mcs_update.py`: #30の変更が多い。Codex側は2行の差分なので本ブランチ側を基準にCodexの1変更を載せ直すのが安全。
- `ROADMAP.md`・`docs/roadmap/*.md`: 本ブランチの版には#30〜#32が追記済み。Codex側の変更と手で統合する。
  live checkout（`~/.mcs`）にも同じ計画文書が未コミットで残っており、本ブランチの版と内容が異なる（mergeの前に揃える）。

## 4. リリース・配備で必要なこと

- 更新時の注意（各changesの`upgrade`に記載済み）: Hermes Gatewayの再起動（Slack/Discord）、LINE WORKS独立アダプターの再起動。
  再起動前の旧アダプターでは📊ボタンが正しく動かない。
- Slack `/mcs-summary`を使う場合: Slackアプリにslash commandと`commands` scopeを追加して再インストールし、plugin settingsに`snapshot`を設定
  （`docs/guides/INSTALLATION.md`付録B末尾）。
- 日次サマリーの送信先: カード通知が有効ならカード用の配送先（`delivery_scope`）、無効なら従来どおり`notify_target`。
- 巻戻し: 1.0.11より前へ戻すと、未封印のカード版日次サマリーはその日の分が送られず、封印済みで未送の分は送られても通知キューに残る。
- README・`docs/development/readme-review.json`の1.0.11照合: 📊サマリー・コマンド・更新手順書（UPGRADE_AGENT.md）を機能・導入・ドキュメント導線に反映するか確認してほしい。

## 5. 未実装・持ち越し

- #30: v1.0.0〜1.0.2は手動経路のみ（自動巻戻し不可）。独立モードはhost経由の更新を案内するだけ。
- #31: 📊集計はカード処理の書込みロック内で走る（負荷未計測）。LINE WORKSでカードのボタンが10個を超える場合の扱いは未確認。
- ROADMAP 1.0.11の#22（スタンプ）・#23・F-7・F-8は本ブランチでは未着手（Codex側で実装中の内容を正とする）。
