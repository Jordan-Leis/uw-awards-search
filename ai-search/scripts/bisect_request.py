"""
Bisect which field of the Interactions request Gemini rejects. Adds one field
per step to a known-good minimal body and reports the first 400.

    python ai-search/scripts/bisect_request.py [--model gemini-3.5-flash-lite]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_gemini import build_schema, call, load_key, load_system_prompt, load_vocab, redact  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini-3.5-flash-lite")
    args = ap.parse_args()

    key = load_key()
    vocab = load_vocab()
    full_schema = build_schema(vocab)
    system_prompt = load_system_prompt()
    q = "<q>bursaries for a 2nd year software engineering student</q>"

    tiny_schema = {"type": "object", "properties": {"keywords": {"type": "string"}},
                   "required": ["keywords"]}
    schema_no_ap = {k: v for k, v in full_schema.items() if k != "additionalProperties"}
    schema_small_enum = json.loads(json.dumps(schema_no_ap))
    schema_small_enum["properties"]["areaOfStudy"]["items"]["enum"] = vocab["areaOfStudy"][:10]

    steps = [
        ("model + input only",
         {"model": args.model, "input": q}),
        ("+ system_instruction",
         {"model": args.model, "input": q, "system_instruction": system_prompt}),
        ("+ generation_config.max_output_tokens",
         {"model": args.model, "input": q, "system_instruction": system_prompt,
          "generation_config": {"max_output_tokens": 512}}),
        ("+ generation_config.thinking_level=minimal",
         {"model": args.model, "input": q, "system_instruction": system_prompt,
          "generation_config": {"max_output_tokens": 512, "thinking_level": "minimal"}}),
        ("+ response_format with a tiny schema",
         {"model": args.model, "input": q, "system_instruction": system_prompt,
          "generation_config": {"max_output_tokens": 512, "thinking_level": "minimal"},
          "response_format": {"type": "text", "mime_type": "application/json", "schema": tiny_schema}}),
        ("+ full schema, small areaOfStudy enum (10), no additionalProperties",
         {"model": args.model, "input": q, "system_instruction": system_prompt,
          "generation_config": {"max_output_tokens": 512, "thinking_level": "minimal"},
          "response_format": {"type": "text", "mime_type": "application/json", "schema": schema_small_enum}}),
        ("+ full schema, full 129 enum, no additionalProperties",
         {"model": args.model, "input": q, "system_instruction": system_prompt,
          "generation_config": {"max_output_tokens": 512, "thinking_level": "minimal"},
          "response_format": {"type": "text", "mime_type": "application/json", "schema": schema_no_ap}}),
        ("+ additionalProperties:false",
         {"model": args.model, "input": q, "system_instruction": system_prompt,
          "generation_config": {"max_output_tokens": 512, "thinking_level": "minimal"},
          "response_format": {"type": "text", "mime_type": "application/json", "schema": full_schema}}),
    ]

    for label, body in steps:
        status, raw, dt = call(key, body)
        raw = redact(raw, key)
        if status == 200:
            p = json.loads(raw)
            print(f"[200] {dt*1000:6.0f}ms  {label}  status={p.get('status')} "
                  f"thought={p.get('usage', {}).get('total_thought_tokens')}")
        else:
            print(f"[{status}] {dt*1000:6.0f}ms  {label}")
            print("       ", raw[:600].replace("\n", " "))
            print("        ^ first failing field — stopping here")
            return 1
    print("\nall steps accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
