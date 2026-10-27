import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # every test gets its own empty database file
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "test.db"))
    from app.main import create_app
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def account(client):
    return client.post("/accounts", json={"name": "Aryan"}).json()


def tx(client, account_id, kind, amount, ref, description="t"):
    return client.post(
        f"/accounts/{account_id}/transactions",
        json={"type": kind, "amount": amount, "reference_id": ref, "description": description},
    )
