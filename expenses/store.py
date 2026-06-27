"""A thin SQLite persistence layer.

Holds employees (with manager hierarchy + roles), expenses (amount, category,
date, status, receipt ref, plus the serialised approval chain), and an
append-only audit trail. The store knows nothing about routing rules; it just
loads/saves the domain objects the engine operates on.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Optional

from expenses.models import (
    ApprovalStep,
    AuditEntry,
    Category,
    Employee,
    Expense,
    ExpenseStatus,
    Role,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    manager_id  INTEGER REFERENCES employees(id),
    absent      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS expenses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id  INTEGER NOT NULL REFERENCES employees(id),
    amount       REAL NOT NULL,
    category     TEXT NOT NULL,
    description  TEXT NOT NULL,
    date         TEXT NOT NULL,
    status       TEXT NOT NULL,
    receipt_ref  TEXT,
    chain        TEXT NOT NULL DEFAULT '[]',
    current_step INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id  INTEGER NOT NULL REFERENCES expenses(id),
    event       TEXT NOT NULL,
    actor_id    INTEGER,
    detail      TEXT NOT NULL,
    at          TEXT NOT NULL
);
"""


class Store:
    """Wraps a SQLite connection. Pass ``:memory:`` for tests."""

    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- employees ----------------------------------------------------------

    def add_employee(self, emp: Employee) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO employees (id, name, role, manager_id, absent) "
            "VALUES (?, ?, ?, ?, ?)",
            (emp.id, emp.name, emp.role.value, emp.manager_id, int(emp.absent)),
        )
        self.conn.commit()

    def get_employee(self, emp_id: int) -> Optional[Employee]:
        row = self.conn.execute(
            "SELECT * FROM employees WHERE id = ?", (emp_id,)
        ).fetchone()
        return _row_to_employee(row) if row else None

    def org(self) -> dict[int, Employee]:
        """Return the whole org keyed by id (what the engine needs)."""
        rows = self.conn.execute("SELECT * FROM employees").fetchall()
        return {r["id"]: _row_to_employee(r) for r in rows}

    # -- expenses -----------------------------------------------------------

    def create_expense(self, exp: Expense) -> Expense:
        cur = self.conn.execute(
            "INSERT INTO expenses "
            "(employee_id, amount, category, description, date, status, receipt_ref, chain, current_step) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                exp.employee_id,
                exp.amount,
                exp.category.value,
                exp.description,
                exp.date,
                exp.status.value,
                exp.receipt_ref,
                _dump_chain(exp.chain),
                exp.current_step,
            ),
        )
        self.conn.commit()
        exp.id = cur.lastrowid
        return exp

    def save_expense(self, exp: Expense) -> None:
        """Persist a (possibly advanced) expense back to the row."""
        self.conn.execute(
            "UPDATE expenses SET status = ?, chain = ?, current_step = ?, receipt_ref = ? "
            "WHERE id = ?",
            (exp.status.value, _dump_chain(exp.chain), exp.current_step, exp.receipt_ref, exp.id),
        )
        self.conn.commit()

    def get_expense(self, exp_id: int) -> Optional[Expense]:
        row = self.conn.execute(
            "SELECT * FROM expenses WHERE id = ?", (exp_id,)
        ).fetchone()
        return _row_to_expense(row) if row else None

    def expenses_for(self, employee_id: int) -> list[Expense]:
        rows = self.conn.execute(
            "SELECT * FROM expenses WHERE employee_id = ? ORDER BY id", (employee_id,)
        ).fetchall()
        return [_row_to_expense(r) for r in rows]

    def all_expenses(self) -> list[Expense]:
        rows = self.conn.execute("SELECT * FROM expenses ORDER BY id").fetchall()
        return [_row_to_expense(r) for r in rows]

    def awaiting_approval_by(self, approver_id: int) -> list[Expense]:
        """Expenses currently PENDING on ``approver_id`` (their turn now)."""
        rows = self.conn.execute(
            "SELECT * FROM expenses WHERE status = ? ORDER BY id",
            (ExpenseStatus.PENDING.value,),
        ).fetchall()
        out = []
        for r in rows:
            exp = _row_to_expense(r)
            if exp.current_approver_id == approver_id:
                out.append(exp)
        return out

    # -- audit --------------------------------------------------------------

    def add_audit(self, entry: AuditEntry) -> None:
        self.conn.execute(
            "INSERT INTO audit (expense_id, event, actor_id, detail, at) "
            "VALUES (?, ?, ?, ?, ?)",
            (entry.expense_id, entry.event, entry.actor_id, entry.detail, entry.at),
        )
        self.conn.commit()

    def audit_for(self, expense_id: int) -> list[AuditEntry]:
        rows = self.conn.execute(
            "SELECT * FROM audit WHERE expense_id = ? ORDER BY id", (expense_id,)
        ).fetchall()
        return [
            AuditEntry(
                expense_id=r["expense_id"],
                event=r["event"],
                actor_id=r["actor_id"],
                detail=r["detail"],
                at=r["at"],
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Row <-> dataclass helpers
# ---------------------------------------------------------------------------

def _row_to_employee(row: sqlite3.Row) -> Employee:
    return Employee(
        id=row["id"],
        name=row["name"],
        role=Role(row["role"]),
        manager_id=row["manager_id"],
        absent=bool(row["absent"]),
    )


def _dump_chain(chain: list[ApprovalStep]) -> str:
    return json.dumps([asdict(s) for s in chain])


def _load_chain(raw: str) -> list[ApprovalStep]:
    out = []
    for d in json.loads(raw or "[]"):
        d = dict(d)
        d["approver_role"] = Role(d["approver_role"])
        out.append(ApprovalStep(**d))
    return out


def _row_to_expense(row: sqlite3.Row) -> Expense:
    return Expense(
        id=row["id"],
        employee_id=row["employee_id"],
        amount=row["amount"],
        category=Category(row["category"]),
        description=row["description"],
        date=row["date"],
        status=ExpenseStatus(row["status"]),
        receipt_ref=row["receipt_ref"],
        chain=_load_chain(row["chain"]),
        current_step=row["current_step"],
    )
