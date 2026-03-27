"""Custom exceptions for Agent Harness Service."""


class AHSValidationError(ValueError):
    """Raised for request validation failures that should map to HTTP 400.

    Separates validation errors (missing user_id, sender mismatch) from
    lookup failures (session/agent not found) which map to HTTP 404.
    Both are ValueError subclasses for backward compatibility.
    """
