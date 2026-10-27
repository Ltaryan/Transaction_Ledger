"""Request validation models (Pydantic)."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# up to 10 digits before the decimal point and 2 after (e.g. 9,999,999,999.99)
Amount = Decimal


class _Strict(BaseModel):
    # Unknown fields are rejected. This is what stops a client from sending
    # "balance" (or id / created_at) when creating an account.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _reject_bool(v):
    # True/False would otherwise be coerced to 1/0
    if isinstance(v, bool):
        raise ValueError("amount must be a number")
    return v


class AccountCreate(_Strict):
    name: str = Field(min_length=1, max_length=100)


class TransactionCreate(_Strict):
    type: Literal["credit", "debit"]
    amount: Amount = Field(gt=0, max_digits=12, decimal_places=2)
    description: str = Field(default="", max_length=500)
    reference_id: str = Field(min_length=1, max_length=100)

    _amount_not_bool = field_validator("amount", mode="before")(_reject_bool)


class TransferCreate(_Strict):
    from_account_id: str = Field(min_length=1, max_length=100)
    to_account_id: str = Field(min_length=1, max_length=100)
    amount: Amount = Field(gt=0, max_digits=12, decimal_places=2)
    description: str = Field(default="", max_length=500)
    reference_id: str = Field(min_length=1, max_length=100)

    _amount_not_bool = field_validator("amount", mode="before")(_reject_bool)
