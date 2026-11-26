# CCO terminal/file隔離の候補設定

## 候補設定の範囲

`cco-terminal.candidate.yaml` は snapshot を読取専用、`cmd-proposals` を
提案の書込み先として公開する。実行用 `cmd` はモデルへ書込み可能にしない。
提案ファイルだけで人の承認とみなさず、native plugin の preview/confirm を経て
実行用 inbox へ投入する。terminal 節だけの候補であり、config 全体を置換しない。

`cco-approval-scope.json` は未承認の記入用テンプレートです。端末固有のパス、
設定hash、ローカルimage IDはリポジトリに保持しません。YAMLの`__MCS_ROOT__`と
`__VERIFIED_IMAGE_DIGEST__`、承認票の`__PROFILE_CONFIG__`は適用前に私有作業領域で
明示的に置換します。変数展開や自動適用は行いません。描画後の候補hashと承認証跡はrepo外で保管してください。

## マウントと権限の確認

- `data/snapshots` は読取り専用、`data/cmd-proposals` は提案専用の書込み先です。原本DB、実行用の `data/cmd`、添付本体、バックアップ、認証情報、ホームディレクトリはマウントしません。
- 候補では作業ディレクトリの自動マウント、ネットワーク、環境変数の転送、追加のDocker引数を無効にします。端末で使用するイメージと絶対パスは適用前に確認してください。
- この制限はterminal/fileツールに適用されます。ホストで動くnative plugin、MCP、委譲先のツール、外部接続にはそれぞれのアクセス権があるため、業務で許可するものを個別に確認します。
