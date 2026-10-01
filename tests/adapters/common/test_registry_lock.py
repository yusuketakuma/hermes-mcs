"""Registry thread safety — mutators run via asyncio.to_thread while
save() serializes on the event loop."""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from hermes_plugin.mcs_delivery import paths, registry


def test_mutation_waits_for_in_progress_save(tmp_path, monkeypatch):
    reg = registry.Registry(str(tmp_path))
    real_write = paths.atomic_write
    seen = {}

    def slow_write(path, raw, **kw):
        # a worker thread mutates mid-save: it must block on the lock
        # instead of resizing the dict the serializer is walking
        t = threading.Thread(target=reg.put_tokens,
                             args=({"tok-b": {"action": "ack"}},))
        t.start()
        t.join(0.2)
        seen["blocked"] = t.is_alive()
        seen["thread"] = t
        monkeypatch.setattr(paths, "atomic_write", real_write)
        real_write(path, raw, **kw)

    reg._data["tokens"]["tok-a"] = {"action": "page", "at": 0}
    monkeypatch.setattr(paths, "atomic_write", slow_write)
    reg.save()
    seen["thread"].join(5)
    assert seen["blocked"] is True
    assert not seen["thread"].is_alive()
    on_disk = json.loads((tmp_path / "registry.json").read_text())
    assert set(on_disk["tokens"]) == {"tok-a", "tok-b"}


def test_concurrent_mutation_and_save_never_raise(tmp_path):
    reg = registry.Registry(str(tmp_path))
    errors = []

    def mutate(prefix):
        try:
            for i in range(300):
                reg.put_tokens({f"{prefix}-{i}": {"action": "ack"}})
                reg.claim(f"{prefix}-{i}", {"n": i})
                reg.drop_claim(f"{prefix}-{i}")
        except Exception as exc:          # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=mutate, args=(p,)) for p in "ab"]
    for t in threads:
        t.start()
    for _ in range(300):
        reg.save()
        reg.claims()
    for t in threads:
        t.join(30)
    assert errors == []
    assert len(reg._data["tokens"]) == 600
