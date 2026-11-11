"""v3 private witness deps面: PrivatePreparedWitnessのprivate項目合成。

ownerが`witness_private(active)`へ要求するのはschema_version/publicを除く
全項目。最終的なschema検証はowner側の`contract.parse_document`が担うため、
本moduleは証拠源providerから値を集め、欠落・None・空値をfail-closedで拒否する
合成層に徹する(二重のschema検証はしない)。

各証拠源はfactory注入:
- file_witnesses(): {'locks':[PrivateFileWitness×8], 'configs':[...×1..64]}
  — G/U/Q/R/C/A/M/O各lock fileとconfig fileの{logical_id,identity,sha256}。
  Q EX保持中のfresh stat+内容hashが契約(spec 03: lock_fingerprints再照合)。
- source_facts(): {'source_tree_sha256','runtime_fingerprint'}
- ledger_stat(): ledger DBのstat(dictまたはos.stat_result)
- stage_stat(active): {'path','identity'} — staged artifactの
  FileIdentity+path(active['target']から導出)
- backup_detail(): {'path','sha256'} — backup_evidenceが発行した私有記録の
  実path/hash(wireへは出さない。uuidはpublic側の責務)
- retired_processes(): RetiredProcess[] — await_retirement証拠または
  Q EX下の再probeからservice_id/process_instance_id/generation/boot_id/
  birth_witness/retired=trueを組み立てたもの
- required_free_bytes(): Uint — stage+予約を含む必要空き容量
- reserved_owner_bytes(): 1..8MiB — LocalFacts等から
- active['prepare_deadline'] → prepared_until
"""
from adapters.sdc import release_auto_state as auto_state


class WitnessError(RuntimeError):
    """private witness合成失敗。codeは固定token。"""

    def __init__(self, code='WITNESS_FAILED'):
        super().__init__(code)
        self.code = code


def _need(value, code='WITNESS_MISSING'):
    if value is None or value == '' or value == []:
        raise WitnessError(code)
    return value


_IDENTITY_KEYS = ('device', 'inode', 'uid', 'mode', 'nlink', 'size',
                  'mtime_ns', 'ctime_ns')
_STAT_ATTRS = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink',
               'st_size', 'st_mtime_ns', 'st_ctime_ns')


# UInt64Decimalは10進string・Uintはint — FileIdentityのwire表現に合わせる。
_U64_FIELDS = ('device', 'inode', 'mtime_ns', 'ctime_ns')


def _stat_fields(st):
    """os.stat_resultまたはFileIdentity形dictをschema表現へ正規化。"""
    try:
        if isinstance(st, dict):
            raw = {k: st[k] for k in _IDENTITY_KEYS}
        else:
            raw = {k: getattr(st, a)
                   for k, a in zip(_IDENTITY_KEYS, _STAT_ATTRS, strict=True)}
    except (KeyError, AttributeError, TypeError) as e:
        raise WitnessError('WITNESS_STAT') from e
    for k in _U64_FIELDS:
        v = raw[k]
        if type(v) is int and v >= 0:
            raw[k] = str(v)
        elif not (isinstance(v, str) and v.isdigit() and v == str(int(v))):
            raise WitnessError('WITNESS_STAT')
    return raw


class PrivateWitness:
    """deps契約の`witness_private(active)`面。

    store: auto_state.load可能なv3 Store(root_fingerprint照合に使用)。
    上記providerは全て呼出毎に評価する(cacheしない — Q EX下のfresh証拠が契約)。
    """

    def __init__(self, store, *, file_witnesses, source_facts, ledger_stat,
                 stage_stat, backup_detail, retired_processes,
                 required_free_bytes, reserved_owner_bytes):
        self._store = store
        self._providers = {
            'file_witnesses': file_witnesses, 'source_facts': source_facts,
            'ledger_stat': ledger_stat, 'stage_stat': stage_stat,
            'backup_detail': backup_detail,
            'retired_processes': retired_processes,
            'required_free_bytes': required_free_bytes,
            'reserved_owner_bytes': reserved_owner_bytes}

    def _call(self, name, *args):
        try:
            return self._providers[name](*args)
        except WitnessError:
            raise
        except Exception as e:
            raise WitnessError('WITNESS_' + name.upper()) from e

    def witness_private(self, active):
        if not isinstance(active, dict):
            raise WitnessError('WITNESS_ACTIVE')
        deadline = active.get('prepare_deadline')
        if type(deadline) is not int or deadline <= 0:
            raise WitnessError('WITNESS_DEADLINE')

        owner = auto_state.load(self._store)

        files = self._call('file_witnesses')
        if not isinstance(files, dict):
            raise WitnessError('WITNESS_FILES')
        locks = _need(files.get('locks'))
        configs = _need(files.get('configs'))
        if len(locks) != 8:
            raise WitnessError('WITNESS_LOCKS')
        for w in list(locks) + list(configs):
            if (not isinstance(w, dict)
                    or not isinstance(w.get('logical_id'), str)
                    or not w['logical_id']
                    or not isinstance(w.get('sha256'), str)
                    or len(w['sha256']) != 64):
                raise WitnessError('WITNESS_FILES')
            # providerはos.stat_resultまたはFileIdentity形dictを返せる
            w['identity'] = _stat_fields(w.get('identity'))

        src = self._call('source_facts')
        if not isinstance(src, dict):
            raise WitnessError('WITNESS_SOURCE')
        source_tree = _need(src.get('source_tree_sha256'))
        runtime_fp = _need(src.get('runtime_fingerprint'))
        for v in (source_tree, runtime_fp):
            if not isinstance(v, str) or len(v) != 64:
                raise WitnessError('WITNESS_SOURCE')

        led = _stat_fields(self._call('ledger_stat'))
        # stage_statのみactiveを受け取る — stage artifactはtarget依存のため
        stg = self._call('stage_stat', active)
        if not isinstance(stg, dict):
            raise WitnessError('WITNESS_STAGE')
        stage_path = _need(stg.get('path'))
        stage_ident = stg.get('identity')
        if not isinstance(stage_ident, dict):
            raise WitnessError('WITNESS_STAGE')
        stage_ident = _stat_fields(stage_ident)
        if stage_ident['nlink'] != 1:
            raise WitnessError('WITNESS_STAGE')

        bkp = self._call('backup_detail')
        if not isinstance(bkp, dict):
            raise WitnessError('WITNESS_BACKUP')
        backup_path = _need(bkp.get('path'))
        backup_sha = _need(bkp.get('sha256'))
        if not isinstance(backup_sha, str) or len(backup_sha) != 64:
            raise WitnessError('WITNESS_BACKUP')

        retired = self._call('retired_processes')
        if not isinstance(retired, list):
            raise WitnessError('WITNESS_RETIRED')
        for p in retired:
            if (not isinstance(p, dict) or p.get('retired') is not True
                    or not isinstance(p.get('service_id'), str)
                    or not p['service_id']
                    or not isinstance(p.get('process_instance_id'), str)
                    or not isinstance(p.get('generation'), int)
                    or p['generation'] <= 0
                    or not isinstance(p.get('boot_id'), str)
                    or len(p['boot_id']) != 64
                    or not isinstance(p.get('birth_witness'), str)
                    or len(p['birth_witness']) != 64):
                raise WitnessError('WITNESS_RETIRED')

        required_free = self._call('required_free_bytes')
        if type(required_free) is not int or required_free < 0:
            raise WitnessError('WITNESS_CAPACITY')
        reserved = self._call('reserved_owner_bytes')
        if type(reserved) is not int or not 1 <= reserved <= 8 * 1024 * 1024:
            raise WitnessError('WITNESS_CAPACITY')

        return {'root_fingerprint': owner['root_fingerprint'],
                'repository_fingerprint': owner['repository_fingerprint'],
                'lock_identities': [dict(w) for w in locks],
                'configuration_files': [dict(w) for w in configs],
                'source_tree_sha256': source_tree,
                'runtime_fingerprint': runtime_fp,
                'ledger_device': led['device'],
                'ledger_inode': led['inode'],
                'stage_identity': stage_ident,
                'stage_path': stage_path,
                'backup_path': backup_path,
                'backup_sha256': backup_sha,
                'retired_processes': [dict(p) for p in retired],
                'required_free_bytes': required_free,
                'reserved_owner_bytes': reserved,
                'prepared_until': deadline}
