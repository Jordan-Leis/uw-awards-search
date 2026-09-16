/**
 * AI search Worker for the Waterloo Awards Finder.
 *
 * POST /interpret  {"q": "bursaries for a 2nd year software engineering student"}
 *   -> {ok, filters:{career,level,awardType,term,affiliation,areaOfStudy},
 *       keywords, notes, source, dropped}
 *
 * Design notes worth knowing before editing:
 *
 * - The model NEVER sees award data. It only picks filter values; the browser
 *   applies them to data it already loaded. So it cannot invent an award, and
 *   each request costs ~1.2k tokens, which puts us far under Gemini's
 *   tokens-per-minute ceiling and makes requests-per-day the binding limit.
 * - The vocabulary is compiled in (vocab.generated.js), never accepted from the
 *   client. It IS the validation allowlist — taking it from the caller would
 *   let an attacker authorise their own values. validate() exact-matches every
 *   value against it; the response schema only makes the model's first guess
 *   more likely to be right.
 * - Talks to the Interactions API (v1beta/interactions), not generateContent.
 *   Google retired generateContent-era models for new accounts in 2026 and
 *   Gemini 3 accepts different parameters; see prompt.js and README.md. When
 *   Gemini misbehaves, run scripts/probe_gemini.py — do not debug via 502s.
 * - Every failure path is a graceful one: the site falls back to its existing
 *   keyword search, so an outage here is a small note, not a broken page.
 */
import { VOCAB, VOCAB_VERSION } from "./vocab.generated.js";
import { PROMPT_VERSION, buildRequestBody } from "./prompt.js";

const ALLOWED_ORIGINS = new Set([
  "https://jordanleis.com",
  "https://www.jordanleis.com",
  "https://jordan-leis.github.io",
]);

const MAX_QUESTION_CHARS = 300;
const MAX_BODY_BYTES = 1024;
// ~2x the p95 measured against the slower failover model (4.2s for
// gemini-3.1-flash-lite, Sept 2026; see scripts/probe_gemini.py). The browser
// side waits longer than this so a Worker timeout surfaces as a clean 502.
const GEMINI_TIMEOUT_MS = 8000;
const CACHE_TTL_SECONDS = 60 * 60 * 24 * 30; // 30 days

// Isolate-local throttles. These are leaky by construction (Cloudflare runs
// many isolates), but the case they exist to stop — one script hammering from
// one IP — lands in one isolate. Gemini's own 429 is the real backstop, and it
// is handled as a graceful fallback rather than an error.
const PER_IP_PER_MIN = 20;
const PER_IP_PER_DAY = 200;
// Lite models are 15 RPM on the free tier; leave headroom because a failover
// call shares the same minute.
const GLOBAL_GEMINI_PER_MIN = 12;

const ipMinute = new Map();
const ipDay = new Map();
let geminiCalls = [];

const FACULTY_WIDE_ALL = "All Programs";
const ENGINEERING_FACULTY = "Engineering Faculty - All Programs";

const FILTER_KEYS = ["career", "level", "awardType", "term", "affiliation", "areaOfStudy"];

// Vocabulary as Sets, built once per isolate, for O(1) exact-match validation.
const VOCAB_SETS = Object.fromEntries(
  FILTER_KEYS.map((key) => [key, new Set(VOCAB[key] || [])])
);

// A student can plausibly mean several programs; they cannot plausibly mean 30.
// A cap bounds both the response size and how much the model can pad.
const MAX_VALUES_PER_FILTER = { career: 2, level: 7, awardType: 6, term: 4, affiliation: 8, areaOfStudy: 30 };

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    const allowedOrigin = ALLOWED_ORIGINS.has(origin) ? origin : null;

    if (request.method === "OPTIONS") {
      return withCors(new Response(null, { status: 204 }), allowedOrigin);
    }
    if (request.method !== "POST") {
      return withCors(json({ ok: false, error: "method_not_allowed" }, 405), allowedOrigin);
    }

    const url = new URL(request.url);
    if (url.pathname !== "/interpret") {
      return withCors(json({ ok: false, error: "not_found" }, 404), allowedOrigin);
    }

    // An absent Origin header is a non-browser caller (curl, a script). Browsers
    // always send one on a cross-origin POST, so requiring it costs real users
    // nothing while keeping the endpoint off the list of open LLM proxies.
    if (!allowedOrigin) {
      return withCors(json({ ok: false, error: "forbidden_origin" }, 403), null);
    }

    if (env.ENABLED === "0") {
      return withCors(json({ ok: false, error: "disabled" }, 503), allowedOrigin);
    }

    let question;
    try {
      question = normalizeQuestion(await readBody(request));
    } catch (e) {
      return withCors(json({ ok: false, error: "bad_request" }, 400), allowedOrigin);
    }
    if (!question) {
      return withCors(json({ ok: false, error: "empty_question" }, 400), allowedOrigin);
    }

    const ip = request.headers.get("CF-Connecting-IP") || "unknown";
    if (!checkIpBudget(ip)) {
      return withCors(json({ ok: false, error: "rate_limited" }, 429), allowedOrigin);
    }

    // Cache entries are stored WITHOUT CORS headers and keyed only by the
    // question hash; CORS is attached on the way out. Caching the CORS headers
    // instead would let the first origin to warm a key poison it for the other.
    const cacheKey = await buildCacheKey(question);
    const cache = caches.default;
    const cached = await cache.match(cacheKey);
    if (cached) {
      const body = await cached.json();
      return withCors(json({ ...body, source: "cache" }, 200), allowedOrigin);
    }

    if (!checkGeminiBudget()) {
      return withCors(json({ ok: false, error: "rate_limited" }, 429), allowedOrigin);
    }

    let raw;
    try {
      raw = await callGemini(question, env);
    } catch (e) {
      return withCors(json({ ok: false, error: "upstream_unavailable" }, 502), allowedOrigin);
    }

    let result;
    try {
      result = validate(raw);
    } catch (e) {
      // Malformed / blocked / truncated model output. Not cached, so a
      // transient bad answer doesn't outlive itself.
      console.error(`gemini output rejected: ${e.message}`);
      return withCors(json({ ok: false, error: "upstream_unavailable" }, 502), allowedOrigin);
    }
    result.source = "ai";
    console.log(`gemini ok model=${raw._model} dropped=${result.dropped}`);

    // Cache successes only. A cached error would outlive the outage that caused it.
    ctx.waitUntil(
      cache.put(
        cacheKey,
        new Response(JSON.stringify(result), {
          headers: {
            "Content-Type": "application/json",
            "Cache-Control": `public, max-age=${CACHE_TTL_SECONDS}`,
          },
        })
      )
    );

    return withCors(json(result, 200), allowedOrigin);
  },
};

/* The pure helpers below are also exported so tests can exercise them without
   a Workers runtime. They are not part of the HTTP surface. */

/* ---------------------------------------------------------------- plumbing */

function json(body, status) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function withCors(response, allowedOrigin) {
  const r = new Response(response.body, response);
  if (allowedOrigin) {
    r.headers.set("Access-Control-Allow-Origin", allowedOrigin);
    r.headers.set("Access-Control-Allow-Methods", "POST, OPTIONS");
    r.headers.set("Access-Control-Allow-Headers", "Content-Type");
    r.headers.set("Access-Control-Max-Age", "86400");
  }
  // Set even when we echo nothing, so a shared cache can never hand one
  // origin's CORS headers to another.
  r.headers.set("Vary", "Origin");
  return r;
}

async function readBody(request) {
  const text = await request.text();
  if (new TextEncoder().encode(text).length > MAX_BODY_BYTES) throw new Error("too_large");
  const parsed = JSON.parse(text);
  if (!parsed || typeof parsed.q !== "string") throw new Error("bad_shape");
  return parsed.q;
}

// Filler openings students type that carry no filter signal. Stripping them
// merges "find me awards for CS students" and "awards for CS students" onto one
// cache entry. Conservative on purpose — over-normalising would collapse
// questions that genuinely differ.
const FILLER_PREFIX_RE =
  /^(?:please\s+|can\s+you\s+|could\s+you\s+|i(?:'m| am)\s+looking\s+for\s+|show\s+me\s+|find\s+me\s+|find\s+|give\s+me\s+|list\s+|search\s+for\s+|what\s+(?:awards|scholarships|bursaries)\s+(?:are\s+there\s+)?(?:for\s+)?)+/i;

export function normalizeQuestion(q) {
  let s = q.normalize("NFKC").replace(/\s+/g, " ").trim();
  if (s.length > MAX_QUESTION_CHARS) s = s.slice(0, MAX_QUESTION_CHARS);
  return s;
}

export function cacheNormalize(q) {
  return q.toLowerCase().replace(FILLER_PREFIX_RE, "").replace(/[?!.\s]+$/, "").trim();
}

export async function buildCacheKey(question) {
  const material = `${PROMPT_VERSION}|${VOCAB_VERSION}|${cacheNormalize(question)}`;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(material));
  const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  // A cache key must be a URL. This host is never resolved — it is only an
  // internal cache namespace.
  return new Request(`https://ai-search-cache.invalid/q/${hex}`, { method: "GET" });
}

/* --------------------------------------------------------------- throttles */

function bump(map, key, windowMs, limit) {
  const now = Date.now();
  const entry = map.get(key);
  if (!entry || now - entry.start > windowMs) {
    map.set(key, { start: now, count: 1 });
    // Keep the isolate's memory bounded; these maps are otherwise unbounded
    // under a distributed flood.
    if (map.size > 5000) {
      for (const [k, v] of map) {
        if (now - v.start > windowMs) map.delete(k);
      }
    }
    return true;
  }
  entry.count += 1;
  return entry.count <= limit;
}

function checkIpBudget(ip) {
  const perMin = bump(ipMinute, ip, 60_000, PER_IP_PER_MIN);
  const perDay = bump(ipDay, ip, 86_400_000, PER_IP_PER_DAY);
  return perMin && perDay;
}

function checkGeminiBudget() {
  const now = Date.now();
  geminiCalls = geminiCalls.filter((t) => now - t < 60_000);
  if (geminiCalls.length >= GLOBAL_GEMINI_PER_MIN) return false;
  geminiCalls.push(now);
  return true;
}

/* ------------------------------------------------------------------ gemini */

const GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions";
const DEFAULT_MODELS = "gemini-3.5-flash-lite,gemini-3.1-flash-lite";

// Statuses on which it is worth trying the next model in the list: quota
// exhausted, or the model has been retired. Anything else (400, 5xx, timeout)
// would fail the same way on every model and only burn quota twice.
const FAILOVER_STATUSES = new Set([429, 404]);

/**
 * Try each configured model in order. On the free tier every model has its
 * own requests-per-day pool, so a second Lite model is a second 500/day for
 * free — and when Google retires one (they retired 2.5-flash for new accounts
 * with a 404 in Sept 2026), the site keeps working on the next.
 */
async function callGemini(question, env) {
  const models = (env.GEMINI_MODEL || DEFAULT_MODELS).split(",").map((m) => m.trim()).filter(Boolean);
  let lastError = null;
  for (const model of models) {
    try {
      return await callGeminiModel(question, model, env);
    } catch (e) {
      lastError = e;
      if (!FAILOVER_STATUSES.has(e.status)) throw e;
      console.warn(`gemini ${e.status} on ${model}; trying next model`);
    }
  }
  throw lastError || new Error("no_models_configured");
}

async function callGeminiModel(question, model, env) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), GEMINI_TIMEOUT_MS);
  try {
    const response = await fetch(GEMINI_ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // Header, not a query param, so the key can't end up in a URL log.
        "x-goog-api-key": env.GEMINI_API_KEY,
      },
      body: JSON.stringify(buildRequestBody(question, model)),
      signal: controller.signal,
    });
    if (!response.ok) {
      // Surface Google's reason in `wrangler tail`. The body is Google's error
      // JSON (code + message); the API key travels in a header and is never
      // echoed back, so this is safe to log. Truncated: quota errors include
      // long retry-info blobs.
      const detail = (await response.text().catch(() => "")).slice(0, 600);
      console.error(`gemini ${response.status} for model=${model}: ${detail}`);
      const err = new Error(`gemini_${response.status}`);
      err.status = response.status;
      throw err;
    }
    const payload = await response.json();
    payload._model = model;
    return payload;
  } catch (e) {
    if (e && e.name === "AbortError") console.error(`gemini timeout after ${GEMINI_TIMEOUT_MS}ms on ${model}`);
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

/* -------------------------------------------------------------- validation */

/**
 * Treat the model as untrusted even though the schema guides it: a 200 can
 * still carry a safety block, a truncation, or malformed JSON — and the docs
 * themselves say "always validate values in your application".
 *
 * Interactions response shape (https://ai.google.dev/api/interactions-api):
 *   { status: "completed" | "incomplete" | "failed" | ...,
 *     steps: [{ type: "model_output", content: [{ type: "text", text }] }, ...] }
 * "incomplete" means max_output_tokens was hit; "failed" carries `errors`.
 */
export function validate(payload) {
  if (!payload || typeof payload !== "object") throw new Error("no_payload");
  if (payload.status !== "completed") {
    console.error(`gemini interaction status=${payload.status}`
      + (payload.errors ? ` errors=${JSON.stringify(payload.errors).slice(0, 300)}` : ""));
    throw new Error(`status_${payload.status || "missing"}`);
  }

  let text = null;
  for (const step of payload.steps || []) {
    if (step && step.type === "model_output") {
      for (const c of step.content || []) {
        if (c && c.type === "text" && typeof c.text === "string") { text = c.text; break; }
      }
    }
    if (text !== null) break;
  }
  if (text === null) throw new Error("no_text_output");

  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    throw new Error("unparseable_json");
  }
  if (!parsed || typeof parsed !== "object") throw new Error("not_an_object");

  const filters = {};
  let dropped = 0;
  for (const key of FILTER_KEYS) {
    const raw = Array.isArray(parsed[key]) ? parsed[key] : [];
    const allowed = VOCAB_SETS[key];
    const seen = new Set();
    for (const value of raw) {
      // Exact, case-sensitive. These strings are compared with === against the
      // award data in site/js/search.js, so anything else is a silent no-match.
      if (typeof value === "string" && allowed.has(value) && !seen.has(value)) {
        seen.add(value);
      } else {
        dropped += 1;
      }
    }
    const capped = [...seen].slice(0, MAX_VALUES_PER_FILTER[key]);
    dropped += seen.size - capped.length;
    filters[key] = capped;
  }

  const expanded = expandAreas(filters);

  return {
    ok: true,
    filters,
    keywords: sanitizeKeywords(parsed.keywords),
    notes: expanded ? "area_expanded" : "",
    dropped,
  };
}

/**
 * Deterministic widening applied AFTER validation, so the model cannot
 * influence it.
 *
 * Awards are tagged with a specific program, "<Faculty> Faculty - All
 * Programs", or "All Programs" — a student matches all three. The system
 * prompt asks the model to do this expansion itself (it knows which faculty a
 * program sits in; the data does not), but if it forgets, this floor means the
 * student gets a narrower result rather than a wrong one.
 */
export function expandAreas(filters) {
  const areas = filters.areaOfStudy;
  if (areas.length === 0) return false;

  const additions = [];
  if (!areas.includes(FACULTY_WIDE_ALL)) additions.push(FACULTY_WIDE_ALL);
  // "<Faculty> Faculty - All Programs" entries don't end in "Engineering", so
  // this only fires for actual programs.
  if (areas.some((a) => a.endsWith("Engineering")) && !areas.includes(ENGINEERING_FACULTY)) {
    additions.push(ENGINEERING_FACULTY);
  }

  const valid = additions.filter((a) => VOCAB_SETS.areaOfStudy.has(a));
  if (valid.length === 0) return false;

  // Expansion runs after the per-filter cap, so it has to respect that cap
  // itself or the response can exceed it. Trim the model's own values to make
  // room: the expansion entries are the ones that must survive, since they are
  // what stops the student's eligible pool being silently cut to a third.
  const room = Math.max(0, MAX_VALUES_PER_FILTER.areaOfStudy - valid.length);
  filters.areaOfStudy = [...areas.slice(0, room), ...valid];
  return true;
}

/**
 * The only attacker-influenced text in the response. It is handed to Fuse.js in
 * the browser and rendered into a chip, so it is stripped to a boring charset
 * and hard-capped. Keep it that way.
 */
export function sanitizeKeywords(value) {
  if (typeof value !== "string") return "";
  return value
    .toLowerCase()
    .replace(/[^a-z0-9 +\-']/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 64)
    .split(" ")
    .slice(0, 5)
    .join(" ");
}
