import datetime as dt
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from spend_brakes import BrakeError, ConfigError, Ledger, MonthlyCapReached

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


def test_unreadable_and_invalid_rows_are_skipped_and_counted(ledger):
    reserve(ledger, 0.10)
    with open(ledger.path, "ab") as f:
        f.write(b"this is not json\n")
        f.write(b'{"event":"reserve","id":"x","usd":-5}\n')  # a negative amount must not lower the total
        f.write(b'{"event":"reserve","id":"y","usd":NaN}\n')
    totals = ledger.totals("a")
    assert totals.month_usd == 0.10
    assert totals.unreadable_lines == 3


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
