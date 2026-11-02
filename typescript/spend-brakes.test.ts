import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { BrakeError, HARD_MAX_CALLS_PER_JOB, HARD_MAX_RETRIES, capsFromEnv, checkKillSwitch, fileLedger, guardedCall, makeCaps, memoryLedger } from "./spend-brakes.js";
import type { Ledger } from "./spend-brakes.js";

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

test("monthly cap: the call that would pass the cap is stopped and never runs", async () => {
  const ledger = memoryLedger(), fn = script("ok"), caps = makeCaps({ monthlyUsd: 0.25 });
  await guardedCall(fn, { ...base, ledger, caps });
  await guardedCall(fn, { ...base, ledger, caps });
  await assert.rejects(guardedCall(fn, { ...base, ledger, caps }), brake("monthly_cap", /on purpose: set SPEND_MONTHLY_CAP_USD/));
  assert.equal(fn.calls, 2);
});

test("a call that lands exactly on the cap is allowed, the next one is not", async () => {
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
  for (const v of ["1", "true", "on", "yes"]) await assert.rejects(guardedCall(fn, { ...base, ledger, env: { SPEND_PANIC: v } }), brake("kill_switch", /SPEND_PANIC.*unset/));
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
  await assert.rejects(guardedCall(fn, { ...base, ledger, env }), brake("kill_switch", /delete that file/));
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

test("many calls at the same time cannot pass the same cap (memory ledger)", async () => {
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

test("file ledger reads rows in the format the Python package writes, and skips unreadable lines", async () => {
  const path = tmpFile(), month = new Date().toISOString().slice(0, 7);
  const row = (o: object) => JSON.stringify({ t: `${month}-15T10:00:00+00:00`, provider: "image_api", ...o });
  writeFileSync(path, [
    row({ event: "reserve", id: "a1", project: "proj-a", job: "j", usd: 0.5 }),
    row({ event: "settle", id: "a1", project: "proj-a", job: "j", usd: 0.4, outcome: "ok" }),
    row({ event: "reserve", id: "b1", project: "proj-b", job: "j", usd: 1.0 }),
    "not json",
    row({ event: "reserve", id: "c1", project: "proj-a", job: "j", usd: -9 }),
  ].join("\n") + "\n");
  const ledger = fileLedger(path);
  await assert.rejects(guardedCall(async () => "ok", { ...base, ledger, estimateUsd: 0.2, caps: makeCaps({ monthlyUsd: 1.5 }) }), brake("monthly_cap", /\$1\.40/)); // 0.4 + 1.0
  await assert.rejects(guardedCall(async () => "ok", { ...base, ledger, estimateUsd: 0.2, caps: makeCaps({ monthlyUsd: 100, projectUsd: 0.5 }) }), brake("project_cap", /\$0\.40/));
  assert.ok(existsSync(path));
});
