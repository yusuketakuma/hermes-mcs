"""医薬品辞書の状態・候補照会・参考検索・更新差分・更新影響を読取り専用で確認し、同梱マスターから私有辞書を新規作成する。"""
from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _mcs_path  # noqa: E402,F401
import drug_map  # noqa: E402
from mcs_util import HOME, load_config, loads_dict  # noqa: E402

SNAPSHOT = Path(HOME) / "data" / "snapshots" / "ledger-snapshot.db"
BUNDLE = Path(__file__).resolve().parents[2] / "resources" / "drug-master"
# R08rec1 pp16/18: medicine change values 0/3/5 stay usable (1/9 are deleted or
# abolished); 99999999 means not abolished and 0 means no deadline. These are
# applied only under the operator's --confirmed-by declaration.
BUNDLED_POLICY = {"candidate_change_values": ["0", "3", "5"],
                  "absent_date_values": ["0", "99999999"]}
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


def _bundle(directory: Path = BUNDLE):
    """The bundled manifest of the converter's pinned edition and its archive."""
    import import_drug_master as importer
    folder = (directory / importer.EDITION).resolve()
    manifest = loads_dict(importer._read(folder / "manifest.json", 65536))
    if (manifest is None or manifest.get("edition") != importer.EDITION
            or manifest.get("layout") != importer.LAYOUT
            or manifest.get("member") != f"y_{importer.EDITION}.csv"
            or manifest.get("archive") != f"y_{importer.EDITION}.zip"
            or any(not isinstance(manifest.get(key), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", manifest[key])
                   for key in ("archive_sha256", "csv_sha256"))
            or type(manifest.get("rows")) is not int
            or not 0 < manifest["rows"] <= importer.MAX_ROWS
            or not isinstance(manifest.get("terms"), dict)
            or not drug_map._text(manifest["terms"].get("url"))):
        raise ValueError("bundle_manifest")
    return manifest, folder / manifest["archive"]


def install_bundled(args, *, directory: Path = BUNDLE, home: str = HOME) -> dict:
    """Convert the bundled official master into a new private dictionary.

    Mechanical pin fields come from the bundled manifest; the operator's terms
    check and status-policy confirmation are required inputs, never inferred.
    Config, the ledger and the network are not touched."""
    import import_drug_master as importer
    manifest, archive = _bundle(directory)
    pin = {"schema": importer.PIN_SCHEMA, "layout": importer.LAYOUT,
           "edition": importer.EDITION, "sha256": manifest["archive_sha256"],
           "member": manifest["member"], "as_of": date.today().isoformat(),
           "terms_checked_on": args.terms_checked_on,
           "terms_record": args.terms_record or manifest["terms"]["url"],
           "status_policy": {**BUNDLED_POLICY, "confirmed_by": args.confirmed_by}}
    if args.approved_by is not None:
        pin["approved_by"] = args.approved_by
    result = importer.convert(archive, pin)
    report = result.report
    if report["csv_sha256"] != manifest["csv_sha256"] or report["rows"] != manifest["rows"]:
        raise ValueError("bundle_csv_integrity")
    out = {"edition": report["edition"], "rows": report["rows"], "entries": report["entries"],
           "reasons": report["reasons"], "approved": args.approved_by is not None,
           "held": report.get("held"), "path": None, "sha256": None, "state": "held",
           # the dictionary holds only rows usable on as_of; later starts/ends
           # need a rebuild on or after next_boundary_on
           "as_of": report.get("as_of"), "next_boundary_on": report.get("next_boundary_on"),
           "future_starts": report.get("future_starts") or {},
           "future_ends": report.get("future_ends") or {}}
    if result.payload is None:
        return out
    folder = Path(home).resolve() / "data" / "drug-map"
    if not folder.exists():
        folder.mkdir(mode=0o700, parents=True)
        os.chmod(folder, 0o700)   # umask-independent: write_private requires 0700
    sha = report["output_sha256"]
    path = folder / f"mhlw-y-{importer.EDITION}-{sha[:12]}.json"
    if path.exists() or path.is_symlink():
        if drug_map.load(path, expected_sha256=sha) is None:
            raise ValueError("dictionary_unavailable")
        out["state"] = "present"
    else:
        importer.write_private(result, path)
        out["state"] = "written"
    out.update(path=str(path), sha256=sha)
    return out


def _build_dates(source, today: date):
    """(as_of, next_boundary_on) as dates; as_of None when absent, unreadable
    or after today — such a dictionary is never reported as current."""
    try:
        as_of = date.fromisoformat(source.as_of) if source.as_of else None
        boundary = (date.fromisoformat(source.next_boundary_on)
                    if source.next_boundary_on else None)
    except (TypeError, ValueError):
        return None, None
    if as_of is None or as_of > today:
        return None, boundary
    return as_of, boundary


def bundled_status(cfg, *, directory: Path = BUNDLE, today: date | None = None) -> dict:
    """Compare the bundled master with the config-pinned dictionary, read-only."""
    today = today or date.today()
    try:
        manifest, _ = _bundle(directory)
        bundled = {"edition": manifest["edition"], "rows": manifest["rows"],
                   "archive_sha256": manifest["archive_sha256"],
                   "dict_id": f"mhlw-y-{manifest['edition']}-{manifest['archive_sha256'][:12]}"}
    except (ValueError, OSError):
        bundled = None
    dictionary, error = drug_map.configured(cfg)
    setting = cfg.get("drug_map") if isinstance(cfg, dict) else None
    configured = None
    if dictionary is not None:
        configured = {"id": dictionary.dict_id, "sha256": dictionary.sha256,
                      "approved": dictionary.approved, "source": dictionary.source.name,
                      "terms_checked_on": dictionary.source.terms_checked_on,
                      "as_of": dictionary.source.as_of,
                      "next_boundary_on": dictionary.source.next_boundary_on}
        as_of, boundary = _build_dates(dictionary.source, today)
    state = ("bundle_unavailable" if bundled is None
             else "invalid" if error is not None
             else "unconfigured" if setting is None
             else "missing" if dictionary is None
             else "differs_from_bundle" if dictionary.dict_id != bundled["dict_id"]
             else "build_date_unknown" if as_of is None
             else "rebuild_due" if boundary is not None and today >= boundary
             else "bundled_approved" if dictionary.approved else "bundled_reference_only")
    return {"state": state, "bundled": bundled, "configured": configured,
            "read_only": True, "explanation": STATUS_EXPLANATIONS[state]}


STATUS_EXPLANATIONS = {
    "bundle_unavailable": "同梱の公式医薬品マスターを読めません。配布物を確認してください。",
    "invalid": "設定の drug_map または辞書が不正です。パス・所有者権限・SHA256を確認してください。",
    "unconfigured": "辞書は未設定です。同梱版から作るには mcs drug install-bundled を実行します。",
    "missing": "設定された辞書ファイルがありません。install-bundled で作り直すか設定を見直してください。",
    "differs_from_bundle": "設定中の辞書は同梱版と異なる原本から作られています。更新する場合は install-bundled 後に compare / impact で差分を確認してから設定を切り替えます。",
    "build_date_unknown": "設定中の辞書は同梱版と同じ原本から作られていますが、作成基準日を確認できません（作成基準日の記録がない旧形式、または今日より後の日付）。収載日・終了日の反映を確かめるため、install-bundled で作り直し、compare / impact で差分を確認してから設定を切り替えます。",
    "rebuild_due": "設定中の辞書の作成後に、同梱版で収載開始または終了を迎えた行があります。install-bundled で作り直し、compare / impact で差分を確認してから設定を切り替えます。",
    "bundled_approved": "同梱版から作った承認済み辞書を使用中です。",
    "bundled_reference_only": "同梱版から作った未承認辞書です。照会・参考検索だけに使われ、薬剤候補の注釈には使われません。",
}


def _bundled_display(command, report):
    print(_line(report.get("explanation") or ""))
    if command == "status":
        bundled, configured = report["bundled"], report["configured"]
        if bundled:
            print(f"同梱: 医薬品マスター {bundled['edition']}版 {bundled['rows']}行"
                  f" / {bundled['archive_sha256'][:12]}")
        if configured:
            print(f"設定: {_line(configured['id'])}@{configured['sha256'][:12]}"
                  + ("（承認済み）" if configured["approved"] else "（未承認・参考のみ）"))
            # a missing or untrusted build date leaves the boundary unknown, not absent
            known = configured["as_of"] and report["state"] != "build_date_unknown"
            print(f"作成基準日: {_line(configured['as_of'] or '記録なし')}"
                  f" / 次の見直し日: {_line(configured['next_boundary_on'] or ('なし' if known else '未確認'))}")
        return
    print(f"同梱 {report['edition']}版 {report['rows']}行 → 辞書 {report['entries']}件"
          + ("（承認済み）" if report["approved"] else "（未承認・参考のみ）"))
    if report["path"] is None:
        print(f"保留: {_line(report['held'])}（辞書は作成していません）")
        return
    print(("作成しました: " if report["state"] == "written" else "既に同じ辞書があります: ")
          + _line(report["path"]))
    for key, verb in (("future_starts", "から候補になります"), ("future_ends", "に候補から外れます")):
        for day, count in sorted((report.get(key) or {}).items()):
            print(f"  {count}行は {_line(day)} {verb}")
    if report.get("next_boundary_on"):
        print(f"この辞書は作成基準日 {_line(report['as_of'])} 時点の内容です。"
              f"次の見直し日 {_line(report['next_boundary_on'])} 以降に install-bundled で作り直してください。")
    setting = json.dumps({"path": report["path"], "sha256": report["sha256"]}, ensure_ascii=False)
    print("設定は変更していません。使う場合は内容を確認してから次を実行します:")
    print("  python3 mcs/ops/mcs_setup.py init --set " + shlex.quote("drug_map=" + setting))


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
    command = sub.add_parser("install-bundled",
                             help="同梱の公式医薬品マスターから私有辞書を新規作成する")
    command.add_argument("--terms-checked-on", required=True,
                         help="運用者が利用条件を確認した日（YYYY-MM-DD）")
    command.add_argument("--confirmed-by", required=True,
                         help="変更区分・日付番兵の扱いを確認した運用者")
    command.add_argument("--approved-by",
                         help="薬剤候補の注釈に使うことを承認した運用者（省略時は参考専用）")
    command.add_argument("--terms-record", help="利用条件確認の記録先（省略時は利用条件のURL）")
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("status", help="同梱版と設定中の辞書の対応を確認する")
    command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command in ("install-bundled", "status"):
        import import_drug_master as importer
        try:
            report = (bundled_status(load_config(), directory=BUNDLE)
                      if args.command == "status"
                      else install_bundled(args, directory=BUNDLE, home=HOME))
        except (ValueError, TypeError, OSError, importer.MasterError) as error:
            code = error.code if isinstance(error, importer.MasterError) else (
                str(error) if isinstance(error, ValueError) else type(error).__name__)
            if args.json:
                print(json.dumps({"status": "refused", "reason": code, "written": False}))
            else:
                print(f"drug: 同梱マスターを処理できません（{_line(code)}）", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            _bundled_display(args.command, report)
        return 0 if args.command == "status" or report["path"] else 1
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
