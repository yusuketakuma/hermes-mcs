"""Validate cloud prices, accounting rows and bound reservation evidence."""
from __future__ import annotations
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import re
import time
from types import MappingProxyType

import anthropic_client
import openai_client
import provider_registry
from anthropic_client import AnthropicError, MODEL
from mcs_util import payload_hash, strict_json

PRICE_VERSION = anthropic_client.PRICE_VERSION


PRICE_CHECKED_ON = anthropic_client.PRICE_CHECKED_ON


PRICE_VALID_UNTIL = anthropic_client.PRICE_VALID_UNTIL


PRICE_SOURCES = anthropic_client.PRICE_SOURCES


INPUT_RESERVATION_BOUND = 1048576


INPUT_RESERVATION_MARGIN = 256


PRICES = anthropic_client.PRICES


LONG_INPUT_THRESHOLD = anthropic_client.LONG_INPUT_THRESHOLD


JST = timezone(timedelta(hours=9))


KINDS = ("cloud_usage_reserve", "cloud_usage_result", "cloud_usage_dryrun")
_ROW_COLUMNS = "artifact_id,kind,project_id,message_id,model,meta,created_at"
# The scans match only a string-valued member; the index key alone would also match other JSON types.
_TEXT_ID = " AND json_type(meta,'$.reservation_id')='text'"


ROUTES = ("patient_context", "reextract", "semantic_backlog")


OPENAI_PRICE_VERSION = openai_client.PRICE_VERSION


OPENAI_PRICE_CHECKED_ON = openai_client.PRICE_CHECKED_ON


OPENAI_PRICE_VALID_UNTIL = openai_client.PRICE_VALID_UNTIL


OPENAI_PRICE_SOURCES = openai_client.PRICE_SOURCES


PROVIDERS = {"anthropic": MODEL, "openai": openai_client.MODEL}


if ({model for model, _ in anthropic_client.PRICE_SNAPSHOTS}
        & {model for model, _ in openai_client.PRICE_SNAPSHOTS}):
    # Stored receipts predate an explicit provider field; ambiguous models fail closed.
    raise RuntimeError("duplicate_cloud_price_definition")


PRICE_DEFINITIONS = MappingProxyType({**anthropic_client.PRICE_SNAPSHOTS,
                                      **openai_client.PRICE_SNAPSHOTS})


_CURRENT_PRICE = object()


def _configured_price_version(model):
    if model == PROVIDERS["anthropic"]:
        # Read at call time: a price rotation replaces the current Haiku version.
        return anthropic_client.PRICE_VERSION
    if model in anthropic_client.CURRENT_PRICE_VERSIONS:
        return anthropic_client.CURRENT_PRICE_VERSIONS[model]
    if model in openai_client.CURRENT_PRICE_VERSIONS:
        return openai_client.CURRENT_PRICE_VERSIONS[model]
    raise AnthropicError("pricing_unverified", "price_or_limit_invalid")


def current_price_version(model, now=None):
    """The price table in force for a new send on the JST day of ``now``.

    The newest configured table applies from its recheck day; before that day the
    newest table already rechecked applies, so a rotation never reprices an
    earlier day with a table that was not yet confirmed.
    """
    configured = _configured_price_version(model)
    day = datetime.fromtimestamp(time.time() if now is None else now, JST).date().isoformat()
    chosen = PRICE_DEFINITIONS.get((model, configured))
    if chosen is not None and chosen.get("price_checked_on", "") <= day:
        return configured
    earlier = [d for (m, _v), d in PRICE_DEFINITIONS.items()
               if m == model and isinstance(d.get("price_checked_on"), str) and d["price_checked_on"] <= day]
    if not earlier:
        return configured
    return max(earlier, key=lambda d: (d["price_checked_on"], d["price_valid_until"]))["price_version"]


def _price_definition(model, price_version=_CURRENT_PRICE, *, now=None):
    if not isinstance(model, str):
        raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    if price_version is _CURRENT_PRICE:
        price_version = current_price_version(model, now)
    if not isinstance(price_version, str):
        raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    definition = PRICE_DEFINITIONS.get((model, price_version))
    if (definition is None or definition.get("provider") not in ("anthropic", "openai")
            or definition.get("model") != model or definition.get("price_version") != price_version):
        raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    return definition


_BINDING_KEYS = ("reservation_id", "request_fp", "route", "project_id",
                 "unit_key", "message_id", "model", "price_version", "logical_request_fp", "logical_target_id", "logical_unit_fp",
                 "effort", "profile", "input_binding")


PROFILE_MAX_TOKENS = (8192, 16384, 32768)


def profile_contract_valid(provider, model, effort, profile, max_tokens):
    """One finite (model, effort, profile) contract per provider. A selection
    without a profile is the provider's original contract: OpenAI Luna at
    "low", Anthropic Haiku with effort None (its wire effort is "low")."""
    module = provider_registry.provider_module(provider)
    if (module is None or not isinstance(model, str)
            or effort is not None and not isinstance(effort, str)
            or profile is not None and not isinstance(profile, str)):
        return False
    if profile is not None:
        return ((model, effort) in module.VALID_MODEL_EFFORTS
                and module.PROFILE_DEFINITIONS.get(profile) == (model, effort) and max_tokens in PROFILE_MAX_TOKENS)
    if provider == "anthropic":
        # The original Anthropic contract carries no effort; its model is checked by
        # the caller against the price definition (rotations included). A model that
        # only a profile may select (Sonnet) never stands without its profile.
        profile_only = {selected for selected, _ in module.PROFILE_DEFINITIONS.values()} - {module.ORIGINAL_CONTRACT[0]}
        return effort is None and model not in profile_only
    return (model, effort) == module.ORIGINAL_CONTRACT


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("cost_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("cost_invalid") from None
    if not result.is_finite() or result < 0 or result > Decimal("1e15"):
        raise ValueError("cost_invalid")
    return result


def pricing_valid(now=None, *, model=MODEL):
    now = time.time() if now is None else now
    try:
        definition = _price_definition(model, now=now)
    except AnthropicError:
        return False
    day = datetime.fromtimestamp(now, JST).date().isoformat()
    return definition["price_checked_on"] <= day <= definition["price_valid_until"]


# Days before a price table's recheck date from which diagnostics announce it.
PRICE_RECHECK_NOTICE_DAYS = 7


def price_info(model, now=None):
    now = time.time() if now is None else now
    definition = _price_definition(model, now=now)
    today = datetime.fromtimestamp(now, JST).date()
    remaining = (datetime.strptime(definition["price_valid_until"], "%Y-%m-%d").date() - today).days
    valid = pricing_valid(now, model=model)
    return {"price_version": definition["price_version"],
            "price_checked_on": definition["price_checked_on"],
            "price_valid_until": definition["price_valid_until"],
            "price_valid": valid,
            "price_days_remaining": remaining,
            "price_recheck_due": not valid or remaining <= PRICE_RECHECK_NOTICE_DAYS}


def _texts(body):
    """The request's text parts: the system prompt and each message's string or text blocks."""
    yield str(body.get("system", ""))
    yield str(body.get("instructions", ""))
    if isinstance(body.get("input"), str):
        yield body["input"]
    for message in body.get("messages", []) if isinstance(body.get("messages"), list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    yield block["text"]


def input_bound(body):
    """Input tokens to reserve for one request (never above the 1M ceiling)."""
    return min(INPUT_RESERVATION_BOUND,
               sum(len(text.encode("utf-8")) for text in _texts(body)) + INPUT_RESERVATION_MARGIN)


def _reservation_cost(input_bound, max_tokens, *, legacy=False, model=MODEL, price_version=_CURRENT_PRICE):
    definition = _price_definition(model, price_version)
    return provider_registry.provider_module(definition["provider"]).reservation_cost(
        definition, input_bound, max_tokens, legacy=legacy)


def max_cost(max_tokens, *, input_bound=INPUT_RESERVATION_BOUND, now=None, model=MODEL):
    definition = _price_definition(model, now=now)
    if (not pricing_valid(now, model=model) or type(max_tokens) is not int or not 1 <= max_tokens <= definition["max_tokens"]
            or type(input_bound) is not int or not 1 <= input_bound <= INPUT_RESERVATION_BOUND):
        raise AnthropicError("pricing_unverified", "price_or_limit_invalid")
    return _reservation_cost(input_bound, max_tokens, model=model, price_version=definition["price_version"])


def usage_cost(usage, *, model=MODEL, price_version=_CURRENT_PRICE):
    definition = _price_definition(model, price_version)
    return provider_registry.provider_module(definition["provider"]).usage_cost(
        definition, definition["validate_usage"](usage), input_limit=INPUT_RESERVATION_BOUND)


def _unbilled(http_class):
    """Only an attempt proven never sent settles at zero. Every HTTP outcome keeps
    the reserved maximum: the published error list does not state which statuses
    are unbilled."""
    return http_class == "not_sent"


def _periods(now):
    try:
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError
        date = datetime.fromtimestamp(now, JST)
        day = date.replace(hour=0, minute=0, second=0, microsecond=0)
        return day.timestamp(), day.replace(day=1).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        raise AnthropicError("pricing_unverified", "usage_timestamp_invalid") from None


def _loads(value):
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise AnthropicError("pricing_unverified", "usage_invalid") from None


def _accounting_meta(value):
    try:
        result = strict_json(value)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise AnthropicError("pricing_unverified", "usage_invalid") from None


@contextmanager
def _accounting_snapshot(db):
    # A pending result keeps the SQLite read snapshot without committing a caller's transaction.
    anchor = db.execute("SELECT artifact_id FROM (SELECT artifact_id FROM artifacts "
        "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') LIMIT 1) "
        "UNION ALL SELECT NULL UNION ALL SELECT NULL")
    try:
        anchor.fetchone()
        yield
    finally:
        anchor.close()


def _validate_accounting_timestamps(db):
    # Values outside this conservative interior are checked precisely; no
    # otherwise valid historical timestamp is excluded by these scan bounds.
    candidates = db.execute("SELECT created_at FROM artifacts WHERE kind IN (?,?) AND CASE "
        "WHEN typeof(created_at) NOT IN ('integer','real') THEN 1 "
        "ELSE created_at < ? OR created_at > ? END", (KINDS[0], KINDS[1], -62135500000, 253402000000))
    for row in candidates:
        _periods(row[0])


def _indexed_rows(db, kinds, reservation_ids, *, columns, where="", args=()):
    """Rows naming these reservations, through the reservation index, or None.

    None means the write-time guard has not vouched for the retained rows
    (or these kinds are outside it): the caller's full scan decides instead.
    The caller holds the read snapshot, so the guard and the rows agree."""
    import ledger_schema
    state = ledger_schema.cloud_usage_guard_state(db)
    if state is None or not state[0] or not set(kinds) <= set(ledger_schema.CLOUD_USAGE_KINDS):
        return None
    ids = list(reservation_ids)
    rows = []
    for kind in kinds:
        for offset in range(0, len(ids), 900):
            group = ids[offset:offset + 900]
            # The index's own WHERE term is repeated so the planner can prove the partial index applies.
            rows.extend(db.execute(
                f"SELECT {columns} FROM artifacts INDEXED BY {ledger_schema.CLOUD_USAGE_INDEX} "
                f"WHERE kind IN {ledger_schema.CLOUD_USAGE_KINDS_SQL} AND kind=? "
                f"AND {ledger_schema.CLOUD_USAGE_RESERVATION_SQL} IN ("
                + ','.join('?' for _ in group) + ')' + where, (kind, *group, *args)))
    return sorted(rows, key=lambda row: row[0])


def _binding(meta):
    return tuple(meta.get(k) for k in _BINDING_KEYS)


def _receipt_binding(reservation):
    return {key: reservation.get(key) for key in _BINDING_KEYS
            if key not in ("effort", "profile", "input_binding") or key in reservation}


def _reserve_valid(meta, row):
    model = meta.get("model")
    price_version = meta.get("price_version")
    if not isinstance(price_version, str):
        raise AnthropicError("pricing_unverified", "reservation_invalid")
    definition = _price_definition(model, price_version)
    if ("effort" in meta) != ("profile" in meta):
        raise AnthropicError("pricing_unverified", "reservation_invalid")
    if "effort" in meta:
        if not profile_contract_valid(definition["provider"], model, meta["effort"], meta["profile"],
                                      meta.get("max_tokens")):
            raise AnthropicError("pricing_unverified", "reservation_invalid")
    if (not isinstance(meta.get("reservation_id"), str) or len(meta["reservation_id"]) != 32
            or not isinstance(meta.get("request_fp"), str) or len(meta["request_fp"]) != 64
            or 'input_binding' in meta and (not isinstance(meta['input_binding'], str)
                or re.fullmatch(r'[0-9a-f]{64}', meta['input_binding']) is None)
            or meta.get('logical_request_fp') is not None and (not isinstance(meta['logical_request_fp'], str)
                or re.fullmatch(r'[0-9a-f]{64}', meta['logical_request_fp']) is None)
            or meta.get('logical_unit_fp') is not None and (not isinstance(meta['logical_unit_fp'], str)
                or re.fullmatch(r'[0-9a-f]{64}', meta['logical_unit_fp']) is None)
            or meta.get('logical_target_id') is not None and (type(meta['logical_target_id']) is not int
                or meta['logical_target_id'] <= 0 or meta['logical_target_id'] != row['message_id'])
            or row["model"] != model
            or meta.get("route") not in ROUTES
            or not definition["legacy_reservation"] and "input_bound" not in meta
            or type(meta.get("project_id")) is not int or meta["project_id"] <= 0
            or meta["project_id"] != row["project_id"]
            or meta.get("message_id") != row["message_id"]
            or not isinstance(meta.get("unit_key"), str) or not meta["unit_key"]):
        raise AnthropicError("pricing_unverified", "reservation_invalid")
    tokens = meta.get("max_tokens")
    bound = meta.get("input_bound", INPUT_RESERVATION_BOUND)   # rows before the request-sized bound
    maximum = _decimal(meta.get("max_cost_usd"))
    if (type(tokens) is not int or not 1 <= tokens <= definition["max_tokens"]
            or type(bound) is not int or not 1 <= bound <= INPUT_RESERVATION_BOUND
            or maximum != _reservation_cost(bound, tokens, legacy="input_bound" not in meta,
                                            model=model, price_version=price_version)):
        raise AnthropicError("pricing_unverified", "reservation_bound_invalid")
    return maximum


def _stored_reservation(ledger, reservation, *, bound_only=False):
    """The one retained reservation row matching ``reservation``, validated."""
    rid = reservation.get("reservation_id")
    rows = (_matching_bound_rows(ledger, KINDS[0], rid) if bound_only
            else _matching_rows(ledger, KINDS[0], rid))
    if len(rows) != 1:
        raise AnthropicError("pricing_unverified", "reservation_missing")
    if bound_only and rows[0]["created_at"] < time.time() - 62 * 86400:
        raise AnthropicError("pricing_unverified", "reservation_missing")
    stored = _accounting_meta(rows[0]["meta"])
    _reserve_valid(stored, rows[0])
    if stored != reservation:
        raise AnthropicError("pricing_unverified", "reservation_mismatch")
    return stored


def _matching_rows_many(ledger, kind, reservation_ids, *, now=None, _timestamps_checked=False):
    """Select retained reservation identities in SQL, preserving every duplicate match."""
    if not _timestamps_checked:
        with _accounting_snapshot(ledger.db):
            _validate_accounting_timestamps(ledger.db)
            return _matching_rows_many(ledger, kind, reservation_ids, now=now, _timestamps_checked=True)
    ids = list(dict.fromkeys(reservation_ids))
    cutoff = (time.time() if now is None else now) - 62 * 86400
    rows = _indexed_rows(ledger.db, (kind,), ids, columns=_ROW_COLUMNS,
                         where=" AND created_at>=?", args=(cutoff,))
    if rows is not None:
        return rows
    rows = []
    if ids and ledger.db.execute(
            "SELECT 1 FROM artifacts WHERE kind=? AND created_at>=? AND "
            "CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
            "WHEN json_type(meta)!='object' THEN 1 "
            "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
            "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
            "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' "
            "AND abs(value)>1.7976931348623157e308) THEN 1 ELSE 0 END LIMIT 1",
            (kind, cutoff)).fetchone():
        raise AnthropicError("pricing_unverified", "usage_invalid")
    for offset in range(0, len(ids), 900):
        group = ids[offset:offset + 900]
        rows.extend(ledger.db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
            "WHERE kind=? AND created_at>=? AND CASE WHEN json_valid(meta) "
            "THEN json_extract(meta,'$.reservation_id') END IN ("
            + ','.join('?' for _ in group) + ') ORDER BY artifact_id',
            (kind, cutoff, *group)))
    return rows


def _matching_rows(ledger, kind, reservation_id):
    return _matching_rows_many(ledger, kind, [reservation_id])


def _matching_bound_rows(ledger, kind, reservation_id):
    """Inspect all retained rows naming this reservation, without unrelated accounting rows."""
    if not isinstance(reservation_id, str) or len(reservation_id) != 32:
        raise AnthropicError("pricing_unverified", "reservation_invalid")
    with _accounting_snapshot(ledger.db):
        rows = _indexed_rows(ledger.db, (kind,), [reservation_id], columns=_ROW_COLUMNS, where=_TEXT_ID)
        if rows is None:
            rows = ledger.db.execute(
                "SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
                "WHERE kind=? AND CASE WHEN json_valid(meta) THEN EXISTS "
                "(SELECT 1 FROM json_each(meta) WHERE key='reservation_id' AND type='text' AND value=?) "
                "WHEN typeof(meta)='text' THEN instr(meta,?)>0 "
                "WHEN typeof(meta)='blob' THEN instr(meta,CAST(? AS BLOB))>0 ELSE 0 END ORDER BY artifact_id",
                (kind, reservation_id, reservation_id, reservation_id)).fetchall()
    for row in rows:
        if not isinstance(row["meta"], str) or len(row["meta"]) > 32768:
            raise AnthropicError("pricing_unverified", "usage_invalid")
        _accounting_meta(row["meta"])
        _periods(row["created_at"])
    return rows


def reservation_evidence(db, artifact_id):
    """Validate a retained reservation's original successful result by exact reference."""
    try:
        return _reservation_evidence(db, artifact_id)
    except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("cloud_receipt_invalid") from None


def _receipt_meta(raw):
    if not isinstance(raw, str) or len(raw) > 32768:
        raise ValueError('cloud_receipt_invalid')
    try:
        value = strict_json(raw)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError('cloud_receipt_invalid') from None
    if not isinstance(value, dict):
        raise ValueError('cloud_receipt_invalid')
    return value


def _reservation_evidence(db, artifact_id):
    return reservation_evidence_batch(db, [artifact_id])[artifact_id]


def reservation_evidence_batch(db, artifact_ids):
    # All financial rows are checked in one borrowed snapshot. No proof is
    # cached outside this call, so later changes must be checked again.
    try:
        ids = list(artifact_ids)
        if any(type(item) is not int or not 0 < item < 2**63 for item in ids):
            raise ValueError("cloud_receipt_invalid")
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        with closing(db.execute('SELECT artifact_id FROM (SELECT artifact_id FROM artifacts LIMIT 1) '
                                'UNION ALL SELECT NULL UNION ALL SELECT NULL')) as anchor:
            anchor.fetchone()
            return _reservation_evidence_batch_snapshot(db, ids)
    except (AnthropicError, InvalidOperation, ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("cloud_receipt_invalid") from None


def _reservation_evidence_batch_snapshot(db, ids):
    before = (db.execute('PRAGMA data_version').fetchone()[0], db.total_changes)
    fields = ('artifact_id', 'kind', 'project_id', 'message_id', 'model', 'meta', 'created_at', 'content')
    originals = {}
    for row in db.execute(
            "SELECT artifact_id,kind,project_id,message_id,model,"
            "CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END,created_at,content "
            "FROM artifacts WHERE kind='cloud_usage_reserve' "
            "AND artifact_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),)):
        original = dict(zip(fields, row))
        meta = _receipt_meta(original['meta'])
        originals[original['artifact_id']] = (meta, _reserve_valid(meta, original))
    if len(originals) != len(ids):
        raise ValueError("cloud_receipt_missing")
    kinds = (KINDS[0], KINDS[1], 'cloud_usage_disposition')
    # A corrupt row may also have lost its patient/UUID.  It cannot be proved
    # unrelated to this source's disposition, so new adoption evidence holds.
    wanted = {meta['reservation_id'] for meta, _ in originals.values()}
    rows = _indexed_rows(db, kinds, sorted(wanted), where=_TEXT_ID, columns=(
        "artifact_id,kind,project_id,message_id,model,"
        "CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END AS meta,created_at,content"))
    if rows is None:
        bad = db.execute("SELECT CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END "
                      "FROM artifacts WHERE kind IN (?,?,?) AND CASE "
                      "WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
                      "WHEN json_type(meta)!='object' THEN 1 "
                      "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
                      "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
                      "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 "
                      "ELSE 0 END LIMIT 1", kinds).fetchone()
        if bad is not None:
            _receipt_meta(bad[0])
            raise ValueError("cloud_receipt_invalid")
        rows = db.execute("SELECT artifact_id,kind,project_id,message_id,model,"
            "CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END AS meta,created_at,content FROM artifacts "
            "WHERE kind IN (?,?,?) AND CASE WHEN json_valid(meta) THEN EXISTS "
            "(SELECT 1 FROM json_each(meta) m WHERE m.key='reservation_id' AND m.type='text' "
            "AND m.value IN (SELECT value FROM json_each(?))) END ORDER BY artifact_id",
            (*kinds, json.dumps(sorted(wanted))))
    rows_by_reservation = {rid: [] for rid in wanted}
    for row in rows:
        meta = _receipt_meta(row['meta'])
        rows_by_reservation[meta['reservation_id']].append((row, meta))
    result = {}
    for artifact_id in ids:
        meta, maximum = originals[artifact_id]
        matched = rows_by_reservation[meta['reservation_id']]
        reservations = [row for row, _ in matched if row['kind'] == KINDS[0]]
        results = [(row, value) for row, value in matched if row['kind'] == KINDS[1]]
        if (len(reservations) != 1 or reservations[0]['artifact_id'] != artifact_id or len(results) != 1
                or any(row['kind'] == 'cloud_usage_disposition' for row, _ in matched)):
            raise ValueError("cloud_receipt_unresolved")
        result_row, result_meta = results[0]
        _, settled = _settled(meta, maximum, [(result_meta, result_row)])
        if not settled or result_meta.get('http_class') != 'http_200' or result_meta.get('error_kind') or result_meta.get('disposition'):
            raise ValueError("cloud_receipt_unresolved")
        if "answer_text_sha256_v1" in result_meta:
            answer_hash = result_meta["answer_text_sha256_v1"]
            if not isinstance(answer_hash, str) or re.fullmatch(r"[0-9a-f]{64}", answer_hash) is None:
                raise ValueError("cloud_answer_hash_invalid")
        if ("answer_finish_reason_v1" in result_meta
                and result_meta["answer_finish_reason_v1"] not in ("stop", "length")):
            raise ValueError("cloud_answer_finish_reason_invalid")
        result[artifact_id] = {'reservation': meta, 'result': result_meta,
                               'witness': payload_hash([tuple(row) for row, _ in matched])}
    if (db.execute('PRAGMA data_version').fetchone()[0], db.total_changes) != before:
        raise ValueError("cloud_receipt_changed")
    return result


def _is_openai_model(model):
    return (isinstance(model, str)
            and any(key[0] == model and definition["provider"] == "openai"
                    for key, definition in PRICE_DEFINITIONS.items()))


def _confirmed_openai_quota(result, model):
    code = result.get("openai_error_code")
    return (isinstance(code, str) and code in openai_client.QUOTA_ERROR_CODES
            and _is_openai_model(model) and "openai_429_diagnostic" not in result
            and result.get("http_class") == "http_429"
            and result.get("error_kind") == "payment_required"
            and "usage" in result and result["usage"] is None)


def _confirmed_openai_rate_diagnostic(result, model):
    diagnostic = result.get("openai_429_diagnostic")
    return (isinstance(diagnostic, str) and diagnostic in openai_client.RATE_LIMIT_DIAGNOSTICS
            and _is_openai_model(model) and "openai_error_code" not in result
            and result.get("http_class") == "http_429"
            and result.get("error_kind") == "rate_limited"
            and "usage" in result and result["usage"] is None)


def _settled(m, maximum, matching):
    """(cost, settled?) of one reservation's single bound result; raises when
    the result is duplicated, unbound or its usage/cost disagrees."""
    if len(matching) != 1:
        raise ValueError
    r, row = matching[0]
    if (_binding(m) != _binding(r) or row["project_id"] != m["project_id"]
            or row["message_id"] != m["message_id"] or row["model"] != m["model"]):
        raise ValueError
    if "openai_error_code" in r and not _confirmed_openai_quota(r, m["model"]):
        raise ValueError
    if "openai_429_diagnostic" in r and not _confirmed_openai_rate_diagnostic(r, m["model"]):
        raise ValueError
    reported = _decimal(r.get("cost_usd"))
    if r.get("usage") is None:
        if reported != (Decimal(0) if _unbilled(r.get("http_class")) else maximum):
            raise ValueError
        return reported, _unbilled(r.get("http_class"))
    if (reported != usage_cost(r["usage"], model=m["model"], price_version=m["price_version"]) or reported > maximum
            or r["usage"].get("output_tokens", 0) > m["max_tokens"]):
        raise ValueError
    return reported, True
