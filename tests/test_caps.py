import dataclasses

import pytest

import spend_brakes.caps as caps_module
from spend_brakes import HARD_MAX_CALLS_PER_JOB, HARD_MAX_RETRIES, Caps, ConfigError, HardCeilingExceeded


def test_defaults_are_small_example_numbers():
    caps = Caps.from_env("proj", {})
    assert caps.monthly_usd == 20.0 and caps.project_usd is None and caps.max_calls_per_job == 6


def test_caps_are_read_from_the_environment():
    env = {"SPEND_MONTHLY_CAP_USD": "7.5", "SPEND_PROJECT_CAP_USD_BLOG_IMAGES": "2", "SPEND_MAX_CALLS_PER_JOB": "4"}
    caps = Caps.from_env("blog-images", env)
    assert (caps.monthly_usd, caps.project_usd, caps.max_calls_per_job) == (7.5, 2.0, 4)
    assert Caps.from_env("other", env).project_usd is None  # another project has no sub-cap


@pytest.mark.parametrize("name,value", [
    ("SPEND_MONTHLY_CAP_USD", "abc"), ("SPEND_MONTHLY_CAP_USD", "nan"), ("SPEND_MONTHLY_CAP_USD", "inf"),
    ("SPEND_MONTHLY_CAP_USD", "-1"), ("SPEND_PROJECT_CAP_USD_P", "x"), ("SPEND_MAX_CALLS_PER_JOB", "2.5"),
    ("SPEND_MAX_CALLS_PER_JOB", "0"), ("SPEND_MAX_CALLS_PER_JOB", "-3"),
])
def test_unreadable_settings_are_refused_not_ignored(name, value):
    with pytest.raises(ConfigError):
        Caps.from_env("p", {name: value})


def test_hard_ceiling_cannot_be_raised_by_an_argument():
    assert HARD_MAX_CALLS_PER_JOB == 20
    Caps(max_calls_per_job=HARD_MAX_CALLS_PER_JOB)  # exactly the ceiling is fine
    with pytest.raises(HardCeilingExceeded) as stop:
        Caps(max_calls_per_job=HARD_MAX_CALLS_PER_JOB + 1)
    assert stop.value.brake == "hard_ceiling"
    assert "hard ceiling" in str(stop.value) and "HARD_MAX_CALLS_PER_JOB" in str(stop.value)


def test_hard_ceiling_cannot_be_raised_by_the_environment():
    env = {"SPEND_MAX_CALLS_PER_JOB": "999", "SPEND_HARD_MAX_CALLS_PER_JOB": "999", "HARD_MAX_CALLS_PER_JOB": "999"}
    with pytest.raises(HardCeilingExceeded):
        Caps.from_env("p", env)
    with pytest.raises(HardCeilingExceeded):
        Caps(max_calls_per_job=HARD_MAX_CALLS_PER_JOB + 1)  # a made-up variable changes nothing
    assert caps_module.HARD_MAX_CALLS_PER_JOB == 20 and HARD_MAX_RETRIES == 3


def test_caps_cannot_be_changed_after_they_are_made():
    caps = Caps(max_calls_per_job=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.max_calls_per_job = 50  # type: ignore[misc]


def test_a_project_name_without_letters_is_refused():
    with pytest.raises(ConfigError):
        Caps.from_env("---", {})
