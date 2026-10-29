"""Guide setup answers and dictionary consent before the caller saves configuration."""
from __future__ import annotations

import re

from mcs_config import positive_number_problem, transport_enabled

# Item = (dotted key, kind, default, description, gate).
#   kind: req | opt | bool | int | num | csv | intlist | choice:a,b,...
#   req* kinds loop until a value is given; everything else accepts an
#   empty answer meaning "keep current, else the shown default".
#   gate(cfg) hides keys whose feature is off — discord.* only exists
#   when interactive delivery is on, digest knobs only when signals
#   notify, semantic details only when a mode is enabled.
UNSET = object()

def _discord_on(cfg):
    return transport_enabled(cfg, "discord")


def _slack_on(cfg):
    return transport_enabled(cfg, "slack")


def _lineworks_on(cfg):
    return transport_enabled(cfg, "lineworks")


def _cards_on(cfg):
    return (cfg.get("notify") or {}).get("interactive") \
        in ("discord", "slack", "lineworks")


def _signals_on(cfg):
    return (cfg.get("signals") or {}).get("notify") is True


def _digest_on(cfg):
    return (cfg.get("daily_digest") or {}).get("enabled") is True


def _semantic_on(cfg):
    m = (cfg.get("semantic") or {}).get("mode", "auto")
    return m != "off"


WIZARD = [
    ("使い方とMCSのアカウント", [
        ("runtime_mode", "choice:hermes,standalone", "hermes",
         "どの方法で使いますか？ hermes=Hermesと連携 / standalone=MCSだけで動かす", None),
        ("mcs_login_id", "req", None,
         "MCS のログインID", None),
    ]),
    ("通知の送り先", [
        ("notify_target", "req", None,
         "通知の送り先（例: slack:<チャンネルID>、"
         "discord:<チャンネルID>、lineworks:<トークルームID>）", None),
        ("notify_system_target", "opt", None,
         "エラーや動作状況の通知先（空欄なら通常の通知と同じ場所）", None),
    ]),
    ("通知の形式を選ぶ", [
        ("notify.interactive", "choice:off,slack,discord,lineworks", "off",
         "どの形式で通知しますか？ slack/discord/lineworks=ボタン付き通知 / off=文章のみ", None),
    ]),
    ("LINE WORKSの接続先と操作できる人", [
        ("notify.lineworks.profile", "req", "default",
         "このLINE WORKS接続を区別する名前（通常はdefault）", _lineworks_on),
        ("notify.lineworks.application_id", "req", None,
         "LINE WORKS Bot ID", _lineworks_on),
        ("notify.lineworks.team_id", "req", None,
         "LINE WORKS ドメインID", _lineworks_on),
        ("notify.lineworks.channel_id", "req", None,
         "配信先トークルームのチャンネルID", _lineworks_on),
        ("notify.lineworks.allowed_user_ids", "reqcsv", None,
         "操作を許可するユーザーID・カンマ区切り（必須）", _lineworks_on),
        ("notify.lineworks.project_ids", "reqintlist", None,
         "配信・操作を許可するMCSプロジェクトID・カンマ区切り（必須）", _lineworks_on),
        ("notify.lineworks.project_ids_auto", "bool", False,
         "公開済みscope snapshotからプロジェクトを解決（通常はオフ）", _lineworks_on),
    ]),
    ("Slackの接続先と操作できる人", [
        ("notify.slack.profile", "req", None,
         "この接続先を区別する名前（プロファイル名。例: mcs）", _slack_on),
        ("notify.slack.application_id", "req", None,
         "Slack アプリID", _slack_on),
        ("notify.slack.team_id", "req", None,
         "Slack ワークスペース（team）ID", _slack_on),
        ("notify.slack.channel_id", "req", None,
         "カードの投稿先チャンネルID", _slack_on),
        ("notify.slack.allowed_user_ids", "reqcsv", None,
         "操作を許可するSlackユーザーID・カンマ区切り", lambda cfg: cfg.get("runtime_mode") == "standalone" and _slack_on(cfg)),
        ("notify.slack.project_ids_auto", "bool", False,
         "公開済みscope snapshotからプロジェクトを解決", lambda cfg: cfg.get("runtime_mode") == "standalone" and _slack_on(cfg)),
        ("notify.slack.project_ids", "intlist", [],
         "閲覧・操作を許可するMCSプロジェクトID・カンマ区切り（自動解決がオフなら必須）", lambda cfg: cfg.get("runtime_mode") == "standalone" and _slack_on(cfg)),
    ]),
    ("Discordの接続先と操作できる人", [
        ("notify.discord.profile", "req", None,
         "この接続先を区別する名前（プロファイル名。例: mcs）", _discord_on),
        ("notify.discord.application_id", "req", None,
         "Discord アプリケーションID", _discord_on),
        ("notify.discord.guild_id", "req", None,
         "Discord サーバーID", _discord_on),
        ("notify.discord.channel_id", "req", None,
         "カードの投稿先チャンネルID", _discord_on),
        ("notify.discord.allowed_user_ids", "reqcsv", None,
         "操作を許可するDiscordユーザーID・カンマ区切り", lambda cfg: cfg.get("runtime_mode") == "standalone" and _discord_on(cfg)),
        ("notify.discord.allowed_role_ids", "csv", None,
         "操作を許可するDiscordロールID（@everyone不可・空欄可）", lambda cfg: cfg.get("runtime_mode") == "standalone" and _discord_on(cfg)),
        ("notify.discord.project_ids_auto", "bool", False,
         "公開済みscope snapshotからプロジェクトを解決", lambda cfg: cfg.get("runtime_mode") == "standalone" and _discord_on(cfg)),
        ("notify.discord.project_ids", "intlist", [],
         "閲覧・操作を許可するMCSプロジェクトID・カンマ区切り（自動解決がオフなら必須）", lambda cfg: cfg.get("runtime_mode") == "standalone" and _discord_on(cfg)),
        ("notify.operator", "opt", None,
         "運用者の Discord ユーザーID（空欄可）", _discord_on),
    ]),
    ("通知のまとめ方と対象期間", [
        ("notify.card_thread", "bool", True,
         "同じ患者の通知を1つのスレッドにまとめますか？", lambda cfg: _slack_on(cfg) or _discord_on(cfg)),
        ("notify.card_thread_archive_min", "int", 10080,
         "スレッドをアーカイブするまでの時間（分。10080分=7日）", lambda cfg: _slack_on(cfg) or _discord_on(cfg)),
        ("notify.route_epoch", "int", 1,
         "通知先の変更を区別する番号（通常はそのまま。通知先を変更する場合のみ1増やす）", _cards_on),
        ("notify_bot_profile", "opt", None,
         "通知に使うHermesの設定名（複数の接続設定がある場合のみ入力）", lambda cfg: cfg.get("runtime_mode") != "standalone"),
        ("notify_max_age_h", "num", None,
         "何時間前までの投稿を通知しますか？（空欄=制限なし。期間内に新しく取り込んだ投稿は既読でも通知）", None),
        ("notify_all_replies", "bool", False,
         "リアルタイムで新規取得した返信は既読・投稿時刻によらず全件通知（履歴一括取込みは対象外）", None),
        ("hermes_bin", "opt", None,
         "hermes コマンドのパス（空欄=自動検出）", lambda cfg: cfg.get("runtime_mode") != "standalone"),
    ]),
    ("朝のまとめ通知", [
        ("daily_digest.enabled", "bool", False,
         "朝に1日分のまとめを通知先へ送りますか？（件数とID。患者名は次の項目で選びます）", None),
        ("daily_digest.hour_jst", "int", 8,
         "日次ダイジェストを送る時刻（JST・0-23時）", _digest_on),
        ("daily_digest.include_names", "bool", False,
         "日次ダイジェストの一覧に患者名を添える（送信先は notify_target）",
         _digest_on),
        ("daily_digest.scope", "opt", None,
         "日次ダイジェストの対象患者（例: station:○○ / project:1,2 / days:3。空欄=全患者）",
         _digest_on),
    ]),
    ("取り込む投稿と履歴", [
        ("self_posts", "bool", False,
         "自分がMCSに投稿した内容も取り込んで通知しますか？（未読一覧には自分の投稿が含まれない"
         "ため、最新の投稿を別途確認します）", None),
        ("metadata_shadow", "bool", False,
         "未読保持の実証・運用合意後のみスタンプを再取得（shadow・反応値は非公開）",
         None),
        ("metadata_refresh_publish", "bool", False,
         "shadow再取得の成功結果を表示用の観測へ反映（未読保持と照合の確認後のみ）",
         None),
        ("metadata_actors", "bool", False,
         "スタンプを押した人の氏名・所属・職種を取得してスレッドに表示（無期限保持）",
         None),
        ("clinical_metadata", "bool", False,
         "deep runで登録薬剤・検査項目・観測値をGET取得（既読化操作は行わない）",
         None),
        ("deep_history", "bool", True,
         "初回に過去の投稿もすべて取り込みますか？（件数によって時間がかかります）", None),
        ("discover_archived", "bool", False,
         "MCSでアーカイブ済みの患者も取り込みますか？", None),
        ("trickle_pages", "int", 3,
         "1回の実行で履歴を遡るページ数（1-40）", None),
        ("job_budget_seconds", "num", None,
         "内部処理の時間予算・秒（空欄=既定）", None),
    ]),
    ("このMacで動くAI（通常はEnterで進めます）", [
        ("local_llm.url", "opt", None,
         "AIの接続先URL（通常は空欄。既定は http://127.0.0.1:8080/v1/chat/completions。指定する場合は /v1/chat/completions まで入力してください。外部URLは指定できません）",
         None),
        ("local_llm.model", "opt", None,
         "AIモデル名（通常は空欄でQwen3.5-9B。別のモデルを導入した場合のみ指定）", None),
    ]),
    ("確認が必要な内容の通知", [
        ("signals.notify", "bool", False,
         "確認が必要な内容の候補を通知カードに表示しますか？", None),
        ("signals.digest", "bool", False,
         "複数候補をダイジェストにまとめて送る", _signals_on),
        ("signals.digest_interval_h", "num", None,
         "ダイジェストの間隔・時間（空欄=既定）", _signals_on),
        ("signals.self_organizations", "csv", None,
         "自施設名・カンマ区切り（空欄=MCSプロフィールから自動検出）",
         None),
        ("signals.self_professions", "csv", None,
         "自職種・カンマ区切り（同上）", None),
        ("signals.request_targets", "csv", None,
         "依頼先として数える宛名（空欄可）", None),
        ("signals.med_exclude_names", "csv", None,
         "薬剤判定から除外する語・カンマ区切り（空欄可）", None),
    ]),
    ("外部サービスによる追加解析", [
        ("semantic.mode", "choice:auto,off,shadow,assist,enforce", "auto",
         "auto=このMCSの.envにキーがあれば追加解析を使用。対象の投稿本文をJev APIへ送信します。"
         "off=使わない / shadow=比較用 / assist=解析結果を使用 / enforce=承認済みの通知判断に使用", None),
        ("semantic.project_ids", "intlist", None,
         "対象プロジェクトID・カンマ区切り（autoで未指定なら全対象、手動有効化時は必須）",
         _semantic_on),
        ("semantic.extract_qc", "choice:off,annotate", "off",
         "抽出結果への Jev 監査注記", _semantic_on),
        ("semantic.daily_request_budget", "int", None,
         "Jev要求の1日上限（再試行込み、空欄=auto時10000・手動mode時0、最大10000、0=送信停止）", _semantic_on),
    ]),
]


def get_key(cfg: dict, dotted: str):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def has_key(cfg: dict, dotted: str) -> bool:
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def set_key(cfg: dict, dotted: str, val):
    if dotted == "semantic.mode" and val == "auto":
        val = UNSET
    parts = dotted.split(".")
    if val is UNSET:
        cur = cfg
        for p in parts[:-1]:
            cur = cur.get(p)
            if not isinstance(cur, dict):
                return
        cur.pop(parts[-1], None)
        return
    cur = cfg
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = cur[p] = {}
        cur = nxt
    cur[parts[-1]] = val


def _fmt(v) -> str:
    if type(v) is bool:
        return "オン" if v else "オフ"
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return str(v)


def parse_answer(kind: str, raw: str):
    """Typed answer -> (ok, value|error message)."""
    if kind in ("req", "opt"):
        return True, raw
    if kind == "bool":
        v = raw.lower()
        if v in ("y", "yes", "1", "true", "on", "はい", "オン"):
            return True, True
        if v in ("n", "no", "0", "2", "false", "off", "いいえ", "オフ"):
            return True, False
        return False, "1（はい）か 2（いいえ）で答えてください（y / n も使えます）"
    if kind == "int":
        try:
            return True, int(raw)
        except ValueError:
            return False, "整数で入力してください"
    if kind == "num":
        try:
            v = float(raw)
        except ValueError:
            return False, "数値で入力してください"
        if positive_number_problem(v) is not None:
            return False, "0より大きく10億以下の有限の数値で入力してください"
        return True, int(v) if v == int(v) else v
    if kind in ("csv", "reqcsv", "intlist", "reqintlist"):
        parts = [x.strip() for x in raw.split(",") if x.strip()]
        if kind.startswith("req") and not parts:
            return False, "1件以上の値をカンマ区切りで入力してください"
        if "intlist" in kind:
            try:
                return True, [int(x) for x in parts]
            except ValueError:
                return False, "整数をカンマ区切りで入力してください"
        return True, parts
    if kind.startswith("choice:"):
        opts = kind.split(":", 1)[1].split(",")
        if raw in {str(i) for i in range(1, len(opts) + 1)}:
            raw = opts[int(raw) - 1]
        return ((True, raw) if raw in opts
                else (False, "表示された番号、または次の値で入力: " + ", ".join(opts)))
    return True, raw


_CHOICE_LABELS = {
    "recommended": "必要な項目だけ設定する（わからない場合はこちら）",
    "detailed": "すべての設定を一つずつ確認する",
    "hermes": "Hermes と連携する（Hermes を導入済みの方）",
    "standalone": "MCS だけで動かす（Hermes が不要）",
    "off": "使わない", "slack": "Slack のカード通知（通知を使う場合の推奨）",
    "discord": "Discord のカード通知", "lineworks": "LINE WORKS のカード通知",
    "auto": "キーがあれば自動で追加解析を使用する",
    "assist": "外部サービスへ送信して解析結果を使用する",
    "shadow": "外部サービスへ送信して結果を記録する",
    "enforce": "外部サービスへ送信して判定に使う",
    "annotate": "外部サービスの検査結果を付ける",
    "follow": "既定に従う（版の既定が変われば追従します）",
    "show": "案内を表示する",
    "omit": "案内を省略する（今後の既定変更でも表示しません）",
}
# optional feature -> (what the guidance is about, where it is explained)
OPTIONAL_FEATURES = {
    "csdc": ("CSDC（状態監視・遠隔支援）のセットアップ案内", "docs/guides/SDC_SELF_MONITORING.md"),
}


def ask(key: str, kind: str, default, desc: str, cur, present=False):
    """One wizard item. Returns the value to store, or UNSET.
    `present`: the key exists in cfg (so cur=None is an explicit null)."""
    eff = cur if cur is not None else default
    # explicit null on a required item is a valid stored value (e.g.
    # semantic.project_ids: null = all projects) — Enter keeps it
    keep_null = present and cur is None and key == "semantic.project_ids"
    marks = []
    default_label = "初期値" if key == "notify.interactive" else "推奨"
    value_label = _fmt(eff)
    if kind == "bool" and isinstance(eff, bool):
        value_label = "はい" if eff else "いいえ"
    if keep_null:
        marks.append("現在: null（全対象）")
    if cur is not None:
        marks.append(f"現在: {value_label}")
    elif eff is not None:
        marks.append(f"{default_label}: {value_label}（Enter で選択）")
    if kind == "bool":
        marks.append("1=はい / 2=いいえ")
    elif kind.startswith("choice:"):
        print(f"\n  {desc}")
        for number, option in enumerate(kind.split(":", 1)[1].split(","), 1):
            label = ("カードを使わない（文字だけの通知）"
                     if key == "notify.interactive" and option == "off"
                     else _CHOICE_LABELS.get(option, option))
            suffix = "（現在の設定・Enter で保持）" if option == cur else (
                f"（{default_label}・Enter で選択）" if cur is None and option == default else "")
            print(f"    {number}. {label}{suffix}")
        marks.append("番号または設定名")
    if cur is not None and kind in ("opt", "num", "csv", "intlist"):
        marks.append("`-` で削除")
    question = "番号を選んでください" if kind.startswith("choice:") else desc
    prompt = (f"  {question}\n    設定項目: {key}"
              + (f" [{' / '.join(marks)}]" if marks else "") + ": ")
    while True:
        raw = input(prompt).strip()
        if raw.lower() in ("q", "quit", "中止"):
            raise KeyboardInterrupt
        if raw in ("?", "わからない"):
            if key in ("notify_target", "notify_system_target"):
                print("    Slack は投稿先チャンネルを開き、チャンネル名 → 詳細の最下部でIDを確認します。"
                      "\n    slack: に続けてそのIDを入力します（入力形式の例: slack:C0123456789）。"
                      "\n    Discord / LINE WORKS は担当者に通知先のIDを確認してください。"
                      "\n    手順: docs/guides/EASY_SETUP.md、詳しいIDの調べ方: docs/guides/INSTALLATION.md")
            if kind.startswith("req") and eff is None:
                print("    この項目は施設ごとに異なるため、自動で決められません。"
                      "管理者に確認してください。q で保存せず中止できます。")
                continue
            print("    Enter で現在の設定を保持します。未設定なら表示された推奨設定を使います。")
            continue
        if not raw:
            if keep_null:
                return None
            if kind.startswith("req") and eff is None:
                print("    必須項目です — 値を入力してください。わからない場合は ?、中止は q。")
                continue
            return eff if eff is not None else UNSET
        if raw == "-" and cur is not None and not kind.startswith("req"):
            return UNSET
        ok, val = parse_answer(kind, raw)
        if ok:
            return val
        print(f"    {val}")


def wizard(cfg: dict):
    """Walk every config key in sections — Enter accepts the shown
    current/default value, so a beginner can step through safely.
    Recommended setup skips advanced items while retaining existing values.
    Items whose feature is off are skipped via their gate."""
    print("\nMCS の設定を順番にご案内します。番号を入力して Enter を押してください。"
          "\n空欄で Enter を押すと、現在の設定を保持します。未設定なら推奨設定を使います。"
          "\n必須のIDは施設の管理者に確認してください。? で説明、q または Ctrl+C で中止できます。")
    detailed = ask("setup_detail", "choice:recommended,detailed", "recommended",
                    "設定の進め方", None) == "detailed"
    basic = {"runtime_mode", "mcs_login_id", "notify_target", "notify_system_target",
             "notify.interactive", "self_posts", "deep_history",
             "notify.card_thread", "signals.digest"}
    step = 0
    for title, items in WIZARD:
        if not any(when is None or when(cfg) for *_, when in items):
            continue
        step += 1
        print(f"\n【手順 {step}】{title}")
        if not detailed:
            print("  質問しない設定は現在の値を保持し、未設定ならその版の既定に従います。")
        for key, kind, default, desc, when in items:
            if when is not None and not when(cfg):
                continue
            if key == "semantic.project_ids" and get_key(cfg, "semantic.mode") in ("shadow", "assist", "enforce"):
                kind = "reqintlist"
            if not detailed and key not in basic and not kind.startswith("req") \
                    and not key.endswith(("project_ids", "project_ids_auto", "allowed_role_ids")):
                continue
            if key in ("notify.slack.channel_id", "notify.discord.channel_id") \
                    and get_key(cfg, key) is None:
                transport = key.split(".")[1]
                for target in (cfg.get("notify_target"), cfg.get("notify_system_target")):
                    if isinstance(target, str) and target.startswith(transport + ":"):
                        channel = target.partition(":")[2]
                        if ((transport == "slack" and re.fullmatch(r"[CGD][A-Z0-9]+", channel))
                                or (transport == "discord" and channel.isdecimal()
                                    and any(c != "0" for c in channel))):
                            default = channel
                            break
            set_key(cfg, key,
                     ask(key, kind, default, desc, get_key(cfg, key),
                          has_key(cfg, key)))
    return step + 1


def wizard_optional_features(cfg: dict, *, interactive: bool | None = None) -> None:
    """Choose per optional feature whether its setup guidance follows the release default.

    Only the guidance choice is stored; registration, consent, keys and sending
    stay with their own steps. Choosing the default removes the explicit key so a
    later release that changes the default applies. Without a terminal the current
    choice is only shown and nothing is written."""
    import sys
    from mcs_config import OPTIONAL_FEATURE_DEFAULTS
    if interactive is None:
        interactive = sys.stdin.isatty()
    current_all = cfg.get("optional_features")
    for name, (label, guide) in OPTIONAL_FEATURES.items():
        current = current_all.get(name) if isinstance(current_all, dict) else None
        current = current if type(current) is bool else None
        default = "表示" if OPTIONAL_FEATURE_DEFAULTS[name] else "省略"
        state = {None: "既定に従う", True: "案内を表示する", False: "案内を省略する"}[current]
        print(f"\n【任意機能】{label}（説明: {guide}）"
              f"\n  この版の既定: 案内を{default}。現在の選択: {state}。"
              "ここで選ぶのは案内の表示だけで、登録・同意・送信は行いません。")
        if not interactive:
            print("  変更する場合は端末で mcs setup features（PATH未設定なら ~/.local/bin/mcs setup features）を実行してください。")
            continue
        choice = ask(f"optional_features.{name}", "choice:follow,show,omit", "follow",
                     label, {None: None, True: "show", False: "omit"}[current])
        values = cfg.get("optional_features")
        values = dict(values) if isinstance(values, dict) else {}
        if choice == "show":
            values[name] = True
        elif choice == "omit":
            values[name] = False
        else:
            values.pop(name, None)
        if values:
            cfg["optional_features"] = values
        else:
            cfg.pop("optional_features", None)


def offer_sdc_enrollment(cfg: dict) -> None:
    """Offer optional monitoring only after core setup has saved its configuration."""
    from pathlib import Path
    from adapters.sdc.protocol import endpoint
    from adapters.sdc import provision
    from adapters.sdc.storage import Store
    import sdc_cli

    settings = cfg.get('sdc', {})
    root = settings.get('state_root') if isinstance(settings, dict) else None
    try:
        with Store(root) as store:
            if provision.monitor_endpoint(store) is not None:
                from time import time
                if not provision.status(store, now_ms=int(time() * 1000))['registration_started']:
                    print('状態監視の配布は保存済みですが、登録は保留しています。本体設定は保存済みです。'
                          '同じ --state-root と --import-provision FILE でConsoleの内容を再確認し、'
                          '--confirm-human・--reason を指定して再承認した後、同じ操作内で登録へ進んでください。'
                          '顧客の登録は --customer も指定します。')
                    return
            endpoint(store.read('endpoint.json'))
            selected = store.root
            kind = 'customer' if store.customer else ('self' if store.read('credential', {}) or store.read('state.json', {}) else 'new')
    except (OSError, ValueError, TypeError):
        print('任意の状態監視は未設定です。信頼済みの送信先が未配置か確認できません。'
              '本体設定は保存済みです。新規導入は導入担当が mcs connect --prepare-provision-root で専用領域を準備し、'
              '認証済みConsoleから受け取ったmonitor配布を --import-provision で確認・保存した後に登録できます。'
              '各操作には同じ --state-root を指定し、保存には --confirm-human と --reason が必要です。既存領域や復元元は作り直しません。')
        return
    print('任意の状態監視: 版とプロセス状態だけを中央へ送ります。患者情報・投稿本文は送りません。'
          '登録コードと管理者の承認が必要です。登録時に送信内容を別途確認します。')
    try:
        argv = ['connect', '--state-root', str(Path(selected))]
        if kind == 'new':
            choice = input('状態監視をどの用途で登録しますか？ 1: 顧客として登録（Keychain保存） / 2: 自社検証として登録 / 3: 今は登録しない [3]: ').strip()
            if choice == '1':
                kind = 'customer'
            elif choice != '2':
                return
        elif input('現在の登録用途を保持して状態監視の登録へ進みますか？ 1: 進む / 2: 今はしない [2]: ').strip() != '1':
            return
        if kind == 'customer':
            # A menu choice is not the credential-change confirmation; the operator gives it explicitly.
            import shlex
            print('本体設定は保存済みです。顧客の状態監視の登録は保留しています（Keychainへの保存は本人の明示確認が必要です）。'
                  '次の操作で登録を再開してください。登録コードは実行時に入力します: '
                  'mcs connect --customer --state-root ' + shlex.quote(str(Path(selected)))
                  + " --confirm-human --reason '<秘密を含まない半角の理由>'")
            return
        result = sdc_cli.main(argv)
    except (EOFError, KeyboardInterrupt):
        result = 1
    if result:
        print('状態監視の登録は未完了です。同意記録と本体設定は保持します。mcs connect で再開できます。')


def confirm_save(cfg: dict, *, plugin_profile, key_presence=None) -> bool:
    """Show the selected destinations and writes; require an explicit answer before saving."""
    import stamp_write
    import local_llm
    from semantic_policy import runtime_config, semantic_config
    import cloud_policy

    def shown(value):
        if value is None or value == "":
            return "未設定"
        return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value))[:300]

    ntf = cfg.get("notify") or {}
    mode = cfg.get("runtime_mode", "hermes")
    interactive = ntf.get("interactive", "off")
    scope = ntf.get(interactive) or {}
    digest = cfg.get("daily_digest") or {}
    semantic, _ = (runtime_config(cfg) if key_presence is None else
                   semantic_config(cfg, key_present=key_presence.get("jev") is True))
    if key_presence is None:
        from mcs_util import private_env_problem
        key_presence = {name: private_env_problem(key) is None for name, key in (
            ("anthropic", "MCS_ANTHROPIC_API_KEY"), ("openai", "MCS_OPENAI_API_KEY"))}
    cloud, cloud_errors = cloud_policy.cloud_config(cfg)
    provider = cloud["provider"]
    if provider == "auto":
        provider = next((name for name in cloud["provider_order"] if key_presence.get(name) is True),
                        cloud["provider_order"][0])
    cloud_key = key_presence.get(provider) is True
    block = cfg.get("cloud_llm")
    if (block is None or isinstance(block, dict) and "mode" not in block) and not cloud_errors:
        cloud, _ = cloud_policy.cloud_config(cfg, key_present=cloud_key)
    cloud["provider"], cloud["model"] = provider, cloud_policy.PROVIDERS[provider]
    print("\n【保存前の確認】設定した送り先と、この後に保存する内容を確認してください。"
          "\n  ここで中止すると、今回の設定・認証情報・辞書は保存しません。")
    print(f"  実行方法: {shown(mode)}"
          f"\n  通常通知先: {shown(cfg.get('notify_target'))}"
          f"\n  動作状況・エラー通知先: {shown(cfg.get('notify_system_target') or cfg.get('notify_target'))}")
    if mode == "hermes":
        print("  通知送信に使うHermes設定名: "
              + shown(cfg.get("notify_bot_profile") or "未指定（実行環境の既定を使用）"))
    print("  通知する情報: 患者名・施設、投稿者・日時、要約・投稿本文・送信対象の添付。"
          "通知先の参加者が閲覧できるため、送り先を確認してください。")
    if interactive in ("slack", "discord", "lineworks"):
        print(f"  ボタン付き通知: {shown(interactive)} / 接続設定名 {shown(scope.get('profile'))}"
              f" / チャンネルID {shown(scope.get('channel_id'))}")
    else:
        print("  ボタン付き通知: 使いません（文章のみの通知）")
    if digest.get("enabled") is True:
        print("  朝のまとめ通知: 有効 / 一覧の患者名: "
              + ("表示する" if digest.get("include_names") is True else "表示しない"))
    else:
        print("  朝のまとめ通知: 無効")
    endpoint, _model = local_llm.resolve(cfg)
    print("  通常の要約・抽出: このMacのAIで処理します。接続先: "
          + shown(endpoint))
    project_scope = "全対象" if semantic["project_ids"] is None else shown(semantic["project_ids"])
    features = ("mode", "summary_mode", "loop_mode", "extract_qc")
    if any(semantic[key] != "off" for key in features) and semantic["daily_request_budget"] > 0:
        print(f"  外部Jev追加解析: 対象プロジェクト {project_scope} の投稿本文を、"
              "機能ごとの許可条件がそろった場合にJev APIへ送ります。患者名を含む本文は匿名化しません。")
    else:
        print("  外部Jev追加解析: 無効（有効な機能と送信予算が必要です）")
    print("  Jev設定: " + ", ".join(f"{key}={shown(semantic[key])}" for key in features)
          + f" / 1日の要求上限 {semantic['daily_request_budget']} / 対象 {project_scope}")
    print(f"  AI利用先: {cloud['provider']} / モデル {cloud['model']} / API別上限 {cloud['provider_budgets']}")
    cloud_scope = "全対象" if cloud["project_ids"] is None else shown(cloud["project_ids"])
    if cloud["mode"] in ("on", "shadow") and cloud_key:
        print(f"  外部AI追加処理: {cloud['mode']} / 対象 {cloud_scope}。患者コンテキストと対象の再抽出で、"
              f"患者名を含む投稿本文・参考文脈を匿名化せず選択した{'OpenAI' if provider == 'openai' else 'Anthropic'}のAPIへ送信します。通常のAPI保持条件が適用されます。"
              "キー登録だけではZDRやHIPAAに関する契約は確認できません。保持条件と契約を利用者側で確認してください。"
              "詳細: docs/guides/ANTHROPIC.md")
    else:
        print(f"  外部AI追加処理: {cloud['mode']} / キー未登録・off・dry_runの場合は外部送信しません。")
    daily_cap, monthly_cap = ("上限なし" if cloud[key] is None else f"{cloud[key]:g} USD"
                              for key in ("daily_cost_limit_usd", "monthly_cost_limit_usd"))
    print(f"  外部AI合計費用上限: 1日 {daily_cap} / 1か月 {monthly_cap}（両方の上限を適用）")
    print("  公開薬情報の照会: 同梱公開マスターで照合した薬品名・一般疾患名・公式URLを"
          "TinyFishへ送り、トークンファイルがない場合だけ固定の無料Exaへ送ります。患者本文は送りません。"
          "\n  定期的な薬情報更新確認: "
          + ("無効" if cfg.get("drug_info_watch") is False else "有効（強制再取得できるTinyFish経路のみ）"))
    print("  MCSの既読: 定期収集には、取得完了と取得時刻を確認した対象を既読にする経路があります。"
          "\n  MCSスタンプ送信: "
          + ("設定上は利用可能。操作者の個別操作と承認記録を経て送信します。"
             if stamp_write.enabled(cfg) else "現在の設定では無効。"))
    print("  保存する操作: config.json、入力・環境で渡されたMCSパスワードのKeychainと本人用.env、"
          "渡された追加解析キーの.env保存。辞書は続く専用手順で選択・承認後に作成します。")
    if mode == "hermes" and interactive in ("slack", "discord"):
        profile = "未確定（接続設定を確認してください）" if plugin_profile is None else plugin_profile or "既定"
        print(f"  Hermesへの接続設定・認証情報の保存先: {shown(profile)}"
              "。利用条件がそろった場合は通知gateway設定も反映します。")
    elif mode == "standalone":
        print("  独立接続: Slack・Discord・LINE WORKSの認証情報と接続設定は、利用する接続について本人用の保存先へ保存します。")
    print("  保存後に接続設定を中断した場合、保存済みの内容は残り、同じ手順で再開できます。"
          "\n  この確認は個別のスタンプ操作の承認や、別サービスへの登録同意を作成しません。"
          "\n  空欄のまま進むと中止します。保存する場合だけ「はい」または1を選んでください。")
    return ask("setup.save", "bool", False, "表示した送り先と保存内容で続ける", None) is True


def wizard_drug_map(cfg: dict, step: int, *, home) -> None:
    """Offer the bundled official medicine master as the matching dictionary
    (default: yes). Every operator input is shown and taken only from a real
    answer; EOF/Ctrl+C abort init like any other item. An existing `drug_map`
    pin is never touched, and a held or failed conversion leaves cfg as is."""
    if cfg.get("drug_map") is not None:
        return
    from datetime import date
    from types import SimpleNamespace
    import mcs_drug
    try:
        manifest, _ = mcs_drug.load_bundled_master(mcs_drug.BUNDLE)
    except (ValueError, OSError):
        print(f"\n【手順 {step}】医薬品辞書\n  同梱の公式医薬品マスターを読めないため、辞書の設定は省略します。")
        return
    print(f"\n【手順 {step}】医薬品辞書（同梱の公式医薬品マスターで薬剤名を照合）"
          f"\n  同梱の公式医薬品マスター（版 {manifest['edition']}）から私有辞書を作成し、"
          "投稿に書かれた薬剤名を辞書と照合して候補を表示します。"
          "\n  利用条件の確認と採用の承認は運用者の入力として記録します。自動では埋めません。"
          f"\n  利用条件: {manifest['terms']['url']}")
    if not ask("drug_map.bundled", "bool", True,
                "同梱の公式医薬品マスターで薬剤名を照合する", None):
        print("  辞書は設定しません。後から mcs_drug.py install-bundled と init --set drug_map=... で設定できます。")
        return
    today = date.today()
    while True:
        checked = ask("drug_map.terms_checked_on", "req", today.isoformat(),
                       "利用条件を確認した日（YYYY-MM-DD・Enter で今日）", None)
        try:
            if date.fromisoformat(checked) <= today:
                break
        except ValueError:
            pass
        print("    YYYY-MM-DD 形式で、今日以前の日付を入力してください")
    print("  採用方針（install-bundled と同じ）: 変更区分 0/3/5 の行だけを候補にし、抹消 1・廃止 9 と"
          "\n    収載日・変更日が今日より後の行は除外します。終了日 0・廃止日 99999999 は期限なしです。"
          "\n    辞書は薬剤名の照合候補を示すだけで、成分や治療上の同等性は確定しません。")
    confirmed = ask("drug_map.confirmed_by", "req", cfg.get("mcs_login_id"),
                     "利用条件と採用方針を確認した人（氏名またはID・表示の値は Enter で採用）", None)
    approve = ask("drug_map.approve", "bool", True,
                   f"辞書の照合を有効にする（承認者として「{confirmed}」を記録。いいえなら参照のみで照合しない）",
                   None)
    args = SimpleNamespace(terms_checked_on=checked, confirmed_by=confirmed,
                           approved_by=confirmed if approve else None, terms_record=None)
    try:
        out = mcs_drug.install_bundled(args, directory=mcs_drug.BUNDLE, home=home)
    except (ValueError, OSError) as error:
        print(f"  辞書を作成できませんでした（{error}）。照合は有効になっていません。"
              "\n  設定は変更せず続行します。原因を確認してから mcs_drug.py install-bundled で再作成してください。")
        return
    if out["path"] is None:
        print(f"  辞書の作成を保留しました（{out['held']}）。照合は有効になっていません。"
              "\n  設定は変更せず続行します。保留理由を解消してから mcs_drug.py install-bundled で再作成してください。")
        return
    cfg["drug_map"] = {"path": out["path"], "sha256": out["sha256"]}
    print(f"  辞書を{'作成' if out['state'] == 'written' else '再利用'}しました: {out['path']}（{out['entries']}件）"
          + ("。照合は設定を保存した後の定期処理で有効になります。" if approve
             else "。承認者なしのため参照のみで、照合は行いません。"))
