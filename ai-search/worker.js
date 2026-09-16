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
 *   each request costs ~1.5k tokens, which puts us far under Gemini's
 *   tokens-per-minute ceiling and makes requests-per-day the binding limit.
 * - The vocabulary is compiled in (vocab.generated.js), never accepted from the
 *   client. It IS the validation allowlist — taking it from the caller would
 *   let an attacker authorise their own values.
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
const GEMINI_TIMEOUT_MS = 6000;
const CACHE_TTL_SECONDS = 60 * 60 * 24 * 30; // 30 days

// Isolate-local throttles. These are leaky by construction (Cloudflare runs
// many isolates), but the case they exist to stop — one script hammering from
// one IP — lands in one isolate. Gemini's own 429 is the real backstop, and it
// is handled as a graceful fallback rather than an error.
const PER_IP_PER_MIN = 20;
const PER_IP_PER_DAY = 200;
const GLOBAL_GEMINI_PER_MIN = 8;

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

    const result = validate(raw);
    result.source = "ai";

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

async function callGemini(question, env) {
  const model = env.GEMINI_MODEL || "gemini-2.5-flash";
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), GEMINI_TIMEOUT_MS);
  try {
    const response = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          // Header, not a query param, so the key can't end up in a URL log.
          "x-goog-api-key": env.GEMINI_API_KEY,
        },
        body: JSON.stringify(buildRequestBody(question)),
        signal: controller.signal,
      }
    );
    if (!response.ok) throw new Error(`gemini_${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

/* -------------------------------------------------------------- validation */

/**
 * Treat the model as untrusted even though the schema constrains it: a 200 can
 * still carry a safety block, a truncation, or malformed JSON.
 */
export function validate(payload) {
  const candidate = payload && payload.candidates && payload.candidates[0];
  if (!candidate || candidate.finishReason !== "STOP") throw new Error("bad_finish_reason");

  const text = candidate.content && candidate.content.parts && candidate.content.parts[0]
    && candidate.content.parts[0].text;
  if (typeof text !== "string") throw new Error("no_text_part");

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
