"""quarantine子processの -I -S bootstrap (M6/W10)。

`python -I -S -X pycache_prefix=<gen> <このfile>` として起動され、
MCS_Q_* env bindingを検証→runtime_entryでQ SH取得→owner.active照合→
witness/heartbeatを確立してからservice mainへ渡す。

app constructor/importで外部通信・schema migration・自動restore・通知を
始めない(02_DEVICE_PROTOCOL §4): service mainはwitness確立後にだけ呼び、
呼出側はobserve専用契約を守ること。binding不一致・pycache不一致・
owner不整合は全て起動拒否(fail-closed)。
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid

_ENV = 'MCS_Q_'
_REQUIRED = ('RECEIPT_ID', 'OPERATION_ID', 'STARTUP_NONCE', 'GENERATION',
             'BOOT_ID', 'PYCACHE_PREFIX', 'SERVICE_ID', 'SERVICE_KIND',
             'RUNTIME_HOME', 'STATE_DIR', 'SOURCE_ROOT')
_UUID4 = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_KINDS = ('resident', 'timer', 'disabled')
_ACTIVE_PHASES = ('APPLIED', 'CANDIDATE_READY', 'ACTIVATING')
_HEARTBEAT_SCHEMA = 'mcs.quarantine-heartbeat.v3'
_WITNESS_SCHEMA = 'mcs.quarantine-witness.v3'
MAX_DOC = 64 * 1024


class BootstrapError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _env(env, name):
    key = _ENV + name
    value = env.get(key)
    if not isinstance(value, str) or not value:
        raise BootstrapError('ENV_MISSING:' + name)
    return value


def validate_env(env):
    """MCS_Q_* bindingを検査して正規化dictを返す。"""
    out = {name: _env(env, name) for name in _REQUIRED}
    for name in ('RECEIPT_ID', 'OPERATION_ID'):
        if _UUID4.fullmatch(out[name]) is None:
            raise BootstrapError('ENV_INVALID:' + name)
    if _SHA256.fullmatch(out['STARTUP_NONCE']) is None:
        raise BootstrapError('ENV_INVALID:STARTUP_NONCE')
    if _SHA256.fullmatch(out['BOOT_ID']) is None:
        raise BootstrapError('ENV_INVALID:BOOT_ID')
    if not out['GENERATION'].isdigit() or int(out['GENERATION']) < 1:
        raise BootstrapError('ENV_INVALID:GENERATION')
    out['GENERATION'] = int(out['GENERATION'])
    if out['SERVICE_KIND'] not in _KINDS:
        raise BootstrapError('ENV_INVALID:SERVICE_KIND')
    return out


def check_pycache(expected):
    """-X pycache_prefix(CLI)とsys.pycache_prefix(runtime)の両点一致を要求。"""
    actual = sys.pycache_prefix or ''
    if actual != expected:
        raise BootstrapError('PYCACHE_MISMATCH')


def enter_source_path(source_root):
    """-Sでsite未初期化のため、source rootのmcs/を明示登録してから
    _mcs_pathでflat import rootを全登録する。"""
    mcs_dir = os.path.join(source_root, 'mcs')
    if not os.path.isdir(mcs_dir):
        raise BootstrapError('SOURCE_ROOT_INVALID')
    if mcs_dir not in sys.path:
        sys.path.insert(0, mcs_dir)
    import _mcs_path  # noqa: F401  (flat roots + checkout rootを登録)


def verify_entry(entry, b):
    """runtime_entryがv3管理下で、active operationがenv bindingと一致するか。"""
    if getattr(entry, 'managed', False) is not True:
        raise BootstrapError('NOT_MANAGED')
    owner = getattr(entry, 'owner', None)
    active = owner.get('active') if isinstance(owner, dict) else None
    if not isinstance(active, dict):
        raise BootstrapError('ACTIVE_MISSING')
    if (active.get('operation_id') != b['OPERATION_ID']
            or active.get('startup_nonce') != b['STARTUP_NONCE']
            or active.get('activation_receipt_id') != b['RECEIPT_ID']
            or active.get('candidate_generation') != b['GENERATION']
            or active.get('phase') not in _ACTIVE_PHASES):
        raise BootstrapError('BINDING_MISMATCH')
    return active


def default_birth(pid):
    """native birth witness: ps lstart(開始時刻)をpidと共にsha256化。

    sysctl kern.proc.pidは現行macOSに存在しないためpsを使う。
    quarantine envはPATHを持たないので/bin/ps絶対パス。親側proc_infoも
    同じ計算を使うため値は一致する。失敗は隠さずBootstrapErrorにする。
    """
    import subprocess
    try:
        out = subprocess.run(['/bin/ps', '-o', 'lstart=', '-p', str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise BootstrapError('BIRTH_UNAVAILABLE') from None
    value = out.stdout.strip()
    if out.returncode != 0 or not value:
        raise BootstrapError('BIRTH_UNAVAILABLE')
    return hashlib.sha256(f'{pid}|{value}'.encode()).hexdigest()


def _atomic_write(path, raw: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def write_witness(state_dir, b, *, pid, birth, process_instance_id,
                  loaded_source_sha256, state, observed_at):
    """quarantine witnessをatomicに書く。親の_check_witnessと同形。"""
    doc = {'schema_version': _WITNESS_SCHEMA,
           'receipt_id': b['RECEIPT_ID'], 'service_id': b['SERVICE_ID'],
           'operation_id': b['OPERATION_ID'],
           'startup_nonce': b['STARTUP_NONCE'],
           'runtime_generation': b['GENERATION'], 'boot_id': b['BOOT_ID'],
           'loaded_source_sha256': loaded_source_sha256,
           'process_instance_id': process_instance_id,
           'pid': pid, 'birth_witness': birth, 'state': state,
           'observed_at': observed_at}
    raw = json.dumps(doc, separators=(',', ':')).encode()
    if len(raw) > MAX_DOC:
        raise BootstrapError('WITNESS_TOO_LARGE')
    path = os.path.join(state_dir, 'quarantine', b['RECEIPT_ID'],
                        b['SERVICE_ID'] + '.json')
    _atomic_write(path, raw)
    return path


def append_heartbeat(state_dir, b, at):
    path = os.path.join(state_dir, 'quarantine', b['RECEIPT_ID'],
                        f"heartbeat-{b['SERVICE_ID']}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps({'schema_version': _HEARTBEAT_SCHEMA,
                       'service_id': b['SERVICE_ID'],
                       'heartbeat_at': at}, separators=(',', ':')) + '\n'
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def heartbeat_loop(state_dir, b, *, interval_s=5.0, now_ms=None,
                   stop=None):
    """fresh heartbeatを定期appendするdaemon loop。"""
    now_ms = now_ms or (lambda: int(time.time() * 1000))
    stop = stop if stop is not None else threading.Event()
    while not stop.is_set():
        append_heartbeat(state_dir, b, now_ms())
        stop.wait(interval_s)


def run(env=None, *, acquire=None, birth_fn=None, main=None,
        process_instance_id=None, now_ms=None):
    """bootstrap本体。binding検証→Q SH→witness/heartbeat→service main。

    acquire/birth_fn/main/now_msは注入可能(合成試験は実processを立てない)。
    """
    env = os.environ if env is None else env
    b = validate_env(env)
    check_pycache(b['PYCACHE_PREFIX'])
    enter_source_path(b['SOURCE_ROOT'])
    if acquire is None:
        import runtime_entry

        def acquire(home):
            return runtime_entry.acquire(home)
    entry = acquire(b['RUNTIME_HOME'])
    try:
        verify_entry(entry, b)
        if birth_fn is None:
            birth_fn = default_birth
        pid = os.getpid()
        birth = birth_fn(pid)
        if not isinstance(birth, str) or _SHA256.fullmatch(birth) is None:
            raise BootstrapError('BIRTH_INVALID')
        from mcs.ops.release_quarantine import measure_tree
        loaded = measure_tree(b['SOURCE_ROOT'])
        state_name = {'resident': 'running', 'timer': 'armed',
                      'disabled': 'stopped'}[b['SERVICE_KIND']]
        now_ms = now_ms or (lambda: int(time.time() * 1000))
        piid = str(process_instance_id or uuid.uuid4())
        if _UUID4.fullmatch(piid) is None:
            raise BootstrapError('INSTANCE_ID_INVALID')
        write_witness(b['STATE_DIR'], b, pid=pid, birth=birth,
                      process_instance_id=piid,
                      loaded_source_sha256=loaded, state=state_name,
                      observed_at=now_ms())
        stop = threading.Event()
        thread = threading.Thread(
            target=heartbeat_loop,
            args=(b['STATE_DIR'], b),
            kwargs={'interval_s': 5.0, 'now_ms': now_ms, 'stop': stop},
            daemon=True)
        thread.start()
        append_heartbeat(b['STATE_DIR'], b, now_ms())  # 初回即時
        if main is not None:
            try:
                main(env, entry)
            finally:
                stop.set()
        return entry
    except BaseException:
        try:
            entry.close()
        except Exception:
            pass
        raise
