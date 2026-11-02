"""The ledger: one append-only JSONL file that every script writes to.

Each paid call writes two rows with the same id:
  reserve  written BEFORE the call, with the estimated cost (so a crash still leaves the estimate behind)
  settle   written AFTER the call, with the real cost, or 0 when the provider refused the call

The cost of a call is the usd of its last row. Rows are never edited or deleted by this package.
The check against the caps and the reserve row are written under one lock, so two processes cannot both
pass the same cap. The lock uses fcntl (macOS, Linux). Without fcntl the lock works inside one process only.
Months are UTC calendar months.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import math
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from .caps import check_money
from .errors import ConfigError

try:  # POSIX only
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

ENV_LEDGER = "SPEND_BRAKES_LEDGER"
MAX_NAME_LENGTH = 200  # keeps every row short enough to be written in one piece
_THREAD_LOCK = threading.Lock()


@dataclass(frozen=True)
class Totals:
    month_usd: float  # all projects, this month
    project_usd: float  # this project, this month
    job_calls: int  # calls ever recorded for this project + job (refused calls count too)
    unreadable_lines: int  # lines that could not be read (they are skipped)


def default_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    custom = (env.get(ENV_LEDGER) or "").strip()
    return Path(custom) if custom else Path.home() / ".spend-brakes" / "ledger.jsonl"


class Ledger:
    def __init__(self, path: Optional[os.PathLike] = None, clock: Optional[Callable[[], _dt.datetime]] = None):
        self.path = Path(path) if path is not None else default_path()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Ledger":
        return cls(default_path(env))

    # ---- writing -------------------------------------------------------------------------------------------

    def reserve(self, *, project: str, provider: str, job: str, usd: float,
                check: Callable[[Totals], None]) -> str:
        """Under the lock: read the totals, let `check` refuse (it raises), then write the reserve row."""
        for label, value in (("project", project), ("provider", provider), ("job", job)):
            if not value or len(value) > MAX_NAME_LENGTH:
                raise ConfigError(f"{label} must be a name of 1 to {MAX_NAME_LENGTH} characters.")
        check_money(usd, "estimate_usd")
        with self._locked():
            rows, bad = self._read_rows()
            check(self._totals(rows, bad, project, job))
            call_id = uuid.uuid4().hex
            self._append({"event": "reserve", "id": call_id, "project": project, "provider": provider,
                          "job": job, "usd": usd})
            return call_id

    def settle(self, call_id: str, *, project: str, provider: str, job: str, usd: float, outcome: str) -> None:
        """Write the final cost of a call. `outcome` is ok, rejected or error."""
        check_money(usd, "usd")
        self._append({"event": "settle", "id": call_id, "project": project, "provider": provider,
                      "job": job, "usd": usd, "outcome": outcome})

    # ---- reading -------------------------------------------------------------------------------------------

    def totals(self, project: str, job: str = "") -> Totals:
        rows, bad = self._read_rows()
        return self._totals(rows, bad, project, job)

    def month_total(self) -> float:
        return self.totals("").month_usd

    def project_total(self, project: str) -> float:
        return self.totals(project).project_usd

    def job_calls(self, project: str, job: str) -> int:
        return self.totals(project, job).job_calls

    def rows(self) -> List[dict]:
        return self._read_rows()[0]

    # ---- inside -------------------------------------------------------------------------------------------

    def _append(self, row: dict) -> None:
        row = {"t": self._clock().isoformat(timespec="seconds"), **row}
        data = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.write(fd, data) != len(data):  # one write call: the system appends the whole line or none of it
                raise OSError("The ledger row was only partly written. Check the disk and the ledger file.")
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with _THREAD_LOCK:
            if fcntl is None:  # pragma: no cover
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)  # closing the file also releases the lock

    def _read_rows(self) -> Tuple[List[dict], int]:
        rows: List[dict] = []
        bad = 0
        try:
            handle = open(self.path, "rb")
        except FileNotFoundError:
            return rows, bad
        with handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    bad += 1
                    continue
                if _row_ok(row):
                    rows.append(row)
                else:
                    bad += 1
        return rows, bad

    def _totals(self, rows: List[dict], bad: int, project: str, job: str) -> Totals:
        month = self._clock().strftime("%Y-%m")
        calls: Dict[str, dict] = {}
        for row in rows:
            call = calls.get(row["id"])
            if call is None:
                call = calls[row["id"]] = {"month": str(row.get("t", ""))[:7], "project": row.get("project"),
                                           "job": row.get("job"), "usd": row["usd"]}
            if row["event"] == "settle":
                call["usd"] = row["usd"]
        month_usd = project_usd = 0.0
        job_calls = 0
        for call in calls.values():
            if call["project"] == project and call["job"] == job:
                job_calls += 1
            if call["month"] == month:
                month_usd += call["usd"]
                if call["project"] == project:
                    project_usd += call["usd"]
        return Totals(round(month_usd, 6), round(project_usd, 6), job_calls, bad)


def _row_ok(row: object) -> bool:
    if not isinstance(row, dict) or row.get("event") not in ("reserve", "settle") or not isinstance(row.get("id"), str):
        return False
    usd = row.get("usd")
    return isinstance(usd, (int, float)) and not isinstance(usd, bool) and math.isfinite(usd) and usd >= 0
