"""Show the Jev model in use and switch it by a human-confirmed, probe-gated, receipted command."""
from __future__ import annotations

import json
import uuid

import mcs_util

CMD = "ops.jev_model_set"
DEFAULT_ALIAS = "default"
_PROBE_COUNTS = ("tp", "fp", "fn", "tn", "unevaluated", "requests")


def _target(text):
    """The model a ``--to`` value names, or None when it is not a choosable fixed version."""
    import provider_settings
    import semantic_jev as jev
    model = jev.JEV_MODEL if text == DEFAULT_ALIAS else text
    if model in provider_settings.JEV_RETIRED_PINS or jev.resolve_model(model) != model:
        return None
    return model


def _probe_ok(probe) -> bool:
    return (isinstance(probe, dict) and set(probe) == set(_PROBE_COUNTS)
            and all(type(probe[k]) is int and probe[k] >= 0 for k in _PROBE_COUNTS)
            and probe["fp"] == probe["fn"] == probe["unevaluated"] == 0 and probe["tp"] + probe["tn"] > 0)


def validate_operation(req, base) -> str | None:
    """Operation rules for ops.jev_model_set after the shared human-confirmed envelope."""
    import semantic_jev as jev
    if req.get("project_id") is not None:
        return "bad_project_id"
    if req.keys() - (base | {"reason", "model", "expected_model", "probe"}):
        return "unknown_field"
    if not mcs_util.valid_text(req.get("reason"), 2000):
        return "bad_reason"
    expected = req.get("expected_model")
    if _target(req.get("model")) != req.get("model") or expected is None or jev.resolve_model(expected) != expected:
        return "bad_model"
    if req["model"] == req["expected_model"]:
        return "jev_model_unchanged"
    # The code default needs no probe; any other version only after its synthetic probe passed.
    if req["model"] != jev.JEV_MODEL and not _probe_ok(req.get("probe")):
        return "jev_model_probe_required"
    if req["model"] == jev.JEV_MODEL and req.get("probe") is not None and not _probe_ok(req["probe"]):
        return "bad_probe"
    return None


def _current_model(cfg):
    import semantic_policy
    scfg, errors = semantic_policy.runtime_config(cfg, root=str(mcs_util.HOME))
    return None if errors else scfg["model"]


def apply_tx(db, req, filesystem_changes) -> tuple[str | None, dict]:
    """Save semantic.model inside the receipt transaction; a failed COMMIT puts the old value back."""
    import config_edit
    import semantic_jev as jev
    if filesystem_changes is None:
        raise RuntimeError("jev_model_transaction_required")
    try:
        cfg = config_edit.read_strict()
    except (OSError, ValueError, RecursionError, mcs_util.ProviderSettingsUnreadable):
        return "config_unreadable", {}
    if _current_model(cfg) != req["expected_model"]:
        return "jev_model_changed", {}
    block = cfg.get("semantic", {})
    previous = block.get("model") if isinstance(block, dict) else None
    try:
        saved, errors = config_edit.apply({"semantic.model": None if req["model"] == jev.JEV_MODEL else req["model"]},
                                          expected=cfg)
    except config_edit.ConfigConflict:
        return "config_busy", {}
    except (OSError, ValueError, RecursionError, mcs_util.ProviderSettingsUnreadable):
        return "config_unreadable", {}
    if errors:
        return "jev_model_config_invalid", {}

    def restore_on_failure(exc_type, _exc, _tb):
        if exc_type is not None:
            config_edit.apply({"semantic.model": previous}, expected=saved)
        return False
    filesystem_changes.push(restore_on_failure)
    return None, {"cmd": CMD, "reason": req["reason"],
                  "jev_model": {"before": req["expected_model"], "after": req["model"],
                                "probe": req.get("probe")}}


def _probe(model):
    """Run the bench's synthetic detection probe against ``model`` under the shared daily budget."""
    import semantic_bench
    import semantic_jev as jev
    key = jev.api_key(mcs_util.HOME)
    if not key:
        return None, "no_api_key"
    try:
        with jev.live_client(key, operation="jev_model_probe", root=mcs_util.HOME,
                             model=model, job_budget=120) as client:
            result = semantic_bench.detection_probe(client)
    except jev.JevError as error:
        return None, error.kind
    return result, None


def _status(cfg) -> int:
    import jev_model_updates
    import semantic_jev as jev
    current = _current_model(cfg)
    state = jev_model_updates.read_state(mcs_util.HOME)
    print(json.dumps({"model": current, "default": jev.JEV_MODEL,
                      "newer": jev_model_updates.newer_models(state["models"], current) if state and current else [],
                      "checked_at": state["checked_at"] if state else None,
                      "check_error": state["error"] if state else None}, ensure_ascii=False))
    return 0 if current else 1


def command(args) -> int:
    import config_edit
    import mcs_requests
    import semantic_jev as jev
    try:
        cfg = config_edit.read_strict()
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError, RecursionError, mcs_util.ProviderSettingsUnreadable):
        print("Jevのモデル: 設定ファイルを読み取れないため変更していません。mcs_setup.py check で確認してください")
        return 1
    if args.to is None:
        return _status(cfg)
    if args.confirm_human is not True or not mcs_util.valid_text(args.reason, 2000):
        print("Jevのモデル: --confirm-human と --reason による人の確認が必要です")
        return 1
    target = _target(args.to)
    if target is None:
        print("Jevのモデル: 指定できるのは固定版のID（例: jev-1.14.0）か default です。"
              "以前の版が固定していたIDや latest などの別名は指定できません")
        return 1
    current = _current_model(cfg)
    if current is None:
        print("Jevのモデル: 意味解析の設定に誤りがあるため変更していません。mcs_setup.py check で確認してください")
        return 1
    if target == current:
        print(f"Jevのモデル: すでに {current} を使用しています。変更はありません")
        return 0
    probe = None
    if target != jev.JEV_MODEL:
        result, error = _probe(target)
        if result is None:
            print(f"Jevのモデル: {target} の確認を実行できなかったため変更していません（{error}）")
            return 2
        if result["verdict"] != "pass":
            why = ("合成データの判定が期待と一致しませんでした" if result["verdict"] == "fail"
                   else "Jevが一部の合成データに回答しなかったため評価できませんでした")
            print(f"Jevのモデル: {target} の確認に合格しなかったため変更していません（{why}: "
                  f"TP={result['tp']} FP={result['fp']} FN={result['fn']} TN={result['tn']} "
                  f"未評価={result['unevaluated']}）")
            return 1 if result["verdict"] == "fail" else 2
        probe = {key: result[key] for key in _PROBE_COUNTS}
    req = {"version": 1, "cmd": CMD, "command_id": args.command_id or str(uuid.uuid4()),
           "actor": args.actor, "human_confirmed": True, "project_id": None, "reason": args.reason,
           "model": target, "expected_model": current, "probe": probe}
    error = mcs_requests.validate(req)
    if error:
        print("Jevのモデル: 指定を確認してください（" + error + "）")
        return 1
    try:
        receipt = mcs_requests.apply_local_operation(req)
    except (OSError, ValueError) as exc:
        print("Jevのモデル: 変更できませんでした（" + mcs_util.safe_error_code(exc) + "）")
        return 1
    if receipt is None:
        print("Jevのモデル: ほかの保存処理が終わってから再試行してください（変更していません）")
        return 3
    if receipt["outcome"] != "applied":
        print("Jevのモデル: 変更していません（" + str(receipt.get("error")) + "）")
        return 1
    print(f"Jevのモデルを {current} から {target} に切り替えました（承認記録 {receipt['command_id']}）。"
          "次の定期処理から新しいモデルを使い、完了済みの意味解析は新しいモデルで順にやり直されます。"
          f"元に戻す場合は --to {current if current != jev.JEV_MODEL else DEFAULT_ALIAS} を指定します")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("jev-model", help="Jevのモデルを表示し、合成データの確認に合格したときだけ人の確認で切り替えます")
    p.add_argument("--to", help="切り替え先の固定版ID（例: jev-1.15.0）。default でコードの既定へ戻します")
    p.add_argument("--confirm-human", action="store_true")
    p.add_argument("--reason")
    p.add_argument("--actor", default="local-human")
    p.add_argument("--command-id", help="同じ操作を再試行する場合の承認記録UUID")
    p.set_defaults(fn=command)
