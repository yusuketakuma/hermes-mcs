# 抽出世代 2 の記録

`extract_version=2` は台帳に残る抽出結果の世代番号です。現行のLLM抽出は [extract/llm](../llm/) にあり、保存結果に使う世代番号は [inference_version.py](../../core/inference_version.py) の `LLM_EXTRACT_VERSION` で確認できます。旧い結果の再抽出と保留の条件は[推論の仕様](../../../docs/specs/inference-engine-v1.md)を参照してください。
