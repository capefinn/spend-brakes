"""A small JSON-over-HTTP helper that keeps the API key off the command line and out of messages.

The key is read from an environment variable and sent in a header. It is never an argument, never in the URL,
and it is removed from error text. It is also never sent to a second address: redirects are NOT followed (a 3xx
answer becomes HttpStatusError), and only https is allowed (http only for the local machine). Use this helper,
or use your own client: guarded_call only needs a function that raises on HTTP errors.
"""
from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from .errors import ConfigError


class HttpStatusError(Exception):
    """The server answered with a status of 300 or more. `.status` holds the number."""

    def __init__(self, status: int, body: str = ""):
        super().__init__(f"HTTP {status}: {body}" if body else f"HTTP {status}")
        self.status = status
        self.body = body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the library would send the Authorization header to the new address too."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _opener() -> urllib.request.OpenerDirector:
    """Only plain http(s) handlers: no file:, ftp: or data: handler exists, even if a URL asks for one."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
                    urllib.request.UnknownHandler(), urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor(), _NoRedirect()):
        opener.add_handler(handler)
    return opener


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str) -> str:
    """Allow https. Allow http only for the local machine. Refuse everything else, before the key is read."""
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 for c in url):
        raise ConfigError("The URL is empty or contains spaces or control characters.")
    try:
        parts = urlsplit(url)
        scheme, host = parts.scheme.lower(), parts.hostname
    except ValueError:
        raise ConfigError("The URL cannot be read.") from None
    if scheme not in ("https", "http"):
        raise ConfigError(f"Only https URLs are allowed (http only for the local machine). Refused scheme: {scheme or 'none'!r}.")
    if not host:
        raise ConfigError("The URL has no host name.")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("The URL must not contain a user name or password. The key goes in a header, not in the URL.")
    if scheme == "http" and not _is_loopback(host):
        raise ConfigError("Plain http is allowed only for the local machine (localhost, 127.0.0.1, ::1). "
                          "A key sent over http can be read on the way. Use an https URL.")
    return url


def read_key(key_env: str, env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    key = (env.get(key_env) or "").strip()
    if not key:
        raise ConfigError(f"The environment variable {key_env} is not set. Put the API key there (never in a command).")
    return key


def post_json(url: str, payload: Any, *, key_env: str, header: str = "Authorization", scheme: str = "Bearer",
              timeout: float = 60.0, env: Optional[Mapping[str, str]] = None) -> Any:
    """POST a JSON body and return the decoded JSON answer. Raises HttpStatusError on 3xx, 4xx and 5xx."""
    check_url(url)
    key = read_key(key_env, env)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={header: f"{scheme} {key}".strip(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _opener().open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace").replace(key, "[removed]")[:300]
        if 300 <= err.code < 400:
            body = "redirect not followed: the key is never sent to a second address. Use the final address of the API."
        raise HttpStatusError(err.code, body) from None
