"""Expense management system.

The substance of this package is the approval-routing engine in
:mod:`expenses.routing`: given an expense and an organisation policy it
computes the ordered chain of approvers and advances a small state machine
as each approver approves or rejects.

The other modules are the supporting cast: :mod:`expenses.models` holds the
domain types, :mod:`expenses.store` is a thin SQLite persistence layer, and
:mod:`expenses.api` exposes the whole thing over FastAPI.
"""

from expenses.models import (
    Category,
    Employee,
    Expense,
    ExpenseStatus,
    Role,
    ApprovalStep,
    AuditEntry,
)
from expenses.routing import (
    PolicyError,
    build_policy,
    compute_chain,
    decide,
    submit,
)

__all__ = [
    "Category",
    "Employee",
    "Expense",
    "ExpenseStatus",
    "Role",
    "ApprovalStep",
    "AuditEntry",
    "PolicyError",
    "build_policy",
    "compute_chain",
    "decide",
    "submit",
]
