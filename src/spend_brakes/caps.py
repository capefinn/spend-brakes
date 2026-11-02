"""Caps: money per month, money per project, calls per job. Plus the ceilings no setting can raise."""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional

from .errors import ConfigError, HardCeilingExceeded

# These two numbers are part of the code on purpose. No argument and no environment variable can raise them.
# To change one, edit this file and have the change reviewed.
HARD_MAX_CALLS_PER_JOB = 20
HARD_MAX_RETRIES = 3

DEFAULT_MONTHLY_CAP_USD = 20.0  # an example number: set your own
DEFAULT_MAX_CALLS_PER_JOB = 6

ENV_MONTHLY = "SPEND_MONTHLY_CAP_USD"
ENV_PROJECT_PREFIX = "SPEND_PROJECT_CAP_USD_"
ENV_JOB_CALLS = "SPEND_MAX_CALLS_PER_JOB"


def _cleaned(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def env_word(name: str) -> str:
    """'blog images' -> 'BLOG_IMAGES' (used inside environment variable names)."""
    word = _cleaned(name)
    if not word:
        raise ConfigError(f"The name {name!r} has no letters or digits, so no setting name can be made from it.")
    return word


def project_key(name: str) -> str:
    """The key that totals use to count a project: the same cleaned-up name as in the setting name.

    'blog-images', 'blog images', 'Blog_Images' and 'BLOG-IMAGES' all give 'BLOG_IMAGES', so they share one project
    cap and one job count. A name with no letters or digits cannot give a setting name; it keeps its exact text as
    its key, so such names are still counted one by one. It never raises."""
    return _cleaned(name) or name


def check_money(value: float, label: str) -> float:
    try:
        usable = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except OverflowError:  # a whole number too big to be turned into a float
        usable = False
    if not usable:
        text = repr(value) if len(repr(value)) <= 40 else repr(value)[:37] + "..."
        raise ConfigError(f"{label} must be a number that is zero or more (got {text}).")
    return float(value)


def check_estimate(value: float) -> float:
    return check_money(value, "estimate_usd")


def read_number(env: Mapping[str, str], name: str) -> Optional[float]:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number. Set it to a plain number such as 20.") from None
    if not math.isfinite(value):  # nan, inf and a number too big to store (1e400) are not usable settings
        raise ConfigError(f"{name}={raw!r} is not a usable number. Set it to a plain number such as 20.")
    return value


@dataclass(frozen=True)
class Caps:
    monthly_usd: float = DEFAULT_MONTHLY_CAP_USD  # all projects together, shared through one ledger
    project_usd: Optional[float] = None  # this project only; None means no sub-cap
    max_calls_per_job: int = DEFAULT_MAX_CALLS_PER_JOB

    def __post_init__(self) -> None:
        check_money(self.monthly_usd, "monthly_usd")
        if self.project_usd is not None:
            check_money(self.project_usd, "project_usd")
        n = self.max_calls_per_job
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ConfigError(f"max_calls_per_job must be a whole number of 1 or more (got {n!r}).")
        if n > HARD_MAX_CALLS_PER_JOB:
            raise HardCeilingExceeded(
                f"max_calls_per_job={n} is above the hard ceiling of {HARD_MAX_CALLS_PER_JOB}. "
                "The ceiling is written into the code on purpose, so no setting can raise it. "
                "Only the owner can change it, in the code (HARD_MAX_CALLS_PER_JOB in spend_brakes/caps.py), with a review."
            )

    @classmethod
    def from_env(cls, project: str, env: Optional[Mapping[str, str]] = None) -> "Caps":
        """Read the caps from the environment, so every script on the machine sees the same numbers."""
        env = os.environ if env is None else env
        monthly = read_number(env, ENV_MONTHLY)
        per_project = read_number(env, ENV_PROJECT_PREFIX + env_word(project))
        calls = read_number(env, ENV_JOB_CALLS)
        if calls is not None and (not math.isfinite(calls) or calls != int(calls)):
            raise ConfigError(f"{ENV_JOB_CALLS}={calls} must be a whole number.")
        return cls(
            monthly_usd=DEFAULT_MONTHLY_CAP_USD if monthly is None else monthly,
            project_usd=per_project,
            max_calls_per_job=DEFAULT_MAX_CALLS_PER_JOB if calls is None else int(calls),
        )


def check_caps(value: object) -> Caps:
    """Check caps that come from anywhere (a Caps, a Caps made by a trick, any object with the three fields).

    Each field is read ONCE and checked by the same rules as `Caps(...)`: a money cap that is not a finite number of
    zero or more, or a job limit that is not a whole number from 1 to the hard ceiling, is refused. The result is a
    new, frozen `Caps` that holds plain values, so a changing property cannot give a different number later."""
    fields = []
    for name in ("monthly_usd", "project_usd", "max_calls_per_job"):
        try:
            fields.append(getattr(value, name))
        except AttributeError:
            raise ConfigError(f"caps must be a Caps object (or have the same three fields); it has no {name}.") from None
    return Caps(monthly_usd=fields[0], project_usd=fields[1], max_calls_per_job=fields[2])
