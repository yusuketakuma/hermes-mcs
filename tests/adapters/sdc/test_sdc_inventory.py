"""Exercise inventory privacy and completeness using synthetic metadata only."""
from importlib import metadata
import hashlib
import json
import sys

import pytest

from adapters.sdc.inventory import collect_inventory, inventory_pages
from adapters.sdc import inventory

NOW = 1792713600
REVISION = 'b' * 40
SNAPSHOT_ID = '12345678-1234-4234-8234-123456789abc'


class Distribution:
    def __init__(self, name, version='1.2.3', direct_url=None, error=None):
        self.metadata = {'Name': name, 'Summary': 'SYNTHETIC_PRIVATE_DESCRIPTION'}
        self.version = version
        self.direct_url = direct_url
        self.error = error
        self.reads = []

    def read_text(self, name):
        self.reads.append(name)
        if self.error:
            raise self.error
        assert name == 'direct_url.json'
        return self.direct_url


def collect(distributions, *, names=('synthetic-public',), **kwargs):
    return collect_inventory(public_packages=names, runtime_mode='standalone', source_revision=REVISION,
                             distributions=distributions, clock=lambda: NOW, python_version='3.14.0', **kwargs)


def rehash(snapshot):
    snapshot['hash'] = hashlib.sha256(json.dumps({key: value for key, value in snapshot.items() if key != 'hash'},
                                               ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()


def test_collects_exact_metadata_and_hash_without_extra_distribution_fields():
    dist = Distribution('Synthetic_Public', '1!2.3.0rc1')
    result = collect([dist], lock_entries={'synthetic-public': '1!2.3.0rc1'})
    assert result['packages'] == [{'ecosystem': 'PyPI', 'name': 'synthetic-public', 'version': '1!2.3.0rc1', 'public': True}]
    assert result['complete'] is True
    assert result['coverage'] == 'complete'
    assert result['fetched_at'] == NOW * 1000
    assert result['runtime'] == {'mode': 'standalone', 'python_version': '3.14.0'}
    assert result['source_revision'] == REVISION
    assert result['lock_complete'] is True
    assert result['lock_discrepancies'] == []
    assert dist.reads == ['direct_url.json']
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(result)
    before = result['hash']
    rehash(result)
    assert result['hash'] == before


@pytest.mark.parametrize('version', ['1.0.0+SYNTHETIC_CUSTOMER_ID', '1.0.0-SYNTHETIC_CUSTOMER_ID'])
def test_private_build_labels_never_enter_snapshots_lock_differences_or_pages(version):
    result = collect([Distribution('synthetic-public', version)], lock_entries={'synthetic-public': version})
    assert result['packages'] == []
    assert result['excluded_counts']['invalid_metadata'] == 1
    assert result['complete'] is False and result['coverage'] == 'unknown'
    assert result['lock_complete'] is False
    assert 'SYNTHETIC_CUSTOMER_ID' not in json.dumps(result)
    assert all(b'SYNTHETIC_CUSTOMER_ID' not in raw for raw in inventory_pages(result, SNAPSHOT_ID))
    valid = collect([Distribution('synthetic-public')], lock_entries={'synthetic-public': version})
    assert valid['lock_complete'] is False
    assert 'SYNTHETIC_CUSTOMER_ID' not in json.dumps(valid)
    valid['packages'][0]['version'] = version
    rehash(valid)
    with pytest.raises(ValueError, match='SDC_INVALID_INVENTORY'):
        inventory_pages(valid, SNAPSHOT_ID)


def test_public_version_contract_matches_the_shared_vector():
    from pathlib import Path
    vector = json.loads((Path(__file__).parent / 'fixtures' / 'inventory-versions.json').read_text())
    for version in vector['accepted']:
        assert inventory._version(version) == version
    for version in vector['rejected']:
        with pytest.raises(ValueError, match='SDC_INVALID_PACKAGE_VERSION'):
            inventory._version(version)


def test_metadata_reader_never_imports_the_installed_package(tmp_path, monkeypatch):
    info = tmp_path / 'synthetic_public-1.2.3.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: synthetic-public\nVersion: 1.2.3\n', encoding='utf-8')
    (tmp_path / 'synthetic_public.py').write_text('raise RuntimeError("MUST_NOT_IMPORT")\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(metadata, 'distributions', lambda: metadata.Distribution.discover(path=[str(tmp_path)]))
    result = collect_inventory(public_packages={'synthetic-public'}, runtime_mode='hermes', source_revision=REVISION,
                               clock=lambda: NOW, python_version='3.14.0')
    assert result['packages'][0]['version'] == '1.2.3'
    assert 'synthetic_public' not in sys.modules


def test_unapproved_names_never_leave_the_device_or_trigger_origin_reads():
    secret = Distribution('synthetic-private-name')
    result = collect([Distribution('synthetic-public'), secret])
    assert result['complete'] is False
    assert result['coverage'] == 'unknown'
    assert result['excluded_counts']['not_public'] == 1
    assert secret.reads == []
    assert 'synthetic-private-name' not in json.dumps(result)


@pytest.mark.parametrize('origin', ['{"url":"file:///SYNTHETIC_PRIVATE_PATH"}', '{"url":"https://user:SYNTHETIC_SECRET@example.invalid/repo"}', ''])
def test_direct_sources_are_excluded_even_when_the_name_is_approved(origin):
    result = collect([Distribution('synthetic-public', direct_url=origin)])
    assert result['packages'] == []
    assert result['excluded_counts']['direct_source'] == 1
    assert result['complete'] is False
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(result)
    assert 'SYNTHETIC_SECRET' not in json.dumps(result)


def test_metadata_and_enumeration_failures_remain_partial_without_exception_text():
    def broken():
        yield Distribution('synthetic-public')
        yield Distribution('synthetic-public', error=OSError('SYNTHETIC_PRIVATE_PATH'))
        raise OSError('SYNTHETIC_SECRET')
    result = collect(broken())
    assert result['packages'][0]['name'] == 'synthetic-public'
    assert result['collection_failed'] is True
    assert result['excluded_counts']['metadata_error'] == 1
    assert result['complete'] is False
    assert 'SYNTHETIC_' not in json.dumps(result)


def test_empty_or_conflicting_inventory_cannot_claim_complete():
    assert collect([])['complete'] is False
    result = collect([Distribution('synthetic-public', '1.0'), Distribution('synthetic-public', '2.0'), Distribution('synthetic-public', '3.0')])
    assert result['packages'] == []
    assert result['excluded_counts']['conflicting_distribution'] == 3
    assert result['complete'] is False


def test_collection_cap_is_partial_instead_of_silently_truncating(monkeypatch):
    monkeypatch.setattr(inventory, 'MAX_DISTRIBUTIONS', 3)
    result = collect(Distribution('synthetic-public') for _ in range(4))
    assert result['collection_failed'] is True
    assert result['complete'] is False


def test_collection_preserves_the_owners_deadline():
    def expired():
        yield from ()
        raise TimeoutError('synthetic tick deadline')
    with pytest.raises(TimeoutError):
        collect(expired())


def test_lock_differences_are_separate_from_exact_installed_packages():
    names = ('synthetic-public', 'synthetic-missing', 'synthetic-unlocked')
    result = collect([Distribution('synthetic-public', '2.0'), Distribution('synthetic-unlocked', '3.0')], names=names,
                     lock_entries={'synthetic-public': '1.0', 'synthetic-missing': '4.0', 'synthetic-private': '9.0'})
    assert result['lock_complete'] is False
    assert [item['version'] for item in result['packages']] == ['2.0', '3.0']
    assert result['lock_discrepancies'] == [
        {'ecosystem': 'PyPI', 'name': 'synthetic-missing', 'installed_version': None, 'locked_version': '4.0', 'kind': 'missing'},
        {'ecosystem': 'PyPI', 'name': 'synthetic-public', 'installed_version': '2.0', 'locked_version': '1.0', 'kind': 'version_mismatch'},
        {'ecosystem': 'PyPI', 'name': 'synthetic-unlocked', 'installed_version': '3.0', 'locked_version': None, 'kind': 'unlocked'}]
    assert 'synthetic-private' not in json.dumps(result)


def test_unknown_runtime_and_invalid_metadata_are_reduced_to_fixed_states():
    result = collect_inventory(public_packages={'synthetic-public'}, runtime_mode='/SYNTHETIC_PRIVATE_PATH',
                               source_revision='SYNTHETIC_SECRET', python_version='SYNTHETIC_SECRET', clock=lambda: NOW,
                               distributions=[Distribution('synthetic-public', 'file:///SYNTHETIC_PRIVATE_PATH')])
    assert result['runtime'] == {'mode': 'unknown', 'python_version': None}
    assert result['source_revision'] is None
    assert result['excluded_counts']['invalid_metadata'] == 1
    assert result['complete'] is False
    assert 'SYNTHETIC_' not in json.dumps(result)


def test_allowlist_is_required_and_invalid_values_fail_without_echoing_them():
    with pytest.raises(ValueError, match='^SDC_INVALID_PUBLIC_ALLOWLIST$'):
        collect([], names={'https://SYNTHETIC_SECRET@example.invalid'})
    result = collect([Distribution('synthetic-public')], names=())
    assert result['packages'] == []
    assert result['complete'] is False


def test_pages_bound_actual_bytes_and_entry_count_and_reconstruct_the_hash():
    names = [f'synthetic-public-{index:04d}-' + 'x' * 165 for index in range(220)]
    result = collect([Distribution(name, '1.' + '2' * 120) for name in names], names=names,
                     lock_entries={name: '1.0' for name in names})
    raw_pages = inventory_pages(result, SNAPSHOT_ID)
    assert len(raw_pages) > 2
    pages = [json.loads(raw) for raw in raw_pages]
    assert all(len(raw) <= 16384 for raw in raw_pages)
    assert all(len(page['packages']) + len(page['lock_discrepancies']) <= 100 for page in pages)
    assert [page['page'] for page in pages] == list(range(1, len(pages) + 1))
    assert all(page['snapshot_id'] == SNAPSHOT_ID and page['snapshot_hash'] == result['hash'] and page['page_count'] == len(pages) for page in pages)
    assert [item for page in pages for item in page['packages']] == result['packages']
    assert [item for page in pages for item in page['lock_discrepancies']] == result['lock_discrepancies']
    reconstructed = {key: value for key, value in pages[0].items() if key not in {'snapshot_id', 'snapshot_hash', 'page', 'page_count'}}
    reconstructed.update(schema_version='sdc.inventory.v1', packages=[item for page in pages for item in page['packages']],
                         lock_discrepancies=[item for page in pages for item in page['lock_discrepancies']])
    assert hashlib.sha256(json.dumps(reconstructed, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest() == result['hash']


def test_empty_incomplete_snapshot_has_one_page():
    pages = inventory_pages(collect([]), SNAPSHOT_ID)
    assert len(pages) == 1
    assert json.loads(pages[0])['complete'] is False


def test_page_count_limit_fails_without_truncating(monkeypatch):
    monkeypatch.setattr(inventory, 'MAX_PAGES', 1)
    names = [f'synthetic-public-{index:03d}' for index in range(101)]
    snapshot = collect([Distribution(name) for name in names], names=names)
    with pytest.raises(ValueError, match='^SDC_INVENTORY_PAGE_LIMIT$'):
        inventory_pages(snapshot, SNAPSHOT_ID)


def test_paging_rejects_hash_changes_and_nested_extra_data_before_serializing():
    result = collect([Distribution('synthetic-public')])
    result['packages'][0]['version'] = '2.0'
    with pytest.raises(ValueError, match='^SDC_INVALID_INVENTORY$'):
        inventory_pages(result, SNAPSHOT_ID)
    rehash(result)
    result['runtime']['path'] = 'SYNTHETIC_PRIVATE_PATH'
    rehash(result)
    with pytest.raises(ValueError, match='^SDC_INVALID_INVENTORY$'):
        inventory_pages(result, SNAPSHOT_ID)
    with pytest.raises(ValueError, match='^SDC_INVALID_INVENTORY$'):
        inventory_pages(collect([]), '/SYNTHETIC_PRIVATE_PATH')


@pytest.mark.parametrize('stamp', [True, float('nan'), float('inf'), -1])
def test_invalid_time_is_not_emitted(stamp):
    with pytest.raises(ValueError, match='^SDC_INVALID_INVENTORY_TIME$'):
        collect_inventory(public_packages=set(), runtime_mode='hermes', source_revision=REVISION,
                          distributions=[], clock=lambda: stamp, python_version='3.14.0')
