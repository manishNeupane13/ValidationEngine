class SamsaraError(Exception):
    """Base class for expected integration errors."""


class RetryableSamsaraError(SamsaraError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentSamsaraError(SamsaraError):
    """The request is not expected to succeed unchanged on retry."""


class ResponseSchemaError(SamsaraError):
    """The response did not match the contract expected by this adapter."""
