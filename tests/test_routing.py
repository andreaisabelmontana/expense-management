"""Direct tests of the routing engine (no HTTP)."""

from __future__ import annotations

import pytest

from expenses.models import Category, Employee, Expense, ExpenseStatus, Role
from expenses.routing import (
    Policy,
    PolicyError,
    compute_chain,
    decide,
    submit,
)


def make_org() -> dict[int, Employee]:
    return {
        1: Employee(1, "Dana Reyes", Role.DIRECTOR, manager_id=None),
        2: Employee(2, "Marco Lindt", Role.FINANCE, manager_id=1),
        3: Employee(3, "Priya Anand", Role.MANAGER, manager_id=1),
        4: Employee(4, "Tomas Vega", Role.MANAGER, manager_id=1, absent=True),
        5: Employee(5, "Lena Ortiz", Role.EMPLOYEE, manager_id=3),
        7: Employee(7, "Iris Chen", Role.EMPLOYEE, manager_id=4),
    }


POLICY = Policy(
    auto_approve_under=25.0,
    manager_limit=500.0,
    finance_categories=frozenset({Category.TRAVEL, Category.EQUIPMENT}),
    finance_amount_floor=2000.0,
)


def make_expense(emp_id: int, amount: float, category: Category = Category.MEALS) -> Expense:
    return Expense(
        id=1,
        employee_id=emp_id,
        amount=amount,
        category=category,
        description="test",
        date="2026-06-27",
    )


def collect_audit() -> tuple[list, callable]:
    log: list = []
    return log, log.append


# ---------------------------------------------------------------------------
# Chain computation
# ---------------------------------------------------------------------------

def test_small_expense_auto_approves():
    org = make_org()
    exp = make_expense(5, 12.50, Category.MEALS)
    chain = compute_chain(exp, org, POLICY)
    assert chain == []

    log, sink = collect_audit()
    submit(exp, org, POLICY, audit=sink)
    assert exp.status is ExpenseStatus.APPROVED
    events = [a.event for a in log]
    assert "auto_approved" in events


def test_mid_expense_routes_to_manager_then_stops():
    org = make_org()
    exp = make_expense(5, 120.0, Category.MEALS)  # Lena -> manager Priya(3)
    chain = compute_chain(exp, org, POLICY)
    assert [s.approver_id for s in chain] == [3]
    assert chain[0].approver_role is Role.MANAGER


def test_large_expense_routes_manager_director_finance_in_order():
    org = make_org()
    exp = make_expense(5, 5000.0, Category.OTHER)  # over manager + over finance floor
    chain = compute_chain(exp, org, POLICY)
    assert [s.approver_id for s in chain] == [3, 1, 2]
    assert [s.approver_role for s in chain] == [Role.MANAGER, Role.DIRECTOR, Role.FINANCE]


def test_travel_category_requires_finance_even_when_small():
    org = make_org()
    # Above auto-approve, under manager limit, but travel -> finance appended.
    exp = make_expense(5, 80.0, Category.TRAVEL)
    chain = compute_chain(exp, org, POLICY)
    roles = [s.approver_role for s in chain]
    assert Role.FINANCE in roles
    assert roles == [Role.MANAGER, Role.FINANCE]
    assert "travel" in chain[-1].reason


def test_equipment_category_requires_finance():
    org = make_org()
    exp = make_expense(5, 300.0, Category.EQUIPMENT)
    chain = compute_chain(exp, org, POLICY)
    assert [s.approver_role for s in chain] == [Role.MANAGER, Role.FINANCE]


def test_director_appended_above_manager_limit():
    org = make_org()
    exp = make_expense(5, 750.0, Category.SOFTWARE)  # over 500, under finance floor
    chain = compute_chain(exp, org, POLICY)
    assert [s.approver_role for s in chain] == [Role.MANAGER, Role.DIRECTOR]


def test_absent_manager_escalates_to_next_approver():
    org = make_org()
    # Iris(7) reports to Tomas(4) who is absent -> escalate to Tomas' manager Dana(1).
    exp = make_expense(7, 100.0, Category.MEALS)
    chain = compute_chain(exp, org, POLICY)
    assert chain[0].approver_id == 1
    assert "escalated" in chain[0].reason


def test_unknown_employee_raises():
    org = make_org()
    exp = make_expense(999, 100.0)
    with pytest.raises(PolicyError):
        compute_chain(exp, org, POLICY)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def test_full_approval_walkthrough_advances_and_finalizes():
    org = make_org()
    exp = make_expense(5, 5000.0, Category.OTHER)
    log, sink = collect_audit()
    submit(exp, org, POLICY, audit=sink)
    assert exp.status is ExpenseStatus.PENDING
    assert exp.current_approver_id == 3

    decide(exp, 3, True, org, audit=sink)  # manager
    assert exp.status is ExpenseStatus.PENDING
    assert exp.current_approver_id == 1

    decide(exp, 1, True, org, audit=sink)  # director
    assert exp.current_approver_id == 2

    decide(exp, 2, True, org, audit=sink)  # finance
    assert exp.status is ExpenseStatus.APPROVED
    assert exp.current_approver_id is None


def test_rejection_halts_the_chain():
    org = make_org()
    exp = make_expense(5, 5000.0, Category.OTHER)
    submit(exp, org, POLICY)
    decide(exp, 3, False, org, note="not budgeted")  # manager rejects
    assert exp.status is ExpenseStatus.REJECTED
    # Director and finance never get a turn.
    assert exp.chain[1].decided is None
    assert exp.chain[2].decided is None


def test_out_of_turn_approval_is_rejected():
    org = make_org()
    exp = make_expense(5, 5000.0, Category.OTHER)
    submit(exp, org, POLICY)
    # Director (1) tries to approve before manager (3).
    with pytest.raises(PolicyError):
        decide(exp, 1, True, org)


def test_cannot_decide_a_terminal_expense():
    org = make_org()
    exp = make_expense(5, 120.0, Category.MEALS)
    submit(exp, org, POLICY)
    decide(exp, 3, True, org)
    assert exp.status is ExpenseStatus.APPROVED
    with pytest.raises(PolicyError):
        decide(exp, 3, True, org)


def test_audit_trail_records_each_step():
    org = make_org()
    exp = make_expense(5, 750.0, Category.SOFTWARE)  # manager -> director
    log, sink = collect_audit()
    submit(exp, org, POLICY, audit=sink)
    decide(exp, 3, True, org, audit=sink)
    decide(exp, 1, True, org, audit=sink)
    events = [a.event for a in log]
    assert events.count("submitted") == 1
    assert events.count("approved") == 2  # one per approver
    assert "finalized" in events
    # Every entry carries a timestamp.
    assert all(a.at for a in log)
