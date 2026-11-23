"""The pharmacy module's declaration: pharmacist qualification and the primary-pharmacist role."""
from __future__ import annotations

from homecare_modules import Module, Role

MODULE = Module(
    "pharmacy", "薬局",
    qualifications={"pharmacy.pharmacist": "薬剤師"},
    roles={"pharmacy.primary_pharmacist": Role("担当薬剤師", ("pharmacy.pharmacist",))})
