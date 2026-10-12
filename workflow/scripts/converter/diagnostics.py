"""Collect conversion warnings and errors.

Bad input must never surface as a traceback: it either becomes a warning in the
conversion log/report, or a `ConversionError` with a clear message.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

LOGGER = logging.getLogger("converter")


class ConversionError(Exception):
    """Raised when a legacy model cannot be converted at all."""


@dataclass(frozen=True)
class Message:
    """A single diagnostic message."""

    level: str  # "warning" | "info"
    code: str  # short machine-readable category, e.g. "unknown_param"
    message: str
    source: str | None = None  # legacy file / override the message relates to

    def __str__(self) -> str:
        where = f" [{self.source}]" if self.source else ""
        return f"{self.level.upper()} {self.code}{where}: {self.message}"


@dataclass
class Diagnostics:
    """Accumulates messages emitted while converting one model."""

    messages: list[Message] = field(default_factory=list)

    def warn(self, code: str, message: str, source: str | None = None) -> None:
        msg = Message("warning", code, message, source)
        if msg not in self.messages:
            self.messages.append(msg)
            LOGGER.warning(str(msg))

    def info(self, code: str, message: str, source: str | None = None) -> None:
        msg = Message("info", code, message, source)
        if msg not in self.messages:
            self.messages.append(msg)
            LOGGER.info(str(msg))

    @property
    def warnings(self) -> list[Message]:
        return [m for m in self.messages if m.level == "warning"]

    def by_code(self, code: str) -> list[Message]:
        return [m for m in self.messages if m.code == code]

    def to_list(self) -> list[dict]:
        return [asdict(m) for m in self.messages]
