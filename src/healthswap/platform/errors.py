"""Errors with stable RFC 9457 problem codes (HLD Appendix A). The API maps HsError to application/problem+json."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FieldError:
    field: str
    message: str
    code: str | None = None

    def to_api(self) -> dict[str, str]:
        d = {"field": self.field, "message": self.message}
        if self.code is not None:
            d["code"] = self.code
        return d


class HsError(Exception):
    """C# HsException. `status` is the HTTP status, `code` the stable HS-* code."""

    def __init__(self, status: int, code: str, title: str, detail: str | None = None, errors: list[FieldError] | None = None) -> None:
        super().__init__(detail or title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.errors = errors

    @classmethod
    def not_found(cls, what: str) -> HsError:
        return cls(404, "HS-NOT-FOUND", "Resource not found", f"{what} not found or not visible to this partner")

    @classmethod
    def invalid(cls, detail: str, errors: list[FieldError] | None = None) -> HsError:
        return cls(400, "HS-REQ-INVALID", "Request validation failed", detail, errors)

    @classmethod
    def too_large(cls, detail: str) -> HsError:
        return cls(400, "HS-REQ-TOO-LARGE", "Request too large", detail)

    @classmethod
    def conflict(cls, code: str, detail: str) -> HsError:
        return cls(409, code, "Conflict", detail)

    @classmethod
    def unprocessable(cls, code: str, detail: str) -> HsError:
        return cls(422, code, "Unprocessable request", detail)

    @classmethod
    def forbidden(cls, detail: str) -> HsError:
        return cls(403, "HS-AUTH-SCOPE", "Forbidden", detail)

    def __repr__(self) -> str:
        return f"HsError({self.status}, {self.code!r}, {self.detail or self.title!r})"
