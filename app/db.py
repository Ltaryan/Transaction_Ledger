"""SQLite access layer.

Every write runs inside one explicit transaction started with BEGIN IMMEDIATE.
That takes SQLite's single write lock up front, so two requests can never
interleave a read-check-write sequence (no lost updates, no double spends).
"""
import os
import sqlite3
from contextlib import contextmanager

# Money is stored as an integer number of minor units (paise/cents) so that
# there is never any floating-point rounding. 100.50 is stored as 10050.
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    name        TEXT    NOT NULL CHECK (length(trim(name)) > 0),
    balance     INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT    NOT NULL UNIQUE,
    account_id    TEXT    NOT NULL REFERENCES accounts(id),
    type          TEXT    NOT NULL CHECK (type IN ('credit', 'debit')),
    amount        INTEGER NOT NULL CHECK (amount > 0),
    description   TEXT    NOT NULL DEFAULT '',
    reference_id  TEXT    NOT NULL CHECK (length(trim(reference_id)) > 0),
    transfer_id   TEXT,
    created_at    TEXT    NOT NULL,
    UNIQUE (account_id, reference_id)
);

CREATE INDEX IF NOT EXISTS idx_tx_account_time
    ON transactions (account_id, created_at DESC, seq DESC);

-- A transaction can never be edited or deleted, even by a buggy query.
CREATE TRIGGER IF NOT EXISTS transactions_no_update
BEFORE UPDATE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS transactions_no_delete
BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;
"""


def db_path() -> str:
    return os.environ.get("LEDGER_DB", "ledger.db")


def connect() -> sqlite3.Connection:
    # isolation_level=None -> autocommit; we control BEGIN/COMMIT ourselves.
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def read_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx():
    """One atomic unit of work: everything inside commits, or nothing does."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()
