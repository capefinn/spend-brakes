"""Kill switch: one master switch and one switch per provider, read from the environment on every call.

  SPEND_PANIC=1                 stops every paid call
  SPEND_<PROVIDER>_ENABLED=0    stops one provider, e.g. SPEND_IMAGE_API_ENABLED=0
  SPEND_STOP_FILE=/some/path    if that file exists, every paid call stops (reaches processes that are already running)

A value that cannot be read (for example "ofF!") counts as STOP. A brake must fail on the safe side.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from .caps import env_word
from .errors import KillSwitchOn

ENV_PANIC = "SPEND_PANIC"
ENV_STOP_FILE = "SPEND_STOP_FILE"
_ON = {"1", "true", "on", "yes"}
_OFF = {"0", "false", "off", "no"}


def provider_env_name(provider: str) -> str:
    return f"SPEND_{env_word(provider)}_ENABLED"


def _word(env: Mapping[str, str], name: str) -> Optional[str]:
    raw = (env.get(name) or "").strip().lower()
    return raw or None


def check_kill_switch(provider: str, env: Optional[Mapping[str, str]] = None) -> None:
    """Raise KillSwitchOn when any switch says stop. Returns None when the call may go on."""
    env = os.environ if env is None else env

    panic = _word(env, ENV_PANIC)
    if panic in _ON:
        raise KillSwitchOn(
            f"Stopped by the kill switch: {ENV_PANIC} is on, so no paid call is allowed. "
            f"To allow calls again on purpose, unset {ENV_PANIC} or set it to 0."
        )
    if panic is not None and panic not in _OFF:
        raise KillSwitchOn(
            f"Stopped by the kill switch: {ENV_PANIC}={panic!r} cannot be read, so it counts as ON (the safe side). "
            f"Set it to 1 (stop) or 0 (go)."
        )

    stop_file = (env.get(ENV_STOP_FILE) or "").strip()
    if stop_file and Path(stop_file).exists():
        raise KillSwitchOn(
            f"Stopped by the kill switch: the stop file named in {ENV_STOP_FILE} exists ({stop_file}). "
            "To allow calls again on purpose, delete that file."
        )

    name = provider_env_name(provider)
    value = _word(env, name)
    if value is None or value in _ON:
        return
    if value in _OFF:
        raise KillSwitchOn(
            f"Stopped by the kill switch: {name} is {value}, so provider {provider!r} is switched off. "
            f"To allow calls again on purpose, set {name}=1 or unset it."
        )
    raise KillSwitchOn(
        f"Stopped by the kill switch: {name}={value!r} cannot be read, so provider {provider!r} counts as OFF "
        f"(the safe side). Set it to 1 (go) or 0 (stop)."
    )
