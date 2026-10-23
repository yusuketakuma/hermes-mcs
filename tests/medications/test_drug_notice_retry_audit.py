"""Observed insert changes remain owed independently of bounded web recheck rotation."""
import hashlib
import json

import drug_info


def _seed(tmp_path, count=7, *, changed=False):
    items = [dict(name=f'架空薬{i}', code=str(i), url=f'https://www.pmda.go.jp/fictional/{i}',
                  sha256='a' * 64, revision='旧版', gen=str(i), added_at=1,
                  last_checked=i, last='changed' if changed else None,
                  observed_sha256='b' * 64 if changed else None)
             for i in range(count)]
    (tmp_path / 'watch.json').write_text(json.dumps({'inserts': items, 'notified': {}}))
    return items


def test_failed_enqueue_change_is_retried_even_when_other_inserts_are_due(tmp_path, monkeypatch):
    items = _seed(tmp_path)
    monkeypatch.setattr(drug_info, 'check_listing', lambda local: ('same', 'same'))
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return {'text': 'new version', 'etag': None}, {}

    monkeypatch.setattr(drug_info, 'fetch_page', fetch)
    first = drug_info.daily_notices(('20261013', 200), tmp_path)
    assert len(calls) == 3 and len(first) == 3
    key, value, _text = first[0]
    assert key == 'insert:' + items[0]['url']
    assert value == hashlib.sha256(b'new version').hexdigest()
    # No mark_notified after enqueue failure; next due batch excludes this item.
    calls.clear()
    second = drug_info.daily_notices(('20261013', 200), tmp_path)
    assert len(calls) == 3 and items[0]['url'] not in calls
    assert (key, value) in [(k, v) for k, v, _text in second]
    drug_info.mark_notified(key, value, tmp_path)
    third = drug_info.daily_notices(('20261013', 200), tmp_path)
    assert key not in [k for k, _v, _text in third]


def test_saved_changes_are_bounded_and_acknowledgement_prevents_stale_notice(tmp_path, monkeypatch):
    items = _seed(tmp_path, changed=True)
    monkeypatch.setattr(drug_info, 'check_listing', lambda local: ('same', 'same'))

    def no_recheck(root=None):
        drug_info.formulary_ack(items[0]['name'], 'b' * 64, root)
        return []

    monkeypatch.setattr(drug_info, 'formulary_check', no_recheck)
    notices = drug_info.daily_notices(('20261013', 200), tmp_path)
    assert len(notices) == 3
    assert [k for k, _v, _text in notices] == ['insert:' + item['url'] for item in items[1:4]]
    for key, value, _text in notices:
        drug_info.mark_notified(key, value, tmp_path)
    following = drug_info.daily_notices(('20261013', 200), tmp_path)
    assert len(following) == 3
    assert [k for k, _v, _text in following] == ['insert:' + item['url'] for item in items[4:7]]
