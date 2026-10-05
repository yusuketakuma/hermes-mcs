"""Regression: keychain update under a different account; standalone
target scope warned as ignored."""
import shlex
import subprocess

import mcs_setup
from test_mcs_setup_standalone import config


def test_keychain_store_updates_existing_item_under_its_account(monkeypatch):
    items = [("mcs", "oldpw")]   # first interactive init: login id unknown

    def run(argv, *, timeout=60, input_text=None, cwd=None):
        def ok(out=""):
            return subprocess.CompletedProcess(argv, 0, out, "")

        if argv[:2] == ["security", "-i"]:
            toks = shlex.split(input_text)
            a, w = toks[toks.index("-a") + 1], toks[toks.index("-w") + 1]
            for i, (acc, _) in enumerate(items):
                if acc == a:
                    items[i] = (a, w)
                    break
            else:
                items.append((a, w))
            return ok()
        if argv[1] == "find-generic-password":
            m = [x for x in items
                 if "-a" not in argv or x[0] == argv[argv.index("-a") + 1]]
            if not m:
                return subprocess.CompletedProcess(argv, 44, "", "")
            acc, w = m[0]
            return ok(w + "\n" if "-w" in argv
                      else f'attributes:\n    "acct"<blob>="{acc}"\n')
        if argv[1] == "delete-generic-password":
            a = argv[argv.index("-a") + 1]
            items[:] = [x for x in items if x[0] != a]
            return ok()
        raise AssertionError(argv)

    monkeypatch.setattr(mcs_setup, "_run", run)
    assert mcs_setup._keychain_store("user1", "newpw") is True
    assert items == [("mcs", "newpw")]


def test_standalone_target_scope_is_not_warned_as_ignored():
    cfg = config("slack")
    warn = "notify.discord: scope configured but interactive"
    assert not any(w.startswith(warn) for w in mcs_setup.validate_config(cfg)[1])
    del cfg["runtime_mode"]
    assert any(w.startswith(warn) for w in mcs_setup.validate_config(cfg)[1])
