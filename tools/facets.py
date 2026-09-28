"""
The single definition of what is filterable.

WHY THIS FILE EXISTS
The facet list used to be hardcoded in four places — export_data.facet_counts(),
find_awards.facet_counts(), search.js uniqueValues(), and gen_vocab's
FACET_TO_FILTER_KEY — each with its own copy of the same six keys. Adding one
filter meant four coordinated edits, and the copies had already drifted:
search.js treats affiliation as an OR facet while find_awards treats it as an
eligibility gate, so the website and the CLI disagree about who qualifies for
94 awards.

Everything now derives from FACETS here. Python imports it directly; the
browser reads the compiled form from site/data/filters.json; gen_vocab builds
the Worker's enums from it. Adding a filter is one entry.

Stdlib only, like the rest of tools/.

SEMANTICS — the distinction that matters
  "facet"  An award that states no value is UNCONSTRAINED and matches any
           selection. Program, term and level work this way. This is why
           Alberta Student Aid (which publishes no taxonomy at all) does not
           vanish the moment a program filter is applied.
  "gate"   An award that states values is RESTRICTED to them; an award that
           states none is open to everyone, and an empty SELECTION means the
           student has not answered, so it hides nothing.
  "tag"    Browse semantics. No selection shows everything; selecting a value
           shows only awards carrying it.

TWO MODES, BECAUSE THERE ARE TWO QUESTIONS
Affiliation is the one field where the same selection means different things
depending on who is asking, so matches() takes a mode:

  mode="browse"   (default, the filter bar) "Show me awards restricted to X."
                  Picking "Women" returns the 94 women's awards.
  mode="profile"  (find_awards, Match-my-profile) "I AM X — what can I win?"
                  Picking "Women" returns the 94 plus every unrestricted
                  award, because a woman is eligible for those too.

This is a deliberate, named difference rather than the accidental drift that
existed before, where search.js and find_awards.py simply disagreed. Treating
the profile case as browse narrowed the CLI's result set from 201 awards to 2.
Getting these backwards is the single most expensive kind of bug here, in both
directions — a facet treated as a gate hides awards a student could win, and a
gate treated as a facet wastes their time on ones they cannot.
"""

# Kinds:
#   list    award field is a list of strings; vocabulary is derived from data
#   scalar  award field is a single string; vocabulary is derived from data
#   range   numeric; UI renders min/max rather than checkboxes
#   bool    tri-state yes/no/any
FACETS = [
    # --- who you are -----------------------------------------------------
    {
        "key": "career", "filter_key": "career", "label": "Career",
        "kind": "scalar", "semantics": "facet", "group": "Study",
        "help": "Undergraduate or graduate.",
    },
    {
        "key": "levels", "filter_key": "level", "label": "Year of study",
        "kind": "list", "semantics": "facet", "group": "Study",
        "help": "Which year(s) of a program an award is open to.",
    },
    {
        "key": "areas_of_study", "filter_key": "areaOfStudy", "label": "Program",
        "kind": "list", "semantics": "facet", "group": "Study",
        "searchable": True,
        "help": "Faculty-wide entries ('Engineering Faculty - All Programs') "
                "match every program in that faculty.",
    },
    {
        "key": "affiliations", "filter_key": "affiliation", "label": "Restricted to",
        "kind": "list", "semantics": "tag", "group": "Eligibility",
        "help": "Awards reserved for a specific group. Leave blank to see "
                "everything; pick a group to see only its awards.",
    },

    # --- what the award is ----------------------------------------------
    {
        "key": "award_types", "filter_key": "awardType", "label": "Award type",
        "kind": "list", "semantics": "facet", "group": "Award",
    },
    {
        "key": "terms", "filter_key": "term", "label": "Term",
        "kind": "list", "semantics": "facet", "group": "Award",
        "help": "The term an award is applied for or paid in — not a deadline.",
    },
    {
        "key": "source_id", "filter_key": "source", "label": "Source",
        "kind": "scalar", "semantics": "facet", "group": "Award",
        "help": "Which institution or organisation published the award.",
    },
    {
        "key": "application_status", "filter_key": "applicationStatus",
        "label": "Application status", "kind": "scalar", "semantics": "facet",
        "group": "Award",
        # Selecting "Open" still keeps awards whose status is unstated, because
        # a scalar "facet" treats no value as unconstrained -- which is the
        # whole point here. Only UofA and a few colleges publish a status at
        # all; every other AcademicWorks tenant returns nothing, so reading
        # silence as closed would hide Manitoba's entire 3,145-award catalogue.
        "default": ["Open"],
        "help": "Awards whose application window has closed are hidden by "
                "default. Many are annual and will reopen, so they are kept "
                "and can be shown — sources that publish no status at all "
                "are always shown.",
    },
    {
        "key": "application_type", "filter_key": "applicationType",
        "label": "How you apply", "kind": "scalar", "semantics": "facet",
        "group": "Award",
        "help": "Nomination-only and institution-mediated awards cannot be "
                "applied for directly, however well you match them.",
    },

    # --- numeric / derived ----------------------------------------------
    {
        "key": "amount_max", "filter_key": "amount", "label": "Award value",
        "kind": "range", "semantics": "facet", "group": "Award",
        "unit": "$",
        "help": "Only awards that state a dollar figure. Setting this hides "
                "awards whose value is not published.",
    },
    {
        "key": "deadline_date", "filter_key": "hasDeadline",
        "label": "Has a known deadline", "kind": "bool", "semantics": "facet",
        "group": "Award",
        "help": "Many sources publish a prose window rather than a date.",
    },
]

# Eligibility facets, populated by tools/extract_eligibility.py. Declared here
# so the UI, the CLI and the Worker vocabulary all light up together the moment
# extraction lands, instead of needing another four-file edit.
ELIGIBILITY_FACETS = [
    {"key": "residency", "filter_key": "province", "label": "Your province",
     "kind": "list", "semantics": "facet", "group": "Eligibility",
     "help": "Residency requirements, e.g. 12 months in Alberta."},
    {"key": "citizenship", "filter_key": "citizenship", "label": "Status in Canada",
     "kind": "list", "semantics": "facet", "group": "Eligibility"},
    {"key": "institution", "filter_key": "institution", "label": "School you attend",
     "kind": "list", "semantics": "facet", "group": "Eligibility"},
    {"key": "identity", "filter_key": "identity", "label": "Identity / background",
     "kind": "list", "semantics": "tag", "group": "Eligibility"},
    {"key": "financial_need", "filter_key": "financialNeed", "label": "Requires financial need",
     "kind": "bool", "semantics": "facet", "group": "Eligibility"},
    {"key": "gpa_min", "filter_key": "gpa", "label": "Minimum grade required",
     "kind": "range", "semantics": "facet", "group": "Eligibility", "unit": "%",
     "help": "Only awards that state a grade requirement."},
]

ALL_FACETS = FACETS + ELIGIBILITY_FACETS

BY_FILTER_KEY = {f["filter_key"]: f for f in ALL_FACETS}
BY_KEY = {f["key"]: f for f in ALL_FACETS}

#: Facets whose vocabulary is small and stable enough to ship as a Gemini enum.
#: areaOfStudy is excluded because it exceeds Gemini's measured 122-value enum
#: ceiling and rides in the prompt instead (see ai-search/prompt.js).
VOCAB_FACETS = [f for f in FACETS if f["kind"] in ("list", "scalar")]


def countable(facets=None):
    """Facets whose values are enumerated from the data."""
    return [f for f in (facets or ALL_FACETS) if f["kind"] in ("list", "scalar")]


def values_of(award, facet):
    """The award's value(s) for a facet, always as a list."""
    raw = award.get(facet["key"])
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, bool):
        return [raw]
    return [raw]


def count_values(awards, facets=None):
    """{filter-facing key: {value: count}} for every countable facet.

    Keyed by the DATA key (levels, areas_of_study, ...) to stay compatible with
    the index.json facets block that external consumers already read.
    """
    out = {}
    for facet in countable(facets):
        counts = {}
        for award in awards:
            for value in values_of(award, facet):
                if value in (None, ""):
                    continue
                counts[value] = counts.get(value, 0) + 1
        out[facet["key"]] = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return out


BROWSE = "browse"
PROFILE = "profile"


def matches(award, facet, selected, mode=BROWSE):
    """Does this award pass this facet's filter, given the selected values?

    Implements the facet/gate/tag distinctions described at the top of this
    file. `mode` only affects "tag" facets — see TWO MODES above.
    """
    if facet["kind"] == "range":
        if isinstance(selected, dict):
            lo, hi = selected.get("min"), selected.get("max")
        elif isinstance(selected, (list, tuple)):
            lo, hi = selected
        else:
            lo = hi = None
        if lo is None and hi is None:
            return True

        value = award.get(facet["key"])
        if value is None:
            # A range is the one place where "unstated" is EXCLUDED, and the
            # exception is deliberate. Everywhere else, silence must not hide
            # an award a student could win. But award value is not an
            # eligibility criterion — it is the student narrowing by
            # preference — and an award with no stated figure cannot satisfy
            # "at least $10,000". Letting nulls through made the filter match
            # 2,795 of 4,016 awards, i.e. do nothing at all. The help text
            # says this out loud.
            return False
        if lo is not None and value < lo:
            return False
        if hi is not None and value > hi:
            return False
        return True

    if facet["kind"] == "bool":
        if selected is None:
            return True
        return bool(award.get(facet["key"])) is bool(selected)

    values = set(values_of(award, facet))

    if facet["semantics"] == "tag":
        if not selected:
            return True
        if mode == PROFILE:
            # "I am X, what can I win?" — an unrestricted award is winnable by
            # anyone, so it counts. This is the gate reading.
            if not values:
                return True
        # "Show me awards restricted to X" — untagged awards are not.
        return bool(values & set(selected))

    if facet["semantics"] == "gate":
        # Stated values restrict; no values means open to everyone.
        if not values:
            return True
        # An EMPTY selection means the student has not answered, which is not
        # the same as answering "none of these apply to me". Excluding a
        # restricted award on silence is the false-exclusion failure mode the
        # whole three-state design exists to avoid — and it is what made the
        # website hide 169 awards from a visitor who had simply not touched
        # the filter yet. Declaring nothing hides nothing.
        if not selected:
            return True
        return bool(values & set(selected))

    if not selected:
        return True
    if not values:
        return True              # unstated is unconstrained
    return bool(values & set(selected))


def to_spec():
    """The compiled form written to site/data/filters.json for the browser."""
    return {
        "version": 1,
        "facets": [
            {k: v for k, v in f.items() if k != "key"} | {"key": f["key"]}
            for f in ALL_FACETS
        ],
        "groups": list(dict.fromkeys(f["group"] for f in ALL_FACETS)),
    }
