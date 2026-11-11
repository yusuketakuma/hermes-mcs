"""v3 launchd drain実装: ServiceDrainのper-service機構(hermes agent経路)。

v1 quiesce(update_services)はdrainer停止にlaunchctl bootout+pid不在確認+
stray sweep(TERM→KILL)を使う。v3は仕様02 §6に従い:
- drain対象は起動中のMCS管理agentのみ(resident drainers・設定済み
  LINE WORKS adapter)。既に停止中のserviceは退役対象に含めない
- request_stopはlaunchctl bootoutのみ(強制kill sweepは流用しない)
- 退役証明はagent_absent(launchctl printのpid消失)＋request_stop時に
  捕捉した{pid, native birth}のprocess instance照合。PID単独・
  古いheartbeat単独は証拠にしない
- stray drainer(launchd外のdrainer pattern process)はkillせず、
  存在自体を未退役として扱う(fail-closed。Q SH保持ならQ EXも失敗する)
- gateway/cron child/手動CLI/bookkeepingは本moduleの対象外 —
  marker(stop_new_intake)とQ EXが transient 側の退役を担保する

process_instance_idは{service_id,pid,birth}からの決定論的uuid5、
birth_witnessは同組のcanonical sha256 — 同じ証拠から再計算できるため
witness照合・復旧時の再検証が可能。
"""
import hashlib
import json
import os
import re
import uuid

from release_auto_services import ServiceError


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def birth_witness(service_id, pid, started_at):
    return hashlib.sha256(_canonical(
        {'service_id': service_id, 'pid': pid,
         'started_at': started_at})).hexdigest()


def process_instance_id(service_id, pid, started_at):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f'mcs-auto-v3/{service_id}/{pid}/{started_at}'))


class LaunchdDrain:
    """launchd管理agentのdrain機構。ServiceDrain構築とRetiredProcess写像を提供。

    agent_pid(label)->int|None: launchctl printが報告するpid(非稼働=None、
        照合不能は例外)。
    agent_absent(label)->bool: labelが未ロードかつpid不在を検証済み。
    bootout(label)->None: launchctl bootout(gui/UID/label)。失敗は例外。
    process_started_at(pid)->float|None: native birth(ps lstart相当)。
    stray_drainer_pids()->list[int]|None: launchd外drainerのpid一覧、
        照合不能はNone(=未退役扱い)。
    runtime_generation/boot_id: RetiredProcessへ写す現行値。
    """

    def __init__(self, *, labels, agent_pid, agent_absent, bootout,
                 process_started_at, stray_drainer_pids,
                 runtime_generation, boot_id):
        if (not isinstance(labels, (list, tuple)) or not labels
                or len(set(labels)) != len(labels)
                or any(type(x) is not str or not x for x in labels)):
            raise ServiceError('SERVICE_INVENTORY')
        if (not callable(runtime_generation)
                and (type(runtime_generation) is not int
                     or runtime_generation <= 0)):
            raise ServiceError('SERVICE_INVENTORY')
        self._labels = list(labels)
        self._agent_pid = agent_pid
        self._agent_absent = agent_absent
        self._bootout = bootout
        self._started_at = process_started_at
        self._stray_pids = stray_drainer_pids
        self._generation = runtime_generation
        self._boot_id = boot_id  # callable -> Sha256 (deps.boot_id相当)
        self._captured = {}

    # --- ServiceDrainへ渡す面 ------------------------------------------

    def services(self):
        """起動中の管理agentだけをdrain対象として返す。

        pid観測済みだがnative birthが取れないprocessは退役証明を
        永久に作れないため、inventory時点でfail-closedにする
        (ownerがDEFERREDへ落とす)。
        """
        out = []
        for label in self._labels:
            pid = self._agent_pid(label)
            if pid is None:
                continue
            if type(pid) is not int or pid <= 0:
                raise ServiceError('SERVICE_PID')
            birth = self._started_at(pid)
            if type(birth) not in (int, float):
                raise ServiceError('SERVICE_BIRTH')
            out.append({'id': label, 'kind': 'launchagent',
                        'pid': pid, 'birth': birth})
        return out

    def stopper(self, service):
        label = service['id']
        pid = self._agent_pid(label)
        if pid is None:
            # inventory後に自然停止 — instance未捕捉のままでは
            # RetiredProcessを構成できないためfail-closed
            raise ServiceError('SERVICE_STOP')
        if pid != service.get('pid'):
            # 別instanceへ交代済み — 止めるべき対象が変わった
            raise ServiceError('SERVICE_STOP')
        birth = self._started_at(pid)
        if birth != service.get('birth'):
            raise ServiceError('SERVICE_STOP')
        try:
            self._bootout(label)
        except Exception as e:
            raise ServiceError('SERVICE_STOP') from e
        self._captured[label] = {'pid': pid, 'birth': birth}

    def prober(self, service):
        label = service['id']
        try:
            absent = self._agent_absent(label)
        except Exception:
            return None
        if absent is not True:
            return None
        cap = self._captured.get(label)
        if cap is None:
            # stop要求前に既に不在 — instance証拠がないため未証明
            return None
        return {'service_id': label, 'pid': cap['pid'],
                'birth': cap['birth'], 'retired': True}

    def stray_pids(self):
        """launchd外drainerの有無。None=照合不能(未退役扱い)。"""
        try:
            return self._stray_pids()
        except Exception:
            return None

    def stopped_labels(self):
        """request_stopが実際にbootoutしたlabel(sorted)。"""
        return sorted(self._captured)

    # --- PrivateWitness.retired_processes provider ---------------------

    def retired_processes(self, evidence):
        """await_retirementのevidenceからRetiredProcess[]を組み立てる。

        evidence['services'][id]のproof(pid/birth)を使い、
        service_id/process_instance_id/generation/boot_id/birth_witness/
        retired=trueのschema形へ写す。未証明・stray残存は拒否。
        """
        if not isinstance(evidence, dict):
            raise ServiceError('SERVICE_EVIDENCE')
        rows = evidence.get('services')
        if not isinstance(rows, dict):
            raise ServiceError('SERVICE_EVIDENCE')
        stray = self.stray_pids()
        if stray is None or stray:
            # 照合不能(None)もstray残存も未退役 — fail-closed
            raise ServiceError('SERVICE_STRAY')
        try:
            boot_id = self._boot_id() if callable(self._boot_id) \
                else self._boot_id
            generation = self._generation() if callable(self._generation) \
                else self._generation
        except Exception as e:
            raise ServiceError('SERVICE_EVIDENCE') from e
        if (not isinstance(boot_id, str) or len(boot_id) != 64
                or type(generation) is not int or generation <= 0):
            raise ServiceError('SERVICE_EVIDENCE')
        out = []
        for label in self._labels:
            row = rows.get(label)
            if not isinstance(row, dict) or row.get('retired') is not True:
                continue
            proof = row.get('proof')
            if not isinstance(proof, dict):
                continue
            pid, birth = proof.get('pid'), proof.get('birth')
            if type(pid) is not int or pid <= 0 or type(birth) not in (int, float):
                raise ServiceError('SERVICE_EVIDENCE')
            out.append({
                'service_id': label,
                'process_instance_id': process_instance_id(label, pid, birth),
                'generation': generation,
                'boot_id': boot_id,
                'birth_witness': birth_witness(label, pid, birth),
                'retired': True})
        return out


# --- 実primitive結線 -----------------------------------------------------
#
# launchctl/ps/pgrepを呼ぶのはfactoryが明示した時だけ。update_servicesの
# quiesceはmarker+bootout+stray TERM→KILLを一体で行うが、v3はbootoutのみ
# (strayはkillせず未退役扱い)。rc=113(サービス未検出)のみを「不在」の
# 証拠とし、timeout(124)/起動失敗(127)/その他の異常は全てfail-closed。

_LAUNCHCTL_TIMEOUT_S = 10


def native_launchd_drain(labels, *, uid, runtime_generation, boot_id,
                         run=None):
    """実launchd/ps/pgrep primitive結線のLaunchdDrainを返す。

    uid: launchd gui domainのuid。runtime_generation/boot_idは
    RetiredProcessへ写す現行値(boot_idはcallableまたは値)。
    run: (argv, timeout)->CompletedProcess相当の注入(試験用)。
    既定はlifecycle_io.run_bounded。
    """
    if type(uid) is not int or uid <= 0:
        raise ServiceError('SERVICE_INVENTORY')

    def _run(argv, timeout=_LAUNCHCTL_TIMEOUT_S):
        if run is not None:
            return run(argv, timeout=timeout)
        import lifecycle_io
        return lifecycle_io.run_bounded(argv, timeout=timeout)

    def agent_pid(label):
        r = _run(['launchctl', 'print', f'gui/{uid}/{label}'])
        if r.returncode == 113:
            return None
        if r.returncode != 0:
            raise ServiceError('SERVICE_PROBE')
        m = re.search(r'^\s*pid\s*=\s*(\d+)',
                      getattr(r, 'stdout', '') or '', re.M)
        return int(m.group(1)) if m else None

    def agent_absent(label):
        # 113(未検出)のみが不在の証拠 — timeout等はFalse=未証明
        return _run(['launchctl', 'print',
                     f'gui/{uid}/{label}']).returncode == 113

    def bootout(label):
        r = _run(['launchctl', 'bootout', f'gui/{uid}/{label}'])
        if r.returncode != 0:
            raise ServiceError('SERVICE_STOP')

    def process_started_at(pid):
        import environment_diagnostics
        return environment_diagnostics.process_started_at(pid)

    def stray_drainer_pids():
        # _STRAY_REはupdate_lifecycle_contextが正本 — 重い依存を
        # 関数内importに留める(_standalone_statusと同じ先例)
        from update_lifecycle_context import _STRAY_RE
        import update_discovery
        r = _run(['pgrep', '-u', str(uid), '-f', _STRAY_RE],
                 timeout=update_discovery.LOCAL_TIMEOUT_S)
        if r.returncode not in (0, 1):
            return None
        return [int(p) for p in (getattr(r, 'stdout', '') or '').split()
                if p.isdigit() and int(p) != os.getpid()]

    return LaunchdDrain(
        labels=labels, agent_pid=agent_pid, agent_absent=agent_absent,
        bootout=bootout, process_started_at=process_started_at,
        stray_drainer_pids=stray_drainer_pids,
        runtime_generation=runtime_generation, boot_id=boot_id)


def native_intake_stop():
    """ServiceDrain.stop_new_intakeへ渡す実intake停止を返す。

    既存update marker(update_lifecycle_context._write_marker)を書き、
    cron catchup等のhelperが新規処理を始めないようにする。resident
    drainer自体はrequest_stop側の責務。marker書込み失敗は例外のまま
    ServiceDrainがINTAKE_STOPへ写す。
    """
    def stop():
        from update_lifecycle_context import _write_marker
        _write_marker()
    return stop


def native_restart_same_version(drain, *, agents_dir, run=None):
    """ServiceDrain.restart_same_versionへ渡す実restarterを返す。

    LaunchdDrainがbootoutしたlabelだけを同一plistでbootstrapする
    (prepareはcode/configに触れないため同一版の再起動)。既にloadedな
    labelは触らない=冪等。最後にintake markerを除去して受付を再開する
    (marker除去はupdate_lifecycle_context._remove_markerが正本、
    関数内importで重い依存を遅延)。plist欠落・bootstrap失敗・marker
    除去失敗は全てServiceErrorでfail-closed。
    """
    if not isinstance(drain, LaunchdDrain):
        raise ServiceError('SERVICE_RESTART')
    if not isinstance(agents_dir, str) or not agents_dir:
        raise ServiceError('SERVICE_RESTART')

    def _run(argv):
        if run is not None:
            return run(argv)
        import lifecycle_io
        return lifecycle_io.run_bounded(argv, timeout=_LAUNCHCTL_TIMEOUT_S)

    def restart():
        from service_management import launchd_bootstrap
        for label in drain.stopped_labels():
            plist = os.path.join(agents_dir, label + '.plist')
            if not os.path.isfile(plist):
                raise ServiceError('SERVICE_RESTART')
            # absent(rc=113)のlabelだけbootstrap — loadedは既に稼働中
            try:
                absent = drain._agent_absent(label)
            except Exception as e:
                raise ServiceError('SERVICE_RESTART') from e
            if absent is not True:
                continue
            if launchd_bootstrap(label, plist, _run) is not None:
                raise ServiceError('SERVICE_RESTART')
        try:
            from update_lifecycle_context import MARKER_PATH, _remove_marker
            _remove_marker()
            if os.path.lexists(MARKER_PATH):
                raise ServiceError('SERVICE_RESTART')
        except ServiceError:
            raise
        except Exception as e:
            raise ServiceError('SERVICE_RESTART') from e

    return restart
