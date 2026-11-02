# spend-brakes

Ready-to-copy spend brakes for scripts and AI agents that call paid APIs (image generation, LLMs, scraping, email).

Python package: standard library only. TypeScript: one file, no dependencies. MIT licence.

## The problem

- A paid API charges for every call.
- A loop, a bad retry or an agent that does not stop can make thousands of calls before you look.
- The bill comes later. These brakes stop the calls first.

## The five brakes

Every brake is checked before every attempt, retries included. A stop is an error that names the brake and tells you how to change it on purpose.

| # | Brake | What it does | How you change it on purpose |
|---|-------|--------------|------------------------------|
| 1 | **Kill switch** | One master switch stops all paid calls. One switch per provider stops that provider only. A stop file also reaches programs that are already running. | `SPEND_PANIC`, `SPEND_<PROVIDER>_ENABLED`, `SPEND_STOP_FILE` |
| 2 | **Monthly cap** | One money total for all your scripts together, taken from one shared ledger. | `SPEND_MONTHLY_CAP_USD` (default 20, an example number) |
| 3 | **Project cap** | A smaller money limit for one project. | `SPEND_PROJECT_CAP_USD_<PROJECT>` |
| 4 | **Job limit** | A limit on the number of calls in one job (default 6). A hard ceiling (20) is written in the code. No argument and no environment variable can raise it. | `SPEND_MAX_CALLS_PER_JOB`, up to the ceiling |
| 5 | **Safe retries** | A refusal (401, 402, 403, 429) is never repeated. A server error (5xx) or a timeout is repeated a few times at most (default 1, ceiling 3). | `max_retries`, up to the ceiling |

Behind the brakes is the **ledger**: one append-only file (JSON Lines) that every script writes to. The estimated cost is written *before* the call. The real cost, or 0 if the provider refused the call, is written *after*. If a program crashes in the middle, the estimate stays and keeps counting.

## Quick start (Python)

Copy `src/spend_brakes` into your project, or run `pip install .` in this folder. Python 3.9 or newer.

```python
import os
from spend_brakes import guarded_call
from spend_brakes.httpjson import post_json

result = guarded_call(
    lambda: post_json(os.environ["IMAGE_API_URL"], {"prompt": "a red cube"}, key_env="IMAGE_API_KEY"),
    estimate_usd=0.05,        # what one call may cost, worst case
    project="blog-images",    # for the project cap
    provider="image_api",     # for the provider switch: SPEND_IMAGE_API_ENABLED
    job="batch-1",            # for the job limit
)
```

Your function must raise an error on an HTTP failure, or return an object with a `.status_code` or `.status`. `post_json` (included) does the first. If you use another HTTP client, call its "raise for status" method inside the function.

To see every brake without a network or a key, run `python examples/offline_demo.py`.

When a brake stops a call, you see this (shortened):

```
Stopped by the monthly cap: all projects spent $19.97 this month, and this call is about $0.05,
which would pass the cap of $20.00. To raise it on purpose: set SPEND_MONTHLY_CAP_USD or Caps(monthly_usd=...).
```

Errors you can catch: `BrakeError` (all stops) and its types `KillSwitchOn`, `MonthlyCapReached`, `ProjectCapReached`, `JobLimitReached`, `HardCeilingExceeded`, `NotRetried`, `RetriesExhausted`. Each has `.brake` with the short name. A bad setting (for example a cap that is not a number) raises `ConfigError`. It is never ignored.

## Settings

Put these in your shell profile or your deploy settings. Every script then sees the same numbers.

```bash
export SPEND_BRAKES_LEDGER=/path/to/spend-ledger.jsonl   # one file for all scripts (default: a folder named .spend-brakes in your home folder)
export SPEND_MONTHLY_CAP_USD=20
export SPEND_PROJECT_CAP_USD_BLOG_IMAGES=5               # the project name "blog-images" becomes BLOG_IMAGES
export SPEND_MAX_CALLS_PER_JOB=6                         # 20 is the highest value that works
export SPEND_STOP_FILE=/path/to/STOP                     # create this file to stop everything, even running programs

export SPEND_PANIC=1                                     # stop every paid call (new programs)
export SPEND_IMAGE_API_ENABLED=0                         # stop one provider; the name comes from provider="image_api"
```

On Windows PowerShell, use `$env:SPEND_PANIC = "1"`.

Notes:

- Money can be in any currency. The names say `usd`, but use one currency everywhere.
- Months are UTC calendar months.
- An environment variable changes only programs that start after the change. To stop a program that is already running, create the stop file.
- A switch value that cannot be read (for example `disabled`) counts as STOP, not as GO.
- The API key goes into an environment variable (a name you choose, for example `IMAGE_API_KEY`). `post_json` sends it in a header. It is never a command argument, never in the URL, and it is removed from error text. Do not write the key in a shell command: other users of the machine can see command lines, and your shell history keeps them. If you must use `curl`, give the header through `curl -K -` and standard input.

## How to choose the estimate

The caps are checked against your estimate, not against the provider's real bill. Use the **worst case** for one call. If the real cost is often lower, pass `cost_from_result=` to record the real cost after the call. If the real cost can be higher than your estimate, the caps can be passed by the difference.

A failed attempt keeps its estimate in the ledger, because a timeout or a server error may still be billed. A refusal (401, 402, 403, 429) is recorded as 0, but it still counts as a call for the job limit.

## TypeScript (Node, serverless)

`typescript/spend-brakes.ts` is one file. It has the same brakes, the same messages, the same setting names and the same ledger format, so a Node program and a Python script can share one ledger file.

```ts
import { fileLedger, guardedCall } from "./spend-brakes.js";

const ledger = fileLedger(process.env.SPEND_BRAKES_LEDGER ?? "./spend-ledger.jsonl");

const result = await guardedCall(() => fetch(url, { method: "POST", headers, body }), {
  estimateUsd: 0.05, project: "blog-images", provider: "image_api", job: "batch-1", ledger,
});
```

`fetch` does not throw on HTTP errors, so the guard also reads `.status` from the returned response.

- `memoryLedger()` keeps rows in memory (tests, one process).
- `fileLedger(path)` writes the file (you give the path; there is no default). It is safe inside one process. Several processes at the same time can pass the same cap by a small margin, because Node has no file lock without extra packages.
- Serverless functions have no shared disk. Write a small `Ledger` over a database (two methods: `reserve` and `settle`). `reserve` must read the totals, run the check and write the estimate as one atomic step.

## What it does NOT protect against

- **A leaked key.** Anyone who has your key can spend outside your scripts, and no ledger will show it. Use one key per project, restrict it at the provider, and rotate it if it may have leaked.
- **A provider that bills without telling you.** The ledger holds your estimates, not the provider's invoice. If the provider charges for failed calls, changes prices, or adds fees, the ledger will not know. Compare the ledger with the real invoice from time to time.
- **Costs outside your scripts.** Other tools, a web console, a colleague's script that does not use the brakes, and another machine with its own ledger file are not counted.
- **Code that does not call the guard.** A brake works only on calls that go through `guarded_call`. Review new code for direct calls.
- **A new job name for every call.** A loop that makes up a new `job` each time goes around the job limit. The monthly cap and the project cap still hold.
- **People with access to your machine.** Anyone who can edit the ledger file, the environment or the code can remove the brakes. This is a safety net for mistakes, not a security boundary.
- **Windows file locking.** The ledger lock uses `fcntl` (macOS, Linux). On other systems it works inside one process only, and the tests that need it are skipped.

## An extra layer: a tripwire at the billing level

These brakes live in your own code. Keep a last line of defence outside your code, at the billing level:

1. **Quotas at the provider.** Set a per-minute and per-day request limit on the API itself. It works in real time and stops a fast loop at once.
2. **A budget alert with an action.** Most cloud and API accounts can send a message when spend passes a limit. A budget alone only warns. Connect the alert to a small function that acts: it revokes the key, or turns off billing for the project.
3. **Know the delay.** Usage reports can be hours late. The tripwire is a backstop for what the code brakes miss. It is not a replacement for them.
4. **Test it.** Set a very low budget, make one small call, and check that the function fires. Then set the real budget. Write down who turns the service back on, and how.

## Tests

```bash
pip install pytest && pytest               # Python tests, including real HTTP calls to a fake local server
cd typescript && npm install && npm test   # TypeScript tests with node:test (Node 20 or newer)
```

The tests include break checks: cap reached, kill switch on, the hard ceiling cannot be raised, a 402 is not retried, and the ledger survives many writers at the same time (threads and separate processes). They make no paid call. The only network used is a fake server on the local machine.

## Licence

MIT. See `LICENSE`.
