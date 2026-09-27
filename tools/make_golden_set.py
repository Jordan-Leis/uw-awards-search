"""
Build (and safely rebuild) the hand-labelled golden set for eligibility extraction.

    python3 tools/make_golden_set.py            # create or top up
    python3 tools/make_golden_set.py --report   # labelling progress, no writes

The golden set is the gate on Phase 1: the extractor ships only when it clears
precision >= 0.95 on 'excluded' and recall >= 0.85 on 'eligible' against these
labels. Without it there is no way to tell an extractor that works from one
that merely produces plausible-looking JSON.

TWO PROPERTIES THIS SCRIPT GUARANTEES

1. Stratified, not random. A uniform sample of 1,533 awards would contain
   roughly 3 that mention residency and 8 that mention citizenship — far too
   few to measure the fields that actually decide eligibility. Sampling is
   quota'd per concept so the rare-but-decisive cases are represented.

2. Re-running never destroys labels. Hand-labelling 120 awards is hours of
   work. Existing entries are preserved verbatim; only unlabelled slots are
   added or refreshed. Deterministic seeding means the same awards are chosen
   every time.
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import eligibility as E  # noqa: E402

AWARDS_JSON = REPO / "site/data/awards.json"
GOLDEN_PATH = REPO / "tests/golden/eligibility.json"

SEED = 20260927

# Concept probes, with a quota each. Quotas are weighted toward the fields that
# decide eligibility and are rare in the corpus (residency, citizenship,
# exclusions) rather than the fields that are merely common (full-time).
PROBES = [
    ("program_exclusion", 14, r"\b(excluding|not open to|other than|except(ing)?|ineligible)\b"),
    ("residency",         14, r"\b(resident of|residency|Ontario resident|Alberta resident|resided in)\b"),
    ("citizenship",       14, r"\b(citizen|permanent resident|domestic student|international student|protected person|study permit)\b"),
    ("nomination",        12, r"\b(nominat|selected by the|no application is required|students are chosen)\b"),
    ("identity",          12, r"\b(women|female|Indigenous|Black|disabilit|first.generation|refugee|LGBTQ|mature student)\b"),
    ("year",              12, r"\b(first|second|third|fourth|1st|2nd|3rd|4th)[- ]year\b|\byear (one|two|three|four)\b"),
    ("gpa",               12, r"\b(\d{2}(\.\d)?%|minimum .{0,12}average|cumulative average|grade point)\b"),
    ("financial_need",    10, r"\b(financial need|demonstrated need|financial circumstances|OSAP)\b"),
    ("deadline",          10, r"\b(deadline|apply by|applications? (are )?(due|close)|no later than)\b"),
    ("baseline",          10, None),   # unfiltered random draw, to catch what the probes miss
]

LABEL_TEMPLATE_HINT = {
    "_hint": (
        "Replace 'labels' with the correct eligibility object, or {} if the text states no "
        "requirements at all. Shape per field: "
        "{\"value\": ..., \"confidence\": 1.0, \"src\": \"<verbatim sentence>\"}. "
        "Use confidence 1.0 throughout — these are ground truth, not estimates. "
        "Valid fields: " + ", ".join(sorted(E.FIELD_NAMES)) + ". "
        "Set 'labelled': true when done."
    ),
}


def award_text(a):
    parts = [a.get("eligibility_selection_criteria"), a.get("award_description"),
             a.get("award_value_description"), a.get("additional_instructions")]
    return "\n".join(p for p in parts if p)


def as_lines(text, limit=40):
    """Prose as a JSON array of lines — \\n-escaped blobs are miserable to label against."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return lines[:limit]


def select(awards):
    """Deterministic stratified pick. Returns [(concept, award)] with no repeats."""
    rng = random.Random(SEED)
    chosen, seen = [], set()

    for concept, quota, pattern in PROBES:
        if pattern is None:
            pool = [a for a in awards if a["award_id"] not in seen and award_text(a).strip()]
        else:
            rx = re.compile(pattern, re.I)
            pool = [a for a in awards
                    if a["award_id"] not in seen and rx.search(award_text(a))]
        pool.sort(key=lambda a: a["award_id"])       # stable before shuffling
        rng.shuffle(pool)
        take = pool[:quota]
        if len(take) < quota:
            print(f"  warning: concept '{concept}' wanted {quota}, corpus only offered "
                  f"{len(take)}", file=sys.stderr)
        for a in take:
            seen.add(a["award_id"])
            chosen.append((concept, a))

    return chosen


def build_entry(concept, award):
    return {
        "award_uid": award.get("award_uid") or f"uw:{award['award_id']}",
        "award_id": award["award_id"],
        "award_name": award.get("award_name"),
        "sampled_for": concept,
        "labelled": False,
        "text": as_lines(award_text(award)),
        "labels": None,
    }


def load_existing():
    if not GOLDEN_PATH.exists():
        return {}
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return {e["award_uid"]: e for e in data.get("entries", [])}


# Constraint signals that should normally have a corresponding label. These
# deliberately over-fire: renewal conditions, preferences and activity
# exclusions all trip them and are all correct to omit. The output is a review
# queue for a human, never a pass/fail gate — which is why audit() always
# returns 0.
AUDIT_PROBES = [
    ("residency", r"resid(ed|ent) in Ontario|Ontario resident|resident of Ontario|lived in Ontario"),
    ("citizenship", r"Canadian citizen|permanent resident|domestic (student|undergraduate)|international (student|tuition)|study permit|protected person"),
    ("financial_need", r"demonstrated? financial need|must demonstrate financial"),
    ("gpa_min", r"minimum (?:overall |cumulative |admission )?average of \d{2}|minimum \d{2}%"),
    ("program_excluded", r"excluding|not eligible|are not open to"),
]


def audit(existing):
    """Cross-check labels against the award text.

    Written after the first labelling pass was done against text truncated to
    300 characters, which silently dropped two grade floors and very nearly
    dropped an Ontario residency clause sitting in the last sentence of a long
    description. Any labelling process that reads a prefix of the text has this
    failure mode; this is the check for it.
    """
    import re
    hits = 0
    for uid, e in sorted(existing.items()):
        if not e.get("labelled"):
            continue
        text = " ".join(e["text"])
        labels = e.get("labels") or {}
        for field, pattern in AUDIT_PROBES:
            m = re.search(pattern, text, re.I)
            if m and field not in labels:
                hits += 1
                ctx = " ".join(text[max(0, m.start() - 45):m.start() + 75].split())
                print(f"  {uid} [{field}] @char {m.start()}  {e['award_name'][:40]}")
                print(f"      ...{ctx}...")
                if e.get("note"):
                    print(f"      note: {e['note'][:110]}...")
    print(f"\n{hits} label(s) to review. Expect a steady non-zero count: preferences, "
          f"renewal conditions and activity exclusions all trip these probes and are "
          f"all correct to omit. Check each against its note.")
    return 0


def report(existing):
    total = len(existing)
    done = sum(1 for e in existing.values() if e.get("labelled"))
    print(f"Golden set: {done}/{total} labelled ({done/total:.0%})" if total else "Golden set is empty.")
    if not total:
        return 0
    by_concept = {}
    for e in existing.values():
        c = e.get("sampled_for", "?")
        d, t = by_concept.get(c, (0, 0))
        by_concept[c] = (d + (1 if e.get("labelled") else 0), t + 1)
    for concept, (d, t) in sorted(by_concept.items()):
        bar = "#" * round(10 * d / t) + "." * (10 - round(10 * d / t))
        print(f"  {concept:20s} {bar} {d:3d}/{t:3d}")

    problems = []
    for uid, e in sorted(existing.items()):
        if e.get("labelled"):
            for p in E.validate_eligibility(e.get("labels"), where=uid):
                problems.append(p)
    if problems:
        print(f"\n{len(problems)} invalid label(s):", file=sys.stderr)
        for p in problems[:20]:
            print(f"  - {p}", file=sys.stderr)
        return 1
    if done:
        print(f"\nAll {done} labelled entries validate against the eligibility schema.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="show progress and validate; write nothing")
    ap.add_argument("--audit", action="store_true",
                    help="flag constraints present in an award's text but absent from its labels; write nothing")
    args = ap.parse_args()

    existing = load_existing()
    if args.audit:
        return audit(existing)
    if args.report:
        return report(existing)

    awards = json.loads(AWARDS_JSON.read_text(encoding="utf-8"))
    print(f"Read {len(awards)} awards from {AWARDS_JSON.relative_to(REPO)}")

    chosen = select(awards)
    entries, kept, added = [], 0, 0
    for concept, award in chosen:
        uid = award.get("award_uid") or f"uw:{award['award_id']}"
        prior = existing.get(uid)
        if prior and prior.get("labelled"):
            entries.append(prior)      # never touch finished work
            kept += 1
        else:
            entries.append(build_entry(concept, award))
            added += 1

    # An award that was labelled but is no longer sampled is still valuable.
    sampled_uids = {e["award_uid"] for e in entries}
    for uid, e in sorted(existing.items()):
        if uid not in sampled_uids and e.get("labelled"):
            entries.append(e)
            kept += 1

    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps({
        "_instructions": LABEL_TEMPLATE_HINT["_hint"],
        "_ship_gate": ("Extractor ships at precision >= 0.95 on 'excluded' and "
                       "recall >= 0.85 on 'eligible' against these labels."),
        "seed": SEED,
        "entries": entries,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Wrote {GOLDEN_PATH.relative_to(REPO)}: {len(entries)} entries "
          f"({kept} existing labels preserved, {added} awaiting labelling)")
    print(f"\nNext: label the entries, then `python3 tools/make_golden_set.py --report`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
