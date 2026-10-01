# hermes-mcs — MedicalCareStation 記録の収集・整理・見直し支援

**v1.0.8** — 変更履歴は [CHANGELOG.md](CHANGELOG.md)、リリースは
[GitHub Releases](https://github.com/yusuketakuma/hermes-mcs/releases)。

> **MCS にたまる医療・介護チームのやり取りを、あとから検索・集計・
> 見直せる形に整えるシステムです。**

医療・介護向けメッセージ基盤 **MedicalCareStation（MCS）** の記録を
24時間5分ごとに自動で取り込み、自分のMac上に保存します。Discord / Slack への通知、
全文検索、患者ごとの時系列表示、統計、「確認した方がよいかもしれない
記録」の一覧提示までを一つの仕組みで行います。

> **大切な約束**: このシステムは「記録を集めて見やすくする」ことと
> 「人が確認して判断する」ことを分けています。機械が挙げるのは
> あくまで「アラート」です。記録が見つからないことは
> 「対応がなかった」ことの証拠にはなりません。

## 画面イメージ

通知は **Discord / Slack** のどちらにも同じカード形式で届きます（完全合成の表示例）。
カードには送信者行と構造化要約だけを載せ、本文全文と添付は専用スレッドへ配送します。

![Discord通知カード](docs/screenshots/discord-card.svg)

Slack のカード・スレッドや CLI 画面などの例は [利用者ガイド](docs/USER_GUIDE.md) を参照。

## できること

- **新着連絡の通知** — 新しい投稿を Discord / Slack へ転送（構造化要約カード。本文・添付は専用スレッド）
- **全履歴の保存と検索** — 過去の投稿も全件保存（途中で止まっても再開）。全文検索・患者ごとのタイムライン
- **内容の自動整理** — 薬・症状・依頼・バイタル値・検査値などを機械が拾って構造化（「候補」扱い）
- **患者ごとの一覧・統計** — 現在の薬・最新バイタル・未解決の依頼・次回予定・MCS 連携サマリー、投稿量・職種別内訳（読み取り専用）
- **アラートの提示** — 「後続の記録が見つからない」等を列挙（対応漏れの断定ではない）
- **依頼の台帳** — 人が確認した内容だけを登録。MCS へ自動で送信することはない
- **チャットからの操作** — 通知カードのボタン（確認・担当・タスク作成など。Discord / Slack 共通）と Discord の `/mcs` コマンドで閲覧・依頼の確認（Hermes addon 経由）

## 仕組みと情報の行き先

![全体の流れ](docs/assets/flow-overview.svg)

1. **収集** — 24時間5分ごとに MCS を確認し、新しい記録をこのマシン上のデータベースに保存
2. **整理** — 薬・症状・依頼・バイタル値・検査値などを機械が拾って構造化（間違えることもあるため「候補」扱い）
3. **提示** — Discord / Slack 通知・検索・統計・アラートとして、人が見られる形にする

患者の記録の行き先（詳細は [SECURITY.md](SECURITY.md)「人工知能（AI）の使用箇所と情報の行き先」）:

- **このマシン内** — 記録の保存と、文章の整理（要約・薬名や症状の拾い上げ）。
  ローカルAI（Qwen3.5-9B）を使い、この推論経路は外部へ送りません
- **通知先（Discord / Slack）** — 通知を設定した場合、本文・要約・送信対象の添付・患者名を設定先へ送ります
- **外部 AI（TypeSafe Jev API）** — 任意・既定 OFF。意味チェック（`semantic.mode`）または
  抽出監査（`semantic.extract_qc`）を有効にした場合のみ、本文と必要なスレッド文脈を送ります（匿名化なし）
- **知識ストア向け出力** — `brain_export.py` は患者名・病名等を含む Markdown をローカルに書き出します
  （匿名化なし）。その先の同期・LLM 利用は別経路で、送信先・権限は運用側で管理します
- 「ローカルLLM」は、システム全体が外部へ送らないという意味ではありません

## MCS データで何が追えるか

診療と診療の間の**多職種コミュニケーション**（症状 → 共有 → 相談 → 判断 → 実施 → 再評価）を、
次の7分野で扱えます。

| 分野 | 得られるデータ例 |
|---|---|
| 🧑‍⚕️ 患者経過 | 症状、バイタル、状態変化、入退院、問題の反復 |
| 💊 薬物療法 | 開始、中止、増減量、期間表現、再評価 |
| 💬 多職種連携 | 誰→誰への相談、回答、判断、実施 |
| 🏥 組織間連携 | 薬局・診療所・訪看・居宅間のやり取り |
| ⏱ 時系列 | 対応時間、変化点、再発間隔 |
| 📊 業務 | 記録の集中、未解決依頼、長期滞留 |
| 🔎 確認支援 | 通常との差、フォロー記録不足候補 |

**データが意味しないこと** — MCS に記録がない ≠ 実際に行われていない ／ 投稿数が多い ≠ 患者が重症 ／
薬剤名が記録された ≠ 現在服用中 ／ 薬剤変更後に改善した ≠ 変更が原因 ／ 多職種が多い ≠ 良い連携 ／
warning なし ≠ 患者が安全。「未来を予測する」機能ではありません。
各分野の詳細・データ一覧・図は [利用者ガイド](docs/USER_GUIDE.md)。

## クイックスタート

前提: macOS 13 以降・普段のユーザー（sudo 不要）・Xcode Command Line Tools・Homebrew・
空き約 12 GB・MCS アカウント。Python は `install.sh` が用意する。

```bash
git clone https://github.com/yusuketakuma/hermes-mcs.git && cd hermes-mcs
./install.sh --preflight   # 読み取り専用の事前チェック。NG 行の fix: を実行して 0 blocker(s) にする
./install.sh               # 依存一式を導入（冪等。止まったら直して再実行すれば続きから）
```

最後に `Installed. Summary:` と、次に実行する `mcs_setup.py init` / `services` / `check`
のコマンド（venv インタプリタのフルパス付き）が表示されるので、順に実行する。
困ったときは同じインタプリタで `mcs_setup.py doctor`。手順全体・成功の目安・
メッセージ別の対処は [docs/INSTALLATION.md](docs/INSTALLATION.md) の「最短手順」と §7。
AI エージェントに導入させる場合は [docs/SETUP_AGENT.md](docs/SETUP_AGENT.md)。

## 安全設計

- **人承認境界** — 依頼登録・シグナル却下・閾値変更は `--confirm-human` + 理由 + receipt 記録付きの経路のみ。自動確定・自動通知はしない
- **「不在 ≠ 未実施」** — 候補は「記録が見つからない」事実の提示であり、対応の欠如を意味しない
- **既読化ゲート** — fetch_state=complete かつ ledger commit 済みの患者のみ、snapshot timestamp を必ず送信
- **no-redirect / no-proxy** — Bearer は許可 origin 以外へ送らない。レスポンス本文はログに出さない
- **定期実行は本文・氏名を stdout/ログに出さない** — 通知先へは設定に従い本文・添付・患者名を送る。明示的な `mcs_view` 閲覧は例外
- **外部送信は明示設定のみ** — Jev 連携は既定 OFF。通知先・エクスポート先と閲覧権限は運用側で管理する
- **患者データ・秘密情報をリポジトリに入れない** — テスト・評価データは完全合成のみ
- **限界の明示** — 患者一覧は暫定集約、添付内容は未解析、日次バックアップは同一マシン内（[SECURITY.md](SECURITY.md)「復旧と解析の限界」）

## ドキュメント

| 文書 | 内容 | 主な読者 |
|---|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | 画面イメージ・できること・MCS データで何が追えるか（データ一覧・アラート・限界） | 利用者・責任者 |
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | 導入手順・設定キー一覧・スケジュール構成・トラブルシューティング・Discord/Slack 接続設定 | 運用担当 |
| [docs/SETUP_AGENT.md](docs/SETUP_AGENT.md) | AI エージェント向けの対話セットアップ手順書 | 運用担当・エージェント |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | モジュール・コマンド・統計/シグナル定義・人承認操作・AI 技術詳細・システム構成図 | 運用・開発 |
| [SECURITY.md](SECURITY.md) | データ取扱い・安全境界・AI の使用箇所と情報の行き先・復旧と解析の限界 | 全員 |
| [docs/lifecycle-spec.md](docs/lifecycle-spec.md) | 導入・更新・バックアップ・復旧のライフサイクル仕様 | 運用 |
| [docs/external-export-contract.md](docs/external-export-contract.md) | 外部エクスポート契約 | 運用・開発 |
| [docs/semantic-evaluation.md](docs/semantic-evaluation.md)・[semantic-facts-v2-rollout.md](docs/semantic-facts-v2-rollout.md) | 意味解析の評価・rollout | 開発 |
| [hermes_plugin/README.md](hermes_plugin/README.md) | Discord / Slack プラグイン（`/mcs`・対話カード） | 運用・開発 |
| [deployment/README.md](deployment/README.md) | 配備資産（launchd・cron・復旧 watchdog） | 運用 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 今後の計画（項目ごとの詳細計画は `docs/roadmap/`） | 責任者・開発 |

## ライセンス

Private repository — 現時点で公開・再配布は想定していない。
利用・改変はリポジトリ管理者の明示許可に従う（[LICENSE](LICENSE)）。
