"""What a home-care module declares to the core: its migrations, qualifications and care-team roles."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

_NAME = re.compile(r"^[a-z][a-z0-9]{1,30}$")


@dataclass(frozen=True)
class Role:
    """A care-team role; ``requires`` lists the qualifications that may hold it."""
    label: str
    requires: tuple[str, ...]
    single: bool = True


@dataclass(frozen=True)
class Module:
    """One occupation module (or the core); every code it declares starts with ``<name>.``.

    ``api`` is the module's own routes under api/hc/<name>/: an object with ``read(db, route, params)``,
    ``write(db, op, body, *, actor)`` and ``sheet(db, patient_id)`` (its part of the visit sheet).
    """
    name: str
    label: str
    migrations: Path | None = None
    qualifications: dict[str, str] = field(default_factory=dict)
    roles: dict[str, Role] = field(default_factory=dict)
    visit_kinds: dict[str, str] = field(default_factory=dict)
    api: object = None

    def __post_init__(self):
        if not _NAME.match(self.name):
            raise ValueError(f"bad module name: {self.name}")
        for code in (*self.qualifications, *self.roles, *self.visit_kinds):
            if not code.startswith(self.name + "."):
                raise ValueError(f"{self.name} declares a code outside its namespace: {code}")


CORE = Module("core", "共通", Path(__file__).resolve().parent / "migrations",
              qualifications={"core.clerk": "事務", "core.other": "その他"},
              visit_kinds={"core.other": "その他の訪問"})


class Registry:
    """The core plus the modules enabled on this installation, core first."""

    def __init__(self, modules):
        names = [module.name for module in modules]
        if len(set(names)) != len(names) or CORE.name in names:
            raise ValueError("duplicate or reserved module")
        self.modules = (CORE, *modules)
        self.qualifications = {code: label for module in self.modules for code, label in module.qualifications.items()}
        self.roles = {code: role for module in self.modules for code, role in module.roles.items()}
        self.visit_kinds = {code: label for module in self.modules for code, label in module.visit_kinds.items()}
        for role in self.roles.values():
            if not set(role.requires) <= set(self.qualifications):
                raise ValueError("role requires an unknown qualification")

    def describe(self):
        """The browser's view of the enabled modules, their qualifications and roles."""
        return {"modules": [{"name": module.name, "label": module.label} for module in self.modules],
                "qualifications": self.qualifications,
                "roles": {code: {"label": role.label, "requires": list(role.requires), "single": role.single}
                          for code, role in self.roles.items()},
                "visit_kinds": self.visit_kinds}

    def module(self, name):
        """An enabled module that has its own routes; anything else is refused."""
        found = next((module for module in self.modules if module.name == name and module.api is not None), None)
        if found is None:
            raise ValueError("bad_request")
        return found
