# hermes-mcs 自動更新 Codex向け詳細仕様 v2.0 — 統合読取り版

作成日 2026-11-10 JST。source SHA e85763b5addaabee516f6f69b8ea7465f26fe9d8。

本書はパッケージ内の正本章とfield一覧を結合したもの。JSON Schema、interfaces.pyi、合成fixture、98項目の受入JSONもZIPに含む。製品実装・配備は未実施。

---

<!-- BEGIN CODEX_START_HERE.md -->

# Codexへの引継ぎ：hermes-mcs 署名付き自動更新 v2.0

作成日: 2026-11-10 JST。対象: `yusuketakuma/hermes-mcs`。
基準SHA: `e85763b5addaabee516f6f69b8ea7465f26fe9d8`。

このパッケージは**実装仕様**であり、実装済みソフトウェアでも、本番操作の承認でもない。
文書v2.0は旧`hermes-mcs-auto-update-codex-spec-v1.0.md`の改訂正本となる。文書版、wire版、製品バージョンは別。

## 読む順序

1. `SPEC.md` — 決定、スコープ、不変条件、現行差分。
2. `01_GITHUB_PUBLICATION.md` — 公開処理・公式仕様・受入証拠・API・権限。
3. `02_DEVICE_PROTOCOL.md` — 端末の状態機械・lock・一回性・失敗・移行。
4. `03_CONTRACTS.md` と `contracts/contracts-v2.schema.json` — wire/local/publisherの閉じたデータ契約。
5. `04_IMPLEMENTATION_PLAN.md` と `interfaces.pyi` — 実装順序・関数・変更ファイル・完了条件。
6. `05_ACCEPTANCE_TESTS.md` と `contracts/acceptance-tests.json` — 製品のRED/正常/競合/障害試験。
7. `06_SOURCES_AND_REVIEW.md` — GitHub公式資料、現行source、追加レビューの処置。
8. `VALIDATION.md` — この納品時に実施した検証と未実施範囲。

長文をAGENTS.mdに貼らない。各文書の末尾マーカーまで分割して読み、未読節を推測しない。
単一ファイルを使う場合は、別添`hermes-mcs-auto-update-implementation-spec-v2.0.md`を読む。

## 最初の作業

現行AGENTS.mdと配下の指示を読み、開発用worktreeを使用する。現行HEADと基準SHAが異なれば、全差分を保持してP0の対応表を先に更新する。
`mcs_update.py`はfacadeであり、旧モノリスへロジックを戻さない。`EFFECT_NOT_CONNECTED`を削除するタスクは既に失効している。
まずP0の入口・lock・配備コピー・schema・backend対応表と、連続2更新の失敗試験を作る。正常系のeffectだけ先につながない。

実装では既存v1の個別TTY承認、SPENT/UNKNOWN、DB復元同意、送信/課金/試行予算の意味を維持する。
v2の恒常同意は初期OFF。通常autoはcode-only-compatible / forward-only。同じrelease owner以外の更新エンジンを作らない。

## 禁止とブロッカー

本書はpush/PR/merge/tag/Release公開/クラウド配備/実機再起動/鍵登録/権限変更/本番DB操作を許可しない。
テストは完全合成。一時HOME/DB/repository/偽transport/偽serviceを使用し、実患者・匿名化実投稿・Keychain・実サービスへ接続しない。
SDC中央backendの現行リポジトリは未特定・未レビュー。APIは本書で**新設する契約**であり、実在と仮定しない。
中央adapterは既存backendを確認して接続する。接続先・実鍵・導入baselineの未設定をmock成功で穴埋めしない。独立したローカル開発は継続する。

`contracts/*.schema.json`は構造仕様、`interfaces.pyi`は型・関数境界の仕様、`validation/`は納品物検査用である。これらを本番の認可・暗号検証の代用品にしない。

## 完了報告

変更理由とファイル、満たしたINV/SEM/試験ID、実測結果、再利用した証拠、未実行、環境ブロッカー、外部操作に必要な承認を別々に記録する。
「全試験がHOLDで通る」「モデルだけ通る」「HEADだけ変わる」は完成ではない。

END-CODEX-START-V2.0

<!-- END CODEX_START_HERE.md -->

<!-- BEGIN SPEC.md -->

# hermes-mcs：顧客Mac miniの署名付き自動更新 実装仕様 v2.0

文書日: **2026-11-10 JST** / 基準SHA: **e85763b5addaabee516f6f69b8ea7465f26fe9d8**。
状態: **開発へ渡す詳細仕様。製品実装・本番試験は未実施。**
根拠IDは`06_SOURCES_AND_REVIEW.md`、構造定義は`contracts/contracts-v2.schema.json`を参照する。
MUSTは必須。実在コードと新設する仕様を混同しない。旧仕様との衝突は本書の明示改訂を優先するが、実装HEADの安全制約を黙って弱めない。

## 1. 決定

**GitHubを不変配布物の原本、既存SDCの限定拡張を配布登録・端末別認可、既存release ownerを唯一の変更実行者とする。**

```text
main履歴上の確定コミットへvX.Y.Zを付ける
 → ci（read-only）と既存方針に適合するrelease acceptance
 → trusted publisherが同じコミットの完全bundleを一度固定
 → draftへ全asset → immutable publish → GitHubの公開証拠を検証
 → SDCへ配布登録（ここまでで端末実行はまだ許可しない）
 → self / pilot / batchへ同じdigestを選択配信
 → 顧客Mac：取得・検証 → prepare/drain → 短命grant
 → 唯一のowner：START/SPENT → code-only apply → 新世代起動確認
 → ローカルVERIFIED＋結果outbox → ACKとは独立に次更新可能
```

通常経路は、初回に範囲付き自動更新同意を得た端末だけを無人更新する。初回bridgeは人の承認が必要。
公開直後の全台同時実行ではなく、公開後に自動的な段階配信へ入る。停止条件を通らなければ対象端末だけ保留し、既に正常な業務を不要に止めない。

## 2. 旧仕様からの必須改訂

|旧前提|現行と改訂|
|---|---|
|署名付きeffect未接続|接続済みv1を保全し、v2同意・drain・成功終結を完成させる [SRC02–04]|
|mcs_update.pyへ追加|facadeを維持し、update_cli/update_apply/update_execution等の正本へ変更|
|非公開repoのPAT配布|基準観測はpublic。顧客へGitHubのPAT/App秘密鍵を配らない|
|全6jobを同一SHAで毎回再実行|対象SHAのbindingは必須。2026-10-31の証拠再利用・再検証受入を機械化する [SRC01]|
|共通bailoutをそのまま流用|v1個別承認rollbackは維持。v2通常autoの失敗はforward-only|
|G/U/Rだけのlock表|A（authority）/M（monitor）/C（config）と新Qを含むphase別契約|
|復旧器entrypointだけ更新|4support、hash、世代directory、.prevを一式として扱う [SRC08]|
|抽象モデル成功で十分|連続2更新を含む製品統合試験を必須にする。旧モデルは証拠流用しない|

今回もmainは同じ基準SHAを返した [SRC00]。public/main保護/rulesets/Releaseについて前レビューの観測を、将来も不変な設定とは扱わない。P0と本番有効化時に再確認する。

## 3. 通常autoの許可クラス

`code-only-compatible`はSemVerのpatch番号ではなく、**current→targetの具体的な互換性証拠**で決まる。
次の全条件を満たす。

- DBスキーマ番号だけでなく、schema/永続データの意味・メッセージ/attempt/課金/通知/認可の契約が同じ。移行関数を起動しない。
- Python/Hermes/SDK/OCR/ネイティブ依存、runtime profile、venvの実測fingerprint、モデルを変更しない。pip/Homebrew/モデルdownloadは禁止。
- 設定の生値・形式・未指定時の既定値の認可上の意味、外部送信先、patient/source scope、Humanゲートを拡大しない。
- 認可、署名、SPENT、lock、effects、独立復旧を担うcontrol closureが同一。通常アプリの完全性manifestが変わること自体は禁止しない。
- 同じruntime mode/CPU/Python系列についてcurrent releaseを含む互換性pairがある。未検証の飛び越し更新は許可しない。
- 設定上必要なservice集合を止めて再開でき、quarantine・loaded identity確認を受け入れ済み。

上記以外は`maintenance_required`。自動同意をbridge、DB復元、信頼root更新、credential変更、機能権限拡大の承認へ昇格しない。
静的解析でコードの全意味を証明するとは主張しない。機械検査＋対象差分のレビュー＋回帰証拠で分類する。

## 4. 非目標

別Updater、新しい汎用遠隔コマンド/SSH、顧客MacのGitHub runner化、コンテナ/A-Bへの全面移行、TUF自作、既存暗号の再実装、全ディスク巻戻しへのハードウェア同等保証は対象外。
通常autoでコード/DBを無条件rollbackしない。独立した明示承認による既存復旧経路は残す。
中央障害時は新STARTを止めるが、正常な既存版の業務は継続する。

## 5. 不変条件

|ID|条件|
|---|---|
|INV01|タグ・CI受入・core・artifact・公開証拠・端末grantが同一の対象を表す|
|INV02|GitHub Releaseの存在・Verified表示・HTTP200・OIDCログインだけでは端末実行を許可しない|
|INV03|同一runtime rootに変更実行ownerは一つ。v2導入後のlegacy fallbackは禁止|
|INV04|START intentとSPENTの正常な耐久公開前に本体repo/codeへeffectしない|
|INV05|SPENT/attempt/sequence/historyを削除、減算、別IDで再生しない|
|INV06|UNKNOWNを未実行や成功へ推測変換しない。旧grantから新capabilityを作らない|
|INV07|v1のself/個別TTY/unknownはそのまま。v2をv1の緩和として実装しない|
|INV08|code切替中は全管理対象の旧processとlockless書込みが退役している|
|INV09|HEAD、PID、mtime、終了コード、ACKの単独一致ではVERIFIEDにしない|
|INV10|患者DB・設定・credential・models・venv・予算・既読/通知権限を通常autoで変更しない|
|INV11|通信中処理の結果・費用・送信unknownを更新の都合で失わせない|
|INV12|wireには閉じた非臨床DTOのみ。backup実hash/config実digest/任意ログも外へ出さない|
|INV13|未知schema/duplicate key/型不正/破損状態は拒否し、元証拠を保全する|
|INV14|時間・bytes・展開・process・state・準備停止の予算を有界にする|
|INV15|取得/検証/認可の外部障害だけで現行正常serviceを停止しない|
|INV16|実測・再利用・未実行と観測不能を区別し、CI successや正常状態を偽装しない|
|INV17|通常autoは実際の互換性で限定する。source完全性とcontrol closureの不変性を混同しない|
|INV18|privileged publisher/ownerは未認証candidateやcacheを実行せず、固定control closureだけで権限を判断する|
|INV19|公開順序・連続更新・結果送達を正確に管理し、ACK喪失で正当な次更新を妨げず、配信を後退させない|
|INV20|正常なA→B→Cと否定試験を両方要求する。本書/fixture検査を製品受入や本番操作の承認にしない|

## 6. 信頼境界

A: 開発source/タグ。B: GitHub検証・publisher。C: 配布登録/署名/端末認可。D: 端末の独立trusted closure。E: 更新対象アプリ。

B→CはGitHub OIDCを検証する。ただしpublisherのOIDCは**配布物の登録/署名要求だけ**を許し、顧客grant発行や同意作成を許さない。
C内のrelease署名とdevice-grant署名は用途・鍵・API権限を分ける。2-of-3 root秘密鍵はCIへ置かない。同一侵害領域なら「独立二重防御」と称さない。
Dは別経路で確認したtrust rootから開始する。ダウンロードしたrootへの無条件TOFUは禁止。端末の同一UID/root全侵害、リリースと認可の両鍵侵害は保護限界として明記する。

## 7. 識別子

|項目|意味|
|---|---|
|source_repository_id|GitHubの数値ID。名前と独立。今回の観測値1379530125はP0で再照合|
|source_tag / tag_oid / source_commit|厳密vX.Y.Z / refの生OID / peelしたcommit。すべて固定|
|request_id|公開要求。03のENC03で定義したsource・受入・publisher policyへのdomain-separated SHA-256|
|code_seq|配信streamの中央耐久連番。GitHub run番号/時刻/文字列SemVerを代用しない|
|release_id|既存v1 coreのhash。GitHub数値Release IDと別|
|manifest_digest|短命製品attestationのhash。更新で変わり得る|
|publication_id|中央が固定した公開照合記録のUUID|
|operation_id / grant_id|一回の端末操作 / 一回の短命開始許可|
|runtime_generation|端末で単調増加する整数。PID・host既存generationとは別|
|attempt_key|03 ENC03のH(scope,target_commit,profile_sha256,config/data contract digests)。同じcodeの再梱包/別tagで消費済み枠を復活させない|
|revision / execution_revision|owner全保存のCAS / 認可・基線・実行状態だけの世代。ACKのみでは後者を進めない|

同一repo/tagの別SHAはconflict。正常配布はsourceが先行配布の子孫であることも確認し、古いcommitの新タグ化でanti-rollbackを迂回しない。

## 8. 完了の定義

実装の完成は、契約validator、公開冪等性、端末統合、連続2更新、障害注入、v1回帰が対応する試験を通ること。
運用の完成は、さらに実鍵、backend adapter、保護設定、bridge適格性、承認済みmacOS検証機、担当者の復旧手順が揃うこと。
製品テスト未実行、backend未確認、実機未検証を成功扱いしない。今回納品したschema/fixture検査は製品受入とは別。

END-SPEC-V2.0

<!-- END SPEC.md -->

<!-- BEGIN 01_GITHUB_PUBLICATION.md -->

# 01 — GitHub公開パイプラインの詳細仕様

本章のAPI/Actions仕様はGitHub公式資料GH01–GH16に基づく。本製品独自の規則はPUB要件として区別する。
対象はGitHub.com。REST APIは`X-GitHub-Api-Version: 2026-03-10`を明示する。無指定のdefaultに依存しない [GH14]。

## 1. GitHubが保証すること／しないこと

|公式仕様|本実装への帰結|
|---|---|
|Immutable Releaseは公開時点のtag/assetsを保護する。本文・title等は別 [GH01,GH02]|本文・Latest・prerelease表示を実行認可や配信順序にしない|
|draft→全asset→publishが推奨順序 [GH01]|公開後にassetを追加・置換するパイプラインは禁止|
|GitHubのrelease attestationはimmutable公開で生成 [GH01]|公開前assetリストへ「その公開後のattestation」を循環参照させない|
|`workflow_run`のGITHUB_SHA/GITHUB_REFはdefault branch [GH04]|candidateはupstream runのhead_shaとtag peelから取り、publisher自身のSHAと分ける|
|workflow_runは前runの失敗でも起動し、権限を持ち得る [GH04,GH05]|イベント到着を公開承認とみなさず、独立に受入証拠を検証|
|GITHUB_TOKEN起因のイベントには再帰抑止がある [GH06]|自動公開後のreleaseイベントで仕上げ処理が動くことに依存しない|
|concurrencyのdefault pendingは1。queue:maxでも100まででdispatch順序保証ではない [GH07]|キューは補助。正本は中央reservation/CAS/sequence。queue:maxとcancel-in-progress:trueを併用しない|
|Release asset downloadは200または302 [GH08]|GitHub側redirectは中央だけで検査。端末no-redirectを緩めない|
|Actions artifactとRelease assetは別API・別寿命 [GH09]|CI artifactの保存期限を恒久配布ストレージとみなさない|
|artifact attestationは安全性・試験成功の証明ではない [GH10]|provenance、テスト受入、顧客の適用許可を別々に検証|
|環境のProtected branches onlyは保護規則がない場合all branchesになり得る [GH11]|publisher環境はSelected branchesでmainを明示。保護済みとの推測は禁止|
|`target_commitish`は既存tagがあればtag作成先指定として使われない [GH03]|タグの生OIDとpeelしたcommitをAPI/Gitで別に確認|
|Release作成/更新はworkflow差分により追加権限を要求し404/403になり得る [GH03]|404をRelease不存在と即断して再作成しない。権限エラーとして切り分ける|

## 2. 初回設定（実行は別承認）

PUB01: mainのforce-push/delete制限、レビュー・受入方針、publisher/security helperへの変更管理を確認する。
PUB02: `refs/tags/v*`のcreationは承認済みrelease担当に限定し、update/deleteを禁止する。タグ名だけではmain所属を保証しないためPUB10も必須。
PUB03: Immutable Releasesを有効化する。既存v1.1.2等には遡及しない [GH02]。本機能が作る各Releaseの実際のimmutable=trueを公開後にも確認する。
PUB04: environment `mcs-release-publish`を事前作成し、Selected branchesのmainだけ許可する。environment名だけのOIDC信頼は禁止。初回pilot中の人review要求と、受入方針承認後の機械承認を区別する。
PUB05: root秘密鍵、device grant秘密鍵、顧客credentialをActionsへ渡さない。publisherの認証はOIDCを優先し、製品release署名は中央の限定signerへ要求する。
PUB06: 公開の通常権限はGITHUB_TOKENのcontents:writeだけをwrite jobに与える。workflow差分のため追加Workflows:writeが必要なケースは`PUBLISH_PERMISSION_HOLD`。別承認済み・当該repo限定のApp tokenによる保守経路以外で権限を拡張しない。PAT自動fallbackは禁止。

設定確認結果にはrepo ID、default branch、ruleset ID/対象/実効性、environment制限、publisher closure digest、確認時刻を記録する。設定のGETが403/未対応なら「設定済み」としない。

## 3. Workflow構成

### 3.1 変更する既存workflow

`ci.yml`の6 lane（lint-test/hygiene/standalone-sdk/incident-gates/macos/hermes-integration）は維持する。失敗項目だけの再検証を許す既存方針を取り消さない。
タグイベントでは、軽量な`publication-request.json`を出力してよい。ただし未信頼データとして扱う。値はrepo ID/tag/tag OID/source SHA/upstream run ID/attemptのみ、最大4KiB。
plain main pushで全suiteを再起動する変更はしない。既存PR/schedule/manualの用途も維持する。

`release-notes.yml`は日本語ノートcheck/export/syncを保持するが、公開系を一本化した後はtagでReleaseを作る役割を新publisherへ移す。二つのdraft作成者を並走させない。
`sync-existing`はreleaseイベントなら必ずそのeventのtagだけを対象にし、manual無指定の場合のみ既存の一括確認とする。現行workflow本文と文書の対象限定説明に差があるため、移行試験で両方を照合する [SRC09]。
ノート用画像も全assetに含めて公開前に添付する。公開後の図の差替え要求は、immutable assetの上書きでは解決しない。

### 3.2 新設するworkflow

配置: `.github/workflows/release-publish.yml`。
イベント: `workflow_run(workflows:[ci], types:[completed])` と明示復旧用`workflow_dispatch`。
manual入力: `request_id`と`acceptance_id`。任意URL、shell command、任意refの入力は受けない。CI run自体が作られていないcomposed受入では、認証済み管理面が受入票と実tag照合からrequestを登録し、upstream_run_id/attemptを共にnullで保持できる。自動workflow_run経路では両方非null必須。nullを架空run番号で埋めない。

```yaml
# 実装の必須設定。step/helperは以下の契約を実装してから接続する。
permissions:
  contents: read
concurrency:
  group: mcs-release-publisher
  cancel-in-progress: false
  queue: max
```

queue:maxをサポートしない環境へ無言で設定を落とさない。本書はGitHub.com対象であり、将来GHESへ移す際は別受入が必要。
workflow_runのchainはci→publisherの2段だけにし、releaseイベントの再連鎖を要求しない。

|job|入力|最小権限|処理と出力|
|---|---|---|---|
|identify|イベント、既知repo/workflow/policy|contents:read, actions:read|upstreamとtag/acceptanceを照合し固定BuildRequest出力|
|package|固定BuildRequest|contents:read|隔離Gitで完全bundle、ノート、画像、core材料を生成。candidateを実行しない|
|authorize-publish|同じpackage ID/digestと受入票|contents:read, actions:read, id-token:write|中央予約・署名・artifact永久保存。固定PublishPlanを返す|
|publish|固定PublishPlanと中央保存済みbytes|contents:write, actions:read, id-token:write|同一Release/draftの照合、upload、publish。candidate sourceをcheckoutしない|
|verify-register|同じpublication ID/release ID|contents:read, id-token:write|GitHub公式verify、再download hash、中央登録。成功後のみ配信可能|

`id-token:write`はJWT取得能力であってリポジトリ内容へのwrite権限ではない [GH12]。
全jobでactionsを完全SHAへ固定し、現行承認済みpinを不用意に更新しない。package/privileged job間で書込可能cache・workspaceを共有しない。
別jobに渡すartifactを実行しない。unzipは空の隔離directoryだけへ、entry/展開量/経路/symlinkを制限する。ZIPのdigestと中のbundleのdigestを混同しない。

### 3.3 trusted helperの固定

publisher helperは`publisher_policy`が認めたworkflow/helper/toolchain closureからのみ起動する。candidate tagにあるhelperを署名/write権限で起動しない。
`github.sha`はpublisher側のsource pinとして使えるが、targetには使わない。中央はOIDCのworkflow_shaに存在するworkflow/helperのdigestが承認closureと一致することを確認する。
変更のない別main commitで実行されることは許せるが、helper/blobが違えば新しい方針承認まで拒否する。`.github`全体を無条件に信じない。
Git、gh、Pythonの実バージョンと配布digestを`publisher-toolchain.lock`へ記録する。ghにverify/verify-assetの必要能力がなければfail closed。根拠のないSHA値を本書では作らない。

## 4. Candidateと検証受入の解決

PUB10 `resolve_build_request(event, policy, github_read) -> BuildRequest`

1. repo数値IDとowner IDが登録値に一致。repository名の再利用だけでは受け入れない。
2. upstream runをIDと**attempt**で取得。workflow ID/path/そのsource版をallowlistと照合。head_repositoryも同一repo。PR/forkからの通知は拒否する。
3. 自動経路はtag対象の既存ci runだけを候補化。request artifact内のevent名を信用せず、GitHub run APIのevent/head_shaと既知workflowのtag triggerを照合する。head_branchだけをtagの証明にしない。
4. 厳密`^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$`、各数値は2^31-1以下、全長64以下。先頭ゼロ、vなし、prereleaseを通常入口で拒否する。
5. `refs/tags/<tag>`を取得。annotated tagはtypeとSHAを追って最大8段、循環/非commit終端を拒否。生tag_oidと最終source_commitを両方保存する。
6. upstreamの実source SHAとpeel結果が一致することを検証。必要情報が曖昧なら公開しない。`target_commitish=main`は証明に使わない。
7. 読取り時のmain_snapshot_shaを固定し、隔離Gitでtargetがその祖先であることを確認する。mainが後へ進むこと自体で拒否しない。
8. `acceptance_id`で対象SHAへの正式受入を取得する。次節のmode/coverageを検証。
9. タグの再読取り結果が同じであることを確認し、request_idを計算して返す。

同一requestのmetadataが違えばHOLD。git fetchの不足objectをクライアントへ丸投げしない。APIのpartial pages/取得不能を空として受け入れない。

### 4.1 ReleaseAcceptanceの意味

受入票はcommitの外に置き、target SHAに署名で束縛する。自分自身のSHAをtagged tree内の票へ埋める循環を作らない。
構造はschemaの`ReleaseAcceptance`。全6laneを一意に列挙するが、実施方法を一律再実行へ限定しない。

- `full_run`: 対象SHAの全必須laneとrelease検査が成功。collectorが機械受入可能。skip/neutral/cancel/未開始は成功扱いしない。
- `composed`: 元の実測証拠、修正差分、失敗/影響箇所の再検証、再利用根拠、未実行範囲、独立した受入判断を揃える。2026-10-31方針に対応。人の承認identityは中央が実認証から設定する。
- infrastructureの失敗は`infra_unavailable`として元のstatusを保持し、既存の隔離local同等確認と承認で補完する。製品/test failureをinfra例外で免除しない。

各laneは`source_commit`,`environment_digest`,`suite_digest`,`evidence_sha256`,`measured_result`,`coverage`,`reused_from_commit`,`impact_review_sha256`,`resolution_evidence_sha256`を持つ。
再利用時はtargetとの差分一覧と影響レビューを固定する。自動collectorは差分影響不明を勝手にunaffectedにしない。
`unresolved_product_failures`は必ず空。missing lane/未承認未実行/古いgreenだけの流用を拒否する。

`acceptance.accepted=true`はGitHub CIがsuccessだったという意味ではない。必要なら別名`release-acceptance`のcheckで受入判断を表示するが、元の6job statusを書き換えない。
完全な実測の場合はtagから公開まで手動操作不要。例外的composed受入は事前の明示受入がない限りそこで停止する。

### 4.2 APIから証拠を読むとき

`GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}`を使用し、jobsも当該attemptのendpointで全pageを読む。default latest attemptを混ぜない [GH09]。
Artifactsはrunに対する全pageを読み、ID、expired、run ID、source SHA、repo ID、digestを照合する。artifact名だけでは選ばない。
API artifact metadataにattemptがない場合、artifact内の自己申告だけでattemptを証明しない。対応するjobの実行記録・upload出力のartifact IDと結合できない証拠は`composed`の承認証拠へ回すか拒否する。
必要な一次証拠は中央へcontent-addressedで保持し、Actionsの保持期限後に検証不能にならないようにする。患者・秘密・任意ログを公開assetへ入れない。

## 5. Bundleとassetの作成

PUB20: 対象commitの到達履歴だけを含む完全Git bundle v2。対象treeだけの疑似commitや`--all`は禁止。prerequisite、thin pack、LFS/submoduleのネットワーク補完は不可。
タグ前に実依存lockと全source manifestを確定する。`sdc-release-owner.json`は既存の自己除外を維持し、新control manifestも自分のhashを含めない。依存lock→control manifest→全source manifestの非循環順を検証する。

trusted packagerはcandidateファイルをデータとして読む。build/install/importをしない。必要な依存環境変更がある候補はcode-only分類から外れる。
全packを機密/無関係refの混入検査に通し、既存artifact inspectorとGit strict graph検査を通す。禁止path/case/NFC/サイズ/深さの制限を緩めない。

必須asset集合（固定名、ASCII、正確な集合で照合）:

|名前|内容|
|---|---|
|`hermes-mcs.bundle`|認証対象の完全bundle|
|`release-core.json`|既存v1のcanonical core。commit/artifact size/hash/依存lock/profile/schemaを束縛|
|`release-attestation.initial.json`|既存製品署名の公開時attestation。期限付きであり将来の開始許可にはならない|
|`compatibility.json`|新v2の署名付き互換性pair。target/source/control/data contractのbinding|
|`release-acceptance.json`|公開してよい項目だけの正式受入票。詳細ログは含めない|
|ノートが参照する画像群|許可した説明図。一覧・hashはPublishPlanで固定|

全asset集合のhashはPublishPlanへ記録し、集合の中にそのPublishPlan自体のhashを入れる循環は作らない。GitHubのrelease-attestationは公開後に中央証拠へ保存し、公開済みReleaseへの追加を要求しない。
Core内のrelease_idは既存core hashで計算。GitHub release_id/asset_idは後で割り当てられるため、Coreに埋めない。公開後PublicationRecordが両者を結び付ける。

### 5.1 buildの冪等性

中央は`(source_repository_id, source_tag)`をuniqueに予約する。予約時にtag_oid/source_commit/request_idを固定し、streamのcode_seqをserializable transactionで一度だけ採番する。欠番は許容、再利用は禁止。
Buildは同じGit版でもbytesが毎回同一とは仮定しない。最初にCASで採用されたasset集合を中央へ保存し、以降のretryは**同じ保存bytes**を再利用する。別bytesは同じ版として採用しない。
公開reservationのphaseは`reserved → packaged → draft_created → assets_complete → published_unverified → registered`。`conflict`はterminal。
各phaseはrevisionとpublisher leaseのfencing tokenでCASする。GitHub concurrencyだけをロックにしない。

## 6. GitHub API操作表

|操作|API/条件|成功条件/不明時|
|---|---|---|
|tag確認|GET `/git/ref/tags/{tag}`、必要ならGET `/git/tags/{sha}`|exact OID/peeled SHA。404/権限不明はHOLD|
|既存Release検索|GET `/releases/tags/{tag}`＋必要時全page一覧|予約済みIDとの一致。曖昧な複数一致は拒否|
|draft作成|POST `/releases`、tag_name固定、target_commitish=固定SHA、draft=true、prerelease=false、make_latest=false|IDを耐久保存。timeoutなら同じtagを再照会し、無条件再作成しない|
|asset upload|応答のupload_urlのtemplateを安全に解決。HTTPS uploads.github.com・同一repo/release IDを検証|201、state=uploaded、正しいname/size/hash。生bytesを送る|
|asset列挙|GET `/releases/{id}/assets` 全page|max100/page、上限20page。完全集合を満たさなければ拒否|
|publish|PATCH `/releases/{id}`、draft=false、prerelease=false、make_latest=false|公開後GETで固定ID/tag/assets/immutableを確認|
|整合検証|`gh release verify TAG -R OWNER/REPO`、各assetへ`gh release verify-asset TAG PATH -R OWNER/REPO`|終了コード成功＋expected tag/commit/digest照合。native source ZIP/tarは使わない|
|配布登録|中央register_publication|公開証拠・asset bytes・SHA/受入を再照合してregistered|

APIの401/403/404/422/429/5xxを区別する。404を盲目的createにしない。同名assetへの422は、同じbytesが既に存在するか読取りで確認し、不一致ならHOLD。
502でstate=starterの空assetが残り得る [GH08]。GitHubでは削除可能だが、本実装の既定動作はHOLD。operatorが当該draft・当該未完成asset IDの削除だけを明示承認する。公開asset・他人のdraftを自動削除しない。
公開に必要な画像の追加漏れも「後で添付」で通さない。immutable=falseなら配布登録禁止。

### 6.1 不確実な公開と再実行

公開timeoutは`published_unverified`。新しい版を作らず、同じRelease IDをGETして読取り主体で確定する。
公開確認できたが中央登録が失敗した場合も、同一ID/bytesでregisterだけをretryする。GitHubと中央間に分散transactionがあると仮定しない。
GitHub上で削除されたRelease、失効した配信、API取得不能は別状態。404/取得不能をローカルの旧版削除・DBrollback・権限追加に変換しない。

### 6.2 HTTPとredirect

GitHub向け中央clientは認証つきAPIの302を検査し、redirect先へAuthorization/Cookieを転送しない。最大3hop、HTTPS443、user-info/fragment禁止、許可したGitHub download host群のみ。DNS/private/link-local/loopback/metadata宛てを拒否し、host許可リストはデプロイ時の検証値として固定する。任意URLを端末入力から受けない。
metadataは1response最大1MiB、page上限20、connect/readを含む単一request10秒、検証段階全体300秒を初期上限とする。大きなbundleは別のbyte budgetを持つ。
secondary limitではRetry-Afterを尊重し、ない場合は公式ガイドの待機後に有限backoff。長い待機はjob/phaseを永続延期し、write権限やleaseを握ってsleepし続けない。Retry-Afterを短縮して連打しない。
304は再取得省略の意味であり製品署名の期限延長ではない。GitHubの未知response fieldは抽出対象から無視できるが、内部の署名DTOはclosed schemaで拒否する。両parser方針を混同しない。

## 7. OIDC条件

中央は標準JWTライブラリ/既存OIDC実装で署名・iss/aud/exp/nbf/iat/jtiを検証する。JWTの手作り暗号は禁止。
要求するclaim: repository_id、repository_owner_id、workflow_ref、workflow_sha、run_id、run_attempt、event_name、ref、environment、runner_environment。
許可refは`refs/heads/main`、environmentは`mcs-release-publish`、runnerはgithub-hosted、eventはworkflow_runまたは明示manual。repo ID/owner IDは文字列として厳密一致。
reusable workflowを使用する場合だけjob_workflow_ref/job_workflow_shaも検証する。通常workflowにそのclaimがあると仮定しない [GH12]。

publisherのsource SHAとcandidate SHAは別。JWTのshaがtargetであると比較してはならない。candidateとの関連はBuildRequest→Acceptance→PublishPlanのbindingを中央がGitHub APIから再照合する。
同じJWTの再提示は同じ操作/body hashにだけ冪等に応答し、別requestの権限に使わせない。jti/idempotency記録をtoken TTL以上保持する。
OIDCによって認める中央操作は`reserve_publication`、`freeze_assets`、`sign_release`、`register_publication`。`issue_device_grant`、顧客同意、root更新、任意shellは拒否する。

## 8. 配布登録と順序

登録後、中央は同じsource/assetを配信streamへ追加する。code_seqが低い遅着runをcurrentへ戻さない。高い番号でも既に採用したsourceの祖先・分岐なら通常配信を拒否する。
GitHub `/releases/latest`やpublished_at順は実行順序の正本にしない [GH03]。
self→pilot→batchは同じrelease_id/artifact digestだけを使う。変更はrollout_epochと許可cohortだけ。失敗時は新grantを止め、Immutable Releaseを差し替えない。

## 9. 公式資料による追加反例

PUB-A: workflow_runで`${{ github.sha }}`をpackすると、tagではなく後続mainを配布する → target SHAを独立binding。
PUB-B: queueで配信順序と耐久性を保証したつもり → queueは補助、中央seq/CAS/leaseが正本。
PUB-C: public repoなら誰でも更新を許せると考える → 公開bytesとdevice同意を分離。
PUB-D: attestationがあるのでtestを省く → 正式Acceptanceを別検証。
PUB-E: publishedイベントで画像追加 → 画像をdraft前に固定。
PUB-F: default branchにある旧helperを実行 → publisher closure digestとOIDC workflow_shaを照合。
PUB-G: global GITHUB_TOKENにwriteを与えcandidate testsを実行 → candidate lane read-only、write専用jobはdataだけ読む。
PUB-H: uploaded filenameの正規化により別名になる → ASCII固定名＋応答exact比較。

END-GITHUB-PUBLICATION-V2.0

<!-- END 01_GITHUB_PUBLICATION.md -->

<!-- BEGIN 02_DEVICE_PROTOCOL.md -->

# 02 — 端末プロトコル・一回性・停止・移行

本章は顧客用v2の新設契約。現行v1のself/TTY/unknown経路は維持する。
現行の`_run_native_release`、borrowed effect、source/successor検証を出発点とし、同じownerの内部で拡張する [SRC02–08]。

## 1. 端末の適格性とbootstrap

DEV01: 初回bridgeは別承認の保守作業。稼働checkoutで開発しない。
bridgeは、端末identity、trusted root、実依存profile、base release/source、control closure、独立復旧器、全管理入口、所有者の継続同意を照合する。
旧`update.mode=auto`、SDC監視登録、一般サポート同意はv2更新同意の代わりにならない。

`StandingConsent`はproduct/tenant/installation/device/realm、stream、runtime profile、code-only class、保守窓、TTL、最大準備停止、方針digest、epochを束縛する。
表示→人の確認→before/after一致→私有receipt保存→中央登録の順にする。既存Human入口を再利用し、任意booleanで人確認を偽装しない。
撤回は独立trusted CLIの`auto-disable`でA/Oだけを使い、local_consent_epochを単調に増やす。業務Q共有lockを要求して撤回を塞がない。
設定の`auto=true`だけを承認receiptとして扱わない。consent期限切れは新規prepare/STARTを止め、正常稼働版を停止しない。

### 1.1 trusted closureと全sourceの分離

全source manifest (`sdc-release-owner.json`) は現在の広い検証対象を維持する。
新`deployment/sdc-update-control.json`は**権限制御部分と全依存closure**のpath/mode/hashだけを持つ。自身と全source manifestを一覧に含めず循環を作らない。
closureには署名/currentness、strict parser、状態/START、lock、固定Git effect、readiness採用、独立復旧loaderを含める。アプリpresenter/臨床moduleを無条件に含めて通常更新を全面禁止にはしない。
そのために必要な最小の純粋helper抽出を行い、動的import先と標準外dependencyもmanifestへ明示する。未検査の遅延importをtrustedと呼ばない。
通常更新ではcontrol closure digest同一を必須とし、全sourceの新digestは認証済みtargetの完全性証拠として採用する。既存source-manifest v1のowner_revisionを顧客owner v2の認可版と混同しない。v1形式の全source検査はデータ完全性として保持できるが、v2権限はControlManifest/OwnerState v2だけで判断する。

bridgeはこのclosureを既存独立復旧領域のcontent-addressed generationへ配備する。第二のUpdaterとして別権限/別stateを作らず、同じrelease ownerの固定実行コードとする。
更新実行者がmutable checkoutから自分のhelperを遅延importする構成は禁止。

### 1.2 管理対象launcher

全業務入口はアプリimportより前にQ共有lockを取得する固定bootstrapを通る。予約済み親の共有FDを子へ渡し、子/孫の退役まで寿命を保持する。
bootstrapは私有control state、start-intent、root/lock identity、許可generationを検証し、許可された起動だけを行う。
推奨起動は隔離したinterpreterの`-I -S`で固定bootstrapを読み、Q取得とruntime検証後に必要なsite/flat-import pathを明示設定する方式。`-B`だけで古いpycを読まなくなるとは扱わない。
世代別pycache prefixをCLI/`sys.pycache_prefix`の両検証点で確定し、sourceless pyc・shadowing・未許可PYTHONPATHを拒否する。
Hermes起動経路へこの契約を適用できないprofileはauto不適格のままにする。standaloneの成功をHermesへ流用しない。

owner/復旧CLIはQ共有を保持する業務processではなく、既存サービス管理が所有する独立実行枠から起動する。単なる`--role=updater`という外部引数でgateを迂回しない。

## 2. 永続状態の正本

既存control root（`_control_root(runtime_home)`）を使用。0700 directory、0600 regular single-link files、nofollow/owner/size/identity、原子的replaceとfile/directory同期を維持する。

|ファイル|責務|
|---|---|
|`release-owner.json`|v1または明示移行後の`mcs.release-owner.v2`。基線、実行履歴、現在操作、outboxの正本|
|`release-owner-activation.json`|legacyを再開放しない導入fence。元bytesを保持|
|`release-start-intent.json`（新）|SPENT保存結果不明を未開始へ変えないfence。操作/nonce/grant hash/rootを固定|
|`release-migration-v2.json`（新）|bridge移行intentと旧recordのdigest集合|
|旧owner/sidecarの不変保存領域（新）|明示移行時の元bytes。名前・数・容量をStore allowlistで制限|
|`runtime-entry.lock`（新Q）|業務process寿命の共有/コード切替の排他。inodeは導入後再作成しない|
|既存G/A/O/U/R/C/Mのlock|既存の所有と順序を維持し、v2のphase契約へ接続|
|`data/update_state.json`|既存更新journal。v2参照は明示版付きvariant。ownerの代わりの認可台帳ではない|

起動gateはownerのactiveとstart-intentの整合を読む。高速化のために別の「実行してよい」正本を追加しない。単独markerの消失を許可とみなさない。
start-intentは最終結果に整合した終結証拠をownerへ耐久保存した後にだけ、その正確なIDのものを退役できる。SPENT/終結履歴を消す操作ではない。

### 2.1 v2 ownerの必須情報

- root/repository/lock fingerprints、trust/realm、migration evidence。
- `revision`（全保存）と`execution_revision`（安全に関わる状態）。どちらも単調。
- `bindings`（code_seq→release_id）、`highest_seen`、`installed`、`highest_installed`、`verified_installed`。
- `runtime_generation`、現在の`active`操作、準備lease、private witness、grant、activation証拠。
- 不変の`spent`、attempt_key集合、終結済みoutcomes。
- fixed canonical bytesのoutbox、report ID、delivery ACK。
- installation単位rolling 24hのprepare回数/累積停止時間。

保存時はO下で最新stateを読み、旧revisionの辞書で上書きしない。
ACKだけの変更はrevisionを進めるがexecution_revisionを進めない。grantの実行witnessは後者へ束縛し、遅れたACKで準備が無限に失敗する構造を避ける。
安全項目をACK扱いにしてrevisionから外すことは禁止。変更項目ごとの分類をvalidatorと試験で固定する。

## 3. 状態遷移

|状態|意味|許される次状態|
|---|---|---|
|IDLE|activeなし|STAGED|
|STAGED|同意/配布を検証し、候補bytesを私有準備|PREPARING / DEFERRED|
|PREPARING|新規受付停止、既存処理の確定・退役待ち|PREPARED / DEFERRED / UNKNOWN|
|PREPARED|静止証拠とnonceを固定、code未変更|START_INTENT / DEFERRED / UNKNOWN|
|START_INTENT|一回実行の開始意図を耐久化|STARTED / UNKNOWN|
|STARTED|SPENT耐久化済み。同じprocessだけが一回effect可能|APPLIED / CANCELLED_NO_EFFECT / UNKNOWN|
|APPLIED|target code観測済み、まだ業務開始不可|CANDIDATE_READY / UNKNOWN|
|CANDIDATE_READY|quarantine起動が合格|ACTIVATING / UNKNOWN|
|ACTIVATING|当該generationの受付再開intentを耐久化|VERIFIED / UNKNOWN|
|VERIFIED|同一操作の稼働証拠/基線/outboxを原子的確定|activeを終結しIDLE|
|DEFERRED|START前に延期。元業務へ安全復帰可能|終結後、次の新しいprepare|
|CANCELLED_NO_EFFECT|同じ生存ownerが効果入口未消費を証明した中止|終結。ただしSPENT/attemptは残す|
|UNKNOWN|どこまで起きたか不確実|既完了の観測的VERIFIEDのみ、または別承認保守|

DEFERREDも履歴を残す。ACTIVEをIDLEへ戻すには、該当nonceと終結証拠が一致すること。UNKNOWNに新grantを継ぎ足して継続しない。

## 4. lock契約

|記号|実体|寿命|
|---|---|---|
|G|release-entry.lock EX|owner/recovery間の変更排他|
|U|data/update.lock EX|既存journal/更新排他|
|Q|runtime-entry.lock EX/SH|code切替/全業務process寿命|
|R|data/run.lock EX|既存writer排他。通信中解放があるため停止証拠にしない|
|C|既存config_edit._edit_lockと対象設定writerの共通gate|private設定snapshotの最終整合|
|A|release-authority.lock|同意/取消/bootstrap/currentness|
|M|monitor Store.tick.lock|端末identity/credential snapshot|
|O|release-owner.lock|短いowner state CAS|

v2の**目標取得順序**は`G → U → Q → R → C → A → M → O`。
現行A→M→OとG→U→R→Aの相対関係を維持する。Cの実体・別設定fileのwriter・support lockとの関係はP0で全callerを照合し、逆順があればsnapshot境界の分離を先に実装する。既存v1全経路が既にこのDAGを満たすと主張しない。
1processで同じO/M/Aの再入はしない。borrowed FDは別openで取り直さず、dev/inode/open-file-descriptionを検証する。
lockはnonblockingまたは共通absolute deadline内の短い試行。partial取得は逆順release。lock内でnetwork、TTY、worker drain、process ready待ちをしない。
例外: G/Uはcode適用～起動確認の間保持できるが、Q/R/C/A/M/Oは各phaseに従い解放する。G/U保持中に外部APIを待つ設計はしない。

|操作|取得するlockと禁止事項|
|---|---|
|consent/revoke|A→O（identity更新を要する場合だけA→M→O）。G/U/Q/Rを待たない|
|metadata取込|A→Oでsnapshot/CAS。通信・暗号検証はlock外|
|prepare intent|G→U→A→M→Oで短く保存し全解放|
|drain待ち|lockを持たず、既存workerがRで結果確定できる|
|PREPARED採用|G→U→Q(EX)→R→C→A→M→O。停止/基線/nonce再測定後全解放|
|grant取得|全lock外。PREPARED fenceは残る|
|START|同順序で再取得。最終CAS、intent、SPENT。A/M/Oは一回入口確認後解放|
|code切替|G/U/Q/R/C。変更後source確認までQを離さない|
|quarantine起動|G/Uのみ残す。Q/R/Cを解放して新processのQ取得を許す|
|activation/終結|G/U保持、必要な短いA/M/OのCAS。R/Oを握ってheartbeatを待たない|
|結果ACK|Oのみ。安全stateを変えない|

管理されたconfig writerも同じgateに参加する。参加しないGUI/手動編集がある場合は最終snapshot変化で拒否し、未知の同一UID操作を強制的に防御済みと呼ばない。

## 5. STAGED：業務を止めない準備

DEV10 `stage_candidate()`:
1. signed Offerのscope、request_nonce、期限、cohort、publication/acceptance/compatibilityを確認。
2. current→target互換性pair、profile、schema、control closure、最小updater、forward ancestryを確認。
3. 適格candidateだけを既存ルールに従ってhighest_seenへ保存。未許可canaryをstable端末へ無差別に観測させない。
4. 私有stageへbounded download。size/hashを検証し、既存pack/tree/依存lock/source検査を行う。candidateをimportしない。
5. 既存整合backup経路でローカル復旧証拠を確認。offsite暗号鍵登録や実復元を勝手に実行しない。
6. 同時存在するdownload/copy/inflated objects/pack/Git objects/worktree/cache/backup/state-tempのピーク容量を計算。各filesystemを別に検査。
7. 完了記録までowner容量が足りるか予約計算し、STAGEDへ保存。

既存Transportは16KiB/10秒のJSON制御専用 [SRC10]。bundleを通すためにこの上限を拡張しない。
`ReleaseArtifactTransport`を同じ承認origin/認証の固定pathに追加する。POSTの小さい取得要求に対し200のoctet-streamだけ受ける。最大256MiB、64KiB chunk、単一総deadline300秒、Content-Encoding identity、redirect禁止。予想sizeを超えたら即中止。Range再開は初版では対応しない。
ダウンロード後もstage FD/path/inode/digestをeffect前に再照合する。失敗で消してよいのは自分の未採用stageだけ。臨床dataや既存履歴の削除は禁止。

## 6. PREPARINGとPREPARED

DEV20 `prepare_runtime()`:
- 保守窓、consent、現在の版、別のupdate/restore hold、予算を確認する。
- operation_id、prepare_nonce、old generation、対象サービス集合、prepare期限を原子的に保存。ここが新規受付停止の線形化点。
- 新規収集/解析/通知/手動変更を受付停止する。既に予約・送信済みの処理は既存の結果/費用/unknown記録規則で確定させる。update延期を臨床の新attemptとして消費しない。
- 各管理processへ既存安全停止の限定要求を送る。完全drainまでRを握らない。既存quiesceの強制killを無条件流用しない。
- 停止確認はPID単独/pgrep単独/古いheartbeat単独では不可。process instance、native birth/boot witness、generation、Q共有holderの退役を照合する。
- resident、sender、gateway/standalone、LINE WORKS、cron child、手動CLI、bookkeepingを対象。LLM daemon等の共有外部processは勝手にkillせず、該当呼出しの予約と結果確定だけを確認する。
- Q EXを取得でき、全管理対象の退役と他holdを確認したらPREPARED witnessを保存して全lock解放。

prepare期限の初期値120秒、rolling24hで最大3回/累積360秒。既存同意が厳しければ厳しい値。長時間inflightがあることを事前観測できればprepare自体を延期する。
期限までにdrainできなければ、code/設定/DBを変更せず、自分のprepareだけを終結し、元々動いていた同一版のserviceだけを再開する。別holdは解除しない。
再起動・start-intent存在・fsync結果不明・old code不一致なら自動準備解除しない。

### 6.1 PrivatePreparedWitness

schemaの`PrivatePreparedWitness`に、root/repo/lock identity、source/依存profile、設定snapshot、DB inode/schema、old generation/boot、service retirement証拠、stage identity、backupローカル証拠、capacity reservationを保存する。
患者DB全体のhash/size/mtimeをwitnessの常時一致条件にしない。clinical更新と無関係な採用保留を作らない。
config/credential/private path/backup hashは端末内だけ。外へはopaqueなlocal_preflight_id/local_backup_evidence_idと公開可能なprofile/commit/nonceを送る。

## 7. grant取得・START

DEV30 `request_start()`はPREPARED後のlock外で行う。TTLは30秒以下。古いnonce、違うboot、違うexecution_revision、古いconsent、違うprofileを拒否する。
grantの発行をSTARTとみなさない。中央がgrantを発行してresponseが失われても、deviceの保存receiptから副作用は生まれない。

DEV31 `consume_start()`:
1. G/U/Q/R/C/A/M/Oを短いdeadlineで取得。
2. private witness、consent/currentness、grant、source/stage、別hold、予算、root、世代を再検証。
3. 無関係なACK変更は許し、最新stateにmergeする。execution_revision変化は拒否。
4. grant期限・保守窓を、重い検査後の新しい時刻で再確認。
5. `release-start-intent.json`を同じop/grant/nonce/hashで耐久化。失敗/不明ならeffect禁止。
6. 同じowner保存でSPENT、attempt_key、STARTED、必要な記録容量予約を確定。file/directory同期成功後にのみRAM capabilityを発行。
7. journal参照を固定し、同じprocess・同じFD群・同じ生存ownerの一回入口をclaimする。
8. 最初のrepo書込み直前にも期限とlocal revokeを確認。開始後にscope/targetを変更しない。

grant deadlineはwall clockとmonotonicを対応させ、clock rollback/許容外不確実性で開始を拒否する。再起動後にmonotonic leaseを再生しない。
STARTED後に期限が切れても、既に始まった同じ有限操作の整合性確定を別のgrant取得で延長しない。新effect/retry/別targetは許さない。

### 7.1 START前のgrant紛失と再試行

同じrequest_id/body hashのstart要求は同じgrant bytesを返す。timeout時は同じrequestのstatusを読む。
期限切れgrantを新しいoperation IDで回避しない。
まだintent/SPENTがなく、同じPREPARED nonceを正確に終結できる端末は、ローカルにprestart-abort証拠を耐久保存してから中央`cancel-prestart`へ送る。中央は同じgrant/requestをcancelled_prestartへCASし、次のprepareを許可できる。
旧grantはローカルのnonce/operation終結により無効になり、中央でも失効する。取消ACK喪失は同じbytesでstatus照合する。結果不明のSTART intent、SPENT、active effectがある操作にはこのAPIを使わせない。
中央はgrant発行後に単に期限が切れたことを「端末では未実行」の証拠にしない。自動配信の次batchは未確認開始を解消するまで進めない。

## 8. 同じownerのv2 effect

現在の`update_apply._apply_pipeline`はv1のrollback可能なbailoutを持つ [SRC02]。
v2は同じowner内の明示的`apply_code_only_v2(capability)`へdispatchする。capabilityはprivate live factory以外では作れず、公開CLIのboolean/JSON/IDで代用しない。
共通Git/保護path/journal等の小さいprimitiveを抽出して再利用するが、v1の失敗policyをv2へ継承しない。

順序:
1. STARTEDと生存capabilityを再確認。
2. 固定bundleを制限Git環境で本体へimport。未検証objectをネットワークfetchしない。
3. exact source_commitへff-only。tagを再解決しない。untracked/ignored衝突を`git clean/reset --hard`で消さない。
4. HEAD、tree/index、full-source、control closure不変、依存profile、DB schema、設定不変を再測定。
5. APPLIEDをownerへ保存し、対応journalも記録する。途中の不一致はUNKNOWN。自動rollbackなし。
   v2通常code-onlyはlegacyの汎用`--post-merge`（migration/再導入を含み得る）を呼ばない。固定trusted closureの読取り検査で済ませ、targetアプリの実行は次のquarantineへ分ける。v1の3FD child ticketをv2の8lockへ読み替えない。
6. 全effect子が退役したことを証明してからQ EXを解放する。

Gitのsystem/global/local設定、hook/filter/fsmonitor/replace/alternates/includeの影響を排除または検査する。候補のsymlink/submodule/.gitattributes等を既存検査に従い拒否する。
Git/import/ファイルI/Oのtimeoutは対象childの退役まで含む。child生存不明のままlock/fenceを解放して成功としない。

## 9. Quarantine、activation、VERIFIED

DEV40: G/Uを保持して別更新を排除し、Q/R/C/A/M/Oを解放してtarget processをquarantine起動する。
新processはQ SHを取り、trusted bootstrapのactivation token/target generation/source/runtimeを検証する。app constructor/importで外部通信・schema migration・自動restore・通知を始めない。

ready証拠は、target release/commit、profile、control closure、runtime_generation、boot witness、process_instance_id、native PID/birth witness、service_id、起動nonce、fresh heartbeatを束縛する。
loaded identityは、Q下で検証した起動sourceと実import rootの証拠から作る。ディスクHEADを問い合わせるだけの自己申告は禁止。
必要service集合はprofile/configから固定する。常駐serviceは生存、新たなtimer/CLIは正しい起動設定とoffline selfcheckで確認し、非実行中の定期jobまで常時PIDを要求しない。
設定上無効なserviceはstoppedがdesired state。既存Human/restore/update holdは新更新の成功で消さない。

DEV41:
- 全quarantine proofを検証→CANDIDATE_READYを保存。
- activation intentを同じoperation/generationで耐久保存→ACTIVATING。
- 自分の更新fenceだけを解放し、そのgenerationだけの受付を許可する。
- fresh heartbeatを2回（初期5秒以上の間隔）観測、全necessary serviceとsourceを再照合。
- O下で最新stateへ**outcome、verified_installed、active終結、最小fixed outboxを一つのatomic保存**で確定。ここがVERIFIEDの線形化点。

`readiness=ready`はdesired stateに一致する意味。既存の個別送信unknown/課金保留は残したままでもよい。global restore hold等で通常更新自体が禁じられるケースは開始前に拒否する。
中央のACK、実MCSへの試験投稿、患者宛て通知、LLM課金呼出しを成功条件にしない。
稼働証拠に不確実性があればAPPLIED_UNVERIFIED/UNKNOWNを表示し、コードを戻さない。

## 10. 成功後の連続更新

terminal outcomeを持つoperationはactiveから外すがSPENT/attemptを残す。次のreleaseはそのverified_installedをbaseとして新しいoperationを作れる。
outbox未ACKでも次更新可。ACKはreport_idとoutcome hashが一致したentryのacknowledgedだけを変える。
source successorは完全な認証済みtarget観測とVERIFIEDの同じterminal保存からのみactive baselineへ採用する。現行v1のpending successorを推測で昇格しない。
二回目更新がclosure/bodyの古いself fingerprintで永久拒否されないよう、全source baselineと不変control closureを別のpointerとして保持する。

## 11. crashと復旧

|観測|自動で可能|禁止|
|---|---|---|
|STAGEDのみ|再検証・再取得・延期|code effect|
|PREPARING/PREPARED、intentなし、旧code一致|自nonceの準備終結と元service再開|他hold解除|
|intentあり/SPENT不確実|UNKNOWN保存・読取り診断|intent削除→再開始|
|同じ生存ownerが未入口を証明|CANCELLED_NO_EFFECT、SPENT保持|自動attempt枠復活|
|START後crash、HEAD旧版|UNKNOWN|未実行断定|
|target code、process旧版/不明|APPLIED_UNVERIFIED/UNKNOWN|新grantなしで追加再起動|
|同一opのtarget processとactivationが既に成立|trusted recoveryが観測的にVERIFIEDを確定|旧grantで新しいeffect|
|VERIFIED後ACK喪失|同じbytes再報告|再適用|
|全disk復元/端末copy|中央floor/realmと照合して別承認再導入|fresh enrollment扱い|

v2の自動復旧は、**既に完了した同一操作の観測的確定**まで。新コード変更・不足service起動・DB復元が必要なら別承認保守。
復旧controllerは本体をimportせず、自分の固定closure/support世代を使う。entrypoint＋4support＋manifest/hash＋旧.prev参照を一式として検証する。

### 11.1 v1→v2移行

全sidecarを分類する: owner/activation、bootstrap、planning、plan、change、successor、authority、依存receipt、update journal、backup/restore hold。
P0で実Store.NAMESとcallerから一覧を生成し、未列挙recordがあれば自動移行を拒否する。
v1 spent/unknown、pending successor、未解決restore/start intent、applying/stages途中は移行禁止。
移行intent（before hashes、new schema、root/repo/lock、bridge evidence）を保存→旧bytesを不変保存→v2正本atomic replace→配備closure/loaderを再検証→同意取得。
途中停止のresumeはこの同じintent/before/afterにだけ従う。旧owner/marker削除や「壊れているので初期化」で旧経路を開かない。

## 12. 有界性と運用

- JSON制御: raw/envelope最大16KiB、署名payload最大8KiB。private witness最大64KiB、owner最大8MiB。
- bundle最大256MiB、既存inflated上限1GiB、tree entries最大50,000。全pack/objectsの既存上限も維持。
- prepare120秒、grant30秒、metadata TTL既存24h、plan30分/preflight5分以下。download300秒。
- apply/healthは既存上限と新profile上限の小さい方。新profileは各deadlineを必須とし、初期合計600秒以下。期限後に子生存不明ならUNKNOWNで保留。
- check周期600秒＋0〜120秒jitter。既存schedulerから唯一ownerへ要求し二重登録しない。
- 保守窓はtimezone/曜日/開始分/終了分を同意へ含める。初版は日跨ぎ窓を二つの窓へ明示分割する。DSTをzoneinfoで扱い、境界の曖昧さは開始を延期。
- 日時はwireで整数Unix ms、local deadlineはmonotonic。基準時刻の不確実性を勝手に広げない。

owner容量はencoded bytesで最悪終結・outboxまで予約する。heartbeatを履歴へ無限appendしない。ACK再送も同じentryを使う。
履歴の自動削除/容量上限引上げは行わない。70%で警告、85%または厳密予約量不足で新STARTを止め、必要な保守を通知する。remaining_capacityはbytesと予約可能操作数を表示し、無期限無保守を保証しない。

## 13. 外部への最小開示

wireは新schemaのallowlistのみ。患者名/ID/件数、本文、添付、hostname、username、絶対path、config全体hash、backup実hash、任意stderrを送信しない。
local_preflight_id/local_backup_evidence_idは無作為IDで、ローカル証拠との対応表は端末内だけ。
ログは固定エラーコードと公開release/op IDだけ。URLのquery、署名、credential、秘密入力理由全文を出力しない。
例外cleanupやtimeoutでも同じ規則を守る。合成canaryでwire/outbox/log全経路を検査する。

END-DEVICE-PROTOCOL-V2.0

<!-- END 02_DEVICE_PROTOCOL.md -->

<!-- BEGIN 03_CONTRACTS.md -->

# 03 — データ契約、API、永続化、意味検証

本章と`contracts/contracts-v2.schema.json`が新v2契約の正本。既存のrelease core/trust/attestation v1はそのまま再利用する。**文書版2.0は、既存製品schema全体をv2に変える指示ではない。**
新APIは実装する論理契約であり、配備済みURLではない。SDCサーバーの実リポジトリを確認して既存認証・保存基盤へ実装する。確認不能な間もclient/validator/合成serverの実装は進め、実環境への接続だけをOFFとする。

## 1. 構造検証とcanonical bytes

`$defs`の名前を指定して検査する。schema rootに対して単にobjectとして検査しただけでは合格にしない。
全objectはclosed、全fieldが必須。nullableは明示したfieldだけ。JSON Schemaは構造の規約であり、下記の意味検証・署名・独立した期待値との一致を代替しない。

### ENC01

通信入力は最大16,384 bytes、JSON depth最大32、総node数最大10,000、文字列のdecoded文字数とraw byte上限の両方を検査する。重複キー（escape後の同一キーも含む）、NaN/Infinity、float、`-0`、bool-as-intを拒否。unknown schema/propertyは補正しない。
署名payloadは既存`adapters.sdc.support_contract.canonical/parse_canonical`に合わせたASCII CJSON、最大8,192 bytes。鍵順序、escaping、integer表現を同じ公開vectorで検証する。Pythonの`json.dumps`任意設定を別canonical方式として持ち込まない。
base64urlはpaddingなしの一意表現だけ。decode→encode同一性、PS256 signature 384bytes、既存3072-bit公開鍵SPKI検査を維持する。新しいcryptoライブラリや暗号アルゴリズムを収集・解析coreへ追加しない。

### ENC02

boot_idは端末installation固有saltとOS起動identityから作る擬名witnessとし、hardware serial/hostname/生OS boot識別子を送らない。
wireの時刻はUTC Unix **milliseconds**、非負safe integer（2^53−1以下）。単位を秒・nanosecondsと混ぜない。
privateなstatのdevice/inode/mtime_ns/ctime_nsは`UInt64Decimal`（先頭ゼロなしの10進文字列）とし、意味検証で0..2^64−1へ制限する。inodeは0を拒否する。nanosecondsをsafe integerに押し込むと実際の時刻が範囲外になるため、この表現を採用する。これらprivate値をwireへ送らない。

### ENC03：hashの定義

既存のrelease_id / manifest_digest / trust_digestは既存のdomain-separated関数を維持する。新v2の値は以下で固定する。

```text
H(kind, payload) = sha256(ASCII("MCS-AUTO-v2\n" + kind + "\n") + canonical(payload))
body_sha256      = sha256(送受信・保存するexact raw bytes)
preflight_sha256 = H("prepared-public", PreparedPublic)
acceptance_sha256= H("release-acceptance", ReleaseAcceptance)
asset_set_sha256 = H("asset-set", name順にsortしたAssetSpec配列)
compatibility_sha256 = H("compatibility", 検証済みCompatibility payload)
request_id(BuildRequest) = H("publication-request", {repository_id,tag,tag_oid,commit,acceptance_sha256,publisher_policy_sha256})
attempt_key = H("automatic-attempt", {scope,source_target_commit,profile_sha256,config_contract_sha256,data_contract_sha256})
```

attempt_keyにtag、publication ID、日付、policy epoch、artifactの再圧縮digest、operation IDを入れて消費済み枠を迂回させない。通常の同じtarget commitへの自動STARTは同じattempt_keyとなる。
Publicationの同一tag予約はrequest_idより強いunique制約を持つ。後でacceptanceが更新されても同じtagで新しいbytesを公開しない。

### ENC04：署名の用途

新v2は`SignedEnvelope`のpurposeを明示し、既存署名プリミティブのdomain-separated署名message規則を使う。鍵pinの用途allowlistを新v2だけに限定追加する。旧v1のpurposeをv2として解釈しない。

|purpose|payload|役割|
|---|---|---|
|mcs-auto-offer-v2|Offer|端末別候補・no-candidateの証拠|
|mcs-auto-compatibility-v2|Compatibility|検証済みbase→targetの互換性|
|mcs-auto-grant-v2|StartGrant|一回の開始許可|
|mcs-auto-ack-v2|OutcomeAck|結果bytesの受領だけ|
|mcs-auto-status-v2|OperationStatus|request/受領の観測。開始権限ではない|
|mcs-publication-plan-v2|PublishPlan|限定publisherが扱う固定asset集合|
|mcs-release-acceptance-v2|ReleaseAcceptance|正式な検証受入|

rootは既存2-of-3とchain更新を維持。日常release署名keyとdevice grant keyは用途・アクセス権を分離する。公開鍵は初回bridgeで独立確認し、HTTP応答から新keyを自己登録しない。
BuildRequestのupstream_run_id/attemptは両方存在または両方null。nullは認証済みmanual/composed受入だけで許可し、workflow_runの対象SHA確認を省くためには使わない。

署名payloadが8KiBを超える場合は拒否する。上限拡大で済ませず、対応profile/asset数を減らすか、詳細証拠をcontent-addressed referenceへ分離する。大きいcompatibility catalogueはprofile別envelopeを最大4件まとめる保管物で、端末には該当envelope一件だけを返す。

## 2. 構造に加える必須意味検証

|ID|対象|必須検証|
|---|---|---|
|SEM01|全wire|ENC01、独立expected scope、canonical一致。入力自身をexpectedとして渡さない|
|SEM02|SourceRef|repo/owner ID登録値、厳密tag、各SemVer桁2^31−1以下、実tag OID/peeled SHA/main祖先|
|SEM03|ReleaseRef|release_idは認証core hash、commit/size/hash/code_seqは同coreに一致|
|SEM04|ReleaseAcceptance|6laneは重複なしexact集合。対象SHA、policy、署名actor、release checksが一致|
|SEM05|full_run|全lane executed/success、source_commit=target、jobのexact run/attempt/source。未実行は0|
|SEM06|composed|failureには解消証拠、reusedには元SHA/影響レビュー、local_equivalentには実測証拠と承認。未解消製品failureを拒否|
|SEM07|PublishPlan|request/asset集合/hash/seq/leaseを中央reservationへ照合。公開前後の同じID/bytes|
|SEM08|PublicationRecord|immutable公開証拠、全assetのID/size/digest、source、受入票の一致。suspended中はgrant禁止|
|SEM09|Compatibility|target/profile/base組を認証coreと実baselineに照合。schema/profile/config/data/control全て同じで禁止flagは全false|
|SEM10|StandingConsent|確認receiptを実TTY/人承認経路へ束縛。有効期日、enabled、scope、profile、epoch、中央方針一致|
|SEM11|MaintenanceWindow|weekdayは月曜0、start<end、同weekday重複なし。跨ぎは2record。IANA timezone実在、DST欠落時は窓なし、重複時でも同attemptは一回|
|SEM12|Offer|request_id/nonce/profileを保存要求に一致。candidate⇔selected!=nullかつELIGIBLE。none⇔selected=nullかつ理由!=ELIGIBLE|
|SEM13|Offer時刻|issued<=not_before<expires、TTL<=600秒。期限切れは新Offer照会、candidate実行許可へ昇格しない|
|SEM14|metadata|旧core/trust/attestationの期限・root/epochを維持。新しいmanifest_digestは古いplan/grantと混ぜない|
|SEM15|PreparedPublic|installed/current/target/profile/nonce/generationはprivate witness由来。患者hash/設定hashを入れない|
|SEM16|PrivatePreparedWitness|root/repo/lockの同一性、private snapshot、同じstage、backup、全retirement、容量予約。UInt64範囲を確認|
|SEM17|StartRequest|preflight hash再計算、consentと保守窓、publication/compat/manifest、attempt未消費、同operation一件|
|SEM18|StartGrant|同StartRequestの全binding一致。issued<=not_before<expires、TTL<=30秒、窓/consent/metadataの期限以内|
|SEM19|開始時刻|最新wall/monotonicとdeadlineを照合。boot変更、clock後退、1秒を超える既存許容外skewは開始禁止|
|SEM20|StartIntent/SPENT|op/grant/nonce/attemptに一意。intent→SPENT同期成功後だけ同processRAMcap。読取りだけで再発行しない|
|SEM21|PrestartAbort|ローカルintent/SPENT不在と同nonce終結receiptを確認。中央は同grant/operationの未消費予約だけCAS。timeout単独では未START認定しない|
|SEM22|RuntimeProof|exact必要service集合、source/profile/control、boot/generation、fresh birth/heartbeat/activation。同一PIDだけは不十分|
|SEM23|ServiceProof|residentはrunning＋instance/PID/birth/loaded非null、2heartbeat。timerはarmed＋設定検査、disabledはstopped。各proofは対象opへ束縛|
|SEM24|Outcome verified|readiness=ready,error=NONE,verified_at非null,installed=verified_installed=target。START<=verified<=observed|
|SEM25|Outcome unknown|readiness=unknown,verified_at=null,error!=NONE。既知installedと以前のverified_installedを保持できる|
|SEM26|Outcome cancelled|readiness=not_started,error=NO_EFFECT_CONFIRMED,verified_at=null。同生存ownerの入口未消費証明が必要|
|SEM27|OutcomeAck|request_id=Outcome.report_id、nonce=Outcome.request_nonce、report/op/scope、hash=sha256(canonical Outcome bytes)。受領は実行stateを変えない|
|SEM28|OperationStatus|元StatusRequestのID/nonce/kind/object/op/scope一致、TTL<=60秒。not_foundはobject_sha256=null。これをcapabilityにしない|
|SEM29|OwnerState|active/spent/outcomes/bindings/outbox参照整合、seq/世代高水位非後退、operation/grant/attemptの一意、verifiedは終結証拠を持つ|
|SEM30|Owner revisions|全保存でrevision増分。authority/active/baseline/generation変更でexecution_revisionも増分。ACKだけはexecution_revision不変|
|SEM31|記録容量|raw owner<=8MiB、private witness<=64KiB、最悪完了＋outbox＋同期tempの容量予約。schema配列max未満だけでは不十分|
|SEM32|outbox|rawの正確なhash、parsed OutcomeのID一致、同ID異bytes拒否。再送でreport_id/sequence/observed時刻を作り直さない|
|SEM33|再導入|owner/activation/root/外部floor不整合や古いdisk snapshotをfresh installと認定しない|
|SEM34|境界値|enum・型・max bytesの全境界、bool/int混同、ns整数精度、NFC/case衝突を合成vectorで確認|

`RuntimeProof.services`の観測上限は64。インストールprofileがそれ以上なら無言truncateせず未対応としてHOLDする。
source/baselineへの変更とoutbox受領は独立revisionで扱うが、これを理由にconfig/credentialの真の変更を無視してはいけない。

## 3. SDC APIの具体的な追加面

単一の登録済みHTTPS originに以下の**固定POST path**を追加する。既存monitor/support/restartのPATHSは広げず、専用v2 transportのallowlistを用いる。既存pathと衝突する場合はサーバー側実装前にbase pathを一括版管理し、任意URL入力は追加しない。

|path `/v2/mcs/releases/…`|request $defs|成功response|副作用|
|---|---|---|---|
|select|SelectionRequest|200 SignedEnvelope(Offer)|候補選択。インストール許可なし|
|metadata|MetadataRequest|200 既存attestation envelope または SignedEnvelope(Compatibility)|同releaseの正確なmetadata。kindで閉じる|
|artifact|ReadArtifactRequest|200 application/octet-stream|同digestのbytesのみ。実行許可なし|
|start|StartRequest|200 SignedEnvelope(StartGrant)|一意の中央grant予約。端末START済みとはみなさない|
|status|StatusRequest|200 SignedEnvelope(OperationStatus)|保存済みrequest/ACKの観測のみ|
|cancel-prestart|PrestartAbort|200 SignedEnvelope(OperationStatus)、kind=prestart_abort|同じ準備の取消。spent/unknown解除不可|
|result|Outcome|200 SignedEnvelope(OutcomeAck)|exact bytesの冪等受領のみ|

端末認証は既存device/key/time/nonce/signatureのHMAC contractを専用pathへ拡張し、body/path/method bindingを維持する。ヘッダーnonceはHTTP送信ごと、payloadのrequest_nonceは論理requestのretryで固定。サーバーはcredentialからtenant/device/realmを解決し、body自己申告だけで権限を選ばない。
metadata応答の既存envelopeはrequest nonceを含まなくてもよいが、受信側が独立して固定したrelease_id/manifest_digest/profileと完全一致させる。期限更新でmanifestが変わった場合はselectから新たに計画し直し、PREPARED中に勝手に切り替えない。

errorは`{schema_version:"mcs.release-error.v2",code:<固定enum>,retry_after_ms:<0..86400000>}`のclosed object、最大1KiB。固定コードは末節参照。401/403/409/410/413/422/429/503を用途で区別する。HTML、302、曖昧な202、長すぎる本文、構文不正は成功にしない。
`result`のACK TTLは最大300秒。遅れたACKはsame raw Outcomeを再送してfreshなACKを取得できる。実行stateへ影響させない。
`status`のnot_foundは「サーバーにその記録がない」であり「端末では何も実行していない」の証拠ではない。

### 3.1 artifact streaming

リクエストは16KiB以内の認証JSON。応答はContent-Type=application/octet-stream、Content-Encoding未設定またはidentity、予想Content-Lengthとサイズ一致を検査する。長さが無ければ累積bytesで同じ上限を強制する。hashはstream中に計算する。通常TLS検証、proxyなし、redirectなし、総deadline300秒、chunk64KiB。
中央のGitHub取得だけは登録済みrepo/release/assetからの200/302を扱う。LocationのHTTPS host allowlist、port/userinfo禁止、最大3redirect、private/link-local/loopback解決先拒否、host変更時Authorizationを転送しない。一般URL fetch/proxyにしない。設定したGitHub download hostが変わったら保守で検証し、ワイルドカード全host許可に拡張しない。
初期実装はRange/部分再開なし。途中ファイルは非採用として管理し、次回再検証済みの同bytesを最初から取得する。

### 3.2 Publisher専用の中央API

端末credentialでは利用不可。01のOIDC条件を満たすpublisherだけが使用する。device grant signer/root管理には到達させない。

|path|method/input|response|契約|
|---|---|---|---|
|`/v2/mcs/publications/reserve`|POST BuildRequest|PublicationReservation|同tag/sourceの一意予約とcode_seq。ここではasset未確定でもよい|
|`/v2/mcs/publications/objects/{sha256}`|PUT raw bytes、固定publication IDとfencing token header|200/201のdigest/size receipt|最大256MiB、create-only。pathは64hexだけ。byte hash一致と予約を検証|
|`/v2/mcs/publications/finalize`|POST PublicationFinalizeRequest|SignedEnvelope(PublishPlan)|既存core validatorとreservation seq/source、target lock/profile、acceptanceを検証して限定release署名とmetadata asset生成|
|`/v2/mcs/publications/register`|POST PublicationRegisterRequest|PublicationRecord|公開済みのexact GitHub ID/assetとtrusted verifier証拠を照合して登録|

reserve/finalizeのmetadataは128KiB以下、payload内のrelease coreは8KiB以下。これはサーバーのpublisher専用面の上限であり、既存device transportを広げない。
finalizeのcandidate_assetsはbundleと説明画像だけ。reservedな4metadata名（core/initial attestation/compatibility/acceptance）は中央が認証済み材料から生成して固定する。任意payloadへの汎用sign APIを作らない。
packagerはcode_seqが必要なcoreを先に確定しない。bundle bytesとcore材料を準備→reserve採番→core形成/検査→finalize署名→全asset集合固定と進む。tag/source treeは一切変更しない。

registerで`release_verified=true`というcallerのbooleanだけを信じない。専用verify-register jobの実OIDC、承認verifier closure/toolchain、当該run/attempt、保存済みGitHub原証拠hashを束縛する。中央もGitHub APIからRelease ID/tag/immutable/asset集合を再照合する。
中央runtimeがGitHub CLIを実行できるとは仮定しない。公式CLIの暗号検証は固定したverifier jobが行い、その結果を上の認証済み契約で受領する。中央で独立検証する実装が存在するなら追加できるが、未実装の検証を実施済みと表示しない。
同OIDC/jtiを別publicationの書込みへ流用させない。retryは同request/hashだけ。これらのendpointは実backendへ対応付けるまで呼び出さない。

## 4. 中央の永続化（既存DBへ追加する論理table）

製品側はstdlib制約を維持する。以下はDB製品選定ではなく一意性とtransactionの契約。既存SDCが使うDB/transactionで実現し、別の遠隔操作基盤を作らない。

|table|主キー/unique|内容|
|---|---|---|
|release_requests|request_id PK; unique(repo_id,tag)|tag OID、SHA、受入票、code_seq、phase、revision、fencing token|
|release_objects|sha256 PK|write-once bytes、size、content type。公開アセットと原証拠を分類|
|release_bindings|unique(stream_id,code_seq); unique(stream_id,release_id)|同sequence別core拒否|
|release_publications|publication_id PK; unique(repo_id,github_release_id)|PublicationRecordと不変asset集合|
|release_device_policies|scope PK|同意id/epoch、policy、profile、device realm、停止、verified floor|
|release_assignments|unique(rollout_epoch,device_id)|固定cohort/target/期限、stage、観測状態|
|release_start_requests|unique(device_id,request_id); unique(device_id,operation_id)|request hash、exact grant bytes、expires、state|
|release_attempts|unique(device_id,attempt_key)|reserved/consumed/unknown/cancelled_no_effect/verified、元operation|
|release_outcomes|unique(device_id,report_id); unique(device_id,operation_id,report_sequence)|exact raw/hash、受領時刻、正規の状態遷移|
|release_policy_audit|audit_id PK|同意・停止・復旧の認証済み変更。患者情報なし|

### TX-PUBLISH

予約採番はtransaction内でstream seqを増分し、同じtagへ同じcode_seqを割当てる。phase updateは`WHERE revision=expected AND fencing_token=current`でCAS。ゼロ行更新は競合であり成功ではない。
古いpublisherがGitHub APIを既に送った可能性はfencingだけで消せない。そのため各writeは同じ固定Release/asset集合だけへ収束させ、tag再読取り・exact ID・digest検査を必須にする。新lease取得時も公開結果を照会し、旧run不明を理由に異なるbytesを採用しない。

### TX-START

1. 認証device/epoch、同意、window、publication、rollout、profile、現在のbaseline/floorを照合。
2. request IDが既存ならbody hash一致で**同じ保存grant bytes**を返す。不一致は409。
3. 同operation別request、同attempt既消費/unknown、他active reservation、local/central holdがあれば拒否。
4. preflight age<=5分、同generation/boot、manifest TTLを確認。TTL30秒以下のgrantを発行してexact bytesを一意予約としてcommitする。
5. commit後の応答喪失は再生成しない。clientは同request再照会。期限切れはstatus/明示prestart abortの契約へ。

中央は署名鍵を渡さず、認証済み内容だけをsignする。外部signerとの原子性がなければoperation intent→署名digest固定→commitの段階を持ち、曖昧なsign返答で別grantを生成しない。

### TX-ABORT

2. deviceがローカル未STARTを証明した同じop/prepare_nonceのabortを保持する。同request/hashは冪等。中央に結果unknown/consumed/verifiedがある場合は取消不可。未START予約を終結した後だけ次prepareを許可する。攻撃者による全disk rollbackをclient申告だけで消さず、realm/高水位不一致は再導入保留。

### TX-RESULT

同report ID+同bytesはACKを再返却。異bytesは409。operation/grant/attempt/targetを中央保存の発行記録に照合。
状態はunknown→verifiedへの**観測的な後続report**のみ許容し、verified→unknown/別targetやsequence後退は拒否。後続reportは新report ID/次sequenceを取り、旧rawを上書きしない。
中央の成功受領はdeviceローカルの終結を作らない。deviceの次grantは自身のverified baselineと中央受領との同期確認を必要とするが、前のACKをdeviceが受け取っていないだけなら同body再送で解消できる。network断時に現在アプリを停止させない。

## 5. ローカル永続状態と多ファイル境界

正本`release-owner.json`に`OwnerState`、同じ既存activation fenceを維持する。state保存はO下のload→意味検証→whole-state CAS→新tempへwrite→fsync→atomic replace→directory fsync。
statのidentityはnofollow/private owner/mode/nlinkの既存検査を維持。正本のinodeはatomic replaceで変わるため、永続identityとして固定するのは**lock inode/root**であり、正本inodeそのものを次保存まで不変と要求しない。

|record|保存・読取り条件|削除/再利用|
|---|---|---|
|release-owner.json|唯一のv2正本、最大8MiB|旧SPENT/outcomeは消さない|
|release-owner-activation.json|legacy拒否の永続fence|削除して旧入口を開かない|
|release-start-intent.json|START前、exact op/grant/hash|完了証拠をownerへ保存後、旧intent bytesを証拠保管したうえで次op用へ原子的置換可。消失だけで未STARTとしない|
|release-auto-private.json|private witness、64KiB以内、op/nonce binding|確定済みopのcleanup方針は自分のstage等に限定。元証拠のhash/referenceはownerへ残す|
|release-runtime-proof.json|op/generation/boot/sourceの限定proof|同世代のbounded snapshot。履歴は終結digestをownerで保持|
|既存update_state.json|既存journalへのv2 operation参照|勝手に全体初期化しない。ownerとの不一致はHOLD|
|既存v1のbootstrap/planning/change/successor等|移行前全件棚卸し|unknown/pendingを破棄・成功化しない|

上の新filenameは`Store.NAMES/LIMITS`へ明示追加し、任意filenameを許すAPIを作らない。private witnessに存在しないファイルはmissingを明示する別source一覧へ記録し、架空のinodeで埋めない。

**改訂補足**: 実装時に必要な証拠が最大64KiBへ収まらないprofileは未適格とする。上限超過をsilent truncationしない。通常activeの全証拠とgrant/manifest/consentのrawが結果確認時に解決できることをcapacity予約に含める。
大きい旧owner/source manifestは`StoredEvidence`でprivate object_key（SHA-256名）へ参照する。保存先はcontrol root直下の固定`evidence/` directoryだけ、最大8MiB/file、名前は64桁hash、no symlink・single-link・0600。新規publishはO_EXCLまたは同bytes照合、overwrite禁止。v1 rawを16KiBに切り詰めない。operation_evidenceはmanifest/compatibility/consent等の署名rawを保ち、参照不明はHOLD。object_key=raw SHA256を要求する。
OwnerState.lock_fingerprintsはG/U/Q/R/C/A/M/Oの導入済みinode等を束縛し、全開始・復旧で再照合する。startup_nonceはActiveOperationで起動前に保存し、新process自己申告から期待値を作らない。installed/verifiedの全source manifest digestも別々に更新する。

ownerとjournal/intentは同時commitできない。ownerは権限消費・終結の正本、intentは曖昧な未実行扱いを防ぐfence、journalはeffect位置の補助とする。crash時に整合する正本へ観測だけで収束できなければUNKNOWNを保持する。

## 6. エラーと再試行の分類

|code|表示/分類|自動再試行|
|---|---|---|
|NO_CANDIDATE|候補なし（有効なsigned noneのみ）|通常poll|
|NETWORK_UNAVAILABLE / RATE_LIMITED|取得または認可保留|START前の同requestのみ、backoff/Retry-After|
|SIGNATURE_INVALID / SCOPE_MISMATCH / CONTRACT_INVALID|検証失敗|同候補の無限retry不可。状態変化/人確認まで保留|
|PUBLICATION_UNVERIFIED / PUBLISH_PERMISSION_HOLD|公開証拠/権限不足|安全なread再照合。createを反復しない|
|COMPATIBILITY_HOLD / PROFILE_UNQUALIFIED|保守更新が必要|通常auto不可|
|CONSENT_REQUIRED / CONSENT_REVOKED / OUTSIDE_WINDOW|端末の許可条件不成立|同意/時刻の正規変化後だけ|
|PREPARE_BUSY / PREPARE_BUDGET_EXCEEDED|業務優先の延期|準備予算内だけ|
|WITNESS_CHANGED / START_EXPIRED|開始しない|未STARTの正式終結後、新prepare|
|ATTEMPT_SPENT / START_OUTCOME_UNKNOWN|一回性保留|旧grant/別IDで回避しない|
|CAPACITY_HOLD / STORAGE_UNKNOWN|容量/保存の不確実性|START前のみ是正後再計画。後は観測/人対応|
|APPLIED_UNVERIFIED / RUNTIME_HOLD|新版稼働未確認|観測的確定のみ。無承認restartなし|
|LEGACY_STATE_HOLD / RECOVERY_GENERATION_HOLD|移行/復旧器未適格|bridgeの別承認|

NO_CANDIDATEはsigned Offerのstatus説明であり、未署名errorを根拠に正常な「候補なし」へ移さない。それ以外のAPI error codeはこの表の固定値に限定し、生exception、絶対path、hostname、原log、backup digest等を送らない。詳細なローカル原因はprivate診断へ格納し、外部用DTOはallowlistから新たに構築する。

END-CONTRACTS-V2.0

<!-- END 03_CONTRACTS.md -->

<!-- BEGIN 04_IMPLEMENTATION_PLAN.md -->

# 04 — Codex実装手順と変更位置

## 1. 着手時の固定手順

1. `AGENTS.md`、`SECURITY.md`、変更対象配下の指示を読む。作業checkoutと稼働`~/.mcs`を混ぜない。
2. 現在のHEADを記録。本書の`e85763b5addaabee516f6f69b8ea7465f26fe9d8`との差分を取得。新しい変更を削除して古い設計へ戻さない。
3. `git status --porcelain`で他者変更を保持。開発用worktreeで実装する。push/PR/merge/tag/公開/配備は行わない。
4. 本書全章、FIELD_CATALOG、schema、interfaces、受入表を読む。名前だけ読んで実装を開始しない。
5. 以下P0の一覧をリポジトリ内`docs/dev-records/<作業ID>-auto-update-inventory.md`に生成する。接続先不明は具体的に記録し、独立した契約・合成試験は進める。

## 2. P0：入口・ロック・証拠の棚卸し

### P0-a 更新経路

`apply`, `_apply_pipeline`, `cmd_check`, `--post-merge`, `rollback`, `recover`, `_run_native_release`, `install.sh`, `mcs_upgrade`, `reinstall_done`, `settings_migration_receipt`の**全caller**をgrepする。結果は切り詰めず保存。
callerごとにsource file/function、起動profile、親process、既存G/U/R/A/M/O/C、実dataの変更、外部送信、自己再起動、借用FDを記録する。
facade `mcs_update.py`への追記だけで修正を終わらせない。直接importするcallerとfacade経由が同じ実装へ到達することを試験する。

### P0-b runtime/writer

writer_lockの全acquire/unlocked_transport/wait_run_lock、SQLite connection/transaction、通知sender、課金reservation、OCR、backup/offsite、各手動CLIの入口を調べる。全てのSQLを今回変更するのではなく、管理対象起動・新規admission・既存結果確定の境界へ接続する。
Hermes/standalone/LINE WORKSをprofile別に一覧化。3workerや追加定期jobを固定2workerと誤認しない。無効service、共有LLM daemon、監視・復旧自身を区別する。

### P0-c locks

Cの正本は`config_edit._edit_lock`の実配置を`git ls-files`/`git grep`で解決する。`mcs/ops/config_edit.py`が存在するという推測を置かない。split provider設定、credential変異、support stateの追加lockも含める。
各関数に「取得済みlock」「取得するlock」「解放」「FD継承」「外部callback」「待機」を記録。02の目標DAGへの逆向きedgeはsnapshotの分離で解消する。nested callを丸ごとR外へ移すだけの変更は禁止。

### P0-d baseline/移行物

Store.NAMES/LIMITSと全read/write callerから実filename表を生成する。owner、activation、bootstrap、planning、plan、change、successor、authority、runtime dependencies receipt、update/restore journal、recovery `.prev`/support世代を分類する。
`deployment/sdc-release-lock.json`欠損は開発fixtureで隠さず「実profile準備待ち」。生成手順は実装可能だが実環境の値を推測してcommitしない。

P0完了条件: 現行経路対応表、実lock DAG、bridge対象、backend integration slots、今回触らない機能が明確。これらを製品受入テストのfixture設計に反映する。

## 3. 作業単位

|ID|変更箇所（新設は明示）|実装する関数・責務|完成条件|
|---|---|---|---|
|W01|新`adapters/sdc/release_auto_contract.py`＋contracts JSON|parse_typed/canonical_typed/hash_v2、SEM01–34、既存v1dispatch保持|全構造/意味vector、v1未知schema拒否、署名purpose分離|
|W02|`mcs/ops/sdc_support_signatures.py`|新v2 purpose wrapper、独立pins/expected、historical検査分離|旧用途の受入集合不変。historicalはgrant発行不可|
|W03|新`adapters/sdc/release_auto_state.py`＋`storage.py`|OwnerState load/validate/CAS、capacity、intent/spent/terminal/outbox|fault injection、連続2更新、ACK遅延でexecution_revision不変|
|W04|新`mcs/ops/release_auto_bridge.py`、既存recovery一式|ControlManifest/MigrationIntent、初期baseline、既存v1から明示移行|pending/unknown拒否、before/after crash、旧入口閉鎖、`.prev`世代維持|
|W05|新`mcs/core/runtime_entry.py`＋各launcher|Q寿命gate、prepared fence、世代/boot、quarantine admission|import途中の切替禁止、旧process拒否、全入口coverage|
|W06|`update_services.py`、service_management、standalone、Hermes/LINE WORKS入口|profile別drain/退役/起動観測、独立owner起動|R外drain、強制kill非流用、自己deadlockなし|
|W07|新`adapters/sdc/release_auto_transport.py`|固定JSON v2 path＋別artifact stream|16KiB既存上限維持、256MiB stream/hash/timeout/no-redirect|
|W08|新`mcs/ops/release_auto_owner.py`＋既存owner private dispatch|stage→prepare→grant→consume→effect→proof、clock/nonce/budgets|正常経路が最後まで前進、未知時は証拠保持|
|W09|`mcs/ops/update_apply.py`、既存repository/journal primitives|private `apply_code_only_v2(capability)`、既存v1bailとは異なるforward-onlypolicy|post-merge失敗でreset/DBrestoreゼロ、v1回帰維持|
|W10|新`mcs/ops/release_runtime_proof.py`、独立recovery generation|quarantine/activation/proof/観測的終結|HEAD/PIDだけ不可、boot/loadedsource/heartbeatの束縛|
|W11|新`scripts/development/release_publish.py`、`release-publish.yml`|01のidentify/package/authorize/publish/verify-register、正式受入票|publisher fork拒否、samebytesretry、公開後証拠、OIDC/ID検証|
|W12|`ci.yml`、`release-notes.yml`、既存release generators|request/evidence出力、日本語ノート維持、draft責任移管、event tag限定sync|新旧workflow競合なし、全suite強制方針変更なし|
|W13|既存SDC backend（実repoを確認して実装）|03のtransaction/table/routes、CAS/cohort/grant/result|clientと共通vector、tenant分離、署名key分離、rate budgets|
|W14|`update_cli.py`、scheduler、status/doctor表示|顧客v2ルーティング、legacy auto抑止、固定状態/理由、outboxretry|autoモードだけで同意せず、unknownを正常にしない|
|W15|tests、docs、changes記録|05の受入、既存回帰、現行受入票|実測/再利用/未実行を明示、外部操作未実施|

新helperは実装が既存責務へ自然に収まる場合統合してよい。ただし別状態正本・別Updater・別認可体系を作らない。pathの変更は本書対応表とtest importを更新し、ファイル名だけ違う重複実装を残さない。
interfaces.pyiは関数境界の仕様であり、...のまま製品へ入れて完了としない。DTOはschemaからclosedな型へ落とし、実装内部で任意dictが権限を持つ状態を避ける。

## 4. 中核アルゴリズムの手順

### 4.1 同期/非同期境界

定期ジョブの役割は`request_update_tick()`まで。独立した既存サービス管理枠から、同じmcs.updaterの固定launcherを起動する。
現在の親hostをstopすると自分も死ぬprocess treeでは実行しない。ownerへ業務processのQ共有FDを継承しない。任意CLI引数`--is-owner`だけでgateを免除しない。

```text
tick:
  pending_outcomesを固定bytesで送達（failureでも業務を止めない）
  owner/consentをread（不存在・未適格ならstatusのみ）
  activeがある:
      START前なら同operationを限定的に準備/照会
      START後なら観測的復旧だけ
  activeがない:
      signed offer取得 → eligibility → stage
      window/予算成立時だけprepare
      preparedからgrant → final consume → code-only effect
      quarantine → activation → verified atomic completion
```

同一process内の多重tickは既存owner mutex/lockで一つにする。systemcron/standalonejob/手動CLIで別のconsumerを増やさない。

### 4.2 State保存

```text
mutate_state(expected_revision, expected_execution_revision?, reducer):
  O取得
  stateのraw/private/schema/意味/参照を検査
  revisionをCAS。executionに触るならexecution_revisionもCAS
  reducerはpure。外部call/file削除/process起動をしない
  変更fieldでrevision増分、必要時execution_revision増分
  意味検証・サイズ・予約容量検証
  atomic durable publish
  O解放
```

SPENTとterminal/outboxの保存失敗は別扱い。fsync結果不明は`PersistenceUnknown`として通常Exception retryへ落とさない。保存を読めたことだけをpower-loss耐久性の証明にはしない。
`activate`の同意撤回との競合はlinearizationを定める。最初のeffect前のローカル撤回は止める。effect後の撤回は次回を禁止し、現在の整合性確定を壊さない。

### 4.3 ControlManifestと移行

ControlManifestは最小権限制御のファイル一覧で、entrypoint/直接・遅延importの必要closureとrecovery generationを固定する。manifest自身はfilesに含めない。
`deployment/sdc-release-owner.json`の全アプリ完全性manifestとは別用途とし、両方を検査する。control closureのcodeはcheckout外の既存recovery管理領域へ世代別に配備し、本体切替中にcandidate helperを読み直さない。

初回baseline設定は実際のsource/lock/runtime/schemaと署名coreを照合し、外部history floorが未導入と認める導入だけを新規登録する。ローカル記録がないだけでは足りない。v1 spent/unknownがあればbridgeを止め、別の明示解決へ。
MigrationIntentのfilesは全sidecarのbefore hash/size/dispositionを含み、未列挙名・future schemaを拒否する。privateなarchiveの元bytesを保全し、hash不一致で継続しない。
recoveryは4supportの現行世代を単位として導入。loader、hash manifest、runtime directory、entrypoint、.prevの対応をcrash injectionする。本体と同じ可変venvしか使えない状態ではenableしない。

### 4.4 Publisher CLI（新設する公開処理の内部CLI）

|command|入力file|出力file|外部変更|
|---|---|---|---|
|`inspect-event --event … --policy …`|GitHub event/固定policy|build-request.json|GETのみ|
|`check-acceptance --request … --acceptance …`|同SHA受入票/原証拠|acceptance-check.json|GETのみ|
|`package --request … --output …`|isolated Git/source|asset-list.json＋固定assets|ローカルだけ|
|`reserve --request … --assets …`|OIDC/公開予約|publish-plan.json|中央予約。承認済みworkflowだけ|
|`publish --plan …`|署名PublishPlan＋固定objects|publish-observation.json|GitHub draft/upload/publish|
|`verify-register --plan … --observation …`|exact Release/assets|publication-record.json|中央登録のみ|

CLIはユーザーの任意URLを受けない。引数はpathのみで秘密はstdoutへ出さない。外部変更commandはこの仕様を渡しただけで実行しない。CIが有効化された後の固定workflowだけが機械的に使用する。

## 5. 実装順序と並列化

P0→W01/W02/W03のRED→W04/W05/W06→W08/W09/W10の合成統合を先に完成させる。
W07とW11/W12、backend確認済みならW13は別ownerで並列化できる。共有contract/schemaは一人が統合し、並列で意味を変えない。
W14で起動を接続するのは既存v1回帰・正常2連続更新・fault injectionが通ってから。既定値はOFFのまま。
公開/bridge/自社実機pilotを行う直前に、別承認と環境パラメータを確認する。未実装backendをmockへ向けたまま完成としない。

## 6. 環境パラメータ（未確定値はnull/OFF）

付属`deployment-parameters.example.json`は設定作成のチェックリストであり、そのまま本番へ適用しない。
repo ID/owner ID/source SHAは観測値として入れる。中央URL、backend repo、鍵fingerprint、publisher closure hash、toolchain pin、各profile、保守窓、人同意、ruleset/environment設定証拠は実測/承認後に設定する。
必須値がnullなら`AUTO_ELIGIBLE=false`。テスト用鍵やexample.invalidを本番値の代替にしない。backend実装が別repoの場合、契約版とconsumer testsを両repoの受入票に記録する。

## 7. 検証と完了報告

既存`AGENTS.md`の隔離runnerを使う。実際のコマンドは実装HEADで確認し、ファイル移動に追随する。

```bash
scripts/run_tests.sh tests/adapters/sdc/ tests/ops/ tests/meta/ tests/release/
scripts/run_tests.sh   # 必要な範囲の総合確認。再利用する場合は受入票に根拠を記録
make lint
python3 ci/gates.py
python3 ci/mine_gates.py --check
python3 scripts/development/update_readme.py --check
```

全6CI再実行を一律に要求しない。実行件数、skip、元の失敗、修正後再検証、再利用元、対象SHA、環境、未実行、承認を区別した受入票を作る。製品失敗未修正や安全境界未確認を免除しない。
macOS native flock、launchd、PID/birth、Keychain/ログイン、FileVault/スリープ復帰、電源断durabilityはLinuxのstub試験で代替しない。実機は別承認の自社検証機だけで開始する。

完了報告の必須項目: 変更path/責務、INV/SEM/AT対応、実行した試験と件数、未実行/再利用、外部設定待ち、各profile資格、known risk、外部操作を実施した範囲（なければ未実施）。

END-IMPLEMENTATION-V2.0

<!-- END 04_IMPLEMENTATION_PLAN.md -->

<!-- BEGIN 05_ACCEPTANCE_TESTS.md -->

# 05 — 製品受入試験の正本

全項目は**製品実装時に実行すべき試験**。この成果物作成時点の製品実行結果ではない。
`acceptance-tests.json`を機械可読正本とし、各assertionをpytestの具体的なassertへ落とす。
既存64項目と追加レビューの15項目の論点を統合し、GitHub公式仕様から追加した境界も含めた。

## 試験層

L1: pure validator/policy、L2: 偽APIと実ローカルGit/SQLite/私有file、L3: 複数process/fault injection、L4: 別承認のmacOS検証機。
製品testsは既存isolated runnerを使用。実患者data/実投稿の匿名化/実Keychain/実MCS/実課金APIはfixtureに使用しない。
GitHubも原則fake response/event。公開APIを実際にwriteする受入は別承認の検証repositoryだけ。
各否定試験の直近に正常caseを置く。全てHOLDにすることで試験を通してはいけない。

## 必須の実行スケジュール例

競合試験はsleep依存だけにせず、barrier/event/pipeで順序を固定する。
例AT049: workerがR解放を通知→ownerがprepare受付停止→workerのtransport完了→workerがRを取得し結果/予算を確定→process退役→ownerがQ/RでPREPAREDを採用。
例AT075: terminal/outboxを同じ保存に構築し、write/flush/fsync/replace/directory fsync/再読取りの各点で故障を注入する。
例AT076: Bの結果受領は中央でcommit、ACKだけ喪失→Cを選択→B exact raw再報告→C grant/適用→旧B grant再送を拒否。

## 試験一覧

|ID|分類|scenario|必須assertion|不変条件|
|---|---|---|---|

|AT001|PUB|tagのみ、Release未公開|START/SPENT/effect=0。現在の正常版を継続。|INV01,INV02,INV15|
|AT002|PUB|draft/prerelease表示だけ変更|登録済み正式candidate以外を開始しない。mutable表示から認可しない。|INV01,INV02|
|AT003|PUB|workflow_run側SHAがmainの別commit|upstream run head_shaとpeeled tagを採用し、publisher SHAをtargetにしない。|INV01|
|AT004|PUB|annotated tag多段/循環/別OID|8段まで正確にpeel。上限/循環/変更は拒否。正常lightweightも通る。|INV01,INV14|
|AT005|PUB|main非祖先とmain先行進行|非祖先拒否。同一snapshot祖先でmainが先行する正常候補は受入。|INV01|
|AT006|PUB|fork/PR起因の同名ci workflow|repo/workflow/event/IDを検証し、署名・write権限を渡さない。|INV02|
|AT007|PUB|candidate helper/cache/workspace汚染|privileged jobは固定closureだけを実行。候補artifactはデータとして扱う。|INV01,INV18|
|AT008|PUB|GITHUB_TOKEN公開後release event不発|verify-registerは明示jobとして動き、イベント再帰に依存しない。|INV19|
|AT009|PUB|OIDC正署名だが別repo/env/workflow/audience|全claimsとclosureを照合。job_workflow_ref不存在の通常jobを誤って必須化しない。|INV02,INV18|
|AT010|PUB|workflow差分によるcreate404/403|権限HOLD。盲目createループやPAT/Workflows権限自動拡張なし。|INV02,INV19|
|AT011|PUB|composed受入：実測再利用＋失敗再検証|正式policyと影響証拠で公開可能。元GitHub job failureをsuccessへ書換えない。|INV16,INV20|
|AT012|PUB|未修正製品failureをinfraと偽装|受入票拒否。必要lane重複/欠落/未知skipを通さない。|INV16,INV20|
|AT013|PUB|同名artifact別run/attemptと2page目のjob|全page/attempt/ID結合。自己申告attemptだけでは採用しない。|INV01,INV16|
|AT014|PUB|Actions artifact期限切れ|中央に固定保存済み原証拠があれば使える。原証拠不明なら拒否。|INV01,INV16|
|AT015|PUB|concurrency pending置換/queue上限|durable registryから未完requestを識別。queueを完了台帳にしない。|INV19|
|AT016|PUB|高番号公開後に低番号build完了|配信current/epoch非後退、既実行operation不変。|INV05,INV19|
|AT017|PUB|同tagで異bytesを再build|初回CASの固定bytes再利用だけ。異bytes/異tagOIDはconflict。|INV01,INV05|
|AT018|PUB|24時間後にattestation更新|release_id/core/artifact固定。新manifestで古いplan/grantを流用しない。|INV01,INV06|
|AT019|PUB|公開後GitHub release証拠生成|公開後asset追加をしない。中央にattestation/検証証拠を保持。|INV01,INV19|
|AT020|PUB|publish応答timeout、実際は公開済み|同Release IDを照会・照合。再公開/同version置換なし。|INV01,INV19|
|AT021|PUB|upload502→starter asset残存|state/size/hashを確認しHOLD。無承認削除と無限uploadなし。|INV01,INV21|
|AT022|PUB|filename正規化とduplicate422|予定ASCII名と集合を完全照合。不一致は上書きせず拒否。|INV01|
|AT023|PUB|asset metadata digest一致、download bytes相違|実bytes hashと公式verify-assetの両方で拒否。|INV01|
|AT024|PUB|自動生成source ZIPを配布物と指定|明示bundle asset以外拒否。source archiveをverify済みと主張しない。|INV01|
|AT025|PUB|API404/不完全pagination/immutable false|不存在・未取得・公開未検証を区別、登録しない。|INV01,INV16|
|AT026|PUB|release eventで1版のノートだけ修正|そのtagだけsync。manual無指定の既存一括動作は別に回帰確認。|INV19|
|AT027|PUB|検証後タグ移動・別repository同名復活|rawOID/repo数値IDの最終再照合で拒否。|INV01,INV05|
|AT028|PUB|environmentがprotected onlyだがbranch保護なし|enablement gateが拒否。Selected mainと実effective保護を確認。|INV02|
|AT029|ART|source manifestあり依存lockなし|LOCK_MISSING拒否。タグ前に実lockが揃う正常系は検査成功。|INV01,INV17|
|AT030|ART|通常app変更とprivileged closure変更|前者は署名/互換性成立で更新可、後者はbridge要求。|INV17,INV18|
|AT031|ART|targetは無害だが過去packに秘密canary|全packの公開検査で拒否。無関係branchを含めない。|INV12,INV14|
|AT032|ART|symlink/submodule/.gitattributes/case/NFC衝突|既存tree検査を保持。git cleanで消さない。|INV01,INV10|
|AT033|ART|圧縮爆弾/深いdelta/prerequisite/切断|上限/deadlineで停止。不足objectをネットワーク補完しない。|INV14|
|AT034|ART|期限切れmetadataと304/cache|新START不可、現在業務は継続。期限を受信時刻で延長しない。|INV06,INV15|
|AT035|ART|鍵ローテーションと未pin鍵|旧/新threshold chainは通り、自己提示の新keyは拒否。|INV02,INV18|
|AT036|CON|重複/escape重複/NaN/float/-0/bool-as-int|strict parse拒否。unknown schema/propertyを補正しない。|INV13|
|AT037|CON|v1署名をv2 purposeへ再解釈|domain/purpose/key用途不一致で拒否。v1正常fixtureは維持。|INV07,INV18|
|AT038|CON|signed payload 8KiB、envelope16KiB境界|境界以内正常、超過拒否。catalogは該当profile一件へ分離。|INV14|
|AT039|CON|stat ns=1.7e18、inode64bit|private decimalstringでlossless照合。JSsafe intへ丸めない。|INV13,INV16|
|AT040|CON|別tenant/device/realm/profile正署名|保存scope/credential由来expectedと不一致でSTARTなし。|INV02|
|AT041|CON|backup/config/pathをwireへ混入|closedDTOとallowlistで拒否。公開artifact hashは正常に送る。|INV12|
|AT042|NET|JSON transportへ巨大bundle|専用streamのみ。既存16KiB/10秒制約不変。|INV14|
|AT043|NET|redirect/proxy/TLS不正|端末拒否。中央の限定redirectでも別hostへ認証headerを転送しない。|INV12,INV14|
|AT044|NET|stream size超過/Content-Encoding変更/総deadline|最初の違反で中止、stageは非採用、現行業務継続。|INV14,INV15|
|AT045|NET|429/Retry-After・GET障害|有限backoff、stateとrequest identity保持。通信失敗を候補なしにしない。|INV15,INV16|
|AT046|DEV|update.mode=autoのみ、standing consentなし|prepareさえ開始しない。旧modeから同意を昇格させない。|INV02,INV07|
|AT047|DEV|同意撤回とgrantの競合|effect前に観測した撤回で停止、SPENTは残す。effect後は整合確定のみ。|INV02,INV04|
|AT048|DEV|保守窓跨ぎ/夏時間/clock後退|明示timezone、window境界、monotonic照合。過去lease再生なし。|INV02,INV14|
|AT049|DEV|worker通信中にRを解放|drain中Rを保持せず結果/費用を既存規則で確定。未drainで切替しない。|INV08,INV11|
|AT050|DEV|lockless bookkeeping/第3worker/LINE WORKS残存|profile別必要集合で未退役を検出、Q EX取れず延期。|INV08|
|AT051|DEV|長時間inflight、prepare期限超過|新STARTなし。元の同じserviceのみ再開、別hold維持。|INV08,INV15|
|AT052|DEV|operation/release変更で準備予算回避|rolling24h3回/360秒をinstallation単位で維持。|INV14,INV15|
|AT053|DEV|PREPARED後ロック解放中に別Updater|耐久fence/nonceで二重開始不可。正常grant取得は全lock外。|INV03,INV08|
|AT054|DEV|grant待ち中config/credential/source/inode変更|final witness/CASで拒否。設定を元へ上書きしない。|INV02,INV10|
|AT055|DEV|Q EX保持中に新processのQ SH ready待ち|検出・修正。APPLIED後Q/R等解放して起動、deadlockなし。|INV03,INV08|
|AT056|DEV|A/M/G/U/Q/R/C/O競合とcallback再入|DAG逆順/再入拒否、有限deadline、同意取消を失わない。|INV03,INV14|
|AT057|DEV|grant timeout再送|同request同bytesのみ。別ID/自動期限延長禁止。|INV04,INV06|
|AT058|DEV|未STARTの正当なprestart abortとACK喪失|ローカル耐久終結→中央CAS。旧nonce grant拒否、次prepare可。|INV04,INV06|
|AT059|DEV|START intent不明なのにprestart abort|拒否。timeout/旧HEADだけでは未実行にしない。|INV04,INV06|
|AT060|DEV|start-intentの各write/fsync点でkill|beforeを証明できなければUNKNOWN、capabilityなし。|INV04,INV06|
|AT061|DEV|SPENT rename後directory fsync失敗|RAMcapなし。再起動で再実行せず全証拠保全。|INV04,INV05|
|AT062|DEV|copy/fork/保存JSON/CLIでcapability偽造|同processlive factory以外拒否。二重claimは一勝者。|INV04,INV06|
|AT063|DEV|別スレッドが同capabilityを同時claim|exactly一回effect、FD同一性/状態不変。|INV03,INV04|
|AT064|DEV|初回effect直前にgrant期限切れ|effect0、SPENT保全。同生存ownerだけno-effect終結可能。|INV04,INV06|
|AT065|DEV|開始後にgrant期限切れ|同じ有限操作の整合性確定は可能、新操作/再試行不可。|INV04,INV06|
|AT066|DEV|Git merge/post-merge失敗|v2はcode reset/DB置換なし。v1人承認rollback回帰を維持。|INV07,INV10|
|AT067|DEV|Git child停止結果不明|子の退役を証明せずfence解放/成功化しない。|INV03,INV08|
|AT068|DEV|アプリimport中のcode切替要求|Q SHの寿命中はcode切替不可。candidateimport前bootstrap固定。|INV08,INV18|
|AT069|DEV|同mtime/sizeの古いpyc|世代cacheとloadedsource検証で旧bytecode不採用。-B単独に頼らない。|INV08,INV09|
|AT070|DEV|HEAD新版、旧PID・古いheartbeat・PID再利用|runtime proof拒否。実loaded identity/newgenerationを要求。|INV09|
|AT071|DEV|quarantine constructorが通信/DB移行|profile資格拒否。実APIや臨床writeでヘルスチェックしない。|INV10,INV11|
|AT072|DEV|timer/disabled serviceの正常起動|desired状態のproofを受入。非residentへ常時PIDを要求しない。|INV09,INV15|
|AT073|DEV|新processが自己申告だけtarget|native birth/起動nonce/importrootとの照合で拒否。|INV09,INV18|
|AT074|DEV|activation前後で同意・別hold変更|自op以外のhold保持。activation証拠なしに業務開始不可。|INV02,INV11|
|AT075|DEV|VERIFIED保存とoutbox保存の間でkill|一つのatomic owner保存。部分成功ならUNKNOWN/観測確定。|INV04,INV09|
|AT076|DEV|A→B→C、B ACKだけ喪失|2操作が個別VERIFIED、B raw再送可、旧B grant再実行拒否。|INV05,INV06,INV19|
|AT077|DEV|遅れたACKと新grant witness競合|ACKはrevisionだけ更新。execution_revision不変で正規grantを誤拒否しない。|INV03,INV19|
|AT078|DEV|unknown→実際に既完了の観測|historical署名/同op/activation/proofで終結可、追加effect0。|INV06,INV09|
|AT079|DEV|unknownでservice未起動|観測だけでは成功不可。旧grantでstartせず別承認保守。|INV06,INV09|
|AT080|DEV|別op ACK/同report異bytes/sequence後退|拒否。正しい重複ACKだけ冪等。|INV05,INV19|
|AT081|DEV|owner容量は残るが完了予約量不足|START前にHOLD。履歴削除や上限引上げなし。|INV05,INV14|
|AT082|DEV|download/pack/backupが別filesystemの容量不足|各mountの同時ピーク検査。患者data掃除なし。|INV10,INV14|
|AT083|DEV|完了記録disk full/atomic replace不明|成功断定なし、同op証拠保持、再applyなし。|INV04,INV16|
|AT084|DEV|backup後新着/送信receiptが増加|自動DB復元なし。attempt/budget/notification履歴不変。|INV10,INV11|
|AT085|DEV|patch版だがDB/SDK/model/config/default権限変更|実diff/profile検査でcode-only拒否。版番号で許可しない。|INV11,INV17|
|AT086|MIG|v1 spent/unknown/pending successorあり|移行しない。旧bytes削除・成功化なし。|INV05,INV07|
|AT087|MIG|migration intent/oldarchive/replace各点でcrash|同before/afterだけ照合。legacy入口が開かない。|INV03,INV07|
|AT088|MIG|recovery entry/support混成、.prev起動|混成拒否、旧版は自世代support利用。|INV07,INV18|
|AT089|MIG|全disk復元・他deviceへcredential/ownerコピー|中央floor/realm/root不一致でHOLD。fresh扱いなし。|INV05,INV07|
|AT090|MIG|旧別checkout/非協調manual writer|AUTO_ELIGIBLE拒否。Qだけで全process検証済みとしない。|INV08,INV18|
|AT091|OPS|FileVault/未ログイン/Keychain locked/再起動|未稼働を正常扱いしない。root化/自動ログインで回避しない。|INV15,INV16|
|AT092|OPS|self/pilot成功未観測・offline|未STARTと開始後不明を区別。後者でbatch拡大停止。|INV09,INV19|
|AT093|OPS|途中cohort増減・新Release出現|固定assignment/target、二重STARTなし。|INV05,INV19|
|AT094|OPS|同commitを別tag/再圧縮して再配布|attempt_keyが一致し消費済み枠復活なし。|INV05,INV06|
|AT095|OPS|server停止・報告未送信|正常アプリ継続。新grantは停止、結果は同bytesoutbox保持。|INV15,INV19|
|AT096|OPS|fixture鍵/mock backend/空profileの有効化|本番gate拒否。実装済みと配備済みを分ける。|INV16,INV20|
|AT097|OPS|全失敗ケースHOLD、正常系も進まない実装|正常end-to-endと2連続更新を必須にし、全HOLDを合格にしない。|INV09,INV20|
|AT098|OPS|旧人承認restore・課金unknownに更新を適用|別ゲート/不確定履歴の意味不変。更新同意で解除しない。|INV07,INV11|

## 完成条件

採用する実装HEADについて、実行件数/環境/skip/失敗/再検証/再利用/未実行を受入票へ保存する。
macos_native_required=trueは少なくともそのOS境界をL4で追加確認する指標であり、falseならOS試験が不要という意味ではない。
抽象モデル/このパッケージのschema自己検査で、製品試験のexecution_statusをPASSへ書き換えない。
未実装backend/profileはOFFのまま記録。runtimeごとのE2E適格性を一つの成功から推測しない。

END-ACCEPTANCE-V2.0

<!-- END 05_ACCEPTANCE_TESTS.md -->

<!-- BEGIN 06_SOURCES_AND_REVIEW.md -->

# 06 — 公式資料、コード根拠、敵対的レビューの決定記録

確認日: **2026-11-10 JST**。以下は文書作成時の読取り根拠。外部仕様・repository設定は有効化直前にも再確認する。
GitHub公式の保証と、この製品が独自に定めた同意・予算・状態契約を区別する。GitHubがhermes-mcsの患者安全や臨床ロジックを認証したとは扱わない。

## 1. GitHub公式資料

|ID|公式URL|確認した事項／適用箇所|
|---|---|---|
|GH01|`https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases`|tag/assets不変、公開でrelease attestation生成、draft→添付→publish。01公開順序|
|GH02|`https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes` と `https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository`|既存版への非遡及、title/body等の管理とasset不変性の区別|
|GH03|`https://docs.github.com/en/rest/releases/releases`|tagとtarget_commitish、create/update、immutable、workflow変更時の追加権限と404/403、latestの意味|
|GH04|`https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows`|workflow_runのdefault branch SHA、completedとsuccessの違い、chain制約、privileged後続run|
|GH05|`https://docs.github.com/en/actions/reference/security/secure-use`|untrusted checkout/cache/入力実行、最小権限、action SHA pin|
|GH06|`https://docs.github.com/en/actions/concepts/security/github_token`|GITHUB_TOKENの権限とイベント再帰抑止。例外があるため「全イベント不発」と一般化しない|
|GH07|`https://docs.github.com/en/actions/how-tos/workflows-and-actions/choose-when-workflows-run/control-workflow-concurrency`|queue single/max、最大100、cancel-in-progressとの制約、dispatch順序非保証|
|GH08|`https://docs.github.com/en/rest/releases/assets`|binary200/302、upload_url、生bytes、502 starter、name競合・正規化|
|GH09|`https://docs.github.com/en/rest/actions/workflow-runs` / `https://docs.github.com/en/rest/actions/workflow-jobs` / `https://docs.github.com/en/rest/actions/artifacts`|run/attempt/jobs/artifacts ID、ページング、artifact寿命を区別|
|GH10|`https://docs.github.com/en/actions/concepts/security/artifact-attestations`|provenanceの生成だけでなく検証が必要。安全性・テスト成功とは別|
|GH11|`https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments`|environmentのbranch制限、保護が無い場合のProtected branches onlyの落とし穴|
|GH12|`https://docs.github.com/en/actions/reference/security/oidc`|iss/aud/sub、repo/owner IDs、workflow/ref/run/attempt/environment。job_workflow_refはreusable時|
|GH13|`https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api`|rate limit/backoff、conditional request、必要なpage、取得失敗の扱い|
|GH14|`https://docs.github.com/en/rest/about-the-rest-api/api-versions`|新実装は2026-03-10をpin。既存clientの暗黙挙動を一括変更しない|
|GH15|`https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/verify-release-integrity`|gh release verify/verify-asset。自動生成source zip/tarballを対象にしない|
|GH16|`https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets`|ref creation/update/delete、必要チェックとbypassの管理|

公式CLIのverifyコマンドが表示する自由文を独自正規表現だけで権限判定しない。固定toolchainの終了結果とGitHubのexact ID/commit/asset bytesを照合する。署名原証拠の取得・保持には、その固定CLIがサポートする公式機能を使い、存在しないflagを推測しない。対応不能なら公開検証gateを閉じる。

## 2. リポジトリ根拠

全codeは`e85763b5addaabee516f6f69b8ea7465f26fe9d8`に固定。GitHub connectorで実読取り。必要範囲の読取りであり全製品逐行監査ではない。
固定URLは`https://github.com/yusuketakuma/hermes-mcs/blob/e85763b5addaabee516f6f69b8ea7465f26fe9d8/`に下記pathを続ける。

|ID|path/API|今回設計へ反映した事実|
|---|---|---|
|SRC00|branches/main API|同じSHA、protected=false。設定を保護済みと仮定しない|
|SRC01|AGENTS.md / docs/development/RELEASE_NOTES.md|2026-10-31の失敗項目再検証・既存証拠再利用。合成試験/外部操作権限|
|SRC02|mcs/ops/mcs_update.py / update_apply.py|facade分割、borrowed実適用接続、共通bailoutはrollbackを持つ|
|SRC03|mcs/ops/release_apply_owner.py|TTY、live factory、G/U/R下の通信、successor pending、readiness unknown|
|SRC04|adapters/sdc/release_state.py / release_apply_contract.py|v1 unknownとself/individual制限、SPENT/署名契約|
|SRC05|mcs/ops/update_cli.py / mcs/ops/update/update_discovery.py|git tag最大値→delay→auto。公開済み判定とは別|
|SRC06|mcs/ops/release_owner_source.py / adapters/sdc/release_artifact.py|広い完全性manifest、全pack検査、target内の実依存lock必須|
|SRC07|deployment/一覧 / sdc-release-lock.schema.json / sdc-release-owner.json|実依存lockは基準treeにない。source manifestで代替不可|
|SRC08|deployment/recovery/mcs_recover.pyとSUPPORT_FILES|hash固定4support、generation directory、旧.prev。配備単位は一式|
|SRC09|.github/workflows/ci.yml / release-notes.yml|6lane、draft作成、main/tag source混在、日本語ノート、scope限定syncの必要|
|SRC10|adapters/sdc/transport.py（1–220）|16KiB JSON/10秒、同一origin、no-proxy/no-redirect、固定認証headers|
|SRC11|mcs/core/writer_lock.py|通信中R解放、再取得、lockless bookkeeping、mtime stamp|
|SRC12|adapters/sdc/storage.py（590–810）|A→M/O、G/U/Rの別寿命、nonblocking flock、private file境界|
|SRC13|mcs/ops/install_options.py（1–175）|config_edit._edit_lock、raw snapshot/CAS。config_editの物理pathはP0で解決|
|SRC14|mcs_standalone/service.py（1–140）/ update_services.py|status TTL/host generation、restart intent、3worker、quiesce/起動|
|SRC15|mcs/ops/release_apply_checks.py|private backup/preflight、artifact/Git検査、child ticket、control root|
|SRC16|tests/ops/test_release_apply_owner.py|既存capability/fork/FD/ticket試験。今回製品としては未実行|

GitHub searchで0件・incomplete_results=trueになった結果を不存在の証拠にしていない。直接pathの404は「そのpathにない」という意味だけで、移動先を未読のまま断定しない。
旧レビューのAPI観測: repoはpublic、rulesets空、v1.1.2 immutable=false。現在mainの実コードと、本番端末に導入された版は同一とは限らない。
SDC中央backend、鍵、実顧客端末、実DB・設定・ログ、実機launchdは未確認。新APIや運用設定を配備済みと報告しない。

## 3. 敵対的レビューの反例と決定

|ID|反例・対立する要求|本書の決定|試験|
|---|---|---|---|
|AR01|workflow_runがmainを指すのにtargetへ流用|publisher/source/upstreamの三つの同一性を別binding|AT003–005|
|AR02|queueで配信順序と耐久性を保証したつもり|queueは補助、中央seq/CAS/leaseが正本|AT015–017|
|AR03|公開後attestationを公開前assetに入れる循環|不変assetと公開後の検証証拠を分離|AT019–020|
|AR04|不変Releaseだけで悪いbuildを防げると解釈|受入票・provenance・端末grantは別|AT011–012,023|
|AR05|CI失敗→全部停止か、無条件に無視かの二択|現行composed受入を機械化。未解消製品失敗は拒否|AT011–014|
|AR06|write権限付きjobがcandidate helperを実行|trusted helper固定、候補はデータ|AT006–009|
|AR07|workflow差分による404を不存在と誤認|権限HOLD、追加権限は別承認|AT010|
|AR08|artifact upload timeoutで同名を強制削除|同一bytes照合、starterは明示HOLD/保守|AT020–022|
|AR09|SDC JSON上限をbundle用に広げる|固定制御面とbounded streamを分離|AT042–045|
|AR10|鍵・source・backupの信頼を一つのhashへ集約|用途別hash/opaque private evidence、独立expected|AT035–041|
|AR11|SPENTはあるが成功終結がなく一回で詰まる|明示v2 owner terminal、SPENTを保持して次更新|AT075–080|
|AR12|遅いACKでwhole-state revisionが変わりgrantが毎回失敗|execution_revision分離、最新ownerへACKだけmerge|AT077|
|AR13|R取得を全worker停止とみなす|admission fence→R外drain→Q EX→witness|AT049–056|
|AR14|新Q EXを持ったまま新process ready待ち|APPLIED後Q解放→new Q SH起動|AT055|
|AR15|正常grantでも起動元hostを止め自分が死ぬ|固定独立起動枠、業務Q FDをownerへ継承しない|AT055–056,068|
|AR16|startedか不明なので別IDでretry|intent/SPENT/live cap一回性、UNKNOWNは観測のみ|AT057–065|
|AR17|正当な未STARTでも永久にgrant予約が残る|耐久prestart abortと中央CAS。expiry単独は証拠にしない|AT058–059|
|AR18|forward-onlyを選んだのにv1 bailoutがrollback|正常primitiveのみ共有しv2failure policyを分離|AT066,084|
|AR19|HEAD/PID正常だが旧bytecode/旧process|Q寿命、世代cache、native/startup/source proof|AT068–074|
|AR20|全source manifestを凍結して全修正が保守扱い|完全性manifestと最小control closureを分離|AT030,085|
|AR21|復旧器一枚を更新しsupport世代が混ざる|4support＋entry＋hash＋.prevを一式検証|AT086–088|
|AR22|disk rollbackでSPENT消失|中央floor/realm、失われた記録はfresh扱いしない|AT089–090|
|AR23|完了時容量不足で成功記録を書けない|最悪final/outbox容量をSTART前予約|AT081–083|
|AR24|型はintegerだからstat nsもsafeと思う|private64bit decimalstring、wireはms safe int|AT039|
|AR25|全caseがHOLDになれば安全|正常E2EとA→B→Cを必須にする|AT076,097|

## 4. 採用した代償・残余リスク

forward-onlyは一部の更新後障害で人対応が必要になる。全自動復旧を優先すると未知の送信/課金/DB状態を再実行する危険があるため、ここでは一回性を優先する。人対応の手順と担当者がない環境へ配備しない。
独立control closure、phase別drain、署名付きscopeは実装量を増やす。ただし二つ目の更新エンジンは作らず既存プリミティブを再利用する。control closure変更は通常配信対象外なので、その保守導線は残す。
中央障害で新更新が止まるが正常版は継続する。完全オフラインの新規更新許可は今回対象外。瞬時の遠隔失効、OS全巻戻しへのハードウェア同等保護、同一UID/root完全侵害まで解決済みとはしない。
有限owner容量で無限履歴保持はできない。本版は容量予約・事前警告・新START保留まで。将来の証拠checkpoint/移管は別設計であり、履歴間引きで回避しない。

設計上の方向は一本に確定したが、実装の正しさは製品の合成・統合・macOS試験で初めて確認する。パッケージの自己検査結果は`VALIDATION.md`に限定して記す。

END-SOURCES-V2.0

<!-- END 06_SOURCES_AND_REVIEW.md -->

<!-- BEGIN contracts/FIELD_CATALOG.md -->

# 機械契約のfield一覧

これは`contracts-v2.schema.json`から生成した一覧。全objectはadditionalProperties=false。各fieldは必須（null許可は明示）。

## TimeWindow

|field|型/条件|
|---|---|
|`starts_at`|`{"$ref":"#/$defs/Uint"}`|
|`ends_at`|`{"$ref":"#/$defs/Uint"}`|

## Scope

|field|型/条件|
|---|---|
|`product`|`{"const":"hermes-mcs"}`|
|`tenant`|`{"$ref":"#/$defs/Slug"}`|
|`installation`|`{"$ref":"#/$defs/Slug"}`|
|`device_id`|`{"$ref":"#/$defs/Uuid"}`|
|`realm_epoch`|`{"$ref":"#/$defs/Token"}`|
|`environment`|`{"$ref":"#/$defs/Slug"}`|
|`stream_id`|`{"$ref":"#/$defs/Token"}`|

## ProductScope

|field|型/条件|
|---|---|
|`product`|`{"const":"hermes-mcs"}`|
|`environment`|`{"$ref":"#/$defs/Slug"}`|
|`trust_domain`|`{"$ref":"#/$defs/Token"}`|
|`stream_id`|`{"$ref":"#/$defs/Token"}`|

## Profile

|field|型/条件|
|---|---|
|`profile_id`|`{"$ref":"#/$defs/Slug"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`runtime_mode`|`{"type":"string","enum":["hermes","standalone"]}`|
|`os`|`{"const":"macos"}`|
|`cpu`|`{"type":"string","enum":["arm64","x86_64"]}`|
|`python`|`{"type":"array","prefixItems":[{"const":3},{"type":"integer","minimum":9,"maximum":99}],"items":false,"minItems":2,"maxItems":2}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## ReleaseRef

|field|型/条件|
|---|---|
|`release_id`|`{"$ref":"#/$defs/Sha256"}`|
|`code_seq`|`{"$ref":"#/$defs/Positive"}`|
|`source_commit`|`{"$ref":"#/$defs/Commit"}`|
|`artifact_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`artifact_size`|`{"type":"integer","minimum":1,"maximum":268435456}`|

## SourceRef

|field|型/条件|
|---|---|
|`repository_id`|`{"$ref":"#/$defs/Positive"}`|
|`repository_owner_id`|`{"$ref":"#/$defs/Positive"}`|
|`tag`|`{"$ref":"#/$defs/Tag"}`|
|`tag_oid`|`{"$ref":"#/$defs/Commit"}`|
|`commit`|`{"$ref":"#/$defs/Commit"}`|
|`main_snapshot_sha`|`{"$ref":"#/$defs/Commit"}`|

## JobRef

|field|型/条件|
|---|---|
|`workflow_id`|`{"$ref":"#/$defs/Positive"}`|
|`run_id`|`{"$ref":"#/$defs/Positive"}`|
|`run_attempt`|`{"$ref":"#/$defs/Positive"}`|
|`job_id`|`{"$ref":"#/$defs/Positive"}`|
|`artifact_id`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|

## LaneEvidence

|field|型/条件|
|---|---|
|`lane`|`{"type":"string","enum":["lint-test","hygiene","standalone-sdk","incident-gates","macos","hermes-integration"]}`|
|`source_commit`|`{"$ref":"#/$defs/Commit"}`|
|`environment_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`suite_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`evidence_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`measured_result`|`{"type":"string","enum":["success","failure","infra_unavailable"]}`|
|`coverage`|`{"type":"string","enum":["executed","reused","local_equivalent"]}`|
|`reused_from_commit`|`{"anyOf":[{"$ref":"#/$defs/Commit"},{"type":"null"}]}`|
|`impact_review_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`resolution_evidence_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`job_ref`|`{"anyOf":[{"$ref":"#/$defs/JobRef"},{"type":"null"}]}`|
|`unexecuted_reason`|`{"anyOf":[{"$ref":"#/$defs/Token"},{"type":"null"}]}`|

## ReleaseAcceptance

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.release-acceptance.v2"}`|
|`acceptance_id`|`{"$ref":"#/$defs/Uuid"}`|
|`source`|`{"$ref":"#/$defs/SourceRef"}`|
|`mode`|`{"type":"string","enum":["full_run","composed"]}`|
|`policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`lanes`|`{"type":"array","items":{"$ref":"#/$defs/LaneEvidence"},"minItems":6,"maxItems":6}`|
|`release_checks_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`unresolved_product_failures`|`{"type":"array","items":{"$ref":"#/$defs/Token"},"minItems":0,"maxItems":0}`|
|`accepted`|`{"const":true}`|
|`accepted_at`|`{"$ref":"#/$defs/Uint"}`|
|`approved_actor`|`{"$ref":"#/$defs/Token"}`|

## BuildRequest

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.build-request.v2"}`|
|`request_id`|`{"$ref":"#/$defs/Sha256"}`|
|`source`|`{"$ref":"#/$defs/SourceRef"}`|
|`acceptance_id`|`{"$ref":"#/$defs/Uuid"}`|
|`acceptance_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`upstream_run_id`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|
|`upstream_run_attempt`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|
|`publisher_source_commit`|`{"$ref":"#/$defs/Commit"}`|
|`publisher_policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`created_at`|`{"$ref":"#/$defs/Uint"}`|

## AssetSpec

|field|型/条件|
|---|---|
|`name`|`{"type":"string","minLength":1,"maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`size`|`{"type":"integer","minimum":1,"maximum":268435456}`|
|`media_type`|`{"type":"string","enum":["application/octet-stream","application/json","image/png","image/svg+xml","text/markdown"]}`|

## PublishPlan

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.publish-plan.v2"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request`|`{"$ref":"#/$defs/BuildRequest"}`|
|`release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`profile_sha256s`|`{"type":"array","items":{"$ref":"#/$defs/Sha256"},"minItems":1,"maxItems":4}`|
|`assets`|`{"type":"array","items":{"$ref":"#/$defs/AssetSpec"},"minItems":5,"maxItems":32}`|
|`asset_set_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`github_release_id`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|
|`revision`|`{"$ref":"#/$defs/Positive"}`|
|`fencing_token`|`{"$ref":"#/$defs/Positive"}`|
|`phase`|`{"type":"string","enum":["reserved","packaged","draft_created","assets_complete","published_unverified","registered","conflict"]}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## PublishedAsset

|field|型/条件|
|---|---|
|`asset_id`|`{"$ref":"#/$defs/Positive"}`|
|`name`|`{"type":"string","minLength":1,"maxLength":128,"pattern":"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"}`|
|`size`|`{"$ref":"#/$defs/Positive"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|

## PublicationRecord

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.publication-record.v2"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`source`|`{"$ref":"#/$defs/SourceRef"}`|
|`release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`github_release_id`|`{"$ref":"#/$defs/Positive"}`|
|`assets`|`{"type":"array","items":{"$ref":"#/$defs/PublishedAsset"},"minItems":5,"maxItems":32}`|
|`github_release_proof_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`acceptance_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`asset_set_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`immutable`|`{"const":true}`|
|`registered_at`|`{"$ref":"#/$defs/Uint"}`|
|`last_github_observed_at`|`{"$ref":"#/$defs/Uint"}`|
|`suspended`|`{"type":"boolean"}`|

## CompatibilityBase

|field|型/条件|
|---|---|
|`base_release_id`|`{"$ref":"#/$defs/Sha256"}`|
|`base_commit`|`{"$ref":"#/$defs/Commit"}`|
|`base_schema`|`{"$ref":"#/$defs/Positive"}`|
|`base_profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`config_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`data_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## Compatibility

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.compatibility.v2"}`|
|`scope`|`{"$ref":"#/$defs/ProductScope"}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`profile`|`{"$ref":"#/$defs/Profile"}`|
|`bases`|`{"type":"array","items":{"$ref":"#/$defs/CompatibilityBase"},"minItems":1,"maxItems":4}`|
|`target_schema`|`{"$ref":"#/$defs/Positive"}`|
|`allowed_class`|`{"const":"code-only-compatible"}`|
|`config_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`data_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`classification_evidence_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`db_migration`|`{"const":false}`|
|`runtime_change`|`{"const":false}`|
|`configuration_change`|`{"const":false}`|
|`authorization_expansion`|`{"const":false}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|

## MaintenanceWindow

|field|型/条件|
|---|---|
|`weekday`|`{"type":"integer","minimum":0,"maximum":6}`|
|`start_minute`|`{"type":"integer","minimum":0,"maximum":1439}`|
|`end_minute`|`{"type":"integer","minimum":1,"maximum":1440}`|

## StandingConsent

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.standing-consent.v2"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile`|`{"$ref":"#/$defs/Profile"}`|
|`allowed_class`|`{"const":"code-only-compatible"}`|
|`timezone`|`{"type":"string","minLength":1,"maxLength":64,"pattern":"^[A-Za-z0-9_+/-]+$"}`|
|`windows`|`{"type":"array","items":{"$ref":"#/$defs/MaintenanceWindow"},"minItems":1,"maxItems":32}`|
|`max_prepare_seconds`|`{"type":"integer","minimum":1,"maximum":120}`|
|`max_prepare_count_24h`|`{"type":"integer","minimum":1,"maximum":3}`|
|`max_prepare_seconds_24h`|`{"type":"integer","minimum":1,"maximum":360}`|
|`local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`policy_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`central_policy_id`|`{"$ref":"#/$defs/Uuid"}`|
|`central_policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`confirmation_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`confirmed_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|
|`enabled`|`{"type":"boolean"}`|

## SelectionRequest

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.selection-request.v2"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile`|`{"$ref":"#/$defs/Profile"}`|
|`current_release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`verified_release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`policy_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|

## SelectedRelease

|field|型/条件|
|---|---|
|`release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`manifest_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`compatibility_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`publication_proof_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`acceptance_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## Offer

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.offer.v2"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`rollout_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`status`|`{"type":"string","enum":["candidate","none"]}`|
|`reason`|`{"type":"string","enum":["ELIGIBLE","NO_NEW_RELEASE","COHORT_WAIT","MAINTENANCE_REQUIRED","POLICY_HOLD","PROFILE_UNQUALIFIED"]}`|
|`selected`|`{"anyOf":[{"$ref":"#/$defs/SelectedRelease"},{"type":"null"}]}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`not_before`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## MetadataRequest

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.metadata-request.v2"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`release_id`|`{"$ref":"#/$defs/Sha256"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`kind`|`{"type":"string","enum":["attestation","compatibility"]}`|

## ReadArtifactRequest

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.read-artifact-request.v2"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`release_id`|`{"$ref":"#/$defs/Sha256"}`|
|`artifact_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`artifact_size`|`{"type":"integer","minimum":1,"maximum":268435456}`|

## PreparedPublic

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.prepared-public.v2"}`|
|`local_preflight_id`|`{"$ref":"#/$defs/Uuid"}`|
|`local_backup_evidence_id`|`{"$ref":"#/$defs/Uuid"}`|
|`current_release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`target_release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`config_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`data_contract_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`installed_schema`|`{"$ref":"#/$defs/Positive"}`|
|`prepare_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`execution_revision`|`{"$ref":"#/$defs/Positive"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`boot_id`|`{"$ref":"#/$defs/Sha256"}`|
|`resources_verified`|`{"const":true}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|

## StartRequest

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.start-request.v2"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`policy_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`rollout_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`manifest_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`compatibility_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`preflight`|`{"$ref":"#/$defs/PreparedPublic"}`|
|`preflight_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`window`|`{"$ref":"#/$defs/TimeWindow"}`|

## StartGrant

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.start-grant.v2"}`|
|`grant_id`|`{"$ref":"#/$defs/Uuid"}`|
|`start_request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`consent_id`|`{"$ref":"#/$defs/Uuid"}`|
|`local_consent_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`policy_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`rollout_epoch`|`{"$ref":"#/$defs/Positive"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`manifest_digest`|`{"$ref":"#/$defs/Sha256"}`|
|`compatibility_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`prepare_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`execution_revision`|`{"$ref":"#/$defs/Positive"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`boot_id`|`{"$ref":"#/$defs/Sha256"}`|
|`preflight_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`window`|`{"$ref":"#/$defs/TimeWindow"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`not_before`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## PrestartAbort

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.prestart-abort.v2"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`start_request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_id`|`{"anyOf":[{"$ref":"#/$defs/Uuid"},{"type":"null"}]}`|
|`prepare_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`execution_revision`|`{"$ref":"#/$defs/Positive"}`|
|`local_abort_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`reason`|`{"type":"string","enum":["EXPIRED_BEFORE_START","REVOKED_BEFORE_START","PREPARE_DEFERRED"]}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|

## Outcome

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.outcome.v2"}`|
|`report_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`report_sequence`|`{"$ref":"#/$defs/Positive"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_id`|`{"$ref":"#/$defs/Uuid"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`installed`|`{"anyOf":[{"$ref":"#/$defs/ReleaseRef"},{"type":"null"}]}`|
|`verified_installed`|`{"anyOf":[{"$ref":"#/$defs/ReleaseRef"},{"type":"null"}]}`|
|`state`|`{"type":"string","enum":["verified","unknown","cancelled_no_effect"]}`|
|`readiness`|`{"type":"string","enum":["ready","unknown","not_started"]}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`started_at`|`{"$ref":"#/$defs/Uint"}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|
|`verified_at`|`{"anyOf":[{"$ref":"#/$defs/Uint"},{"type":"null"}]}`|
|`local_proof_id`|`{"$ref":"#/$defs/Uuid"}`|
|`error_code`|`{"type":"string","enum":["NONE","RESULT_UNKNOWN","NO_EFFECT_CONFIRMED","SOURCE_HOLD","RUNTIME_HOLD","PERSISTENCE_UNKNOWN"]}`|

## OutcomeAck

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.outcome-ack.v2"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`report_id`|`{"$ref":"#/$defs/Uuid"}`|
|`outcome_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`state`|`{"const":"received"}`|
|`received_at`|`{"$ref":"#/$defs/Uint"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## StatusRequest

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.status-request.v2"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`kind`|`{"type":"string","enum":["start","prestart_abort","result"]}`|
|`object_id`|`{"$ref":"#/$defs/Uuid"}`|

## FileIdentity

|field|型/条件|
|---|---|
|`device`|`{"$ref":"#/$defs/UInt64Decimal"}`|
|`inode`|`{"$ref":"#/$defs/UInt64Decimal"}`|
|`uid`|`{"$ref":"#/$defs/Uint"}`|
|`mode`|`{"$ref":"#/$defs/Uint"}`|
|`nlink`|`{"const":1}`|
|`size`|`{"$ref":"#/$defs/Uint"}`|
|`mtime_ns`|`{"$ref":"#/$defs/UInt64Decimal"}`|
|`ctime_ns`|`{"$ref":"#/$defs/UInt64Decimal"}`|

## OperationStatus

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.operation-status.v2"}`|
|`request_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`kind`|`{"type":"string","enum":["start","prestart_abort","result"]}`|
|`object_id`|`{"$ref":"#/$defs/Uuid"}`|
|`object_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`state`|`{"type":"string","enum":["not_found","pending","granted","cancelled_prestart","received"]}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## PrivateFileWitness

|field|型/条件|
|---|---|
|`logical_id`|`{"$ref":"#/$defs/Token"}`|
|`identity`|`{"$ref":"#/$defs/FileIdentity"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|

## RetiredProcess

|field|型/条件|
|---|---|
|`service_id`|`{"$ref":"#/$defs/Token"}`|
|`process_instance_id`|`{"$ref":"#/$defs/Uuid"}`|
|`generation`|`{"$ref":"#/$defs/Positive"}`|
|`boot_id`|`{"$ref":"#/$defs/Sha256"}`|
|`birth_witness`|`{"$ref":"#/$defs/Sha256"}`|
|`retired`|`{"const":true}`|

## PrivatePreparedWitness

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.private-prepared-witness.v2"}`|
|`public`|`{"$ref":"#/$defs/PreparedPublic"}`|
|`root_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`repository_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`lock_identities`|`{"type":"array","items":{"$ref":"#/$defs/PrivateFileWitness"},"minItems":8,"maxItems":8}`|
|`configuration_files`|`{"type":"array","items":{"$ref":"#/$defs/PrivateFileWitness"},"minItems":1,"maxItems":64}`|
|`source_tree_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`runtime_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`ledger_device`|`{"$ref":"#/$defs/UInt64Decimal"}`|
|`ledger_inode`|`{"$ref":"#/$defs/UInt64Decimal"}`|
|`stage_identity`|`{"$ref":"#/$defs/FileIdentity"}`|
|`stage_path`|`{"type":"string","minLength":1,"maxLength":4096}`|
|`backup_path`|`{"type":"string","minLength":1,"maxLength":4096}`|
|`backup_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`retired_processes`|`{"type":"array","items":{"$ref":"#/$defs/RetiredProcess"},"minItems":0,"maxItems":64}`|
|`required_free_bytes`|`{"$ref":"#/$defs/Uint"}`|
|`reserved_owner_bytes`|`{"type":"integer","minimum":1,"maximum":8388608}`|
|`prepared_until`|`{"$ref":"#/$defs/Uint"}`|

## StartIntent

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.start-intent.v2"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`prepare_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`root_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`source_before`|`{"$ref":"#/$defs/Commit"}`|
|`source_target`|`{"$ref":"#/$defs/Commit"}`|
|`created_at`|`{"$ref":"#/$defs/Uint"}`|
|`phase`|`{"type":"string","enum":["open","retired"]}`|
|`terminal_outcome_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|

## ServiceProof

|field|型/条件|
|---|---|
|`service_id`|`{"$ref":"#/$defs/Token"}`|
|`desired_kind`|`{"type":"string","enum":["resident","timer","disabled"]}`|
|`proof_id`|`{"$ref":"#/$defs/Uuid"}`|
|`process_instance_id`|`{"anyOf":[{"$ref":"#/$defs/Uuid"},{"type":"null"}]}`|
|`pid`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|
|`birth_witness`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`loaded_source_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`state`|`{"type":"string","enum":["running","armed","stopped"]}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|
|`previous_heartbeat_at`|`{"anyOf":[{"$ref":"#/$defs/Uint"},{"type":"null"}]}`|

## RuntimeProof

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.runtime-proof.v2"}`|
|`proof_id`|`{"$ref":"#/$defs/Uuid"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`release`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`profile_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`boot_id`|`{"$ref":"#/$defs/Sha256"}`|
|`startup_nonce`|`{"$ref":"#/$defs/Sha256"}`|
|`services`|`{"type":"array","items":{"$ref":"#/$defs/ServiceProof"},"minItems":1,"maxItems":64}`|
|`activation_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|

## OutboxEntry

|field|型/条件|
|---|---|
|`report_id`|`{"$ref":"#/$defs/Uuid"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`raw`|`{"$ref":"#/$defs/RawJson"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`acknowledged`|`{"type":"boolean"}`|

## Binding

|field|型/条件|
|---|---|
|`code_seq`|`{"$ref":"#/$defs/Positive"}`|
|`release_id`|`{"$ref":"#/$defs/Sha256"}`|

## BudgetEntry

|field|型/条件|
|---|---|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`started_at`|`{"$ref":"#/$defs/Uint"}`|
|`ended_at`|`{"anyOf":[{"$ref":"#/$defs/Uint"},{"type":"null"}]}`|
|`reserved_ms`|`{"type":"integer","minimum":1,"maximum":120000}`|

## StoredEvidence

|field|型/条件|
|---|---|
|`logical_id`|`{"$ref":"#/$defs/Token"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`size`|`{"type":"integer","minimum":1,"maximum":8388608}`|
|`object_key`|`{"$ref":"#/$defs/Sha256"}`|

## LockFingerprints

|field|型/条件|
|---|---|
|`G`|`{"$ref":"#/$defs/Sha256"}`|
|`U`|`{"$ref":"#/$defs/Sha256"}`|
|`Q`|`{"$ref":"#/$defs/Sha256"}`|
|`R`|`{"$ref":"#/$defs/Sha256"}`|
|`C`|`{"$ref":"#/$defs/Sha256"}`|
|`A`|`{"$ref":"#/$defs/Sha256"}`|
|`M`|`{"$ref":"#/$defs/Sha256"}`|
|`O`|`{"$ref":"#/$defs/Sha256"}`|

## SavedEvidence

|field|型/条件|
|---|---|
|`logical_id`|`{"$ref":"#/$defs/Token"}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`raw`|`{"$ref":"#/$defs/RawJson"}`|

## SpentRecord

|field|型/条件|
|---|---|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`grant_id`|`{"$ref":"#/$defs/Uuid"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`start_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`started_at`|`{"$ref":"#/$defs/Uint"}`|
|`grant_raw`|`{"$ref":"#/$defs/RawJson"}`|
|`grant_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`public_preflight`|`{"$ref":"#/$defs/PreparedPublic"}`|
|`manifest_evidence_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`consent_evidence_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## ActiveOperation

|field|型/条件|
|---|---|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`phase`|`{"type":"string","enum":["STAGED","PREPARING","PREPARED","START_INTENT","STARTED","APPLIED","CANDIDATE_READY","ACTIVATING","UNKNOWN"]}`|
|`target`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`prepare_nonce`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`prepare_deadline`|`{"anyOf":[{"$ref":"#/$defs/Uint"},{"type":"null"}]}`|
|`private_witness`|`{"anyOf":[{"$ref":"#/$defs/PrivatePreparedWitness"},{"type":"null"}]}`|
|`grant_raw`|`{"anyOf":[{"$ref":"#/$defs/RawJson"},{"type":"null"}]}`|
|`start_receipt_id`|`{"anyOf":[{"$ref":"#/$defs/Uuid"},{"type":"null"}]}`|
|`candidate_generation`|`{"anyOf":[{"$ref":"#/$defs/Positive"},{"type":"null"}]}`|
|`startup_nonce`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`activation_receipt_id`|`{"anyOf":[{"$ref":"#/$defs/Uuid"},{"type":"null"}]}`|
|`runtime_proof`|`{"anyOf":[{"$ref":"#/$defs/RuntimeProof"},{"type":"null"}]}`|
|`reserved_owner_bytes`|`{"type":"integer","minimum":1,"maximum":8388608}`|
|`error_code`|`{"anyOf":[{"$ref":"#/$defs/Token"},{"type":"null"}]}`|

## TerminalRecord

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.terminal-record.v2"}`|
|`operation_id`|`{"$ref":"#/$defs/Uuid"}`|
|`state`|`{"type":"string","enum":["VERIFIED","DEFERRED","CANCELLED_NO_EFFECT"]}`|
|`attempt_key`|`{"$ref":"#/$defs/Sha256"}`|
|`outcome_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`proof_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`terminal_at`|`{"$ref":"#/$defs/Uint"}`|

## OwnerState

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.release-owner.v2"}`|
|`scope`|`{"$ref":"#/$defs/Scope"}`|
|`root_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`repository_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`activation_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`revision`|`{"$ref":"#/$defs/Positive"}`|
|`execution_revision`|`{"$ref":"#/$defs/Positive"}`|
|`trust_json`|`{"type":"string","minLength":1,"maxLength":8192}`|
|`legacy_evidence`|`{"type":"array","items":{"$ref":"#/$defs/StoredEvidence"},"minItems":0,"maxItems":128}`|
|`operation_evidence`|`{"type":"array","items":{"$ref":"#/$defs/StoredEvidence"},"minItems":0,"maxItems":10000}`|
|`lock_fingerprints`|`{"$ref":"#/$defs/LockFingerprints"}`|
|`installed_source_manifest_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`verified_source_manifest_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`profile`|`{"$ref":"#/$defs/Profile"}`|
|`consent`|`{"anyOf":[{"$ref":"#/$defs/StandingConsent"},{"type":"null"}]}`|
|`highest_seen`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`installed`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`highest_installed`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`verified_installed`|`{"$ref":"#/$defs/ReleaseRef"}`|
|`runtime_generation`|`{"$ref":"#/$defs/Positive"}`|
|`bindings`|`{"type":"array","items":{"$ref":"#/$defs/Binding"},"minItems":1,"maxItems":100000}`|
|`active`|`{"anyOf":[{"$ref":"#/$defs/ActiveOperation"},{"type":"null"}]}`|
|`spent`|`{"type":"array","items":{"$ref":"#/$defs/SpentRecord"},"minItems":0,"maxItems":10000}`|
|`outcomes`|`{"type":"array","items":{"$ref":"#/$defs/TerminalRecord"},"minItems":0,"maxItems":10000}`|
|`outbox`|`{"type":"array","items":{"$ref":"#/$defs/OutboxEntry"},"minItems":0,"maxItems":10000}`|
|`prepare_budget`|`{"type":"array","items":{"$ref":"#/$defs/BudgetEntry"},"minItems":0,"maxItems":128}`|

## SignedEnvelope

|field|型/条件|
|---|---|
|`alg`|`{"const":"PS256"}`|
|`kid`|`{"$ref":"#/$defs/Sha256"}`|
|`purpose`|`{"type":"string","enum":["mcs-auto-offer-v2","mcs-auto-compatibility-v2","mcs-auto-grant-v2","mcs-auto-ack-v2","mcs-auto-status-v2","mcs-publication-plan-v2","mcs-release-acceptance-v2"]}`|
|`payload`|`{"type":"string","minLength":1,"maxLength":10923,"pattern":"^[A-Za-z0-9_-]+$"}`|
|`signature`|`{"type":"string","minLength":1,"maxLength":512,"pattern":"^[A-Za-z0-9_-]+$"}`|

## CompatibilityCatalog

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.compatibility-catalog.v2"}`|
|`entries`|`{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"profile_sha256":{"$ref":"#/$defs/Sha256"},"envelope":{"$ref":"#/$defs/SignedEnvelope"}},"required":["profile_sha256","envelope"]},"minItems":1,"maxItems":4}`|

## ControlFile

|field|型/条件|
|---|---|
|`path`|`{"type":"string","minLength":1,"maxLength":4096}`|
|`mode`|`{"type":"string","enum":["100644","100755"]}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|

## ControlManifest

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.release-control-manifest.v2"}`|
|`owner_id`|`{"const":"mcs.updater"}`|
|`owner_revision`|`{"const":2}`|
|`entrypoints`|`{"type":"array","items":{"type":"string","minLength":1,"maxLength":4096},"minItems":1,"maxItems":16}`|
|`files`|`{"type":"array","items":{"$ref":"#/$defs/ControlFile"},"minItems":1,"maxItems":512}`|
|`dependency_policy_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`recovery_generation_sha256`|`{"$ref":"#/$defs/Sha256"}`|

## MigrationFile

|field|型/条件|
|---|---|
|`logical_id`|`{"$ref":"#/$defs/Token"}`|
|`before_sha256`|`{"anyOf":[{"$ref":"#/$defs/Sha256"},{"type":"null"}]}`|
|`size`|`{"$ref":"#/$defs/Uint"}`|
|`disposition`|`{"type":"string","enum":["preserve","archive_exact","translate_verified","must_be_absent"]}`|

## MigrationIntent

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.release-owner-migration.v2"}`|
|`migration_id`|`{"$ref":"#/$defs/Uuid"}`|
|`root_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`repository_fingerprint`|`{"$ref":"#/$defs/Sha256"}`|
|`source_schema`|`{"type":"string","enum":["none","mcs.release-owner.v1"]}`|
|`target_schema`|`{"const":"mcs.release-owner.v2"}`|
|`bridge_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`control_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`recovery_generation_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`external_floor_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`files`|`{"type":"array","items":{"$ref":"#/$defs/MigrationFile"},"minItems":1,"maxItems":128}`|
|`human_receipt_id`|`{"$ref":"#/$defs/Uuid"}`|
|`created_at`|`{"$ref":"#/$defs/Uint"}`|
|`phase`|`{"type":"string","enum":["prepared","old_archived","owner_replaced","verified"]}`|

## ApiError

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.release-error.v2"}`|
|`code`|`{"type":"string","enum":["NETWORK_UNAVAILABLE","RATE_LIMITED","SIGNATURE_INVALID","SCOPE_MISMATCH","CONTRACT_INVALID","PUBLICATION_UNVERIFIED","PUBLISH_PERMISSION_HOLD","COMPATIBILITY_HOLD","PROFILE_UNQUALIFIED","CONSENT_REQUIRED","CONSENT_REVOKED","OUTSIDE_WINDOW","PREPARE_BUSY","PREPARE_BUDGET_EXCEEDED","WITNESS_CHANGED","START_EXPIRED","ATTEMPT_SPENT","START_OUTCOME_UNKNOWN","CAPACITY_HOLD","STORAGE_UNKNOWN","APPLIED_UNVERIFIED","RUNTIME_HOLD","LEGACY_STATE_HOLD","RECOVERY_GENERATION_HOLD"]}`|
|`retry_after_ms`|`{"type":"integer","minimum":0,"maximum":86400000}`|

## PublicationReservation

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.publication-reservation.v2"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`request_id`|`{"$ref":"#/$defs/Sha256"}`|
|`code_seq`|`{"$ref":"#/$defs/Positive"}`|
|`revision`|`{"$ref":"#/$defs/Positive"}`|
|`fencing_token`|`{"$ref":"#/$defs/Positive"}`|
|`issued_at`|`{"$ref":"#/$defs/Uint"}`|
|`expires_at`|`{"$ref":"#/$defs/Uint"}`|

## PublicationFinalizeRequest

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.publication-finalize-request.v2"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`expected_revision`|`{"$ref":"#/$defs/Positive"}`|
|`fencing_token`|`{"$ref":"#/$defs/Positive"}`|
|`release_core_json`|`{"type":"string","minLength":1,"maxLength":8192}`|
|`compatibility_payloads`|`{"type":"array","items":{"$ref":"#/$defs/Compatibility"},"minItems":1,"maxItems":4}`|
|`candidate_assets`|`{"type":"array","items":{"$ref":"#/$defs/AssetSpec"},"minItems":1,"maxItems":28}`|

## AssetVerification

|field|型/条件|
|---|---|
|`asset_id`|`{"$ref":"#/$defs/Positive"}`|
|`name`|`{"type":"string","minLength":1,"maxLength":128}`|
|`sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`size`|`{"$ref":"#/$defs/Positive"}`|
|`verified`|`{"const":true}`|

## GithubVerification

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.github-verification.v2"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`github_release_id`|`{"$ref":"#/$defs/Positive"}`|
|`source`|`{"$ref":"#/$defs/SourceRef"}`|
|`publisher_run_id`|`{"$ref":"#/$defs/Positive"}`|
|`publisher_run_attempt`|`{"$ref":"#/$defs/Positive"}`|
|`verifier_closure_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`toolchain_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`release_attestation_evidence_sha256`|`{"$ref":"#/$defs/Sha256"}`|
|`release_verified`|`{"const":true}`|
|`immutable`|`{"const":true}`|
|`assets`|`{"type":"array","items":{"$ref":"#/$defs/AssetVerification"},"minItems":5,"maxItems":32}`|
|`observed_at`|`{"$ref":"#/$defs/Uint"}`|

## PublicationRegisterRequest

|field|型/条件|
|---|---|
|`schema_version`|`{"const":"mcs.publication-register-request.v2"}`|
|`publication_id`|`{"$ref":"#/$defs/Uuid"}`|
|`expected_revision`|`{"$ref":"#/$defs/Positive"}`|
|`fencing_token`|`{"$ref":"#/$defs/Positive"}`|
|`verification`|`{"$ref":"#/$defs/GithubVerification"}`|

END-FIELD-CATALOG-V2.0

<!-- END contracts/FIELD_CATALOG.md -->

<!-- BEGIN VALIDATION.md -->

# 納品物の検証結果

日付: 2026-11-10 JST。対象はこの**仕様・契約パッケージ**であり、hermes-mcs製品の実装ではない。

## この作成環境で実行した検査

```text
python3 validation/check_package.py
14 package checks: PASS
schema definitions: 70
closed object definitions: 60
positive synthetic examples: 11
negative synthetic vectors: 15
product acceptance specifications: 98 (NOT_RUN)
```

詳細ログ: `validation/package-check.txt`。
JSON Schema Draft2020-12の構造、参照、closed fields、合成入力、限定した意味検証、要求/grantおよび結果/ACKのhash・binding、strict JSON拒否、64bit精度、文書完読marker、根拠ID、interface stub構文、OFF初期設定を検査した。
`jsonschema`はこの開発用検査の依存であり、製品のstdlib coreへ追加する指示ではない。

## 実行していないもの

hermes-mcsのpytest/lint/build、実Git切替・実SQLite移行/復元、製品crypto試験、実SDC backend、GitHubへのwrite、macOS launchd/Keychain/実顧客端末、実運用の耐久性/性能試験は実行していない。
11正常例は構造・意味の例であり、署名済み権限ではない。15否定例が拒否されたことは、全SEM01–34の製品実装が完成した証拠ではない。
98項目の`execution_status`は全てNOT_RUN。旧抽象モデルの12試験成功を、本v2製品の成功へ流用していない。

## 残る導入前提

1. 実装HEADに対するP0棚卸しと最小変更・製品受入。
2. 実SDC backendへの閉契約実装、実公開鍵pin、issuer/toolchain/publisher policy。
3. main/tag/environment/Immutableの承認済み設定。
4. 実runtime profile/依存lock/control closure、独立復旧器の世代配備。
5. 自社検証機、profile別受入、顧客の範囲付き同意と保守窓。

未確定パラメータはnull、AUTO_ELIGIBLEはfalseのままとした。仕様書の納品は外部操作の許可ではない。

## 再現とファイル整合性

ZIP展開後、既存の開発環境で`python3 validation/check_package.py`を実行する。
`SHA256SUMS`はパッケージ内ファイルの受渡し整合性用であり、信頼rootや製品release署名の代わりではない。
同梱の`FULL_SPEC.md`は章とfield一覧を結合した読取り用。実装時はschema、fixtures、interfaces、受入JSONも読む。

END-VALIDATION-V2.0

<!-- END VALIDATION.md -->

END-HERMES-MCS-FULL-SPEC-V2.0
