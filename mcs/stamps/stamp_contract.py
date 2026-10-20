"""Define MCS stamp tokens, aggregate validation and display vocabulary without I/O."""
import re

from mcs_util import positive, valid_text

# Known display kinds do not restrict forward-compatible read tokens.
STAMP_TYPES = {
    "viewed": ("見ました", "👀"), "accepted": ("承知", "🙆"),
    "thanked": ("感謝", "🙏"), "good": ("いいね", "👍"),
    "completed": ("完了", "✅"),
}
REACTION_LABELS = {kind: value[0] for kind, value in STAMP_TYPES.items()}
STAMP_EMOJI = {kind: value[1] for kind, value in STAMP_TYPES.items()}

_REACTION_KIND = re.compile(r"[a-zA-Z0-9_-]{1,64}")


def reaction_kind_ok(kind) -> bool:
    """A concrete reaction kind: a short token, never the 'all' aggregate."""
    return isinstance(kind, str) and bool(_REACTION_KIND.fullmatch(kind)) \
        and kind != "all"


def reaction_label(kind):
    """公式の既知種別を表示し、将来種別は値を埋め込まず未知と表示する。"""
    return REACTION_LABELS.get(kind, "未知")


def valid_stored_reactions(value) -> bool:
    return isinstance(value, list) and not any(
        not isinstance(r, dict) or not isinstance(r.get("type"), str)
        or not r["type"] or type(r.get("count")) is not int or r["count"] < 0
        or type(r.get("self_reacted")) is not bool for r in value)


def normalize_reactions(value) -> list[dict]:
    """Normalize wire aggregates; malformed fields fail independently of the post body."""
    if not isinstance(value, list):
        raise ValueError("reactions_invalid")
    normalized, seen = [], set()
    for reaction in value:
        if (not isinstance(reaction, dict)
                or not reaction_kind_ok(reaction.get("type"))
                or reaction["type"] in seen
                or type(reaction.get("count")) is not int
                or not 0 <= reaction["count"] <= 2**63 - 1
                or type(reaction.get("self_reacted")) is not bool
                or (reaction["self_reacted"] and reaction["count"] == 0)):
            raise ValueError
        seen.add(reaction["type"])
        normalized.append({key: reaction[key] for key in ("type", "count", "self_reacted")})
    return normalized


def config_error(value):
    if not isinstance(value, dict) or value.keys() - {"enabled", "account_user_id", "operator"}:
        return "bad_stamps_config"
    if "enabled" in value and type(value["enabled"]) is not bool:
        return "bad_stamps_enabled"
    if "account_user_id" in value and not positive(value["account_user_id"]):
        return "bad_stamps_account"
    operator = value.get("operator")
    if operator is not None and (not valid_text(operator, 120)
            or re.fullmatch(r"(?:discord:[^:\s]+|(?:slack|lineworks):[^:\s]+:[^:\s]+)", operator) is None):
        return "bad_stamps_operator"
    if value.get("enabled") is True and (not positive(value.get("account_user_id")) or not operator):
        return "stamps_identity_required"
    return None

def options(cfg) -> dict:
    value = cfg.get("stamps") if isinstance(cfg, dict) else None
    return dict(value) if isinstance(value, dict) and config_error(value) is None else {}
