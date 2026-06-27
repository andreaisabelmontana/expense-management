r"""The approval-routing engine.

This is the core of the project. Everything else (SQLite, FastAPI) exists to
feed this module. Two ideas live here:

1. :func:`compute_chain` turns an expense + an org policy into an *ordered,
   reasoned* list of approvers. It is a pure function: same inputs, same
   chain, no side effects.

2. :func:`submit` and :func:`decide` drive the state machine over that chain:

       DRAFT --submit--> (auto-approve?) --> APPROVED
                                \--> PENDING(step 0) --approve--> PENDING(step 1)
                                                       \--approve(last)--> APPROVED
                                                       \--reject--------> REJECTED

   Rejection at any step halts the chain immediately. Approving when you are
   not the current approver is refused.

The policy is data, not code: thresholds, the finance-review category list and
the auto-approve limit all come from a :class:`Policy` object that the caller
builds (usually loaded from ``data/policy.json``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from expenses.models import (
    ApprovalStep,
    AuditEntry,
    Category,
    Employee,
    Expense,
    ExpenseStatus,
    Role,
    TERMINAL_STATUSES,
)


class PolicyError(Exception):
    """Raised when an expense cannot be routed (e.g. no manager exists)."""


@dataclass
class Policy:
    """A declarative approval policy.

    Attributes
    ----------
    auto_approve_under:
        Expenses strictly below this amount skip the chain entirely and are
        auto-approved on submit.
    manager_limit:
        A manager can be the *final* approver up to and including this amount.
        Above it, a director is appended after the manager.
    finance_categories:
        Categories that always require a final finance review, regardless of
        amount (e.g. travel, equipment).
    finance_amount_floor:
        Any expense at or above this amount also requires finance review,
        regardless of category.
    """

    auto_approve_under: float = 25.0
    manager_limit: float = 500.0
    finance_categories: frozenset[Category] = field(
        default_factory=lambda: frozenset({Category.TRAVEL, Category.EQUIPMENT})
    )
    finance_amount_floor: float = 2000.0


def build_policy(raw: dict) -> Policy:
    """Build a :class:`Policy` from a plain dict (e.g. parsed JSON)."""
    cats = frozenset(
        Category(c) for c in raw.get("finance_categories", ["travel", "equipment"])
    )
    return Policy(
        auto_approve_under=float(raw.get("auto_approve_under", 25.0)),
        manager_limit=float(raw.get("manager_limit", 500.0)),
        finance_categories=cats,
        finance_amount_floor=float(raw.get("finance_amount_floor", 2000.0)),
    )


# ---------------------------------------------------------------------------
# Hierarchy helpers
# ---------------------------------------------------------------------------

def _resolve_approver(
    start_id: Optional[int],
    org: dict[int, Employee],
) -> Optional[Employee]:
    """Walk up the management chain past any absent approvers.

    Returns the first present (non-absent) employee at or above ``start_id``,
    or ``None`` if the chain runs out. This is how timeout/absence escalation
    works: an out-of-office manager hands the decision to *their* manager.
    """
    seen: set[int] = set()
    current = org.get(start_id) if start_id is not None else None
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if not current.absent:
            return current
        current = org.get(current.manager_id) if current.manager_id is not None else None
    return None


def _first_with_role(
    start: Optional[Employee],
    role: Role,
    org: dict[int, Employee],
) -> Optional[Employee]:
    """Walk up from ``start`` to the first present employee with ``role``."""
    seen: set[int] = set()
    current = start
    while current is not None and current.id not in seen:
        seen.add(current.id)
        if current.role == role and not current.absent:
            return current
        current = org.get(current.manager_id) if current.manager_id is not None else None
    return None


def _any_with_role(role: Role, org: dict[int, Employee]) -> Optional[Employee]:
    """Find any present employee with ``role`` (used for finance reviewers)."""
    for emp in org.values():
        if emp.role == role and not emp.absent:
            return emp
    return None


# ---------------------------------------------------------------------------
# Chain computation (pure)
# ---------------------------------------------------------------------------

def compute_chain(
    expense: Expense,
    org: dict[int, Employee],
    policy: Policy,
) -> list[ApprovalStep]:
    """Compute the ordered approval chain for an expense.

    Pure function. Does not mutate the expense. The rules, in order:

    * Under ``auto_approve_under`` -> empty chain (caller auto-approves).
    * Always start with the submitter's manager (escalating past absentees).
    * Above ``manager_limit`` -> append a director after the manager.
    * Travel/equipment categories, or amount >= ``finance_amount_floor``
      -> append a finance reviewer as the final step.

    Raises :class:`PolicyError` if a required approver does not exist.
    """
    if expense.amount < policy.auto_approve_under:
        return []

    submitter = org.get(expense.employee_id)
    if submitter is None:
        raise PolicyError(f"unknown employee {expense.employee_id}")

    chain: list[ApprovalStep] = []
    seen_ids: set[int] = set()

    def add(emp: Employee, reason: str) -> None:
        if emp.id in seen_ids:
            return
        seen_ids.add(emp.id)
        chain.append(
            ApprovalStep(
                approver_id=emp.id,
                approver_role=emp.role,
                reason=reason,
            )
        )

    # 1. Manager approval (escalate past absent managers).
    manager = _resolve_approver(submitter.manager_id, org)
    if manager is None:
        raise PolicyError(
            f"no available manager for employee {submitter.id} ({submitter.name})"
        )
    if submitter.manager_id is not None and org[submitter.manager_id].absent:
        add(manager, "manager approval (escalated past absent approver)")
    else:
        add(manager, "manager approval")

    # 2. Director approval above the manager limit.
    if expense.amount > policy.manager_limit:
        director = _first_with_role(manager, Role.DIRECTOR, org)
        if director is None:
            director = _any_with_role(Role.DIRECTOR, org)
        if director is None:
            raise PolicyError("amount exceeds manager limit but no director exists")
        add(director, f"director approval (amount over {policy.manager_limit:g})")

    # 3. Finance review for flagged categories or large amounts.
    needs_finance = (
        expense.category in policy.finance_categories
        or expense.amount >= policy.finance_amount_floor
    )
    if needs_finance:
        finance = _any_with_role(Role.FINANCE, org)
        if finance is None:
            raise PolicyError("expense requires finance review but no finance role exists")
        if expense.category in policy.finance_categories:
            reason = f"finance review ({expense.category.value} category)"
        else:
            reason = f"finance review (amount >= {policy.finance_amount_floor:g})"
        add(finance, reason)

    return chain


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def submit(
    expense: Expense,
    org: dict[int, Employee],
    policy: Policy,
    audit: Optional[Callable[[AuditEntry], None]] = None,
) -> Expense:
    """Move an expense from DRAFT to either APPROVED (auto) or PENDING.

    Computes and attaches the approval chain, then either auto-approves the
    expense (empty chain) or parks it PENDING on the first approver. Mutates
    and returns ``expense``. Records audit entries via the optional callback.
    """
    if expense.status != ExpenseStatus.DRAFT:
        raise PolicyError(
            f"can only submit a DRAFT expense, this one is {expense.status.value}"
        )

    expense.chain = compute_chain(expense, org, policy)
    expense.current_step = 0

    def record(event: str, detail: str, actor: Optional[int] = None) -> None:
        if audit is not None:
            audit(AuditEntry(expense.id, event, actor, detail, _now()))

    record("submitted", f"{expense.category.value} expense for {expense.amount:g}", expense.employee_id)

    if not expense.chain:
        expense.status = ExpenseStatus.APPROVED
        record("auto_approved", f"under auto-approve threshold ({policy.auto_approve_under:g})")
        return expense

    expense.status = ExpenseStatus.PENDING
    first = expense.chain[0]
    record(
        "routed",
        f"awaiting {first.approver_role.value} (employee {first.approver_id}): {first.reason}",
    )
    return expense


def decide(
    expense: Expense,
    approver_id: int,
    approve: bool,
    org: dict[int, Employee],
    note: Optional[str] = None,
    audit: Optional[Callable[[AuditEntry], None]] = None,
) -> Expense:
    """Apply an approve/reject decision from ``approver_id``.

    Enforces turn order: only the current step's approver may decide, and the
    expense must be PENDING. Approving the last step finalises the expense as
    APPROVED; any rejection halts the chain and marks it REJECTED. Mutates and
    returns ``expense``.

    Raises :class:`PolicyError` on an out-of-turn or out-of-state decision.
    """
    if expense.status in TERMINAL_STATUSES:
        raise PolicyError(
            f"expense {expense.id} is already {expense.status.value}; no further decisions"
        )
    if expense.status != ExpenseStatus.PENDING:
        raise PolicyError(
            f"expense {expense.id} is {expense.status.value}, not awaiting approval"
        )

    expected = expense.current_approver_id
    if approver_id != expected:
        raise PolicyError(
            f"out-of-turn decision: expense {expense.id} is awaiting approver "
            f"{expected}, not {approver_id}"
        )

    def record(event: str, detail: str) -> None:
        if audit is not None:
            audit(AuditEntry(expense.id, event, approver_id, detail, _now()))

    step = expense.chain[expense.current_step]
    step.decided_by = approver_id
    step.decided_at = _now()
    step.note = note

    if not approve:
        step.decided = "rejected"
        expense.status = ExpenseStatus.REJECTED
        record("rejected", note or f"rejected at step {expense.current_step + 1} ({step.reason})")
        return expense

    step.decided = "approved"
    record("approved", note or f"approved step {expense.current_step + 1} ({step.reason})")

    # Advance to the next pending step, or finalise.
    if expense.current_step + 1 < len(expense.chain):
        expense.current_step += 1
        nxt = expense.chain[expense.current_step]
        record(
            "routed",
            f"awaiting {nxt.approver_role.value} (employee {nxt.approver_id}): {nxt.reason}",
        )
    else:
        expense.status = ExpenseStatus.APPROVED
        record("finalized", "all approvals complete")

    return expense
