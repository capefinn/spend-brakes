"""spend_brakes: spend brakes for scripts and agents that call paid APIs. Standard library only."""
from .caps import HARD_MAX_CALLS_PER_JOB, HARD_MAX_RETRIES, Caps
from .errors import (BrakeError, ConfigError, HardCeilingExceeded, JobLimitReached, KillSwitchOn, MonthlyCapReached,
                     NotRetried, ProjectCapReached, RetriesExhausted, SpendBrakesError)
from .guard import guarded_call
from .killswitch import check_kill_switch
from .ledger import Ledger, Totals

__all__ = [
    "guarded_call", "Caps", "Ledger", "Totals", "check_kill_switch",
    "HARD_MAX_CALLS_PER_JOB", "HARD_MAX_RETRIES",
    "SpendBrakesError", "ConfigError", "BrakeError", "KillSwitchOn", "MonthlyCapReached", "ProjectCapReached",
    "JobLimitReached", "HardCeilingExceeded", "NotRetried", "RetriesExhausted",
]
__version__ = "0.1.0"
