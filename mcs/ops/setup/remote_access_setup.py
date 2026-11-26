"""Set up this Mac for other devices: save its Tailscale name, publish the patient workspace to the tailnet and run it resident."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

from mcs_config import REMOTE_ACCESS_PORT, TAILNET_HOST, remote_access

# The App Store and standalone app both ship the command inside the bundle; Homebrew's formula puts it on PATH.
APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
TIMEOUT_S = 30
# How long a just-(re)started workspace may take to answer before it is not published.
POLL_S, POLLS = 0.5, 40
# A path no page uses and a login no staff member has: the resident workspace refuses it with its own
# unregistered-login answer, and only when the request names the tailnet host it was started with.
PROBE_PATH = "/mcs-setup-probe"
PROBE_LOGIN = "mcs-setup-probe"


def tailscale_cli():
    found = shutil.which("tailscale")
    if found:
        return found
    return APP_CLI if os.access(APP_CLI, os.X_OK) else None


def _run(cli, *argv):
    """The local tailscale command only; it talks to this Mac's own Tailscale service, never to MCS."""
    try:
        return subprocess.run([cli, *argv], capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None


def this_mac(cli):
    """This Mac's tailnet name when Tailscale is signed in and running, else the reason it is not usable."""
    done = _run(cli, "status", "--json")
    if done is None or done.returncode not in (0, 1):
        return None, "Tailscale の状態を読み取れませんでした。Tailscale アプリが起動しているか確認してください。"
    try:
        status = json.loads(done.stdout)
    except ValueError:
        return None, "Tailscale の状態を読み取れませんでした。Tailscale を最新版に更新してから再実行してください。"
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        return None, "Tailscale にサインインしていません。Tailscale アプリを開いてサインインしてから再実行してください。"
    name = (status.get("Self") or {}).get("DNSName") if isinstance(status.get("Self"), dict) else None
    host = name.rstrip(".").lower() if isinstance(name, str) else ""
    if not TAILNET_HOST.match(host):
        return None, ("この Mac の Tailscale の名前（*.ts.net）を確認できません。Tailscale の管理画面で"
                      "MagicDNS と HTTPS 証明書を有効にしてから再実行してください。")
    return host, None


def _route_settings(path):
    if path is None:
        return None, None
    path = Path(path).expanduser().resolve()
    try:
        info = path.stat()
    except OSError:
        return None, f"訪問ルートの設定ファイルが見つかりません: {path}"
    if info.st_mode & 0o077:
        return None, f"訪問ルートの設定ファイルは所有者だけが読めるようにしてください（chmod 600 {path}）"
    import homecare_route
    try:
        # The same check the resident server makes at start, so a bad file never stops the whole workspace.
        homecare_route.RouteSettings(path)
    except (OSError, SystemExit) as exc:
        return None, f"訪問ルートの設定ファイルを使えません: {exc}"
    return str(path), None


def port_in_use(port):
    """Whether something on this Mac already answers on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def probe_headers(host):
    from mcs_local_chat import IDENTITY
    return {"Host": host, IDENTITY: PROBE_LOGIN}


def is_workspace(status, body):
    from mcs_local_chat import UNREGISTERED
    return status == 403 and body == UNREGISTERED


def workspace_answers(port, host, *, connection=http.client.HTTPConnection):
    """Whether 127.0.0.1:port is the resident workspace for ``host``; a plain connection, never a proxy or redirect."""
    from mcs_local_chat import UNREGISTERED
    probe = connection("127.0.0.1", port, timeout=2)
    try:
        probe.request("GET", PROBE_PATH, headers=probe_headers(host))
        answer = probe.getresponse()
        return is_workspace(answer.status, answer.read(len(UNREGISTERED) + 1))
    except (OSError, http.client.HTTPException):
        return False
    finally:
        probe.close()


def _workspace_listening(port, host):
    for attempt in range(POLLS):
        if workspace_answers(port, host):
            return True
        if attempt + 1 < POLLS:
            time.sleep(POLL_S)
    return False


def _save(config_edit, changes):
    try:
        _, errors = config_edit.apply(changes, origin="remote_access")
    except (config_edit.ConfigConflict, OSError, ValueError):
        return False
    return not errors


def command(args, *, services) -> int:
    import config_edit
    import mcs_util

    cfg = mcs_util.load_config_checked()
    if cfg is None:
        print("設定を読み取れないため変更していません。mcs_setup.py check で確認してください。")
        return 1
    current = remote_access(cfg) or {}
    if args.off:
        changes = {f"remote_access.{key}": None for key in current}
        if changes:
            try:
                _, errors = config_edit.apply(changes, origin="remote_access")
            except (config_edit.ConfigConflict, OSError, ValueError):
                errors = ["config"]
            if errors:
                print("設定を保存できなかったため、遠隔接続は止めていません。mcs_setup.py check で確認してください。")
                return 1
        print("遠隔接続をやめました。常駐サーバーを止めます。")
        print("tailnet への公開も止めるには `tailscale serve status` で確認し、`tailscale serve reset` を実行してください。")
        return services(args)
    print("MCS 訪問薬剤管理は現時点では利用を推奨していません。")
    cli = tailscale_cli()
    if cli is None:
        print("Tailscale が見つかりません。install.sh --with-tailscale で導入するか、"
              "https://tailscale.com/download から Tailscale を入れてから再実行してください。")
        return 1
    host, problem = this_mac(cli)
    if problem:
        print(problem)
        return 1
    port = args.port or current.get("port", REMOTE_ACCESS_PORT)
    if not 1024 <= port <= 65535:
        print("--port は1024から65535までの番号を指定してください。")
        return 1
    # A kept file is checked too: the restarted workspace reads it again and would not start without it.
    route, problem = _route_settings(args.route_settings or current.get("route_settings"))
    if problem:
        print(problem)
        return 1
    if port != current.get("port") and port_in_use(port):
        # Publishing would hand that other program to the tailnet, outside the workspace's staff check.
        print(f"この Mac の {port} 番は別のプログラムが使っています。--port で空いている番号を指定してください。")
        return 1
    changes = {"remote_access.tailnet_host": host, "remote_access.port": port}
    if route is not None:
        changes["remote_access.route_settings"] = route
    if not _save(config_edit, changes):
        print("設定を保存できなかったため、遠隔接続は設定していません。mcs_setup.py check で確認してください。")
        return 1
    # The workspace starts listening before the port is published, so the tailnet only ever reaches it.
    status = services(args)
    if status != 0:
        return status
    # launchd listing the agent is not proof that it listens: another program may hold the port while the
    # agent is down, or an earlier workspace may still be running with other settings. Publish only this one.
    if not _workspace_listening(port, host):
        print(f"常駐サーバーが {port} 番で応答していることを確認できなかったため、tailnet には公開していません。"
              "mcs_setup.py check で常駐サービスの状態を確かめ、少し待ってから mcs setup remote をもう一度実行してください。"
              "別のプログラムがこの番号を使っているときは、`tailscale serve status` で公開先を確かめ、"
              "--port で空いている番号を指定してください。")
        return 1
    # `serve` publishes to this tailnet only. `funnel` would publish to the internet and is never used.
    served = _run(cli, "serve", "--bg", f"http://127.0.0.1:{port}")
    if served is None or served.returncode != 0:
        restored = False
        if current.get("port") not in (None, port):
            # The published address still points at the old port: put the workspace back there.
            restore = {f"remote_access.{key}": current.get(key) for key in ("tailnet_host", "port", "route_settings")}
            restored = _save(config_edit, restore) and services(args) == 0
        if restored:
            # Publishing the new port by hand now would point the tailnet at a port nothing listens on.
            print(f"tailnet への公開を {port} 番に変更できなかったため、これまでの {current['port']} 番の公開に戻しました。"
                  f"Tailscale の状態を確かめてから、mcs setup remote --port {port} をもう一度実行してください。")
        else:
            print("tailnet への公開を設定できませんでした。Tailscale の管理画面で HTTPS 証明書を有効にし、"
                  f"`tailscale serve --bg http://127.0.0.1:{port}` を実行してください。")
        return 1
    if status == 0:
        print(f"遠隔接続を設定しました。同じ tailnet の端末から https://{host}/ を開いてください。")
        print("最初に開いた人のログインが最初の職員になります。ほかの職員は職員の画面で Tailscale のログイン名を登録してください。")
    return status


def add_parser(sub, services) -> None:
    description = "MCS 訪問薬剤管理を同じ tailnet の端末から使えるようにします。現時点では利用を推奨していません。"
    p = sub.add_parser("remote", help=description, description=description)
    p.add_argument("--port", type=int, help=f"この Mac の中だけで待ち受ける番号（既定 {REMOTE_ACCESS_PORT}）")
    p.add_argument("--route-settings", help="訪問ルートの設定ファイル（所有者だけが読めるJSON）")
    p.add_argument("--off", action="store_true", help="遠隔接続をやめ、常駐サーバーを止めます")
    p.set_defaults(fn=lambda args: command(args, services=services))
