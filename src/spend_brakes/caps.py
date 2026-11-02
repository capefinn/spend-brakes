"""Caps: money per month, money per project, calls per job. Plus the ceilings no setting can raise."""
from __future__ import annotations

import math
import os
import unicodedata
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

MAX_NAME_LENGTH = 200  # keeps every ledger row short


def project_key(name: str) -> str:
    """The key of a project or provider name. It is the name inside the setting names, and it is what totals count by.

    Steps: Unicode NFKC normalisation, then case-folding (and NFKC once more, because case-folding can split a letter
    into a letter and a mark, and Python and TypeScript must give the same key), then every run of characters that are
    not Unicode letters or digits becomes one "_", the "_" at the start and the end is removed, and the result is
    written in capital letters. So "blog-images", "Blog_Images" and "BLOG IMAGES" all give "BLOG_IMAGES", and
    "Kuvat äänet" gives "KUVAT_ÄÄNET" whether the ä is one character or a letter plus a mark. The TypeScript function
    projectKey gives the same result.

    It does not raise for a str. It gives "" for a name with no letter and no digit (only punctuation, spaces or
    emoji); guarded_call refuses such a name with ConfigError."""
    words, current = [], []
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold())
    for char in folded:
        if char.isalnum():
            current.append(char)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return "_".join(words).upper()


def env_word(name: str, label: str = "name") -> str:
    """'blog images' -> 'BLOG_IMAGES' (used inside environment variable names). ConfigError when no key can be made."""
    if type(name) is not str:
        raise ConfigError(f"The {label} must be plain text (type str, not another type and not a subclass of str).")
    word = project_key(name)
    if not word:
        raise ConfigError(
            f"The {label} {name!r} has no letter and no digit, so it cannot be used. "
            "Use a name with at least one letter or digit, for example 'blog-images'."
        )
    return word


def check_names(project: str, provider: str, job: str) -> None:
    """Every name must be plain text (type str, not a subclass of it) of 1 to 200 characters. A project and a provider
    name must also have a key (see project_key). This runs on every call, whether or not a cap is set."""
    for label, value in (("project", project), ("provider", provider), ("job", job)):
        if type(value) is not str:
            raise ConfigError(f"{label} must be plain text (type str, not another type and not a subclass of str).")
        if not value or len(value) > MAX_NAME_LENGTH:
            raise ConfigError(f"{label} must be a name of 1 to {MAX_NAME_LENGTH} characters.")
    env_word(project, "project")
    env_word(provider, "provider")


def check_money(value: float, label: str) -> float:
    """Return the value as a plain float, or raise ConfigError. The value is turned into a plain float FIRST and the
    checks run on that float only, so a subclass of int or float cannot change a comparison."""
    plain = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            plain = float(value)  # a whole number too big to be a float raises OverflowError
        except Exception:
            plain = None
    if plain is None or not math.isfinite(plain) or plain < 0:
        text = repr(value) if len(repr(value)) <= 40 else repr(value)[:37] + "..."
        raise ConfigError(f"{label} must be a number that is zero or more (got {text}).")
    return plain


def check_estimate(value: float) -> float:
    return check_money(value, "estimate_usd")


def check_job_limit(value: object) -> int:
    """A plain int from 1 to the hard ceiling. Above the ceiling is HardCeilingExceeded."""
    plain = None
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            plain = int(value)
        except Exception:
            plain = None
    if plain is None or plain < 1:
        raise ConfigError(f"max_calls_per_job must be a whole number of 1 or more (got {value!r}).")
    if plain > HARD_MAX_CALLS_PER_JOB:
        raise HardCeilingExceeded(
            f"max_calls_per_job={plain} is above the hard ceiling of {HARD_MAX_CALLS_PER_JOB}. "
            "The ceiling is written into the code on purpose, so no setting can raise it. "
            "Only the owner can change it, in the code (HARD_MAX_CALLS_PER_JOB in spend_brakes/caps.py), with a review."
        )
    return plain


def check_retries(value: object) -> int:
    """A plain int from 0 to the hard ceiling for retries. Above the ceiling is HardCeilingExceeded."""
    plain = None
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            plain = int(value)
        except Exception:
            plain = None
    if plain is None or plain < 0:
        raise ConfigError(f"max_retries must be a whole number from 0 to {HARD_MAX_RETRIES} (got {value!r}).")
    if plain > HARD_MAX_RETRIES:
        raise HardCeilingExceeded(
            f"max_retries={plain} is above the hard ceiling of {HARD_MAX_RETRIES}. The ceiling is written into the "
            "code on purpose, so no setting can raise it. Only the owner can change it, in the code "
            "(HARD_MAX_RETRIES in spend_brakes/caps.py), with a review."
        )
    return plain


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
        # Check, then keep the plain value that was checked. A subclass of int or float is never kept: a subclass can
        # change how it compares, so only int(...) and float(...) results are stored.
        object.__setattr__(self, "monthly_usd", check_money(self.monthly_usd, "monthly_usd"))
        if self.project_usd is not None:
            object.__setattr__(self, "project_usd", check_money(self.project_usd, "project_usd"))
        object.__setattr__(self, "max_calls_per_job", check_job_limit(self.max_calls_per_job))

    @classmethod
    def from_env(cls, project: str, env: Optional[Mapping[str, str]] = None) -> "Caps":
        """Read the caps from the environment, so every script on the machine sees the same numbers."""
        env = os.environ if env is None else env
        monthly = read_number(env, ENV_MONTHLY)
        per_project = read_number(env, ENV_PROJECT_PREFIX + env_word(project, "project"))
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
