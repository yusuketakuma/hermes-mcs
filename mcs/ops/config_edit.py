"""Validated, atomic edits of selected config.json keys from the command line."""
from __future__ import annotations

import json
from pathlib import Path

import mcs_util


def read_strict() -> dict:
    """The current config.json; raises instead of returning {} — writing {}
    back after an unreadable file would erase every other setting."""
    path = Path(mcs_util.CONF_PATH)
    if path.is_symlink() or path.stat().st_size > mcs_util.CONFIG_MAX_BYTES:
        raise ValueError("config_unreadable")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("config_unreadable")
    return cfg


def apply(changes: dict, prefix: str) -> tuple[dict | None, list[str]]:
    """Set (or, for None, remove) dotted keys and save 0600 when the runtime
    validator accepts them. Returns (saved config or None, validator errors
    whose key starts with any of ``prefix``)."""
    from mcs_config import validate_config
    import service_management
    cfg = read_strict()
    for dotted, value in changes.items():
        section, key = dotted.split(".", 1)
        block = dict(cfg.get(section) or {})
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
        cfg[section] = block
    errors, _ = validate_config(cfg)
    relevant = [e for e in errors if any(p in e for p in prefix.split("|"))]
    if relevant:     # never write a config the runtime validator rejects
        return None, relevant
    service_management.write_service_file(
        mcs_util.CONF_PATH, json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True) + "\n", 0o600)
    return cfg, []


def _on_off(text):
    value = {"on": True, "true": True, "off": False, "false": False}.get(text.strip().lower())
    if value is None:
        raise ValueError
    return value


def settings_command(args) -> int:
    """Show or set extract.prefilter / local_llm.disk_guard_mb / local_llm.admission."""
    try:
        changes = {}
        if args.prefilter is not None:
            changes["extract.prefilter"] = _on_off(args.prefilter)
        if args.admission is not None:
            changes["local_llm.admission"] = _on_off(args.admission)
        if args.disk_guard_mb is not None:
            guard = float(args.disk_guard_mb)
            if not guard >= 0 or guard == float("inf"):
                raise ValueError
            changes["local_llm.disk_guard_mb"] = int(guard) if guard.is_integer() else guard
    except ValueError:
        print("設定: --prefilter・--admission は on/off、--disk-guard-mb は0以上の数で指定してください")
        return 1
    try:
        if changes:
            _, errors = apply(changes, "extract.prefilter|local_llm")
            if errors:
                print("設定に誤りがあるため保存していません: " + "; ".join(errors))
                return 1
        else:
            read_strict()      # refuse to report defaults for an unreadable file
    except (OSError, ValueError, RecursionError):
        print("設定: 設定ファイルを読み取れないため変更していません（先に mcs_setup.py init で設定してください）")
        return 1
    import llm_availability
    import local_llm
    import extract_llm
    print(json.dumps({"prefilter": extract_llm._prefilter_enabled(),
                      "disk_guard_mb": llm_availability.disk_floor_mb(),
                      "admission": local_llm.admission_enabled(),
                      "saved": bool(changes)}, ensure_ascii=False))
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("settings", help="抽出の事前判定・ディスク下限・LLM受付制御を表示・設定します")
    p.add_argument("--prefilter", help="on/off: 挨拶だけの投稿などをLLMに送らない事前判定")
    p.add_argument("--disk-guard-mb", dest="disk_guard_mb", help="空きがこのMB未満ならローカルLLMを止める（0で無効）")
    p.add_argument("--admission", help="on/off: ローカルLLMの共有受付制御（既定off）")
    p.set_defaults(fn=settings_command)
