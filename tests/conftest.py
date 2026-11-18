import pytest

import spend_brakes.ledger as ledger_module
from spend_brakes import Caps, Ledger, guarded_call


@pytest.fixture(autouse=True)
def _isolated_ledger_settings(monkeypatch):
    """Every test starts with no skip setting in the environment, and with no ledger file marked as already warned about."""
    monkeypatch.delenv("SPEND_BRAKES_SKIP_UNREADABLE", raising=False)
    ledger_module._WARNED_PATHS.clear()


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.jsonl")


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def call(ledger, sleeps):
    """guarded_call with safe test defaults: own ledger, empty environment, no real sleeping."""

    def _call(fn, **overrides):
        settings = dict(
            estimate_usd=0.10, project="proj-a", provider="image_api", job="job-1",
            caps=Caps(monthly_usd=100.0, max_calls_per_job=20), ledger=ledger, env={}, sleep=sleeps.append,
        )
        settings.update(overrides)
        return guarded_call(fn, **settings)

    return _call
