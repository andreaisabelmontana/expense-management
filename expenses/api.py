"""FastAPI surface over the expense engine.

Endpoints
---------
POST   /expenses                  submit a new expense (computes the chain)
GET    /expenses/mine             list my expenses
GET    /expenses/awaiting         list expenses awaiting *my* approval
POST   /expenses/{id}/approve     approve the step currently assigned to me
POST   /expenses/{id}/reject      reject (halts the chain)
GET    /expenses/{id}             view one expense + its computed chain
GET    /expenses/{id}/audit       view the audit trail
GET    /org                       view the seeded org (handy for demos)

The acting employee is identified by an ``X-Employee-Id`` header. This stands
in for the hosted-auth service described in the project's cloud design; the
routing engine, not auth, is the substance here.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from expenses.models import Category, Expense, ExpenseStatus
from expenses.routing import PolicyError, decide, submit
from expenses.seed import fresh_store


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class SubmitExpense(BaseModel):
    amount: float = Field(gt=0)
    category: Category
    description: str
    date: str
    receipt_ref: Optional[str] = None


class Decision(BaseModel):
    note: Optional[str] = None


def _expense_view(exp: Expense) -> dict:
    return {
        "id": exp.id,
        "employee_id": exp.employee_id,
        "amount": exp.amount,
        "category": exp.category.value,
        "description": exp.description,
        "date": exp.date,
        "status": exp.status.value,
        "receipt_ref": exp.receipt_ref,
        "current_approver_id": exp.current_approver_id,
        "chain": [
            {
                "approver_id": s.approver_id,
                "approver_role": s.approver_role.value,
                "reason": s.reason,
                "decided": s.decided,
                "decided_by": s.decided_by,
                "decided_at": s.decided_at,
                "note": s.note,
            }
            for s in exp.chain
        ],
    }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(store=None, policy=None) -> FastAPI:
    """Build the app. A fresh in-memory seeded store is used if none given,
    which is exactly what the tests want."""
    if store is None or policy is None:
        store, policy = fresh_store()

    app = FastAPI(title="Expense Management", version="1.0.0")
    app.state.store = store
    app.state.policy = policy

    def actor(x_employee_id: int = Header(...)) -> int:
        if store.get_employee(x_employee_id) is None:
            raise HTTPException(404, f"unknown employee {x_employee_id}")
        return x_employee_id

    @app.get("/org")
    def get_org():
        return {
            str(e.id): {
                "name": e.name,
                "role": e.role.value,
                "manager_id": e.manager_id,
                "absent": e.absent,
            }
            for e in store.org().values()
        }

    @app.post("/expenses", status_code=201)
    def post_expense(body: SubmitExpense, me: int = Depends(actor)):
        exp = Expense(
            id=0,
            employee_id=me,
            amount=body.amount,
            category=body.category,
            description=body.description,
            date=body.date,
            status=ExpenseStatus.DRAFT,
            receipt_ref=body.receipt_ref,
        )
        exp = store.create_expense(exp)
        try:
            submit(exp, store.org(), policy, audit=store.add_audit)
        except PolicyError as e:
            raise HTTPException(422, str(e))
        store.save_expense(exp)
        return _expense_view(exp)

    @app.get("/expenses/mine")
    def list_mine(me: int = Depends(actor)):
        return [_expense_view(e) for e in store.expenses_for(me)]

    @app.get("/expenses/awaiting")
    def list_awaiting(me: int = Depends(actor)):
        return [_expense_view(e) for e in store.awaiting_approval_by(me)]

    @app.get("/expenses/{exp_id}")
    def get_one(exp_id: int, me: int = Depends(actor)):
        exp = store.get_expense(exp_id)
        if exp is None:
            raise HTTPException(404, f"unknown expense {exp_id}")
        return _expense_view(exp)

    @app.get("/expenses/{exp_id}/audit")
    def get_audit(exp_id: int, me: int = Depends(actor)):
        if store.get_expense(exp_id) is None:
            raise HTTPException(404, f"unknown expense {exp_id}")
        return [
            {"event": a.event, "actor_id": a.actor_id, "detail": a.detail, "at": a.at}
            for a in store.audit_for(exp_id)
        ]

    @app.post("/expenses/{exp_id}/approve")
    def approve(exp_id: int, body: Decision, me: int = Depends(actor)):
        return _decide(exp_id, me, True, body.note)

    @app.post("/expenses/{exp_id}/reject")
    def reject(exp_id: int, body: Decision, me: int = Depends(actor)):
        return _decide(exp_id, me, False, body.note)

    def _decide(exp_id: int, me: int, approve_it: bool, note: Optional[str]):
        exp = store.get_expense(exp_id)
        if exp is None:
            raise HTTPException(404, f"unknown expense {exp_id}")
        try:
            decide(exp, me, approve_it, store.org(), note=note, audit=store.add_audit)
        except PolicyError as e:
            raise HTTPException(409, str(e))
        store.save_expense(exp)
        return _expense_view(exp)

    return app


# Module-level app for `uvicorn expenses.api:app`.
app = create_app()
