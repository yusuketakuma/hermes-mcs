# LINE WORKSの合成画面ギャラリー

READMEのLINE WORKS折りたたみで使う、完全合成の説明図2枚。
実画面のキャプチャではなく、実患者・実投稿・匿名化データは使用しない。
PNGはREADME表示用、SVGは編集用のソース。配置・書体・絵文字は版や設定で異なる。
画像内のボタンは操作できない。

## ソースとの対応

| 画面 | 根拠 |
|---|---|
| `01-delivery` | `adapters/lineworks/cards.py`（ボタンテンプレート）、`adapters/lineworks/delivery.py`（同じトークルームへカード・番号付き原文・添付を連続投稿）、`mcs/notify/notify_cards.py`（条件付き操作ラベル） |
| `02-task-confirm` | `adapters/lineworks/actions.py`（本人の1:1トークへの項目別入力・確認・確定）、`adapters/common/text.py`（入力ラベル・確認本文・結果の文言） |

ボタンの種類とCallbackは[公式ボタンテンプレート仕様](https://developers.worksmobile.com/jp/docs/bot-send-button)とも照合。
元投稿と抽出参照がある合成カードを例にしている。ボタンは状態・設定で変わり、
本文の長さや操作数によって追加投稿に分割される。
添付は保存・通知のみで、画像内容の解析は示していない。

1:1図はスタッフ一覧を使わず担当者を手入力する例。入力プロンプトと本文は
説明用に一部省略し、理由の入力後の確認画面を右側に示す。
「受け付けました。」と、処理結果取得後の「反映しました。」を区別する。
確認済みの表示はタスク完了を意味しない。表示更新は新規投稿になり、
古いボタンと開いている確認は無効になる。詳細は[導入・接続手順](../../guides/LINEWORKS.md)。

## 生成と確認

```bash
python3 scripts/development/generate_lineworks_gallery.py --png
python3 scripts/development/generate_lineworks_gallery.py --check
```

PNGの生成には日本語フォントとInkscape、またはmacOS標準Quick Lookを使用する。
新しいPython依存やLINE WORKS接続は不要。macOSの正方形サムネイルに合わせた
1100×1100のSVGを1650×1650で出力する。
SVGとPNGを一緒に更新し、PNGを開いて文字切れ・重なり・表示の意味を確認する。
`assets.json`は両形式のSHA256を記録し、`--check`でソースとの同期、PNGの
サイズ・CRC・画像データ・組のhashを検証する。CIのrelease-notes workflowも確認する。
