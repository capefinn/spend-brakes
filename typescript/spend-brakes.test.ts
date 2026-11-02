import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { mock, test } from "node:test";
import { BrakeError, HARD_MAX_CALLS_PER_JOB, HARD_MAX_RETRIES, capsFromEnv, checkCall, checkKillSwitch, defaultLedgerPath, fileLedger, guardedCall, makeCaps, memoryLedger, projectKey, runId } from "./spend-brakes.js";
import type { Ledger } from "./spend-brakes.js";

// The warning about skipped ledger lines is shown once per program. Its own test runs in a separate process (see the end
// of this file); here it is kept out of the test output.
mock.method(console, "warn", () => {});

const noSleep = async () => {};
const base = { estimateUsd: 0.1, project: "proj-a", provider: "image_api", job: "job-1", env: {}, sleep: noSleep, caps: makeCaps({ monthlyUsd: 100 }) };
const brake = (name: string, text?: RegExp) => (e: unknown) => e instanceof BrakeError && e.brake === name && (!text || text.test(e.message));

/** A fake paid function. Each call plays the next outcome; the last one repeats. */
function script(...outcomes: unknown[]) {
  const fn = async () => { const o = outcomes[Math.min(fn.calls++, outcomes.length - 1)]; if (o instanceof Error) throw o; return o; };
  fn.calls = 0;
  return fn;
}
const httpError = (status: number) => Object.assign(new Error(`HTTP ${status}`), { status });
const tmpFile = () => join(mkdtempSync(join(tmpdir(), "sb-")), "ledger.jsonl");

test("the estimate is on record before the call, and the real cost replaces it", async () => {
  const ledger = memoryLedger();
  let during: string[] = [];
  const result = await guardedCall(async () => { during = ledger.rows.map((r) => r.event); return { cost: 0.03 }; }, { ...base, ledger, estimateUsd: 0.25, costFromResult: (r) => r.cost });
  assert.deepEqual(during, ["reserve"]);
  assert.equal(result.cost, 0.03);
  assert.deepEqual(ledger.rows.map((r) => [r.event, r.usd]), [["reserve", 0.25], ["settle", 0.03]]);
});

test("monthly cap: the call that would exceed the cap is stopped and never runs", async () => {
  const ledger = memoryLedger(), fn = script("ok"), caps = makeCaps({ monthlyUsd: 0.25 });
  await guardedCall(fn, { ...base, ledger, caps });
  await guardedCall(fn, { ...base, ledger, caps });
  await assert.rejects(guardedCall(fn, { ...base, ledger, caps }), brake("monthly_cap", /Ask the owner to raise it \(setting: SPEND_MONTHLY_CAP_USD\)/));
  assert.equal(fn.calls, 2);
});

test("a call that reaches exactly the cap is allowed, the next one is not", async () => {
  const ledger = memoryLedger(), fn = script("ok"), caps = makeCaps({ monthlyUsd: 0.3 });
  for (let i = 0; i < 3; i++) await guardedCall(fn, { ...base, ledger, caps });
  await assert.rejects(guardedCall(fn, { ...base, ledger, caps }), brake("monthly_cap"));
});

test("project cap stops one project and leaves the others", async () => {
  const ledger = memoryLedger(), caps = makeCaps({ monthlyUsd: 100, projectUsd: 0.2 });
  await guardedCall(script("ok"), { ...base, ledger, caps, project: "blog-images" });
  await guardedCall(script("ok"), { ...base, ledger, caps, project: "blog-images" });
  await assert.rejects(guardedCall(script("ok"), { ...base, ledger, caps, project: "blog-images" }), brake("project_cap", /SPEND_PROJECT_CAP_USD_BLOG_IMAGES/));
  await guardedCall(script("ok"), { ...base, ledger, project: "other" });
});

test("job limit stops a loop; refused calls count too; another job is unaffected", async () => {
  const ledger = memoryLedger(), caps = makeCaps({ monthlyUsd: 100, maxCallsPerJob: 3 }), refused = script(httpError(429));
  for (let i = 0; i < 3; i++) await assert.rejects(guardedCall(refused, { ...base, ledger, caps }), brake("no_retry"));
  await assert.rejects(guardedCall(refused, { ...base, ledger, caps }), brake("job_limit", /SPEND_MAX_CALLS_PER_JOB/));
  assert.equal(refused.calls, 3);
  await guardedCall(script("ok"), { ...base, ledger, caps, job: "another" });
});

test("kill switch: master, one provider, stop file, unreadable value, read again on every call", async () => {
  const ledger = memoryLedger(), fn = script("ok");
  for (const v of ["1", "true", "on", "yes"]) await assert.rejects(guardedCall(fn, { ...base, ledger, env: { SPEND_PANIC: v } }), brake("kill_switch", /SPEND_PANIC.*owner/));
  await assert.rejects(guardedCall(fn, { ...base, ledger, env: { SPEND_IMAGE_API_ENABLED: "0" } }), brake("kill_switch", /SPEND_IMAGE_API_ENABLED/));
  await assert.rejects(guardedCall(fn, { ...base, ledger, env: { SPEND_PANIC: "maybe" } }), brake("kill_switch", /cannot be read/));
  await assert.rejects(guardedCall(fn, { ...base, ledger, env: { SPEND_IMAGE_API_ENABLED: "disabled" } }), brake("kill_switch", /cannot be read/));
  assert.equal(fn.calls, 0);
  assert.equal(ledger.rows.length, 0); // a stopped call writes nothing
  assert.doesNotThrow(() => checkKillSwitch("llm_api", { SPEND_IMAGE_API_ENABLED: "0" })); // other providers go on
  assert.doesNotThrow(() => checkKillSwitch("image_api", { SPEND_PANIC: "0" }));

  const stopFile = tmpFile();
  const env = { SPEND_STOP_FILE: stopFile };
  await guardedCall(fn, { ...base, ledger, env });
  writeFileSync(stopFile, "");
  await assert.rejects(guardedCall(fn, { ...base, ledger, env }), brake("kill_switch", /owner removes that file/));
  rmSync(stopFile);
  await guardedCall(fn, { ...base, ledger, env });
  assert.equal(fn.calls, 2);
});

test("the kill switch also stops a retry", async () => {
  const env: Record<string, string> = {};
  const flaky = async () => { env.SPEND_PANIC = "1"; throw httpError(503); };
  await assert.rejects(guardedCall(flaky, { ...base, ledger: memoryLedger(), env, maxRetries: 3 }), brake("kill_switch"));
});

test("hard ceiling cannot be raised by an argument or by the environment", () => {
  assert.equal(HARD_MAX_CALLS_PER_JOB, 20);
  assert.doesNotThrow(() => makeCaps({ maxCallsPerJob: 20 }));
  assert.throws(() => makeCaps({ maxCallsPerJob: 21 }), brake("hard_ceiling", /HARD_MAX_CALLS_PER_JOB/));
  assert.throws(() => capsFromEnv("p", { SPEND_MAX_CALLS_PER_JOB: "999", SPEND_HARD_MAX_CALLS_PER_JOB: "999" }), brake("hard_ceiling"));
  assert.ok(Object.isFrozen(makeCaps()));
  assert.equal(HARD_MAX_RETRIES, 3);
});

test("hard ceiling covers retries too, and unreadable settings are refused", async () => {
  const fn = script("ok");
  await assert.rejects(guardedCall(fn, { ...base, ledger: memoryLedger(), maxRetries: 4 }), brake("hard_ceiling"));
  await assert.rejects(guardedCall(fn, { ...base, ledger: memoryLedger(), maxRetries: -1 }), brake("bad_setting"));
  for (const bad of [Number.NaN, Infinity, -1]) await assert.rejects(guardedCall(fn, { ...base, ledger: memoryLedger(), estimateUsd: bad }), brake("bad_setting"));
  assert.throws(() => capsFromEnv("p", { SPEND_MONTHLY_CAP_USD: "abc" }), brake("bad_setting"));
  for (const bad of ["nan", "NaN", "inf", "-inf", "1e400", "Infinity", "0x10", "2.5"]) {
    assert.throws(() => capsFromEnv("p", { SPEND_MAX_CALLS_PER_JOB: bad }), brake("bad_setting"), `SPEND_MAX_CALLS_PER_JOB=${bad}`);
  }
  assert.throws(() => capsFromEnv("p", { SPEND_MONTHLY_CAP_USD: "1e400" }), brake("bad_setting"));
  assert.throws(() => capsFromEnv("p", { SPEND_MAX_CALLS_PER_JOB: "1e300" }), brake("hard_ceiling"));
  assert.equal(fn.calls, 0);
});

for (const status of [401, 402, 403, 429]) {
  test(`${status} is not retried`, async () => {
    const ledger = memoryLedger(), fn = script(httpError(status));
    await assert.rejects(guardedCall(fn, { ...base, ledger, maxRetries: 3 }), (e: unknown) => brake("no_retry", new RegExp(String(status)))(e) && (e as BrakeError).status === status);
    assert.equal(fn.calls, 1);
    assert.equal(ledger.rows.at(-1)?.outcome, "rejected");
    assert.equal(ledger.rows.at(-1)?.usd, 0); // refused before any work: nothing billed
  });
}

test("a refusal on error.response, or on a returned response, is not retried either", async () => {
  const nested = script(Object.assign(new Error("x"), { response: { status: 402 } }));
  await assert.rejects(guardedCall(nested, { ...base, ledger: memoryLedger(), maxRetries: 3 }), brake("no_retry"));
  assert.equal(nested.calls, 1);
  const returned = script({ status: 402 }); // fetch does not throw on 402
  await assert.rejects(guardedCall(returned, { ...base, ledger: memoryLedger(), maxRetries: 3 }), brake("no_retry"));
  assert.equal(returned.calls, 1);
  const fine = { status: 200, body: "ok" };
  assert.equal(await guardedCall(script(fine), { ...base, ledger: memoryLedger() }), fine);
});

test("5xx and timeouts are retried a bounded number of times, and every attempt stays on record", async () => {
  const failures = [httpError(500), httpError(503), Object.assign(new Error("slow"), { name: "TimeoutError" }), Object.assign(new Error("x"), { code: "ECONNRESET" })];
  for (const failure of failures) {
    const ledger = memoryLedger(), fn = script(failure), sleeps: number[] = [];
    await assert.rejects(guardedCall(fn, { ...base, ledger, maxRetries: 2, sleep: async (ms) => { sleeps.push(ms); } }), brake("retries_exhausted", /maxRetries/));
    assert.equal(fn.calls, 3);
    assert.equal(sleeps.length, 2);
    assert.equal(ledger.rows.filter((r) => r.event === "reserve").length, 3);
  }
});

test("one retry is the default; 503 then a good answer works; a returned 503 is retried", async () => {
  const one = script(httpError(502));
  await assert.rejects(guardedCall(one, { ...base, ledger: memoryLedger() }), brake("retries_exhausted"));
  assert.equal(one.calls, 2);
  assert.equal(await guardedCall(script(httpError(503), "second time"), { ...base, ledger: memoryLedger(), maxRetries: 2 }), "second time");
  const returned = script({ status: 503 });
  await assert.rejects(guardedCall(returned, { ...base, ledger: memoryLedger(), maxRetries: 1 }), brake("retries_exhausted"));
  assert.equal(returned.calls, 2);
});

test("every retry checks the brakes again", async () => {
  const fn = script(httpError(503));
  await assert.rejects(guardedCall(fn, { ...base, ledger: memoryLedger(), caps: makeCaps({ monthlyUsd: 0.15 }), maxRetries: 3 }), brake("monthly_cap"));
  assert.equal(fn.calls, 1);
});

test("other errors are raised as they are and not repeated", async () => {
  const bad = script(httpError(400));
  await assert.rejects(guardedCall(bad, { ...base, ledger: memoryLedger(), maxRetries: 3 }), (e: unknown) => !(e instanceof BrakeError) && (e as { status?: number }).status === 400);
  assert.equal(bad.calls, 1);
  const bug = script(new RangeError("bug")), ledger = memoryLedger();
  await assert.rejects(guardedCall(bug, { ...base, ledger, maxRetries: 3 }), RangeError);
  assert.equal(bug.calls, 1);
});

test("a broken cost reader keeps the result and the estimate", async () => {
  const ledger = memoryLedger();
  assert.equal(await guardedCall(script("ok"), { ...base, ledger, estimateUsd: 0.25, costFromResult: () => { throw new Error("x"); } }), "ok");
  assert.equal(ledger.rows.at(-1)?.usd, 0.25);
});

test("many calls at the same time cannot exceed the same cap (memory ledger)", async () => {
  const ledger = memoryLedger(), caps = makeCaps({ monthlyUsd: 1, maxCallsPerJob: 20 });
  let ran = 0;
  const results = await Promise.allSettled(Array.from({ length: 50 }, (_, i) => guardedCall(async () => { ran++; return "ok"; }, { ...base, ledger, caps, job: `job-${i}` })));
  assert.equal(results.filter((r) => r.status === "fulfilled").length, 10); // 1.00 / 0.10, not 9, not 11
  assert.equal(ran, 10);
  assert.ok(results.filter((r) => r.status === "rejected").every((r) => brake("monthly_cap")((r as PromiseRejectedResult).reason)));
});

test("file ledger: concurrent appends are all kept whole, the cap holds, and old bytes never change", async () => {
  const path = tmpFile(), ledger: Ledger = fileLedger(path), caps = makeCaps({ monthlyUsd: 3, maxCallsPerJob: 20 });
  const results = await Promise.allSettled(Array.from({ length: 100 }, (_, i) => guardedCall(async () => "ok", { ...base, ledger, caps, job: `job-${i}` })));
  assert.equal(results.filter((r) => r.status === "fulfilled").length, 30);
  const text = readFileSync(path, "utf8"), lines = text.trim().split("\n");
  assert.equal(lines.length, 60); // 30 reserve + 30 settle
  assert.ok(lines.every((l) => JSON.parse(l).id)); // every line is whole and valid
  await guardedCall(async () => "ok", { ...base, ledger, caps: makeCaps({ monthlyUsd: 100 }), job: "later" });
  assert.ok(readFileSync(path, "utf8").startsWith(text)); // only added to
});

test("file ledger reads rows in the format the Python package writes (unreadable lines only with the opt-in)", async () => {
  const path = tmpFile(), month = new Date().toISOString().slice(0, 7);
  const row = (o: object) => JSON.stringify({ t: `${month}-15T10:00:00+00:00`, provider: "image_api", ...o });
  writeFileSync(path, [
    row({ event: "reserve", id: "a1", project: "proj-a", job: "j", usd: 0.5 }),
    row({ event: "settle", id: "a1", project: "proj-a", job: "j", usd: 0.4, outcome: "ok" }),
    row({ event: "reserve", id: "b1", project: "proj-b", job: "j", usd: 1.0 }),
    "not json",
    row({ event: "reserve", id: "c1", project: "proj-a", job: "j", usd: -9 }),
  ].join("\n") + "\n");
  await assert.rejects(guardedCall(async () => "ok", { ...base, ledger: fileLedger(path), estimateUsd: 0.2 }), brake("ledger_unreadable"));
  const ledger = fileLedger(path, { skipUnreadable: true });
  await assert.rejects(guardedCall(async () => "ok", { ...base, ledger, estimateUsd: 0.2, caps: makeCaps({ monthlyUsd: 1.5 }) }), brake("monthly_cap", /\$1\.40/)); // 0.4 + 1.0
  await assert.rejects(guardedCall(async () => "ok", { ...base, ledger, estimateUsd: 0.2, caps: makeCaps({ monthlyUsd: 100, projectUsd: 0.5 }) }), brake("project_cap", /\$0\.40/));
  assert.ok(existsSync(path));
});

test("a job name is required and a run id makes a new one every time", async () => {
  const fn = script("ok");
  // @ts-expect-error job is required: there is no default name
  await assert.rejects(guardedCall(fn, { estimateUsd: 0.1, project: "p", provider: "x", ledger: memoryLedger(), env: {}, caps: makeCaps() }), brake("bad_setting", /job is required/));
  await assert.rejects(guardedCall(fn, { ...base, ledger: memoryLedger(), job: "" }), brake("bad_setting"));
  assert.equal(fn.calls, 0);
  const ids = new Set(Array.from({ length: 50 }, () => runId()));
  assert.equal(ids.size, 50);
  for (const id of ids) assert.match(id, /^\d{8}T\d{6}Z-[0-9a-f]{6}$/);
  // a new run has its own count; a reused name keeps its old count
  const ledger = memoryLedger(), caps = makeCaps({ monthlyUsd: 100, maxCallsPerJob: 2 });
  for (let i = 0; i < 4; i++) await guardedCall(script("ok"), { ...base, ledger, caps, job: `batch-${runId()}` });
  const reused = `batch-${runId()}`;
  await guardedCall(script("ok"), { ...base, ledger, caps, job: reused });
  await guardedCall(script("ok"), { ...base, ledger, caps, job: reused });
  await assert.rejects(guardedCall(script("ok"), { ...base, ledger, caps, job: reused }), brake("job_limit"));
});

test("stop messages name the setting and do not teach how to turn off the stop", async () => {
  const stops: BrakeError[] = [];
  const grab = async (p: Promise<unknown>) => { try { await p; } catch (e) { stops.push(e as BrakeError); } };
  await grab(guardedCall(script("ok"), { ...base, ledger: memoryLedger(), caps: makeCaps({ monthlyUsd: 0.05 }) }));
  await grab(guardedCall(script("ok"), { ...base, ledger: memoryLedger(), caps: makeCaps({ monthlyUsd: 100, projectUsd: 0.05 }) }));
  await grab(guardedCall(script("ok"), { ...base, ledger: memoryLedger(), env: { SPEND_PANIC: "1" } }));
  await grab(guardedCall(script("ok"), { ...base, ledger: memoryLedger(), env: { SPEND_IMAGE_API_ENABLED: "0" } }));
  await grab(guardedCall(script(httpError(402)), { ...base, ledger: memoryLedger() }));
  assert.equal(stops.length, 5);
  for (const stop of stops) {
    assert.match(stop.message, /owner/);
    assert.doesNotMatch(stop.message, /makeCaps\(|set SPEND_|unset|delete|export|would pass/);
  }
  assert.match(stops[0].message, /would exceed the cap/);
});

test("AbortError (the caller cancelled) is not retried; TimeoutError is", async () => {
  const cancelled = script(Object.assign(new Error("cancelled"), { name: "AbortError" }));
  await assert.rejects(guardedCall(cancelled, { ...base, ledger: memoryLedger(), maxRetries: 3 }), (e: unknown) => !(e instanceof BrakeError) && (e as Error).name === "AbortError");
  assert.equal(cancelled.calls, 1);
  const slow = script(Object.assign(new Error("slow"), { name: "TimeoutError" }));
  await assert.rejects(guardedCall(slow, { ...base, ledger: memoryLedger(), maxRetries: 2 }), brake("retries_exhausted"));
  assert.equal(slow.calls, 3);
});

// ---- a damaged ledger must stop the calls ----------------------------------------------------------------------
const goodRow = (month: string, o: object = {}) => ({ t: `${month}-15T10:00:00.000Z`, id: "a1", event: "reserve", project: "proj-a", provider: "image_api", job: "j", usd: 19.99, ...o });
const thisMonth = () => new Date().toISOString().slice(0, 7);

test("one broken line can no longer turn the spend into zero: calls stop, nothing is written", async () => {
  const garbage = ["not json", '{"event":"reserve","id":"x","project":"a","job":"j","t":"1999-01-01T00:00:00Z","usd":-5}', '{"event":"reserve","id":"y","project":"a","job":"j","usd":1}',
    '{"event":"reserve","id":"y","job":"j","t":"1999-01-01T00:00:00Z","usd":1}', "[1,2]", "null", "{"];
  for (const bad of garbage) {
    const path = tmpFile();
    writeFileSync(path, JSON.stringify(goodRow(thisMonth())) + "\n" + bad + "\n");
    const before = readFileSync(path, "utf8"), fn = script("ok");
    await assert.rejects(guardedCall(fn, { ...base, ledger: fileLedger(path), estimateUsd: 0.05, caps: makeCaps({ monthlyUsd: 20 }) }),
      brake("ledger_unreadable", /line 2\)?.*make a copy of the file/));
    assert.equal(fn.calls, 0, bad);
    assert.equal(readFileSync(path, "utf8"), before); // nothing was added to a damaged file
  }
});

test("skipping unreadable lines is an explicit choice (option or environment) and says so in the numbers", async () => {
  const path = tmpFile();
  writeFileSync(path, JSON.stringify(goodRow(thisMonth())) + "\nnot json\n");
  const fn = script("ok");
  // the readable $19.99 still counts, so the cap of $20 stops a call of $0.05; the skipped line is simply not counted
  await assert.rejects(guardedCall(fn, { ...base, ledger: fileLedger(path, { skipUnreadable: true }), estimateUsd: 0.05, caps: makeCaps({ monthlyUsd: 20 }) }), brake("monthly_cap"));
  await guardedCall(fn, { ...base, ledger: fileLedger(path, { skipUnreadable: true }), estimateUsd: 0.005, caps: makeCaps({ monthlyUsd: 20 }) });
  process.env.SPEND_BRAKES_SKIP_UNREADABLE = "1";
  try { await guardedCall(fn, { ...base, ledger: fileLedger(path), estimateUsd: 0.001, caps: makeCaps({ monthlyUsd: 100 }) }); }
  finally { delete process.env.SPEND_BRAKES_SKIP_UNREADABLE; }
  await assert.rejects(guardedCall(fn, { ...base, ledger: fileLedger(path), estimateUsd: 0.001, caps: makeCaps({ monthlyUsd: 100 }) }), brake("ledger_unreadable")); // off by default again
  assert.equal(fn.calls, 2);
});

test("a row cut off by a crash is reported, and the next row is not hidden by it", async () => {
  const path = tmpFile(), fn = script("ok");
  writeFileSync(path, JSON.stringify(goodRow(thisMonth(), { usd: 0.1 })) + '\n{"t":"cut');
  await assert.rejects(guardedCall(fn, { ...base, ledger: fileLedger(path), caps: makeCaps({ monthlyUsd: 100 }) }), brake("ledger_unreadable"));
  await guardedCall(fn, { ...base, ledger: fileLedger(path, { skipUnreadable: true }), caps: makeCaps({ monthlyUsd: 100 }) });
  const lines = readFileSync(path, "utf8").split("\n");
  assert.equal(lines.at(-1), "");
  assert.equal(JSON.parse(lines.at(-2)!).event, "settle"); // the new rows are whole lines of their own
  assert.equal(JSON.parse(lines.at(-3)!).event, "reserve");
  assert.equal(lines[1], '{"t":"cut'); // the damaged line is still there to be repaired, not hidden
});

test("a complete last row that only lacks its end-of-line mark is fine", async () => {
  const path = tmpFile(), fn = script("ok");
  writeFileSync(path, JSON.stringify(goodRow(thisMonth(), { usd: 0.1 }))); // no final newline
  await guardedCall(fn, { ...base, ledger: fileLedger(path), caps: makeCaps({ monthlyUsd: 100 }) });
  assert.equal(readFileSync(path, "utf8").trim().split("\n").length, 3);
});

test("a huge whole number in the ledger is an unreadable line, not a crash or a free call", async () => {
  const path = tmpFile(), fn = script("ok");
  writeFileSync(path, JSON.stringify(goodRow(thisMonth(), { usd: 0.1 })) + "\n" + `{"event":"reserve","id":"big","project":"a","job":"j","t":"${thisMonth()}-01T00:00:00Z","usd":1${"0".repeat(400)}}\n`);
  await assert.rejects(guardedCall(fn, { ...base, ledger: fileLedger(path) }), brake("ledger_unreadable", /line 2/));
  assert.equal(fn.calls, 0);
});

// ---- the message of retries_exhausted --------------------------------------------------------------------------

test("the retries message says maxRetries is an option, not a setting, and names the ceiling", async () => {
  await assert.rejects(guardedCall(script(httpError(503)), { ...base, ledger: memoryLedger(), maxRetries: 1 }), (e: unknown) => {
    assert.ok(e instanceof BrakeError && e.brake === "retries_exhausted");
    assert.ok(!e.message.includes("setting: maxRetries"), "maxRetries is not an environment setting");
    assert.match(e.message, /the maxRetries option of guardedCall/);
    assert.match(e.message, /not an environment setting/);
    assert.match(e.message, new RegExp(`the ceiling is ${HARD_MAX_RETRIES}`));
    return true;
  });
});

// ---- the default stop file: a file named STOP in the same folder as the ledger file ----------------------------

test("a file ledger has a default stop file named STOP next to it; a memory ledger has none", () => {
  const path = tmpFile();
  assert.equal(fileLedger(path).stopFile, join(dirname(path), "STOP"));
  assert.equal(fileLedger(join(dirname(path), "deep", "other-name.jsonl")).stopFile, join(dirname(path), "deep", "STOP"));
  assert.equal(memoryLedger().stopFile, undefined);
});

test("the default stop file stops calls with no setting at all, and a memory ledger ignores it", async () => {
  const path = tmpFile(), ledger = fileLedger(path), fn = script("ok"), stopFile = join(dirname(path), "STOP");
  await guardedCall(fn, { ...base, ledger }); // env is {}: no SPEND_STOP_FILE anywhere
  writeFileSync(stopFile, "");
  const before = readFileSync(path, "utf8");
  await assert.rejects(guardedCall(fn, { ...base, ledger }), (e: unknown) => brake("kill_switch")(e) && [`the default stop file exists (${stopFile})`, "owner removes that file", "SPEND_STOP_FILE"].every((t) => (e as Error).message.includes(t)));
  assert.equal(fn.calls, 1); // the stopped call never ran
  assert.equal(readFileSync(path, "utf8"), before); // and wrote nothing
  await guardedCall(script("ok"), { ...base, ledger: memoryLedger() }); // a ledger that is not a file has no default stop file
  rmSync(stopFile);
  await guardedCall(fn, { ...base, ledger });
  assert.equal(fn.calls, 2);
});

test("a stop file in another folder does not stop calls, and the default stop file also stops a retry", async () => {
  const path = tmpFile(), other = tmpFile();
  writeFileSync(join(dirname(other), "STOP"), "");
  await guardedCall(script("ok"), { ...base, ledger: fileLedger(path) });
  const flaky = async () => { writeFileSync(join(dirname(path), "STOP"), ""); throw httpError(503); };
  await assert.rejects(guardedCall(flaky, { ...base, ledger: fileLedger(path), maxRetries: 3 }), brake("kill_switch"));
});

test("SPEND_STOP_FILE replaces the default stop file", async () => {
  const path = tmpFile(), ledger = fileLedger(path), mine = join(dirname(path), "my-own-stop-file");
  writeFileSync(join(dirname(path), "STOP"), "");
  await guardedCall(script("ok"), { ...base, ledger, env: { SPEND_STOP_FILE: mine } }); // the named file does not exist: go
  writeFileSync(mine, "");
  await assert.rejects(guardedCall(script("ok"), { ...base, ledger, env: { SPEND_STOP_FILE: mine } }), brake("kill_switch", /named in SPEND_STOP_FILE/));
  await assert.rejects(guardedCall(script("ok"), { ...base, ledger }), brake("kill_switch", /default stop file/)); // no setting: the default applies again
  assert.doesNotThrow(() => checkKillSwitch("image_api", { SPEND_STOP_FILE: join(dirname(path), "nothing") }, mine));
  assert.throws(() => checkKillSwitch("image_api", { SPEND_STOP_FILE: "   " }, mine), brake("kill_switch")); // an empty setting is the same as no setting
});

// ---- checkCall: the read-only "would this call be allowed?" check ----------------------------------------------

const listing = (folder: string) => readdirSync(folder).map((n) => `${n}:${readFileSync(join(folder, n), "utf8").length}`).sort();

test("checkCall: an allowed call returns and writes nothing, not even a folder; it never uses up the job limit", async () => {
  const folder = join(mkdtempSync(join(tmpdir(), "sb-")), "fresh-folder");
  const ledger = fileLedger(join(folder, "ledger.jsonl"));
  assert.equal(await checkCall({ ...base, ledger }), undefined);
  assert.ok(!existsSync(folder));

  const memory = memoryLedger(), caps = makeCaps({ monthlyUsd: 100, maxCallsPerJob: 2 });
  for (let i = 0; i < 5; i++) await checkCall({ ...base, ledger: memory, caps });
  assert.equal(memory.rows.length, 0);
  await guardedCall(script("ok"), { ...base, ledger: memory, caps });
  await guardedCall(script("ok"), { ...base, ledger: memory, caps });
  await assert.rejects(checkCall({ ...base, ledger: memory, caps }), brake("job_limit", /limit 2/));
});

test("checkCall raises the same error with the same message as a real call, for every brake, and writes nothing", async () => {
  const used = async (runs: number) => {
    const path = tmpFile(), ledger = fileLedger(path);
    for (let i = 0; i < runs; i++) await guardedCall(script("ok"), { ...base, ledger });
    return { path, ledger };
  };
  const scenarios: Array<[string, () => Promise<{ path: string; opts: any }>]> = [
    ["kill_switch", async () => { const u = await used(0); return { path: u.path, opts: { ledger: u.ledger, env: { SPEND_PANIC: "1" } } }; }],
    ["kill_switch", async () => { const u = await used(0); return { path: u.path, opts: { ledger: u.ledger, env: { SPEND_IMAGE_API_ENABLED: "0" } } }; }],
    ["kill_switch", async () => { const u = await used(0); writeFileSync(join(dirname(u.path), "STOP"), ""); return { path: u.path, opts: { ledger: u.ledger } }; }],
    ["job_limit", async () => { const u = await used(2); return { path: u.path, opts: { ledger: u.ledger, caps: makeCaps({ monthlyUsd: 100, maxCallsPerJob: 2 }) } }; }],
    ["project_cap", async () => { const u = await used(2); return { path: u.path, opts: { ledger: u.ledger, job: "job-2", caps: makeCaps({ monthlyUsd: 100, projectUsd: 0.25 }) } }; }],
    ["monthly_cap", async () => { const u = await used(2); return { path: u.path, opts: { ledger: u.ledger, job: "job-2", caps: makeCaps({ monthlyUsd: 0.25 }) } }; }],
    ["ledger_unreadable", async () => { const u = await used(1); writeFileSync(u.path, readFileSync(u.path, "utf8") + "this line is not a ledger row\n"); return { path: u.path, opts: { ledger: u.ledger } }; }],
    ["hard_ceiling", async () => { const u = await used(0); return { path: u.path, opts: { ledger: u.ledger, maxRetries: 4 } }; }],
    ["bad_setting", async () => { const u = await used(0); return { path: u.path, opts: { ledger: u.ledger, estimateUsd: Number.NaN } }; }],
    ["bad_setting", async () => { const u = await used(0); return { path: u.path, opts: { ledger: u.ledger, job: "" } }; }],
  ];
  for (const [name, build] of scenarios) {
    const { path, opts } = await build();
    const before = listing(dirname(path));
    const dry = await checkCall({ ...base, ...opts }).then(() => assert.fail(`checkCall did not stop: ${name}`), (e: unknown) => e);
    assert.deepEqual(listing(dirname(path)), before); // a refusal writes nothing either
    const fn = script("ok");
    const real = await guardedCall(fn, { ...base, ...opts }).then(() => assert.fail(`guardedCall did not stop: ${name}`), (e: unknown) => e);
    assert.equal(fn.calls, 0); // the real call was stopped too
    assert.ok(dry instanceof BrakeError && real instanceof BrakeError, name);
    assert.equal((dry as BrakeError).brake, name);
    assert.equal((dry as BrakeError).brake, (real as BrakeError).brake);
    assert.equal((dry as BrakeError).message, (real as BrakeError).message);
  }
});

test("checkCall takes the same options as guardedCall and reads the environment settings like it", async () => {
  const ledger = memoryLedger();
  const same = { ...base, ledger, maxRetries: 2, retryDelayMs: 0, costFromResult: () => 0.01 };
  await checkCall(same);
  assert.equal(await guardedCall(script("ok"), same), "ok");
  const env: Record<string, string> = { SPEND_MONTHLY_CAP_USD: "0.05" }; // below the estimate
  const open = { estimateUsd: 0.1, project: "p", provider: "image_api", job: "j", ledger: memoryLedger(), env };
  await assert.rejects(checkCall(open), brake("monthly_cap", /SPEND_MONTHLY_CAP_USD/));
  env.SPEND_MONTHLY_CAP_USD = "5";
  await checkCall(open);
});


// ---- caps and retries are checked inside the guard, whatever object they come from ------------------------------

test("the attack: a plain caps object with maxCallsPerJob 500 lets no call through (guardedCall and checkCall)", async () => {
  const ledger = memoryLedger(), fn = script("ok");
  const caps = { monthlyUsd: 20, maxCallsPerJob: 500 }; // not made by makeCaps()
  let through = 0;
  for (let i = 0; i < 500; i++) {
    try { await guardedCall(fn, { ...base, ledger, caps, estimateUsd: 0 }); through++; } catch (e) { assert.ok(brake("hard_ceiling", /HARD_MAX_CALLS_PER_JOB/)(e)); }
  }
  assert.equal(through, 0);
  assert.equal(fn.calls, 0);
  assert.equal(ledger.rows.length, 0);
  await assert.rejects(checkCall({ ...base, ledger, caps }), brake("hard_ceiling"));
});

test("a plain caps object at exactly the ceiling works, and the call after the ceiling is stopped", async () => {
  const ledger = memoryLedger(), fn = script("ok"), caps = { monthlyUsd: 100, maxCallsPerJob: HARD_MAX_CALLS_PER_JOB };
  for (let i = 0; i < HARD_MAX_CALLS_PER_JOB; i++) await guardedCall(fn, { ...base, ledger, caps });
  await assert.rejects(guardedCall(fn, { ...base, ledger, caps }), brake("job_limit", /limit 20,/));
  await assert.rejects(guardedCall(fn, { ...base, ledger, caps: { ...caps, maxCallsPerJob: HARD_MAX_CALLS_PER_JOB + 1 } }), brake("hard_ceiling"));
  assert.equal(fn.calls, HARD_MAX_CALLS_PER_JOB);
});

const badCaps: Array<[string, unknown]> = [
  ["NaN monthly cap", { monthlyUsd: Number.NaN, maxCallsPerJob: 3 }],
  ["Infinity monthly cap", { monthlyUsd: Infinity, maxCallsPerJob: 3 }],
  ["negative monthly cap", { monthlyUsd: -0.01, maxCallsPerJob: 3 }],
  ["monthly cap as text", { monthlyUsd: "20", maxCallsPerJob: 3 }],
  ["monthly cap null", { monthlyUsd: null, maxCallsPerJob: 3 }],
  ["monthly cap missing", { maxCallsPerJob: 3 }],
  ["NaN project cap", { monthlyUsd: 20, projectUsd: Number.NaN, maxCallsPerJob: 3 }],
  ["Infinity project cap", { monthlyUsd: 20, projectUsd: Infinity, maxCallsPerJob: 3 }],
  ["negative project cap", { monthlyUsd: 20, projectUsd: -1, maxCallsPerJob: 3 }],
  ["project cap as text", { monthlyUsd: 20, projectUsd: "5", maxCallsPerJob: 3 }],
  ["project cap null", { monthlyUsd: 20, projectUsd: null, maxCallsPerJob: 3 }],
  ["NaN job limit", { monthlyUsd: 20, maxCallsPerJob: Number.NaN }],
  ["Infinity job limit", { monthlyUsd: 20, maxCallsPerJob: Infinity }],
  ["job limit 0", { monthlyUsd: 20, maxCallsPerJob: 0 }],
  ["negative job limit", { monthlyUsd: 20, maxCallsPerJob: -3 }],
  ["fractional job limit", { monthlyUsd: 20, maxCallsPerJob: 2.5 }],
  ["job limit as text", { monthlyUsd: 20, maxCallsPerJob: "3" }],
  ["job limit null", { monthlyUsd: 20, maxCallsPerJob: null }],
  ["job limit missing", { monthlyUsd: 20 }],
  ["empty object", {}],
  ["not an object: text", "caps"],
  ["not an object: number", 20],
  ["not an object: true", true],
];

test("a cap that is not a finite number of zero or more is a bad_setting error in guardedCall and checkCall, and nothing is written", async () => {
  for (const [name, caps] of badCaps) {
    const ledger = memoryLedger(), fn = script("ok");
    await assert.rejects(guardedCall(fn, { ...base, ledger, caps: caps as any }), brake("bad_setting"), name);
    await assert.rejects(checkCall({ ...base, ledger, caps: caps as any }), brake("bad_setting"), name);
    assert.equal(fn.calls, 0, name);
    assert.equal(ledger.rows.length, 0, name);
  }
});

test("a NaN cap no longer switches the cap off", async () => {
  const ledger = memoryLedger(), fn = script("ok");
  await assert.rejects(guardedCall(fn, { ...base, ledger, estimateUsd: 1000, caps: { monthlyUsd: Number.NaN, maxCallsPerJob: 3 } }), brake("bad_setting", /monthlyUsd/));
  await assert.rejects(guardedCall(fn, { ...base, ledger, estimateUsd: 1000, caps: { monthlyUsd: 100, projectUsd: Number.NaN, maxCallsPerJob: 3 } }), brake("bad_setting", /projectUsd/));
  assert.equal(fn.calls, 0);
});

test("a plain caps object with good numbers is accepted, and the project cap in it works", async () => {
  const ledger = memoryLedger(), fn = script("ok"), caps = { monthlyUsd: 1, projectUsd: 0.1, maxCallsPerJob: 2 };
  await guardedCall(fn, { ...base, ledger, caps });
  await assert.rejects(guardedCall(fn, { ...base, ledger, caps }), brake("project_cap"));
});

test("the cap numbers are read once, so a getter cannot give a higher limit later", async () => {
  let reads = 0;
  const shifty = { monthlyUsd: 100, get maxCallsPerJob() { reads++; return reads === 1 ? 3 : 500; } };
  const flaky = script(httpError(503));
  await assert.rejects(guardedCall(flaky, { ...base, ledger: memoryLedger(), caps: shifty, maxRetries: HARD_MAX_RETRIES, retryDelayMs: 0 }), brake("job_limit"));
  assert.equal(flaky.calls, 3);
  assert.equal(reads, 1);
});

test("maxRetries and retryDelayMs follow the same rules in guardedCall and checkCall", async () => {
  for (const bad of [Number.NaN, Infinity, 1.5, null, "2", true, -1]) {
    const fn = script("ok");
    await assert.rejects(guardedCall(fn, { ...base, ledger: memoryLedger(), maxRetries: bad as any }), brake("bad_setting"), String(bad));
    await assert.rejects(checkCall({ ...base, ledger: memoryLedger(), maxRetries: bad as any }), brake("bad_setting"), String(bad));
    assert.equal(fn.calls, 0);
  }
  for (const tooMany of [HARD_MAX_RETRIES + 1, 50, 1e9]) {
    await assert.rejects(guardedCall(script("ok"), { ...base, ledger: memoryLedger(), maxRetries: tooMany }), brake("hard_ceiling"));
    await assert.rejects(checkCall({ ...base, ledger: memoryLedger(), maxRetries: tooMany }), brake("hard_ceiling"));
  }
  for (const bad of [Number.NaN, Infinity, -1, "5", null]) {
    const fn = script("ok");
    await assert.rejects(guardedCall(fn, { ...base, ledger: memoryLedger(), retryDelayMs: bad as any }), brake("bad_setting", /retryDelayMs/), String(bad));
    await assert.rejects(checkCall({ ...base, ledger: memoryLedger(), retryDelayMs: bad as any }), brake("bad_setting", /retryDelayMs/), String(bad));
    assert.equal(fn.calls, 0);
  }
  await guardedCall(script("ok"), { ...base, ledger: memoryLedger(), maxRetries: HARD_MAX_RETRIES, retryDelayMs: 0 });
});

// ---- one project, many spellings -----------------------------------------------------------------------------------

const SPELLINGS = ["blog-images", "blog images", "Blog_Images", "BLOG-IMAGES"]; // all give SPEND_PROJECT_CAP_USD_BLOG_IMAGES

test("the project cap is one cap for every spelling of the project name, and rows keep the original text", async () => {
  const ledger = memoryLedger(), caps = makeCaps({ monthlyUsd: 100, projectUsd: 0.4 });
  for (const project of SPELLINGS) await guardedCall(script("ok"), { ...base, ledger, caps, project }); // four calls of $0.10: exactly the cap
  for (const project of SPELLINGS) await assert.rejects(guardedCall(script("ok"), { ...base, ledger, caps, project }), brake("project_cap", /SPEND_PROJECT_CAP_USD_BLOG_IMAGES/), project);
  assert.deepEqual(ledger.rows.filter((r) => r.event === "reserve").map((r) => r.project), SPELLINGS);
  await guardedCall(script("ok"), { ...base, ledger, caps, project: "blog-images-2" }); // another name is another project
  await guardedCall(script("ok"), { ...base, ledger, caps, project: "blog" });
});

test("the job limit counts every spelling of the project name together", async () => {
  const ledger = memoryLedger(), caps = makeCaps({ monthlyUsd: 100, maxCallsPerJob: 4 }), fn = script("ok");
  for (const project of SPELLINGS) await guardedCall(fn, { ...base, ledger, caps, project, job: "run-1" });
  for (const project of SPELLINGS) await assert.rejects(guardedCall(fn, { ...base, ledger, caps, project, job: "run-1" }), brake("job_limit"), project);
  await guardedCall(fn, { ...base, ledger, caps, project: "blog-images", job: "run-2" }); // another job
  await guardedCall(fn, { ...base, ledger, caps, project: "other-project", job: "run-1" }); // another project
  assert.equal(fn.calls, 6);
});

test("a row written under one spelling (for example by the Python package) counts for the other spellings in a file ledger", async () => {
  const path = tmpFile(), caps = makeCaps({ monthlyUsd: 100, projectUsd: 0.5 });
  writeFileSync(path, JSON.stringify(goodRow(thisMonth(), { project: "Blog_Images", usd: 0.45, job: "j-old" })) + "\n");
  await assert.rejects(guardedCall(script("ok"), { ...base, ledger: fileLedger(path), caps, project: "blog-images", estimateUsd: 0.1 }), brake("project_cap"));
  await guardedCall(script("ok"), { ...base, ledger: fileLedger(path), caps, project: "blog-images", estimateUsd: 0.05 });
});

test("projectKey: the key of a setting name; Finnish letters are cleaned like the setting name; a name without letters keeps its text", () => {
  for (const name of [...SPELLINGS, "  blog -- images  ", "__blog.images__"]) assert.equal(projectKey(name), "BLOG_IMAGES", name);
  assert.notEqual(projectKey("blog-images-2"), projectKey("blog-images"));
  assert.equal(projectKey("Kuvat äänet"), "KUVAT_NET");
  assert.equal(projectKey("kuvat-äänet"), projectKey("Kuvat äänet"));
  assert.equal(projectKey("---"), "---");
  assert.equal(projectKey("ブログ"), "ブログ");
  assert.notEqual(projectKey("ニュース"), projectKey("ブログ"));
  assert.equal(projectKey(""), ""); // never throws
});

test("Finnish and non-Latin project names are counted without errors", async () => {
  const ledger = memoryLedger(), caps = makeCaps({ monthlyUsd: 100, projectUsd: 0.2 });
  await guardedCall(script("ok"), { ...base, ledger, caps, project: "Kuvat äänet" });
  await guardedCall(script("ok"), { ...base, ledger, caps, project: "kuvat-äänet" });
  await guardedCall(script("ok"), { ...base, ledger, caps, project: "ブログ" }); // no A-Z or 0-9: counted under its exact text
  await guardedCall(script("ok"), { ...base, ledger, caps, project: "ニュース" }); // another such name is another project
});

// ---- the default ledger file and the default stop file are the same as in Python ------------------------------

test("defaultLedgerPath: SPEND_BRAKES_LEDGER, else ~/.spend-brakes/ledger.jsonl (the same rule as Python)", () => {
  assert.equal(defaultLedgerPath({ SPEND_BRAKES_LEDGER: "/some/where/l.jsonl" }), "/some/where/l.jsonl");
  assert.equal(defaultLedgerPath({ SPEND_BRAKES_LEDGER: "  /some/where/l.jsonl  " }), "/some/where/l.jsonl");
  const standard = join(homedir(), ".spend-brakes", "ledger.jsonl");
  for (const env of [{}, { SPEND_BRAKES_LEDGER: "" }, { SPEND_BRAKES_LEDGER: "   " }, { SPEND_BRAKES_LEDGER: undefined }]) assert.equal(defaultLedgerPath(env), standard);
  const saved = process.env.SPEND_BRAKES_LEDGER;
  try {
    process.env.SPEND_BRAKES_LEDGER = "/from/process/env.jsonl";
    assert.equal(defaultLedgerPath(), "/from/process/env.jsonl");
    delete process.env.SPEND_BRAKES_LEDGER;
    assert.equal(defaultLedgerPath(), standard);
  } finally { if (saved === undefined) delete process.env.SPEND_BRAKES_LEDGER; else process.env.SPEND_BRAKES_LEDGER = saved; }
});

test("the default stop file of the default ledger is ~/.spend-brakes/STOP, the same file the Python package uses", () => {
  assert.equal(fileLedger(defaultLedgerPath({})).stopFile, join(homedir(), ".spend-brakes", "STOP"));
  assert.equal(fileLedger(defaultLedgerPath({ SPEND_BRAKES_LEDGER: "/data/x/l.jsonl" })).stopFile, join("/data/x", "STOP"));
});

// ---- SPEND_BRAKES_SKIP_UNREADABLE: unknown values are an error, like in Python ----------------------------------

test("an unknown SPEND_BRAKES_SKIP_UNREADABLE value is a bad_setting error; the known values still work", () => {
  const saved = process.env.SPEND_BRAKES_SKIP_UNREADABLE;
  try {
    for (const bad of ["maybe", "2", "enabled", "disabled", "y", "tru"]) {
      process.env.SPEND_BRAKES_SKIP_UNREADABLE = bad;
      assert.throws(() => fileLedger(tmpFile()), brake("bad_setting", /SPEND_BRAKES_SKIP_UNREADABLE.*cannot be read\. Use 1 or 0/), bad);
    }
    for (const ok of ["", "0", "false", "OFF", " no ", "1", "true", "On", "YES"]) {
      process.env.SPEND_BRAKES_SKIP_UNREADABLE = ok;
      assert.doesNotThrow(() => fileLedger(tmpFile()), ok);
    }
    process.env.SPEND_BRAKES_SKIP_UNREADABLE = "maybe";
    assert.doesNotThrow(() => fileLedger(tmpFile(), { skipUnreadable: false })); // an explicit option is used, and the setting is not read (like Python's Ledger(path, skip_unreadable=...))
  } finally { if (saved === undefined) delete process.env.SPEND_BRAKES_SKIP_UNREADABLE; else process.env.SPEND_BRAKES_SKIP_UNREADABLE = saved; }
});

// ---- the skip warning: once per program ------------------------------------------------------------------------

test("skipping unreadable lines warns once per program (console.warn), not on every read and not for every ledger", () => {
  const moduleUrl = new URL("./spend-brakes.js", import.meta.url).href;
  const program = `
    import { checkCall, fileLedger } from ${JSON.stringify(moduleUrl)};
    import { mkdtempSync, writeFileSync } from "node:fs";
    import { tmpdir } from "node:os";
    import { join } from "node:path";
    const warnings = [];
    console.warn = (m) => warnings.push(String(m));
    const dir = mkdtempSync(join(tmpdir(), "sb-"));
    const a = join(dir, "a.jsonl"), b = join(dir, "b.jsonl"), clean = join(dir, "c.jsonl");
    writeFileSync(a, "not json\\n");
    writeFileSync(b, "also not json\\nnor this\\n");
    const options = { estimateUsd: 0.1, project: "p", provider: "x", job: "j", env: {}, caps: { monthlyUsd: 100, maxCallsPerJob: 5 } };
    await checkCall({ ...options, ledger: fileLedger(clean, { skipUnreadable: true }) }); // no unreadable line: no warning
    const before = warnings.length;
    for (let i = 0; i < 3; i++) await checkCall({ ...options, ledger: fileLedger(a, { skipUnreadable: true }) });
    await checkCall({ ...options, ledger: fileLedger(b, { skipUnreadable: true }) });
    console.log(JSON.stringify({ before, warnings }));
  `;
  const out = JSON.parse(execFileSync(process.execPath, ["--input-type=module", "-e", program], { encoding: "utf8" }));
  assert.equal(out.before, 0);
  assert.equal(out.warnings.length, 1);
  assert.match(out.warnings[0], /^spend_brakes: skipping 1 unreadable ledger line\(s\); their spend is NOT counted\.$/);
});
