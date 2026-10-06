"""A cached admission broker must serve concurrent transport threads safely."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import llm_admission
import local_llm


def test_cached_broker_supports_parallel_calls_without_serializing_transport(tmp_path, monkeypatch):
    path = str(tmp_path / 'admission.db')
    monkeypatch.setattr(local_llm, '_BROKERS', {})
    broker = local_llm._broker(path)
    assert broker.open_epoch(lambda: True)
    barrier = threading.Barrier(2)
    tokens = []

    def transport(endpoint, method, body, timeout, deadline):
        tokens.append(body['admission_token'])
        barrier.wait(timeout=3)
        return 200, {}, json.dumps({
            'choices': [{'message': {'content': 'synthetic'}, 'finish_reason': 'stop'}]
        }).encode()

    def run():
        return local_llm.admitted_chat('mcs.extract', 'synthetic',
                                      broker_path=path, request_fn=transport)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(run) for _ in range(2)]
            results = [call.result(timeout=5) for call in calls]
        assert [result['status'] for result in results] == [200, 200]
        assert len(set(tokens)) == 2
        assert broker.status()['occupying'] == {'RT': 0, 'BACKLOG': 0}
        assert [row[0] for row in broker.db.execute('SELECT state FROM permits')] == ['terminal', 'terminal']
    finally:
        broker.close()


def test_parallel_first_lookup_creates_one_shared_broker(tmp_path, monkeypatch):
    monkeypatch.setattr(local_llm, '_BROKERS', {})
    path = str(tmp_path / 'admission.db')
    start = threading.Barrier(8)

    def lookup():
        start.wait(timeout=3)
        return local_llm._broker(path)

    brokers = []
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            calls = [pool.submit(lookup) for _ in range(8)]
            brokers = [call.result(timeout=5) for call in calls]
        assert len({id(broker) for broker in brokers}) == 1
        assert local_llm._BROKERS[path] is brokers[0]
        assert brokers[0].status()['state'] == 'closed'
    finally:
        for broker in {id(broker): broker for broker in brokers}.values():
            broker.close()


def test_failed_commit_rolls_back_before_next_admission(tmp_path):
    broker = llm_admission.Broker(str(tmp_path / 'admission.db'))
    assert broker.open_epoch(lambda: True)
    connection = broker.db

    class FailCommitOnce:
        failed = False

        def __getattr__(self, name):
            return getattr(connection, name)

        def commit(self):
            if not self.failed:
                self.failed = True
                raise sqlite3.OperationalError('synthetic_commit_failure')
            return connection.commit()

    broker.db = FailCommitOnce()
    try:
        with pytest.raises(sqlite3.OperationalError, match='synthetic_commit_failure'):
            broker.acquire('mcs.extract', 'BACKLOG')
        assert connection.in_transaction is False
        assert connection.execute('SELECT COUNT(*) FROM permits').fetchone()[0] == 0
        assert broker.acquire('mcs.extract', 'BACKLOG')['admitted'] is True
    finally:
        broker.close()


def test_broker_setup_failure_closes_new_connection(monkeypatch):
    closed = []

    class FailedSetup:
        def execute(self, sql):
            raise sqlite3.OperationalError('synthetic_setup_failure')

        def close(self):
            closed.append(True)

    monkeypatch.setattr(llm_admission.sqlite3, 'connect', lambda *args, **kwargs: FailedSetup())
    with pytest.raises(sqlite3.OperationalError, match='synthetic_setup_failure'):
        llm_admission._connect('synthetic.db')
    assert closed == [True]
