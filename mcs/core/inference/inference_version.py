"""Shared public inference-version interpretation and currentness predicates."""
import re

VERSION_KEY = 'inference_engine_version'
LEGACY_VERSION = '0.1'
# Development target only; acceptance remains with the existing promotion owner.
CURRENT_VERSION = '1.0'
# LLM extraction's internal contract counter; independent of public generation.
LLM_EXTRACT_VERSION = 11

_MAX_VERSION_LENGTH = 32
_VERSION = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', re.ASCII)
_COLUMN = re.compile(r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?', re.ASCII)


def _valid_version(value: object) -> bool:
    return (isinstance(value, str) and len(value) <= _MAX_VERSION_LENGTH
            and _VERSION.fullmatch(value) is not None)


def _target_version(target: object) -> str:
    if not _valid_version(target):
        raise ValueError('invalid inference version target')
    return target


def read_version(meta: object) -> str | None:
    """Read a canonical version, treating only a missing dict key as legacy."""
    if not isinstance(meta, dict):
        return None
    if VERSION_KEY not in meta:
        return LEGACY_VERSION
    value = meta[VERSION_KEY]
    return value if _valid_version(value) else None


def is_current(meta: object, *, target: str = CURRENT_VERSION) -> bool:
    """Match the public version exactly, without interpreting internal versions."""
    return read_version(meta) == _target_version(target)


def valid_meta_pred(meta_column: str = 'a.meta') -> str:
    """Accept canonical public-version syntax, with only an absent key meaning legacy."""
    if not isinstance(meta_column, str) or _COLUMN.fullmatch(meta_column) is None:
        raise ValueError('invalid inference metadata column')
    path = f"'$.{VERSION_KEY}'"
    value = f'json_extract({meta_column},{path})'
    dot = f"instr({value},'.')"
    major, minor = f'substr({value},1,{dot}-1)', f'substr({value},{dot}+1)'
    canonical = (f"length({value}) BETWEEN 3 AND {_MAX_VERSION_LENGTH} "
        f"AND {value} NOT GLOB '*[^0-9.]*' "
        f"AND length({value})-length(replace({value},'.',''))=1 "
        f"AND {dot}>1 AND {dot}<length({value}) "
        f"AND ({major}='0' OR substr({major},1,1) BETWEEN '1' AND '9') "
        f"AND ({minor}='0' OR substr({minor},1,1) BETWEEN '1' AND '9')")
    return (f'(CASE WHEN json_valid({meta_column}) THEN '
            f"CASE WHEN json_type({meta_column})='object' THEN "
            f'CASE WHEN json_type({meta_column},{path}) IS NULL THEN 1 '
            f"WHEN json_type({meta_column},{path})='text' THEN ({canonical}) "
            'ELSE 0 END ELSE 0 END ELSE 0 END)')


def current_meta_pred(meta_column: str = 'a.meta', *, target: str = CURRENT_VERSION) -> str:
    """Build a zero-or-one SQLite predicate for an internal metadata column."""
    target = _target_version(target)
    # Callers supply a code-owned column name, never a user SQL expression.
    if not isinstance(meta_column, str) or _COLUMN.fullmatch(meta_column) is None:
        raise ValueError('invalid inference metadata column')
    path = f"'$.{VERSION_KEY}'"
    kind = f'json_type({meta_column},{path})'
    value = f'json_extract({meta_column},{path})'
    missing_matches = int(target == LEGACY_VERSION)
    # Nested CASE gates every JSON access even under arbitrary query plans.
    # json_type returns SQL NULL for absence and the string 'null' for JSON null.
    return (f'(CASE WHEN json_valid({meta_column}) THEN '
            f"CASE WHEN json_type({meta_column})='object' THEN "
            f'CASE WHEN {kind} IS NULL THEN {missing_matches} '
            f"WHEN {kind}='text' THEN ({value} COLLATE BINARY='{target}') "
            'ELSE 0 END ELSE 0 END ELSE 0 END)')
