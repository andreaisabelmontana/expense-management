"""Load the committed sample org + policy from ``data/`` into a store."""

from __future__ import annotations

import json
import os

from expenses.models import Employee, Role
from expenses.routing import Policy, build_policy
from expenses.store import Store

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def load_policy(path: str | None = None) -> Policy:
    path = path or os.path.join(DATA_DIR, "policy.json")
    with open(path, "r", encoding="utf-8") as fh:
        return build_policy(json.load(fh))


def seed_org(store: Store, path: str | None = None) -> None:
    """Populate ``store`` with the employees from ``data/org.json``."""
    path = path or os.path.join(DATA_DIR, "org.json")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for e in data["employees"]:
        store.add_employee(
            Employee(
                id=e["id"],
                name=e["name"],
                role=Role(e["role"]),
                manager_id=e.get("manager_id"),
                absent=bool(e.get("absent", False)),
            )
        )


def fresh_store(path: str = ":memory:") -> tuple[Store, Policy]:
    """Convenience: a seeded store + the loaded policy."""
    store = Store(path)
    seed_org(store)
    return store, load_policy()
