"""Typed workflow errors with distinguishable reason codes."""
from __future__ import annotations

from enum import Enum


class Reason(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
    PRECONDITION_MISSING = "PRECONDITION_MISSING"
    DUPLICATE_SUBMISSION = "DUPLICATE_SUBMISSION"
    SUPPLIER_NOT_ADMITTED = "SUPPLIER_NOT_ADMITTED"
    SUPPLIER_SUSPENDED = "SUPPLIER_SUSPENDED"
    QUOTE_WINDOW_CLOSED = "QUOTE_WINDOW_CLOSED"
    QUOTE_DUPLICATE = "QUOTE_DUPLICATE"
    APPROVAL_FORBIDDEN = "APPROVAL_FORBIDDEN"
    APPROVAL_NOT_PENDING = "APPROVAL_NOT_PENDING"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


class WorkflowError(Exception):
    """Business-rule violation. `reason` is machine-distinguishable."""

    def __init__(self, reason: Reason, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason.value)
