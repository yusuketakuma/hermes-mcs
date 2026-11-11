"""v3 owner depsのholds面: 永続化された未解決hold源の列挙。

spec 02: 別のupdate/restore hold・Human/restore/update holdを新更新の
成功で消さない。個別送信unknown/課金保留は更新を妨げない(spec明示)。
列挙源はv1更新journalの未解決applying/破損と、restore系のcanonical
writer hold(`notify_spool.restore_writer_hold`)のみ — backup restoreの
adopted delivery hold等、writerを止めないholdは対象外。
"""
import os

import notify_spool
import update_journal


def unresolved_holds(data_dir):
    """未解決hold名の一覧。空=hold無し。読取不能はhold扱い(fail-closed)。"""
    holds = []
    state = update_journal.load_state(os.path.join(data_dir, 'update_state.json'))
    if state.get('_corrupt'):
        holds.append('update_state_unreadable')
    elif state.get('applying') is not None:
        holds.append('update_applying')
    if notify_spool.restore_writer_hold(data_dir) is not None:
        holds.append('restore_hold')
    return holds


class NativeHolds:
    """deps契約のholds()面。data_dirはfactoryが配備構成から注入する。"""

    def __init__(self, data_dir):
        self._data_dir = os.fspath(data_dir)

    def holds(self):
        return unresolved_holds(self._data_dir)
