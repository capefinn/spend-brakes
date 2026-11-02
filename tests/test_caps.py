import dataclasses

import pytest

import spend_brakes.caps as caps_module
from spend_brakes import HARD_MAX_CALLS_PER_JOB, HARD_MAX_RETRIES, Caps, ConfigError, HardCeilingExceeded, project_key


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
    # not a finite number: must be a ConfigError, never a crash from int() and never a silent default
    ("SPEND_MAX_CALLS_PER_JOB", "nan"), ("SPEND_MAX_CALLS_PER_JOB", "NaN"), ("SPEND_MAX_CALLS_PER_JOB", "inf"),
    ("SPEND_MAX_CALLS_PER_JOB", "-inf"), ("SPEND_MAX_CALLS_PER_JOB", "1e400"), ("SPEND_MAX_CALLS_PER_JOB", "Infinity"),
    ("SPEND_MONTHLY_CAP_USD", "1e400"), ("SPEND_PROJECT_CAP_USD_P", "inf"), ("SPEND_PROJECT_CAP_USD_P", "nan"),
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


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e400"])
def test_a_non_finite_job_limit_is_a_config_error_not_another_error_type(value):
    with pytest.raises(ConfigError) as stop:
        Caps.from_env("p", {"SPEND_MAX_CALLS_PER_JOB": value})
    assert type(stop.value) is ConfigError  # not ValueError, OverflowError or HardCeilingExceeded


def test_a_huge_but_finite_job_limit_is_stopped_by_the_ceiling():
    with pytest.raises(HardCeilingExceeded):
        Caps.from_env("p", {"SPEND_MAX_CALLS_PER_JOB": "1e300"})


@pytest.mark.parametrize("name", ["blog-images", "blog images", "Blog_Images", "BLOG-IMAGES", "  blog -- images  ", "__blog.images__"])
def test_every_spelling_of_a_project_name_has_the_key_of_its_setting_name(name):
    assert project_key(name) == "BLOG_IMAGES"
    assert caps_module.ENV_PROJECT_PREFIX + project_key(name) == "SPEND_PROJECT_CAP_USD_BLOG_IMAGES"


def test_different_names_have_different_keys_and_a_name_without_letters_keeps_its_text():
    assert project_key("blog-images-2") != project_key("blog-images") != project_key("blog")
    assert project_key("---") == "---" and project_key("ブログ") == "ブログ" and project_key("ニュース") != project_key("ブログ")
    assert project_key("") == ""  # never raises
    assert project_key("Kuvat äänet") == project_key("kuvat-äänet") == "KUVAT_NET"
