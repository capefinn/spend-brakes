"""The ledger: one append-only JSONL file that every script writes to.

Each paid call writes two rows with the same id:
  reserve  written BEFORE the call, with the estimated cost (so a crash still leaves the estimate behind)
  settle   written AFTER the call, with the real cost, or 0 when the provider refused the call

The cost of a call is the usd of its last row. Rows are never edited or deleted by this package.
Rows keep the project name exactly as it was given. Totals count a project by its key (project_key in caps.py, the
same key as in the setting SPEND_PROJECT_CAP_USD_<KEY>), so "blog-images" and "Blog_Images" are one project, and so are
"Kuvat äänet" written with one character or with two for each ä.
The check against the caps and the reserve row are written under one lock, so two processes cannot both
exceed the same cap. The lock uses fcntl (macOS, Linux; it may not work on network file systems).
Without fcntl the lock works inside one process only. Months are UTC calendar months.

A line that cannot be read means the real spend is not known. By default the ledger then REFUSES to give totals,
so no call is allowed until the owner repairs the file. Skipping unreadable lines is an explicit opt-in
(skip_unreadable=True, or SPEND_BRAKES_SKIP_UNREADABLE=1). An explicit skip_unreadable argument wins over the setting.
The warning about skipped lines is shown once per program for each ledger file.

Writes are guarded against cut-off rows: a row is written with one call (the rest is completed if the system writes
only part of it), and if an earlier write left a line without its end-of-line mark, the next row starts on a new line,
so a damaged line never hides a good one. The damaged line itself is then reported as unreadable.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import math
import os
import re
import threading
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from .caps import check_money, check_names, project_key
from .errors import ConfigError, LedgerUnreadable
from .killswitch import STOP_FILE_NAME

try:  # POSIX only
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

ENV_LEDGER = "SPEND_BRAKES_LEDGER"
ENV_SKIP_UNREADABLE = "SPEND_BRAKES_SKIP_UNREADABLE"
_THREAD_LOCK = threading.Lock()
_WARN_LOCK = threading.Lock()
_WARNED_PATHS: set = set()  # ledger files for which the skip warning was shown already (once per program for each file)
_TIME_START = re.compile(r"^\d{4}-\d{2}-\d{2}T")


@dataclass(frozen=True)
class Totals:
    month_usd: float  # all projects, this month
    project_usd: float  # this project, this month (counted by project_key, so spellings of one name are one project)
    job_calls: int  # calls recorded for this project + job (refused calls count too; same project_key rule)
    unreadable_lines: int  # lines skipped because of skip_unreadable=True (always 0 otherwise)


def default_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    custom = (env.get(ENV_LEDGER) or "").strip()
    return Path(custom) if custom else Path.home() / ".spend-brakes" / "ledger.jsonl"


def _skip_from_env(env: Mapping[str, str]) -> bool:
    raw = (env.get(ENV_SKIP_UNREADABLE) or "").strip().lower()
    if raw in ("", "0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    raise ConfigError(f"{ENV_SKIP_UNREADABLE}={raw!r} cannot be read. Use 1 or 0.")


def run_id() -> str:
    """A new id for every run of a script, to use inside a job name: job="batch-" + run_id()."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


class Ledger:
    def __init__(self, path: Optional[os.PathLike] = None, clock: Optional[Callable[[], _dt.datetime]] = None,
                 skip_unreadable: Optional[bool] = None):
        self.path = Path(path) if path is not None else default_path()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        # An explicit argument wins. Without one, SPEND_BRAKES_SKIP_UNREADABLE decides (an unknown value is a ConfigError).
        self.skip_unreadable = _skip_from_env(os.environ) if skip_unreadable is None else bool(skip_unreadable)
        self._clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Ledger":
        env = os.environ if env is None else env
        return cls(default_path(env), skip_unreadable=_skip_from_env(env))

    @property
    def stop_file(self) -> Path:
        """The default stop file: a file named STOP in the same folder as the ledger file."""
        return self.path.parent / STOP_FILE_NAME

    # ---- writing -------------------------------------------------------------------------------------------

    def reserve(self, *, project: str, provider: str, job: str, usd: float,
                check: Callable[[Totals], None]) -> str:
        """Under the lock: read the totals, let `check` refuse (it raises), then write the reserve row."""
        check_names(project, provider, job)
        usd = check_money(usd, "estimate_usd")  # the plain float is what is checked and what is written
        with self._locked():
            rows, bad = self._read_rows()
            self._require_readable(bad)
            check(self._totals(rows, len(bad), project, job))
            call_id = uuid.uuid4().hex
            self._append({"event": "reserve", "id": call_id, "project": project, "provider": provider,
                          "job": job, "usd": usd})
            return call_id

    def preview(self, *, project: str, provider: str, job: str, usd: float, check: Callable[[Totals], None]) -> None:
        """The read-only twin of `reserve`: the same name check, the same rule for unreadable lines and the same `check`
        (it raises to refuse), but nothing is written. No lock is taken, and no file or folder is made."""
        check_names(project, provider, job)
        usd = check_money(usd, "estimate_usd")
        rows, bad = self._read_rows()
        self._require_readable(bad)
        check(self._totals(rows, len(bad), project, job))

    def settle(self, call_id: str, *, project: str, provider: str, job: str, usd: float, outcome: str) -> None:
        """Write the final cost of a call. `outcome` is ok, rejected or error."""
        usd = check_money(usd, "usd")
        with self._locked():
            self._append({"event": "settle", "id": call_id, "project": project, "provider": provider,
                          "job": job, "usd": usd, "outcome": outcome})

    # ---- reading -------------------------------------------------------------------------------------------

    def totals(self, project: str, job: str = "") -> Totals:
        """Raises LedgerUnreadable when a line cannot be read (unless skip_unreadable is on)."""
        rows, bad = self._read_rows()
        self._require_readable(bad)
        return self._totals(rows, len(bad), project, job)

    def month_total(self) -> float:
        return self.totals("").month_usd

    def project_total(self, project: str) -> float:
        return self.totals(project).project_usd

    def job_calls(self, project: str, job: str) -> int:
        return self.totals(project, job).job_calls

    def rows(self) -> List[dict]:
        """The readable rows. Never raises for a damaged file, so it can be used to look at one."""
        return self._read_rows()[0]

    def unreadable_line_numbers(self) -> List[int]:
        """Line numbers (first line is 1) that cannot be read. Empty when the file is whole."""
        return self._read_rows()[1]

    # ---- inside -------------------------------------------------------------------------------------------

    def _append(self, row: dict) -> None:
        """Add one row. Call it only while holding the lock."""
        row = {"t": self._clock().isoformat(timespec="seconds"), **row}
        data = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            size = os.fstat(fd).st_size
            if size:
                os.lseek(fd, size - 1, os.SEEK_SET)
                if os.read(fd, 1) != b"\n":  # an earlier write was cut off: end that line, then write ours
                    data = b"\n" + data
            view = memoryview(data)
            while view:  # the system may write only part of it; the lock keeps other writers out meanwhile
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("The ledger row could not be written. Check the disk and the ledger file.")
                view = view[written:]
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

    def _read_rows(self) -> Tuple[List[dict], List[int]]:
        rows: List[dict] = []
        bad: List[int] = []
        try:
            handle = open(self.path, "rb")
        except FileNotFoundError:
            return rows, bad
        with handle:
            for number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, RecursionError):
                    bad.append(number)
                    continue
                if _row_ok(row):
                    rows.append(row)
                else:
                    bad.append(number)
        return rows, bad

    def _require_readable(self, bad: List[int]) -> None:
        if not bad:
            return
        if self.skip_unreadable:
            key = os.path.abspath(self.path)
            with _WARN_LOCK:
                first_time = key not in _WARNED_PATHS
                _WARNED_PATHS.add(key)
            if first_time:  # once per program for each ledger file, even when the number of bad lines changes
                warnings.warn(f"spend_brakes: skipping {len(bad)} unreadable ledger line(s) in {self.path}; their spend is NOT counted.")
            return
        shown = ", ".join(str(n) for n in bad[:5]) + (" and more" if len(bad) > 5 else "")
        raise LedgerUnreadable(
            f"Stopped: the ledger file {self.path} has {len(bad)} line(s) that cannot be read (line {shown}). "
            "With an unreadable line the real spend is not known, so no paid call is allowed. "
            "To repair it, ask the owner to make a copy of the file, then fix or remove only the damaged line(s). "
            f"Skipping them is the owner's choice and not a repair: the money on those lines is then not counted "
            f"(setting: {ENV_SKIP_UNREADABLE})."
        )

    def _totals(self, rows: List[dict], skipped: int, project: str, job: str) -> Totals:
        month = self._clock().strftime("%Y-%m")
        calls: Dict[str, dict] = {}
        for row in rows:
            call = calls.get(row["id"])
            if call is None:
                call = calls[row["id"]] = {"month": row["t"][:7], "project": row["project"], "job": row["job"],
                                           "usd": row["usd"]}
            if row["event"] == "settle":
                call["usd"] = row["usd"]
        month_usd = project_usd = 0.0
        job_calls = 0
        wanted = project_key(project)  # rows keep the original text; the counting uses the cleaned-up name
        for call in calls.values():
            same_project = project_key(call["project"]) == wanted
            if same_project and call["job"] == job:
                job_calls += 1
            if call["month"] == month:
                month_usd += call["usd"]
                if same_project:
                    project_usd += call["usd"]
        return Totals(round(month_usd, 6), round(project_usd, 6), job_calls, skipped)


def _row_ok(row: object) -> bool:
    """A row we can count with: every field the totals use must be there and make sense."""
    if not isinstance(row, dict) or row.get("event") not in ("reserve", "settle"):
        return False
    if not isinstance(row.get("id"), str) or not isinstance(row.get("project"), str) or not isinstance(row.get("job"), str):
        return False
    if not isinstance(row.get("t"), str) or not _TIME_START.match(row["t"]):
        return False
    usd = row.get("usd")
    try:
        return isinstance(usd, (int, float)) and not isinstance(usd, bool) and math.isfinite(usd) and usd >= 0
    except OverflowError:  # a whole number too big to be turned into a float: the row cannot be counted
        return False
