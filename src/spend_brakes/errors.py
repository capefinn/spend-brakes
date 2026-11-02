"""Errors. Every error that comes from a brake is a BrakeError and says which brake it was."""
from __future__ import annotations


class SpendBrakesError(Exception):
    """Base class for everything this package raises on purpose."""


class ConfigError(SpendBrakesError):
    """A setting is missing or cannot be read (for example a cap that is not a number)."""


class BrakeError(SpendBrakesError):
    """A brake stopped the call. `.brake` names it. The message names the setting; the owner decides to change it."""

    def __init__(self, brake: str, message: str):
        super().__init__(message)
        self.brake = brake


class KillSwitchOn(BrakeError):
    def __init__(self, message: str):
        super().__init__("kill_switch", message)


class MonthlyCapReached(BrakeError):
    def __init__(self, message: str):
        super().__init__("monthly_cap", message)


class ProjectCapReached(BrakeError):
    def __init__(self, message: str):
        super().__init__("project_cap", message)


class JobLimitReached(BrakeError):
    def __init__(self, message: str):
        super().__init__("job_limit", message)


class HardCeilingExceeded(BrakeError):
    """A setting asks for more than the ceiling that is written into the code."""

    def __init__(self, message: str):
        super().__init__("hard_ceiling", message)


class NotRetried(BrakeError):
    """The provider refused the call (401, 402, 403 or 429). The call was not repeated."""

    def __init__(self, status: int, message: str):
        super().__init__("no_retry", message)
        self.status = status


class RetriesExhausted(BrakeError):
    """A server error or a timeout kept coming back, and the small retry budget is used up."""

    def __init__(self, attempts: int, message: str):
        super().__init__("retries_exhausted", message)
        self.attempts = attempts


class LedgerUnreadable(BrakeError):
    """The ledger has a line that cannot be read, so the real spend is not known. No call is allowed."""

    def __init__(self, message: str):
        super().__init__("ledger_unreadable", message)
