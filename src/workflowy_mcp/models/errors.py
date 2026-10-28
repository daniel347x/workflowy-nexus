"""Error response models and exception classes."""

import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Standard error response for all operations."""

    error: str = Field(..., description="Error message")
    code: str = Field(..., description="Error code")
    details: dict[str, Any] | None = Field(None, description="Additional error context")
    success: bool = Field(False, description="Always false for errors")

    class Config:
        """Pydantic model configuration."""

        json_schema_extra = {
            "example": {
                "error": "Node not found",
                "code": "NODE_NOT_FOUND",
                "details": {"node_id": "invalid-123"},
                "success": False,
            }
        }


class APIError(Exception):
    """Base API error class (alias for compatibility)."""

    pass


class WorkFlowyError(Exception):
    """Base exception for WorkFlowy MCP errors."""

    def __init__(
        self, message: str, code: str = "WORKFLOWY_ERROR", details: dict[str, Any] | None = None
    ):
        """Initialize the error."""
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def to_response(self) -> ErrorResponse:
        """Convert exception to error response."""
        return ErrorResponse(
            error=self.message, code=self.code, details=self.details, success=False
        )


class AuthenticationError(WorkFlowyError):
    """Raised when authentication fails."""

    def __init__(
        self, message: str = "Authentication failed", details: dict[str, Any] | None = None
    ):
        """Initialize authentication error."""
        super().__init__(message, "AUTH_ERROR", details)


class NodeNotFoundError(WorkFlowyError):
    """Raised when a requested node doesn't exist."""

    def __init__(self, node_id: str, message: str | None = None):
        """Initialize node not found error."""
        msg = message or f"Node with ID '{node_id}' not found"
        super().__init__(msg, "NODE_NOT_FOUND", {"node_id": node_id})


class ValidationError(WorkFlowyError):
    """Raised when input validation fails."""

    def __init__(self, message: str, field: str | None = None):
        """Initialize validation error."""
        details = {"field": field} if field else {}
        super().__init__(message, "VALIDATION_ERROR", details)


# @beacon[
#   id=carto-safety@parse-retry-after,
#   slice_labels=ra-carto-jobs,nexus-core-v1,
#   kind=ast,
#   comment=Single strict Retry-After grammar authority for delta-seconds and HTTP-date forms; malformed or missing input returns the caller-supplied conservative fallback with diagnostics.,
# ]
def parse_retry_after_header(
    raw_header: str | None,
    now_epoch: float | None = None,
    *,
    fallback_seconds: float = 60.0,
) -> dict[str, Any]:
    """Parse Retry-After without throwing, preserving zero as a valid delay."""

    raw = "" if raw_header is None else str(raw_header).strip()
    now = time.time() if now_epoch is None else float(now_epoch)
    fallback = max(0.0, float(fallback_seconds))

    if raw:
        try:
            delay = float(raw)
        except (TypeError, ValueError):
            delay = math.nan
        else:
            if math.isfinite(delay) and delay >= 0:
                return {
                    "delay_seconds": delay,
                    "used_fallback": False,
                    "diagnostic": "delta_seconds",
                }

        try:
            parsed = parsedate_to_datetime(raw)
            if parsed is None:
                raise ValueError("HTTP-date parser returned no value")
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = max(0.0, parsed.timestamp() - now)
            if math.isfinite(delay):
                return {
                    "delay_seconds": delay,
                    "used_fallback": False,
                    "diagnostic": "http_date",
                }
        except (TypeError, ValueError, OverflowError):
            pass

    reason = "missing_or_blank" if not raw else "malformed_negative_or_non_finite"
    return {
        "delay_seconds": fallback,
        "used_fallback": True,
        "diagnostic": reason,
    }


class RateLimitError(WorkFlowyError):
    """Raised when API rate limit is exceeded."""

    def __init__(self, retry_after: float | None = None):
        """Initialize rate limit error and expose the parsed delay to retry loops."""
        message = "Rate limit exceeded"
        details: dict[str, Any] = {}
        self.retry_after = retry_after
        if retry_after is not None:
            message += f". Retry after {retry_after:g} seconds"
            details["retry_after"] = retry_after
        super().__init__(message, "RATE_LIMIT_ERROR", details)


class AccountWriteBlockedError(WorkFlowyError):
    """Raised when the account-scoped WFB breaker refuses a write locally."""

    def __init__(
        self,
        *,
        retry_after: float,
        blocked_until: float,
        reason: str,
        message: str | None = None,
    ):
        retry_after = max(0.0, float(retry_after))
        blocked_until = float(blocked_until)
        self.retry_after = retry_after
        self.blocked_until = blocked_until
        self.reason = str(reason)
        super().__init__(
            message or f"Account writes are blocked: {self.reason}",
            "ACCOUNT_WRITE_BLOCKED",
            {
                "retry_after": retry_after,
                "blocked_until": blocked_until,
                "reason": self.reason,
            },
        )


class WriteGovernorUnavailableError(WorkFlowyError):
    """Raised when the write-governance path or lock substrate is broken."""

    def __init__(self, governance_path: str, reason: str):
        self.governance_path = str(governance_path)
        self.reason = str(reason)
        super().__init__(
            f"Write governor unavailable at '{self.governance_path}': {self.reason}",
            "WRITE_GOVERNOR_UNAVAILABLE",
            {"governance_path": self.governance_path, "reason": self.reason},
        )


class WriteGovernorDeferredError(WorkFlowyError):
    """Raised when a healthy governor's next slot exceeds the caller wait bound."""

    def __init__(
        self,
        *,
        retry_after: float,
        deferred_until: float,
        reason: str,
    ):
        retry_after = max(0.0, float(retry_after))
        deferred_until = float(deferred_until)
        self.retry_after = retry_after
        self.deferred_until = deferred_until
        self.reason = str(reason)
        super().__init__(
            f"Workflowy write deferred: {self.reason}",
            "WRITE_GOVERNOR_DEFERRED",
            {
                "retry_after": retry_after,
                "deferred_until": deferred_until,
                "reason": self.reason,
            },
        )


class WriteSlotBusyTimeoutError(WorkFlowyError):
    """Raised when a healthy write-slot mutex remains busy past its wait bound."""

    def __init__(self, governance_path: str, timeout_seconds: float):
        self.governance_path = str(governance_path)
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.reason = "write-slot mutex remained busy past the bounded wait"
        super().__init__(
            f"Write-slot mutex remained busy past {self.timeout_seconds:g}s: "
            f"'{self.governance_path}'.",
            "WRITE_SLOT_BUSY_TIMEOUT",
            {
                "governance_path": self.governance_path,
                "timeout_seconds": self.timeout_seconds,
                "reason": self.reason,
            },
        )


class APIResponseError(WorkFlowyError):
    """Raised for non-retryable HTTP 4xx responses from the Workflowy API."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        response_body: str | None = None,
    ):
        """Preserve status/body diagnostics without classifying 4xx as transport failure."""
        details: dict[str, Any] = {"status_code": int(status_code)}
        if response_body:
            details["response_body"] = response_body
        super().__init__(message, "API_RESPONSE_ERROR", details)
        self.status_code = int(status_code)
        self.response_body = response_body


class OperationCancelledError(WorkFlowyError):
    """Raised when a detached CARTO operation observes cooperative cancellation."""

    def __init__(self, operation: str = "Workflowy API operation"):
        """Initialize a non-retryable cooperative-cancellation signal."""
        super().__init__(f"{operation} cancelled", "OPERATION_CANCELLED", {"operation": operation})


class NetworkError(WorkFlowyError):
    """Raised when network operations fail."""

    def __init__(
        self, message: str = "Network error occurred", details: dict[str, Any] | None = None
    ):
        """Initialize network error."""
        super().__init__(message, "NETWORK_ERROR", details)


class TimeoutError(WorkFlowyError):
    """Raised when operations timeout."""

    def __init__(self, operation: str | None = None):
        """Initialize timeout error."""
        message = f"Operation '{operation}' timed out" if operation else "Request timed out"
        super().__init__(message, "TIMEOUT_ERROR", {"operation": operation} if operation else {})
