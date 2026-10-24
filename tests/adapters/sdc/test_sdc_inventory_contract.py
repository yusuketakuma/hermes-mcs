"""Check the shared synthetic wire vector against the selected-runtime collector."""
import json
from pathlib import Path

from adapters.sdc.inventory import collect_inventory, inventory_pages


def test_shared_central_inventory_vector_matches_collector_and_page_contract():
    vector = json.loads((Path(__file__).parent / 'fixtures/inventory-vector.json').read_text())

    class PublicDistribution:
        metadata = {'Name': 'synthetic-public'}
        version = '1.0.0'

        def read_text(self, name):
            assert name == 'direct_url.json'
            return None

    expected = vector['snapshot']
    snapshot = collect_inventory(
        public_packages=['synthetic-public'], runtime_mode=expected['runtime']['mode'],
        python_version=expected['runtime']['python_version'], source_revision=expected['source_revision'],
        distributions=[PublicDistribution()], clock=lambda: expected['fetched_at'] / 1000,
    )
    assert snapshot == {**expected, 'hash': vector['hash']}
    raw = inventory_pages(snapshot, '11111111-1111-4111-8111-111111111111')[0]
    page = json.loads(raw)
    assert page['snapshot_hash'] == vector['hash']
    assert page['schema_version'] == 'sdc.inventory.page.v1'
    assert page['page'] == page['page_count'] == 1
    assert len(raw) <= 16384
