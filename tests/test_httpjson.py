"""End to end against a local fake server on the loopback address: no paid call, no outside network."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from spend_brakes import Caps, ConfigError, NotRetried, RetriesExhausted, guarded_call
from spend_brakes.httpjson import HttpStatusError, post_json

KEY = "test-key-abc123"
ENV = {"TEST_API_KEY": KEY}


@pytest.fixture
def server():
    state = {"plan": [], "hits": 0, "seen": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["hits"] += 1
            state["seen"].append({"path": self.path, "auth": self.headers.get("Authorization")})
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            plan = state["plan"]
            status, body = plan[min(state["hits"], len(plan)) - 1] if plan else (200, {"ok": True})
            if body == "slow":
                time.sleep(0.6)
                body = {}
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_address[1]}/call"
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
