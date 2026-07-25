from __future__ import annotations

from http import HTTPStatus


class ForgeTraceError(Exception):
    """A user-facing, structured application error."""

    def __init__(
        self,
        message: str,
        status: int = HTTPStatus.BAD_REQUEST,
        code: str = "invalid_request",
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.details = details or {}


RepositoryError = ForgeTraceError
