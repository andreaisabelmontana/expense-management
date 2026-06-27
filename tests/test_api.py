"""API tests using FastAPI's TestClient against a fresh seeded store."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from expenses.api import create_app


@pytest.fixture()
def client() -> TestClient:
    # create_app() with no args builds a fresh in-memory seeded store.
    return TestClient(create_app())


def submit(client, employee_id, amount, category, description="x", date="2026-06-27"):
    return client.post(
        "/expenses",
        headers={"X-Employee-Id": str(employee_id)},
        json={
            "amount": amount,
            "category": category,
            "description": description,
            "date": date,
        },
    )


def test_small_expense_auto_approves(client):
    r = submit(client, 5, 12.5, "meals")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "approved"
    assert body["chain"] == []


def test_mid_expense_routes_to_manager(client):
    r = submit(client, 5, 120.0, "meals")
    body = r.json()
    assert body["status"] == "pending"
    assert body["current_approver_id"] == 3
    assert [s["approver_role"] for s in body["chain"]] == ["manager"]


def test_large_expense_routes_manager_director_finance(client):
    r = submit(client, 5, 5000.0, "other")
    body = r.json()
    assert [s["approver_role"] for s in body["chain"]] == ["manager", "director", "finance"]
    assert body["current_approver_id"] == 3


def test_travel_requires_finance(client):
    r = submit(client, 5, 80.0, "travel")
    roles = [s["approver_role"] for s in r.json()["chain"]]
    assert roles == ["manager", "finance"]


def test_full_approve_walkthrough_via_api(client):
    exp_id = submit(client, 5, 5000.0, "other").json()["id"]

    # Manager approves.
    r = client.post(
        f"/expenses/{exp_id}/approve",
        headers={"X-Employee-Id": "3"},
        json={"note": "ok"},
    )
    assert r.json()["current_approver_id"] == 1

    # Director approves.
    r = client.post(
        f"/expenses/{exp_id}/approve", headers={"X-Employee-Id": "1"}, json={}
    )
    assert r.json()["current_approver_id"] == 2

    # Finance approves -> done.
    r = client.post(
        f"/expenses/{exp_id}/approve", headers={"X-Employee-Id": "2"}, json={}
    )
    assert r.json()["status"] == "approved"


def test_out_of_turn_approval_is_409(client):
    exp_id = submit(client, 5, 5000.0, "other").json()["id"]
    # Director tries before manager.
    r = client.post(
        f"/expenses/{exp_id}/approve", headers={"X-Employee-Id": "1"}, json={}
    )
    assert r.status_code == 409


def test_reject_halts_chain_via_api(client):
    exp_id = submit(client, 5, 5000.0, "other").json()["id"]
    r = client.post(
        f"/expenses/{exp_id}/reject",
        headers={"X-Employee-Id": "3"},
        json={"note": "over budget"},
    )
    assert r.json()["status"] == "rejected"


def test_awaiting_approval_listing(client):
    submit(client, 5, 120.0, "meals")  # -> manager 3
    submit(client, 6, 200.0, "supplies")  # -> manager 3
    r = client.get("/expenses/awaiting", headers={"X-Employee-Id": "3"})
    assert r.status_code == 200
    assert len(r.json()) == 2
    # Lena (5) is not an approver -> sees nothing.
    r2 = client.get("/expenses/awaiting", headers={"X-Employee-Id": "5"})
    assert r2.json() == []


def test_list_mine(client):
    submit(client, 5, 12.5, "meals")
    submit(client, 5, 120.0, "meals")
    r = client.get("/expenses/mine", headers={"X-Employee-Id": "5"})
    assert len(r.json()) == 2


def test_audit_trail_endpoint_records_steps(client):
    exp_id = submit(client, 5, 750.0, "software").json()["id"]  # manager -> director
    client.post(f"/expenses/{exp_id}/approve", headers={"X-Employee-Id": "3"}, json={})
    client.post(f"/expenses/{exp_id}/approve", headers={"X-Employee-Id": "1"}, json={})
    r = client.get(f"/expenses/{exp_id}/audit", headers={"X-Employee-Id": "5"})
    events = [a["event"] for a in r.json()]
    assert "submitted" in events
    assert events.count("approved") == 2
    assert "finalized" in events


def test_unknown_employee_header_is_404(client):
    r = submit(client, 999, 10.0, "meals")
    assert r.status_code == 404
