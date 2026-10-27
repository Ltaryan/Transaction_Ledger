"""Domain errors. Each carries the HTTP status and a stable machine-readable code."""


class LedgerError(Exception):
    status_code = 400
    code = "LEDGER_ERROR"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AccountNotFound(LedgerError):
    status_code = 404
    code = "ACCOUNT_NOT_FOUND"


class TransactionNotFound(LedgerError):
    status_code = 404
    code = "TRANSACTION_NOT_FOUND"


class InsufficientBalance(LedgerError):
    status_code = 422
    code = "INSUFFICIENT_BALANCE"


class DuplicateTransaction(LedgerError):
    status_code = 409
    code = "DUPLICATE_TRANSACTION"


class InvalidTransfer(LedgerError):
    status_code = 422
    code = "INVALID_TRANSFER"
