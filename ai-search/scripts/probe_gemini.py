"""
Probe the Gemini Interactions API directly, bypassing the Worker.

Builds the request by hand from the docs so it tests the API contract, not our
code. Prints Gemini's own status, timing, token usage and full error bodies.
Stdlib only. Reads the key from ai-search/.dev.vars and never prints it.

    python ai-search/scripts/probe_gemini.py                 # default matrix
    python ai-search/scripts/probe_gemini.py --models gemini-3.5-flash-lite
    python ai-search/scripts/probe_gemini.py --questions "phd chemistry" "hi"

Docs this is written against (Sept 2026):
  https://ai.google.dev/gemini-api/docs/interactions/text-generation
  https://ai.google.dev/gemini-api/docs/interactions/structured-output
  https://ai.google.dev/gemini-api/docs/interactions/thinking
  https://ai.google.dev/api/interactions-api
"""
import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parent
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"

DEFAULT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
DEFAULT_QUESTIONS = [
    "bursaries for a 2nd year software engineering student",
    "phd chemistry scholarships",
    "first year math",
    "awards only for women in engineering",
    "I'm a woman studying CS",
]

KEY_PATTERNS = [re.compile(r"AQ\.[A-Za-z0-9_\-]{20,}"), re.compile(r"AIza[0-9A-Za-z_\-]{20,}")]


def load_key():
    path = AI_DIR / ".dev.vars"
    if not path.exists():
        sys.exit(f"missing {path} — create it with GEMINI_API_KEY=<key>")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("GEMINI_API_KEY not found in .dev.vars")


def redact(text, key):
    text = text.replace(key, "<REDACTED>")
    for p in KEY_PATTERNS:
        text = p.sub("<REDACTED>", text)
    return text


def load_vocab():
    """Parse VOCAB out of vocab.generated.js without a JS engine (it's plain
    literals). This keeps the probe independent of the Worker code path."""
    src = (AI_DIR / "vocab.generated.js").read_text(encoding="utf-8")
    body = src[src.index("export const VOCAB = ") + len("export const VOCAB = "):]
    body = body[: body.rindex("};") + 1]
    # Turn the JS object literal into JSON: quote keys, drop trailing commas.
    body = re.sub(r"^\s*(\w+):", r'"\1":', body, flags=re.M)
    body = re.sub(r",(\s*[}\]])", r"\1", body)
    return json.loads(body)


def load_system_prompt():
    src = (AI_DIR / "prompt.js").read_text(encoding="utf-8")
    m = re.search(r"const RULES = `(.*?)`;", src, re.S)
    if not m:
        sys.exit("could not find RULES in prompt.js")
    # Mirror prompt.js: the area-of-study vocabulary is appended to the rules
    # because a 129-value enum is over Gemini's (undocumented, measured 122)
    # per-enum limit.
    return m.group(1) + "\n" + "\n".join(load_vocab()["areaOfStudy"])


def build_schema(vocab):
    props = {}
    for key in ["career", "level", "awardType", "term", "affiliation"]:
        props[key] = {"type": "array", "items": {"type": "string", "enum": vocab[key]}}
    # Plain strings: see prompt.js — Gemini rejects any single enum > 122 values.
    props["areaOfStudy"] = {"type": "array", "items": {"type": "string"}}
    props["keywords"] = {"type": "string"}
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


def build_body(model, question, system_prompt, schema, *, placement, thinking):
    body = {
        "model": model,
        "system_instruction": system_prompt,
        "input": f"<q>{question}</q>",
        "generation_config": {"max_output_tokens": 512},
    }
    if thinking:
        body["generation_config"]["thinking_level"] = thinking
    fmt = {"type": "text", "mime_type": "application/json", "schema": schema}
    if placement == "top":
        body["response_format"] = fmt
    else:
        body["generation_config"]["response_format"] = fmt
    return body


def call(key, body, timeout=30):
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), time.time() - t0
    except Exception as e:  # timeout, DNS, TLS
        return 0, f"{type(e).__name__}: {e}", time.time() - t0


def extract_text(payload):
    for step in payload.get("steps", []):
        if step.get("type") == "model_output":
            for c in step.get("content", []):
                if c.get("type") == "text":
                    return c.get("text")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument("--questions", nargs="+", default=DEFAULT_QUESTIONS)
    ap.add_argument("--placement", choices=["top", "nested", "auto"], default="auto",
                    help="where response_format goes; auto = top, then nested on 400")
    ap.add_argument("--thinking", default="minimal",
                    help="thinking_level, or 'none' to omit the field")
    ap.add_argument("--gemma-probe", action="store_true",
                    help="also send ONE request to gemma-4-26b-a4b-it to learn if it honours response_format")
    args = ap.parse_args()

    key = load_key()
    vocab = load_vocab()
    schema = build_schema(vocab)
    system_prompt = load_system_prompt()
    print(f"schema: {sum(len(vocab[k]) for k in ['career','level','awardType','term','affiliation'])} enum values "
          f"across 5 filters + {len(vocab['areaOfStudy'])} areas listed in the prompt; "
          f"system prompt {len(system_prompt)} chars\n")

    requests_sent = 0
    summary = []

    def run_one(model, question, placement, thinking):
        nonlocal requests_sent
        body = build_body(model, question, system_prompt, schema, placement=placement, thinking=thinking)
        status, raw, dt = call(key, body)
        requests_sent += 1
        raw = redact(raw, key)
        line = f"  [{status:>3}] {dt*1000:6.0f}ms  {question!r}"
        if status != 200:
            print(line)
            print("        ERROR BODY:", raw[:900].replace("\n", " "))
            return status, dt, None
        payload = json.loads(raw)
        istatus = payload.get("status")
        usage = payload.get("usage", {})
        text = extract_text(payload)
        print(f"{line}  status={istatus}  in={usage.get('total_input_tokens')} "
              f"out={usage.get('total_output_tokens')} thought={usage.get('total_thought_tokens')}")
        if istatus != "completed":
            print("        NOT COMPLETED — full payload:", raw[:900].replace("\n", " "))
            return status, dt, None
        try:
            parsed = json.loads(text)
            compact = {k: v for k, v in parsed.items() if v}
            print("        ->", json.dumps(compact, ensure_ascii=False)[:400])
            return status, dt, parsed
        except Exception as e:
            print(f"        TEXT IS NOT JSON ({e}): {text!r:.300}")
            return status, dt, None

    for model in args.models:
        print(f"=== {model} ===")
        placement = "top" if args.placement == "auto" else args.placement
        thinking = None if args.thinking == "none" else args.thinking

        # First question decides the response_format placement (documented
        # ambiguity: reference says generation_config, curl example says top).
        status, dt, parsed = run_one(model, args.questions[0], placement, thinking)
        if status == 400 and args.placement == "auto":
            print("  -> 400 with top-level response_format; retrying nested under generation_config")
            placement = "nested"
            status, dt, parsed = run_one(model, args.questions[0], placement, thinking)
        if status in (429, 404, 403):
            print(f"  -> {status}: skipping the rest of this model to save quota\n")
            summary.append((model, placement, status, [dt], 0, len(args.questions)))
            continue

        times = [dt]
        ok = 1 if parsed else 0
        for q in args.questions[1:]:
            status, dt, parsed = run_one(model, q, placement, thinking)
            times.append(dt)
            ok += 1 if parsed else 0
            if status == 429:
                print("  -> 429 mid-run; stopping this model")
                break
        summary.append((model, placement, 200, times, ok, len(args.questions)))
        print()

    if args.gemma_probe:
        print("=== gemma-4-26b-a4b-it (single probe: does it honour response_format?) ===")
        run_one("gemma-4-26b-a4b-it", args.questions[0], "top", None)
        print()

    print("=== summary ===")
    print(f"requests sent: {requests_sent}")
    for model, placement, status, times, ok, total in summary:
        med = statistics.median(times) * 1000
        p95 = sorted(times)[max(0, int(len(times) * 0.95) - 1)] * 1000
        print(f"  {model:<24} placement={placement:<6} http={status}  ok={ok}/{total}  "
              f"median={med:.0f}ms  p95={p95:.0f}ms  max={max(times)*1000:.0f}ms")


if __name__ == "__main__":
    main()
