# 合成契約例

全ID/hash/時刻/tenantは架空。署名を付けておらず、開始権限・公開証拠ではない。
JSON wrapperのdefinitionに対応する`$defs`でdataを検証する。wrapperを製品wireへ送らない。
start-request/start-grant、およびoutcome-verified/outcome-ackは相互binding例。
その他は独立した構造/意味fixtureであり、完全な一回更新の全証拠集合ではない。
公開鍵・秘密鍵は同梱しない。製品cryptoの正常/不正vectorは既存の完全合成署名fixtureで追加する。
negative-vectors.jsonは意図的に無効な値を含み、全て指定層で拒否される必要がある。
