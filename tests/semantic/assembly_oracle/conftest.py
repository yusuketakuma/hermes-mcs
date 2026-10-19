"""Explicit, local-only controls for the synthetic assembly meaning comparison."""


def pytest_addoption(parser):
    group = parser.getgroup("assembly meaning comparison")
    group.addoption("--assembly-oracle-mode", choices=("seal", "compare"))
    group.addoption("--assembly-oracle-file")
