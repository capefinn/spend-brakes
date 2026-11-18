"""End to end against a local fake server on the loopback address: no paid call, no outside network."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import urllib.error
import urllib.request

import pytest

import spend_brakes.httpjson as httpjson_module
from spend_brakes import Caps, ConfigError, NotRetried, RetriesExhausted, guarded_call
from spend_brakes.httpjson import HttpStatusError, check_url, post_json

KEY = "test-key-abc123"
ENV = {"TEST_API_KEY": KEY}


def start_server():
    """A fake API on the loopback address. Plan entries: (status, body) or (status, body, extra_headers)."""
    state = {"plan": [], "hits": 0, "seen": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["hits"] += 1
            state["seen"].append({"path": self.path, "auth": self.headers.get("Authorization")})
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            plan = state["plan"]
            status, body, *more = plan[min(state["hits"], len(plan)) - 1] if plan else (200, {"ok": True})
            if body == "slow":
                time.sleep(0.6)
                body = {}
            raw = json.dumps(body).encode()
            self.send_response(status)
            for name, value in (more[0] if more else {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/call"
    return state, httpd


@pytest.fixture
def server():
    state, httpd = start_server()
    yield state
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def other_server():
    """A second address: where a redirect would send the key."""
    state, httpd = start_server()
    yield state
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def run(ledger, sleeps):
    def _run(url, **overrides):
        settings = dict(estimate_usd=0.10, project="p", provider="api", job="j", ledger=ledger, env={},
                        caps=Caps(monthly_usd=100.0), sleep=sleeps.append)
        settings.update(overrides)
        return guarded_call(lambda: post_json(url, {"prompt": "x"}, key_env="TEST_API_KEY", timeout=0.3, env=ENV), **settings)

    return _run


def test_a_good_answer_comes_back_and_the_key_travels_in_a_header_only(server, run):
    server["plan"] = [(200, {"result": 7})]
    assert run(server["url"]) == {"result": 7}
    assert server["seen"][0]["auth"] == f"Bearer {KEY}"
    assert KEY not in server["seen"][0]["path"]


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_402_and_friends_reach_the_server_exactly_once(server, run, status):
    server["plan"] = [(status, {"error": "no"})]
    with pytest.raises(NotRetried):
        run(server["url"], max_retries=3)
    assert server["hits"] == 1


def test_503_is_tried_again_only_as_often_as_allowed(server, run):
    server["plan"] = [(503, {"error": "busy"})]
    with pytest.raises(RetriesExhausted):
        run(server["url"], max_retries=2)
    assert server["hits"] == 3


def test_503_then_200_works(server, run):
    server["plan"] = [(503, {}), (200, {"ok": 1})]
    assert run(server["url"], max_retries=1) == {"ok": 1}
    assert server["hits"] == 2


def test_a_timeout_is_tried_again_once_then_stops(server, run):
    server["plan"] = [(200, "slow")]
    with pytest.raises(RetriesExhausted):
        run(server["url"], max_retries=1)
    assert server["hits"] == 2


def test_a_key_that_the_server_repeats_back_never_reaches_errors_or_the_ledger(server, run, ledger):
    server["plan"] = [(401, {"error": f"bad key {KEY}"})]
    with pytest.raises(NotRetried) as stop:
        run(server["url"])
    shown = str(stop.value) + str(stop.value.__cause__) + repr(stop.value.__cause__.body)
    assert KEY not in shown and "[removed]" in shown
    assert KEY not in ledger.path.read_text()


def test_a_missing_key_stops_before_any_request(server, ledger):
    with pytest.raises(ConfigError):
        post_json(server["url"], {}, key_env="TEST_API_KEY", env={})
    assert server["hits"] == 0


def test_http_status_error_carries_the_status():
    err = HttpStatusError(402, "body")
    assert err.status == 402 and "402" in str(err)


# ---- a redirect must never carry the key to another address ----------------------------------------------------

@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_not_followed_and_the_key_never_reaches_the_second_address(server, other_server, status):
    server["plan"] = [(status, {}, {"Location": other_server["url"]})]
    with pytest.raises(HttpStatusError) as stop:
        post_json(server["url"], {"prompt": "x"}, key_env="TEST_API_KEY", env=ENV)
    assert stop.value.status == status and "redirect not followed" in str(stop.value)
    assert server["hits"] == 1
    assert other_server["hits"] == 0 and other_server["seen"] == []  # nothing, and so no Authorization header, arrived


def test_a_redirect_through_the_guard_is_one_call_and_an_error_not_a_retry(server, other_server, run):
    server["plan"] = [(302, {}, {"Location": other_server["url"]})]
    with pytest.raises(HttpStatusError):
        run(server["url"], max_retries=3)
    assert server["hits"] == 1 and other_server["hits"] == 0


def test_a_redirect_without_a_location_is_also_an_error(server):
    server["plan"] = [(302, {})]
    with pytest.raises(HttpStatusError) as stop:
        post_json(server["url"], {}, key_env="TEST_API_KEY", env=ENV)
    assert stop.value.status == 302


# ---- only https, or http for the local machine -----------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/hosts", "FILE:///etc/hosts", "ftp://api.invalid/x", "data:text/plain,hello", "javascript:alert(1)",
    "gopher://api.invalid/x", "ws://api.invalid/x", "//api.invalid/x", "api.invalid/x", "", "   ",
    "http://api.invalid/x",  # plain http to another machine: the key could be read on the way
    "HTTP://api.invalid/x",
    "http://localhost.api.invalid/x",  # looks like localhost, is not
    "http://127.0.0.1.api.invalid/x",
    "http://[::1]@api.invalid/x",
    "https://user:secret@api.invalid/x",  # user name and password in the URL
    "https://@api.invalid/x",
    "https:///no-host",
    "https://api.invalid/a b", "https://api.invalid/\nHost: other",
    "http://[::1/x",
])
def test_unsafe_addresses_are_refused_before_any_request_is_made(url, monkeypatch):
    def no_request(*args, **kwargs):
        raise AssertionError("a request was about to be made")

    monkeypatch.setattr(httpjson_module, "_opener", no_request)
    with pytest.raises(ConfigError) as stop:
        post_json(url, {}, key_env="TEST_API_KEY", env=ENV)  # the key IS set: the URL itself is what is refused
    assert "TEST_API_KEY" not in str(stop.value)


def test_file_urls_are_refused_and_the_file_is_never_opened(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")
    with pytest.raises(ConfigError):
        post_json(secret.as_uri(), {}, key_env="TEST_API_KEY", env=ENV)
    # and even if a file URL got through the check, this client has no handler that can open files
    monkeypatch.setattr(httpjson_module, "check_url", lambda url: url)
    with pytest.raises(urllib.error.URLError):
        post_json(secret.as_uri(), {}, key_env="TEST_API_KEY", env=ENV)


@pytest.mark.parametrize("url", [
    "https://api.invalid/v1/x", "HTTPS://api.invalid/v1/x", "https://api.invalid:8443/x?y=1",
    "http://localhost:8080/x", "http://127.0.0.1:1/x", "http://127.0.0.2/x", "http://[::1]:8080/x",
])
def test_https_and_local_http_addresses_are_accepted(url):
    assert check_url(url) == url


# ---- a proxy must never see the key in clear text ---------------------------------------------------------------

@pytest.fixture
def proxy_env(monkeypatch):
    """No proxy settings from the real environment, so every test starts the same."""
    import os
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    return monkeypatch


def test_a_local_http_call_goes_straight_to_the_server_even_when_http_proxy_is_set(server, other_server, proxy_env):
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        proxy_env.setenv(name, other_server["url"].rsplit("/", 1)[0])  # the fake "proxy" is the second server
    server["plan"] = [(200, {"result": 1})]
    assert post_json(server["url"], {}, key_env="TEST_API_KEY", env=ENV) == {"result": 1}
    assert server["hits"] == 1 and server["seen"][0]["auth"] == f"Bearer {KEY}"
    assert other_server["hits"] == 0 and other_server["seen"] == []  # the proxy never saw the call or the key


def test_only_the_https_proxy_of_the_environment_is_used(proxy_env):
    proxy_env.setenv("http_proxy", "http://proxy-one.invalid:3128")
    proxy_env.setenv("all_proxy", "http://proxy-two.invalid:3128")
    assert httpjson_module._proxies() == {}  # nothing for http, nothing from all_proxy
    proxy_env.setenv("https_proxy", "http://proxy-three.invalid:3128")
    assert httpjson_module._proxies() == {"https": "http://proxy-three.invalid:3128"}
    handlers = [h for h in httpjson_module._opener().handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert len(handlers) == 1 and handlers[0].proxies == {"https": "http://proxy-three.invalid:3128"}
    assert not hasattr(handlers[0], "http_open")  # a ProxyHandler for http would have this method


def test_http_status_error_is_exported_from_the_package_root():
    import spend_brakes
    from spend_brakes import HttpStatusError as from_root
    assert from_root is HttpStatusError
    assert "HttpStatusError" in spend_brakes.__all__
    assert from_root(402, "no money").status == 402
