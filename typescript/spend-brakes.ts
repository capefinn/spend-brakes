// spend-brakes.ts - spend brakes for Node and serverless code that call paid APIs. One file, no dependencies.
// Same five brakes and the same ledger row format as the Python package, so both can share one ledger file.
// Brakes, checked before EVERY attempt: kill switch, job limit, project cap, monthly cap. Then the retry rules.
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";

// Part of the code on purpose: no argument and no environment variable can raise these two numbers.
export const HARD_MAX_CALLS_PER_JOB = 20;
export const HARD_MAX_RETRIES = 3;
const DEFAULT_MONTHLY_CAP_USD = 20; // an example number: set your own
const DEFAULT_MAX_CALLS_PER_JOB = 6;

type Env = Record<string, string | undefined>;
export type BrakeName = "kill_switch" | "monthly_cap" | "project_cap" | "job_limit" | "hard_ceiling" | "no_retry" | "retries_exhausted" | "bad_setting";

/** Every stop is a BrakeError. `.brake` says which brake; the message says how to change it on purpose. */
export class BrakeError extends Error {
  readonly brake: BrakeName;
  readonly status?: number;
  constructor(brake: BrakeName, message: string, options: { status?: number; cause?: unknown } = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "BrakeError";
    this.brake = brake;
    this.status = options.status;
  }
}

const usd = (x: number) => (x === 0 || x >= 0.01 ? `$${x.toFixed(2)}` : `$${x.toFixed(4)}`);
const round6 = (x: number) => Math.round(x * 1e6) / 1e6;
const envWord = (name: string) => {
  const w = name.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toUpperCase();
  if (!w) throw new BrakeError("bad_setting", `The name "${name}" has no letters or digits.`);
  return w;
};
const money = (v: unknown, label: string): number => {
  if (typeof v !== "number" || !Number.isFinite(v) || v < 0) throw new BrakeError("bad_setting", `${label} must be a number that is zero or more (got ${String(v)}).`);
  return v;
};

// ---- caps -----------------------------------------------------------------------------------------------------
export interface Caps { readonly monthlyUsd: number; readonly projectUsd?: number; readonly maxCallsPerJob: number }

export function makeCaps(c: { monthlyUsd?: number; projectUsd?: number; maxCallsPerJob?: number } = {}): Caps {
  const n = c.maxCallsPerJob ?? DEFAULT_MAX_CALLS_PER_JOB;
  if (!Number.isInteger(n) || n < 1) throw new BrakeError("bad_setting", `maxCallsPerJob must be a whole number of 1 or more (got ${String(n)}).`);
  if (n > HARD_MAX_CALLS_PER_JOB) {
    throw new BrakeError("hard_ceiling", `maxCallsPerJob=${n} is above the hard ceiling of ${HARD_MAX_CALLS_PER_JOB}. The ceiling is written into the code on purpose, so no setting can raise it. To change it, edit HARD_MAX_CALLS_PER_JOB in this file and have that change reviewed.`);
  }
  return Object.freeze({
    monthlyUsd: money(c.monthlyUsd ?? DEFAULT_MONTHLY_CAP_USD, "monthlyUsd"),
    projectUsd: c.projectUsd === undefined ? undefined : money(c.projectUsd, "projectUsd"),
    maxCallsPerJob: n,
  });
}

/** Read SPEND_MONTHLY_CAP_USD, SPEND_PROJECT_CAP_USD_<PROJECT> and SPEND_MAX_CALLS_PER_JOB. */
export function capsFromEnv(project: string, env: Env = process.env): Caps {
  const num = (name: string): number | undefined => {
    const raw = (env[name] ?? "").trim();
    if (!raw) return undefined;
    const v = Number(raw);
    if (!Number.isFinite(v)) throw new BrakeError("bad_setting", `${name}="${raw}" is not a number. Set it to a plain number such as 20.`);
    return v;
  };
  return makeCaps({ monthlyUsd: num("SPEND_MONTHLY_CAP_USD"), projectUsd: num(`SPEND_PROJECT_CAP_USD_${envWord(project)}`), maxCallsPerJob: num("SPEND_MAX_CALLS_PER_JOB") });
}

// ---- kill switch ----------------------------------------------------------------------------------------------
// SPEND_PANIC=1 stops everything. SPEND_<PROVIDER>_ENABLED=0 stops one provider. If the file named in
// SPEND_STOP_FILE exists, everything stops. A value that cannot be read counts as STOP (the safe side).
export function checkKillSwitch(provider: string, env: Env = process.env): void {
  const stop = (m: string) => { throw new BrakeError("kill_switch", `Stopped by the kill switch: ${m}`); };
  const word = (name: string) => (env[name] ?? "").trim().toLowerCase();
  const ON = ["1", "true", "on", "yes"], OFF = ["0", "false", "off", "no"];

  const panic = word("SPEND_PANIC");
  if (ON.includes(panic)) stop("SPEND_PANIC is on, so no paid call is allowed. To allow calls again on purpose, unset SPEND_PANIC or set it to 0.");
  if (panic && !OFF.includes(panic)) stop(`SPEND_PANIC="${panic}" cannot be read, so it counts as ON (the safe side). Set it to 1 (stop) or 0 (go).`);

  const file = (env.SPEND_STOP_FILE ?? "").trim();
  if (file && existsSync(file)) stop(`the stop file named in SPEND_STOP_FILE exists (${file}). To allow calls again on purpose, delete that file.`);

  const name = `SPEND_${envWord(provider)}_ENABLED`, value = word(name);
  if (!value || ON.includes(value)) return;
  if (OFF.includes(value)) stop(`${name} is ${value}, so provider "${provider}" is switched off. To allow calls again on purpose, set ${name}=1 or unset it.`);
  stop(`${name}="${value}" cannot be read, so provider "${provider}" counts as OFF (the safe side). Set it to 1 (go) or 0 (stop).`);
}

// ---- ledger ---------------------------------------------------------------------------------------------------
// Each call writes a "reserve" row BEFORE the call (the estimate) and a "settle" row AFTER (the real cost, or 0 when
// refused). The cost of a call is the usd of its last row. Rows are only ever added. Same format as the Python package.
export interface Row { t: string; id: string; event: "reserve" | "settle"; project: string; provider: string; job: string; usd: number; outcome?: string }
export interface Totals { monthUsd: number; projectUsd: number; jobCalls: number }
export interface Ledger {
  /** Must be atomic: read the totals, let `check` throw to refuse, then record the estimate. Returns the call id. */
  reserve(e: { project: string; provider: string; job: string; usd: number }, check: (t: Totals) => void): Promise<string>;
  settle(e: { id: string; project: string; provider: string; job: string; usd: number; outcome: "ok" | "rejected" | "error" }): Promise<void>;
}

const rowOk = (r: any): r is Row => r && typeof r === "object" && (r.event === "reserve" || r.event === "settle") && typeof r.id === "string" && typeof r.usd === "number" && Number.isFinite(r.usd) && r.usd >= 0;

function totalsOf(rows: Row[], project: string, job: string, month: string): Totals {
  const calls = new Map<string, { month: string; project: string; job: string; usd: number }>();
  for (const r of rows) {
    const c = calls.get(r.id) ?? { month: String(r.t ?? "").slice(0, 7), project: r.project, job: r.job, usd: r.usd };
    if (r.event === "settle") c.usd = r.usd;
    calls.set(r.id, c);
  }
  let monthUsd = 0, projectUsd = 0, jobCalls = 0;
  for (const c of calls.values()) {
    if (c.project === project && c.job === job) jobCalls++;
    if (c.month === month) { monthUsd += c.usd; if (c.project === project) projectUsd += c.usd; }
  }
  return { monthUsd: round6(monthUsd), projectUsd: round6(projectUsd), jobCalls };
}

// The work inside reserve is synchronous (read, check, append), so in one process two calls cannot interleave between
// the check and the record. A Ledger over an async store (a database) must give the same guarantee itself.
function rowsLedger(store: { read(): Row[]; append(row: Row): void }, now: () => Date): Ledger {
  return {
    reserve: async (e, check) => {
      for (const [label, v] of [["project", e.project], ["provider", e.provider], ["job", e.job]]) if (!v || v.length > 200) throw new BrakeError("bad_setting", `${label} must be a name of 1 to 200 characters.`);
      money(e.usd, "estimateUsd");
      check(totalsOf(store.read(), e.project, e.job, now().toISOString().slice(0, 7)));
      const id = globalThis.crypto.randomUUID().replace(/-/g, "");
      store.append({ t: now().toISOString(), id, event: "reserve", project: e.project, provider: e.provider, job: e.job, usd: e.usd });
      return id;
    },
    settle: async (e) => store.append({ t: now().toISOString(), event: "settle", ...e }),
  };
}

/** Rows kept in memory: for tests and for one running process. Nothing is shared between processes. */
export function memoryLedger(now: () => Date = () => new Date()): Ledger & { rows: Row[] } {
  const rows: Row[] = [];
  return { ...rowsLedger({ read: () => rows, append: (r) => void rows.push(r) }, now), rows };
}

/** A JSONL file. Every row is one append (the system adds the whole line or none of it). Check-and-record is safe
 *  inside one process only; for several processes or servers, write a Ledger over a database transaction instead. */
export function fileLedger(path: string, now: () => Date = () => new Date()): Ledger {
  const read = (): Row[] => {
    if (!existsSync(path)) return [];
    return readFileSync(path, "utf8").split("\n").flatMap((line) => { try { const r = JSON.parse(line); return rowOk(r) ? [r] : []; } catch { return []; } });
  };
  const append = (row: Row) => { mkdirSync(dirname(path), { recursive: true }); appendFileSync(path, JSON.stringify(row) + "\n", { mode: 0o600 }); };
  return rowsLedger({ read, append }, now);
}

// ---- guarded call ---------------------------------------------------------------------------------------------
const NO_RETRY: Record<number, string> = { 401: "the key was not accepted", 402: "payment is required: no money or a blocked plan", 403: "the call is not allowed", 429: "rate limit or quota reached" };
const NET_CODES = new Set(["ECONNRESET", "ETIMEDOUT", "ECONNREFUSED", "EAI_AGAIN", "EPIPE", "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_HEADERS_TIMEOUT", "UND_ERR_BODY_TIMEOUT", "UND_ERR_SOCKET"]);

/** Find an HTTP status on an error or a returned response: .status, .statusCode, .status_code, or the same on .response. */
export function statusOf(x: any): number | undefined {
  const pick = (o: any) => [o?.status, o?.statusCode, o?.status_code, o?.code].find((v) => Number.isInteger(v) && v >= 100 && v <= 599);
  return pick(x) ?? pick(x?.response);
}
const isTransient = (e: any): boolean => {
  const s = statusOf(e);
  if (s !== undefined) return s >= 500;
  return ["TimeoutError", "AbortError"].includes(e?.name) || NET_CODES.has(e?.code) || NET_CODES.has(e?.cause?.code) || (e instanceof TypeError && e.message === "fetch failed");
};

export interface CallOptions {
  estimateUsd: number; project: string; provider: string; job?: string; ledger: Ledger;
  caps?: Caps; maxRetries?: number; retryDelayMs?: number;
  /** Read the real cost from the result. If it fails or gives a bad number, the estimate stays. */
  costFromResult?: (result: any) => number;
  env?: Env; sleep?: (ms: number) => Promise<void>;
}

/** Run `fn` only after every brake agrees. `fn` must throw on HTTP errors, or return an object with .status (fetch does). */
export async function guardedCall<T>(fn: () => Promise<T>, o: CallOptions): Promise<T> {
  const env = o.env ?? process.env, job = o.job ?? "default", maxRetries = o.maxRetries ?? 1;
  const estimate = money(o.estimateUsd, "estimateUsd");
  if (!Number.isInteger(maxRetries) || maxRetries < 0) throw new BrakeError("bad_setting", `maxRetries must be a whole number from 0 to ${HARD_MAX_RETRIES} (got ${String(maxRetries)}).`);
  if (maxRetries > HARD_MAX_RETRIES) throw new BrakeError("hard_ceiling", `maxRetries=${maxRetries} is above the hard ceiling of ${HARD_MAX_RETRIES}. The ceiling is written into the code on purpose, so no setting can raise it. To change it, edit HARD_MAX_RETRIES in this file and have that change reviewed.`);
  const caps = o.caps ?? capsFromEnv(o.project, env);
  const sleep = o.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const names = { project: o.project, provider: o.provider, job };

  const check = (t: Totals) => {
    if (t.jobCalls >= caps.maxCallsPerJob) throw new BrakeError("job_limit", `Stopped by the job limit: job "${job}" already has ${t.jobCalls} recorded calls (limit ${caps.maxCallsPerJob}, refused calls count too). Look at why the job needs more calls. To allow more on purpose: set SPEND_MAX_CALLS_PER_JOB or makeCaps({ maxCallsPerJob }), up to the hard ceiling written in the code (HARD_MAX_CALLS_PER_JOB), which no setting can raise.`);
    if (caps.projectUsd !== undefined && round6(t.projectUsd + estimate) > caps.projectUsd) throw new BrakeError("project_cap", `Stopped by the project cap: project "${o.project}" spent ${usd(t.projectUsd)} this month, and this call is about ${usd(estimate)}, which would pass the cap of ${usd(caps.projectUsd)}. To raise it on purpose: set SPEND_PROJECT_CAP_USD_${envWord(o.project)} or makeCaps({ projectUsd }).`);
    if (round6(t.monthUsd + estimate) > caps.monthlyUsd) throw new BrakeError("monthly_cap", `Stopped by the monthly cap: all projects spent ${usd(t.monthUsd)} this month, and this call is about ${usd(estimate)}, which would pass the cap of ${usd(caps.monthlyUsd)}. To raise it on purpose: set SPEND_MONTHLY_CAP_USD or makeCaps({ monthlyUsd }).`);
  };

  for (let attempt = 1; ; attempt++) {
    checkKillSwitch(o.provider, env);
    const id = await o.ledger.reserve({ ...names, usd: estimate }, check); // the estimate is on record before the call
    let result: T;
    try {
      result = await fn();
      const returned = [(result as any)?.status, (result as any)?.statusCode, (result as any)?.status_code].find((v) => Number.isInteger(v));
      if (returned !== undefined && (returned in NO_RETRY || returned >= 500)) throw Object.assign(new Error(`HTTP ${returned}`), { status: returned });
    } catch (error) {
      const status = statusOf(error);
      if (status !== undefined && status in NO_RETRY) {
        await o.ledger.settle({ id, ...names, usd: 0, outcome: "rejected" }); // refused before any work: nothing billed
        throw new BrakeError("no_retry", `Not retried: the provider answered ${status} (${NO_RETRY[status]}). A repeat cannot fix this and can make it worse. Fix the cause (key, payment, permission or call rate), then run it again on purpose.`, { status, cause: error });
      }
      await o.ledger.settle({ id, ...names, usd: estimate, outcome: "error" }); // may have been billed: keep the estimate
      if (!isTransient(error)) throw error;
      if (attempt > maxRetries) throw new BrakeError("retries_exhausted", `Gave up after ${attempt} attempt(s): the provider kept answering with a server error or a timeout. Every attempt is counted in the ledger as a possible cost. Check the provider's status before you run it again. To allow more tries on purpose: pass maxRetries (highest allowed: ${HARD_MAX_RETRIES}).`, { cause: error });
      await sleep((o.retryDelayMs ?? 2_000) * attempt);
      continue;
    }
    let cost = estimate;
    try { if (o.costFromResult) cost = money(o.costFromResult(result), "cost"); } catch { cost = estimate; }
    await o.ledger.settle({ id, ...names, usd: cost, outcome: "ok" }).catch(() => undefined); // the estimate is already on record
    return result;
  }
}
