"""v3 backup evidence deps面: local_backup_evidence_idの発行。

spec 02 §prepare: 既存整合backup経路(backup_evidence.status)でローカル復旧証拠を
確認し、opaqueなuuidを発行する。実hash・backup path・状態詳細は端末内だけに
留め、wireへはuuidだけを送る(INV12/SEM16)。

判定は `within_rpo is True` かつ `last_attempt_failed` でないこと — policyの
max_rpo_secondsが新鮮さの正本。status読取失敗・記録破損・RPO超過・直近失敗は
全て例外でfail-closedし、証拠なしのprepareを許さない。

uuid→検査記録の対応表はevidence_dir配下の私有file(O_EXCL/0600)に保存する。
対応表上限を超えたら黙って古い記録を消さずfail-closedする。
"""
import json
import os
import uuid
from pathlib import Path

import backup_evidence


class BackupEvidenceError(RuntimeError):
    """backup evidence発行失敗。codeは固定token。"""

    def __init__(self, code='BACKUP_EVIDENCE_FAILED'):
        super().__init__(code)
        self.code = code


class BackupEvidence:
    """deps契約 `backup_evidence() -> uuid str` の実装。"""

    def __init__(self, *, state_dir, policy, evidence_dir, now_ms,
                 max_entries=256):
        self._state_dir = state_dir
        self._policy = policy
        self._dir = Path(evidence_dir)
        self._now_ms = now_ms
        self._max_entries = max_entries

    def __call__(self):
        try:
            status = backup_evidence.status(self._state_dir, self._policy)
        except Exception as e:
            raise BackupEvidenceError('BACKUP_STATUS') from e
        if (not isinstance(status, dict)
                or status.get('within_rpo') is not True
                or status.get('last_attempt_failed') is not False):
            raise BackupEvidenceError('BACKUP_NOT_FRESH')
        try:
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if sum(1 for _ in self._dir.iterdir()) >= self._max_entries:
                raise BackupEvidenceError('BACKUP_EVIDENCE_FULL')
            evidence_id = str(uuid.uuid4())
            record = {'schema_version': 'mcs.backup-evidence.v3',
                      'evidence_id': evidence_id,
                      'checked_at_ms': self._now_ms(),
                      'policy_id': status.get('policy_id'),
                      'rpo_seconds': status.get('rpo_seconds'),
                      'bundle_age_seconds': status.get('bundle_age_seconds'),
                      'within_rpo': True}
            path = self._dir / (evidence_id + '.json')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(record, f, separators=(',', ':'), sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            dir_fd = os.open(self._dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BackupEvidenceError:
            raise
        except Exception as e:
            raise BackupEvidenceError('BACKUP_EVIDENCE_IO') from e
        return evidence_id
