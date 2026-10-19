"""Exceptions contain fixed descriptions, never vendor bodies or credential URLs."""


class BlinkError(Exception):
    """Recoverable protocol or service failure."""


class AuthError(BlinkError):
    """A new interactive sign-in is required."""


class ApiError(BlinkError):
    def __init__(self, status: int, retry_after: float = 0) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"Blink request failed (HTTP {status})")


class VerificationRequired(BlinkError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"Blink {kind} verification code required")
