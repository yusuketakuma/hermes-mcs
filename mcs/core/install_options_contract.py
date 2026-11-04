"""導入時に記録する省略項目の共通条件。"""

OPTIONS = frozenset({'--no-brew', '--no-llm', '--no-plugin', '--no-recovery', '--with-yomitoku'})


def valid(value):
    return isinstance(value, list) and all(isinstance(x, str) and x in OPTIONS for x in value)
