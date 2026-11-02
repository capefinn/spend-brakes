"""Shows what each brake says. Uses a fake function: no network, no key, no cost.

Run from the repository folder:   python examples/offline_demo.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # not needed once the package is installed

from spend_brakes import BrakeError, Caps, Ledger, guarded_call  # noqa: E402
from spend_brakes.httpjson import HttpStatusError  # noqa: E402

ledger = Ledger(Path(tempfile.mkdtemp()) / "demo-ledger.jsonl")


def try_call(title, fn, env=None, caps=None, **options):
    options.setdefault("estimate_usd", 0.10)
    try:
        guarded_call(fn, project="demo", provider="image_api", job=title, ledger=ledger, env=env or {},
                     caps=caps or Caps(monthly_usd=1.00), sleep=lambda seconds: None, **options)
        print(f"[{title}] call allowed")
    except BrakeError as stop:
        print(f"[{title}] STOPPED by {stop.brake}:\n    {stop}\n")


def fail_with(status):
    def fn():
        raise HttpStatusError(status)
    return fn


try_call("normal", lambda: "ok")
try_call("kill-switch", lambda: "ok", env={"SPEND_PANIC": "1"})
try_call("payment-error", fail_with(402))
try_call("server-error", fail_with(503))
try_call("too-big", lambda: "ok", estimate_usd=5.00)
try:
    Caps(max_calls_per_job=500)
except BrakeError as stop:
    print(f"[hard-ceiling] STOPPED by {stop.brake}:\n    {stop}\n")
print(f"Month total in the demo ledger: ${ledger.month_total():.2f}")
