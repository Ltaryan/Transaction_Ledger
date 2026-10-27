"""Tests: one section per requirement in the assessment brief."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import db, service
from app.errors import DuplicateTransaction, InsufficientBalance
from tests.conftest import tx


def bal(client, account_id):
    return client.get(f"/accounts/{account_id}/balance").json()["balance"]


def new_account(client, name="X"):
    return client.post("/accounts", json={"name": name}).json()["id"]


# ---------- 1. create account ----------

class TestCreateAccount:
    def test_creates_with_system_fields_and_zero_balance(self, client):
        r = client.post("/accounts", json={"name": "Aryan"})
        assert r.status_code == 201
        a = r.json()
        assert a["name"] == "Aryan" and a["balance"] == 0
        assert a["id"] and a["created_at"]

    def test_ids_are_unique(self, client):
        ids = {new_account(client) for _ in range(20)}
        assert len(ids) == 20

    @pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}, {"name": None}])
    def test_name_required(self, client, body):
        r = client.post("/accounts", json=body)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.parametrize("extra", [{"balance": 500}, {"id": "mine"}, {"created_at": "2020-01-01"}])
    def test_client_cannot_set_balance_id_or_created_at(self, client, extra):
        r = client.post("/accounts", json={"name": "A", **extra})
        assert r.status_code == 422
        assert client.get("/accounts/mine").status_code == 404

    def test_malformed_json(self, client):
        r = client.post("/accounts", content="{not json",
                        headers={"content-type": "application/json"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_REQUEST_BODY"


# ---------- 2. transactions: credit / debit ----------

class TestTransactions:
    def test_credit_increases_balance(self, client, account):
        # brief example: balance 100, credit 50 -> 150
        tx(client, account["id"], "credit", 100, "r1")
        r = tx(client, account["id"], "credit", 50, "r2")
        assert r.status_code == 201
        assert r.json()["balance_after"] == 150
        assert bal(client, account["id"]) == 150

    def test_debit_decreases_balance(self, client, account):
        # brief example: balance 100, debit 30 -> 70
        tx(client, account["id"], "credit", 100, "r1")
        r = tx(client, account["id"], "debit", 30, "r2")
        assert r.status_code == 201
        assert bal(client, account["id"]) == 70

    def test_debit_with_insufficient_balance_is_rejected(self, client, account):
        # brief example: balance 100, debit 150 -> rejected, balance stays 100
        tx(client, account["id"], "credit", 100, "r1")
        r = tx(client, account["id"], "debit", 150, "r2")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
        assert bal(client, account["id"]) == 100
        # and nothing was recorded for it
        history = client.get(f"/accounts/{account['id']}/transactions").json()
        assert history["pagination"]["total_records"] == 1

    def test_debit_of_exact_balance_is_allowed(self, client, account):
        tx(client, account["id"], "credit", 100, "r1")
        assert tx(client, account["id"], "debit", 100, "r2").status_code == 201
        assert bal(client, account["id"]) == 0

    def test_transaction_fields(self, client, account):
        r = tx(client, account["id"], "credit", 12.5, "ref-9", "salary").json()
        assert r["account_id"] == account["id"]
        assert r["type"] == "credit" and r["amount"] == 12.5
        assert r["description"] == "salary" and r["reference_id"] == "ref-9"
        assert r["id"] and r["created_at"]

    def test_decimals_have_no_rounding_errors(self, client, account):
        for i in range(10):
            tx(client, account["id"], "credit", 0.1, f"r{i}")
        assert bal(client, account["id"]) == 1.0   # float math would give 0.9999999

    def test_unknown_account(self, client):
        r = tx(client, "nope", "credit", 10, "r1")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


# ---------- duplicates (reference_id) ----------

class TestDuplicates:
    def test_same_reference_same_account_is_not_applied_twice(self, client, account):
        first = tx(client, account["id"], "credit", 100, "dup")
        second = tx(client, account["id"], "credit", 100, "dup")
        assert first.status_code == 201
        assert second.status_code == 409
        err = second.json()["error"]
        assert err["code"] == "DUPLICATE_TRANSACTION"
        # response points at the transaction that already exists
        assert err["details"]["transaction"]["id"] == first.json()["id"]
        assert bal(client, account["id"]) == 100
        assert client.get(f"/accounts/{account['id']}/transactions").json()["pagination"]["total_records"] == 1

    def test_duplicate_is_rejected_even_if_type_differs(self, client, account):
        tx(client, account["id"], "credit", 100, "dup")
        assert tx(client, account["id"], "debit", 10, "dup").status_code == 409
        assert bal(client, account["id"]) == 100

    def test_same_reference_on_different_accounts_is_fine(self, client):
        a, b = new_account(client), new_account(client)
        assert tx(client, a, "credit", 10, "shared").status_code == 201
        assert tx(client, b, "credit", 10, "shared").status_code == 201

    def test_database_enforces_uniqueness_too(self, client, account):
        tx(client, account["id"], "credit", 10, "r1")
        conn = db.connect()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO transactions (id, account_id, type, amount, reference_id, created_at)"
                " VALUES ('z', ?, 'credit', 1, 'r1', 'now')", (account["id"],))
        conn.close()


# ---------- 3. balance ----------

class TestBalance:
    def test_balance_response(self, client, account):
        tx(client, account["id"], "credit", 40, "r1")
        assert client.get(f"/accounts/{account['id']}/balance").json() == {
            "account_id": account["id"], "balance": 40}

    def test_unknown_account(self, client):
        r = client.get("/accounts/missing/balance")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


# ---------- 4. history: filter, pagination, sorting ----------

class TestHistory:
    @pytest.fixture()
    def acc(self, client):
        a = new_account(client)
        tx(client, a, "credit", 100, "c1")
        tx(client, a, "credit", 50, "c2")
        tx(client, a, "debit", 30, "d1")
        tx(client, a, "debit", 20, "d2")
        tx(client, a, "credit", 5, "c3")
        return a

    def test_latest_first(self, client, acc):
        refs = [t["reference_id"] for t in client.get(f"/accounts/{acc}/transactions").json()["data"]]
        assert refs == ["c3", "d2", "d1", "c2", "c1"]

    def test_filter_credit_and_debit(self, client, acc):
        c = client.get(f"/accounts/{acc}/transactions?type=credit").json()
        d = client.get(f"/accounts/{acc}/transactions?type=debit").json()
        assert [t["reference_id"] for t in c["data"]] == ["c3", "c2", "c1"]
        assert [t["reference_id"] for t in d["data"]] == ["d2", "d1"]
        assert c["pagination"]["total_records"] == 3

    def test_no_type_returns_both(self, client, acc):
        assert client.get(f"/accounts/{acc}/transactions").json()["pagination"]["total_records"] == 5

    def test_pagination_metadata_and_pages(self, client, acc):
        p1 = client.get(f"/accounts/{acc}/transactions?page=1&limit=2").json()
        p3 = client.get(f"/accounts/{acc}/transactions?page=3&limit=2").json()
        assert p1["pagination"] == {"current_page": 1, "page_size": 2,
                                    "total_records": 5, "total_pages": 3}
        assert [t["reference_id"] for t in p1["data"]] == ["c3", "d2"]
        assert [t["reference_id"] for t in p3["data"]] == ["c1"]

    def test_pages_do_not_overlap_and_cover_everything(self, client, acc):
        seen = []
        for page in (1, 2, 3):
            seen += [t["id"] for t in client.get(
                f"/accounts/{acc}/transactions?page={page}&limit=2").json()["data"]]
        assert len(seen) == len(set(seen)) == 5

    def test_page_past_the_end_is_empty_not_an_error(self, client, acc):
        r = client.get(f"/accounts/{acc}/transactions?page=99&limit=2")
        assert r.status_code == 200 and r.json()["data"] == []

    def test_filter_and_pagination_together(self, client, acc):
        r = client.get(f"/accounts/{acc}/transactions?type=credit&limit=2&page=2").json()
        assert [t["reference_id"] for t in r["data"]] == ["c1"]
        assert r["pagination"]["total_pages"] == 2

    def test_empty_history(self, client):
        r = client.get(f"/accounts/{new_account(client)}/transactions").json()
        assert r["data"] == []
        assert r["pagination"]["total_records"] == 0 and r["pagination"]["total_pages"] == 0

    @pytest.mark.parametrize("qs", ["page=0", "page=-1", "page=abc", "limit=0", "limit=101", "limit=x"])
    def test_invalid_pagination(self, client, acc, qs):
        r = client.get(f"/accounts/{acc}/transactions?{qs}")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_PAGINATION"

    def test_invalid_type_filter(self, client, acc):
        r = client.get(f"/accounts/{acc}/transactions?type=refund")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_TRANSACTION_TYPE"

    def test_unknown_account(self, client):
        assert client.get("/accounts/missing/transactions").status_code == 404


# ---------- 5. data integrity ----------

class TestIntegrity:
    def test_brief_balance_example(self, client, account):
        # initial 0; +100 +50 -30 -20 -> 100
        for kind, amt, ref in [("credit", 100, "a"), ("credit", 50, "b"),
                               ("debit", 30, "c"), ("debit", 20, "d")]:
            tx(client, account["id"], kind, amt, ref)
        assert bal(client, account["id"]) == 100

    def test_rejected_transactions_never_change_balance(self, client, account):
        tx(client, account["id"], "credit", 100, "ok")
        tx(client, account["id"], "debit", 500, "too-much")     # insufficient
        tx(client, account["id"], "credit", -5, "neg")          # invalid
        tx(client, account["id"], "credit", 10, "ok")           # duplicate
        assert bal(client, account["id"]) == 100

    def test_no_edit_or_delete_endpoints(self, client, account):
        t = tx(client, account["id"], "credit", 10, "r1").json()
        url = f"/accounts/{account['id']}/transactions/{t['id']}"
        for method in ("put", "patch", "delete"):
            r = client.request(method.upper(), url, json={"amount": 999})
            assert r.status_code == 405
            assert r.json()["error"]["code"] == "TRANSACTION_IMMUTABLE"
        assert client.get(url).json()["amount"] == 10
        assert bal(client, account["id"]) == 10

    def test_database_blocks_update_and_delete(self, client, account):
        tx(client, account["id"], "credit", 10, "r1")
        conn = db.connect()
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("UPDATE transactions SET amount = 999")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("DELETE FROM transactions")
        conn.close()

    def test_database_blocks_negative_balance(self, client, account):
        conn = db.connect()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE accounts SET balance = -1 WHERE id = ?", (account["id"],))
        conn.close()

    def test_balance_always_equals_sum_of_ledger(self, client):
        a = new_account(client)
        for i in range(40):
            kind = "credit" if i % 3 else "debit"
            tx(client, a, kind, 10 + i, f"r{i}")   # some debits will be rejected
        conn = db.connect()
        ledger = conn.execute(
            "SELECT COALESCE(SUM(CASE type WHEN 'credit' THEN amount ELSE -amount END),0) AS s "
            "FROM transactions WHERE account_id = ?", (a,)).fetchone()["s"]
        stored = conn.execute("SELECT balance FROM accounts WHERE id = ?", (a,)).fetchone()["balance"]
        conn.close()
        assert ledger == stored

    def test_atomicity_failure_after_balance_update_rolls_back(self, client, account, monkeypatch):
        """If recording the transaction fails after the balance was changed,
        the balance change must be undone."""
        tx(client, account["id"], "credit", 100, "seed")

        def boom(*a, **k):
            raise RuntimeError("disk exploded")
        with monkeypatch.context() as m:        # patch only for this one call
            m.setattr(service, "_insert_transaction", boom)
            r = tx(client, account["id"], "debit", 40, "will-fail")
        assert r.status_code == 500
        assert r.json()["error"]["code"] == "INTERNAL_ERROR"
        assert bal(client, account["id"]) == 100
        history = client.get(f"/accounts/{account['id']}/transactions").json()
        assert history["pagination"]["total_records"] == 1

    def test_concurrent_debits_cannot_overspend(self, client, account):
        tx(client, account["id"], "credit", 100, "seed")

        def attempt(i):
            try:
                service.add_transaction(account["id"], "debit", __import__("decimal").Decimal("10"),
                                        "", f"d{i}")
                return "ok"
            except InsufficientBalance:
                return "rejected"

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(attempt, range(30)))
        assert results.count("ok") == 10 and results.count("rejected") == 20
        assert bal(client, account["id"]) == 0

    def test_concurrent_same_reference_applied_once(self, client, account):
        from decimal import Decimal
        barrier = threading.Barrier(10)

        def attempt(_):
            barrier.wait()
            try:
                service.add_transaction(account["id"], "credit", Decimal("25"), "", "same-ref")
                return "ok"
            except DuplicateTransaction:
                return "dup"

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(attempt, range(10)))
        assert results.count("ok") == 1 and results.count("dup") == 9
        assert bal(client, account["id"]) == 25


# ---------- 7 & 8. validation and error handling ----------

class TestValidation:
    @pytest.mark.parametrize("amount", [0, -1, -0.01, "abc", None, True, [], 1.234, 10**13])
    def test_invalid_amount(self, client, account, amount):
        r = tx(client, account["id"], "credit", amount, "r1")
        assert r.status_code == 422, amount
        assert r.json()["error"]["code"] == "INVALID_AMOUNT"
        assert bal(client, account["id"]) == 0

    @pytest.mark.parametrize("kind", ["refund", "CREDIT", "", None, 1])
    def test_invalid_type(self, client, account, kind):
        r = tx(client, account["id"], kind, 10, "r1")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_TRANSACTION_TYPE"

    @pytest.mark.parametrize("ref", [None, "", "   "])
    def test_reference_id_required(self, client, account, ref):
        r = tx(client, account["id"], "credit", 10, ref)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_fields(self, client, account):
        r = client.post(f"/accounts/{account['id']}/transactions", json={})
        assert r.status_code == 422
        assert len(r.json()["error"]["details"]["errors"]) >= 3

    def test_body_must_be_an_object(self, client, account):
        r = client.post(f"/accounts/{account['id']}/transactions", json=[1, 2])
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_REQUEST_BODY"

    def test_every_error_has_the_same_shape(self, client, account):
        responses = [
            client.get("/accounts/nope/balance"),
            tx(client, account["id"], "credit", -1, "r"),
            client.get(f"/accounts/{account['id']}/transactions?page=0"),
            client.get("/no/such/route"),
            client.post("/accounts", json={}),
        ]
        for r in responses:
            assert r.status_code >= 400
            e = r.json()["error"]
            assert set(e) == {"code", "message", "details"}
            assert isinstance(e["code"], str) and isinstance(e["message"], str)

    def test_status_codes(self, client, account):
        assert client.post("/accounts", json={"name": "A"}).status_code == 201
        assert tx(client, account["id"], "credit", 10, "r").status_code == 201
        assert tx(client, account["id"], "credit", 10, "r").status_code == 409
        assert tx(client, account["id"], "debit", 999, "big").status_code == 422
        assert client.get("/accounts/zzz/balance").status_code == 404


# ---------- 9. bonus: transfers ----------

class TestTransfers:
    def _pair(self, client, a_bal=100, b_bal=50):
        a, b = new_account(client, "A"), new_account(client, "B")
        tx(client, a, "credit", a_bal, "seed-a")
        tx(client, b, "credit", b_bal, "seed-b")
        return a, b

    def test_brief_example(self, client):
        # A=100, B=50, transfer 30 from A to B -> A=70, B=80
        a, b = self._pair(client)
        r = client.post("/transfers", json={"from_account_id": a, "to_account_id": b,
                                            "amount": 30, "reference_id": "t1"})
        assert r.status_code == 201
        body = r.json()
        assert bal(client, a) == 70 and bal(client, b) == 80
        assert body["from_account_balance"] == 70 and body["to_account_balance"] == 80
        # both sides appear in each account's history, linked by transfer_id
        assert body["debit_transaction"]["transfer_id"] == body["credit_transaction"]["transfer_id"]
        assert body["debit_transaction"]["type"] == "debit"
        assert body["credit_transaction"]["type"] == "credit"

    def test_insufficient_balance_changes_nothing(self, client):
        a, b = self._pair(client)
        r = client.post("/transfers", json={"from_account_id": a, "to_account_id": b,
                                            "amount": 500, "reference_id": "t1"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
        assert bal(client, a) == 100 and bal(client, b) == 50

    def test_missing_destination_changes_nothing(self, client):
        a, _ = self._pair(client)
        r = client.post("/transfers", json={"from_account_id": a, "to_account_id": "ghost",
                                            "amount": 10, "reference_id": "t1"})
        assert r.status_code == 404
        assert bal(client, a) == 100

    def test_missing_source(self, client):
        _, b = self._pair(client)
        r = client.post("/transfers", json={"from_account_id": "ghost", "to_account_id": b,
                                            "amount": 10, "reference_id": "t1"})
        assert r.status_code == 404
        assert bal(client, b) == 50

    def test_same_account_rejected(self, client):
        a, _ = self._pair(client)
        r = client.post("/transfers", json={"from_account_id": a, "to_account_id": a,
                                            "amount": 10, "reference_id": "t1"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_TRANSFER"

    def test_duplicate_transfer_applied_once(self, client):
        a, b = self._pair(client)
        body = {"from_account_id": a, "to_account_id": b, "amount": 30, "reference_id": "t1"}
        assert client.post("/transfers", json=body).status_code == 201
        assert client.post("/transfers", json=body).status_code == 409
        assert bal(client, a) == 70 and bal(client, b) == 80

    def test_partial_transfer_is_impossible(self, client, monkeypatch):
        """Debit succeeds, then recording the credit fails -> everything rolls back."""
        a, b = self._pair(client)
        real = service._insert_transaction
        calls = {"n": 0}

        def fail_on_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("crash mid-transfer")
            return real(*args, **kwargs)
        with monkeypatch.context() as m:        # patch only for this one call
            m.setattr(service, "_insert_transaction", fail_on_second)
            r = client.post("/transfers", json={"from_account_id": a, "to_account_id": b,
                                                "amount": 30, "reference_id": "t1"})
        assert r.status_code == 500
        assert bal(client, a) == 100 and bal(client, b) == 50
        for acc in (a, b):
            assert client.get(f"/accounts/{acc}/transactions").json()["pagination"]["total_records"] == 1

    def test_concurrent_transfers_conserve_total_money(self, client):
        from decimal import Decimal
        a, b = self._pair(client, 100, 100)

        def go(i):
            src, dst = (a, b) if i % 2 == 0 else (b, a)
            try:
                service.transfer(src, dst, Decimal("15"), "", f"t{i}")
            except InsufficientBalance:
                pass

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(go, range(40)))
        assert bal(client, a) + bal(client, b) == 200
        assert bal(client, a) >= 0 and bal(client, b) >= 0

    def test_invalid_transfer_body(self, client):
        r = client.post("/transfers", json={"from_account_id": "a", "to_account_id": "b",
                                            "amount": 0, "reference_id": "t"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_AMOUNT"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
