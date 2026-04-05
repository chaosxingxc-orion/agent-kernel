"""Error types used by compensation execution and retry flow."""

from __future__ import annotations


class CompensationTimeoutError(TimeoutError):
    """Raised when a compensation handler call exceeds timeout."""


class TransientCompensationError(RuntimeError):
    """Raised by handlers for retryable transient compensation failures."""


class CompensationExhaustedError(RuntimeError):
    """Raised when all compensation attempts are exhausted."""
