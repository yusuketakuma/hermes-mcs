"""A sibling profile whose path extends ours with a space is not this dedicated profile."""
import signal
import subprocess
from types import SimpleNamespace

import mcs_adapter


def test_space_suffixed_sibling_profile_is_not_this_profile(monkeypatch):
    listing = "\n".join([
        "201 /Apps/Chrome --user-data-dir=/p/chrome-profile --no-first-run",
        "202 /Apps/Chrome --user-data-dir=/p/chrome-profile copy --no-first-run",
    ])
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=listing, returncode=0))
    pids = mcs_adapter.MCSAdapter._profile_chrome_pids("/p/chrome-profile", "/Apps/Chrome")
    # 202 runs the profile "/p/chrome-profile copy" — stopping it kills another browser
    assert pids == [201]


def test_stop_never_signals_space_suffixed_sibling(monkeypatch):
    listing = "202 /Apps/Chrome --user-data-dir=/p/chrome-profile 2 --no-first-run"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=listing, returncode=0))
    sent = []

    alive = {202}

    def kill(pid, sig):
        if sig == 0:
            if pid not in alive:
                raise ProcessLookupError
            return
        sent.append((pid, sig))
        alive.discard(pid)

    monkeypatch.setattr(mcs_adapter.os, "kill", kill)
    adapter = mcs_adapter.MCSAdapter()
    monkeypatch.setattr(adapter, "_sleep_bounded", lambda *_: None)
    adapter._stop_profile_chrome("/p/chrome-profile", "/Apps/Chrome")
    assert (202, signal.SIGTERM) not in sent


def test_profile_chrome_started_with_about_blank_still_matches(monkeypatch):
    import subprocess as sp
    lines = ("201 /Apps/Chrome --user-data-dir=/p/chrome-profile about:blank\n"
             "202 /Apps/Chrome --user-data-dir=/p/chrome-profile copy --no-first-run\n")
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(a, 0, lines, ""))
    assert mcs_adapter.MCSAdapter._profile_chrome_pids("/p/chrome-profile", "/Apps/Chrome") == [201]
