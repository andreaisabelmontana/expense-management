"""End-to-end demo of the expense routing engine.

Submits several expenses of different amounts and categories against the
committed sample org/policy, prints the computed approval chain for each, then
runs a full approve walkthrough and a rejection walkthrough, printing the audit
trail at the end. Everything here is real: it drives the same engine and store
the API uses.

Run:  python demo.py
"""

from __future__ import annotations

from expenses.models import Category, Expense, ExpenseStatus
from expenses.routing import compute_chain, decide, submit
from expenses.seed import fresh_store


def role_name(store, emp_id):
    e = store.get_employee(emp_id)
    return f"{e.name} ({e.role.value})"


def show_chain(store, exp: Expense) -> None:
    print(f"\n  #{exp.id}  {exp.category.value:<9} {exp.amount:>8.2f}  -> status: {exp.status.value}")
    if not exp.chain:
        print("     (auto-approved, no chain)")
        return
    for i, step in enumerate(exp.chain, 1):
        marker = ">" if (exp.status is ExpenseStatus.PENDING and i - 1 == exp.current_step) else " "
        print(f"   {marker} {i}. {role_name(store, step.approver_id):<24} - {step.reason}")


def main() -> None:
    store, policy = fresh_store()

    print("=" * 70)
    print("SAMPLE ORG")
    print("=" * 70)
    for e in store.org().values():
        mgr = store.get_employee(e.manager_id).name if e.manager_id else "-"
        flag = "  [ABSENT]" if e.absent else ""
        print(f"  {e.id}  {e.name:<14} {e.role.value:<9} manager: {mgr}{flag}")

    print("\n" + "=" * 70)
    print("COMPUTED APPROVAL CHAINS")
    print("=" * 70)

    samples = [
        # (employee_id, amount, category)
        (5, 12.50, Category.MEALS),     # auto-approve
        (5, 120.00, Category.MEALS),    # manager only
        (5, 80.00, Category.TRAVEL),    # manager -> finance (category rule)
        (5, 750.00, Category.SOFTWARE), # manager -> director (over limit)
        (5, 5000.00, Category.OTHER),   # manager -> director -> finance
        (7, 100.00, Category.MEALS),    # Iris reports to absent Tomas -> escalates
    ]

    created = []
    for emp_id, amount, cat in samples:
        exp = Expense(
            id=0,
            employee_id=emp_id,
            amount=amount,
            category=cat,
            description=f"{cat.value} expense",
            date="2026-06-27",
            receipt_ref=f"receipts/{cat.value}-{int(amount)}.pdf",
        )
        exp = store.create_expense(exp)
        submit(exp, store.org(), policy, audit=store.add_audit)
        store.save_expense(exp)
        created.append(exp)
        show_chain(store, exp)

    # -- Full approve walkthrough on the large expense --------------------
    big = created[4]  # the 5000 OTHER expense: manager -> director -> finance
    print("\n" + "=" * 70)
    print(f"APPROVE WALKTHROUGH  -  expense #{big.id} ({big.amount:.2f} {big.category.value})")
    print("=" * 70)
    for step in list(big.chain):
        approver = step.approver_id
        decide(big, approver, True, store.org(), note="looks good", audit=store.add_audit)
        store.save_expense(big)
        nxt = big.current_approver_id
        nxt_txt = role_name(store, nxt) if nxt else "DONE"
        print(f"  {role_name(store, approver):<24} approved  ->  next: {nxt_txt}")
    print(f"  final status: {big.status.value.upper()}")

    # -- Rejection walkthrough on a fresh large expense -------------------
    rej = Expense(
        id=0, employee_id=5, amount=3200.0, category=Category.EQUIPMENT,
        description="standing desks", date="2026-06-27",
        receipt_ref="receipts/equipment-3200.pdf",
    )
    rej = store.create_expense(rej)
    submit(rej, store.org(), policy, audit=store.add_audit)
    store.save_expense(rej)
    print("\n" + "=" * 70)
    print(f"REJECT WALKTHROUGH  -  expense #{rej.id} ({rej.amount:.2f} {rej.category.value})")
    print("=" * 70)
    show_chain(store, rej)
    decide(rej, rej.current_approver_id, False, store.org(),
           note="not in this quarter's budget", audit=store.add_audit)
    store.save_expense(rej)
    print(f"  {role_name(store, 3)} rejected  ->  status: {rej.status.value.upper()}")
    print("  (director and finance never get a turn)")

    # -- Audit trails -----------------------------------------------------
    for exp in (big, rej):
        print("\n" + "-" * 70)
        print(f"AUDIT TRAIL  -  expense #{exp.id}")
        print("-" * 70)
        for a in store.audit_for(exp.id):
            actor = store.get_employee(a.actor_id).name if a.actor_id else "system"
            print(f"  [{a.event:<12}] {actor:<14} {a.detail}")


if __name__ == "__main__":
    main()
