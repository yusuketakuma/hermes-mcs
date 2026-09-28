# v2 — retired extraction generation

`extract_llm` schema v2 (最初の LLM 構造化抽出: meds/symptoms/vitals +
evidence 照合)。実装は v3 → v4 へ **in-place で置き換わったため、
この世代のコードは残っていない**。旧実装は git 履歴を参照する
(`git log --follow mcs/extract/v4/extract_llm.py`)。

DB 上の `extract_version=2` artifact は引き続き読み取り可能で、
有限 cohort による v4 置換の対象となる。仕様の正本は
`docs/semantic-evaluation.md` の v4 移行節。
