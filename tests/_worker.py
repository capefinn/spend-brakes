"""Run by test_ledger.py in separate processes: try to reserve money against one shared cap."""
import sys

from spend_brakes import Ledger, MonthlyCapReached

path, attempts, cap, estimate = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
ledger = Ledger(path)


def check(totals):
    if round(totals.month_usd + estimate, 6) > cap:
        raise MonthlyCapReached("full")


won = 0
for _ in range(attempts):
    try:
        ledger.reserve(project="p", provider="x", job="j", usd=estimate, check=check)
        won += 1
    except MonthlyCapReached:
        pass
print(won)
