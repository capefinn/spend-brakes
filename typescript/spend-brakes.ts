// spend-brakes.ts - spend brakes for Node and serverless code that call paid APIs. One file, no dependencies.
// Same five brakes and the same ledger row format as the Python package, so both can share one ledger file.
// Brakes, checked before EVERY attempt: kill switch, job limit, project cap, monthly cap. Then the retry rules.
import { appendFileSync, closeSync, existsSync, fstatSync, mkdirSync, openSync, readFileSync, readSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";

// Part of the code on purpose: no argument and no environment variable can raise these two numbers.
export const HARD_MAX_CALLS_PER_JOB = 20;
export const HARD_MAX_RETRIES = 3;
const DEFAULT_MONTHLY_CAP_USD = 20; // an example number: set your own
const DEFAULT_MAX_CALLS_PER_JOB = 6;

type Env = Record<string, string | undefined>;
export type BrakeName = "kill_switch" | "monthly_cap" | "project_cap" | "job_limit" | "hard_ceiling" | "no_retry" | "retries_exhausted" | "ledger_unreadable" | "bad_setting";

/** Every stop is a BrakeError. `.brake` says which brake; the message names the setting, and the owner decides. */
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
const MAX_NAME_LENGTH = 200; // keeps every ledger row short
/** The key of a project or provider name. It is the name inside the setting names, and it is what totals count by.
 *  Steps: Unicode NFKC normalisation, then case-folding (toLowerCase, and NFKC once more, so that Python and TypeScript
 *  give the same key when case-folding splits a letter into a letter and a mark), then every run of characters that are
 *  not Unicode letters, digits or marks becomes one "_", the "_" at the start and the end is removed, and the result is
 *  written in capital letters. A mark (a vowel sign in Hindi or Thai, a vowel mark in Arabic, an accent) is part of the
 *  word, so two names that differ only by a mark have two keys. So "blog-images", "Blog_Images" and "BLOG IMAGES" all
 *  give "BLOG_IMAGES", and "Kuvat äänet" gives "KUVAT_ÄÄNET" whether the ä is one character or a letter plus a mark.
 *  The Python function project_key gives the same result. It gives "" for a name with no letter and no digit (only
 *  punctuation, spaces, emoji or marks); guardedCall and checkCall refuse such a name with a bad_setting error.
 *  A Ledger you write yourself must count the project total and the job count by this key. */
export const projectKey = (name: string): string => {
  if (typeof name !== "string") throw new BrakeError("bad_setting", "A project or provider name must be plain text (a string).");
  const key = name.normalize("NFKC").toLowerCase().normalize("NFKC").replace(/[^\p{L}\p{N}\p{M}]+/gu, "_").replace(/^_+|_+$/g, "").toUpperCase();
  return /[\p{L}\p{N}]/u.test(key) ? key : "";
};
const envWord = (name: string, label = "name") => {
  const w = projectKey(name);
  if (!w) throw new BrakeError("bad_setting", `The ${label} "${name}" has no letter and no digit, so it cannot be used. Use a name with at least one letter or digit, for example "blog-images".`);
  return w;
};
const money = (v: unknown, label: string): number => {
  if (typeof v !== "number" || !Number.isFinite(v) || v < 0) throw new BrakeError("bad_setting", `${label} must be a number that is zero or more (got ${String(v)}).`);
  return v;
};

// ---- caps -----------------------------------------------------------------------------------------------------
export interface Caps { readonly monthlyUsd: number; readonly projectUsd?: number; readonly maxCallsPerJob: number }

// The one place where caps are checked. Values are passed in (not an object), so each field of a caller's object is read once.
function buildCaps(monthlyUsd: unknown, projectUsd: unknown, maxCallsPerJob: unknown): Caps {
  const n = maxCallsPerJob;
  if (typeof n !== "number" || !Number.isInteger(n) || n < 1) throw new BrakeError("bad_setting", `maxCallsPerJob must be a whole number of 1 or more (got ${String(n)}).`);
  if (n > HARD_MAX_CALLS_PER_JOB) {
    throw new BrakeError("hard_ceiling", `maxCallsPerJob=${n} is above the hard ceiling of ${HARD_MAX_CALLS_PER_JOB}. The ceiling is written into the code on purpose, so no setting can raise it. Only the owner can change it, in the code (HARD_MAX_CALLS_PER_JOB in this file), with a review.`);
  }
  return Object.freeze({
    monthlyUsd: money(monthlyUsd, "monthlyUsd"),
    projectUsd: projectUsd === undefined ? undefined : money(projectUsd, "projectUsd"),
    maxCallsPerJob: n,
  });
}

export function makeCaps(c: { monthlyUsd?: number; projectUsd?: number; maxCallsPerJob?: number } = {}): Caps {
  return buildCaps(c.monthlyUsd ?? DEFAULT_MONTHLY_CAP_USD, c.projectUsd, c.maxCallsPerJob ?? DEFAULT_MAX_CALLS_PER_JOB);
}

/** Check caps that come from anywhere (a plain object, `as Caps`, a getter): the same rules as makeCaps, but nothing
 *  is filled in with a default. Every field is read once, and the result is a new frozen Caps made of plain numbers. */
function checkCaps(c: unknown): Caps {
  if (typeof c !== "object" || c === null) throw new BrakeError("bad_setting", `caps must be an object with monthlyUsd, projectUsd and maxCallsPerJob, for example from makeCaps() (got ${String(c)}).`);
  const f = c as Record<string, unknown>;
  return buildCaps(f.monthlyUsd, f.projectUsd, f.maxCallsPerJob);
}

/** Read SPEND_MONTHLY_CAP_USD, SPEND_PROJECT_CAP_USD_<PROJECT> and SPEND_MAX_CALLS_PER_JOB. */
export function capsFromEnv(project: string, env: Env = process.env): Caps {
  const num = (name: string): number | undefined => {
    const raw = (env[name] ?? "").trim();
    if (!raw) return undefined;
    const v = /^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(raw) ? Number(raw) : Number.NaN;
    if (!Number.isFinite(v)) throw new BrakeError("bad_setting", `${name}="${raw}" is not a number. Set it to a plain number such as 20.`);
    return v;
  };
  return makeCaps({ monthlyUsd: num("SPEND_MONTHLY_CAP_USD"), projectUsd: num(`SPEND_PROJECT_CAP_USD_${envWord(project, "project")}`), maxCallsPerJob: num("SPEND_MAX_CALLS_PER_JOB") });
}

// ---- kill switch ----------------------------------------------------------------------------------------------
// SPEND_PANIC=1 stops everything. SPEND_<PROVIDER>_ENABLED=0 stops one provider. If the file named in
// SPEND_STOP_FILE exists, everything stops. When SPEND_STOP_FILE is not set, `defaultStopFile` is used: for a file
// ledger it is a file named STOP in the same folder as the ledger file (a ledger that is not a file has none).
// A value that cannot be read counts as STOP (the safe choice).
export function checkKillSwitch(provider: string, env: Env = process.env, defaultStopFile?: string): void {
  const stop = (m: string) => { throw new BrakeError("kill_switch", `Stopped by the kill switch: ${m}`); };
  const word = (name: string) => (env[name] ?? "").trim().toLowerCase();
  const ON = ["1", "true", "on", "yes"], OFF = ["0", "false", "off", "no"];

  const panic = word("SPEND_PANIC");
  if (ON.includes(panic)) stop("SPEND_PANIC is on, so no paid call is allowed. Only the owner turns it off (setting: SPEND_PANIC).");
  if (panic && !OFF.includes(panic)) stop(`SPEND_PANIC="${panic}" cannot be read, so it counts as ON (the safe choice). Ask the owner to fix the value (setting: SPEND_PANIC; 1 means stop, 0 means go).`);

  const file = (env.SPEND_STOP_FILE ?? "").trim();
  if (file) {
    if (existsSync(file)) stop(`the stop file named in SPEND_STOP_FILE exists (${file}). Only the owner removes that file.`);
  } else if (defaultStopFile && existsSync(defaultStopFile)) {
    stop(`the default stop file exists (${defaultStopFile}). It sits in the same folder as the ledger file. Only the owner removes that file (to use another file, set SPEND_STOP_FILE).`);
  }

  const name = `SPEND_${envWord(provider, "provider")}_ENABLED`, value = word(name);
  if (!value || ON.includes(value)) return;
  if (OFF.includes(value)) stop(`${name} is ${value}, so provider "${provider}" is switched off. Only the owner turns it on again (setting: ${name}).`);
  stop(`${name}="${value}" cannot be read, so provider "${provider}" counts as OFF (the safe choice). Ask the owner to fix the value (setting: ${name}; 1 means go, 0 means stop).`);
}

// ---- ledger ---------------------------------------------------------------------------------------------------
// Each call writes a "reserve" row BEFORE the call (the estimate) and a "settle" row AFTER (the real cost, or 0 when
// refused). The cost of a call is the usd of its last row. Rows are only ever added. Same format as the Python package.
export interface Row { t: string; id: string; event: "reserve" | "settle"; project: string; provider: string; job: string; usd: number; outcome?: string }
export interface Totals { monthUsd: number; projectUsd: number; jobCalls: number }
export interface Ledger {
  /** Must be atomic: read the totals, let `check` throw to refuse, then record the estimate. Returns the call id.
   *  Count the project total and the job count by `projectKey(project)`, not by the exact text (see `totalsOf` below). */
  reserve(e: { project: string; provider: string; job: string; usd: number }, check: (t: Totals) => void): Promise<string>;
  settle(e: { id: string; project: string; provider: string; job: string; usd: number; outcome: "ok" | "rejected" | "error" }): Promise<void>;
  /** The read-only twin of `reserve`, used by `checkCall`: read the totals and let `check` throw to refuse. Write nothing. */
  preview(e: { project: string; provider: string; job: string; usd: number }, check: (t: Totals) => void): Promise<void>;
  /** The default stop file: a file named STOP in the same folder as the ledger file. Only a file ledger has one. */
  readonly stopFile?: string;
}

// A row we can count with: every field the totals use must be there and make sense.
const rowOk = (r: any): r is Row => !!r && typeof r === "object" && !Array.isArray(r) && (r.event === "reserve" || r.event === "settle")
  && typeof r.id === "string" && typeof r.project === "string" && typeof r.job === "string"
  && typeof r.t === "string" && /^\d{4}-\d{2}-\d{2}T/.test(r.t) && typeof r.usd === "number" && Number.isFinite(r.usd) && r.usd >= 0;

/** A new id for every run of a script, to use inside a job name: job: "batch-" + runId(). */
export const runId = (): string => new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z") + "-" + globalThis.crypto.randomUUID().slice(0, 6);

/** Every name must be plain text (typeof "string") of 1 to 200 characters. A project and a provider name must also have
 *  a key (see projectKey). Each value is read once, and the result holds plain strings only. This runs on every call,
 *  whether or not a cap is set. */
function checkNames(e: { project: unknown; provider: unknown; job: unknown }): { project: string; provider: string; job: string } {
  const project = e.project, provider = e.provider, job = e.job; // each is read ONCE
  for (const [label, v] of [["project", project], ["provider", provider], ["job", job]] as const) {
    if (typeof v !== "string") throw new BrakeError("bad_setting", `${label} must be plain text (a string).`);
    if (!v || v.length > MAX_NAME_LENGTH) throw new BrakeError("bad_setting", `${label} must be a name of 1 to ${MAX_NAME_LENGTH} characters.`);
  }
  envWord(project as string, "project");
  envWord(provider as string, "provider");
  return { project: project as string, provider: provider as string, job: job as string };
}

function totalsOf(rows: Row[], project: string, job: string, month: string): Totals {
  const calls = new Map<string, { month: string; project: string; job: string; usd: number }>();
  for (const r of rows) {
    const c = calls.get(r.id) ?? { month: String(r.t ?? "").slice(0, 7), project: r.project, job: r.job, usd: r.usd };
    if (r.event === "settle") c.usd = r.usd;
    calls.set(r.id, c);
  }
  let monthUsd = 0, projectUsd = 0, jobCalls = 0;
  const wanted = projectKey(project); // rows keep the original text; the counting uses the key
  for (const c of calls.values()) {
    const sameProject = projectKey(c.project) === wanted;
    if (sameProject && c.job === job) jobCalls++;
    if (c.month === month) { monthUsd += c.usd; if (sameProject) projectUsd += c.usd; }
  }
  return { monthUsd: round6(monthUsd), projectUsd: round6(projectUsd), jobCalls };
}

// The work inside reserve is synchronous (read, check, append), so in one process two calls cannot interleave between
// the check and the record. A Ledger over an async store (a database) must give the same guarantee itself.
function rowsLedger(store: { read(): Row[]; append(row: Row): void }, now: () => Date): Ledger {
  return {
    reserve: async (e, check) => {
      const names = checkNames(e), usd = money(e.usd, "estimateUsd"); // each value is read once; only the plain copies are used below
      check(totalsOf(store.read(), names.project, names.job, now().toISOString().slice(0, 7)));
      const id = globalThis.crypto.randomUUID().replace(/-/g, "");
      store.append({ t: now().toISOString(), id, event: "reserve", ...names, usd });
      return id;
    },
    settle: async (e) => store.append({ t: now().toISOString(), event: "settle", ...e }),
    // Read only: the same name check, the same rule for unreadable lines and the same `check`, but nothing is written.
    preview: async (e, check) => {
      const names = checkNames(e);
      money(e.usd, "estimateUsd");
      check(totalsOf(store.read(), names.project, names.job, now().toISOString().slice(0, 7)));
    },
  };
}

/** Rows kept in memory: for tests and for one running process. Nothing is shared between processes. */
export function memoryLedger(now: () => Date = () => new Date()): Ledger & { rows: Row[] } {
  const rows: Row[] = [];
  return { ...rowsLedger({ read: () => rows, append: (r) => void rows.push(r) }, now), rows };
}

/** The ledger file when you give no other: SPEND_BRAKES_LEDGER, else `.spend-brakes/ledger.jsonl` in your home folder.
 *  The same rule as the Python package, so a Node program and a Python script share one ledger, and the default stop
 *  file is the same file (`.spend-brakes/STOP` in your home folder) for both. */
export function defaultLedgerPath(env: Env = process.env): string {
  const custom = (env.SPEND_BRAKES_LEDGER ?? "").trim();
  return custom || join(homedir(), ".spend-brakes", "ledger.jsonl");
}

// SPEND_BRAKES_SKIP_UNREADABLE: 1/true/on/yes = skip, 0/false/off/no or empty = do not skip, anything else is refused.
function skipFromEnv(env: Env): boolean {
  const raw = (env.SPEND_BRAKES_SKIP_UNREADABLE ?? "").trim().toLowerCase();
  if (["", "0", "false", "off", "no"].includes(raw)) return false;
  if (["1", "true", "on", "yes"].includes(raw)) return true;
  throw new BrakeError("bad_setting", `SPEND_BRAKES_SKIP_UNREADABLE="${raw}" cannot be read. Use 1 or 0.`);
}

const skipWarned = new Set<string>(); // ledger files whose skip warning was shown: once per program for each file, like the Python package

/** A JSONL file. Every row is one append (the system adds the whole line or none of it). Check-and-record is safe
 *  inside one process only; for several processes or servers, write a Ledger over a database transaction instead.
 *  A line that cannot be read means the real spend is not known, so by default every call is REFUSED (ledger_unreadable)
 *  until the owner repairs the file. Skipping such lines is an explicit choice: { skipUnreadable: true } or
 *  SPEND_BRAKES_SKIP_UNREADABLE=1. A row after a cut-off line starts on a new line, so it is never hidden.
 *  A file named STOP in the same folder as the ledger file is the default stop file (SPEND_STOP_FILE replaces it). */
export function fileLedger(path: string, opts: { now?: () => Date; skipUnreadable?: boolean } = {}): Ledger {
  // An explicit option wins, and it must be a real true or false: the text "false" or the number 0 would be an easy way to
  // switch skipping ON by mistake. Left out (undefined), SPEND_BRAKES_SKIP_UNREADABLE decides.
  const option: unknown = opts.skipUnreadable; // read once
  if (option !== undefined && typeof option !== "boolean") throw new BrakeError("bad_setting", `skipUnreadable must be true or false (a real boolean), or left out to use SPEND_BRAKES_SKIP_UNREADABLE (got ${String(option)}).`);
  const skip = option ?? skipFromEnv(process.env);
  const read = (): Row[] => {
    if (!existsSync(path)) return [];
    const rows: Row[] = [], bad: number[] = [];
    readFileSync(path, "utf8").split("\n").forEach((line, i) => {
      if (!line.trim()) return;
      try { const r = JSON.parse(line); if (rowOk(r)) rows.push(r); else bad.push(i + 1); } catch { bad.push(i + 1); }
    });
    if (bad.length && skip && !skipWarned.has(resolve(path))) {
      skipWarned.add(resolve(path));
      console.warn(`spend_brakes: skipping ${bad.length} unreadable ledger line(s) in ${path}; their spend is NOT counted.`);
    }
    if (bad.length && !skip) {
      throw new BrakeError("ledger_unreadable", `Stopped: the ledger file ${path} has ${bad.length} line(s) that cannot be read (line ${bad.slice(0, 5).join(", ")}${bad.length > 5 ? " and more" : ""}). With an unreadable line the real spend is not known, so no paid call is allowed. To repair it, ask the owner to make a copy of the file, then fix or remove only the damaged line(s). Skipping them is the owner's choice and not a repair: the money on those lines is then not counted (setting: SPEND_BRAKES_SKIP_UNREADABLE).`);
    }
    return rows;
  };
  const append = (row: Row) => {
    mkdirSync(dirname(path), { recursive: true });
    let line = JSON.stringify(row) + "\n";
    if (existsSync(path)) { // an earlier write may have been cut off without its end-of-line mark
      const fd = openSync(path, "r");
      try { const size = fstatSync(fd).size, last = Buffer.alloc(1); if (size > 0 && readSync(fd, last, 0, 1, size - 1) === 1 && last[0] !== 10) line = "\n" + line; } finally { closeSync(fd); }
    }
    appendFileSync(path, line, { mode: 0o600 });
  };
  return { ...rowsLedger({ read, append }, opts.now ?? (() => new Date())), stopFile: join(dirname(path), "STOP") };
}

// ---- guarded call ---------------------------------------------------------------------------------------------
const NO_RETRY: Record<number, string> = { 401: "the key was not accepted", 402: "payment is required: no money or a blocked plan", 403: "the call is not allowed", 429: "rate limit or quota reached" };
const NET_CODES = new Set(["ECONNRESET", "ETIMEDOUT", "ECONNREFUSED", "EAI_AGAIN", "EPIPE", "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_HEADERS_TIMEOUT", "UND_ERR_BODY_TIMEOUT", "UND_ERR_SOCKET"]);

/** Find an HTTP status on an error or a returned response: .status, .statusCode, .status_code, or the same on .response. */
export function statusOf(x: any): number | undefined {
  const pick = (o: any) => [o?.status, o?.statusCode, o?.status_code, o?.code].find((v) => Number.isInteger(v) && v >= 100 && v <= 599);
  return pick(x) ?? pick(x?.response);
}
// An AbortError means the caller cancelled on purpose: never retry it. A TimeoutError is retried.
const isTransient = (e: any): boolean => {
  const s = statusOf(e);
  if (s !== undefined) return s >= 500;
  return e?.name === "TimeoutError" || NET_CODES.has(e?.code) || NET_CODES.has(e?.cause?.code) || (e instanceof TypeError && e.message === "fetch failed");
};

export interface CallOptions {
  estimateUsd: number; project: string; provider: string; ledger: Ledger;
  /** Required, and NEW for every run of your script, for example "batch-" + runId(). A name that is used again keeps its old call count. */
  job: string;
  caps?: Caps; maxRetries?: number; retryDelayMs?: number;
  /** Read the real cost from the result. If it fails or gives a bad number, the estimate stays. */
  costFromResult?: (result: any) => number;
  env?: Env; sleep?: (ms: number) => Promise<void>;
}

// Check the options and build the brake check. guardedCall and checkCall both use it, so both run the same brakes.
// Every option is read ONCE, here, into a local constant. Nothing after this line reads `o` again, so a getter or a Proxy
// cannot show one name to the cap check and another to the ledger, or one number to the check and another to the call.
function prepare(o: CallOptions) {
  const project = o.project, provider = o.provider, job = o.job, ledger = o.ledger;
  const estimateOpt = o.estimateUsd, retriesOpt = o.maxRetries, delayOpt = o.retryDelayMs, capsOpt = o.caps;
  const envOpt = o.env, sleepOpt = o.sleep, costFromResult = o.costFromResult;

  const env = envOpt ?? process.env, maxRetries = retriesOpt === undefined ? 1 : retriesOpt;
  if (typeof job !== "string" || !job) throw new BrakeError("bad_setting", "job is required: a name that is new for every run of your script, for example \"batch-\" + runId().");
  checkNames({ project, provider, job }); // on every call, whether or not a cap is set
  if (typeof ledger !== "object" || ledger === null || typeof ledger.reserve !== "function" || typeof ledger.settle !== "function" || typeof ledger.preview !== "function") {
    throw new BrakeError("bad_setting", "ledger is required: use memoryLedger() or fileLedger(path), or your own Ledger with reserve, settle and preview.");
  }
  const stopFile = ledger.stopFile;
  const estimate = money(estimateOpt, "estimateUsd");
  if (!Number.isInteger(maxRetries) || maxRetries < 0) throw new BrakeError("bad_setting", `maxRetries must be a whole number from 0 to ${HARD_MAX_RETRIES} (got ${String(maxRetries)}).`);
  if (maxRetries > HARD_MAX_RETRIES) throw new BrakeError("hard_ceiling", `maxRetries=${maxRetries} is above the hard ceiling of ${HARD_MAX_RETRIES}. The ceiling is written into the code on purpose, so no setting can raise it. Only the owner can change it, in the code (HARD_MAX_RETRIES in this file), with a review.`);
  const retryDelayMs = money(delayOpt === undefined ? 2_000 : delayOpt, "retryDelayMs");
  // Caps from any source are checked here, so a plain object or a NaN cannot switch a cap off. Values are read once.
  const caps = checkCaps(capsOpt ?? capsFromEnv(project, env));
  const jobLimit = Math.min(caps.maxCallsPerJob, HARD_MAX_CALLS_PER_JOB); // the last guard: never above the ceiling in the code

  const check = (t: Totals) => {
    const jobCalls = Number(t.jobCalls), projectUsd = Number(t.projectUsd), monthUsd = Number(t.monthUsd); // plain numbers, from the ledger
    if (jobCalls >= jobLimit) throw new BrakeError("job_limit", `Stopped by the job limit: job "${job}" already has ${jobCalls} recorded calls (limit ${jobLimit}, refused calls count too). Look at why the job needs more calls. Ask the owner if the limit should change (setting: SPEND_MAX_CALLS_PER_JOB); the hard ceiling in the code cannot be raised by a setting.`);
    if (caps.projectUsd !== undefined && round6(projectUsd + estimate) > caps.projectUsd) throw new BrakeError("project_cap", `Stopped by the project cap: project "${project}" spent ${usd(projectUsd)} this month, and this call is about ${usd(estimate)}, which would exceed the cap of ${usd(caps.projectUsd)}. Ask the owner to raise it (setting: SPEND_PROJECT_CAP_USD_${envWord(project, "project")}).`);
    if (round6(monthUsd + estimate) > caps.monthlyUsd) throw new BrakeError("monthly_cap", `Stopped by the monthly cap: all projects spent ${usd(monthUsd)} this month, and this call is about ${usd(estimate)}, which would exceed the cap of ${usd(caps.monthlyUsd)}. Ask the owner to raise it (setting: SPEND_MONTHLY_CAP_USD).`);
  };
  return { env, project, provider, job, ledger, stopFile, estimate, maxRetries, retryDelayMs, check, sleepOpt, costFromResult };
}

/** Run `fn` only after every brake agrees. `fn` must throw on HTTP errors, or return an object with .status (fetch does). */
export async function guardedCall<T>(fn: () => Promise<T>, o: CallOptions): Promise<T> {
  const { env, project, provider, job, ledger, stopFile, estimate, maxRetries, retryDelayMs, check, sleepOpt, costFromResult } = prepare(o);
  const sleep = sleepOpt ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const names = { project, provider, job };

  for (let attempt = 1; ; attempt++) {
    checkKillSwitch(provider, env, stopFile);
    const id = await ledger.reserve({ ...names, usd: estimate }, check); // the estimate is on record before the call
    let result: T;
    try {
      result = await fn();
      const returned = [(result as any)?.status, (result as any)?.statusCode, (result as any)?.status_code].find((v) => Number.isInteger(v));
      if (returned !== undefined && (returned in NO_RETRY || returned >= 500)) throw Object.assign(new Error(`HTTP ${returned}`), { status: returned });
    } catch (error) {
      const status = statusOf(error);
      if (status !== undefined && status in NO_RETRY) {
        await ledger.settle({ id, ...names, usd: 0, outcome: "rejected" }); // refused before any work: nothing billed
        throw new BrakeError("no_retry", `Not retried: the provider answered ${status} (${NO_RETRY[status]}). A repeat cannot fix this and can make it worse. The owner must fix the cause (key, payment, permission or call rate) before the next run.`, { status, cause: error });
      }
      await ledger.settle({ id, ...names, usd: estimate, outcome: "error" }); // may have been billed: keep the estimate
      if (!isTransient(error)) throw error;
      if (attempt > maxRetries) throw new BrakeError("retries_exhausted", `Gave up after ${attempt} attempt(s): the provider kept answering with a server error or a timeout. Every attempt is counted in the ledger as a possible cost. Check the provider's status before you run it again. More tries are the owner's choice: raise the maxRetries option of guardedCall (it is an option, not an environment setting; the ceiling is ${HARD_MAX_RETRIES}).`, { cause: error });
      await sleep(retryDelayMs * attempt);
      continue;
    }
    let cost = estimate;
    try { if (costFromResult) cost = money(costFromResult(result), "cost"); } catch { cost = estimate; }
    await ledger.settle({ id, ...names, usd: cost, outcome: "ok" }).catch(() => undefined); // the estimate is already on record
    return result;
  }
}

/** Ask: would `guardedCall` allow this call now? Read-only. Use it for a --dry-run.
 *  It takes the same options as `guardedCall` (without the function) and runs the same brakes in the same order: the
 *  options, the kill switch, the job limit, the project cap and the monthly cap. When a brake would stop the call it
 *  throws the same BrakeError with the same message. When all brakes agree it returns.
 *  It writes nothing: no row in the ledger (so it never counts for the job limit). It checks the first attempt only.
 *  It does not call the provider. `costFromResult`, `sleep` and `retryDelayMs` are accepted so that the same options
 *  work for both functions; they have no effect here. */
export async function checkCall(o: CallOptions): Promise<void> {
  const { env, project, provider, job, ledger, stopFile, estimate, check } = prepare(o);
  checkKillSwitch(provider, env, stopFile);
  await ledger.preview({ project, provider, job, usd: estimate }, check);
}
