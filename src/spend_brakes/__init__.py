"""spend_brakes: spend brakes for scripts and agents that call paid APIs. Standard library only."""
from .caps import HARD_MAX_CALLS_PER_JOB, HARD_MAX_RETRIES, Caps, project_key
from .errors import (BrakeError, ConfigError, HardCeilingExceeded, JobLimitReached, KillSwitchOn, LedgerUnreadable,
                     MonthlyCapReached, NotRetried, ProjectCapReached, RetriesExhausted, SpendBrakesError)
from .guard import check_call, guarded_call
from .httpjson import HttpStatusError
from .killswitch import check_kill_switch
from .ledger import Ledger, Totals, run_id

__all__ = [
    "guarded_call", "check_call", "HttpStatusError", "Caps", "Ledger", "Totals", "check_kill_switch", "run_id",
    "HARD_MAX_CALLS_PER_JOB", "HARD_MAX_RETRIES", "project_key",
    "SpendBrakesError", "ConfigError", "BrakeError", "KillSwitchOn", "MonthlyCapReached", "ProjectCapReached",
    "JobLimitReached", "HardCeilingExceeded", "NotRetried", "RetriesExhausted", "LedgerUnreadable",
]
__version__ = "0.1.0"
