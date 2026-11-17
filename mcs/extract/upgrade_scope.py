"""Decide which earlier-generation extraction results the configured upgrade scope may rebuild.

``extract.upgrade_scope`` only selects work; it never changes how a result is
interpreted, so it is not a loaded source and no contract digest includes it.

- unset or ``"all"``: no restriction (the behaviour before this setting existed).
- ``"none"``: earlier-generation results are kept; nothing is rebuilt.
- ``{"project_ids": [positive int, ...]}``: only those patients are rebuilt.
- any other value, a non-object ``extract`` or an unreadable configuration:
  closed, like ``"none"``.

A closed scope never stops new arrivals, edited bodies, same-generation QC or
thin retries, nor a human correction report: an earlier-generation result is
one whose body already has a result of another generation and no result of
the running generation, and a human report admits its post explicitly.
Heavy modules are imported inside functions so that configuration checks can
call this module without loading the extraction engine.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

import mcs_util

_UNSET = object()
_INVALID = ('"all"・"none"・{"project_ids": [正の整数, …]}（重複なし・1件以上）の'
            'いずれかを指定してください。この値では過去世代を作り直しません')
_UNREADABLE = "設定を読み取れないため、過去世代を作り直しません"
_CONTAINER = "extract の設定がJSONオブジェクトではないため、過去世代を作り直しません"
_KINDS = ("llm", "rule")


@dataclass(frozen=True)
class Scope:
    """One resolved upgrade scope; ``error`` says why a configured value closed it."""
    mode: str
    project_ids: tuple = ()
    error: str | None = None

    @property
    def is_open(self) -> bool:
        return self.mode == "all"


OPEN = Scope("all")


def parse(value=_UNSET) -> Scope:
    """Resolve one ``extract.upgrade_scope`` value; anything unrecognised closes."""
    if value is _UNSET or type(value) is str and value == "all":
        return OPEN
    if type(value) is str and value == "none":
        return Scope("none")
    if type(value) is dict and set(value) == {"project_ids"}:
        ids = value["project_ids"]
        if (type(ids) is list and ids and all(type(item) is int and item > 0 for item in ids)
                and len(set(ids)) == len(ids)):
            return Scope("projects", tuple(sorted(ids)))
    return Scope("none", error=_INVALID)


def resolve(cfg) -> Scope:
    """Scope of a loaded configuration; ``None`` (an unreadable one) is closed."""
    if not isinstance(cfg, dict):
        return Scope("none", error=_UNREADABLE)
    if "extract" not in cfg:
        return OPEN
    ext = cfg["extract"]
    if not isinstance(ext, dict):
        return Scope("none", error=_CONTAINER)  # validate_config reports it as an error
    return parse(ext.get("upgrade_scope", _UNSET))


def live() -> Scope:
    """Scope of the host configuration, read now; a failed read closes it."""
    return resolve(mcs_util.load_config_checked(mcs_util.CONF_PATH, missing_ok=True))


def allows(scope: Scope, project_id) -> bool:
    """True when this patient's earlier-generation results may be rebuilt."""
    return scope.is_open or (scope.mode == "projects" and type(project_id) is int
                             and project_id in scope.project_ids)


def config_problems(cfg) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the setting, in the wording of ``validate_config``."""
    ext = cfg.get("extract") if isinstance(cfg, dict) else None
    if not isinstance(ext, dict) or "upgrade_scope" not in ext:
        return [], []
    scope = parse(ext["upgrade_scope"])
    errors = [f"extract.upgrade_scope: {scope.error}"] if scope.error else []
    warnings = [] if scope.is_open else ["過去世代の更新を絞っています（extract.upgrade_scope）"]
    return errors, warnings


def _target(kind, version):
    if kind == "llm":
        import extract_llm_qc
        target = extract_llm_qc.EXTRACT_VERSION if version is None else version
        return extract_llm_qc.KIND, "extract_version", True, target
    if kind == "rule":
        import extract
        target = extract.RULE_VERSION if version is None else version
        return extract.KIND, "rule_version", False, target
    raise ValueError("upgrade_scope_kind_invalid")


def prior_generation_sql(kind: str, alias: str = "m", *, version: int | None = None) -> str:
    """SQL boolean: message ``alias`` has a result of another generation for its
    current body and no current result of the running (or ``version``) generation."""
    from mcs_queries import current_extract_pred, json_or_null, source_extract_pred
    artifact_kind, key, error_check, target = _target(kind, version)
    if type(target) is not int or target < 0:
        raise ValueError("upgrade_scope_version_invalid")
    return (f"(EXISTS (SELECT 1 FROM artifacts g WHERE g.kind='{artifact_kind}'"
            f" AND g.message_id={alias}.message_id"
            f"{source_extract_pred('g', alias, error_check=error_check)}"
            f" AND COALESCE(json_extract({json_or_null('g.meta')},'$.{key}'),0)<>{target})"
            f" AND NOT EXISTS (SELECT 1 FROM artifacts t WHERE t.kind='{artifact_kind}'"
            f" AND t.message_id={alias}.message_id"
            f"{current_extract_pred('t', alias, error_check=error_check)}"
            f" AND json_extract({json_or_null('t.meta')},'$.{key}')={target}))")


def _admitted_sql(scope: Scope) -> str:
    if scope.mode == "projects":
        return "m.project_id IN (" + ",".join(str(pid) for pid in scope.project_ids) + ")"
    return "0"


def _human_requested_sql(version) -> str:
    import extract_llm_qc as qc
    from mcs_queries import EXTRACT_FEEDBACK_KIND
    return (f"({qc._has_kind(EXTRACT_FEEDBACK_KIND, 'h0')}"
            f" AND EXISTS ({qc._human_flagged_sql(val='1', version=version)}))")


def _exempt_sql(scope: Scope, kind: str, version) -> str:
    admitted = _admitted_sql(scope)
    return f"({admitted}) OR {_human_requested_sql(version)}" if kind == "llm" else admitted


def selection_sql(scope: Scope, kind: str, *, version: int | None = None) -> str:
    """AND-fragment over messages ``m`` that keeps closed-scope earlier-generation
    posts out of a selection; empty for an open scope, so its SQL is unchanged."""
    if kind not in _KINDS:
        raise ValueError("upgrade_scope_kind_invalid")
    if scope.is_open:
        return ""
    return (f" AND NOT ({prior_generation_sql(kind, 'm', version=version)}"
            f" AND NOT ({_exempt_sql(scope, kind, version)}))")


def _attachment_generations(db, project_id, source_key, source_binding, contract):
    """(newest result id of another contract, whether a current-contract result exists)."""
    import attachment_extraction
    row = db.execute(
        "SELECT MAX(CASE WHEN c<>? THEN artifact_id END), COALESCE(MAX(c=?),0) FROM ("
        "SELECT artifact_id, CASE WHEN json_valid(content) THEN json_extract(content,'$.contract') END AS c,"
        " CASE WHEN json_valid(content) THEN json_extract(content,'$.source_binding') END AS b"
        " FROM artifacts WHERE kind=? AND project_id=?"
        " AND CASE WHEN json_valid(meta) THEN json_extract(meta,'$.source_key') END=?) WHERE b=?",
        (contract, contract, attachment_extraction.KIND, project_id, source_key, source_binding)).fetchone()
    return row[0], bool(row[1])


def attachment_admitted(db, scope: Scope, project_id, source_key, source_binding, contract,
                        feedback_revision: int = 0) -> bool:
    """True unless a closed scope keeps this OCR page's earlier-contract result.

    A correction report newer than that result admits the page, as a human
    report admits a post."""
    if allows(scope, project_id):
        return True
    prior, current = _attachment_generations(db, project_id, source_key, source_binding, contract)
    return prior is None or current or feedback_revision > prior


class _Rows:
    """Borrow a connection with ``sqlite3.Row`` rows and restore its factory."""

    def __init__(self, db):
        self.db, self.factory = db, db.row_factory

    def __enter__(self):
        self.db.row_factory = sqlite3.Row
        return self.db

    def __exit__(self, *exc):
        self.db.row_factory = self.factory
        return False


def _llm_counts(db, scope):
    import extract_llm_qc as qc
    from mcs_queries import ELIGIBLE_MESSAGE, V4_PROJECTION_KIND, current_v4_id
    counts = dict.fromkeys(("runnable", "sealed", "held", "failed", "outside"), 0)
    rows = db.execute(f"""SELECT
        ({qc._has_kind(V4_PROJECTION_KIND, 'v0', project=True)} AND {current_v4_id()} IS NOT NULL) AS sealed,
        ({qc.pending_pred(db, include_thin=False)}) AS pending,
        EXISTS ({qc._hold_sql()}) AS held,
        ({qc._retry_budget_sql()}) AS budget,
        ({_exempt_sql(scope, 'llm', None)}) AS exempt
        FROM messages m WHERE {ELIGIBLE_MESSAGE} AND {prior_generation_sql('llm')}""")
    for row in rows:
        if row["sealed"]:
            counts["sealed"] += 1
        elif not row["pending"] and row["held"]:
            counts["held"] += 1
        elif row["pending"] and not row["budget"]:
            counts["failed"] += 1
        else:
            # Anything not sealed, held or exhausted is still to be rebuilt.
            counts["runnable"] += 1
            counts["outside"] += not (scope.is_open or row["exempt"])
    return counts


def _rule_counts(db, scope):
    from mcs_queries import ELIGIBLE_MESSAGE
    total, outside = db.execute(
        # A NULL project id is never admitted (NULL IN (...) is NULL): count it outside, as selected.
        f"SELECT COUNT(*), COALESCE(SUM(NOT COALESCE(({_admitted_sql(scope)}),0)),0) FROM messages m "
        f"WHERE {ELIGIBLE_MESSAGE} AND {prior_generation_sql('rule')}").fetchone()
    return total, 0 if scope.is_open else outside


def _attachment_counts(db, cfg, scope):
    """Live OCR pages whose only results carry another extraction contract."""
    import attachment_feedback
    import attachment_inference
    import attachment_ocr
    import original_source_keys
    if isinstance(cfg, dict) and (attachment_ocr.config_error(cfg)
                                  or not cfg.get("attachment_ocr", {}).get("enabled", False)):
        return 0, 0  # readers use no attachment result while OCR is off
    contract = attachment_inference._contract(cfg if isinstance(cfg, dict) else {})
    total = outside = 0
    for row in db.execute(
            "SELECT artifact_id, project_id, CASE WHEN typeof(meta)='text' AND length(meta)<=32768 "
            "AND json_valid(meta) THEN json_extract(meta,'$.attachment_id') END AS attachment_id "
            "FROM artifacts WHERE kind=? ORDER BY artifact_id", (attachment_ocr.KIND,)).fetchall():
        doc = attachment_ocr.current(db, row["attachment_id"], project_id=row["project_id"])
        if doc is None or doc["artifact_id"] != row["artifact_id"]:
            continue
        for page in doc["pages"]:
            if not page["text"].strip():
                continue
            key = f"attachment_ocr:{row['artifact_id']}:{page['page']}"
            source = original_source_keys.source(db, row["project_id"], key)
            if source is None:
                continue
            prior, current = _attachment_generations(db, row["project_id"], key,
                                                     source["_source_binding"], contract)
            if prior is None or current:
                continue
            total += 1
            revision = attachment_feedback.revision(db, row["project_id"], key, source["_source_binding"])
            outside += not allows(scope, row["project_id"]) and revision <= prior
    return total, outside


def prior_generation_remaining(db, cfg) -> dict:
    """Counts of earlier-generation results still to rebuild (no bodies or names).

    ``llm``, ``rule`` and ``attachment_pages`` can still be rebuilt; ``sealed``,
    ``held`` and ``failed`` posts cannot be rebuilt automatically; ``outside_scope``
    is the part of the rebuildable counts the configured scope keeps closed."""
    scope = resolve(cfg)
    with _Rows(db):
        llm = _llm_counts(db, scope)
        rule, rule_outside = _rule_counts(db, scope)
        pages, pages_outside = _attachment_counts(db, cfg, scope)
    return {"llm": llm["runnable"], "rule": rule, "attachment_pages": pages,
            "sealed": llm["sealed"], "held": llm["held"], "failed": llm["failed"],
            "outside_scope": {"llm": llm["outside"], "rule": rule_outside,
                              "attachment_pages": pages_outside}}


def doctor_warnings(db, cfg) -> list[str]:
    """Name what a closed scope is holding back; silent when open or nothing waits."""
    if resolve(cfg).is_open:
        return []
    held = prior_generation_remaining(db, cfg)["outside_scope"]
    if not any(held.values()):
        return []
    return [f"upgrade_scope で止まっている過去世代: 投稿{held['llm']}件・ルール{held['rule']}件・"
            f"添付{held['attachment_pages']}頁（extract.upgrade_scope を開くまで作り直しません）"]
