# 1.0.24 日本語で進める初期設定

目的別の質問に、番号や「はい／いいえ」で回答する流れの説明図です。完全合成であり、端末画面のスクリーンショットではありません。入力値・患者情報・認証情報は含みません。

![最大12区分から必要な手順だけを表示し、番号や「はい／いいえ」で回答できる初期設定の説明図](japanese-setup.png)

## 根拠と比較

比較元は `v1.0.23`（`1c11122808bde5472800df24fb01b94f6f1158be`）、対象は1.0.24候補です。
以前も対話式設定とEnterによる既存値保持はありました。今回、質問を目的別に分け、使わない接続設定の見出しを省き、選択肢の番号入力と「はい／いいえ」を追加しました。従来の値名や y / n による回答も使えます。図は今回の流れだけを示します。

| 図の内容 | 実装の根拠 |
|---|---|
| 冒頭2区分の名称と順序、最大12区分 | [mcs_setup.py](../../../mcs/ops/mcs_setup.py) の `WIZARD` |
| 必要な区分だけ表示、連番 | 同ファイルの `_wizard` |
| 1=hermes / 2=standalone、はい／いいえ、従来入力の維持 | 同ファイルの `_ask` / `_parse_answer` |
| Enterで現在値・既定値を保持、未入力の必須項目を案内 | 同ファイルの `_ask` |
| 回帰確認の対象 | [test_setup_japanese.py](../../../tests/ops/test_setup_japanese.py) |

図では実際の質問文と選択肢の説明を分けて配置しています。実端末では設定キー・現在値・既定値なども表示します。質問の総数が12個という意味ではありません。

## 再生成

`japanese-setup.svg` が正本です。既存のリリース図と同じmacOS Quick Lookと`sips`を使います。追加依存は不要です。リポジトリルートで次のPythonを実行します。

```python
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET

assets = Path("docs/releases/v1.0.24").resolve()
ET.register_namespace("", "http://www.w3.org/2000/svg")
inner = ET.parse(assets / "japanese-setup.svg").getroot()
inner.set("x", "130")
inner.set("y", "0")
outer = ET.Element("{http://www.w3.org/2000/svg}svg", {
    "width": "650", "height": "650", "viewBox": "0 0 650 650",
})
outer.append(inner)
with tempfile.TemporaryDirectory(prefix="mcs-v1024-render-") as tmp:
    wrapper = Path(tmp) / "render.svg"
    ET.ElementTree(outer).write(wrapper, encoding="utf-8", xml_declaration=True)
    subprocess.run(["qlmanage", "-t", "-s", "1300", "-o", tmp, str(wrapper)],
                   check=True, timeout=30)
    subprocess.run(["sips", "--cropToHeightWidth", "1300", "780",
                    str(wrapper) + ".png", "--out", str(assets / "japanese-setup.png")],
                   check=True, timeout=30)
```

2026-10-17、PNG全体（780×1300）と幅390pxの縮小表示で、文字欠け・重なり・余白を目視確認しました。フォントやOSで描画が変わるため、再生成後も両サイズを確認します。

この記録時点では公開前です。固定した公開tagまたはcommitの画像URLを使用し、公開後にURL到達性とGitHub Release実画面の表示を別々に確認します。ローカルの描画確認は公開・本番配備の完了を示しません。
