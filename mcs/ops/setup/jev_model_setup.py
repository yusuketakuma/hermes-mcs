"""Show the Jev model in use and switch it by a human-confirmed, probe-gated, receipted command."""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
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


def _scfg(cfg):
    import semantic_policy
    scfg, errors = semantic_policy.runtime_config(cfg, root=str(mcs_util.HOME))
    return None if errors else scfg


def _current_model(cfg):
    scfg = _scfg(cfg)
    return None if scfg is None else scfg["model"]


def _stored(model):
    import semantic_jev as jev
    return None if model == jev.JEV_MODEL else model


def _publish(before, after, command_id) -> None:
    """Save ``after`` only over a config still naming ``before``; an already saved ``after`` is done."""
    import config_edit
    cfg = config_edit.read_strict()
    current = _current_model(cfg)
    if current == after:
        return
    if current != before:
        raise ValueError("jev_model_changed")
    _, errors = config_edit.apply({"semantic.model": _stored(after)}, expected=cfg,
                                  origin="jev_model", command_id=command_id)
    if errors:
        raise ValueError("jev_model_config_invalid")


@contextmanager
def _published_after_commit(before, after, command_id):
    """Publish only once the receipt has committed; a failure leaves a receipt a rerun finishes."""
    yield
    _publish(before, after, command_id)


def _newest(db):
    row = db.execute("SELECT receipt_json FROM command_receipts WHERE outcome='applied' "
                     "AND json_extract(receipt_json,'$.cmd')=? ORDER BY rowid DESC LIMIT 1", (CMD,)).fetchone()
    return json.loads(row[0]) if row is not None else None


def apply_tx(db, req, filesystem_changes) -> tuple[str | None, dict]:
    """Commit the receipt with its precondition; the config follows the COMMIT (external_transfer's order)."""
    import config_edit
    from mcs_config import validate_config
    if filesystem_changes is None:
        raise RuntimeError("jev_model_transaction_required")
    try:
        cfg = config_edit.read_strict()
    except (OSError, ValueError, RecursionError, mcs_util.ProviderSettingsUnreadable):
        return "config_unreadable", {}
    scfg = _scfg(cfg)
    if scfg is None or scfg["model"] != req["expected_model"]:
        return "jev_model_changed", {}
    if scfg["fact_source"] == "canonical":
        return "jev_model_canonical_approval", {}
    # Refuse now what could not be published after the COMMIT.
    trial = json.loads(json.dumps(cfg))
    block = dict(trial.get("semantic") or {})
    if _stored(req["model"]) is None:
        block.pop("model", None)
    else:
        block["model"] = req["model"]
    trial["semantic"] = block
    if validate_config(trial)[0]:
        return "jev_model_config_invalid", {}
    filesystem_changes.enter_context(_published_after_commit(req["expected_model"], req["model"],
                                                                 req["command_id"]))
    return None, {"cmd": CMD, "reason": req["reason"],
                  "jev_model": {"before": req["expected_model"], "after": req["model"],
                                "probe": req.get("probe")}}


def replay_tx(db, receipt, filesystem_changes) -> None:
    """Finish the config of the newest applied switch that never reached it; never undo a later one."""
    db = getattr(db, "db", db)
    if receipt.get("outcome") != "applied" or receipt.get("cmd") != CMD:
        return
    newest = _newest(db)
    if newest is None or newest.get("command_id") != receipt.get("command_id"):
        return
    change = receipt["jev_model"]
    filesystem_changes.enter_context(_published_after_commit(change["before"], change["after"],
                                                                 receipt["command_id"]))


def receipt_state(receipt) -> str:
    """Whether an applied switch receipt is the model the config names now."""
    import config_edit
    try:
        current = _current_model(config_edit.read_strict())
    except (OSError, ValueError, RecursionError, mcs_util.ProviderSettingsUnreadable):
        return "unverified"
    change = receipt["jev_model"]
    if current == change["after"]:
        return "published"
    if current != change["before"]:
        return "superseded"
    # current still names ``before``: unpublished only while this receipt is the
    # newest applied switch. A later switch (e.g. back to the default) means a
    # rerun would publish a superseded model, so it is not offered as pending.
    try:
        from contextlib import closing
        import locked_command
        from ledger import LedgerReader
        path = locked_command.ledger_path()
        newest = None
        if path.is_file():
            with closing(LedgerReader(str(path))) as reader:
                newest = _newest(reader.db)
    except (OSError, ValueError, RecursionError, sqlite3.Error):
        newest = None
    if newest is not None and newest.get("command_id") != receipt.get("command_id"):
        return "superseded"
    return "unpublished"


def _stored_receipt(command_id):
    """The receipt already kept for ``command_id``, read without a writer, or None."""
    from contextlib import closing
    import locked_command
    from ledger import LedgerReader
    path = locked_command.ledger_path()
    if not path.is_file():
        return None
    with closing(LedgerReader(str(path))) as reader:
        row = reader.db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                                (command_id,)).fetchone()
    return json.loads(row[0]) if row is not None else None


def _replay_request(receipt):
    change = receipt["jev_model"]
    return {"version": 1, "cmd": CMD, "command_id": receipt["command_id"], "actor": receipt["actor"],
            "human_confirmed": True, "project_id": None, "reason": receipt["reason"],
            "model": change["after"], "expected_model": change["before"], "probe": change["probe"]}


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


def _lookup(command_id):
    try:
        return _stored_receipt(command_id)
    except (OSError, ValueError, RecursionError, sqlite3.Error):
        return None


def _submit(req) -> int:
    """Commit the receipt, then the config; report a committed receipt whose config is unconfirmed."""
    import mcs_requests
    import semantic_jev as jev
    retry = f"`--to {req['model'] if req['model'] != jev.JEV_MODEL else DEFAULT_ALIAS} --command-id {req['command_id']}`"
    try:
        receipt = mcs_requests.apply_local_operation(req)
    except (OSError, ValueError, sqlite3.Error) as exc:
        kept = _lookup(req["command_id"])
        if kept is not None and kept.get("outcome") == "applied":
            print(f"Jevのモデル: 承認記録（{req['command_id']}）は保存しましたが、設定への反映を確認できません"
                  f"（{mcs_util.safe_error_code(exc)}）。同じ指定に {retry} を付けて再実行すると反映を確認・完了します")
            return 1
        print("Jevのモデル: 変更できませんでした（" + mcs_util.safe_error_code(exc) + "）")
        return 1
    if receipt is None:
        print("Jevのモデル: ほかの保存処理が終わってから再試行してください（変更していません）")
        return 3
    if receipt.get("outcome") != "applied":
        print("Jevのモデル: 変更していません（" + str(receipt.get("error")) + "）")
        return 1
    state = receipt_state(receipt)
    before, after = receipt["jev_model"]["before"], receipt["jev_model"]["after"]
    if state == "superseded":
        print(f"Jevのモデル: 承認記録（{receipt['command_id']}）の後に別の変更があったため、その変更を保持しています")
        return 0
    if state != "published":
        print(f"Jevのモデル: 承認記録（{receipt['command_id']}）は保存済みですが、設定への反映を確認できません。"
              f"同じ指定に {retry} を付けて再実行してください")
        return 1
    print(f"Jevのモデルを {before} から {after} に切り替えました（承認記録 {receipt['command_id']}）。"
          "次の定期処理から新しいモデルを使い、完了済みの意味解析は新しいモデルで順にやり直されます。"
          f"元に戻す場合は --to {before if before != jev.JEV_MODEL else DEFAULT_ALIAS} を指定します")
    return 0


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
    if args.command_id:
        kept = _lookup(args.command_id)
        if kept is not None:
            if kept.get("cmd") != CMD or kept.get("outcome") != "applied" or kept["jev_model"]["after"] != target:
                print("Jevのモデル: この承認記録UUIDは別の操作か、適用されなかった操作の記録です（変更していません）")
                return 1
            # The same request again: its replay publishes a config that never got it.
            return _submit(_replay_request(kept))
    scfg = _scfg(cfg)
    if scfg is None:
        print("Jevのモデル: 意味解析の設定に誤りがあるため変更していません。mcs_setup.py check で確認してください")
        return 1
    current = scfg["model"]
    if target == current:
        print(f"Jevのモデル: すでに {current} を使用しています。変更はありません")
        return 0
    if scfg["fact_source"] == "canonical":
        print("Jevのモデル: 解析結果の採用方法が canonical（検証済み結果）のため変更していません。"
              "canonical の承認は使用中のモデルでの評価に結び付いています。切り替える場合は先に "
              "`mcs_setup.py fact-source shadow` に戻し、モデルを切り替えてから新しいモデルで評価をやり直し、"
              "合格した評価証拠で canonical を承認し直してください")
        return 1
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
    return _submit(req)


def add_parser(sub) -> None:
    p = sub.add_parser("jev-model", help="Jevのモデルを表示し、合成データの確認に合格したときだけ人の確認で切り替えます")
    p.add_argument("--to", help="切り替え先の固定版ID（例: jev-1.15.0）。default でコードの既定へ戻します")
    p.add_argument("--confirm-human", action="store_true")
    p.add_argument("--reason")
    p.add_argument("--actor", default="local-human")
    p.add_argument("--command-id", help="同じ操作を再試行する場合の承認記録UUID")
    p.set_defaults(fn=command)
