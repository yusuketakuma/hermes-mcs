"""v3 journal参照: 既存 update_state.json への明示版付きvariant記録。

spec 03_CONTRACTS file表: 既存journalへv3 operation参照を載せる。
勝手に全体初期化しない — load_stateで既存stateを保持したまま
`v3` variant節をmergeしてsave_stateで原子的にpublishする。
ownerが権限消費・終結の正本、journalはeffect位置の補助(03_CONTRACTS)。

variant形: state['v3'] = {'schema_version': 'mcs.update-journal-v3.v1',
                          'applied': {operation_id: record}}
appliedはoperation_id keyed dict — 同一opの再記録は上書き(冪等)。
update_journal.valid_stateは未知top-level keyを拒否しないため、
v1 readerはvariantを無視して従来どおり読める。
corrupt journalは上書きせずJOURNAL_CORRUPTで拒否(fail-closed)。
呼出側(apply)はU lock保持中に呼ぶ — save_stateの前提と一致。
"""
import os

import update_journal

JOURNAL_V3_SCHEMA = 'mcs.update-journal-v3.v1'
MAX_RECORDS = 256


class JournalError(RuntimeError):
    """journal variant記録失敗。codeは固定token。"""

    def __init__(self, code='JOURNAL_FAILED'):
        super().__init__(code)
        self.code = code


def _check_record(record):
    if not isinstance(record, dict):
        raise JournalError('JOURNAL_RECORD')
    op = record.get('operation_id')
    if (not isinstance(op, str) or not op
            or not isinstance(record.get('event'), str)
            or not record['event']
            or type(record.get('applied_at')) is not int):
        raise JournalError('JOURNAL_RECORD')


def apply_journal(record, *, state_path):
    """recordを既存journalのv3 variantへ冪等追記する。

    戻り値: 保存したrecordの写し。corrupt/読取不能/容量超過は例外。
    """
    _check_record(record)
    state = update_journal.load_state(state_path)
    if state.get('_corrupt'):
        raise JournalError('JOURNAL_CORRUPT')
    variant = state.get('v3')
    if not isinstance(variant, dict) \
            or variant.get('schema_version') != JOURNAL_V3_SCHEMA \
            or not isinstance(variant.get('applied'), dict):
        variant = {'schema_version': JOURNAL_V3_SCHEMA, 'applied': {}}
    applied = dict(variant['applied'])
    if record['operation_id'] not in applied \
            and len(applied) >= MAX_RECORDS:
        # bounded保持: 最古挿入順から退避(owner正本が権威、journalは補助)
        applied.pop(next(iter(applied)))
    applied[record['operation_id']] = dict(record)
    state['v3'] = {'schema_version': JOURNAL_V3_SCHEMA,
                   'applied': applied}
    try:
        update_journal.save_state(state_path, state)
    except JournalError:
        raise
    except Exception as e:
        raise JournalError('JOURNAL_IO') from e
    return dict(record)


def journal_state_path(data_dir):
    """v3 journal参照のcanonical path(update_lifecycle_context.STATE_PATHと同じ)。"""
    return os.path.join(os.fspath(data_dir), 'update_state.json')
