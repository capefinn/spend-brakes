"""Kill switch: one master switch and one switch per provider, read from the environment on every call.

  SPEND_PANIC=1                 stops every paid call
  SPEND_<PROVIDER>_ENABLED=0    stops one provider, e.g. SPEND_IMAGE_API_ENABLED=0
  SPEND_STOP_FILE=/some/path    if that file exists, every paid call stops (reaches processes that are already running)
  a file named STOP             the default stop file: it sits in the same folder as the ledger file. SPEND_STOP_FILE,
                                when it is set, replaces it. A ledger that is not a file has no default stop file.

A value that cannot be read (for example "ofF!") counts as STOP. A brake must fail in the safe direction.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from .caps import env_word
from .errors import KillSwitchOn

ENV_PANIC = "SPEND_PANIC"
ENV_STOP_FILE = "SPEND_STOP_FILE"
STOP_FILE_NAME = "STOP"  # the default stop file: this name, in the same folder as the ledger file
_ON = {"1", "true", "on", "yes"}
_OFF = {"0", "false", "off", "no"}


def provider_env_name(provider: str) -> str:
    return f"SPEND_{env_word(provider, 'provider')}_ENABLED"


def _word(env: Mapping[str, str], name: str) -> Optional[str]:
    raw = (env.get(name) or "").strip().lower()
    return raw or None


def check_kill_switch(provider: str, env: Optional[Mapping[str, str]] = None,
                      default_stop_file: Optional[os.PathLike] = None) -> None:
    """Raise KillSwitchOn when any switch says stop. Returns None when the call may go on.

    `default_stop_file` is used only when SPEND_STOP_FILE is not set. guarded_call passes the file named STOP
    next to the ledger file."""
    env = os.environ if env is None else env

    panic = _word(env, ENV_PANIC)
    if panic in _ON:
        raise KillSwitchOn(
            f"Stopped by the kill switch: {ENV_PANIC} is on, so no paid call is allowed. "
            f"Only the owner turns it off (setting: {ENV_PANIC})."
        )
    if panic is not None and panic not in _OFF:
        raise KillSwitchOn(
            f"Stopped by the kill switch: {ENV_PANIC}={panic!r} cannot be read, so it counts as ON (the safe choice). "
            f"Ask the owner to fix the value (setting: {ENV_PANIC}; 1 means stop, 0 means go)."
        )

    stop_file = (env.get(ENV_STOP_FILE) or "").strip()
    if stop_file:
        if Path(stop_file).exists():
            raise KillSwitchOn(
                f"Stopped by the kill switch: the stop file named in {ENV_STOP_FILE} exists ({stop_file}). "
                "Only the owner removes that file."
            )
    elif default_stop_file is not None and Path(default_stop_file).exists():
        raise KillSwitchOn(
            f"Stopped by the kill switch: the default stop file exists ({default_stop_file}). It sits in the same "
            f"folder as the ledger file. Only the owner removes that file (to use another file, set {ENV_STOP_FILE})."
        )

    name = provider_env_name(provider)
    value = _word(env, name)
    if value is None or value in _ON:
        return
    if value in _OFF:
        raise KillSwitchOn(
            f"Stopped by the kill switch: {name} is {value}, so provider {provider!r} is switched off. "
            f"Only the owner turns it on again (setting: {name})."
        )
    raise KillSwitchOn(
        f"Stopped by the kill switch: {name}={value!r} cannot be read, so provider {provider!r} counts as OFF "
        f"(the safe choice). Ask the owner to fix the value (setting: {name}; 1 means go, 0 means stop)."
    )
