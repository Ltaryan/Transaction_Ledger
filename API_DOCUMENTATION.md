# API Documentation

Base URL: `http://127.0.0.1:8000`. All bodies are JSON. Amounts are numbers with
at most 2 decimal places. Interactive docs are also available at `/docs`.

## Error format (all endpoints)

```json
{ "error": { "code": "INSUFFICIENT_BALANCE", "message": "Insufficient balance for this debit.", "details": { "balance": 100.0, "requested": 150.0 } } }
```

| HTTP | `code` | When |
| --- | --- | --- |
| 404 | `ACCOUNT_NOT_FOUND` | Account id does not exist |
| 404 | `TRANSACTION_NOT_FOUND` | Transaction id does not exist on that account |
| 409 | `DUPLICATE_TRANSACTION` | `reference_id` already used on that account. `details.transaction` holds the existing one. |
| 422 | `INSUFFICIENT_BALANCE` | Debit (or transfer) larger than the balance |
| 422 | `INVALID_AMOUNT` | Amount missing, not a number, <= 0, more than 2 decimals, or too large |
| 422 | `INVALID_TRANSACTION_TYPE` | Type is not exactly `credit` or `debit` (also the `type` filter) |
| 422 | `INVALID_PAGINATION` | `page` < 1, `limit` outside 1 to 100, or not an integer |
| 422 | `INVALID_REQUEST_BODY` | Body is not valid JSON or not a JSON object |
| 422 | `INVALID_TRANSFER` | Source and destination are the same account |
| 422 | `VALIDATION_ERROR` | Any other invalid field (missing name, empty `reference_id`, unknown field) |
| 405 | `TRANSACTION_IMMUTABLE` | Attempt to PUT / PATCH / DELETE a transaction |
| 500 | `INTERNAL_ERROR` | Unexpected failure; nothing was changed |

Validation errors list each problem in `details.errors` as `{field, message}`.

---

## POST /accounts
Create an account.

Request: `{ "name": "Aryan" }`
The client may not send `balance`, `id` or `created_at` (rejected with 422).

`201 Created`
```json
{ "id": "2c8a6ed6-43de-4429-bca7-fb683484f3df", "name": "Aryan", "balance": 0.0,
  "created_at": "2026-10-27T04:24:36.267770+00:00" }
```

## GET /accounts/{account_id}
Returns the account object above. `404` if unknown.

## POST /accounts/{account_id}/transactions
Add a credit or debit.

Request
```json
{ "type": "credit", "amount": 100, "description": "Salary", "reference_id": "ref-001" }
```
`type`: `"credit"` or `"debit"`. `amount`: greater than 0. `reference_id`: required,
unique per account. `description`: optional.

`201 Created`
```json
{ "id": "8d3e4709-...", "account_id": "2c8a6ed6-...", "type": "credit", "amount": 100.0,
  "description": "Salary", "reference_id": "ref-001",
  "created_at": "2026-10-27T04:24:36.339027+00:00", "balance_after": 100.0 }
```
Errors: `404 ACCOUNT_NOT_FOUND`, `409 DUPLICATE_TRANSACTION`, `422 INSUFFICIENT_BALANCE`,
`422 INVALID_AMOUNT`, `422 INVALID_TRANSACTION_TYPE`, `422 VALIDATION_ERROR`.
A rejected request never changes the balance or creates a transaction.

## GET /accounts/{account_id}/balance
`200 OK` -> `{ "account_id": "2c8a6ed6-...", "balance": 70.0 }`. `404` if unknown.

## GET /accounts/{account_id}/transactions
Transaction history, **latest first** (by `created_at`).

| Query param | Default | Meaning |
| --- | --- | --- |
| `type` | (both) | `credit` or `debit` |
| `page` | 1 | Page number, starting at 1 |
| `limit` | 20 | Page size, 1 to 100 |

`200 OK`
```json
{
  "data": [ { "id": "...", "account_id": "...", "type": "debit", "amount": 30.0,
              "description": "", "reference_id": "d1", "created_at": "..." } ],
  "pagination": { "current_page": 1, "page_size": 20, "total_records": 5, "total_pages": 1 }
}
```
A page past the end returns an empty `data` list, not an error.

## GET /accounts/{account_id}/transactions/{transaction_id}
Returns one transaction. `PUT`, `PATCH` and `DELETE` on this path return
`405 TRANSACTION_IMMUTABLE`.

## POST /transfers (bonus)
Move money between two accounts, atomically.

Request
```json
{ "from_account_id": "<id A>", "to_account_id": "<id B>", "amount": 30,
  "reference_id": "tr-001", "description": "optional" }
```
`201 Created`
```json
{ "transfer_id": "...", "amount": 30.0,
  "debit_transaction":  { "account_id": "<A>", "type": "debit",  "transfer_id": "...", "...": "..." },
  "credit_transaction": { "account_id": "<B>", "type": "credit", "transfer_id": "...", "...": "..." },
  "from_account_balance": 70.0, "to_account_balance": 80.0 }
```
Fails with `404 ACCOUNT_NOT_FOUND`, `409 DUPLICATE_TRANSACTION`,
`422 INSUFFICIENT_BALANCE`, `422 INVALID_TRANSFER` or `422 INVALID_AMOUNT`.
On any failure neither account changes.

## GET /health
`{ "status": "ok" }`
