"""Business logic. No HTTP here, so it can be tested on its own."""
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from . import db
from .errors import (
    AccountNotFound,
    DuplicateTransaction,
    InsufficientBalance,
    InvalidTransfer,
    TransactionNotFound,
)


# ---------- helpers ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def to_minor(amount: Decimal) -> int:
    """Decimal rupees -> integer paise. Validation already guarantees <= 2 decimals."""
    return int((amount * 100).to_integral_value())


def from_minor(minor: int) -> float:
    return float(Decimal(minor) / 100)


def account_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "balance": from_minor(row["balance"]),
        "created_at": row["created_at"],
    }


def tx_dict(row: sqlite3.Row) -> dict:
    out = {
        "id": row["id"],
        "account_id": row["account_id"],
        "type": row["type"],
        "amount": from_minor(row["amount"]),
        "description": row["description"],
        "reference_id": row["reference_id"],
        "created_at": row["created_at"],
    }
    if row["transfer_id"]:
        out["transfer_id"] = row["transfer_id"]
    return out


def _get_account(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise AccountNotFound(f"Account '{account_id}' does not exist.")
    return row


def _find_by_reference(conn, account_id: str, reference_id: str):
    return conn.execute(
        "SELECT * FROM transactions WHERE account_id = ? AND reference_id = ?",
        (account_id, reference_id),
    ).fetchone()


def _apply_balance(conn, account_id: str, kind: str, minor: int) -> None:
    """Change the balance. For a debit the 'balance >= amount' check is part of
    the UPDATE itself, so it cannot race with another request."""
    if kind == "credit":
        conn.execute(
            "UPDATE accounts SET balance = balance + ? WHERE id = ?", (minor, account_id)
        )
        return
    cur = conn.execute(
        "UPDATE accounts SET balance = balance - ? WHERE id = ? AND balance >= ?",
        (minor, account_id, minor),
    )
    if cur.rowcount == 0:
        balance = conn.execute(
            "SELECT balance FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()["balance"]
        raise InsufficientBalance(
            "Insufficient balance for this debit.",
            {"balance": from_minor(balance), "requested": from_minor(minor)},
        )


def _insert_transaction(conn, account_id, kind, minor, description, reference_id,
                        transfer_id=None) -> sqlite3.Row:
    tx_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO transactions
           (id, account_id, type, amount, description, reference_id, transfer_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (tx_id, account_id, kind, minor, description, reference_id, transfer_id, now_iso()),
    )
    return conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()


def _balance(conn, account_id: str) -> float:
    return from_minor(
        conn.execute("SELECT balance FROM accounts WHERE id = ?", (account_id,)).fetchone()["balance"]
    )


# ---------- accounts ----------

def create_account(name: str) -> dict:
    # id and created_at come from the system; balance always starts at 0.
    account_id = str(uuid.uuid4())
    with db.write_tx() as conn:
        conn.execute(
            "INSERT INTO accounts (id, name, balance, created_at) VALUES (?, ?, 0, ?)",
            (account_id, name, now_iso()),
        )
        return account_dict(_get_account(conn, account_id))


def get_account(account_id: str) -> dict:
    with db.read_conn() as conn:
        return account_dict(_get_account(conn, account_id))


def get_balance(account_id: str) -> dict:
    with db.read_conn() as conn:
        row = _get_account(conn, account_id)
        return {"account_id": row["id"], "balance": from_minor(row["balance"])}


# ---------- transactions ----------

def add_transaction(account_id: str, kind: str, amount: Decimal,
                    description: str, reference_id: str) -> dict:
    minor = to_minor(amount)
    # Balance update and transaction insert are ONE atomic unit. If either
    # raises, write_tx() rolls both back.
    with db.write_tx() as conn:
        _get_account(conn, account_id)

        existing = _find_by_reference(conn, account_id, reference_id)
        if existing is not None:
            raise DuplicateTransaction(
                "A transaction with this reference_id already exists for this account.",
                {"transaction": tx_dict(existing)},
            )

        _apply_balance(conn, account_id, kind, minor)
        row = _insert_transaction(conn, account_id, kind, minor, description, reference_id)
        result = tx_dict(row)
        result["balance_after"] = _balance(conn, account_id)
        return result


def get_transaction(account_id: str, transaction_id: str) -> dict:
    with db.read_conn() as conn:
        _get_account(conn, account_id)
        row = conn.execute(
            "SELECT * FROM transactions WHERE id = ? AND account_id = ?",
            (transaction_id, account_id),
        ).fetchone()
        if row is None:
            raise TransactionNotFound(f"Transaction '{transaction_id}' does not exist.")
        return tx_dict(row)


def list_transactions(account_id: str, kind: str | None, page: int, limit: int) -> dict:
    with db.read_conn() as conn:
        _get_account(conn, account_id)

        where, params = "account_id = ?", [account_id]
        if kind:
            where += " AND type = ?"
            params.append(kind)

        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM transactions WHERE {where}", params
        ).fetchone()["n"]

        # latest first; seq breaks ties between identical timestamps
        rows = conn.execute(
            f"""SELECT * FROM transactions WHERE {where}
                ORDER BY created_at DESC, seq DESC
                LIMIT ? OFFSET ?""",
            params + [limit, (page - 1) * limit],
        ).fetchall()

        return {
            "data": [tx_dict(r) for r in rows],
            "pagination": {
                "current_page": page,
                "page_size": limit,
                "total_records": total,
                "total_pages": math.ceil(total / limit) if total else 0,
            },
        }


# ---------- transfers (bonus) ----------

def transfer(from_id: str, to_id: str, amount: Decimal, description: str,
             reference_id: str) -> dict:
    if from_id == to_id:
        raise InvalidTransfer("Source and destination accounts must be different.")
    minor = to_minor(amount)
    transfer_id = str(uuid.uuid4())

    with db.write_tx() as conn:
        _get_account(conn, from_id)
        _get_account(conn, to_id)

        for acc in (from_id, to_id):
            existing = _find_by_reference(conn, acc, reference_id)
            if existing is not None:
                raise DuplicateTransaction(
                    "A transaction with this reference_id already exists for an account in this transfer.",
                    {"transaction": tx_dict(existing)},
                )

        # Debit, credit and both ledger rows share ONE database transaction.
        # Any failure rolls the whole transfer back; a partial transfer is impossible.
        _apply_balance(conn, from_id, "debit", minor)
        _apply_balance(conn, to_id, "credit", minor)
        debit = _insert_transaction(
            conn, from_id, "debit", minor,
            description or f"Transfer to {to_id}", reference_id, transfer_id)
        credit = _insert_transaction(
            conn, to_id, "credit", minor,
            description or f"Transfer from {from_id}", reference_id, transfer_id)

        return {
            "transfer_id": transfer_id,
            "amount": from_minor(minor),
            "debit_transaction": tx_dict(debit),
            "credit_transaction": tx_dict(credit),
            "from_account_balance": _balance(conn, from_id),
            "to_account_balance": _balance(conn, to_id),
        }
