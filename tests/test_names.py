"""Names that are not plain English: Finnish (one character or two for each ä), Japanese, Arabic, Cyrillic.

The key of a name is: NFKC, case-fold, every run of characters that are not letters or digits becomes "_", strip "_",
capital letters. The TypeScript test file has the same table of names and keys, so both languages give the same keys.
"""
import unicodedata

import pytest

from spend_brakes import (BrakeError, Caps, ConfigError, KillSwitchOn, ProjectCapReached, check_call, project_key)
from helpers import Script

FI_NFC = unicodedata.normalize("NFC", "Kuvat äänet")
FI_NFD = unicodedata.normalize("NFD", "Kuvat äänet")

# The same table is in typescript/spend-brakes.test.ts. Keep the two tables equal.
KEYS = [
    ("blog-images", "BLOG_IMAGES"), ("Blog_Images", "BLOG_IMAGES"), ("BLOG IMAGES", "BLOG_IMAGES"),
    ("  blog -- images  ", "BLOG_IMAGES"), ("__blog.images__", "BLOG_IMAGES"), ("blog-images-2", "BLOG_IMAGES_2"),
    (FI_NFC, "KUVAT_ÄÄNET"), (FI_NFD, "KUVAT_ÄÄNET"), ("kuvat-äänet", "KUVAT_ÄÄNET"), ("KUVAT_ÄÄNET", "KUVAT_ÄÄNET"),
    ("Äänet", "ÄÄNET"), ("net", "NET"),
    ("ブログ", "ブログ"), ("ブログ v2", "ブログ_V2"), ("画像 v2", "画像_V2"), ("ﾌﾞﾛｸﾞ", "ブログ"),
    ("مدونة", "مدونة"),
    ("Блог", "БЛОГ"), ("Москва-2026", "МОСКВА_2026"), ("Киев-2026", "КИЕВ_2026"),
    ("Straße", "STRASSE"), ("ＢＬＯＧ　ｉｍａｇｅｓ", "BLOG_IMAGES"), ("ΐ", "Ϊ́"),
    ("!!!", ""), ("---", ""), ("   ", ""), ("🚀", ""), ("🚀🚀", ""), ("", ""),
]
EMPTY_KEY = ["!!!", "---", "   ", "...", "🚀", "🚀 🚀", "—", "_"]
NAMES_FROM_OTHER_SCRIPTS = [FI_NFC, FI_NFD, "ブログ", "画像 v2", "مدونة", "Блог", "Москва-2026"]


@pytest.mark.parametrize("name,key", KEYS)
def test_the_key_of_a_name(name, key):
    assert project_key(name) == key


def test_finnish_in_both_forms_is_one_key_and_different_names_are_different_keys():
    assert FI_NFC != FI_NFD and project_key(FI_NFC) == project_key(FI_NFD) == "KUVAT_ÄÄNET"
    assert project_key("Äänet") != project_key("net")
    assert project_key("ブログ v2") != project_key("画像 v2")
    assert project_key("Москва-2026") != project_key("Киев-2026")


# ---- a name in another script works in the default setup --------------------------------------------------------

@pytest.mark.parametrize("name", NAMES_FROM_OTHER_SCRIPTS)
def test_a_project_name_in_another_script_works_when_no_project_cap_is_set(call, ledger, name):
    fn = Script("ok")
    for _ in range(3):
        call(fn, project=name, caps=None, env={"SPEND_MONTHLY_CAP_USD": "100"})  # the caps come from the environment
        call(fn, project=name)  # caps given as an argument
    assert fn.calls == 6 and ledger.project_total(name) == pytest.approx(0.60)


@pytest.mark.parametrize("name", NAMES_FROM_OTHER_SCRIPTS)
def test_a_provider_name_in_another_script_works(call, name):
    call(Script("ok"), provider=name, caps=None, env={})


@pytest.mark.parametrize("name", NAMES_FROM_OTHER_SCRIPTS)
def test_a_project_cap_reached_by_such_a_name_is_a_brake_and_never_a_config_error(call, ledger, name):
    fn = Script("ok")
    caps = Caps(monthly_usd=100.0, project_usd=0.25, max_calls_per_job=20)
    call(fn, project=name, caps=caps)
    call(fn, project=name, caps=caps)
    with pytest.raises(ProjectCapReached) as stop:
        call(fn, project=name, caps=caps)
    assert isinstance(stop.value, BrakeError) and not isinstance(stop.value, ConfigError)
    assert stop.value.brake == "project_cap" and "SPEND_PROJECT_CAP_USD_" + project_key(name) in str(stop.value)
    assert fn.calls == 2


def test_the_project_cap_setting_works_for_a_finnish_name_in_either_form(call):
    env = {"SPEND_MONTHLY_CAP_USD": "100", "SPEND_PROJECT_CAP_USD_KUVAT_ÄÄNET": "0.25", "SPEND_MAX_CALLS_PER_JOB": "20"}
    fn = Script("ok")
    call(fn, project=FI_NFC, caps=None, env=env)
    call(fn, project=FI_NFD, caps=None, env=env)  # the other form uses the same setting and the same total
    for name in (FI_NFC, FI_NFD):
        with pytest.raises(ProjectCapReached):
            call(fn, project=name, caps=None, env=env)


# ---- what is counted together, and what is not -----------------------------------------------------------------

def test_both_forms_of_a_finnish_name_share_one_project_total_and_the_rows_keep_each_text(call, ledger):
    caps = Caps(monthly_usd=100.0, project_usd=0.30, max_calls_per_job=20)
    for name in (FI_NFC, FI_NFD, FI_NFC):
        call(Script("ok"), project=name, caps=caps)
    for name in (FI_NFC, FI_NFD):
        with pytest.raises(ProjectCapReached):
            call(Script("ok"), project=name, caps=caps)
    assert [row["project"] for row in ledger.rows() if row["event"] == "reserve"] == [FI_NFC, FI_NFD, FI_NFC]
    assert ledger.project_total(FI_NFC) == ledger.project_total(FI_NFD) == pytest.approx(0.30)


def test_both_forms_share_the_job_count(call):
    from spend_brakes import JobLimitReached
    caps, fn = Caps(monthly_usd=100.0, max_calls_per_job=3), Script("ok")
    call(fn, project=FI_NFC, job="run-1", caps=caps)
    call(fn, project=FI_NFD, job="run-1", caps=caps)
    call(fn, project=FI_NFC, job="run-1", caps=caps)
    for name in (FI_NFC, FI_NFD):
        with pytest.raises(JobLimitReached):
            call(fn, project=name, job="run-1", caps=caps)


@pytest.mark.parametrize("first,second", [("Äänet", "net"), ("ブログ v2", "画像 v2"), ("Москва-2026", "Киев-2026"), ("ブログ", "画像")])
def test_different_names_do_not_share_a_cap(call, first, second):
    caps, fn = Caps(monthly_usd=100.0, project_usd=0.25, max_calls_per_job=20), Script("ok")
    for name in (first, second):
        call(fn, project=name, caps=caps)
        call(fn, project=name, caps=caps)
        with pytest.raises(ProjectCapReached):
            call(fn, project=name, caps=caps)
    assert fn.calls == 4


def test_the_provider_switch_for_a_name_in_another_script(call):
    for name, setting in [("ブログ", "SPEND_ブログ_ENABLED"), (FI_NFC, "SPEND_KUVAT_ÄÄNET_ENABLED"), (FI_NFD, "SPEND_KUVAT_ÄÄNET_ENABLED")]:
        with pytest.raises(KillSwitchOn) as stop:
            call(Script("ok"), provider=name, env={setting: "0"})
        assert setting in str(stop.value)


# ---- a name with no letter and no digit is refused, at once, on every call --------------------------------------

@pytest.mark.parametrize("field", ["project", "provider"])
@pytest.mark.parametrize("name", EMPTY_KEY)
def test_a_name_with_no_letter_and_no_digit_is_a_config_error_on_every_call(call, ledger, field, name):
    fn = Script("ok")
    with pytest.raises(ConfigError, match="no letter and no digit") as stop:
        call(fn, **{field: name})  # caps given, and no project cap
    assert field in str(stop.value) and not isinstance(stop.value, BrakeError)
    with pytest.raises(ConfigError, match="no letter and no digit"):
        call(fn, caps=None, env={"SPEND_MONTHLY_CAP_USD": "100"}, **{field: name})  # caps from the environment
    with pytest.raises(ConfigError, match="no letter and no digit"):
        call(fn, caps=Caps(monthly_usd=100.0, project_usd=5.0), **{field: name})  # with a project cap
    with pytest.raises(ConfigError, match="no letter and no digit"):
        check_call(**{**dict(estimate_usd=0.1, project="p", provider="x", job="j", caps=Caps(), ledger=ledger, env={}), field: name})
    assert fn.calls == 0 and not ledger.path.exists()


def test_a_job_name_is_not_a_key_so_punctuation_alone_is_allowed_there(call):
    call(Script("ok"), job="!!!")
