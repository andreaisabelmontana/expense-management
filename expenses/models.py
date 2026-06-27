"""Domain types for the expense system.

These are deliberately plain dataclasses / enums with no persistence or
framework coupling so the routing engine can be tested in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Role(str, Enum):
    """Where an employee sits in the approval hierarchy."""

    EMPLOYEE = "employee"
    MANAGER = "manager"
    DIRECTOR = "director"
    FINANCE = "finance"


class Category(str, Enum):
    """Expense categories. Some categories carry extra routing rules."""

    MEALS = "meals"
    TRAVEL = "travel"
    EQUIPMENT = "equipment"
    SOFTWARE = "software"
    SUPPLIES = "supplies"
    OTHER = "other"


class ExpenseStatus(str, Enum):
    """The expense state machine.

    DRAFT      -> SUBMITTED (submit)
    SUBMITTED  -> PENDING / APPROVED (routing computes the chain)
    PENDING    -> PENDING / APPROVED (an approver approves a step)
    PENDING    -> REJECTED (an approver rejects -> chain halts)
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# Terminal states the engine will refuse to advance.
TERMINAL_STATUSES = frozenset({ExpenseStatus.APPROVED, ExpenseStatus.REJECTED})


@dataclass
class Employee:
    """A person in the org. ``manager_id`` builds the reporting tree."""

    id: int
    name: str
    role: Role
    manager_id: Optional[int] = None
    # If True the employee is out-of-office and approvals routed to them
    # escalate to their own manager (timeout / absence handling).
    absent: bool = False


@dataclass
class ApprovalStep:
    """One link in a computed approval chain.

    ``reason`` explains *why* this approver is in the chain, which is what
    makes the routing decisions auditable rather than a black box.
    """

    approver_id: int
    approver_role: Role
    reason: str
    decided: Optional[str] = None  # "approved" / "rejected" / None if pending
    decided_by: Optional[int] = None
    decided_at: Optional[str] = None
    note: Optional[str] = None


@dataclass
class AuditEntry:
    """One row of the immutable audit trail for an expense."""

    expense_id: int
    event: str  # submitted / approved / rejected / auto_approved / escalated
    actor_id: Optional[int]
    detail: str
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class Expense:
    """An expense claim plus its computed routing state."""

    id: int
    employee_id: int
    amount: float
    category: Category
    description: str
    date: str
    status: ExpenseStatus = ExpenseStatus.DRAFT
    receipt_ref: Optional[str] = None
    # The ordered approval chain, populated at submit time.
    chain: list[ApprovalStep] = field(default_factory=list)
    # Index of the step currently awaiting a decision.
    current_step: int = 0

    @property
    def current_approver_id(self) -> Optional[int]:
        """Who, if anyone, this expense is currently waiting on."""
        if self.status != ExpenseStatus.PENDING:
            return None
        if 0 <= self.current_step < len(self.chain):
            return self.chain[self.current_step].approver_id
        return None
