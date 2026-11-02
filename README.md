# spend-brakes

Ready-to-copy spend brakes for scripts and AI agents that call paid APIs (image generation, LLMs, scraping, email).

Python package: standard library only. TypeScript: one file, no dependencies. MIT licence.

## The problem

- A paid API charges for every call.
- A loop, a bad retry or an agent that does not stop can make thousands of calls before you look.
- The bill comes later. These brakes stop the calls first.

**Important:** if an agent can change the code, the environment, the stop file or the ledger, it can turn off every brake. No setting can raise the two ceilings in the code, but an agent that can edit the code can change them, and a new job name avoids the job ceiling. So also keep a quota at the provider that the agent cannot change (see "An extra layer" below).

## The five brakes

Every brake is checked before every attempt, retries included. A stop is an error that names the brake and the setting. The owner decides whether to change the setting.

| # | Brake | What it does | Setting |
|---|-------|--------------|---------|
| 1 | **Kill switch** | One master switch stops all paid calls. One switch per provider stops that provider only. A stop file also reaches programs that are already running. By default the stop file is a file named `STOP` in the same folder as the ledger file. | `SPEND_PANIC`, `SPEND_<PROVIDER>_ENABLED`, `SPEND_STOP_FILE` (only to use another file) |
| 2 | **Monthly cap** | One money total for all your scripts together, taken from one shared ledger. | `SPEND_MONTHLY_CAP_USD` (default 20, an example number) |
| 3 | **Project cap** | A smaller money limit for one project. | `SPEND_PROJECT_CAP_USD_<PROJECT>` |
| 4 | **Job limit** | A limit on the number of calls in one job (default 6). A hard ceiling (20) is written in the code. No argument and no environment variable can raise it; only a change in the code can. | `SPEND_MAX_CALLS_PER_JOB`, up to the ceiling |
| 5 | **Safe retries** | A refusal (401, 402, 403, 429) is never repeated. A server error (5xx) or a timeout is repeated a few times at most (default 1, ceiling 3). | `max_retries`, up to the ceiling of 3. It is an argument of `guarded_call`, not an environment setting. |

Behind the brakes is the **ledger**: one append-only file (JSON Lines) that every script writes to. The estimated cost is written *before* the call. The real cost, or 0 if the provider refused the call, is written *after*. If a program crashes in the middle, the estimate stays and keeps counting.

### A job is one run

The job limit counts every call ever recorded under the same project and job name. There is no time window. So **a job name must be new for each run of your script.** If you use the same name every day, the old calls still count, and one day the limit is reached permanently. `job` is required: there is no default name.

Use a run id. `run_id()` gives a new one each time:

```python
job = "batch-" + run_id()      # for example batch-<date and time>-<six letters or digits>
```

Build the job name **once, before the loop**, and use that same name for every call of the run. Do not call `run_id()` inside the loop. A new name for each call defeats the job limit, because every call then starts a new count at zero.

Every attempt counts for the job limit, retries included. A job limit of 1 therefore leaves no room for a retry: the second attempt is stopped with `JobLimitReached`. Set the limit to the number of calls you plan, plus the retries you accept.

## Quick start (Python)

Copy `src/spend_brakes` into your project, or run `pip install .` in this folder. Python 3.9 or newer.

```python
import os
from spend_brakes import guarded_call, run_id
from spend_brakes.httpjson import post_json

job = "batch-" + run_id()             # ONCE for each run, before the loop

for prompt in ["a red cube", "a blue cube"]:
    result = guarded_call(
        lambda: post_json(os.environ["IMAGE_API_URL"], {"prompt": prompt}, key_env="IMAGE_API_KEY"),
        estimate_usd=0.05,            # what one call may cost, worst case
        project="blog-images",        # for the project cap
        provider="image_api",         # for the provider switch: SPEND_IMAGE_API_ENABLED
        job=job,                      # the same name for every call of this run
    )
```

Your function must raise an error on an HTTP failure, or return an object with a `.status_code` or `.status`. `post_json` (included) does the first. If you use another HTTP client, call its "raise for status" method inside the function.

If your client, or a function of yours, returns `(status, body)`, raise `HttpStatusError` for a bad status. The guard then treats a 401 as a refusal (never repeated) and a 503 as a server error (repeated at most `max_retries` times):

```python
from spend_brakes import HttpStatusError, guarded_call

def call_api():
    status, body = my_client.post(url, data)              # any client that gives (status, body)
    if status >= 300:
        raise HttpStatusError(status, str(body)[:300])    # keep the text short; never put the key in it
    return body

result = guarded_call(call_api, estimate_usd=0.05, project="blog-images", provider="image_api", job=job)
```

In TypeScript no helper is needed: the guard reads `.status` from what your function returns, and from an error it throws.

`post_json` is careful with the key. It sends the key in a header only. It does not follow redirects (a 3xx answer is an `HttpStatusError`), because a redirect would send the key to a second address. It accepts only `https` URLs, and `http` only for the local machine (`localhost`, `127.0.0.1`, `::1`). It uses a proxy from the environment (`https_proxy`) for `https` only, so the connection to the API stays encrypted from end to end. It never uses a proxy for `http`, so a key is never sent to a proxy in clear text. The variables `http_proxy` and `all_proxy` are ignored. It refuses `file:` and every other scheme, and URLs that contain a user name or password.

If the key is not in `os.environ` (for example you read it from a secrets store), give `post_json` an `env=` mapping. It reads `key_env` from that mapping instead of `os.environ`:

```python
post_json(url, payload, key_env="IMAGE_API_KEY", env={"IMAGE_API_KEY": key_from_your_store})
```

`env=` of `post_json` is used for the key only. It is not the `env=` of `guarded_call`, which holds the settings.

To see every brake without a network or a key, run `python examples/offline_demo.py`.

When a brake stops a call, you see this (shortened):

```
Stopped by the monthly cap: all projects spent $19.97 this month, and this call is about $0.05,
which would exceed the cap of $20.00. Ask the owner to raise it (setting: SPEND_MONTHLY_CAP_USD).
```

Errors you can catch: `BrakeError` (all stops) and its types `KillSwitchOn`, `MonthlyCapReached`, `ProjectCapReached`, `JobLimitReached`, `HardCeilingExceeded`, `NotRetried`, `RetriesExhausted`, `LedgerUnreadable`. Each has `.brake` with the short name. A bad setting (for example a cap that is not a number, or `nan`) raises `ConfigError`. It is never ignored.

## Check without calling (for a dry run)

`check_call` answers one question: would `guarded_call` allow this call now? It takes the same keyword arguments as `guarded_call`, without the function. It runs the same brakes and raises the same `BrakeError`, with the same message. It writes nothing to the ledger, so it never uses up the job limit. Import it with `from spend_brakes import check_call`.

```python
check_call(estimate_usd=0.05, project="blog-images", provider="image_api", job=job)   # raises BrakeError if a real call would stop
print("The brakes would allow this call.")                                           # reached only when no brake stops it
```

It checks the first attempt only, and it does not call the provider. A real call can still be stopped later, for example when another script spends money in between.

In TypeScript, `checkCall` takes the same options as `guardedCall` and returns a promise:

```ts
await checkCall({ estimateUsd: 0.05, project: "blog-images", provider: "image_api", job, ledger });   // throws BrakeError if a real call would stop
console.log("The brakes would allow this call.");                                                   // reached only when no brake stops it
```

## If the ledger file is damaged

If any line in the ledger cannot be read, the real spend is not known. All calls then stop with `LedgerUnreadable`, and nothing is added to the file. A damaged line can be a cut-off row after a crash, text written by mistake, or a row with a missing or invalid field.

To repair it, the owner makes a copy of the file, finds the line numbers named in the error, and fixes or removes only those lines. `Ledger.unreadable_line_numbers()` lists them, and `Ledger.rows()` still shows the readable rows.

Skipping unreadable lines is possible, but it is an explicit choice and not a repair: the money on those lines is then not counted. The owner sets `SPEND_BRAKES_SKIP_UNREADABLE=1` (or `Ledger(skip_unreadable=True)`), and a warning is shown each time.

Writes are guarded against cut-off rows. A row is written with one call, and the rest is written if the system writes only part of it. If an earlier write was cut off, the next row starts on a new line, so the next row is never hidden inside the damaged line.

## Settings

Put these in your shell profile or your deploy settings. Every script then sees the same numbers.

```bash
export SPEND_BRAKES_LEDGER=/path/to/spend-ledger.jsonl   # one file for all scripts (default: a folder named .spend-brakes in your home folder)
export SPEND_MONTHLY_CAP_USD=20
export SPEND_PROJECT_CAP_USD_BLOG_IMAGES=5               # the project name "blog-images" becomes BLOG_IMAGES
export SPEND_MAX_CALLS_PER_JOB=6                         # 20 is the highest value that works
export SPEND_STOP_FILE=/path/to/STOP                     # only to use another stop file (default: a file named STOP in the same folder as the ledger file)
export SPEND_BRAKES_SKIP_UNREADABLE=0                    # 1 = skip unreadable ledger lines (not recommended)

export SPEND_PANIC=1                                     # stop every paid call (new programs)
export SPEND_IMAGE_API_ENABLED=0                         # stop one provider; the name comes from provider="image_api"
```

On Windows PowerShell, use `$env:SPEND_PANIC = "1"`.

Notes:

- Money can be in any currency. The names say `usd`, but use one currency everywhere.
- Months are UTC calendar months.
- An environment variable changes only programs that start after the change. To stop a program that is already running, create the stop file.
- **The stop file.** By default it is a file named `STOP` in the same folder as the ledger file. For the default ledger, create it with `mkdir -p ~/.spend-brakes && touch ~/.spend-brakes/STOP`. While the file exists, every paid call stops, also in programs that are already running. Delete the file to go on. `SPEND_STOP_FILE` replaces the default: when it is set, only that file counts. A ledger that is not a file has no default stop file (the TypeScript `memoryLedger()`, or a ledger you write over a database).
- On macOS and Linux, the Python ledger creates a `.lock` file next to the ledger file (for example `ledger.jsonl.lock`) when it first writes. It is normal, and it may stay.
- The ledger knows only the spend that was recorded through it. Spend from before you started, or from other tools, is not in it. In the first month, set `SPEND_MONTHLY_CAP_USD` lower than your real monthly limit, to allow for that earlier spend.
- A switch value that cannot be read (for example `disabled`) counts as STOP, not as GO. A number that cannot be read (`abc`, `nan`, `inf`) is a `ConfigError`.
- The API key goes into an environment variable (a name you choose, for example `IMAGE_API_KEY`). It is never a command argument, never in the URL, and it is removed from error text. Do not write the key in a shell command: other users of the machine can see command lines, and your shell history keeps them. If you must use `curl`, give the header through `curl -K -` and standard input.

## How to choose the estimate

The caps are checked against your estimate, not against the provider's real bill. Use the **worst case** for one call. If the real cost is often lower, pass `cost_from_result=` to record the real cost after the call. If the real cost can be higher than your estimate, the caps can be exceeded by the difference.

A failed attempt keeps its estimate in the ledger, because a timeout or a server error may still be billed. A refusal (401, 402, 403, 429) is recorded as 0, but it still counts as a call for the job limit.

## TypeScript (Node, serverless)

`typescript/spend-brakes.ts` is one file. It has the same brakes, the same messages, the same setting names and the same ledger format, so a Node program and a Python script may share one ledger file. They must not write to it at the same moment: Node takes no file lock.

```ts
import { fileLedger, guardedCall, runId } from "./spend-brakes.js";

const ledger = fileLedger(process.env.SPEND_BRAKES_LEDGER ?? "./spend-ledger.jsonl");

const job = `batch-${runId()}`;       // ONCE for each run, before the loop

for (const body of bodies) {
  const result = await guardedCall(() => fetch(url, { method: "POST", headers, body, redirect: "manual" }), {
    estimateUsd: 0.05, project: "blog-images", provider: "image_api", job, ledger,
  });
}
```

`job` is required. Build it once for each run, before the loop (see "A job is one run"). `fetch` does not throw on HTTP errors, so the guard also reads `.status` from the returned response. Use `redirect: "manual"` so a redirect is not followed with your key attached; it then comes back as a 3xx response for you to handle.

- `memoryLedger()` keeps rows in memory (tests, one process). It has no default stop file.
- `fileLedger(path)` writes the file (you give the path; there is no default). It is safe inside one process. Node takes no file lock (it has none without extra packages), so two processes that write at the same moment can exceed the same cap by a small margin, and this includes a Node program and a Python script that share the file. It refuses to work with an unreadable line, like the Python ledger; `fileLedger(path, { skipUnreadable: true })` is the explicit opt-in. A file named `STOP` in the same folder is its default stop file.
- Serverless functions have no shared disk. Write a small `Ledger` over a database (three methods: `reserve`, `settle` and `preview`). `reserve` must read the totals, run the check and write the estimate as one atomic step. `preview` does the same without the write: it reads the totals and runs the check. `checkCall` uses it. A ledger may also set `stopFile`, a path that is its default stop file.
- An `AbortError` (you cancelled the request) is never retried. A `TimeoutError` is.

## What it does NOT protect against

- **A leaked key.** Anyone who has your key can spend outside your scripts, and no ledger will show it. Use one key per project, restrict it at the provider, and rotate it if it may have leaked.
- **A provider that bills without telling you.** The ledger holds your estimates, not the provider's invoice. If the provider charges for failed calls, changes prices, or adds fees, the ledger will not know. Compare the ledger with the real invoice from time to time.
- **Costs outside your scripts.** Other tools, a web console, a colleague's script that does not use the brakes, and another machine with its own ledger file are not counted.
- **Code that does not call the guard.** A brake works only on calls that go through `guarded_call`. Review new code for direct calls.
- **An agent or a person who can change the brakes.** Anyone who can edit the code, the environment, the stop file or the ledger can turn off every brake. This is protection against mistakes and loops that do not stop, not a security boundary. Keep a quota at the provider that they cannot change.
- **A new job name for each call.** A loop that makes up a new `job` for each call defeats the job limit: every call then starts a new count at zero, and the job ceiling is avoided too. Build the job name once, before the loop. The monthly cap and the project cap still hold.
- **Windows and network file systems.** The ledger lock uses `fcntl` (macOS, Linux). On other systems it works inside one process only, and the tests that need it are skipped. `flock` may also not work on network file systems (NFS): do not share one ledger file between machines through NFS. Use a local file, or write a `Ledger` over a database.

## An extra layer: a tripwire at the billing level

These brakes live in your own code. Keep a final protection outside your code, at the billing level:

1. **Quotas at the provider.** Set a per-minute and per-day request limit on the API itself. It works in real time and stops a fast loop at once. Keep the key that changes this limit away from your scripts and agents.
2. **A budget alert with an action.** Most cloud and API accounts can send a message when spend passes a limit. A budget alone only warns. Connect the alert to a small function that acts: it revokes the key, or turns off billing for the project.
3. **Know the delay.** Usage reports can be hours late. The tripwire is the final protection for what the code brakes miss. It is not a replacement for them.
4. **Test it.** Set a very low budget, make one small call, and check that the function runs. Then set the real budget. Write down who turns the service back on, and how.

## Tests

```bash
pip install pytest && pytest               # Python tests, including real HTTP calls to a fake local server
cd typescript && npm install && npm test   # TypeScript tests with node:test (Node 20 or newer)
```

The tests try to break the brakes: cap reached, kill switch on, the hard ceiling cannot be raised, a 402 is not retried, a redirect does not carry the key, a damaged ledger stops the calls, and the ledger survives many writers at the same time (threads and separate processes). They make no paid call. The only network used is a fake server on the local machine.

## Licence

MIT. See `LICENSE`.
