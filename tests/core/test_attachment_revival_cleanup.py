"""Relisting a withdrawn attachment after cleanup needs an actual payload."""
import hashlib

import pytest

from ingest_testkit import _ledger, _message
from mcs_adapter import Attachment


def _stored_attachment(tmp_path, *, reply=False):
    db = _ledger(tmp_path)
    if reply:
        db.save_messages([_message(mid=1)])
    m = _message(mid=2 if reply else 1, parent_id=1 if reply else None)
    m.files_present = True
    m.attachments = [Attachment('synthetic-file', 'synthetic.pdf', 'https://synthetic.invalid/file')]
    db.save_messages([m])
    aid = db.db.execute('SELECT attachment_id FROM attachments').fetchone()[0]
    path = tmp_path / 'attachments' / str(aid)
    path.parent.mkdir()
    path.write_bytes(b'synthetic attachment')
    db.attachment_saved(aid, str(path), path.stat().st_size,
                        hashlib.sha256(path.read_bytes()).hexdigest())
    m.attachments = []
    db.save_messages([m])
    assert db.db.execute('SELECT state FROM attachments').fetchone()[0] == 'withdrawn'
    m.attachments = [Attachment('synthetic-file', 'synthetic.pdf', 'https://synthetic.invalid/file')]
    return db, m, aid, path


@pytest.mark.parametrize('reply', [False, True])
def test_relist_missing_withdrawn_payload_returns_to_fetch_queue(tmp_path, reply):
    db, m, aid, path = _stored_attachment(tmp_path, reply=reply)
    try:
        # Cleanup committed withdrawn and unlinked raw, but crashed before
        # NULLing the path. Relisting must not trust the retained path string.
        path.unlink()
        db.save_messages([m])
        row = db.db.execute('SELECT state,attempts,next_try,error FROM attachments').fetchone()
        assert tuple(row) == ('pending', 0, None, None)
        assert [item['attachment_id'] for item in db.attachments_due()] == [aid]
        path.write_bytes(b'synthetic replacement')
        db.attachment_saved(aid, str(path), path.stat().st_size,
                            hashlib.sha256(path.read_bytes()).hexdigest())
        assert db.db.execute('SELECT state FROM attachments').fetchone()[0] == 'downloaded'
    finally:
        db.close()


def test_relist_existing_withdrawn_payload_keeps_downloaded(tmp_path):
    db, m, _, path = _stored_attachment(tmp_path)
    try:
        db.save_messages([m])
        assert tuple(db.db.execute('SELECT state,local_path FROM attachments').fetchone()) == (
            'downloaded', str(path))
        assert db.attachments_due() == []
    finally:
        db.close()


@pytest.mark.parametrize('state', ['pruned', 'withheld'])
def test_relist_does_not_revive_retention_or_withheld_state(tmp_path, state):
    db, m, _, path = _stored_attachment(tmp_path)
    try:
        path.unlink()
        with db.db:
            db.db.execute('UPDATE attachments SET state=?', (state,))
        m.attachments[0].url = 'https://synthetic.invalid/fresh'
        db.save_messages([m])
        assert db.db.execute('SELECT state FROM attachments').fetchone()[0] == state
        assert db.attachments_due() == []
    finally:
        db.close()
