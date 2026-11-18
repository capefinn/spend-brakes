"""check_call: the read-only 'would this call be allowed?' check. It must run the same brakes as guarded_call,
raise the same errors, and write nothing."""
import pytest

from spend_brakes import (BrakeError, Caps, ConfigError, HardCeilingExceeded, JobLimitReached, KillSwitchOn,
                          LedgerUnreadable, Ledger, MonthlyCapReached, ProjectCapReached, check_call, guarded_call)
from helpers import Script


def settings(ledger, **overrides):
    values = dict(estimate_usd=0.10, project="proj-a", provider="image_api", job="job-1",
                  caps=Caps(monthly_usd=100.0, max_calls_per_job=20), ledger=ledger, env={})
    values.update(overrides)
    return values


def snapshot(folder):
    """Every file and folder below `folder`, with the size of each file."""
    return sorted((str(p.relative_to(folder)), p.stat().st_size if p.is_file() else -1) for p in folder.rglob("*"))


def test_an_allowed_call_returns_nothing_and_writes_nothing(tmp_path):
    folder = tmp_path / "fresh-folder"  # does not exist yet
    ledger = Ledger(folder / "ledger.jsonl")
    assert check_call(**settings(ledger)) is None
    assert not folder.exists()  # no ledger file, no lock file, not even the folder


def test_it_does_not_change_an_existing_ledger_or_count_for_the_job_limit(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    guarded_call(lambda: "ok", **settings(ledger))
    before = snapshot(tmp_path)
    text = ledger.path.read_text()
    for _ in range(50):
        check_call(**settings(ledger, caps=Caps(monthly_usd=100.0, max_calls_per_job=2)))  # 50 checks, limit 2
    assert snapshot(tmp_path) == before and ledger.path.read_text() == text
    assert ledger.job_calls("proj-a", "job-1") == 1


def used_ledger(tmp_path, runs=0, each=0.10):
    """A ledger that already holds `runs` finished calls of `each` dollars, all in project proj-a and job job-1."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    for _ in range(runs):
        guarded_call(lambda: "ok", **settings(ledger, estimate_usd=each))
    return ledger


def with_stop_file(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    (ledger.path.parent / "STOP").write_text("")
    return ledger


def with_damaged_line(tmp_path):
    ledger = used_ledger(tmp_path, runs=1)
    with open(ledger.path, "a") as handle:
        handle.write("this line is not a ledger row\n")
    return ledger


# name -> (how to build the arguments, the error that a real call raises)
STOPS = {
    "kill_switch": (lambda t: settings(used_ledger(t), env={"SPEND_PANIC": "1"}), KillSwitchOn),
    "provider_switch": (lambda t: settings(used_ledger(t), env={"SPEND_IMAGE_API_ENABLED": "0"}), KillSwitchOn),
    "default_stop_file": (lambda t: settings(with_stop_file(t)), KillSwitchOn),
    "job_limit": (lambda t: settings(used_ledger(t, runs=2), caps=Caps(monthly_usd=100.0, max_calls_per_job=2)), JobLimitReached),
    "project_cap": (lambda t: settings(used_ledger(t, runs=2), caps=Caps(monthly_usd=100.0, project_usd=0.25), job="job-2"), ProjectCapReached),
    "monthly_cap": (lambda t: settings(used_ledger(t, runs=2), caps=Caps(monthly_usd=0.25), job="job-2"), MonthlyCapReached),
    "ledger_unreadable": (lambda t: settings(with_damaged_line(t)), LedgerUnreadable),
    "hard_ceiling": (lambda t: settings(used_ledger(t), max_retries=4), HardCeilingExceeded),
    "bad_estimate": (lambda t: settings(used_ledger(t), estimate_usd=float("nan")), ConfigError),
    "bad_job_name": (lambda t: settings(used_ledger(t), job=""), ConfigError),
}


@pytest.mark.parametrize("name", list(STOPS))
def test_it_raises_the_same_error_with_the_same_message_as_a_real_call(tmp_path, name):
    build, error_type = STOPS[name]
    args = build(tmp_path)
    before = snapshot(tmp_path)

    with pytest.raises(error_type) as dry:
        check_call(**args)
    assert snapshot(tmp_path) == before  # a refusal writes nothing either

    fn = Script("ok")
    with pytest.raises(error_type) as real:
        guarded_call(fn, **args)
    assert fn.calls == 0  # the real call was stopped too
    assert type(dry.value) is type(real.value) and str(dry.value) == str(real.value)
    if isinstance(dry.value, BrakeError):
        assert dry.value.brake == real.value.brake


def test_the_same_arguments_work_for_both_functions(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    args = settings(ledger, max_retries=2, retry_delay_s=0.0, cost_from_result=lambda r: 0.01, sleep=lambda s: None)
    check_call(**args)
    assert guarded_call(lambda: "ok", **args) == "ok"


def test_it_reads_the_settings_from_the_environment_like_a_real_call(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    env = {"SPEND_MONTHLY_CAP_USD": "0.05"}  # below the estimate
    with pytest.raises(MonthlyCapReached) as dry:
        check_call(estimate_usd=0.10, project="p", provider="image_api", job="j", ledger=ledger, env=env)
    assert "SPEND_MONTHLY_CAP_USD" in str(dry.value)
    env["SPEND_MONTHLY_CAP_USD"] = "5"
    check_call(estimate_usd=0.10, project="p", provider="image_api", job="j", ledger=ledger, env=env)


def test_checking_does_not_use_up_the_job_limit_but_real_calls_do(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    caps = Caps(monthly_usd=100.0, max_calls_per_job=2)
    for _ in range(5):
        check_call(**settings(ledger, caps=caps))  # five checks: still nothing used
    for _ in range(2):
        guarded_call(lambda: "ok", **settings(ledger, caps=caps))
    with pytest.raises(JobLimitReached) as stop:
        check_call(**settings(ledger, caps=caps))
    assert "limit 2" in str(stop.value)
