"""医薬品辞書の候補照会・参考検索・更新差分・更新影響を読取り専用で確認する。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401
import drug_map  # noqa: E402
from mcs_util import HOME, load_config  # noqa: E402

SNAPSHOT = Path(HOME) / "data" / "snapshots" / "ledger-snapshot.db"
IMPACT_LABELS = {"unchanged": "変化なし", "newly_matched": "新たに候補あり",
                 "lost_match": "候補なしに変化", "became_ambiguous": "複数候補に変化",
                 "disambiguated": "単一候補に変化", "candidates_changed": "候補が変化"}
STATUS_LABELS = {"resolved": "単一候補", "ambiguous": "複数候補", "generic": "総称",
                 "unresolved": "候補なし"}


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit は0〜200の整数です") from None
    if not 0 <= number <= 200:
        raise argparse.ArgumentTypeError("limit は0〜200の整数です")
    return number


def _dictionary(path, sha256):
    if path is None and sha256 is None:
        setting = load_config().get("drug_map")
        if drug_map.config_error(setting) is not None:
            raise ValueError("dictionary_unconfigured")
        path, sha256 = setting["path"], setting["sha256"]
    if not isinstance(path, str) or not isinstance(sha256, str):
        raise ValueError("dictionary_path_and_sha256_required")
    value = drug_map.load(path, expected_sha256=sha256)
    if value is None:
        raise ValueError("dictionary_unavailable")
    return value


def _line(value) -> str:
    return "".join(c if c.isprintable() else " " for c in str(value))


def _display(report, command):
    print(_line(report["explanation"]))
    if command == "impact":
        before, after = report["before"], report["after"]
        print(f"辞書: {_line(before['id'])}@{before['sha256'][:12]} → "
              f"{_line(after['id'])}@{after['sha256'][:12]}")
        messages = report["messages"]
        print(f"snapshot: {_line(report['snapshot']['generation_id'])} / "
              f"評価 {messages['evaluated']}件・未抽出 {messages['unevaluated_no_extraction']}件"
              f"・形式不正 {messages['unevaluated_malformed']}件 / 薬剤名 {report['mentions']}件")
        for key, label in IMPACT_LABELS.items():
            print(f"{label}: {report['counts'][key]}件（{report['names'][key]}名称）")
        for row in report["examples"]:
            print(f"- {_line(row['name'])}: {IMPACT_LABELS[row['transition']]} {row['mentions']}件"
                  f"（{STATUS_LABELS[row['before']['status']]}{row['before']['candidate_count']}"
                  f"→{STATUS_LABELS[row['after']['status']]}{row['after']['candidate_count']}）")
        if report["truncated"]["examples"]:
            print("表示上限に達しました。件数は全件の集計です。")
        return
    if command == "compare":
        print("変更件数: " + ", ".join(f"{k}={v}" for k, v in report["counts"].items()))
        for row in report["changes"]:
            item = row.get("after") or row.get("before") or {}
            print(f"{row['change']}: {_line(row['id'])} / {_line(item.get('display', ''))}")
        for row in report["collisions"]:
            print(f"別名の複数候補: {_line(row['alias'])} ({row['after_count']}件)")
        if any(report["truncated"].values()):
            print("表示上限に達しました。変更件数は全件の集計です。")
        return
    dictionary = report["dictionary"]
    print(f"辞書: {_line(dictionary['id'])}@{dictionary['sha256'][:12]}"
          + ("（承認済み辞書・薬剤候補）" if dictionary["approved"] else "（未承認辞書・参考のみ）"))
    source = dictionary["source"]
    print(f"出所: {_line(source['name'])} / {_line(source['url'])}")
    rows = report["cands"] if command == "lookup" else report["items"]
    count = report["candidate_count"] if command == "lookup" else report["total"]
    print(f"候補: {count}件")
    labels = {"ingredient": "成分候補", "class": "総称",
              "general_name": "一般名処方候補", "product": "製品候補"}
    for row in rows:
        print(f"- {labels[row['kind']]}: {_line(row['display'])}"
              f" / {_line(row.get('code', row.get('id')))}")
    if report["truncated"]:
        print("表示上限に達しました。候補件数は全件の集計です。")


class SnapshotError(Exception):
    pass


def _impact(path, before, after, limit):
    """Read the published snapshot only; the live ledger is never opened."""
    try:
        import mcs_view
        view = mcs_view.View(path)
    except (ValueError, sqlite3.Error, OSError) as error:
        raise SnapshotError from error
    try:
        report = drug_map.impact(view.db, before, after, limit=limit)
    except sqlite3.Error as error:
        raise SnapshotError from error
    finally:
        view.close()
    report["snapshot"] = {"generation_id": view.meta["generation_id"]}
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("lookup", "search"):
        command = sub.add_parser(name)
        command.add_argument("name")
        command.add_argument("--dictionary", help="私有辞書の絶対パス（省略時は既存設定）")
        command.add_argument("--sha256", help="照合する辞書のSHA256")
        command.add_argument("--limit", type=_limit, default=20)
        command.add_argument("--json", action="store_true")
    for name in ("compare", "impact"):
        command = sub.add_parser(name)
        for side in ("before", "after"):
            command.add_argument("--" + side, required=True)
            command.add_argument("--" + side + "-sha256", required=True)
        if name == "impact":
            command.add_argument("--snapshot", type=Path, default=SNAPSHOT,
                                 help="公開スナップショット（省略時は既定の公開先）")
        command.add_argument("--limit", type=_limit, default=50)
        command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command in ("compare", "impact"):
            before = _dictionary(args.before, args.before_sha256)
            after = _dictionary(args.after, args.after_sha256)
            report = (_impact(args.snapshot, before, after, args.limit)
                      if args.command == "impact" else before.diff(after, limit=args.limit))
        else:
            dictionary = _dictionary(args.dictionary, args.sha256)
            if args.command == "search":
                report = dictionary.search(args.name, limit=args.limit)
            else:
                report = dictionary.lookup(args.name)
                report["candidate_count"] = len(report["cands"])
                report["alias_count"] = len(report["matched_aliases"])
                report["truncated"] = (report["candidate_count"] > args.limit
                                       or report["alias_count"] > args.limit)
                report["cands"] = report["cands"][:args.limit]
                report["matched_aliases"] = report["matched_aliases"][:args.limit]
        report["read_only"] = True
        report["candidate_only"] = True
    except SnapshotError:
        print("drug: 公開スナップショットを読めません。snapshotの場所と公開状態を確認してください。",
              file=sys.stderr)
        return 1
    except (ValueError, TypeError, OSError):
        print("drug: 辞書の設定・所有者権限・SHA256を確認してください。", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _display(report, args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
