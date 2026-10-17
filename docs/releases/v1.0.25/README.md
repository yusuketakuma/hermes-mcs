# 1.0.25 導入・更新・復旧の操作フロー

[操作フロー](guided-lifecycle.png)は実装に基づく説明図です。実画面のキャプチャではなく、患者情報・認証情報・接続先は含みません。

![導入は設定と接続確認まで、更新と復旧は状態を確認して人が実行を選ぶ流れ](guided-lifecycle.png)

比較元は公開・本番反映済みのv1.0.24（`35b5a5531e04441391bfb43b8389a7ca43a5dc94`）。以前も日本語の質問と番号選択がありました。今回、導入後に設定へ進む選択、必要な質問だけの推奨コース、更新・復旧の実行前確認へつなげました。所要時間やミス削減率は計測していません。

| 表示 | 根拠 |
|---|---|
| 導入確認と初期設定への接続 | [install.sh](../../../install.sh)、[EASY_SETUP](../../guides/EASY_SETUP.md) |
| 推奨・詳細コース | [mcs_setup.py](../../../mcs/ops/mcs_setup.py)、[合成回帰](../../../tests/ops/test_setup_guided.py) |
| 計画後に更新を選び、再計画で確認 | [mcs_upgrade.py](../../../scripts/mcs_upgrade.py)、[回帰](../../../tests/ops/test_lifecycle_guides.py) |
| 状態確認と明示的な復旧、既存承認の保持 | [mcs_recover.py](../../../deployment/recovery/mcs_recover.py) |

原稿は[SVG](guided-lifecycle.svg)。前の未公開作業ツリーにあった同フロー原稿を1.0.25の実装へ照合して引き継ぎました。公開済み1.0.24の図やtagは変更していません。

## 再生成と確認

追加依存は不要です。macOSのQuick LookでSVGを780pxのPNGへ書き出し、390px幅でも文字欠け・重なり・下端を確認します。

```sh
qlmanage -t -s 780 -o /tmp docs/releases/v1.0.25/guided-lifecycle.svg
sips -s format png /tmp/guided-lifecycle.svg.png --out docs/releases/v1.0.25/guided-lifecycle.png
```

2026-10-17にローカル描画を確認。公開時は画像commitの固定SHAを使い、raw URL到達とGitHub Release実画面での表示を別々に確認します。未公開のURLを本文へ追加して成功扱いしません。
