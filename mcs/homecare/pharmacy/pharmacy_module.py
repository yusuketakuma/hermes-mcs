"""The pharmacy module's declaration: pharmacist qualification, roles, visit kinds, migrations and routes."""
from __future__ import annotations

from pathlib import Path

from homecare_modules import Module, Role
import pharmacy_api

MODULE = Module(
    "pharmacy", "薬局",
    migrations=Path(__file__).resolve().parent / "migrations",
    qualifications={"pharmacy.pharmacist": "薬剤師"},
    roles={"pharmacy.primary_pharmacist": Role("担当薬剤師", ("pharmacy.pharmacist",))},
    visit_kinds={"pharmacy.home_visit": "訪問薬剤管理", "pharmacy.delivery": "配薬のみ"},
    # 訪問薬剤管理 done is a pharmacist's clinical record; clerks plan it, enter prescriptions and deliver.
    done_requires={"pharmacy.home_visit": ("pharmacy.pharmacist",)},
    api=pharmacy_api)
