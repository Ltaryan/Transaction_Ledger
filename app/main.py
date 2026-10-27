"""HTTP layer. Thin: validate, call the service, shape the response."""
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, service
from .errors import LedgerError
from .schemas import AccountCreate, TransactionCreate, TransferCreate

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def error_response(status: int, code: str, message: str, details=None) -> JSONResponse:
    """Every error in the API has this exact shape."""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def _code_for(loc: tuple, err_type: str) -> str:
    """Map a validation failure to a specific, stable error code."""
    if err_type in ("json_invalid", "model_attributes_type") or loc == ("body",):
        return "INVALID_REQUEST_BODY"
    if loc[:1] == ("query",) and loc[-1] in ("page", "limit"):
        return "INVALID_PAGINATION"
    if loc[-1] == "type":
        return "INVALID_TRANSACTION_TYPE"
    if loc[-1] == "amount":
        return "INVALID_AMOUNT"
    return "VALIDATION_ERROR"


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init_db()
        yield

    app = FastAPI(
        title="Transaction Ledger API",
        version="1.0.0",
        description="A simple ledger: accounts, credits, debits, history and transfers.",
        lifespan=lifespan,
    )

    # ---------- consistent error handling ----------

    @app.exception_handler(LedgerError)
    async def ledger_error(_: Request, exc: LedgerError):
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        errors = exc.errors()
        details = [
            {
                "field": ".".join(str(p) for p in e["loc"] if p not in ("body",)),
                "message": e["msg"],
            }
            for e in errors
        ]
        first = errors[0] if errors else {"loc": (), "type": ""}
        code = _code_for(tuple(first["loc"]), first["type"])
        return error_response(422, code, "Request validation failed.", {"errors": details})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return error_response(exc.status_code, codes.get(exc.status_code, "HTTP_ERROR"),
                              str(exc.detail))

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        return error_response(500, "INTERNAL_ERROR", "Something went wrong on our side.")

    # ---------- routes ----------

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/accounts", status_code=201)
    def create_account(body: AccountCreate):
        return service.create_account(body.name)

    @app.get("/accounts/{account_id}")
    def get_account(account_id: str):
        return service.get_account(account_id)

    @app.get("/accounts/{account_id}/balance")
    def get_balance(account_id: str):
        return service.get_balance(account_id)

    @app.post("/accounts/{account_id}/transactions", status_code=201)
    def add_transaction(account_id: str, body: TransactionCreate):
        return service.add_transaction(
            account_id, body.type, body.amount, body.description, body.reference_id
        )

    @app.get("/accounts/{account_id}/transactions")
    def list_transactions(
        account_id: str,
        type: Optional[Literal["credit", "debit"]] = Query(default=None),
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    ):
        return service.list_transactions(account_id, type, page, limit)

    @app.get("/accounts/{account_id}/transactions/{transaction_id}")
    def get_transaction(account_id: str, transaction_id: str):
        return service.get_transaction(account_id, transaction_id)

    # Transactions are immutable: say so explicitly instead of a vague 405.
    @app.api_route("/accounts/{account_id}/transactions/{transaction_id}",
                   methods=["PUT", "PATCH", "DELETE"], include_in_schema=False)
    def immutable(account_id: str, transaction_id: str):
        return error_response(
            405, "TRANSACTION_IMMUTABLE",
            "Transactions cannot be edited or deleted once created.")

    @app.post("/transfers", status_code=201)
    def transfer(body: TransferCreate):
        return service.transfer(
            body.from_account_id, body.to_account_id, body.amount,
            body.description, body.reference_id
        )

    return app


app = create_app()
