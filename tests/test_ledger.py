import datetime as dt
import json
import os
import subprocess
import sys
import threading
import warnings
from pathlib import Path

import pytest

import spend_brakes.ledger as ledger_module
from spend_brakes import BrakeError, Caps, ConfigError, Ledger, LedgerUnreadable, MonthlyCapReached
from helpers import Script

SRC = str(Path(__file__).resolve().parent.parent / "src")


def reserve(ledger, usd=0.10, project="a", job="j", check=lambda totals: None):
    return ledger.reserve(project=project, provider="x", job=job, usd=usd, check=check)


def settle(ledger, call_id, usd, project="a", job="j", outcome="ok"):
    ledger.settle(call_id, project=project, provider="x", job=job, usd=usd, outcome=outcome)


def test_rows_are_only_added_never_changed(ledger):
    first = reserve(ledger)
    before = ledger.path.read_bytes()
    settle(ledger, first, 0.04)
    reserve(ledger)
    after = ledger.path.read_bytes()
    assert after.startswith(before)  # the old bytes are untouched, new rows come after them
    assert len(after.splitlines()) == 3


def test_month_total_and_project_total(ledger):
    reserve(ledger, 0.30, project="a")
    reserve(ledger, 0.20, project="b")
    assert ledger.month_total() == 0.50
    assert ledger.project_total("a") == 0.30
    assert ledger.project_total("b") == 0.20
    assert ledger.project_total("nobody") == 0


def test_settle_replaces_the_estimate_it_does_not_add_to_it(ledger):
    call_id = reserve(ledger, 0.10)
    settle(ledger, call_id, 0.04)
    assert ledger.month_total() == 0.04


def test_reserve_without_settle_keeps_counting_the_estimate(ledger):
    reserve(ledger, 0.10)  # the process died before the call ended
    assert ledger.month_total() == 0.10


def test_other_months_do_not_count(tmp_path):
    now = {"t": dt.datetime(1999, 1, 31, 23, 59, tzinfo=dt.timezone.utc)}
    ledger = Ledger(tmp_path / "l.jsonl", clock=lambda: now["t"])
    reserve(ledger, 5.00)
    assert ledger.month_total() == 5.00
    now["t"] = dt.datetime(1999, 2, 1, 0, 1, tzinfo=dt.timezone.utc)
    assert ledger.month_total() == 0


def test_job_calls_count_every_call_even_free_ones(ledger):
    for _ in range(3):
        call_id = reserve(ledger, 0.10, job="j1")
        settle(ledger, call_id, 0.0, job="j1", outcome="rejected")
    reserve(ledger, job="j2")
    assert ledger.job_calls("a", "j1") == 3
    assert ledger.job_calls("a", "j2") == 1
    assert ledger.job_calls("b", "j1") == 0  # same job name in another project is another job


GARBAGE = [
    b"this is not json\n",
    b'{"event":"reserve","id":"x","project":"a","job":"j","t":"1999-01-01T00:00:00+00:00","usd":-5}\n',  # negative amount
    b'{"event":"reserve","id":"y","project":"a","job":"j","t":"1999-01-01T00:00:00+00:00","usd":NaN}\n',
    b'{"event":"reserve","id":"z","project":"a","job":"j","usd":1}\n',  # no time: it could not be put in a month
    b'{"event":"reserve","id":"w","job":"j","t":"1999-01-01T00:00:00+00:00","usd":1}\n',  # no project
    b'{"event":"reserve","id":"v","project":"a","t":"1999-01-01T00:00:00+00:00","usd":1}\n',  # no job
    b'{"event":"maybe","id":"u","project":"a","job":"j","t":"1999-01-01T00:00:00+00:00","usd":1}\n',
    b"[1, 2, 3]\n",
    b"\xff\xfe not text \x00\n",
    b"[" * 100000 + b"\n",  # nesting so deep that the JSON reader gives up with another error type
    # a whole number too big for a float: isfinite() raises OverflowError on it, and that must not escape
    b'{"event":"reserve","id":"big","project":"a","job":"j","t":"1999-01-01T00:00:00+00:00","usd":1' + b"0" * 400 + b"}\n",
    b'{"event":"settle","id":"big2","project":"a","job":"j","t":"1999-01-01T00:00:00+00:00","usd":-1' + b"0" * 400 + b"}\n",
]


@pytest.mark.parametrize("garbage", GARBAGE)
def test_a_line_that_cannot_be_read_stops_every_call_it_is_not_skipped_silently(ledger, garbage):
    reserve(ledger, 0.10)
    with open(ledger.path, "ab") as f:
        f.write(garbage)
    before = ledger.path.read_bytes()
    with pytest.raises(LedgerUnreadable) as stop:
        reserve(ledger, 0.10)
    assert stop.value.brake == "ledger_unreadable"
    text = str(stop.value)
    assert "line 2" in text and "make a copy" in text and "SPEND_BRAKES_SKIP_UNREADABLE" in text  # where, how to repair
    assert ledger.path.read_bytes() == before  # nothing was added to a damaged file
    with pytest.raises(LedgerUnreadable):
        ledger.month_total()
    assert len(ledger.rows()) == 1 and ledger.unreadable_line_numbers() == [2]  # looking at the file still works


def test_one_broken_line_can_no_longer_turn_the_spend_into_zero(ledger, call):
    reserve(ledger, 19.99)
    ledger.path.write_bytes(ledger.path.read_bytes().replace(b'"usd":19.99', b'"usd":19.99,,'))  # one character damage
    fn = Script("ok")
    with pytest.raises(LedgerUnreadable):
        call(fn, caps=Caps(monthly_usd=20.0), estimate_usd=0.05)
    assert fn.calls == 0


def test_skipping_unreadable_lines_is_an_explicit_choice_and_the_skipped_spend_is_not_counted(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", skip_unreadable=True)
    reserve(ledger, 0.10)
    with open(ledger.path, "ab") as f:
        f.write(b"this is not json\n")
        f.write(b'{"event":"reserve","id":"x","project":"a","job":"j","t":"1999-01-01T00:00:00+00:00","usd":-5}\n')
    with pytest.warns(UserWarning, match="NOT counted"):
        totals = ledger.totals("a")
    assert totals.month_usd == 0.10 and totals.unreadable_lines == 2
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # the warning is shown once for this file: later reads are quiet
        reserve(ledger, 0.10)  # calls go on
        assert ledger.month_total() == 0.20


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("0", False), ("", False), ("off", False)])
def test_the_skip_choice_can_also_be_made_with_an_environment_variable(tmp_path, value, expected):
    env = {"SPEND_BRAKES_LEDGER": str(tmp_path / "l.jsonl"), "SPEND_BRAKES_SKIP_UNREADABLE": value}
    assert Ledger.from_env(env).skip_unreadable is expected
    assert Ledger.from_env({"SPEND_BRAKES_LEDGER": str(tmp_path / "l.jsonl")}).skip_unreadable is False  # off by default


def test_an_unreadable_skip_setting_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        Ledger.from_env({"SPEND_BRAKES_SKIP_UNREADABLE": "perhaps"})


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), (" On ", True), ("YES", True), ("0", False), ("", False),
                                            ("off", False), ("No", False)])
def test_a_ledger_made_with_a_path_reads_the_skip_setting_like_the_typescript_fileledger(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("SPEND_BRAKES_SKIP_UNREADABLE", value)
    assert Ledger(tmp_path / "l.jsonl").skip_unreadable is expected
    assert Ledger().skip_unreadable is expected  # the default path as well


def test_a_ledger_made_with_a_path_is_strict_when_the_setting_is_not_set(tmp_path):
    assert Ledger(tmp_path / "l.jsonl").skip_unreadable is False


@pytest.mark.parametrize("value", ["maybe", "2", "enabled", "disabled", "y", "tru"])
def test_a_ledger_made_with_a_path_refuses_an_unknown_skip_value(tmp_path, monkeypatch, value):
    monkeypatch.setenv("SPEND_BRAKES_SKIP_UNREADABLE", value)
    with pytest.raises(ConfigError, match="SPEND_BRAKES_SKIP_UNREADABLE.*cannot be read. Use 1 or 0"):
        Ledger(tmp_path / "l.jsonl")


def test_an_explicit_skip_argument_wins_over_the_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEND_BRAKES_SKIP_UNREADABLE", "1")
    assert Ledger(tmp_path / "l.jsonl", skip_unreadable=False).skip_unreadable is False
    monkeypatch.setenv("SPEND_BRAKES_SKIP_UNREADABLE", "0")
    assert Ledger(tmp_path / "l.jsonl", skip_unreadable=True).skip_unreadable is True
    monkeypatch.setenv("SPEND_BRAKES_SKIP_UNREADABLE", "maybe")  # not read at all when the argument is given
    assert Ledger(tmp_path / "l.jsonl", skip_unreadable=False).skip_unreadable is False


@pytest.mark.parametrize("value", ["0", "false", "False", "true", "1", "", " ", 0, 1, 2, 0.0, 1.0, [], [True], object()])
def test_an_explicit_skip_argument_must_be_a_real_bool(tmp_path, monkeypatch, value):
    for setting in (None, "1", "0"):  # the setting does not matter: a wrong argument is refused
        if setting is None:
            monkeypatch.delenv("SPEND_BRAKES_SKIP_UNREADABLE", raising=False)
        else:
            monkeypatch.setenv("SPEND_BRAKES_SKIP_UNREADABLE", setting)
        with pytest.raises(ConfigError, match="skip_unreadable must be True or False"):
            Ledger(tmp_path / "l.jsonl", skip_unreadable=value)
    assert not (tmp_path / "l.jsonl").exists()


def test_skip_false_as_text_does_not_turn_skipping_on_and_a_real_bool_still_works(tmp_path, monkeypatch):
    path = tmp_path / "l.jsonl"
    path.write_text("garbage\n")
    with pytest.raises(ConfigError):
        Ledger(path, skip_unreadable="false")  # before: this switched skipping ON
    assert Ledger(path, skip_unreadable=False).skip_unreadable is False
    assert Ledger(path, skip_unreadable=True).skip_unreadable is True
    with pytest.raises(LedgerUnreadable):
        Ledger(path, skip_unreadable=False).month_total()
    monkeypatch.setenv("SPEND_BRAKES_SKIP_UNREADABLE", "1")
    assert Ledger(path, skip_unreadable=None).skip_unreadable is True  # None means: read the setting
    monkeypatch.delenv("SPEND_BRAKES_SKIP_UNREADABLE")
    assert Ledger(path, skip_unreadable=None).skip_unreadable is False


def test_the_skip_setting_makes_a_damaged_ledger_usable_and_without_it_the_calls_stop(tmp_path, monkeypatch):
    path = tmp_path / "l.jsonl"
    reserve(Ledger(path), 0.10)
    with open(path, "ab") as f:
        f.write(b"this is not json\n")
    with pytest.raises(LedgerUnreadable):
        Ledger(path).month_total()
    monkeypatch.setenv("SPEND_BRAKES_SKIP_UNREADABLE", "1")
    with pytest.warns(UserWarning, match="NOT counted"):
        assert Ledger(path).month_total() == 0.10


# ---- the skip warning: once per program for each ledger file -----------------------------------------------------

def test_the_skip_warning_is_shown_once_for_a_file_even_when_the_number_of_bad_lines_changes(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_text("garbage\n")
    ledger = Ledger(path, skip_unreadable=True)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        for _ in range(3):
            reserve(ledger, 0.01)
        with open(path, "a") as f:
            f.write("more garbage\n")  # now two bad lines
        for _ in range(3):
            reserve(ledger, 0.01)
        Ledger(path, skip_unreadable=True).totals("a")  # another Ledger object for the same file
    assert len(seen) == 1 and f"skipping 1 unreadable ledger line(s) in {path};" in str(seen[0].message)


def test_the_skip_warning_is_shown_once_for_each_ledger_file(tmp_path):
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    first.write_text("garbage\n")
    second.write_text("garbage\ngarbage\n")
    clean = tmp_path / "c.jsonl"
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        Ledger(clean, skip_unreadable=True).totals("a")  # nothing to skip: no warning
        for path in (first, second, first, second, first):
            Ledger(path, skip_unreadable=True).totals("a")
    assert [str(w.message) for w in seen] == [  # the message names the file, so two files give two different warnings
        f"spend_brakes: skipping 1 unreadable ledger line(s) in {first}; their spend is NOT counted.",
        f"spend_brakes: skipping 2 unreadable ledger line(s) in {second}; their spend is NOT counted.",
    ]


def test_two_ledger_files_with_the_same_number_of_bad_lines_both_warn_under_the_default_warning_filter(tmp_path):
    """Python hides a repeated warning with the same text. The file name in the text keeps the second file visible."""
    code = (
        "import sys, warnings; sys.path.insert(0, %r)\n"
        "from spend_brakes import Ledger\n"
        "for name in ('a.jsonl', 'b.jsonl', 'a.jsonl'):\n"
        "    Ledger(%r + '/' + name, skip_unreadable=True).totals('p')\n"
    ) % (SRC, str(tmp_path))
    (tmp_path / "a.jsonl").write_text("garbage\n")
    (tmp_path / "b.jsonl").write_text("garbage\n")
    result = subprocess.run([sys.executable, "-W", "default", "-c", code], capture_output=True, text=True)
    lines = [line for line in result.stderr.splitlines() if "unreadable ledger line" in line and "UserWarning" in line]
    assert result.returncode == 0 and len(lines) == 2 and "a.jsonl" in lines[0] and "b.jsonl" in lines[1]


def test_a_row_cut_off_by_a_crash_is_reported_and_does_not_hide_the_next_row(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = Ledger(path)
    reserve(ledger, 0.10)
    with open(path, "ab") as f:
        f.write(b'{"event":"reserve","id":"cut","project":"a","job":"j","t":"1999-01-01T00:00:00+')  # no end of line
    with pytest.raises(LedgerUnreadable):  # the cut-off line is damage: calls stop until it is repaired
        reserve(ledger, 0.10)
    lenient = Ledger(path, skip_unreadable=True)
    with pytest.warns(UserWarning):
        reserve(lenient, 0.30)  # a new row after a cut-off line starts on its own line
    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b"" and json.loads(lines[-2])["usd"] == 0.30  # the new row is whole and readable
    assert len(ledger.rows()) == 2 and ledger.unreadable_line_numbers() == [2]  # the first row and the new one


def test_a_whole_last_row_that_only_lacks_its_end_of_line_is_fine(tmp_path):
    path = tmp_path / "l.jsonl"
    ledger = Ledger(path)
    reserve(ledger, 0.10)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))  # the row is complete, only the end-of-line mark is missing
    reserve(ledger, 0.20)  # no error, and the two rows stay on separate lines
    assert len(path.read_bytes().splitlines()) == 2 and ledger.month_total() == 0.30


def test_a_write_that_the_system_cuts_short_is_completed(ledger, monkeypatch):
    real_write, calls = os.write, []

    def short_write(fd, data):
        calls.append(len(data))
        return real_write(fd, bytes(data[:7])) if len(calls) == 1 else real_write(fd, data)

    monkeypatch.setattr(ledger_module.os, "write", short_write)
    reserve(ledger, 0.10)
    monkeypatch.undo()
    assert len(calls) == 2 and ledger.unreadable_line_numbers() == [] and ledger.month_total() == 0.10
    assert len(ledger.path.read_bytes().splitlines()) == 1


def test_settle_also_writes_after_a_cut_off_line(ledger):
    call_id = reserve(ledger, 0.10)
    with open(ledger.path, "ab") as f:
        f.write(b'{"event":"settle","id":"zz"')  # cut off, no end of line
    settle(ledger, call_id, 0.04)
    assert ledger.path.read_bytes().endswith(b"\n") and ledger.unreadable_line_numbers() == [2]
    assert [r["event"] for r in ledger.rows()] == ["reserve", "settle"]


def test_a_refusing_check_writes_nothing(ledger):
    def refuse(totals):
        raise MonthlyCapReached("no")

    with pytest.raises(BrakeError):
        reserve(ledger, check=refuse)
    assert not ledger.path.exists() or ledger.path.read_bytes() == b""


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, "1", None, True])
def test_bad_amounts_are_refused(ledger, bad):
    with pytest.raises(ConfigError):
        reserve(ledger, bad)


def test_threads_cannot_pass_the_same_cap(ledger):
    cap, estimate, wins = 5.0, 0.10, []

    def check(totals):
        if round(totals.month_usd + estimate, 6) > cap:
            raise MonthlyCapReached("full")

    def worker():
        for _ in range(10):
            try:
                reserve(ledger, estimate, check=check)
                wins.append(1)
            except MonthlyCapReached:
                pass

    threads = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 50  # 5.00 / 0.10: not 49, not 51
    assert len(ledger.rows()) == 50


@pytest.mark.skipif(os.name != "posix", reason="the cross-process lock needs fcntl")
def test_processes_append_at_the_same_time_without_losing_or_mixing_rows(tmp_path):
    path = tmp_path / "shared.jsonl"
    env = dict(os.environ, PYTHONPATH=SRC)
    worker = Path(__file__).with_name("_worker.py")
    procs = [subprocess.Popen([sys.executable, str(worker), str(path), "12", "3.0", "0.1"],
                              stdout=subprocess.PIPE, text=True, env=env) for _ in range(6)]
    wins = sum(int(p.communicate()[0]) for p in procs)
    assert all(p.returncode == 0 for p in procs)
    assert wins == 30  # 72 tries, a cap that fits exactly 30: the cap held across processes
    lines = path.read_text().splitlines()
    assert len(lines) == 30
    assert all(json.loads(line)["event"] == "reserve" for line in lines)  # every line whole and valid
    assert Ledger(path).month_total() == 3.0


def test_a_huge_whole_number_in_the_ledger_is_unreadable_not_a_crash(ledger, call):
    reserve(ledger, 0.10)
    huge = b'{"event":"reserve","id":"big","project":"a","job":"j","t":"1999-01-01T00:00:00+00:00","usd":1' + b"0" * 400 + b"}\n"
    with open(ledger.path, "ab") as f:
        f.write(huge)
    fn = Script("ok")
    with pytest.raises(LedgerUnreadable) as stop:  # not OverflowError
        call(fn, caps=Caps(monthly_usd=100.0))
    assert type(stop.value) is LedgerUnreadable and "line 2" in str(stop.value)
    assert fn.calls == 0
    assert ledger.unreadable_line_numbers() == [2]
    lenient = Ledger(ledger.path, skip_unreadable=True)  # with the explicit opt-in the row is skipped, not counted
    with pytest.warns(UserWarning):
        assert lenient.month_total() == 0.10


@pytest.mark.parametrize("huge", [10 ** 400, -(10 ** 400), 10 ** 309])
def test_a_huge_whole_number_as_a_setting_or_an_estimate_is_a_config_error(ledger, call, huge):
    with pytest.raises(ConfigError) as stop:
        reserve(ledger, huge)
    assert type(stop.value) is ConfigError and len(str(stop.value)) < 200  # the message does not print 400 digits
    with pytest.raises(ConfigError):
        Caps(monthly_usd=huge)
    fn = Script("ok")
    with pytest.raises(ConfigError):
        call(fn, estimate_usd=huge)
    assert fn.calls == 0 and not ledger.path.exists()
