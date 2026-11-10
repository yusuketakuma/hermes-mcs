# 共通契約の所有とmirror

CSDC `contracts/mcs-auto/v3/` が正本、MCS `adapters/sdc/contracts/mcs-auto/v3/` はbyte一致mirror。コピー対象はルートの `CONTRACT_MANIFEST.json` に列挙した相対pathで、各root内で相対配置を維持する。

03/07/08章はAPI・権限・永続化・段階配信の意味条件として契約bundleに含む。schemaだけをコピーして意味を別々に実装しない。機密sourceや製品のPython実装はCSDCへ複製しない。

文書版3.0と新規autoのwire v3を分けて扱う。既存の製品v1 core/trust/個別TTY・`/v2/releases/apply/*`は書き換えない。変更には09章のCRと両者の再受領が必要。

`CONTRACT_MANIFEST.json` のdigestはC0時の版照合用であり本番の署名や実行許可ではない。生成式は `VALIDATION.md` を参照。
