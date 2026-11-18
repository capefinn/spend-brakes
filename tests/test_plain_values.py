"""Only plain values are checked and kept. A subclass of int, float or str can change how it compares.

A made-up subclass that answers "no" to every comparison would pass a check like `n > 20` and then pass the limit
as well. These tests build such classes. Each one must be stopped, or turned into a plain value that is then enforced.
"""
import types

import pytest

from spend_brakes import (BrakeError, Caps, ConfigError, HardCeilingExceeded, JobLimitReached, Ledger, MonthlyCapReached,
                          ProjectCapReached, RetriesExhausted, Totals, check_call, guarded_call)
from spend_brakes.caps import check_caps, check_money
from helpers import Script


def _never(self, other):
    return False


class RiggedInt(int):
    """An int for which every comparison is False: `n > 20` is False, and so is `calls >= n`."""
    __lt__ = __le__ = __gt__ = __ge__ = _never


class RiggedFloat(float):
    __lt__ = __le__ = __gt__ = __ge__ = _never


class NegativeButLooksFine(float):
    """A negative number that says it is zero or more, and never less than anything."""
    __ge__ = lambda self, other: True  # noqa: E731
    __lt__ = __le__ = __gt__ = _never


class RiggedStr(str):
    """A text that is never equal to anything, so a count by job name stays at 0."""
    __eq__ = _never
    __ne__ = lambda self, other: True  # noqa: E731
    __hash__ = str.__hash__


class PlainSubclassOfStr(str):
    pass


def attempts_until_stopped(call, fn, limit=60, **overrides):
    """Call until a brake stops it. Returns how many calls got through, and the error."""
    through = 0
    for _ in range(limit):
        try:
            call(fn, **overrides)
            through += 1
        except BrakeError as stop:
            return through, stop
    return through, None


# ---- the values that are kept are plain ------------------------------------------------------------------------

def test_caps_keep_plain_numbers_not_the_objects_they_were_given():
    caps = Caps(monthly_usd=RiggedFloat(5.0), project_usd=RiggedFloat(2.0), max_calls_per_job=RiggedInt(4))
    assert type(caps.monthly_usd) is float and type(caps.project_usd) is float and type(caps.max_calls_per_job) is int
    assert (caps.monthly_usd, caps.project_usd, caps.max_calls_per_job) == (5.0, 2.0, 4)
    assert type(Caps(monthly_usd=5).monthly_usd) is float  # a whole number becomes a float


def test_check_caps_returns_plain_numbers_for_any_object():
    caps = check_caps(types.SimpleNamespace(monthly_usd=RiggedFloat(7.0), project_usd=None, max_calls_per_job=RiggedInt(3)))
    assert type(caps.monthly_usd) is float and caps.project_usd is None and type(caps.max_calls_per_job) is int


def test_check_money_gives_a_plain_float():
    assert type(check_money(RiggedFloat(0.25), "x")) is float and type(check_money(3, "x")) is float


# ---- the attacks ------------------------------------------------------------------------------------------------

def test_a_job_limit_that_never_compares_is_still_enforced(call):
    fn = Script("ok")
    through, stop = attempts_until_stopped(call, fn, caps=Caps(monthly_usd=100.0, max_calls_per_job=RiggedInt(4)))
    assert through == 4 and isinstance(stop, JobLimitReached)


def test_a_job_limit_of_1000_that_never_compares_is_stopped_by_the_ceiling(call):
    fn = Script("ok")
    with pytest.raises(HardCeilingExceeded):
        Caps(monthly_usd=100.0, max_calls_per_job=RiggedInt(1000))
    forged = object.__new__(Caps)  # a Caps whose own checks never ran
    for name, value in dict(monthly_usd=100.0, project_usd=None, max_calls_per_job=RiggedInt(1000)).items():
        object.__setattr__(forged, name, value)
    with pytest.raises(HardCeilingExceeded):
        call(fn, caps=forged)
    assert fn.calls == 0


def test_a_monthly_cap_that_never_compares_is_still_enforced(call):
    fn = Script("ok")
    through, stop = attempts_until_stopped(call, fn, caps=Caps(monthly_usd=RiggedFloat(0.25), max_calls_per_job=20), estimate_usd=0.10)
    assert through == 2 and isinstance(stop, MonthlyCapReached)


def test_a_project_cap_that_never_compares_is_still_enforced(call):
    fn = Script("ok")
    through, stop = attempts_until_stopped(
        call, fn, caps=Caps(monthly_usd=100.0, project_usd=RiggedFloat(0.25), max_calls_per_job=20), estimate_usd=0.10)
    assert through == 2 and isinstance(stop, ProjectCapReached)


def test_a_negative_cap_that_says_it_is_fine_is_refused(call, ledger):
    fn = Script("ok")
    with pytest.raises(ConfigError):
        Caps(monthly_usd=NegativeButLooksFine(-5.0))
    with pytest.raises(ConfigError):
        Caps(monthly_usd=100.0, project_usd=NegativeButLooksFine(-5.0))
    with pytest.raises(ConfigError):
        call(fn, estimate_usd=NegativeButLooksFine(-5.0))
    assert fn.calls == 0 and not ledger.path.exists()


def test_a_retry_count_that_never_compares_is_stopped_by_the_ceiling(call, ledger):
    fn = Script(TimeoutError())
    with pytest.raises(HardCeilingExceeded):
        call(fn, max_retries=RiggedInt(1000))
    assert fn.calls == 0 and not ledger.path.exists()


def test_a_retry_count_that_never_compares_is_still_enforced(call, sleeps):
    fn = Script(TimeoutError())
    with pytest.raises(RetriesExhausted):
        call(fn, max_retries=RiggedInt(2))
    assert fn.calls == 3 and len(sleeps) == 2  # one try and two retries, then it gives up


def test_the_retry_delay_is_a_plain_float(call, sleeps):
    call(Script(TimeoutError(), "ok"), retry_delay_s=RiggedFloat(0.5))
    assert sleeps == [0.5] and type(sleeps[0]) is float


def test_totals_from_a_ledger_of_your_own_are_made_plain_before_they_are_compared(tmp_path):
    class MyLedger:
        stop_file = None

        def reserve(self, *, project, provider, job, usd, check):
            check(Totals(month_usd=RiggedFloat(999.0), project_usd=RiggedFloat(0.0), job_calls=RiggedInt(99), unreadable_lines=0))
            return "id"

        def settle(self, *args, **kwargs):
            pass

    fn = Script("ok")
    with pytest.raises(JobLimitReached):
        guarded_call(fn, estimate_usd=0.10, project="p", provider="x", job="j", caps=Caps(monthly_usd=100.0, max_calls_per_job=5),
                     ledger=MyLedger(), env={})
    assert fn.calls == 0


# ---- names must be exactly of type str ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["project", "provider", "job"])
@pytest.mark.parametrize("bad", [RiggedStr("batch-1"), PlainSubclassOfStr("batch-1"), 5, None, b"batch-1"])
def test_a_name_that_is_not_plain_text_is_refused_and_nothing_is_written(call, ledger, field, bad):
    fn = Script("ok")
    with pytest.raises(ConfigError, match=field):
        call(fn, **{field: bad})
    with pytest.raises(ConfigError, match=field):
        check_call(**{**dict(estimate_usd=0.1, project="p", provider="x", job="j", caps=Caps(), ledger=ledger, env={}), field: bad})
    assert fn.calls == 0 and not ledger.path.exists()


def test_a_job_name_that_is_never_equal_to_itself_cannot_keep_the_job_count_at_zero(call, ledger):
    fn = Script("ok")
    with pytest.raises(ConfigError):
        call(fn, job=RiggedStr("batch-1"), caps=Caps(monthly_usd=100.0, max_calls_per_job=5))
    through, stop = attempts_until_stopped(call, fn, job="batch-1", caps=Caps(monthly_usd=100.0, max_calls_per_job=5))
    assert through == 5 and isinstance(stop, JobLimitReached) and ledger.job_calls("proj-a", "batch-1") == 5


def test_the_ledger_also_refuses_a_name_that_is_not_plain_text(ledger):
    with pytest.raises(ConfigError):
        ledger.reserve(project=RiggedStr("p"), provider="x", job="j", usd=0.1, check=lambda totals: None)
    with pytest.raises(ConfigError):
        ledger.preview(project="p", provider="x", job=PlainSubclassOfStr("j"), usd=0.1, check=lambda totals: None)
    assert not ledger.path.exists()
