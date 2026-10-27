# Transaction Ledger API

A small backend for a transaction ledger: create accounts, record credits and
debits, read balances and paginated history, and (bonus) transfer money
between accounts atomically.

**Stack:** Python 3.11+, FastAPI, SQLite (standard library `sqlite3`), pytest.

## Run it locally

```bash
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt

uvicorn app.main:app --port 8000
```

- API: http://127.0.0.1:8000
- Interactive docs (Swagger UI): http://127.0.0.1:8000/docs
- The database is a single file, `ledger.db`, created on first start.
  Use another file with `LEDGER_DB=/path/to/file.db uvicorn app.main:app`.

## Run the tests

```bash
pytest -q
```

79 tests, each on its own throwaway database. They cover every rule in the brief
(see the table below), including concurrent requests and simulated crashes.

## Try it

```bash
B=http://127.0.0.1:8000

# 1. create an account (note the id in the response)
curl -s -X POST $B/accounts -H 'Content-Type: application/json' -d '{"name":"Aryan"}'

ID=<paste the id here>

# 2. credit 100, then try the same reference_id again (rejected as duplicate)
curl -s -X POST $B/accounts/$ID/transactions -H 'Content-Type: application/json' \
  -d '{"type":"credit","amount":100,"description":"Salary","reference_id":"ref-001"}'
curl -s -X POST $B/accounts/$ID/transactions -H 'Content-Type: application/json' \
  -d '{"type":"credit","amount":100,"reference_id":"ref-001"}'

# 3. a debit bigger than the balance is rejected
curl -s -X POST $B/accounts/$ID/transactions -H 'Content-Type: application/json' \
  -d '{"type":"debit","amount":150,"reference_id":"ref-002"}'

# 4. balance and history (latest first, filtered, paginated)
curl -s $B/accounts/$ID/balance
curl -s "$B/accounts/$ID/transactions?type=credit&page=1&limit=10"
```

Full endpoint reference: [API_DOCUMENTATION.md](API_DOCUMENTATION.md).
Decisions where the brief was open: [ASSUMPTIONS.md](ASSUMPTIONS.md).

## Project layout

```
app/
  main.py      HTTP layer: routes and the single error format
  schemas.py   request validation (Pydantic)
  service.py   business rules; no HTTP, so it is tested directly
  db.py        schema, constraints, triggers, transaction helper
  errors.py    domain errors with a status code and a stable error code
tests/
  conftest.py  fresh database per test
  test_ledger.py
```

## How each requirement is met

| Requirement | How |
| --- | --- |
| Unique account id, name required, balance starts at 0, system `created_at` | Server generates a UUID and timestamp; balance is hard-coded to 0 in the insert. Any extra field (`balance`, `id`, `created_at`) is rejected with 422 (`extra="forbid"`). |
| Credit increases / debit decreases balance | `UPDATE accounts SET balance = balance ± amount` |
| Debit rejected if balance is too low, balance unchanged | The check is inside the UPDATE (`... AND balance >= amount`), so two simultaneous debits cannot both pass. A `CHECK (balance >= 0)` constraint is the last line of defence. |
| Duplicate `reference_id` per account | Checked first (returns `409` plus the existing transaction), and enforced by `UNIQUE (account_id, reference_id)`. The same reference on a different account is allowed. |
| Balance endpoint | `GET /accounts/{id}/balance` |
| History with filter, pagination, latest first | `GET /accounts/{id}/transactions?type=&page=&limit=` ordered by `created_at DESC` (insertion order breaks ties). Response carries current page, page size, total records, total pages. |
| Transactions cannot be edited or deleted | No such endpoints (PUT / PATCH / DELETE return `405 TRANSACTION_IMMUTABLE`) and database triggers abort any `UPDATE` or `DELETE` on the table. |
| Balance always equals the sum of valid transactions | Balance and ledger row are written in the same database transaction, and money is stored as integers (paise), so there is no rounding. A test checks balance == sum(credits) - sum(debits). |
| Atomicity | Each write runs in one `BEGIN IMMEDIATE ... COMMIT` block. If anything fails, everything rolls back. Tests force a crash mid-way and check nothing changed. |
| Validation and error handling | One error shape everywhere: `{"error": {"code", "message", "details"}}`, with specific codes (`INVALID_AMOUNT`, `INSUFFICIENT_BALANCE`, `ACCOUNT_NOT_FOUND`, ...). |
| Bonus: transfer | `POST /transfers`. Debit, credit and both ledger rows commit together or not at all. |
| Runnable locally from the README | See above. |

## Design notes

- **Why SQLite:** zero setup for a reviewer, and it gives real ACID transactions and
  constraints. `BEGIN IMMEDIATE` takes the write lock first, which makes
  check-then-write sequences safe under concurrent requests. The same SQL moves
  to PostgreSQL with minor changes (`SELECT ... FOR UPDATE` instead of the lock).
- **Why integers for money:** floating point cannot represent 0.1 exactly. Amounts
  are stored as integer paise and converted on the way in and out. There is a
  test that adds 0.10 ten times and gets exactly 1.00.
- **Why a service layer:** the rules live in `service.py`, away from HTTP, so
  concurrency tests can call them directly from many threads.

## Known limits (deliberately out of scope)

- No authentication, rate limiting or multi-currency support.
- SQLite allows one writer at a time, which is fine for this scale. A busy
  production ledger would use PostgreSQL.
- Amounts are limited to 2 decimal places and 12 digits in total.
