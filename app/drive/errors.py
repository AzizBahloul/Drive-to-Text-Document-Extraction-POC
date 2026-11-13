"""Errors raised by the Drive layer, with a message safe to show to the user."""

from typing import Literal

ErrorKind = Literal["invalid_link", "not_found", "forbidden", "too_large", "upstream", "config"]


class DriveError(Exception):
    """A Drive operation failed in a way the user can understand and act on."""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

    @property
    def http_status(self) -> int:
        return {
            "invalid_link": 400,
            "not_found": 404,
            "forbidden": 403,
            "too_large": 413,
            "upstream": 502,
            "config": 500,
        }[self.kind]
