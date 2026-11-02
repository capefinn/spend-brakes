"""guarded_call: run one paid call only after every brake has been checked, and record what it cost."""
from __future__ import annotations

import os
import socket
import time
import urllib.error
import warnings
from typing import Any, Callable, Mapping, Optional, TypeVar

from .caps import (ENV_JOB_CALLS, ENV_MONTHLY, ENV_PROJECT_PREFIX, HARD_MAX_RETRIES, Caps, check_estimate,
                   check_money, env_word)
from .errors import (ConfigError, HardCeilingExceeded, JobLimitReached, MonthlyCapReached, NotRetried,
                     ProjectCapReached, RetriesExhausted)
from .killswitch import check_kill_switch
from .ledger import Ledger, Totals

T = TypeVar("T")

# The provider refused the call: wrong key, no money, not allowed, too many calls. Repeating it cannot help.
NO_RETRY_STATUS = {
    401: "the key was not accepted",
    402: "payment is required: no money or a blocked plan",
    403: "the call is not allowed",
    429: "rate limit or quota reached",
}
_TRANSIENT_ERRORS = (TimeoutError, socket.timeout, ConnectionError, urllib.error.URLError)


def status_of(exc: BaseException) -> Optional[int]:
    """Find an HTTP status on an error: .status, .status_code, .code, or the same on .response."""
    for holder in (exc, getattr(exc, "response", None)):
        for name in ("status", "status_code", "code"):
            value = getattr(holder, name, None)
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                return value
    return None


def _is_transient(exc: BaseException) -> bool:
    status = status_of(exc)
    if status is not None:
        return 500 <= status <= 599
    return isinstance(exc, _TRANSIENT_ERRORS)


class _ReturnedStatus(Exception):
    """A function returned a response object with a refusal status instead of raising."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


def _usd(x: float) -> str:
    return f"${x:,.2f}" if x == 0 or x >= 0.01 else f"${x:.4f}"


def _prepare(estimate_usd, project, provider, job, caps, ledger, max_retries, retry_delay_s, env):
    """Check the arguments and build the brake check. guarded_call and check_call both use it, so both run the same brakes."""
    env = os.environ if env is None else env
    estimate = check_estimate(estimate_usd)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ConfigError(f"max_retries must be a whole number from 0 to {HARD_MAX_RETRIES} (got {max_retries!r}).")
    check_money(retry_delay_s, "retry_delay_s")
    if max_retries > HARD_MAX_RETRIES:
        raise HardCeilingExceeded(
            f"max_retries={max_retries} is above the hard ceiling of {HARD_MAX_RETRIES}. The ceiling is written into the "
            "code on purpose, so no setting can raise it. Only the owner can change it, in the code "
            "(HARD_MAX_RETRIES in spend_brakes/caps.py), with a review."
        )
    caps = caps if caps is not None else Caps.from_env(project, env)
    ledger = ledger if ledger is not None else Ledger.from_env(env)

    def check(totals: Totals) -> None:
        if totals.job_calls >= caps.max_calls_per_job:
            raise JobLimitReached(
                f"Stopped by the job limit: job {job!r} already has {totals.job_calls} recorded calls (limit "
                f"{caps.max_calls_per_job}, refused calls count too). Look at why the job needs more calls. "
                f"Ask the owner if the limit should change (setting: {ENV_JOB_CALLS}); the hard ceiling in the code "
                "cannot be raised by a setting."
            )
        if caps.project_usd is not None and round(totals.project_usd + estimate, 6) > caps.project_usd:
            raise ProjectCapReached(
                f"Stopped by the project cap: project {project!r} spent {_usd(totals.project_usd)} this month, and this "
                f"call is about {_usd(estimate)}, which would exceed the cap of {_usd(caps.project_usd)}. "
                f"Ask the owner to raise it (setting: {ENV_PROJECT_PREFIX}{env_word(project)})."
            )
        if round(totals.month_usd + estimate, 6) > caps.monthly_usd:
            raise MonthlyCapReached(
                f"Stopped by the monthly cap: all projects spent {_usd(totals.month_usd)} this month, and this call is "
                f"about {_usd(estimate)}, which would exceed the cap of {_usd(caps.monthly_usd)}. "
                f"Ask the owner to raise it (setting: {ENV_MONTHLY})."
            )

    return env, estimate, ledger, check


def _default_stop_file(ledger: Any) -> Optional[Any]:
    """The file named STOP next to the ledger file. A ledger that has no such file (not a file ledger) gives None."""
    return getattr(ledger, "stop_file", None)


def guarded_call(
    fn: Callable[[], T],
    *,
    estimate_usd: float,
    project: str,
    provider: str,
    job: str,
    caps: Optional[Caps] = None,
    ledger: Optional[Ledger] = None,
    max_retries: int = 1,
    retry_delay_s: float = 2.0,
    cost_from_result: Optional[Callable[[Any], float]] = None,
    env: Optional[Mapping[str, str]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn()` (no arguments; use a lambda) after the brakes agree.

    `job` is required and must be NEW for every run of your script (for example "batch-" + run_id()). The job limit
    counts every call ever recorded under the same project + job, so a name that is used again keeps its old count.

    Before every attempt, retries included: kill switch, job limit, project cap, monthly cap. The estimate is
    written to the ledger before `fn` runs. After it: the real cost (from `cost_from_result`) or the estimate.
    401, 402, 403 and 429 are never repeated. 5xx and timeouts are repeated at most `max_retries` times.
    `fn` must raise on HTTP errors, or return an object whose .status_code / .status is the HTTP status.
    """
    env, estimate, ledger, check = _prepare(estimate_usd, project, provider, job, caps, ledger, max_retries,
                                            retry_delay_s, env)

    names = dict(project=project, provider=provider, job=job)
    attempt = 0
    while True:
        attempt += 1
        check_kill_switch(provider, env, _default_stop_file(ledger))
        call_id = ledger.reserve(usd=estimate, check=check, **names)  # the estimate is on record before the call
        try:
            result = fn()
            refused = getattr(result, "status_code", getattr(result, "status", None))
            if isinstance(refused, int) and not isinstance(refused, bool) and (refused in NO_RETRY_STATUS or 500 <= refused <= 599):
                raise _ReturnedStatus(refused)
        except Exception as exc:
            status = status_of(exc)
            if status in NO_RETRY_STATUS:
                ledger.settle(call_id, usd=0.0, outcome="rejected", **names)  # refused before any work: nothing billed
                raise NotRetried(
                    status,
                    f"Not retried: the provider answered {status} ({NO_RETRY_STATUS[status]}). A repeat cannot fix this "
                    "and can make it worse. The owner must fix the cause (key, payment, permission or call rate) before the next run.",
                ) from exc
            ledger.settle(call_id, usd=estimate, outcome="error", **names)  # may have been billed: keep the estimate
            if not _is_transient(exc):
                raise
            if attempt > max_retries:
                raise RetriesExhausted(
                    attempt,
                    f"Gave up after {attempt} attempt(s): the provider kept answering with a server error or a timeout. "
                    "Every attempt is counted in the ledger as a possible cost. Check the provider's status before you "
                    "run it again. More tries are the owner's choice: raise the max_retries argument of guarded_call "
                    f"(it is an argument, not an environment setting; the ceiling is {HARD_MAX_RETRIES}).",
                ) from exc
            sleep(retry_delay_s * attempt)
            continue
        cost = estimate
        if cost_from_result is not None:
            try:
                cost = check_estimate(cost_from_result(result))
            except Exception:  # never lose a paid result because the cost reader failed
                cost = estimate
        try:
            ledger.settle(call_id, usd=cost, outcome="ok", **names)
        except OSError as err:  # the estimate is already on record; do not lose a paid result over a failed note
            warnings.warn(f"spend_brakes: could not write the final cost to the ledger ({err}); the estimate stays.")
        return result


def check_call(
    *,
    estimate_usd: float,
    project: str,
    provider: str,
    job: str,
    caps: Optional[Caps] = None,
    ledger: Optional[Ledger] = None,
    max_retries: int = 1,
    retry_delay_s: float = 2.0,
    cost_from_result: Optional[Callable[[Any], float]] = None,
    env: Optional[Mapping[str, str]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Ask: would `guarded_call` allow this call now? Read-only. Use it for a --dry-run.

    It takes the same keyword arguments as `guarded_call`, without the function, and runs the same brakes in the same
    order: the arguments, the kill switch, the job limit, the project cap and the monthly cap. When a brake would stop
    the call, it raises the same error with the same message. When all brakes agree, it returns None.

    It writes nothing: no row in the ledger (so it never counts for the job limit), no lock file, no folder. It
    checks the first attempt only. It does not call the provider. `cost_from_result` and `sleep` are accepted so
    that the same arguments work for both functions; they have no effect here.
    """
    env, estimate, ledger, check = _prepare(estimate_usd, project, provider, job, caps, ledger, max_retries,
                                            retry_delay_s, env)
    check_kill_switch(provider, env, _default_stop_file(ledger))
    ledger.preview(project=project, provider=provider, job=job, usd=estimate, check=check)
