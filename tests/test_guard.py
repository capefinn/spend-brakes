import socket
import urllib.error

import pytest

from spend_brakes import (Caps, ConfigError, HardCeilingExceeded, JobLimitReached, MonthlyCapReached, NotRetried,
                          ProjectCapReached, RetriesExhausted)
from spend_brakes.httpjson import HttpStatusError
from helpers import FakeHttpError, Script


# ---- recording ------------------------------------------------------------------------------------------------

def test_the_estimate_is_on_record_before_the_call_runs(call, ledger):
    seen = {}

    def fn():
        seen["rows"] = ledger.rows()
        return "done"

    assert call(fn, estimate_usd=0.25) == "done"
    assert [r["event"] for r in seen["rows"]] == ["reserve"] and seen["rows"][0]["usd"] == 0.25
    assert [r["event"] for r in ledger.rows()] == ["reserve", "settle"]


def test_the_real_cost_replaces_the_estimate(call, ledger):
    call(Script({"cost": 0.03}), estimate_usd=0.25, cost_from_result=lambda r: r["cost"])
    assert ledger.month_total() == 0.03


@pytest.mark.parametrize("reader", [lambda r: 1 / 0, lambda r: -4.0, lambda r: float("nan")])
def test_a_broken_cost_reader_falls_back_to_the_estimate_and_keeps_the_result(call, ledger, reader):
    assert call(Script("ok"), estimate_usd=0.25, cost_from_result=reader) == "ok"
    assert ledger.month_total() == 0.25


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01, "0.1", None])
def test_a_bad_estimate_stops_before_anything_is_written(call, ledger, bad):
    fn = Script("ok")
    with pytest.raises(ConfigError):
        call(fn, estimate_usd=bad)
    assert fn.calls == 0 and not ledger.path.exists()


# ---- the money caps --------------------------------------------------------------------------------------------

def test_monthly_cap_stops_the_call_that_would_pass_it(call, ledger):
    caps = Caps(monthly_usd=0.25)
    fn = Script("ok")
    call(fn, caps=caps)
    call(fn, caps=caps)
    with pytest.raises(MonthlyCapReached) as stop:
        call(fn, caps=caps)
    assert fn.calls == 2  # the third call never ran
    assert stop.value.brake == "monthly_cap"
    assert "SPEND_MONTHLY_CAP_USD" in str(stop.value) and "on purpose" in str(stop.value)
    assert ledger.month_total() == 0.20


def test_a_call_that_lands_exactly_on_the_cap_is_allowed_the_next_is_not(call):
    caps, fn = Caps(monthly_usd=0.30), Script("ok")
    for _ in range(3):
        call(fn, caps=caps)
    with pytest.raises(MonthlyCapReached):
        call(fn, caps=caps)


def test_the_monthly_cap_is_shared_by_every_project(call):
    caps = Caps(monthly_usd=0.30)
    call(Script("ok"), caps=caps, project="proj-a", estimate_usd=0.15)
    call(Script("ok"), caps=caps, project="proj-b", estimate_usd=0.15)
    with pytest.raises(MonthlyCapReached):
        call(Script("ok"), caps=caps, project="proj-c", estimate_usd=0.01)


def test_project_cap_stops_one_project_and_leaves_the_others(call):
    caps = Caps(monthly_usd=100.0, project_usd=0.20)
    call(Script("ok"), caps=caps, project="blog-images")
    call(Script("ok"), caps=caps, project="blog-images")
    with pytest.raises(ProjectCapReached) as stop:
        call(Script("ok"), caps=caps, project="blog-images")
    assert stop.value.brake == "project_cap" and "SPEND_PROJECT_CAP_USD_BLOG_IMAGES" in str(stop.value)
    call(Script("ok"), caps=Caps(monthly_usd=100.0), project="other")  # another project is not blocked


# ---- the job limit ---------------------------------------------------------------------------------------------

def test_job_limit_stops_a_loop_and_another_job_is_unaffected(call):
    caps, fn = Caps(monthly_usd=100.0, max_calls_per_job=3), Script("ok")
    for _ in range(3):
        call(fn, caps=caps, job="loop")
    with pytest.raises(JobLimitReached) as stop:
        call(fn, caps=caps, job="loop")
    assert fn.calls == 3 and stop.value.brake == "job_limit" and "SPEND_MAX_CALLS_PER_JOB" in str(stop.value)
    call(fn, caps=caps, job="another")


def test_refused_calls_count_toward_the_job_limit(call):
    caps = Caps(monthly_usd=100.0, max_calls_per_job=3)
    fn = Script(FakeHttpError(429))
    for _ in range(3):
        with pytest.raises(NotRetried):
            call(fn, caps=caps)
    with pytest.raises(JobLimitReached):
        call(fn, caps=caps)
    assert fn.calls == 3


def test_hard_ceiling_also_covers_retries(call):
    fn = Script("ok")
    with pytest.raises(HardCeilingExceeded) as stop:
        call(fn, max_retries=4)
    assert stop.value.brake == "hard_ceiling" and fn.calls == 0
    with pytest.raises(ConfigError):
        call(fn, max_retries=-1)


# ---- retries: never on 401 402 403 429 -------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_refusals_are_not_retried(call, ledger, sleeps, status):
    fn = Script(HttpStatusError(status))
    with pytest.raises(NotRetried) as stop:
        call(fn, max_retries=3)
    assert fn.calls == 1 and sleeps == []
    assert stop.value.brake == "no_retry" and stop.value.status == status
    assert isinstance(stop.value.__cause__, HttpStatusError)  # the original error is kept
    assert str(status) in str(stop.value) and "on purpose" in str(stop.value)
    assert ledger.month_total() == 0  # refused before any work: nothing billed
    assert [r["outcome"] for r in ledger.rows() if r["event"] == "settle"] == ["rejected"]


def test_a_refusal_found_on_the_error_response_is_not_retried(call):
    fn = Script(FakeHttpError(402))
    with pytest.raises(NotRetried):
        call(fn, max_retries=3)
    assert fn.calls == 1


@pytest.mark.parametrize("status", [401, 402, 403, 429, 503])
def test_a_returned_response_with_a_refusal_status_is_treated_like_a_raised_error(call, status):
    class Response:  # many clients return the response and do not raise
        status_code = status

    fn = Script(Response())
    with pytest.raises((NotRetried, RetriesExhausted)):
        call(fn, max_retries=2)
    assert fn.calls == (1 if status != 503 else 3)


def test_a_returned_ordinary_response_passes_through(call):
    class Response:
        status_code = 200

    response = Response()
    assert call(Script(response)) is response


# ---- retries: a few, only on 5xx and timeouts ------------------------------------------------------------------

@pytest.mark.parametrize("failure", [
    HttpStatusError(500), HttpStatusError(503), FakeHttpError(504),
    TimeoutError("slow"), socket.timeout("slow"), ConnectionResetError("dropped"),
    urllib.error.URLError("no route"),
])
def test_server_errors_and_timeouts_are_retried_a_bounded_number_of_times(call, ledger, sleeps, failure):
    fn = Script(failure)
    with pytest.raises(RetriesExhausted) as stop:
        call(fn, max_retries=2)
    assert fn.calls == 3 and stop.value.attempts == 3 and stop.value.brake == "retries_exhausted"
    assert len(sleeps) == 2
    assert ledger.month_total() == 0.30  # a failed attempt may still be billed, so each stays on record
    assert "max_retries" in str(stop.value)


def test_one_retry_is_the_default(call):
    fn = Script(HttpStatusError(502))
    with pytest.raises(RetriesExhausted):
        call(fn)
    assert fn.calls == 2


def test_no_retry_at_all_when_max_retries_is_zero(call):
    fn = Script(HttpStatusError(502))
    with pytest.raises(RetriesExhausted):
        call(fn, max_retries=0)
    assert fn.calls == 1


def test_a_retry_that_works_returns_the_result(call):
    fn = Script(HttpStatusError(503), "second time")
    assert call(fn, max_retries=2) == "second time"
    assert fn.calls == 2


def test_every_retry_checks_the_brakes_again(call):
    fn = Script(HttpStatusError(503))
    with pytest.raises(MonthlyCapReached):  # money for one attempt only: the retry is stopped by the cap
        call(fn, caps=Caps(monthly_usd=0.15), max_retries=3)
    assert fn.calls == 1


def test_the_job_limit_also_stops_retries(call):
    fn = Script(HttpStatusError(503))
    with pytest.raises(JobLimitReached):
        call(fn, caps=Caps(monthly_usd=100.0, max_calls_per_job=2), max_retries=3)
    assert fn.calls == 2


# ---- other errors ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 404, 422])
def test_other_client_errors_are_not_repeated_and_are_raised_as_they_are(call, status):
    fn = Script(HttpStatusError(status))
    with pytest.raises(HttpStatusError):
        call(fn, max_retries=3)
    assert fn.calls == 1


def test_an_unknown_error_is_raised_unchanged_and_its_estimate_stays(call, ledger):
    with pytest.raises(ValueError):
        call(Script(ValueError("bug")))
    assert ledger.month_total() == 0.10


def test_ctrl_c_is_not_swallowed(call, ledger):
    with pytest.raises(KeyboardInterrupt):
        call(Script(KeyboardInterrupt()))
    assert ledger.month_total() == 0.10  # the estimate was on record before the call
