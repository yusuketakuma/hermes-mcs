"""Synthetic snapshot-path churn must not retain an unbounded project cache."""
from types import SimpleNamespace

from hermes_plugin import projects


def test_snapshot_path_churn_has_bounded_cache_and_closes_connections(monkeypatch):
    monkeypatch.setattr(projects, "_cache", {})
    monkeypatch.setattr(projects.time, "monotonic", lambda: 1000)
    real_stat = projects.os.stat

    def synthetic_stat(path, *args, **kwargs):
        if str(path).startswith("/synthetic-snapshot-"):
            return SimpleNamespace(st_dev=1, st_ino=1, st_mtime_ns=1, st_size=1)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(projects.os, "stat", synthetic_stat)
    connections = {"opened": 0, "closed": 0}

    class SyntheticDB:
        def execute(self, sql):
            assert sql == "SELECT project_id FROM patients"
            return self

        def fetchall(self):
            return [(pid,) for pid in range(1, 101)]

        def close(self):
            connections["closed"] += 1

    def connect(path, *, uri):
        assert uri and path.endswith("?mode=ro")
        connections["opened"] += 1
        return SyntheticDB()

    monkeypatch.setattr(projects.sqlite3, "connect", connect)
    for index in range(1000):
        assert projects._snapshot_projects(f"/synthetic-snapshot-{index}.db") == frozenset(range(1, 101))
    assert connections == {"opened": 1000, "closed": 1000}
    print(f"synthetic_retained_snapshot_paths={len(projects._cache)}")
    print(f"synthetic_retained_project_memberships={sum(len(row[2]) for row in projects._cache.values())}")
    assert len(projects._cache) <= 32
    assert projects._snapshot_projects("/synthetic-snapshot-999.db") == frozenset(range(1, 101))
    assert connections["opened"] == 1000  # newest live entry remains cached
