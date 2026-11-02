import pytest

from spend_brakes import KillSwitchOn, check_kill_switch
from helpers import Script


def test_nothing_set_means_go():
    check_kill_switch("image_api", {})


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "on", "yes"])
def test_master_switch_stops_every_provider(value):
    for provider in ("image_api", "llm_api", "anything"):
        with pytest.raises(KillSwitchOn) as stop:
            check_kill_switch(provider, {"SPEND_PANIC": value})
        assert stop.value.brake == "kill_switch"
        assert "SPEND_PANIC" in str(stop.value) and "unset" in str(stop.value)  # says how to undo it


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
    assert "delete that file" in str(stop.value)
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
