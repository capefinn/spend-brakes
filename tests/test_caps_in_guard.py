"""Caps and retries are checked INSIDE the guard, whatever object they come from.

`Caps(...)` checks its own numbers, but a function argument can be any object: a made-up object, a Caps whose checks
were skipped, a property that gives a new number each time. None of these may switch a cap off or raise the ceiling.
"""
import types

import pytest

import spend_brakes.guard as guard_module
from spend_brakes import (HARD_MAX_CALLS_PER_JOB, HARD_MAX_RETRIES, Caps, ConfigError, HardCeilingExceeded,
                          JobLimitReached, check_call)
from helpers import Script

GOOD = dict(monthly_usd=20.0, project_usd=None, max_calls_per_job=3)


def duck(**changes):
    """An object that is not a Caps but has the three fields."""
    return types.SimpleNamespace(**{**GOOD, **changes})


def forged(**changes):
    """A real Caps that skipped its own checks (its __init__ and __post_init__ never ran)."""
    caps = object.__new__(Caps)
    for name, value in {**GOOD, **changes}.items():
        object.__setattr__(caps, name, value)
    return caps


MAKERS = [pytest.param(duck, id="not-a-Caps"), pytest.param(forged, id="forged-Caps")]


# ---- the attack: a job limit of 500 ----------------------------------------------------------------------------

@pytest.mark.parametrize("make", MAKERS)
def test_500_calls_do_not_get_through_with_a_job_limit_of_500(call, ledger, make):
    fn = Script("ok")
    through = 0
    for _ in range(500):
        try:
            call(fn, caps=make(max_calls_per_job=500), estimate_usd=0.0)
            through += 1
        except HardCeilingExceeded as stop:
            assert stop.brake == "hard_ceiling" and "HARD_MAX_CALLS_PER_JOB" in str(stop)
    assert through == 0 and fn.calls == 0 and not ledger.path.exists()


@pytest.mark.parametrize("make", MAKERS)
def test_a_job_limit_just_above_the_ceiling_is_refused_and_the_ceiling_itself_works(call, make):
    fn = Script("ok")
    with pytest.raises(HardCeilingExceeded):
        call(fn, caps=make(max_calls_per_job=HARD_MAX_CALLS_PER_JOB + 1))
    for _ in range(HARD_MAX_CALLS_PER_JOB):
        call(fn, caps=make(max_calls_per_job=HARD_MAX_CALLS_PER_JOB, monthly_usd=100.0))
    with pytest.raises(JobLimitReached):
        call(fn, caps=make(max_calls_per_job=HARD_MAX_CALLS_PER_JOB, monthly_usd=100.0))
    assert fn.calls == HARD_MAX_CALLS_PER_JOB


def test_the_check_itself_never_uses_a_limit_above_the_ceiling(call, monkeypatch):
    """A second guard behind the first: even a limit that passed the Caps checks cannot go above the ceiling in the guard."""
    monkeypatch.setattr(guard_module, "HARD_MAX_CALLS_PER_JOB", 2)
    fn, caps = Script("ok"), Caps(monthly_usd=100.0, max_calls_per_job=5)
    call(fn, caps=caps)
    call(fn, caps=caps)
    with pytest.raises(JobLimitReached) as stop:
        call(fn, caps=caps)
    assert "limit 2," in str(stop.value) and fn.calls == 2


# ---- the attack: a cap that is not a finite number --------------------------------------------------------------

BAD_FIELDS = [
    dict(monthly_usd=float("nan")), dict(monthly_usd=float("inf")), dict(monthly_usd=float("-inf")),
    dict(monthly_usd=-0.01), dict(monthly_usd="20"), dict(monthly_usd=None), dict(monthly_usd=True),
    dict(monthly_usd=10 ** 400),
    dict(project_usd=float("nan")), dict(project_usd=float("inf")), dict(project_usd=-1), dict(project_usd="5"),
    dict(project_usd=False),
    dict(max_calls_per_job=float("nan")), dict(max_calls_per_job=float("inf")), dict(max_calls_per_job=0),
    dict(max_calls_per_job=-3), dict(max_calls_per_job=2.5), dict(max_calls_per_job=True), dict(max_calls_per_job="3"),
    dict(max_calls_per_job=None),
]


@pytest.mark.parametrize("make", MAKERS)
@pytest.mark.parametrize("bad", BAD_FIELDS, ids=lambda d: "-".join(f"{k}={v!r}"[:40] for k, v in d.items()))
def test_a_cap_that_is_not_a_usable_number_is_a_config_error_in_both_functions(call, ledger, make, bad):
    fn = Script("ok")
    with pytest.raises(ConfigError):
        call(fn, caps=make(**bad))
    with pytest.raises(ConfigError):
        check_call(estimate_usd=0.1, project="p", provider="image_api", job="j", caps=make(**bad), ledger=ledger, env={})
    assert fn.calls == 0 and not ledger.path.exists()


@pytest.mark.parametrize("not_caps", [
    types.SimpleNamespace(monthly_usd=20.0),  # missing fields
    types.SimpleNamespace(),
    {"monthly_usd": 20.0, "project_usd": None, "max_calls_per_job": 3},  # a dict has no such attributes
    "caps", 0, 20.0, [], object(),
])
def test_something_that_does_not_look_like_caps_is_a_config_error(call, ledger, not_caps):
    fn = Script("ok")
    with pytest.raises(ConfigError) as stop:
        call(fn, caps=not_caps)
    assert "caps" in str(stop.value)
    assert fn.calls == 0 and not ledger.path.exists()


def test_check_call_refuses_the_same_caps_with_the_same_message(call, ledger):
    for caps in (duck(max_calls_per_job=500), duck(monthly_usd=float("nan")), "caps"):
        with pytest.raises((ConfigError, HardCeilingExceeded)) as real:
            call(Script("ok"), caps=caps)
        with pytest.raises(type(real.value)) as dry:
            check_call(estimate_usd=0.1, project="proj-a", provider="image_api", job="job-1", caps=caps, ledger=ledger,
                       env={})
        assert str(dry.value) == str(real.value)


def test_a_nan_cap_no_longer_switches_the_cap_off(call, ledger):
    """The old hole: every comparison with nan is False, so a nan cap never stopped anything."""
    fn = Script("ok")
    for caps in (duck(monthly_usd=float("nan")), forged(monthly_usd=float("nan")),
                 duck(monthly_usd=100.0, project_usd=float("nan")), forged(monthly_usd=100.0, project_usd=float("nan"))):
        with pytest.raises(ConfigError):
            call(fn, caps=caps, estimate_usd=1000.0)
    assert fn.calls == 0


# ---- good objects still work, and the numbers are read once ---------------------------------------------------

def test_an_object_with_good_numbers_is_accepted(call):
    assert call(Script("fine"), caps=duck(monthly_usd=1.0, project_usd=0.5, max_calls_per_job=2)) == "fine"


def test_the_numbers_are_read_once_so_a_changing_property_cannot_give_a_higher_limit_later(call):
    class Shifty:
        monthly_usd, project_usd = 100.0, None
        reads = 0

        @property
        def max_calls_per_job(self):
            self.reads += 1
            return 3 if self.reads == 1 else 500

    shifty, fn = Shifty(), Script(types.SimpleNamespace(status_code=503))  # a server error: the guard tries again
    with pytest.raises(JobLimitReached):
        call(fn, caps=shifty, max_retries=HARD_MAX_RETRIES)
    assert fn.calls == 3 and shifty.reads == 1


# ---- max_retries: same rules, same ceiling ----------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1.5, 2.0, True, None, "2", -1])
def test_a_max_retries_that_is_not_a_whole_number_of_zero_or_more_is_a_config_error(call, ledger, bad):
    fn = Script("ok")
    with pytest.raises(ConfigError):
        call(fn, max_retries=bad)
    with pytest.raises(ConfigError):
        check_call(estimate_usd=0.1, project="p", provider="image_api", job="j", max_retries=bad, ledger=ledger,
                   caps=Caps(), env={})
    assert fn.calls == 0 and not ledger.path.exists()


@pytest.mark.parametrize("too_many", [HARD_MAX_RETRIES + 1, 50, 10 ** 9])
def test_max_retries_above_the_ceiling_is_refused_in_both_functions(call, ledger, too_many):
    fn = Script("ok")
    with pytest.raises(HardCeilingExceeded):
        call(fn, max_retries=too_many)
    with pytest.raises(HardCeilingExceeded):
        check_call(estimate_usd=0.1, project="p", provider="image_api", job="j", max_retries=too_many, ledger=ledger,
                   caps=Caps(), env={})
    assert fn.calls == 0 and not ledger.path.exists()
