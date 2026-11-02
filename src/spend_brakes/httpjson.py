"""A small JSON-over-HTTP helper that keeps the API key off the command line and out of messages.

The key is read from an environment variable and sent in a header. It is never an argument, never in the URL,
and it is removed from error text. Use it, or use your own client: guarded_call only needs a function that
raises on HTTP errors.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

from .errors import ConfigError


class HttpStatusError(Exception):
    """The server answered with a status of 400 or more. `.status` holds the number."""

    def __init__(self, status: int, body: str = ""):
        super().__init__(f"HTTP {status}: {body}" if body else f"HTTP {status}")
        self.status = status
        self.body = body


def read_key(key_env: str, env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    key = (env.get(key_env) or "").strip()
    if not key:
        raise ConfigError(f"The environment variable {key_env} is not set. Put the API key there (never in a command).")
    return key


def post_json(url: str, payload: Any, *, key_env: str, header: str = "Authorization", scheme: str = "Bearer",
              timeout: float = 60.0, env: Optional[Mapping[str, str]] = None) -> Any:
    """POST a JSON body and return the decoded JSON answer. Raises HttpStatusError on 4xx and 5xx."""
    key = read_key(key_env, env)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={header: f"{scheme} {key}".strip(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace").replace(key, "[removed]")[:300]
        raise HttpStatusError(err.code, body) from None
