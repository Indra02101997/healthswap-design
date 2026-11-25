"""Services/BillingService.cs ported (HLD 9.6-9.8): month close per partner and currency (FINAL FP-1 attributions + FINAL
RA-1 charges), invoice numbers, STATEMENT postings, disputes with credit notes, payments and the trial balance.

Results are the camelCase dicts of api/openapi.yaml (Statement, StatementList, StatementLinePage, Dispute; ids and timestamps
as strings, amounts as Money dicts, None members omitted) and the records StatementCloseResult / TrialBalanceRow of the
admin plane and the CLI.

Port notes:
    * SQL, role switches, invoice numbers (HS-{geography}-{yyyy}-{MM}-{seq:D6}), statement line order, ledger postings,
      webhooks, the audit row and every message are those of the C#. uuid[] parameters that may be empty get an explicit
      cast (any(@ids::uuid[]), @charges::uuid[]): Npgsql typed Guid[] itself, psycopg cannot type an empty list.
    * The arithmetic is in pure static helpers (invoice_number, statement_lines, statement_totals, dispute_window_closed,
      after_credit_note, credit_note_tax, balanced) that are unit-tested without a database.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from healthswap.domain.commerce.ledger import Postings, StatementTotalsData
from healthswap.domain.knowledge.products import decimal_mul, round_decimal
from healthswap.domain.primitives import Clock, iso, minor_units, new_id, period_try_parse
from healthswap.platform import netjson
from healthswap.platform.api_mapper import ApiMapper
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.errors import HsError
from healthswap.platform.services.events import Cursor
from healthswap.platform.services.infrastructure import LedgerService, WebhookFanoutConsumer, WebhookTypes
from healthswap.platform.tenancy import PartnerDirectory, PartnerInfo

__all__ = ["BillingService", "DisputeRequest", "DisputeResolution", "DisputedLine", "StatementCloseResult", "TrialBalanceRow"]

log = logging.getLogger(__name__)

_ZERO = Decimal(0)
_DECISIONS = ("UPHELD", "PARTIALLY_UPHELD", "REJECTED", "UNDER_REVIEW")
_RESOLVED = ("UPHELD", "PARTIALLY_UPHELD", "REJECTED")
_CLOSABLE_PARTNER_STATES = ("LIVE", "SHADOW", "SANDBOX")


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drops None members (JsonIgnoreCondition.WhenWritingNull)."""
    return {k: v for k, v in d.items() if v is not None}


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return minimum if value < minimum else maximum if value > maximum else value


# ---------------------------------------------------------------------------------------------------------------------
# Records and requests
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatementCloseResult:
    """One partner's month-close outcome: ISSUED, ALREADY_CLOSED or NOTHING_TO_BILL."""

    partner_id: uuid.UUID
    period: str
    statement_id: uuid.UUID | None
    invoice_number: str | None
    premium_fee: Decimal
    reco_fee: Decimal
    reco_charge_count: int
    reversals: Decimal
    tax: Decimal
    total_due: Decimal
    outcome: str


@dataclass(frozen=True, slots=True)
class TrialBalanceRow:
    account: str
    currency: str
    debit: Decimal
    credit: Decimal
    balance: Decimal


def _obj(m: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(m, Mapping):
        raise HsError.invalid(f"{what} must be an object")
    return m


def _str(m: Mapping[str, Any], key: str, what: str, required: bool = False) -> str | None:
    value = m.get(key)
    if value is None:
        if required:
            raise HsError.invalid(f"{what}.{key} is required")
        return None
    if not isinstance(value, str):
        raise HsError.invalid(f"{what}.{key} must be a string")
    return value


def _uuid(value: Any, what: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        if not isinstance(value, str):
            raise ValueError(value)
        return uuid.UUID(value.strip())
    except ValueError:
        raise HsError.invalid(f"{what} must be a UUID") from None


def _array(m: Mapping[str, Any], key: str, what: str) -> list[Any] | None:
    value = m.get(key)
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise HsError.invalid(f"{what}.{key} must be an array")
    return list(value)


@dataclass(frozen=True, slots=True)
class DisputedLine:
    """Contracts.DisputedLine (record equality: same orderId and orderLineId)."""

    order_id: str
    order_line_id: str

    @staticmethod
    def from_api(m: Any, what: str = "line") -> DisputedLine:
        if isinstance(m, DisputedLine):
            return m
        m = _obj(m, what)
        return DisputedLine(_str(m, "orderId", what, True) or "", _str(m, "orderLineId", what, True) or "")

    def to_api(self) -> dict[str, Any]:
        return {"orderId": self.order_id, "orderLineId": self.order_line_id}


def _lines(m: Mapping[str, Any], key: str, what: str) -> tuple[DisputedLine, ...] | None:
    items = _array(m, key, what)
    return None if items is None else tuple(DisputedLine.from_api(x, f"{key}[{i}]") for i, x in enumerate(items))


def _ids(m: Mapping[str, Any], key: str, what: str) -> tuple[uuid.UUID, ...] | None:
    items = _array(m, key, what)
    return None if items is None else tuple(_uuid(x, f"{key}[{i}]") for i, x in enumerate(items))


@dataclass(frozen=True, slots=True)
class DisputeRequest:
    """Contracts.DisputeRequest."""

    statement_id: uuid.UUID
    reason: str
    lines: tuple[DisputedLine, ...] | None = None
    recommendation_charge_ids: tuple[uuid.UUID, ...] | None = None
    comment: str | None = None

    @staticmethod
    def from_api(req: Any) -> DisputeRequest:
        if isinstance(req, DisputeRequest):
            return req
        what = "request"
        req = _obj(req, what)
        statement_id = req.get("statementId")
        if statement_id is None:
            raise HsError.invalid("request.statementId is required")
        return DisputeRequest(
            _uuid(statement_id, "request.statementId"), _str(req, "reason", what, True) or "", _lines(req, "lines", what),
            _ids(req, "recommendationChargeIds", what), _str(req, "comment", what),
        )


@dataclass(frozen=True, slots=True)
class DisputeResolution:
    """Admin decision on a dispute: UPHELD, PARTIALLY_UPHELD (only the listed lines / charges), REJECTED or UNDER_REVIEW."""

    decision: str
    upheld_lines: tuple[DisputedLine, ...] | None = None
    upheld_charge_ids: tuple[uuid.UUID, ...] | None = None
    resolution: str | None = None

    @staticmethod
    def from_api(req: Any) -> DisputeResolution:
        if isinstance(req, DisputeResolution):
            return req
        what = "request"
        req = _obj(req, what)
        return DisputeResolution(
            _str(req, "decision", what, True) or "", _lines(req, "upheldLines", what), _ids(req, "upheldChargeIds", what), _str(req, "resolution", what),
        )


# ---------------------------------------------------------------------------------------------------------------------
# BillingService
# ---------------------------------------------------------------------------------------------------------------------


class BillingService:
    """
    Billing (HLD 9.6-9.8): month close per partner and currency (FINAL FP-1 attributions + FINAL RA-1 charges), invoice numbers,
    STATEMENT postings, disputes with credit notes, payments and a trial balance for the admin plane.
    """

    STATEMENT_COLUMNS = """
        statement_id, partner_id, period_start, currency, status, premium_fee, reco_fee, reco_charge_count, gross_fee, reversals, credits, net_fee, tax_rate,
        tax_amount, total_due, paid_amount, line_count, invoice_number, issued_at, due_at
        """

    DISPUTE_COLUMNS = (
        "dispute_id, partner_id, statement_id, reason, disputed_lines::text as disputed_lines, disputed_charge_ids, comment, status, credit_note_amount, "
        "credit_note_tax, opened_at, resolved_at, resolution"
    )

    def __init__(self, db: Db, ledger: LedgerService, partners: PartnerDirectory, clock: Clock) -> None:
        self.db = db
        self.ledger = ledger
        self.partners = partners
        self.clock = clock

    # ---- pure helpers -----------------------------------------------------------------------------------------------

    @staticmethod
    def invoice_number(geography: str, period_start: date, seq: int) -> str:
        """HS-{geography}-{yyyy}-{MM}-{seq:D6}, for example HS-IN-2026-11-000042."""
        return f"HS-{geography}-{period_start.year:04d}-{period_start.month:02d}-{seq:06d}"

    @staticmethod
    def statement_lines(attributions: Sequence[Mapping[str, Any]]) -> list[tuple[int, str, Mapping[str, Any], Decimal]]:
        """(line_no, type, attribution, amount): a FEE line per attribution, followed by a negative REVERSAL line when reversed."""
        lines: list[tuple[int, str, Mapping[str, Any], Decimal]] = []
        for a in attributions:
            lines.append((len(lines) + 1, "FEE", a, a["fee_amount"]))
            if a["reversed_amount"] > 0:
                lines.append((len(lines) + 1, "REVERSAL", a, -a["reversed_amount"]))
        return lines

    @staticmethod
    def statement_totals(currency: str, attributions: Sequence[Mapping[str, Any]], charges: Sequence[Mapping[str, Any]],
                         tax_rate: Decimal) -> StatementTotalsData:
        """FP-1 fees and reversals of the FINAL attributions plus the FINAL RA-1 charges, taxed at the partner's rate."""
        return StatementTotalsData(
            currency, sum((a["fee_amount"] for a in attributions), _ZERO), sum((c["amount"] for c in charges), _ZERO), len(charges),
            sum((a["reversed_amount"] for a in attributions), _ZERO), _ZERO, tax_rate, _ZERO,
        )

    @staticmethod
    def dispute_window_closed(issued_at: datetime | None, now: datetime, window_days: int) -> bool:
        """True once `now` is past issued_at + the partner's dispute window (unissued statements have no window)."""
        return issued_at is not None and now > issued_at + timedelta(days=window_days)

    @staticmethod
    def credit_note_tax(premium_part: Decimal, reco_part: Decimal, tax_rate: Decimal, currency: str) -> Decimal:
        """Output tax reversed by a credit note: Currency.Round((premium + reco) x tax rate)."""
        return round_decimal(decimal_mul(premium_part + reco_part, tax_rate), minor_units(currency))

    @staticmethod
    def after_credit_note(st: Mapping[str, Any], premium_part: Decimal, reco_part: Decimal, tax: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Statement (credits, tax_amount, net_fee, total_due) after a credit note."""
        credits = st["credits"] + premium_part + reco_part
        tax_amount = st["tax_amount"] - tax
        net = st["gross_fee"] - st["reversals"] - credits
        return credits, tax_amount, net, net + tax_amount

    @staticmethod
    def balanced(rows: Sequence[TrialBalanceRow]) -> bool:
        """The admin trial-balance check: per currency, the account balances sum to zero."""
        sums: dict[str, Decimal] = {}
        for r in rows:
            sums[r.currency] = sums.get(r.currency, _ZERO) + r.balance
        return all(v == 0 for v in sums.values())

    # ---- month close ------------------------------------------------------------------------------------------------

    def close_month(self, period: str, partner_id: uuid.UUID | None = None) -> list[StatementCloseResult]:
        """Closes `period` (YYYY-MM) for one partner, or for every LIVE, SHADOW and SANDBOX partner."""
        parsed = period_try_parse(period)
        if parsed is None:
            raise HsError.invalid("period must be YYYY-MM")
        start, end = parsed
        if partner_id is not None:
            targets = [self.partners.get(partner_id)]
        else:
            targets = [p for p in self.partners.list_all() if p.status in _CLOSABLE_PARTNER_STATES]
        return [self._close(partner, period, start, end) for partner in targets]

    def _close(self, partner: PartnerInfo, period: str, start: datetime, end: datetime) -> StatementCloseResult:
        cfg = partner.config
        currency = cfg.currency
        now = self.clock.utc_now()

        def work(u: Uow) -> StatementCloseResult:
            existing = u.one(
                f"select {self.STATEMENT_COLUMNS} from billing.statement where period_start = @start and currency = @currency",
                {"start": start.date(), "currency": currency},
            )
            if existing is not None:
                return StatementCloseResult(
                    partner.partner_id, period, existing["statement_id"], existing["invoice_number"], existing["premium_fee"], existing["reco_fee"],
                    existing["reco_charge_count"], existing["reversals"], existing["tax_amount"], existing["total_due"], "ALREADY_CLOSED",
                )
            attributions = u.query(
                """
                select a.attribution_id, a.order_id, a.line_id, a.suggestion_id, a.fair_premium, a.fee_amount, a.reversed_amount, h.delivered_at
                from txn.attribution a
                left join txn.order_header h on h.partner_id = a.partner_id and h.order_id = a.order_id
                where a.state = 'FINAL' and a.statement_id is null and a.finalized_at < @end and a.currency = @currency
                order by a.order_id, a.line_id
                """,
                {"end": end, "currency": currency},
            )
            charges = u.query(
                "select charge_id, amount, credited_amount from txn.reco_charge where state = 'FINAL' and statement_id is null and finalized_at < @end and currency = @currency",
                {"end": end, "currency": currency},
            )
            if not attributions and not charges:
                return StatementCloseResult(partner.partner_id, period, None, None, _ZERO, _ZERO, 0, _ZERO, _ZERO, _ZERO, "NOTHING_TO_BILL")

            totals = self.statement_totals(currency, attributions, charges, cfg.tax_rate)
            statement_id = new_id()
            seq = int(u.scalar("select nextval('billing.invoice_no_seq')"))
            invoice = self.invoice_number(partner.geography, start, seq)
            lines = self.statement_lines(attributions)
            u.execute(
                """
                insert into billing.statement (statement_id, partner_id, period_start, period_end, currency, status, premium_fee, reco_fee, reco_charge_count, gross_fee,
                    reversals, credits, net_fee, tax_rate, tax_amount, total_due, paid_amount, line_count, invoice_number, issued_at, due_at)
                values (@statementId, @p, @start, @periodEnd, @currency, 'ISSUED', @premium, @reco, @count, @gross, @reversals, 0, @net, @rate, @tax, @due, 0, @lines,
                    @invoice, @now, @dueAt)
                """,
                {
                    "statementId": statement_id, "p": partner.partner_id, "start": start.date(), "periodEnd": (end - timedelta(days=1)).date(),
                    "currency": currency, "premium": totals.premium_fee, "reco": totals.reco_fee, "count": totals.reco_charge_count, "gross": totals.gross_fee,
                    "reversals": totals.reversals, "net": totals.net_fee, "rate": cfg.tax_rate, "tax": totals.tax, "due": totals.total_due, "lines": len(lines),
                    "invoice": invoice, "now": now, "dueAt": now + timedelta(days=cfg.payment_terms_days),
                },
            )
            for no, line_type, a, amount in lines:
                u.execute(
                    """
                    insert into billing.statement_line (statement_id, line_no, partner_id, attribution_id, line_type, order_id, order_line_id, suggestion_id, delivered_at, fair_premium, amount)
                    values (@statementId, @no, @p, @aid, @type, @o, @l, @sid, @delivered, @premium, @amount)
                    """,
                    {
                        "statementId": statement_id, "no": no, "p": partner.partner_id, "aid": a["attribution_id"], "type": line_type, "o": a["order_id"],
                        "l": a["line_id"], "sid": a["suggestion_id"], "delivered": a["delivered_at"], "premium": a["fair_premium"], "amount": amount,
                    },
                )
            u.execute("update txn.attribution set statement_id = @statementId where attribution_id = any(@ids::uuid[])",
                      {"statementId": statement_id, "ids": [a["attribution_id"] for a in attributions]})
            u.execute("update txn.reco_charge set statement_id = @statementId where charge_id = any(@ids::uuid[])",
                      {"statementId": statement_id, "ids": [c["charge_id"] for c in charges]})
            if totals.net_fee + totals.tax > 0:
                self.ledger.post(u, partner.partner_id, currency, Postings.statement(statement_id, totals.net_fee, totals.tax), "month-close", now)
            WebhookFanoutConsumer.request(u, partner.partner_id, WebhookTypes.STATEMENT_ISSUED, {
                "statementId": statement_id, "period": period, "totalDue": ApiMapper.money(totals.total_due, currency),
            })
            log.info(
                "Statement %s for %s %s: FP-1 %s + RA-1 %s (%d adds) - reversals %s + tax %s = %s %s", invoice, partner.display_name, period,
                totals.premium_fee, totals.reco_fee, totals.reco_charge_count, totals.reversals, totals.tax, totals.total_due, currency,
            )
            return StatementCloseResult(
                partner.partner_id, period, statement_id, invoice, totals.premium_fee, totals.reco_fee, totals.reco_charge_count, totals.reversals,
                totals.tax, totals.total_due, "ISSUED",
            )

        return self.db.in_tx(DbRole.LEDGER_WRITER, partner.partner_id, work)

    # ---- statements -------------------------------------------------------------------------------------------------

    def list(self, partner_id: uuid.UUID, period: str | None = None, status: str | None = None, cursor: str | None = None,
             limit: int = 100) -> dict[str, Any]:
        """StatementList {"items": [...], "nextCursor": ...}: newest period first, offset-paginated (1-200 per page)."""
        start: date | None = None
        if period is not None:
            parsed = period_try_parse(period)
            if parsed is None:
                raise HsError.invalid("period must be YYYY-MM")
            start = parsed[0].date()
        offset = Cursor.decode_offset(cursor)
        limit = _clamp(limit, 1, 200)
        rows = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.query(
            f"select {self.STATEMENT_COLUMNS} from billing.statement where (@start::date is null or period_start = @start) and (@status::text is null or status = @status) "
            "order by period_start desc, statement_id offset @offset limit @take",
            {"start": start, "status": status, "offset": offset, "take": limit + 1},
        ))
        return _compact({
            "items": [self.to_api(r) for r in rows[:limit]], "nextCursor": Cursor.encode_offset(offset + limit) if len(rows) > limit else None,
        })

    def get(self, partner_id: uuid.UUID, statement_id: uuid.UUID) -> dict[str, Any]:
        """Statement (404 when unknown to this partner)."""
        row = self.db.in_tx(DbRole.APP, partner_id, lambda u: u.one(
            f"select {self.STATEMENT_COLUMNS} from billing.statement where statement_id = @statementId", {"statementId": statement_id}))
        if row is None:
            raise HsError.not_found(f"Statement {statement_id}")
        return self.to_api(row)

    def lines(self, partner_id: uuid.UUID, statement_id: uuid.UUID, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        """StatementLinePage {"items": [...], "nextCursor": ...} in line order (1-500 per page)."""
        offset = Cursor.decode_offset(cursor)
        limit = _clamp(limit, 1, 500)

        def work(u: Uow) -> tuple[str, list[dict[str, Any]]]:
            c = u.scalar("select currency from billing.statement where statement_id = @statementId", {"statementId": statement_id})
            if c is None:
                raise HsError.not_found(f"Statement {statement_id}")
            r = u.query(
                "select line_no, line_type, order_id, order_line_id, suggestion_id, delivered_at, fair_premium, amount, attribution_id from billing.statement_line "
                "where statement_id = @statementId order by line_no offset @offset limit @take",
                {"statementId": statement_id, "offset": offset, "take": limit + 1},
            )
            return c.strip(), r

        currency, rows = self.db.in_tx(DbRole.APP, partner_id, work)
        return _compact({
            "items": [_compact({
                "lineNo": x["line_no"], "type": x["line_type"], "orderId": x["order_id"], "orderLineId": x["order_line_id"],
                "suggestionId": str(x["suggestion_id"]), "deliveredAt": iso(x["delivered_at"]), "fairPremium": ApiMapper.money(x["fair_premium"], currency),
                "amount": ApiMapper.money(x["amount"], currency),
            }) for x in rows[:limit]],
            "nextCursor": Cursor.encode_offset(offset + limit) if len(rows) > limit else None,
        })

    # ---- disputes ---------------------------------------------------------------------------------------------------

    def open_dispute(self, partner_id: uuid.UUID, request: Mapping[str, Any] | DisputeRequest) -> dict[str, Any]:
        """DisputeRequest -> Dispute (OPEN); the statement becomes DISPUTED."""
        req = DisputeRequest.from_api(request)
        if not req.lines and not req.recommendation_charge_ids:
            raise HsError.invalid("Provide lines, recommendationChargeIds or both")
        partner = self.partners.get(partner_id)
        dispute_id = new_id()
        now = self.clock.utc_now()

        def work(u: Uow) -> None:
            st = u.one(f"select {self.STATEMENT_COLUMNS} from billing.statement where statement_id = @id", {"id": req.statement_id})
            if st is None:
                raise HsError.not_found(f"Statement {req.statement_id}")
            if st["status"] not in ("ISSUED", "DISPUTED"):
                raise HsError.unprocessable("HS-DISPUTE-STATE", f"Statements in status {st['status']} cannot be disputed")
            if self.dispute_window_closed(st["issued_at"], now, partner.config.dispute_window_days):
                raise HsError.unprocessable("HS-DISPUTE-WINDOW-CLOSED", f"The dispute window of {partner.config.dispute_window_days} days has closed")
            for x in req.lines or ():
                if not u.scalar(
                    "select exists (select 1 from billing.statement_line where statement_id = @s and order_id = @o and order_line_id = @l and line_type = 'FEE')",
                    {"s": req.statement_id, "o": x.order_id, "l": x.order_line_id},
                ):
                    raise HsError.unprocessable("HS-DISPUTE-LINE-UNKNOWN", f"Order line {x.order_id}/{x.order_line_id} is not billed on this statement")
            charges = list(dict.fromkeys(req.recommendation_charge_ids or ()))
            if charges:
                on_statement = u.scalar(
                    "select count(*)::int from txn.reco_charge where statement_id = @s and charge_id = any(@ids) and state = 'FINAL'",
                    {"s": req.statement_id, "ids": charges},
                )
                if on_statement != len(charges):
                    raise HsError.unprocessable("HS-DISPUTE-CHARGE-UNKNOWN", "Some recommendation charges are not billed (FINAL) on this statement")
            u.execute(
                """
                insert into billing.dispute (dispute_id, partner_id, statement_id, reason, disputed_lines, disputed_charge_ids, comment, status, opened_at)
                values (@id, @p, @s, @reason, @lines::jsonb, @charges::uuid[], @comment, 'OPEN', @now)
                """,
                {
                    "id": dispute_id, "p": partner_id, "s": req.statement_id, "reason": req.reason,
                    "lines": netjson.serialize_hs([x.to_api() for x in req.lines or ()]), "charges": charges, "comment": req.comment, "now": now,
                },
            )
            u.set_role(DbRole.LEDGER_WRITER)
            u.execute("update billing.statement set status = 'DISPUTED' where statement_id = @s and status = 'ISSUED'", {"s": req.statement_id})

        self.db.in_tx(DbRole.APP, partner_id, work)
        return self.get_dispute(partner_id, dispute_id)

    def get_dispute(self, partner_id: uuid.UUID, dispute_id: uuid.UUID) -> dict[str, Any]:
        """Dispute (404 when unknown to this partner)."""

        def work(u: Uow) -> tuple[dict[str, Any], str]:
            r = u.one(f"select {self.DISPUTE_COLUMNS} from billing.dispute where dispute_id = @disputeId", {"disputeId": dispute_id})
            if r is None:
                raise HsError.not_found(f"Dispute {dispute_id}")
            c = u.scalar("select currency from billing.statement where statement_id = @s", {"s": r["statement_id"]})
            return r, c.strip()

        row, currency = self.db.in_tx(DbRole.APP, partner_id, work)
        return self.dispute_to_api(row, currency)

    def resolve_dispute(self, dispute_id: uuid.UUID, decision: Mapping[str, Any] | DisputeResolution, actor: str) -> dict[str, Any]:
        """Admin plane: resolves a dispute. Upheld lines and charges are credited with a credit note (revenue contra + output tax)."""
        d_req = DisputeResolution.from_api(decision)
        if d_req.decision not in _DECISIONS:
            raise HsError.invalid("decision must be UPHELD, PARTIALLY_UPHELD, REJECTED or UNDER_REVIEW")
        partner_id = self.db.in_tx(DbRole.OPS, None, lambda u: u.scalar(
            "select partner_id from billing.dispute where dispute_id = @disputeId", {"disputeId": dispute_id}))
        if partner_id is None:
            raise HsError.not_found(f"Dispute {dispute_id}")
        now = self.clock.utc_now()

        def work(u: Uow) -> None:
            d = u.single(f"select {self.DISPUTE_COLUMNS} from billing.dispute where dispute_id = @disputeId", {"disputeId": dispute_id})
            if d["status"] in _RESOLVED:
                raise HsError.conflict("HS-DISPUTE-RESOLVED", "The dispute is already resolved")
            st = u.single(f"select {self.STATEMENT_COLUMNS} from billing.statement where statement_id = @s", {"s": d["statement_id"]})
            currency = st["currency"].strip()
            premium_part = reco_part = tax = _ZERO
            if d_req.decision in ("UPHELD", "PARTIALLY_UPHELD"):
                disputed_lines = self._disputed_lines(d["disputed_lines"])
                disputed_charges: list[uuid.UUID] = list(d["disputed_charge_ids"] or [])
                if d_req.decision == "UPHELD":
                    lines, charge_ids = disputed_lines, disputed_charges
                else:
                    lines = [x for x in d_req.upheld_lines or () if x in disputed_lines]
                    charge_ids = [c for c in d_req.upheld_charge_ids or () if c in disputed_charges]
                for x in lines:
                    a = u.one(
                        "select attribution_id, order_id, line_id, suggestion_id, fair_premium, fee_amount, reversed_amount from txn.attribution "
                        "where order_id = @o and line_id = @l and statement_id = @s and state = 'FINAL'",
                        {"o": x.order_id, "l": x.order_line_id, "s": d["statement_id"]},
                    )
                    if a is None:
                        continue
                    premium_part += a["fee_amount"] - a["reversed_amount"]
                    u.execute("update txn.attribution set state = 'CREDITED' where attribution_id = @id", {"id": a["attribution_id"]})
                    no = u.scalar("select coalesce(max(line_no), 0) + 1 from billing.statement_line where statement_id = @s", {"s": d["statement_id"]})
                    u.execute(
                        """
                        insert into billing.statement_line (statement_id, line_no, partner_id, attribution_id, line_type, order_id, order_line_id, suggestion_id, fair_premium, amount)
                        values (@s, @no, @p, @aid, 'CREDIT', @o, @l, @sid, @premium, @amount)
                        """,
                        {
                            "s": d["statement_id"], "no": no, "p": partner_id, "aid": a["attribution_id"], "o": a["order_id"], "l": a["line_id"],
                            "sid": a["suggestion_id"], "premium": a["fair_premium"], "amount": -(a["fee_amount"] - a["reversed_amount"]),
                        },
                    )
                for c in charge_ids:
                    charge = u.one(
                        "select charge_id, amount, credited_amount from txn.reco_charge where charge_id = @c and statement_id = @s and state = 'FINAL'",
                        {"c": c, "s": d["statement_id"]},
                    )
                    if charge is None:
                        continue
                    reco_part += charge["amount"] - charge["credited_amount"]
                    u.execute("update txn.reco_charge set state = 'CREDITED', credited_amount = amount where charge_id = @c", {"c": c})
                tax = self.credit_note_tax(premium_part, reco_part, st["tax_rate"], currency)
                if premium_part + reco_part > 0:
                    self.ledger.post(u, partner_id, currency, Postings.credit_note(dispute_id, premium_part, reco_part, tax), actor, now)
                credits, tax_amount, net, total = self.after_credit_note(st, premium_part, reco_part, tax)
                u.execute(
                    "update billing.statement set credits = @credits, net_fee = @net, tax_amount = @taxAmount, total_due = @total, "
                    "line_count = (select count(*) from billing.statement_line where statement_id = @s), "
                    "status = case when paid_amount >= @total then 'SETTLED' else 'ISSUED' end where statement_id = @s",
                    {"credits": credits, "net": net, "taxAmount": tax_amount, "total": total, "s": d["statement_id"]},
                )
            elif d_req.decision == "REJECTED":
                u.execute(
                    "update billing.statement set status = case when paid_amount >= total_due then 'SETTLED' else 'ISSUED' end where statement_id = @s and status = 'DISPUTED'",
                    {"s": d["statement_id"]},
                )
            credited = premium_part + reco_part > 0
            u.execute(
                "update billing.dispute set status = @status, credit_note_amount = @amount, credit_note_tax = @tax, resolution = @resolution, resolved_at = @at where dispute_id = @disputeId",
                {
                    "status": d_req.decision, "amount": premium_part + reco_part + tax if credited else None, "tax": tax if credited else None,
                    "resolution": d_req.resolution, "at": now if d_req.decision != "UNDER_REVIEW" else None, "disputeId": dispute_id,
                },
            )
            u.execute(
                "insert into audit.change_log (actor, action, entity_type, entity_id, details) values (@actor, 'DISPUTE_' || @status, 'DISPUTE', @id, @details::jsonb)",
                {
                    "actor": actor, "status": d_req.decision, "id": str(dispute_id),
                    "details": netjson.to_json_string({"premiumPart": premium_part, "recoPart": reco_part, "tax": tax}),
                },
            )
            WebhookFanoutConsumer.request(u, partner_id, WebhookTypes.DISPUTE_UPDATED, {"disputeId": dispute_id, "status": d_req.decision})

        self.db.in_tx(DbRole.LEDGER_WRITER, partner_id, work)
        return self.get_dispute(partner_id, dispute_id)

    # ---- payments and ledger ----------------------------------------------------------------------------------------

    def record_payment(self, statement_id: uuid.UUID, amount: Decimal, payment_ref: str, actor: str) -> dict[str, Any]:
        """Admin plane: records a payment (PAYMENT posting, idempotent per payment_ref); settles the statement when fully paid."""
        if amount <= 0:
            raise HsError.invalid("amount must be positive")
        partner_id = self.db.in_tx(DbRole.OPS, None, lambda u: u.scalar(
            "select partner_id from billing.statement where statement_id = @statementId", {"statementId": statement_id}))
        if partner_id is None:
            raise HsError.not_found(f"Statement {statement_id}")

        def work(u: Uow) -> None:
            st = u.single(f"select {self.STATEMENT_COLUMNS} from billing.statement where statement_id = @statementId", {"statementId": statement_id})
            if not self.ledger.post(u, partner_id, st["currency"].strip(), Postings.payment(payment_ref, statement_id, amount), actor):
                return
            u.execute(
                "update billing.statement set paid_amount = paid_amount + @amount, "
                "status = case when paid_amount + @amount >= total_due and status <> 'DISPUTED' then 'SETTLED' else status end where statement_id = @statementId",
                {"amount": amount, "statementId": statement_id},
            )

        self.db.in_tx(DbRole.LEDGER_WRITER, partner_id, work)
        return self.get(partner_id, statement_id)

    def trial_balance(self, partner_id: uuid.UUID | None = None) -> list[TrialBalanceRow]:
        """Trial balance (hs_ops) per account and currency, optionally for one partner; see `balanced`."""
        rows = self.db.in_tx(DbRole.OPS, None, lambda u: u.query(
            """
            select a.code as account, a.currency, sum(l.debit) as debit, sum(l.credit) as credit, sum(l.debit) - sum(l.credit) as balance
            from billing.journal_line l join billing.account a on a.account_id = l.account_id
            where (@partnerId::uuid is null or l.partner_id = @partnerId)
            group by a.code, a.currency order by a.currency, a.code
            """,
            {"partnerId": partner_id},
        ))
        return [TrialBalanceRow(r["account"], r["currency"], r["debit"], r["credit"], r["balance"]) for r in rows]

    # ---- mapping ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _disputed_lines(text: str | None) -> list[DisputedLine]:
        items = json.loads(text) if text else None
        return [DisputedLine.from_api(x, "disputed_lines") for x in items or []]

    @staticmethod
    def to_api(r: Mapping[str, Any]) -> dict[str, Any]:
        """billing.statement row -> Statement."""
        c = r["currency"].strip()
        period_start: date = r["period_start"]
        return _compact({
            "statementId": str(r["statement_id"]), "period": f"{period_start.year:04d}-{period_start.month:02d}", "currency": c, "status": r["status"],
            "totals": {
                "premiumFee": ApiMapper.money(r["premium_fee"], c), "recommendationFee": ApiMapper.money(r["reco_fee"], c),
                "recommendationChargeCount": r["reco_charge_count"], "grossFee": ApiMapper.money(r["gross_fee"], c),
                "reversals": ApiMapper.money(r["reversals"], c), "credits": ApiMapper.money(r["credits"], c), "netFee": ApiMapper.money(r["net_fee"], c),
                "tax": ApiMapper.money(r["tax_amount"], c), "totalDue": ApiMapper.money(r["total_due"], c),
            },
            "lineCount": r["line_count"], "invoiceNumber": r["invoice_number"], "issuedAt": iso(r["issued_at"]), "dueAt": iso(r["due_at"]),
        })

    @staticmethod
    def dispute_to_api(r: Mapping[str, Any], currency: str) -> dict[str, Any]:
        """billing.dispute row -> Dispute (recommendationChargeIds only when the dispute names charges)."""
        charges = list(r["disputed_charge_ids"] or [])
        return _compact({
            "disputeId": str(r["dispute_id"]), "statementId": str(r["statement_id"]), "status": r["status"], "reason": r["reason"],
            "lines": [x.to_api() for x in BillingService._disputed_lines(r["disputed_lines"])],
            "recommendationChargeIds": [str(c) for c in charges] if charges else None, "comment": r["comment"], "resolution": r["resolution"],
            "creditNoteAmount": None if r["credit_note_amount"] is None else ApiMapper.money(r["credit_note_amount"], currency),
            "openedAt": iso(r["opened_at"]), "resolvedAt": iso(r["resolved_at"]),
        })
