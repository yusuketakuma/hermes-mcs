# CCO terminal/file隔離の候補設定

## 現在の候補（2026-09-25確認）

`cco-terminal.candidate.yaml` は snapshot を読取専用、`cmd-proposals` を
提案の書込み先として公開する。実行用 `cmd` はモデルへ書込み可能にしない。
提案ファイルだけで人の承認とみなさず、native plugin の preview/confirm を経て
実行用 inbox へ投入する。terminal 節だけの候補であり、config 全体を置換しない。

`cco-approval-scope.json` は未承認の記入用テンプレートです。端末固有のパス、
設定hash、ローカルimage IDはリポジトリに保持しません。YAMLの`__MCS_ROOT__`と
`__VERIFIED_IMAGE_DIGEST__`、承認票の`__PROFILE_CONFIG__`は適用前に私有作業領域で
明示的に置換します。変数展開や自動適用は行いません。描画後の候補hashと承認証跡はrepo外で保管してください。
以下の cmd 書込みを含むcanary結果は過去の構成の記録であり、このテンプレートの
承認・適用・実機検証を証明しません。今回、実設定や稼働サービスは確認・変更していません。

## 過去の候補と検証計画

以下は当時の記録を保持したもの。記載された cmd の直接書込みは現在の候補に採用しない。

未適用。`cco-terminal.candidate.yaml`はCCO configのterminal節だけの案で、config全体を置換しない。既存のHermes Docker backendを使用する。

- terminal/fileツールへsnapshotディレクトリをread-only、既存cmdディレクトリをread-writeで公開する。原本DB・添付本体・backup・credentials・HOME全体をmountしない。
- 自動CWD mount、環境変数転送、追加Docker引数を無効にする。コンテナのネットワークは無効。この変更でCCOの既存terminal/file経由の外部アクセスと他のホストファイル利用はできなくなるため、適用前に業務上の必要範囲を確認する。
- 当時は既存ローカルimageを固定して検証した。環境固有のIDは除去済み。現在のテンプレートは各導入先で配布digestを検証して指定する。今回pull/buildは実行していない。
- この設定はモデルのterminal/file実行境界だけ。native Discord pluginはgateway process内で動くため、plugin設定のsnapshot/inboxにはホスト側パスを使う。コンテナの`/mcs/...`をそのまま渡さない。他のhost-side tool/MCP/委譲先もこのDocker設定では隔離されない。

検証済み：一時homeへ候補設定のみをコピーし、実コード`build_profile_terminal_scope`でbackend・2mount・network/CWD mount無効・環境転送なしを確認。コンテナは起動していない。実際の読取/書込拒否の証明ではない。

次の実機検証では本番データを使わず、一時ディレクトリにsnapshot用canary、inbox用canary、非公開canaryを置く。同じ設定形のコンテナからsnapshot読取成功/書込拒否、inbox書込成功、非公開canary非公開、外部ネットワーク不可を確認する。gateway/その他toolの権限は別途検証する。これらの成立前にCCOの全権限隔離が完了したとは扱わない。


## 実Dockerの合成canary結果

2026-09-21、当時の検証用ローカルimage（端末固有IDは除去済み）を使用。`docker run --rm --pull=never --network=none`、一時snapshotディレクトリをreadonly mount、一時cmdをread-write mountとしてPython probeを実行した。

snapshot読取成功、writeはEROFS、inbox書込成功をコンテナ内とホスト側で確認。非公開canary・原本DBのホストパス・Docker socketはコンテナに存在せず、/proc/net/routeは経路0件。コンテナ終了code 0、一時ファイルは終了時にcleanup済み。実原本・credential・snapshotはmountしていない。

これはDocker mount構成の実機証拠であり、Hermesがこの候補設定を用いて生成するコンテナのE2E、host-side plugin/MCP/委譲先の隔離、CCO稼働設定への適用を証明しない。残りの権限確認と有効化承認は引き続き必要。

## Hermes実コード経由の合成canary

候補Hermesのbuild_profile_terminal_scope→terminal_scope→_get_env_config→_create_configured_env→DockerEnvironment.executeを通して同じ検査を実施し成功。一時HOME/HERMES_HOME、許可した最小環境変数、合成mountだけを使用。snapshot読取・書込拒否、inbox書込、原本パス/Docker socket非公開、経路表0件を確認した。force_removeとwait_for_cleanupでテスト用コンテナを終了・削除し、一時ディレクトリをcleanup。

初回probeは戻り値キーをexit_codeと誤記したため検証側で失敗。実APIのreturncodeへ直して再実行し成功。製品コード変更なし。初期設定解決時にホスト上の/workspace不存在警告が出たが、コンテナ生成後のexecuteは/workspaceで正常実行できた。

これはterminal/fileが共有する環境生成経路の検証。モデルturn全体、host-side plugin/MCP/委譲先の権限境界や本番有効化を含まない。

## ホスト側経路の確認範囲

個別端末の有効MCP・plugin一覧は公開記録から除去した。MCS native pluginはホスト側で動作するため、Dockerのmount制限とは別の信頼境界になる。導入先で許可する各MCP・pluginの権限を別途確認する。

MCS pluginはhost contextのnative Discord/user/chat/profileと設定scopeを検査し、固定snapshotのViewと固定inboxへのenqueueだけを呼ぶ。実合成結合でpreview無書込み、確定後も原本のrunnerが適用するまでrequests不変、receiptと重複抑止を検証済み。任意shell/model toolは登録しない。

委譲はdelegate_tool._build_childrenで親toolsetsを継承し、delegate_tool_dispatchでcontextvars.copy_context().runをexecutorへ渡す。terminal_scopeはContextVarなのでこのdispatch経路では親の設定を保持する。子agent全実行や各tool固有のhost I/Oをこのコード確認だけで監査済みとは扱わない。

connectionsはmanaged accountとcatalog MCPの管理入口で、任意ファイル読取APIではない。一方、kanban/memory/session/skillsなどのhost I/Oと、remote connectorの権限はterminal Dockerだけでは制限されない。設定の存在確認やコード上の入口の照合は、OSレベルでCCO process全体が原本を開けない保証ではない。

本番導入時に必要なのは、terminal/fileの隔離案の適用に加え、native pluginを信頼された限定入口として扱う構成と、CCOに許す既存業務toolの明示。既存の業務toolを無断で一括削除したり、単にDocker設定だけを「CCO全体隔離完了」と報告しない。
