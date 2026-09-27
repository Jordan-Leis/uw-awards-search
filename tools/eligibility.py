"""
Structured eligibility: the vocabulary, the record shape, and the matcher.

Stdlib only, like the rest of tools/ — site/js/eligibility.js mirrors this file
and both are held to the same fixture (tests/fixtures/match_cases.json), so the
CLI and the browser can never quietly disagree. That divergence already exists
in the v1 code: site/js/search.js treats affiliation as an OR facet while
tools/find_awards.py treats it as an eligibility gate. Not repeating it.

WHY THREE STATES
Eligibility is mostly not stated. Measured over the 1,533 UW awards, only 7.2%
mention citizenship at all and 4.2% mention residency. A two-state matcher has
to read that silence as either "yes" (floods the student with awards they
cannot win) or "no" (hides almost everything). Neither is honest, so a field
the source never addressed resolves to UNKNOWN and the award is surfaced under
"needs checking" with the reason attached.

The expensive direction to be wrong is EXCLUDED: a false exclusion silently
costs the student money they would never learn they were owed. So exclusion
requires a high-confidence, affirmatively-contradicted field. Everything softer
degrades to UNKNOWN.
"""

# --- verdicts, worst-first ------------------------------------------------
EXCLUDED = "excluded"
UNKNOWN = "unknown"
ELIGIBLE = "eligible"

# An award's verdict is the worst verdict across its fields.
_SEVERITY = {EXCLUDED: 0, UNKNOWN: 1, ELIGIBLE: 2}

# Below this, an extracted value is not trusted enough to exclude anyone on.
# It still counts toward a match, it just cannot produce EXCLUDED on its own.
EXCLUSION_CONFIDENCE_THRESHOLD = 0.75

# --- controlled vocabularies ---------------------------------------------
CITIZENSHIP = {"citizen", "pr", "protected", "international"}

PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
}

# Institution scope. A specific institution is a slug ("university-of-waterloo");
# these two are wildcards that a specific profile institution always satisfies.
ANY_CANADIAN = "any_canadian"
ANY_ELIGIBLE_CANADIAN = "any_eligible_canadian"
INSTITUTION_WILDCARDS = {ANY_CANADIAN, ANY_ELIGIBLE_CANADIAN}

IDENTITIES = {
    "women", "indigenous", "black", "disability", "first_generation",
    "refugee", "mature", "lgbtq", "part_time",
}

APPLICATION_TYPES = {"open", "nomination", "institution_mediated", "member_only"}

# Sentinel a PROFILE may carry to affirm "none of these categories apply to me",
# e.g. identity: ["none"]. It exists because an empty list cannot distinguish
# "I answered: none" from "I have not answered", and those must differ: the
# first excludes you from a women-only award, the second only flags it for
# checking.
#
# It is a sentinel rather than [] on purpose. Reading [] as a declaration would
# mean any profile builder that initialises its list fields to [] silently
# excludes the student from everything — a catastrophic and very easy mistake.
# Accidental emptiness stays UNKNOWN; only this explicit value can exclude.
NONE_DECLARED = "none"


class FieldSpec:
    """How one eligibility field is compared against a student profile.

    kind:
      set_overlap  — award lists acceptable values; profile value(s) must
                     intersect. Used for citizenship, residency, program, year.
      set_exclude  — award lists DISQUALIFYING values; any intersection with
                     the profile excludes. This is the "(excluding CFM and
                     Software Engineering)" case, and the one most likely to
                     produce a costly false positive if it is skipped.
      at_least     — numeric floor; profile value must be >= the requirement.
      must_be      — boolean requirement the profile must match.
    """

    def __init__(self, name, kind, profile_key, wildcards=frozenset()):
        self.name = name
        self.kind = kind
        self.profile_key = profile_key
        self.wildcards = wildcards


FIELD_SPECS = [
    FieldSpec("citizenship", "set_overlap", "citizenship"),
    FieldSpec("residency", "set_overlap", "residency"),
    FieldSpec("institution", "set_overlap", "institution", INSTITUTION_WILDCARDS),
    FieldSpec("program", "set_overlap", "program", {"any"}),
    FieldSpec("program_excluded", "set_exclude", "program"),
    FieldSpec("institution_excluded", "set_exclude", "institution"),
    FieldSpec("year", "set_overlap", "year"),
    FieldSpec("level", "set_overlap", "level"),
    FieldSpec("identity", "set_overlap", "identity"),
    FieldSpec("gpa_min", "at_least", "gpa"),
    FieldSpec("financial_need", "must_be", "financial_need"),
    FieldSpec("full_time", "must_be", "full_time"),
]

FIELD_NAMES = {spec.name for spec in FIELD_SPECS}


# --- validation -----------------------------------------------------------
VALUE_DOMAINS = {
    "citizenship": CITIZENSHIP,
    "residency": PROVINCES,
    "identity": IDENTITIES,
    "level": {"undergraduate", "graduate"},
}


def validate_eligibility(elig, where="eligibility"):
    """Return a list of human-readable problems. Empty means valid.

    Used as a hard gate on extractor output: a hallucinated field name or an
    out-of-vocabulary value must fail loudly at build time, not silently
    mismatch every student forever.
    """
    problems = []
    if elig is None:
        return problems
    if not isinstance(elig, dict):
        return [f"{where}: expected an object, got {type(elig).__name__}"]

    for field, entry in elig.items():
        at = f"{where}.{field}"
        if field not in FIELD_NAMES:
            problems.append(f"{at}: unknown field (not one of {sorted(FIELD_NAMES)})")
            continue
        if not isinstance(entry, dict):
            problems.append(f"{at}: expected an object with value/confidence/src")
            continue
        if "value" not in entry:
            problems.append(f"{at}: missing 'value'")
        conf = entry.get("confidence")
        if not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
            problems.append(f"{at}: confidence must be a number in [0,1], got {conf!r}")
        if not entry.get("src"):
            problems.append(
                f"{at}: missing 'src'. Every extracted value must carry the verbatim "
                f"sentence it came from — that is what makes a wrong match auditable."
            )

        domain = VALUE_DOMAINS.get(field)
        if domain and isinstance(entry.get("value"), list):
            bad = [v for v in entry["value"] if v not in domain]
            if bad:
                problems.append(f"{at}: value(s) {bad} not in {sorted(domain)}")
        if field == "year" and isinstance(entry.get("value"), list):
            bad = [v for v in entry["value"] if not isinstance(v, int) or not 0 <= v <= 6]
            if bad:
                problems.append(f"{at}: year value(s) {bad} must be ints 0-6 (0 = entering)")
        if field == "gpa_min":
            v = entry.get("value")
            if not isinstance(v, (int, float)) or not 0 <= v <= 100:
                problems.append(f"{at}: gpa_min must be a percent 0-100, got {v!r}")

    return problems


# --- matching -------------------------------------------------------------
def _as_set(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        return set(value)
    return {value}


def match_field(spec, entry, profile):
    """(verdict, reason) for one field. reason is None when nothing to say."""
    required = entry.get("value")
    confidence = entry.get("confidence", 0.0)
    src = entry.get("src")
    have = profile.get(spec.profile_key)

    # A field the source never addressed constrains nothing.
    if required is None or (isinstance(required, list) and not required):
        return ELIGIBLE, None

    # The award states a requirement the student has not answered.
    if have is None or (isinstance(have, list) and not have):
        return UNKNOWN, f"{spec.name}: award requires this; your profile does not say. {src or ''}".strip()

    if spec.kind == "set_overlap":
        req, mine = _as_set(required), _as_set(have)
        if req & spec.wildcards:
            return ELIGIBLE, None
        if mine == {NONE_DECLARED}:
            # Affirmatively "none of these apply to me", so no requirement in
            # this category can ever be satisfied.
            verdict = EXCLUDED if confidence >= EXCLUSION_CONFIDENCE_THRESHOLD else UNKNOWN
            return verdict, f"{spec.name}: requires {sorted(req)}; you indicated none apply. {src or ''}".strip()
        if req & mine:
            return ELIGIBLE, None
        verdict = EXCLUDED if confidence >= EXCLUSION_CONFIDENCE_THRESHOLD else UNKNOWN
        return verdict, f"{spec.name}: requires {sorted(req)}, you have {sorted(mine)}. {src or ''}".strip()

    if spec.kind == "set_exclude":
        req, mine = _as_set(required), _as_set(have)
        hit = req & mine
        if not hit:
            return ELIGIBLE, None
        verdict = EXCLUDED if confidence >= EXCLUSION_CONFIDENCE_THRESHOLD else UNKNOWN
        return verdict, f"{spec.name}: explicitly excludes {sorted(hit)}. {src or ''}".strip()

    if spec.kind == "at_least":
        try:
            if float(have) >= float(required):
                return ELIGIBLE, None
        except (TypeError, ValueError):
            return UNKNOWN, f"{spec.name}: could not compare {have!r} to {required!r}"
        verdict = EXCLUDED if confidence >= EXCLUSION_CONFIDENCE_THRESHOLD else UNKNOWN
        return verdict, f"{spec.name}: requires {required}, you have {have}. {src or ''}".strip()

    if spec.kind == "must_be":
        if bool(have) == bool(required):
            return ELIGIBLE, None
        verdict = EXCLUDED if confidence >= EXCLUSION_CONFIDENCE_THRESHOLD else UNKNOWN
        return verdict, f"{spec.name}: requires {required}, you have {have}. {src or ''}".strip()

    raise ValueError(f"unhandled field kind {spec.kind!r}")


def match_award(award, profile):
    """Match one award against a profile.

    Returns {verdict, reasons, unknown_fields, excluded_fields}. An award with
    no structured eligibility at all is UNKNOWN, not ELIGIBLE — "we have not
    looked yet" must never present as "you qualify".
    """
    elig = award.get("eligibility")

    # Source-level defaults (e.g. every UW award requires attending UW) are
    # merged UNDER the award's own fields, which win on conflict.
    defaults = award.get("_source_defaults") or {}
    merged = dict(defaults)
    merged.update(elig or {})

    if not merged:
        return {
            "verdict": UNKNOWN,
            "reasons": ["No structured eligibility has been extracted for this award yet."],
            "unknown_fields": [],
            "excluded_fields": [],
        }

    verdict = ELIGIBLE
    reasons, unknown_fields, excluded_fields = [], [], []

    for spec in FIELD_SPECS:
        entry = merged.get(spec.name)
        if not entry:
            continue
        field_verdict, reason = match_field(spec, entry, profile)
        if reason:
            reasons.append(reason)
        if field_verdict == UNKNOWN:
            unknown_fields.append(spec.name)
        elif field_verdict == EXCLUDED:
            excluded_fields.append(spec.name)
        if _SEVERITY[field_verdict] < _SEVERITY[verdict]:
            verdict = field_verdict

    # Nomination-gated awards are never straightforwardly "apply to this".
    # Engineers Canada, the IEEE Canadian Foundation and Engineers Foundation
    # Ontario all fit a strong candidate perfectly on paper and cannot be
    # applied to directly, so a plain ELIGIBLE would waste real effort.
    app_type = award.get("application_type")
    if verdict == ELIGIBLE and app_type and app_type != "open":
        reasons.append(f"Not an open application: {app_type.replace('_', ' ')}.")

    return {
        "verdict": verdict,
        "reasons": reasons,
        "unknown_fields": unknown_fields,
        "excluded_fields": excluded_fields,
    }


def group_by_verdict(awards, profile):
    """Partition awards into the three buckets the UI renders."""
    out = {ELIGIBLE: [], UNKNOWN: [], EXCLUDED: []}
    for award in awards:
        result = match_award(award, profile)
        out[result["verdict"]].append((award, result))
    return out
