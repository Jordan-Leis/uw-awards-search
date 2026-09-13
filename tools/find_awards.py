#!/usr/bin/env python3
"""
Find UWaterloo awards that fit a student profile.

Built for use by an AI assistant: the full dataset is ~2.6 MB, far too large
to read into a model context, so this filters and ranks locally and prints a
compact shortlist instead. See tools/README.md.

Stdlib only, so it runs on a bare clone with no venv or pip install.
"""
import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = REPO_ROOT / "site" / "data" / "awards.json"
DEFAULT_PROFILE = Path(__file__).resolve().parent / "profile.json"

# Areas of study that mean "not restricted to specific programs".
OPEN_TO_ALL_AREA = "All Programs"
FACULTY_WIDE_SUFFIX = "Faculty - All Programs"

# Fields searched for keyword hits, and how much a hit in each is worth.
# A term scores its field weight ONCE per field, never per occurrence:
# award_description averages far longer than award_name, so counting
# occurrences would let one verbose award dominate the ranking.
FIELD_WEIGHTS = {
    "award_name": 3.0,
    "area_of_study": 2.0,
    "eligibility_selection_criteria": 1.5,
    "award_description": 1.0,
    "award_value_description": 0.5,
    "application_details": 0.25,  # mostly boilerplate about how to apply
}

MONEY_RE = re.compile(r"\$\s?([\d,]+)")
# Eligibility text mentioning criteria the source data does NOT encode as
# structured fields. We flag these for manual review rather than filtering.
CITIZENSHIP_RE = re.compile(r"citizen|permanent resident|international student|domestic|study permit", re.I)
GPA_RE = re.compile(r"\b\d{2}\s?%|minimum .{0,20}(average|CGPA|GPA)|overall average", re.I)

CAREER_ABBR = {"Undergraduate": "UG", "Graduate": "GR"}
LEVEL_ABBR = {
    "UG Entering Year 1": "EY1", "UG Year 1": "Y1", "UG Year 2": "Y2",
    "UG Year 3": "Y3", "UG Year 4": "Y4", "Master's": "Ms", "Doctoral": "Dr",
}
TERM_ABBR = {"Fall": "Fa", "Winter": "Wi", "Spring": "Sp", "Entrance": "En"}
TYPE_ABBR = {
    "Awards/Scholarships/Prizes": "Sch", "Bursaries/Financial need": "Bur",
    "Varsity/Athletics": "Var", "International experience": "Intl",
    "Entrepreneurial": "Ent", "Other experiential awards": "Oth",
}


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_awards(data_path=None, base_url=None):
    if base_url:
        import urllib.request
        url = base_url.rstrip("/") + "/awards.json"
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    path = Path(data_path) if data_path else DEFAULT_DATA
    if not path.is_file():
        sys.exit(
            f"error: no dataset at {path}\n"
            "       Pass --data PATH, or --url https://jordanleis.com/awards-database/data"
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_profile(path, required=False, quiet=False):
    p = Path(path)
    if not p.is_file():
        if required:
            sys.exit(f"error: --require-profile given but no profile at {p}")
        if not quiet:
            print(
                f"note: no profile at {p} (copy tools/profile.example.json to enable "
                "profile matching); using CLI filters only.",
                file=sys.stderr,
            )
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Vocabulary / facets
# --------------------------------------------------------------------------

def facet_counts(awards):
    facets = {}
    for key, is_list in [
        ("career", False), ("levels", True), ("terms", True),
        ("award_types", True), ("affiliations", True), ("areas_of_study", True),
    ]:
        c = Counter()
        for a in awards:
            v = a.get(key)
            if is_list:
                for x in (v or []):
                    c[x] += 1
            elif v:
                c[v] += 1
        facets[key] = c
    return facets


def warn_unknown(label, values, vocabulary):
    """Catch e.g. "Engineering Faculty" vs "Engineering Faculty - All Programs",
    which would otherwise silently produce zero matches."""
    for v in values or []:
        if v not in vocabulary:
            near = [k for k in vocabulary if v.lower() in k.lower()][:2]
            hint = f" Did you mean: {', '.join(near)}?" if near else ""
            print(f"warning: {label} value not in dataset: {v!r}.{hint}", file=sys.stderr)


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

def faculty_wide_label(faculty):
    return f"{faculty} {FACULTY_WIDE_SUFFIX}"


def effective_areas(areas, faculties, expand):
    """The set of area tags that count as a match.

    With expansion on (default), a student in a specific program is also
    eligible for their faculty's blanket awards and for awards open to all
    programs. Without it, "Software Engineering" would miss the 321 awards
    tagged All Programs and the 105 tagged Engineering Faculty - All Programs
    - i.e. most of the real eligible pool.
    """
    eff = set(areas or [])
    for f in faculties or []:
        eff.add(faculty_wide_label(f))
    if eff and expand:
        eff.add(OPEN_TO_ALL_AREA)
        for f in faculties or []:
            eff.add(faculty_wide_label(f))
    return eff


def parse_value(award):
    """Best-effort dollar figure. Most awards state it in the description
    rather than the value field (1264 vs 260), so check both."""
    for field in ("award_value_description", "award_description"):
        text = award.get(field)
        if not text:
            continue
        m = MONEY_RE.search(text)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def passes_filters(award, f):
    def any_of(award_values, selected):
        if not selected:
            return True
        return bool(set(award_values or []) & set(selected))

    if f["career"] and award.get("career") not in f["career"]:
        return False
    if not any_of(award.get("levels"), f["level"]):
        return False
    if not any_of(award.get("terms"), f["term"]):
        return False
    if not any_of(award.get("award_types"), f["type"]):
        return False
    if f["areas"] and not (set(award.get("areas_of_study") or []) & f["areas"]):
        return False

    # Affiliation is an eligibility gate, not a facet: an award tagged
    # e.g. ["Women"] is restricted to that group. An award with no
    # affiliation tag is open to everyone and always passes.
    award_affs = set(award.get("affiliations") or [])
    if award_affs:
        allowed = set(f["affiliation"] or [])
        if not (award_affs & allowed):
            return False

    if f["min_value"] is not None:
        v = parse_value(award)
        if v is None or v < f["min_value"]:
            return False
    return True


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def normalize(text):
    text = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"\s+", " ", text).lower()


def tokenize(text):
    return [t for t in re.split(r"[^a-z0-9]+", normalize(text)) if len(t) > 2]


def build_idf(awards):
    """Document frequency over the corpus, so common words ("student",
    "award", "waterloo") self-neutralize without a hand-kept stopword list."""
    df = Counter()
    for a in awards:
        seen = set()
        for field in FIELD_WEIGHTS:
            seen.update(tokenize(a.get(field) or ""))
        for t in seen:
            df[t] += 1
    n = max(len(awards), 1)
    return df, n


def idf(term, df, n):
    d = df.get(term, 0)
    if d == 0:
        return 1.0
    return max(0.1, min(3.0, math.log(n / d)))


def score_award(award, terms, df, n, profile, exclude_terms, expand):
    score = 0.0
    matched = []
    haystacks = {f: normalize(award.get(f) or "") for f in FIELD_WEIGHTS}

    for term in terms:
        weight_sum = 0.0
        for field, w in FIELD_WEIGHTS.items():
            if re.search(r"\b" + re.escape(term) + r"\b", haystacks[field]):
                weight_sum += w
        if weight_sum:
            score += weight_sum * idf(term, df, n)
            matched.append(term)

    for term in exclude_terms:
        if any(re.search(r"\b" + re.escape(term) + r"\b", h) for h in haystacks.values()):
            score -= 1.5

    if profile:
        areas = set(award.get("areas_of_study") or [])
        progs = set(profile.get("programs") or [])
        fac_labels = {faculty_wide_label(x) for x in ([profile["faculty"]] if profile.get("faculty") else [])}
        # Reward targeted awards over the blanket ones.
        if areas & progs:
            score += 3.0
        elif areas & fac_labels:
            score += 1.0
        elif OPEN_TO_ALL_AREA in areas:
            score += 0.4

        if set(award.get("affiliations") or []) & set(profile.get("affiliations") or []):
            score += 2.0
        if set(award.get("award_types") or []) & set(profile.get("preferred_types") or []):
            score += 2.0
        if profile.get("financial_need") and "Bursaries/Financial need" in (award.get("award_types") or []):
            score += 1.5
        p_terms = set(profile.get("terms") or [])
        a_terms = set(award.get("terms") or [])
        if p_terms and a_terms and not (p_terms & a_terms):
            score -= 2.0

    v = parse_value(award)
    if v:
        score += min(1.5, v / 5000.0)

    return score, matched


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def abbrev(values, table):
    return ",".join(table.get(v, v[:4]) for v in values or []) or "-"


def flags_for(award):
    flags = []
    elig = award.get("eligibility_selection_criteria") or ""
    desc = award.get("award_description") or ""
    blob = elig + " " + desc
    if CITIZENSHIP_RE.search(blob):
        flags.append("!cit")
    if GPA_RE.search(blob):
        flags.append("!gpa")
    return ",".join(flags)


def fmt_value(award):
    v = parse_value(award)
    return f"${v:,}" if v else "varies"


def print_compact(rows, header_lines):
    for line in header_lines:
        print(line)
    print("# cols: rank score id name | career levels | terms | type | value | areas | flags")
    for i, (award, score, matched) in enumerate(rows, 1):
        name = award.get("award_name") or "(untitled)"
        if len(name) > 58:
            name = name[:57] + "…"
        areas = award.get("areas_of_study") or []
        area_s = ",".join(a.replace(" Faculty - All Programs", "-ALL") for a in areas[:2])
        if len(areas) > 2:
            area_s += f",+{len(areas) - 2}"
        print(
            f"{i:3d} {score:6.1f} {award['award_id']} {name:<58s}"
            f" | {CAREER_ABBR.get(award.get('career'), '?')} {abbrev(award.get('levels'), LEVEL_ABBR)}"
            f" | {abbrev(award.get('terms'), TERM_ABBR)}"
            f" | {abbrev(award.get('award_types'), TYPE_ABBR)}"
            f" | {fmt_value(award)}"
            f" | {area_s or '-'}"
            f" | {flags_for(award)}"
        )
    if rows:
        ids = " ".join(a["award_id"] for a, _, _ in rows[:3])
        print(f"# full text: python tools/find_awards.py --detail {ids}")


def print_block(rows, header_lines):
    for line in header_lines:
        print(line)
    for i, (award, score, matched) in enumerate(rows, 1):
        print(f"\n[{i}] {award.get('award_name')}  (id {award['award_id']}, score {score:.1f})")
        print(f"    {CAREER_ABBR.get(award.get('career'), '?')} {abbrev(award.get('levels'), LEVEL_ABBR)}"
              f" | {abbrev(award.get('terms'), TERM_ABBR)} | {fmt_value(award)}"
              f" | {', '.join(award.get('areas_of_study') or []) or '-'} | {flags_for(award)}")
        snippet = re.sub(r"\s+", " ", award.get("award_description") or "")[:160]
        if snippet:
            print(f"    {snippet}…")


def print_detail(awards, ids):
    by_id = {a["award_id"]: a for a in awards}
    for aid in ids:
        a = by_id.get(aid)
        if not a:
            print(f"# {aid}: not found", file=sys.stderr)
            continue
        print("=" * 72)
        print(f"{a.get('award_name')}   (id {a['award_id']})")
        print("=" * 72)
        for label, key in [
            ("Career", "career"), ("Level", "level"), ("Term", "application_selection"),
            ("Type", "award_type"), ("Value", "award_value_description"),
            ("Area of study", "area_of_study"), ("Affiliation", "affiliation"),
            ("Description", "award_description"),
            ("Eligibility", "eligibility_selection_criteria"),
            ("Application", "application_details"),
            ("Documents", "required_supporting_documents"),
            ("Additional", "additional_instructions"),
            ("Contact", "contact_detail"),
        ]:
            val = a.get(key)
            if val:
                print(f"\n{label}:\n{val}")
        print()


# --------------------------------------------------------------------------

def main():
    # Windows consoles default to cp1252 and choke on the dataset's smart
    # quotes/dashes; force UTF-8 so output never dies mid-listing.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="Find UWaterloo awards matching a profile and/or keywords.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("query", nargs="*", help="free-text keywords to rank by")
    ap.add_argument("--data", help="path to awards.json")
    ap.add_argument("--url", help="fetch data from a published /data base URL instead")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE))
    ap.add_argument("--no-profile", action="store_true")
    ap.add_argument("--require-profile", action="store_true")

    ap.add_argument("--career", action="append", default=[])
    ap.add_argument("--level", action="append", default=[])
    ap.add_argument("--term", action="append", default=[])
    ap.add_argument("--type", action="append", default=[])
    ap.add_argument("--affiliation", action="append", default=[])
    ap.add_argument("--area", action="append", default=[])
    ap.add_argument("--faculty", action="append", default=[],
                    help='e.g. "Engineering" -> "Engineering Faculty - All Programs"')
    ap.add_argument("--strict-area", action="store_true",
                    help="disable faculty-wide / All Programs expansion")
    ap.add_argument("--min-value", type=int)

    ap.add_argument("--keywords", default="", help="extra positive terms, comma separated")
    ap.add_argument("--exclude-keywords", default="", help="negative terms, comma separated")
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--format", choices=["compact", "block", "ids", "json"], default="compact")
    ap.add_argument("--detail", nargs="+", metavar="ID")
    ap.add_argument("--facets", action="store_true", help="print valid filter values and exit")
    ap.add_argument("--stats", action="store_true", help="print the filter funnel only")
    ap.add_argument("--explain", action="store_true", help="show matched terms per row")
    args = ap.parse_args()

    awards = load_awards(args.data, args.url)
    facets = facet_counts(awards)

    if args.facets:
        print(f"# {len(awards)} awards")
        for key, counter in facets.items():
            print(f"\n{key} ({len(counter)} distinct):")
            for val, n in counter.most_common():
                print(f"  {n:5d}  {val}")
        return

    if args.detail:
        print_detail(awards, args.detail)
        return

    profile = None if args.no_profile else load_profile(
        args.profile, required=args.require_profile, quiet=False)

    # Merge profile + CLI filters (CLI adds to, never replaces, the profile).
    careers = list(args.career) or ([profile["career"]] if profile and profile.get("career") else [])
    levels = list(args.level) + (profile.get("levels", []) if profile else [])
    terms_f = list(args.term) + (profile.get("terms", []) if profile else [])
    types_f = list(args.type)
    affils = list(args.affiliation) + (profile.get("affiliations", []) if profile else [])
    areas_in = list(args.area) + (profile.get("programs", []) if profile else [])
    faculties = list(args.faculty) + ([profile["faculty"]] if profile and profile.get("faculty") else [])

    warn_unknown("career", careers, facets["career"])
    warn_unknown("level", levels, facets["levels"])
    warn_unknown("term", terms_f, facets["terms"])
    warn_unknown("type", types_f, facets["award_types"])
    warn_unknown("affiliation", affils, facets["affiliations"])
    warn_unknown("area", areas_in, facets["areas_of_study"])
    for f in faculties:
        if faculty_wide_label(f) not in facets["areas_of_study"]:
            print(f"warning: faculty {f!r} has no "
                  f"'{faculty_wide_label(f)}' tag in the dataset", file=sys.stderr)

    filters = {
        "career": careers,
        "level": levels,
        "term": terms_f,
        "type": types_f,
        "affiliation": affils,
        "areas": effective_areas(areas_in, faculties, not args.strict_area),
        "min_value": args.min_value,
    }

    candidates = [a for a in awards if passes_filters(a, filters)]

    if args.stats:
        print(f"total={len(awards)}  after_filters={len(candidates)}")
        print(f"filters: career={careers or '-'} levels={levels or '-'} terms={terms_f or '-'} "
              f"types={types_f or '-'} areas={sorted(filters['areas']) or '-'} "
              f"affiliations={affils or '-'} min_value={args.min_value}")
        return

    query_text = " ".join(args.query)
    terms = set(tokenize(query_text))
    terms |= set(tokenize(args.keywords.replace(",", " ")))
    if profile:
        for interest in profile.get("interests", []):
            terms |= set(tokenize(interest))
    exclude_terms = set(tokenize(args.exclude_keywords.replace(",", " ")))
    if profile:
        for kw in profile.get("exclude_keywords", []):
            exclude_terms |= set(tokenize(kw))

    df, n = build_idf(awards)
    scored = []
    for a in candidates:
        s, matched = score_award(a, terms, df, n, profile, exclude_terms, not args.strict_area)
        if s >= args.min_score:
            scored.append((a, s, matched))

    # Deterministic ordering so repeated runs agree.
    scored.sort(key=lambda r: (-r[1], -(parse_value(r[0]) or 0), r[0].get("award_name") or ""))
    rows = scored[:args.limit]

    header = [
        f"# query={query_text!r} profile={'none' if not profile else args.profile}",
        f"# {len(awards)} -> {len(candidates)} after filters -> "
        f"{len(scored)} scored -> showing top {len(rows)}",
    ]

    if args.format == "ids":
        for a, _, _ in rows:
            print(a["award_id"])
    elif args.format == "json":
        print(json.dumps([
            {"award_id": a["award_id"], "award_name": a.get("award_name"),
             "score": round(s, 2), "matched_terms": m,
             "career": a.get("career"), "levels": a.get("levels"),
             "terms": a.get("terms"), "award_types": a.get("award_types"),
             "areas_of_study": a.get("areas_of_study"), "value": parse_value(a),
             "flags": flags_for(a)}
            for a, s, m in rows], indent=2, ensure_ascii=False))
    elif args.format == "block":
        print_block(rows, header)
    else:
        print_compact(rows, header)
        if args.explain:
            print("\n# matched terms")
            for i, (a, s, m) in enumerate(rows, 1):
                print(f"{i:3d} {a['award_id']}: {', '.join(m) or '(profile/value bonuses only)'}")


if __name__ == "__main__":
    main()
