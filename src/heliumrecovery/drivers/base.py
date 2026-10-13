"""Shared pieces for all instrument drivers."""

from datetime import datetime, timezone


class DriverError(Exception):
    """Base class for every error an instrument driver raises."""


class InstrumentTimeout(DriverError):
    """The instrument did not answer in time."""


class ProtocolError(DriverError):
    """The instrument answered, but the reply could not be understood."""


def now() -> datetime:
    """Timestamp for `last_update` fields (timezone-aware UTC)."""
    return datetime.now(timezone.utc)
