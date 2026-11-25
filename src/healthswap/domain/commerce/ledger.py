"""Double-entry ledger postings and statement totals (HLD 9.6 and 9.7; Commerce/Ledger.cs).

Corrections are always new reversing entries, never updates. Every posting is balanced and has no negative amounts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from healthswap.domain.primitives import round_money

_ZERO = Decimal(0)


class InvalidPostingError(ValueError):
    """C# InvalidOperationException raised by Postings.Make: negative amounts or an unbalanced posting."""


class Accounts:
    UNBILLED_AR = "UNBILLED_AR"
    BILLED_AR = "BILLED_AR"
    DEFERRED_REVENUE = "DEFERRED_REVENUE"
    FEE_REVENUE = "FEE_REVENUE"
    RECO_FEE_REVENUE = "RECO_FEE_REVENUE"
    OUTPUT_TAX = "OUTPUT_TAX"
    CASH = "CASH"


@dataclass(frozen=True, slots=True)
class PostingLine:
    account: str
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True, slots=True)
class LedgerPosting:
    """A balanced double-entry posting (HLD 9.6)."""

    kind: str
    reference_type: str
    reference_id: str
    idempotency_key: str
    lines: tuple[PostingLine, ...]

    @property
    def total_debit(self) -> Decimal:
        return sum((line.debit for line in self.lines), _ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((line.credit for line in self.lines), _ZERO)


class Postings:
    @staticmethod
    def fee_accrual(attribution_id: uuid.UUID, fee: Decimal) -> LedgerPosting:
        return Postings._make("FEE_ACCRUAL", "ATTRIBUTION", str(attribution_id), f"FEE_ACCRUAL:{attribution_id}",
                              (Accounts.UNBILLED_AR, fee, _ZERO), (Accounts.DEFERRED_REVENUE, _ZERO, fee))

    @staticmethod
    def fee_reversal(attribution_id: uuid.UUID, adjustment_id: str, amount: Decimal) -> LedgerPosting:
        return Postings._make("FEE_REVERSAL", "ATTRIBUTION", str(attribution_id), f"FEE_REVERSAL:{attribution_id}:{adjustment_id}",
                              (Accounts.DEFERRED_REVENUE, amount, _ZERO), (Accounts.UNBILLED_AR, _ZERO, amount))

    @staticmethod
    def fee_finalize(attribution_id: uuid.UUID, amount: Decimal) -> LedgerPosting:
        return Postings._make("FEE_FINALIZE", "ATTRIBUTION", str(attribution_id), f"FEE_FINALIZE:{attribution_id}",
                              (Accounts.DEFERRED_REVENUE, amount, _ZERO), (Accounts.FEE_REVENUE, _ZERO, amount))

    @staticmethod
    def reco_fee(charge_id: uuid.UUID, amount: Decimal) -> LedgerPosting:
        """RA-1: recognized when the charge becomes FINAL (no return contingency); billed at month close."""
        return Postings._make("RECO_FEE", "RECO_CHARGE", str(charge_id), f"RECO_FEE:{charge_id}",
                              (Accounts.UNBILLED_AR, amount, _ZERO), (Accounts.RECO_FEE_REVENUE, _ZERO, amount))

    @staticmethod
    def statement(statement_id: uuid.UUID, net_fee: Decimal, tax: Decimal) -> LedgerPosting:
        return Postings._make("STATEMENT", "STATEMENT", str(statement_id), f"STATEMENT:{statement_id}",
                              (Accounts.BILLED_AR, net_fee + tax, _ZERO), (Accounts.UNBILLED_AR, _ZERO, net_fee), (Accounts.OUTPUT_TAX, _ZERO, tax))

    @staticmethod
    def payment(payment_ref: str, statement_id: uuid.UUID, amount: Decimal) -> LedgerPosting:
        return Postings._make("PAYMENT", "PAYMENT", payment_ref, f"PAYMENT:{statement_id}:{payment_ref}",
                              (Accounts.CASH, amount, _ZERO), (Accounts.BILLED_AR, _ZERO, amount))

    @staticmethod
    def credit_note(dispute_id: uuid.UUID, premium_part: Decimal, reco_part: Decimal, tax: Decimal) -> LedgerPosting:
        """Credit note after a dispute is upheld on an issued statement: revenue contra + output tax, against billed AR."""
        return Postings._make("CREDIT_NOTE", "DISPUTE", str(dispute_id), f"CREDIT_NOTE:{dispute_id}",
                              (Accounts.FEE_REVENUE, premium_part, _ZERO), (Accounts.RECO_FEE_REVENUE, reco_part, _ZERO), (Accounts.OUTPUT_TAX, tax, _ZERO),
                              (Accounts.BILLED_AR, _ZERO, premium_part + reco_part + tax))

    @staticmethod
    def _make(kind: str, ref_type: str, ref_id: str, key: str, *lines: tuple[str, Decimal, Decimal]) -> LedgerPosting:
        posting = LedgerPosting(kind, ref_type, ref_id, key, tuple(PostingLine(a, d, c) for a, d, c in lines if d != 0 or c != 0))
        if any(line.debit < 0 or line.credit < 0 for line in posting.lines):
            raise InvalidPostingError("Negative posting amounts are not allowed")
        if posting.total_debit != posting.total_credit:
            raise InvalidPostingError(f"Unbalanced posting {kind}")
        return posting


@dataclass(frozen=True, slots=True)
class StatementTotalsData:
    """Statement totals (HLD 9.7, OpenAPI StatementTotals): gross = FP-1 + RA-1; net = gross - reversals - credits."""

    currency: str
    premium_fee: Decimal
    reco_fee: Decimal
    reco_charge_count: int
    reversals: Decimal
    credits: Decimal
    tax_rate: Decimal
    credit_tax: Decimal

    @property
    def gross_fee(self) -> Decimal:
        return self.premium_fee + self.reco_fee

    @property
    def net_fee(self) -> Decimal:
        return self.gross_fee - self.reversals - self.credits

    @property
    def tax(self) -> Decimal:
        """Tax on the originally billed net fee minus the tax already credited (keeps the ledger exact)."""
        if self.currency is None:
            return _ZERO
        return round_money((self.gross_fee - self.reversals) * self.tax_rate, self.currency) - self.credit_tax

    @property
    def total_due(self) -> Decimal:
        return self.net_fee + self.tax
