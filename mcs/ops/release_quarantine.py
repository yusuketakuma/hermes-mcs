"""v3 quarantine launch / runtime observation (M6/W10): 実deps面。

release_runtime_proof.advance_runtime が要求するdepsを、実process/実ファイルで
実装する。spawn・OS process観測・時計は注入可能で、合成試験は実processを立てない。

信頼鎖: 親が決定論的receipt_id/startup_nonceをenvで子へ渡し、子のtrusted
bootstrap(runtime_entry採用経路)が検証後にwitness fileを書く。親は
witnessのbinding一致＋proc_info(pid)の生死/birth独立照合＋loaded source期待値
照合を要求し、子の自己申告単独を証拠にしない(SEM22/23)。
"""
import hashlib
import json
import os

from mcs.ops.release_auto_owner import OwnerError

WITNESS_SCHEMA = 'mcs.quarantine-witness.v3'
HEARTBEAT_SCHEMA = 'mcs.quarantine-heartbeat.v3'
SERVICES_SCHEMA = 'mcs.quarantine-services.v3'
MAX_WITNESS_BYTES = 64 * 1024


def quarantine_argv(python, bootstrap, pycache_prefix):
    """`python -I -S -X pycache_prefix=<prefix> <bootstrap>` のargvを構築する。

    -I: isolated(-E -s含む・PYTHON*環境変数/PYTHONPATH/PYTHONHOMEを無効化)
    -S: site初期化を遅延(bootstrapがQ取得・検証後に明示設定)
    pycache_prefixはCLI引数で確定し、子側runtime_entryがsys.pycache_prefixと
    照合する(§3.4: -B単独やsourceless pycを証拠にしない)。
    """
    if not (isinstance(python, str) and python
            and isinstance(bootstrap, str) and bootstrap
            and isinstance(pycache_prefix, str) and pycache_prefix):
        raise OwnerError('LAUNCH_SPEC_INVALID')
    return [python, '-I', '-S', '-X', f'pycache_prefix={pycache_prefix}',
            bootstrap]


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def measure_tree(root):
    """source treeのmanifest digest: sorted(relpath, file sha256)のcanonical hash。"""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(('.pyc', '.pyo')):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            entries.append([rel, _sha256_file(full)])
    canonical = json.dumps(entries, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()


class QuarantineDeps:
    """release_runtime_proof.advance_runtime向けの実deps実装。

    state_dir: witness/heartbeat/fenceを置くprivate dir(永続・sync済み前提)
    services_path: {service_id: {'kind': ..., 'entrypoint': ...}} のservices manifest
    generation_root: 対象世代のsource tree root(loaded_source/manifest計測元)
    bootstrap: 子が読む固定bootstrap path
    python: interpreter path(既定 sys.executable相当を注入)
    """

    def __init__(self, *, state_dir, services_path, generation_root,
                 bootstrap, python, spawner, proc_info, now_ms,
                 pycache_prefix, boot_id, fence_path=None):
        self.state_dir = state_dir
        self.services_path = services_path
        self.generation_root = generation_root
        self.bootstrap = bootstrap
        self.python = python
        self.spawner = spawner          # (argv, env) -> obj with .pid
        self.proc_info = proc_info      # pid -> {'alive':bool,'birth':hex}|None
        self.now_ms = now_ms
        self.pycache_prefix = pycache_prefix
        self._boot_id = boot_id
        self.fence_path = fence_path

    # -- services / fences ----------------------------------------------------

    def service_desires(self):
        try:
            with open(self.services_path, 'rb') as f:
                raw = f.read(MAX_WITNESS_BYTES + 1)
        except OSError:
            raise OwnerError('SERVICE_SET_UNKNOWN') from None
        if len(raw) > MAX_WITNESS_BYTES:
            raise OwnerError('SERVICE_SET_UNKNOWN')
        try:
            doc = json.loads(raw)
        except ValueError:
            raise OwnerError('SERVICE_SET_UNKNOWN') from None
        if (not isinstance(doc, dict)
                or doc.get('schema_version') != SERVICES_SCHEMA
                or not isinstance(doc.get('services'), dict)
                or not doc['services']):
            raise OwnerError('SERVICE_SET_UNKNOWN')
        desires = {}
        for sid, spec in doc['services'].items():
            kind = spec.get('kind') if isinstance(spec, dict) else None
            if kind not in ('resident', 'timer', 'disabled'):
                raise OwnerError('SERVICE_SET_UNKNOWN')
            desires[sid] = kind
        return desires

    def release_update_fence(self, generation):
        """自分の更新fenceを冪等解放。未存在=解放済みとして成功扱い。"""
        if self.fence_path is None:
            return
        try:
            os.unlink(self.fence_path)
        except FileNotFoundError:
            pass

    # -- launch ---------------------------------------------------------------

    def _env(self, active, receipt_id):
        env = {
            'MCS_Q_RECEIPT_ID': receipt_id,
            'MCS_Q_OPERATION_ID': active['operation_id'],
            'MCS_Q_STARTUP_NONCE': active['startup_nonce'],
            'MCS_Q_GENERATION': str(active['candidate_generation']),
            'MCS_Q_BOOT_ID': self._boot_id,
            'MCS_Q_PYCACHE_PREFIX': self.pycache_prefix,
            'PYTHONPATH': '', 'PYTHONHOME': '',
        }
        return env

    def quarantine_launch(self, active, receipt_id):
        """全managed serviceをquarantine argvでspawnする。receipt_idで冪等:
        既にwitnessが揃うserviceは再spawnしない(部分起動crashの収束)。
        子はruntime_entry.acquire()で自らQ SHを取る(Qは親が解放済み)。"""
        desires = self.service_desires()
        argv = quarantine_argv(self.python, self.bootstrap, self.pycache_prefix)
        for sid in sorted(desires):
            witness_path = self._witness_path(receipt_id, sid)
            if os.path.exists(witness_path):
                continue  # 冪等: 既起動serviceは触らない
            env = self._env(active, receipt_id)
            env['MCS_Q_SERVICE_ID'] = sid
            env['MCS_Q_SERVICE_KIND'] = desires[sid]
            self.spawner(list(argv), env)

    # -- observation ----------------------------------------------------------

    def _witness_dir(self, receipt_id):
        return os.path.join(self.state_dir, 'quarantine', receipt_id)

    def _witness_path(self, receipt_id, service_id):
        return os.path.join(self._witness_dir(receipt_id),
                            f'{service_id}.json')

    def _heartbeat_path(self, receipt_id, service_id):
        return os.path.join(self._witness_dir(receipt_id),
                            f'heartbeat-{service_id}.jsonl')

    def _read_json(self, path):
        try:
            with open(path, 'rb') as f:
                raw = f.read(MAX_WITNESS_BYTES + 1)
        except OSError:
            return None
        if len(raw) > MAX_WITNESS_BYTES:
            return None
        try:
            doc = json.loads(raw)
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None

    def _heartbeats(self, receipt_id, service_id):
        path = self._heartbeat_path(receipt_id, service_id)
        try:
            with open(path, 'rb') as f:
                raw = f.read(MAX_WITNESS_BYTES + 1)
        except OSError:
            return []
        if len(raw) > MAX_WITNESS_BYTES:
            return []
        times = []
        for line in raw.splitlines():
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            at = doc.get('heartbeat_at') if isinstance(doc, dict) else None
            if isinstance(at, int):
                times.append(at)
        return times

    def _check_witness(self, witness, receipt_id, sid, active, loaded_expect):
        """witnessがこのoperation/generation/bootに束縛されるか検査する。"""
        if (witness.get('schema_version') != WITNESS_SCHEMA
                or witness.get('receipt_id') != receipt_id
                or witness.get('service_id') != sid
                or witness.get('operation_id') != active['operation_id']
                or witness.get('startup_nonce') != active['startup_nonce']
                or witness.get('runtime_generation')
                    != active['candidate_generation']
                or witness.get('boot_id') != self._boot_id
                or witness.get('loaded_source_sha256') != loaded_expect
                or not isinstance(witness.get('process_instance_id'), str)
                or not witness['process_instance_id']):
            raise OwnerError('SERVICE_PROOF_MISMATCH')

    def probe_runtime(self, active):
        """activeのreceiptに束縛されたwitness+独立proc観測+heartbeatを返す。"""
        receipt_id = active['activation_receipt_id']
        if not isinstance(receipt_id, str) or not receipt_id:
            raise OwnerError('SERVICE_PROOF_MISSING')
        desires = self.service_desires()
        loaded_expect = self.loaded_source_expect(active)
        services = {}
        for sid in sorted(desires):
            witness = self._read_json(self._witness_path(receipt_id, sid))
            if witness is None:
                raise OwnerError('SERVICE_PROOF_MISSING')
            self._check_witness(witness, receipt_id, sid, active, loaded_expect)
            pid = witness.get('pid')
            info = self.proc_info(pid) if isinstance(pid, int) else None
            if (not isinstance(info, dict) or info.get('alive') is not True
                    or info.get('birth') != witness.get('birth_witness')):
                raise OwnerError('PROCESS_EVIDENCE_MISMATCH')
            beats = self._heartbeats(receipt_id, sid)
            services[sid] = {
                'state': witness.get('state'),
                'process_instance_id': witness.get('process_instance_id'),
                'pid': pid,
                'birth_witness': witness.get('birth_witness'),
                'loaded_source_sha256': witness.get('loaded_source_sha256'),
                'previous_heartbeat_at': beats[-1] if beats else None,
            }
        # boot_idはwitness内の値でなくdeps固定値を使う(子申告を信用しない)
        return {'boot_id': self._boot_id,
                'loaded_source_sha256': loaded_expect,
                'services': services}

    def heartbeat_observations(self, active):
        receipt_id = active['activation_receipt_id']
        if not isinstance(receipt_id, str) or not receipt_id:
            return []
        desires = self.service_desires()
        out = []
        for sid in sorted(desires):
            for at in self._heartbeats(receipt_id, sid):
                out.append({'service_id': sid, 'heartbeat_at': at})
        return out

    def loaded_source_expect(self, active):
        return measure_tree(self.generation_root)

    def measure_source_manifest(self):
        return self.loaded_source_expect(None)
