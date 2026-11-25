"""Primitives shared by every layer: money and currency rounding, net quantity, GTIN, clocks, ids and billing periods.

Conventions (whole code base):
    * Money is always `decimal.Decimal`, rounded half-even to the currency minor unit (HLD 9.2).
    * Datetimes are timezone-aware UTC (`utc_now()`); `iso()` renders them for JSON with a trailing "Z".
    * Ids are UUIDv7 (time ordered, not guessable).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Protocol

UTC = timezone.utc

# ---------------------------------------------------------------------------------------------------------------------
# Currency and money
# ---------------------------------------------------------------------------------------------------------------------

_ZERO_DECIMAL = {"JPY", "KRW", "VND"}
_THREE_DECIMAL = {"BHD", "KWD", "OMR", "JOD", "TND"}


def minor_units(currency: str) -> int:
    if currency in _ZERO_DECIMAL:
        return 0
    if currency in _THREE_DECIMAL:
        return 3
    return 2


def to_decimal(value: Decimal | int | float | str) -> Decimal:
    """Converts without binary-float artefacts (floats go through repr)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def round_money(amount: Decimal | int | float | str, currency: str) -> Decimal:
    exponent = Decimal(1).scaleb(-minor_units(currency))
    return to_decimal(amount).quantize(exponent, rounding=ROUND_HALF_EVEN)


def round_half_even(value: Decimal | int | float | str, places: int) -> Decimal:
    return to_decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def format_amount(amount: Decimal | int | float | str, currency: str) -> str:
    return f"{round_money(amount, currency):.{minor_units(currency)}f}"


def parse_amount(text: str) -> Decimal:
    value = try_parse_amount(text)
    if value is None:
        raise ValueError(f"Not a decimal amount: {text!r}")
    return value


def try_parse_amount(text: str | None) -> Decimal | None:
    """Accepts an optional leading sign and a decimal point only (no exponent, no thousands separators)."""
    if text is None:
        return None
    t = text.strip()
    body = t[1:] if t[:1] in ("+", "-") else t
    if not body or body.count(".") > 1 or not body.replace(".", "").isdigit() or body == ".":
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    @staticmethod
    def zero(currency: str) -> Money:
        return Money(Decimal(0), currency)

    def rounded(self) -> Money:
        return Money(round_money(self.amount, self.currency), self.currency)

    @property
    def amount_text(self) -> str:
        return format_amount(self.amount, self.currency)

    def to_api(self) -> dict[str, str]:
        """The OpenAPI Money object: {"amount": "89.00", "currency": "INR"}."""
        return {"amount": self.amount_text, "currency": self.currency}

    def __str__(self) -> str:
        return f"{self.amount_text} {self.currency}"


# ---------------------------------------------------------------------------------------------------------------------
# Net quantity
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NetQuantity:
    """Net quantity of a pack, normalized to g, ml or pcs."""

    value: Decimal
    unit: str

    @staticmethod
    def normalize(value: Decimal | int | float | str | None, unit: str | None) -> NetQuantity | None:
        if value is None or unit is None or not unit.strip():
            return None
        v = to_decimal(value)
        if v <= 0:
            return None
        u = unit.strip().lower()
        if u == "kg":
            return NetQuantity(v * 1000, "g")
        if u == "g":
            return NetQuantity(v, "g")
        if u == "l":
            return NetQuantity(v * 1000, "ml")
        if u == "ml":
            return NetQuantity(v, "ml")
        if u == "pcs":
            return NetQuantity(v, "pcs")
        if u == "oz":
            return NetQuantity(round_half_even(v * Decimal("28.3495"), 1), "g")
        if u in ("fl oz", "floz"):
            return NetQuantity(round_half_even(v * Decimal("29.5735"), 1), "ml")
        return None

    def is_comparable_to(self, other: NetQuantity) -> bool:
        """Sizes are comparable only in the same mass or volume unit; HealthSwap never guesses densities."""
        return self.unit == other.unit and self.unit != "pcs"


# ---------------------------------------------------------------------------------------------------------------------
# GTIN (doc 02 section 3)
# ---------------------------------------------------------------------------------------------------------------------


def _check_digit(body: str) -> int:
    total, weight = 0, 3
    for ch in reversed(body):
        total += (ord(ch) - 48) * weight
        weight = 1 if weight == 3 else 3
    return (10 - total % 10) % 10


def gtin_has_valid_check_digit(digits: str) -> bool:
    if not digits or not (digits.isascii() and digits.isdigit()):
        return False
    return _check_digit(digits[:-1]) == ord(digits[-1]) - 48


def gtin_with_check_digit(without_check: str) -> str:
    return without_check + str(_check_digit(without_check))


def gtin_try_normalize(raw: str | None) -> str | None:
    """GTIN-8/12/13/14 to 14 digits with a valid mod-10 check digit, else None."""
    if raw is None or not raw.strip():
        return None
    digits = raw.strip()
    if len(digits) not in (8, 12, 13, 14) or not (digits.isascii() and digits.isdigit()):
        return None
    padded = digits.rjust(14, "0")
    return padded if gtin_has_valid_check_digit(padded) else None


def gtin_is_restricted_circulation(gtin14: str) -> bool:
    """GTIN-13 prefixes 02x and 2xx are retailer-specific; never use them as global keys (map by partner SKU)."""
    return len(gtin14) == 14 and gtin14[0] == "0" and (gtin14[1] == "2" or (gtin14[1] == "0" and gtin14[2] == "2"))


# ---------------------------------------------------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Treats naive datetimes as UTC and converts aware ones to UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso(value: datetime | date | None) -> str | None:
    """ISO 8601 for JSON: UTC datetimes end with "Z" (microsecond precision); dates are YYYY-MM-DD."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return value.isoformat()


def parse_iso(text: str) -> datetime:
    """Parses RFC 3339 timestamps (including a trailing Z) into aware UTC datetimes."""
    return as_utc(datetime.fromisoformat(text.strip().replace("z", "Z").replace("Z", "+00:00")))


class Clock(Protocol):
    def utc_now(self) -> datetime: ...


class SystemClock:
    def utc_now(self) -> datetime:
        return utc_now()


class OffsetClock:
    """Clock with an adjustable offset (development only; the manual test guide uses it to skip return windows)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._offset = timedelta(0)

    @property
    def offset(self) -> timedelta:
        with self._lock:
            return self._offset

    @offset.setter
    def offset(self, value: timedelta) -> None:
        with self._lock:
            self._offset = value

    def utc_now(self) -> datetime:
        return utc_now() + self.offset


class FixedClock:
    """Test clock."""

    def __init__(self, now: datetime) -> None:
        self.now = as_utc(now)

    def utc_now(self) -> datetime:
        return self.now


# ---------------------------------------------------------------------------------------------------------------------
# Ids and billing periods
# ---------------------------------------------------------------------------------------------------------------------

_uuid7_lock = threading.Lock()
_uuid7_last_ms = 0
_uuid7_counter = 0


def new_id() -> uuid.UUID:
    """UUIDv7 (RFC 9562): 48-bit Unix milliseconds, version 7, 12-bit monotonic counter, variant, 62 random bits."""
    global _uuid7_last_ms, _uuid7_counter
    with _uuid7_lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _uuid7_last_ms:
            _uuid7_counter = (_uuid7_counter + 1) & 0xFFF
            if _uuid7_counter == 0:
                _uuid7_last_ms += 1
            ms = _uuid7_last_ms
        else:
            _uuid7_last_ms = ms
            _uuid7_counter = int.from_bytes(os.urandom(2), "big") & 0x7FF
        counter = _uuid7_counter
    rand = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | counter << 64 | 0b10 << 62 | rand
    return uuid.UUID(int=value)


def period_try_parse(text: str | None) -> tuple[datetime, datetime] | None:
    """Parses a billing month (YYYY-MM) into [start, end) UTC."""
    if text is None or len(text) != 7 or text[4] != "-" or not (text[:4].isdigit() and text[5:].isdigit()):
        return None
    year, month = int(text[:4]), int(text[5:])
    if not 1 <= month <= 12 or year < 1:
        return None
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
    return start, end


def period_of(value: datetime) -> str:
    return as_utc(value).strftime("%Y-%m")
