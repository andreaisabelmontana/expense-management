# Expense Management

A working expense system whose substance is the **approval-routing engine**: given an
expense and an org policy, it computes the ordered chain of approvers, advances a small
state machine as each one approves or rejects, and records an audit trail of every step.
SQLite persistence and a FastAPI surface sit on top of that engine.

- **Live page:** https://andreaisabelmontana.github.io/expense-management/
- **Index of all my builds:** https://andreaisabelmontana.github.io/coursework-rebuilds/

## Run it

```bash
pip install -r requirements.txt
python demo.py                       # prints computed chains + approve/reject walkthroughs
python -m pytest -q                  # 24 tests
uvicorn expenses.api:app --reload    # http://127.0.0.1:8000/docs
```

## The org model

Employees have a **role** (`employee` / `manager` / `director` / `finance`) and a
`manager_id` that builds the reporting tree. An `absent` flag marks someone out of
office. The sample org lives in [`data/org.json`](data/org.json):

```
Dana Reyes   director              (1)
├─ Marco Lindt   finance           (2)
├─ Priya Anand   manager           (3)
│  ├─ Lena Ortiz   employee        (5)
│  └─ Sam Okafor   employee        (6)
└─ Tomas Vega    manager [ABSENT]  (4)
   └─ Iris Chen    employee        (7)
```

## The policy

Routing rules are **data, not code** — see [`data/policy.json`](data/policy.json):

| Rule | Default | Effect |
|------|---------|--------|
| `auto_approve_under` | `25` | Below this, the expense auto-approves on submit (empty chain). |
| `manager_limit` | `500` | Manager can be the final approver up to this; above it, a director is appended. |
| `finance_categories` | `travel`, `equipment` | These categories always add a final finance review. |
| `finance_amount_floor` | `2000` | Any amount at/above this also adds finance review. |

## The state machine

```
DRAFT --submit--> compute chain
                    │
                    ├─ empty chain ───────────────► APPROVED   (auto)
                    └─ PENDING(step 0)
                          │ approve ──► PENDING(step 1) … ──► APPROVED (last step)
                          │ reject  ──► REJECTED  (chain halts immediately)
```

Only the **current** step's approver may decide; an out-of-turn or out-of-state decision
is refused. Chain computation (`compute_chain`) is a pure function — same expense + policy
always yields the same ordered, reasoned chain. The engine is in
[`expenses/routing.py`](expenses/routing.py).

## API routes

The acting employee is passed via an `X-Employee-Id` header (a stand-in for hosted auth).

| Method | Route | Purpose |
|--------|-------|---------|
| `POST` | `/expenses` | Submit an expense; response includes the computed chain. |
| `GET`  | `/expenses/mine` | List my expenses. |
| `GET`  | `/expenses/awaiting` | List expenses awaiting **my** approval (my turn now). |
| `POST` | `/expenses/{id}/approve` | Approve the step assigned to me. |
| `POST` | `/expenses/{id}/reject` | Reject — halts the chain. |
| `GET`  | `/expenses/{id}` | View one expense + its chain. |
| `GET`  | `/expenses/{id}/audit` | View the audit trail. |
| `GET`  | `/org` | View the seeded org. |

## Real example chains

Straight from `python demo.py` against the sample org/policy:

```
#1  meals        12.50  -> approved   (auto-approved, no chain)

#2  meals       120.00  -> pending
  > 1. Priya Anand (manager)    - manager approval

#3  travel       80.00  -> pending
  > 1. Priya Anand (manager)    - manager approval
    2. Marco Lindt (finance)    - finance review (travel category)

#4  software    750.00  -> pending
  > 1. Priya Anand (manager)    - manager approval
    2. Dana Reyes (director)    - director approval (amount over 500)

#5  other      5000.00  -> pending
  > 1. Priya Anand (manager)    - manager approval
    2. Dana Reyes (director)    - director approval (amount over 500)
    3. Marco Lindt (finance)    - finance review (amount >= 2000)

#6  meals       100.00  -> pending      (Iris reports to absent Tomas)
  > 1. Dana Reyes (director)    - manager approval (escalated past absent approver)
```

## Real audit trail

The 5000 `other` expense, approved all the way through:

```
[submitted ] Lena Ortiz    other expense for 5000
[routed    ] system        awaiting manager (employee 3): manager approval
[approved  ] Priya Anand   looks good
[routed    ] Priya Anand   awaiting director (employee 1): director approval (amount over 500)
[approved  ] Dana Reyes    looks good
[routed    ] Dana Reyes    awaiting finance (employee 2): finance review (amount >= 2000)
[approved  ] Marco Lindt   looks good
[finalized ] Marco Lindt   all approvals complete
```

A rejected expense halts at the first decision — the later approvers never get a turn:

```
[submitted ] Lena Ortiz    equipment expense for 3200
[routed    ] system        awaiting manager (employee 3): manager approval
[rejected  ] Priya Anand   not in this quarter's budget
```

## Tests

`python -m pytest -q` → **24 passed**. The engine is tested directly and the API through
FastAPI's `TestClient`: small expense auto-approves; mid expense stops at the manager;
large expense routes manager → director → finance in order; rejection halts the chain;
travel/equipment categories pull in finance; the audit trail records each step; and an
out-of-turn approval is refused (409 over the API).

## Project layout

```
expenses/
  models.py    domain types + the expense state enum
  routing.py   the engine: compute_chain + the submit/decide state machine  (the core)
  store.py     SQLite persistence (employees, expenses, audit)
  seed.py      load data/org.json + data/policy.json
  api.py       FastAPI endpoints
data/          committed sample org + policy
tests/         pytest (engine + API)
demo.py        end-to-end walkthrough
```

## On the "cloud" part

The original framing for this project was a cloud deployment — serverless functions,
a managed database, object storage for receipts, hosted auth. That deployment is **out of
scope here and not claimed.** What this repo contains is the real application underneath
it: the routing engine, the state machine, the store and the API, all runnable and tested
locally. Receipts are referenced by path (`receipt_ref`) rather than uploaded to a blob
store, and the `X-Employee-Id` header stands in for a managed identity service.

## License

MIT — see [LICENSE](LICENSE).
