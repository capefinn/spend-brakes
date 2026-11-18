from pathlib import Path

import pytest

from spend_brakes import KillSwitchOn, Ledger, check_kill_switch
from helpers import Script


def test_nothing_set_means_go():
    check_kill_switch("image_api", {})


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "on", "yes"])
def test_master_switch_stops_every_provider(value):
    for provider in ("image_api", "llm_api", "anything"):
        with pytest.raises(KillSwitchOn) as stop:
            check_kill_switch(provider, {"SPEND_PANIC": value})
        assert stop.value.brake == "kill_switch"
        assert "SPEND_PANIC" in str(stop.value) and "owner" in str(stop.value)  # names the setting, leaves the choice to the owner


@pytest.mark.parametrize("value", ["0", "false", "off", "no", ""])
def test_master_switch_off_means_go(value):
    check_kill_switch("image_api", {"SPEND_PANIC": value})


def test_provider_switch_stops_only_that_provider():
    env = {"SPEND_IMAGE_API_ENABLED": "0"}
    with pytest.raises(KillSwitchOn) as stop:
        check_kill_switch("image_api", env)
    assert "SPEND_IMAGE_API_ENABLED" in str(stop.value)
    check_kill_switch("llm_api", env)


def test_provider_names_are_turned_into_variable_names():
    with pytest.raises(KillSwitchOn):
        check_kill_switch("image-api", {"SPEND_IMAGE_API_ENABLED": "off"})


@pytest.mark.parametrize("env", [{"SPEND_PANIC": "maybe"}, {"SPEND_IMAGE_API_ENABLED": "of f"}, {"SPEND_IMAGE_API_ENABLED": "disabled"}])
def test_a_value_that_cannot_be_read_counts_as_stop(env):
    with pytest.raises(KillSwitchOn) as stop:
        check_kill_switch("image_api", env)
    assert "cannot be read" in str(stop.value)


def test_stop_file_stops_calls_while_it_exists(tmp_path):
    stop_file = tmp_path / "STOP"
    env = {"SPEND_STOP_FILE": str(stop_file)}
    check_kill_switch("image_api", env)
    stop_file.write_text("")
    with pytest.raises(KillSwitchOn) as stop:
        check_kill_switch("image_api", env)
    assert "owner removes that file" in str(stop.value)
    stop_file.unlink()
    check_kill_switch("image_api", env)


def test_a_stopped_call_never_runs_and_writes_nothing(call, ledger):
    fn = Script("ok")
    with pytest.raises(KillSwitchOn):
        call(fn, env={"SPEND_PANIC": "1"})
    assert fn.calls == 0
    assert not ledger.path.exists()


def test_the_switch_is_read_again_on_every_call(call):
    env = {}
    fn = Script("ok")
    assert call(fn, env=env) == "ok"
    env["SPEND_PANIC"] = "1"
    with pytest.raises(KillSwitchOn):
        call(fn, env=env)
    assert fn.calls == 1


def test_the_switch_also_stops_a_retry(call):
    env = {}

    def flaky():
        env["SPEND_PANIC"] = "1"  # someone throws the switch while the first attempt is running
        raise ConnectionResetError("dropped")

    with pytest.raises(KillSwitchOn):
        call(flaky, env=env, max_retries=3)


# ---- the default stop file: a file named STOP in the same folder as the ledger file ------------------------

def test_the_default_stop_file_sits_next_to_the_ledger_file(tmp_path):
    assert Ledger(tmp_path / "ledger.jsonl").stop_file == tmp_path / "STOP"
    assert Ledger(tmp_path / "deep" / "other-name.jsonl").stop_file == tmp_path / "deep" / "STOP"
    assert Ledger.from_env({"SPEND_BRAKES_LEDGER": str(tmp_path / "x" / "l.jsonl")}).stop_file == tmp_path / "x" / "STOP"
    assert Ledger.from_env({}).stop_file == Path.home() / ".spend-brakes" / "STOP"  # next to the default ledger


def test_a_file_named_stop_next_to_the_ledger_stops_calls_with_no_setting_at_all(call, ledger):
    fn = Script("ok")
    assert call(fn) == "ok"  # env is empty: no SPEND_STOP_FILE anywhere
    stop_file = ledger.path.parent / "STOP"
    stop_file.write_text("")
    rows_before = ledger.rows()
    with pytest.raises(KillSwitchOn) as stop:
        call(fn)
    assert stop.value.brake == "kill_switch"
    assert str(stop_file) in str(stop.value) and "owner removes that file" in str(stop.value) and "SPEND_STOP_FILE" in str(stop.value)
    assert fn.calls == 1 and ledger.rows() == rows_before  # the stopped call never ran and wrote nothing
    stop_file.unlink()
    assert call(fn) == "ok"


def test_a_stop_file_in_another_folder_does_not_stop_calls(call, ledger, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "STOP").write_text("")
    assert call(Script("ok")) == "ok"


def test_the_default_stop_file_also_stops_a_running_program_between_attempts(call, ledger):
    def flaky():
        (ledger.path.parent / "STOP").write_text("")  # someone creates the file while the first attempt runs
        raise ConnectionResetError("dropped")

    with pytest.raises(KillSwitchOn):
        call(flaky, max_retries=3)


def test_spend_stop_file_replaces_the_default(call, ledger, tmp_path):
    default = ledger.path.parent / "STOP"
    default.write_text("")
    other = tmp_path / "my-own-stop-file"
    assert call(Script("ok"), env={"SPEND_STOP_FILE": str(other)}) == "ok"  # the named file does not exist: go
    other.write_text("")
    with pytest.raises(KillSwitchOn) as stop:
        call(Script("ok"), env={"SPEND_STOP_FILE": str(other)})
    assert "named in SPEND_STOP_FILE" in str(stop.value)
    with pytest.raises(KillSwitchOn):  # no setting: the default applies again
        call(Script("ok"))


def test_a_ledger_that_is_not_a_file_has_no_default_stop_file(tmp_path):
    class NoFileLedger:
        """Keeps rows in the real ledger but shows no file, like a ledger over a database."""

        def __init__(self, inner):
            self._inner = inner
            self.reserve, self.settle = inner.reserve, inner.settle

    inner = Ledger(tmp_path / "ledger.jsonl")
    (tmp_path / "STOP").write_text("")
    from spend_brakes import Caps, guarded_call
    result = guarded_call(lambda: "ok", estimate_usd=0.1, project="p", provider="image_api", job="j",
                          caps=Caps(monthly_usd=10.0), ledger=NoFileLedger(inner), env={})
    assert result == "ok"
    with pytest.raises(KillSwitchOn):  # the same folder with a real file ledger does stop
        guarded_call(lambda: "ok", estimate_usd=0.1, project="p", provider="image_api", job="j",
                     caps=Caps(monthly_usd=10.0), ledger=inner, env={})


def test_check_kill_switch_takes_a_default_stop_file_only_when_the_setting_is_empty(tmp_path):
    default = tmp_path / "STOP"
    check_kill_switch("image_api", {}, default)  # does not exist: go
    default.write_text("")
    with pytest.raises(KillSwitchOn):
        check_kill_switch("image_api", {}, default)
    with pytest.raises(KillSwitchOn):
        check_kill_switch("image_api", {"SPEND_STOP_FILE": "   "}, default)  # an empty setting is the same as no setting
    check_kill_switch("image_api", {"SPEND_STOP_FILE": str(tmp_path / "nothing")}, default)  # the setting replaces it
