"""
The adapter contract.

An adapter turns one source into AwardRecords. Everything source-specific —
HTML shape, pagination, id scheme, whether a browser is needed — lives inside
one adapter; everything downstream (the database, the export, the matcher, the
site) only ever sees AwardRecord.

Adding a source should mean writing one file here and one sources/*.yaml entry.
If it means touching export_data.py or the frontend, the abstraction is wrong.
"""
import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class AwardRecord:
    """One award, normalized. Field names match the awards table columns."""

    source_id: str
    native_id: str
    award_name: str

    # Prose, all optional — sources vary wildly in what they publish.
    award_description: Optional[str] = None
    eligibility_selection_criteria: Optional[str] = None
    award_value_description: Optional[str] = None
    application_details: Optional[str] = None
    required_supporting_documents: Optional[str] = None
    contact_detail: Optional[str] = None
    additional_instructions: Optional[str] = None

    # Faceted fields. The UW vocabulary is the current lingua franca; adapters
    # map into it where they can and leave it empty where they cannot, rather
    # than inventing values that would pollute the facet lists.
    career: Optional[str] = None
    level: Optional[str] = None
    area_of_study: Optional[str] = None
    affiliation: Optional[str] = None
    application_selection: Optional[str] = None
    award_type: Optional[str] = None

    # Schema v2.
    source_url: Optional[str] = None
    deadline_raw: Optional[str] = None
    deadline_date: Optional[str] = None
    amount_min: Optional[int] = None
    amount_max: Optional[int] = None
    amount_raw: Optional[str] = None
    renewable: Optional[bool] = None
    application_type: Optional[str] = None

    #: Structured eligibility, in the provenanced shape tools/eligibility.py
    #: validates: {field: {"value": [...], "confidence": float, "src": str}}.
    #: An adapter fills this only where the SOURCE states the constraint
    #: structurally -- EFC publishes separate "scholarships for women" and
    #: "for western canada" pages, so membership of one is a fact, not a guess.
    #: It is exported as `eligibility` and is not yet wired to a filter; the
    #: matcher is tools/extract_eligibility.py's job. Capturing it now means the
    #: provenance exists when that lands, rather than needing a re-crawl.
    eligibility: Optional[dict] = None

    #: Where this award is in its application cycle: "Open", "Ended",
    #: "Upcoming", or None for the many sources that publish no such state.
    #: None means UNKNOWN and must never be read as closed -- most
    #: AcademicWorks tenants render no status cell at all, so reading absence
    #: as "Ended" would hide thousands of live awards.
    application_status: Optional[str] = None

    raw_fields: dict = field(default_factory=dict)

    def as_row(self):
        d = asdict(self)
        d.pop("raw_fields")
        # The column is eligibility_json and holds serialized JSON; the
        # dataclass field is a dict so adapters never hand-build JSON strings.
        eligibility = d.pop("eligibility", None)
        d["eligibility_json"] = json.dumps(eligibility, ensure_ascii=False) if eligibility else None
        return d


class Adapter:
    """Base class. Subclasses implement fetch_all()."""

    #: registry id; must match a sources/*.yaml entry. Multi-tenant adapters
    #: (one parser, several institutions) leave this None and read their id
    #: from the registry entry instead.
    source_id = None

    def __init__(self, fetcher, entry=None, limit=None, fetch_detail=True):
        self.fetcher = fetcher
        self.entry = entry or {}
        self.limit = limit
        self.fetch_detail = fetch_detail

    @property
    def id(self):
        """The source id this run writes under."""
        return self.entry.get("id") or self.source_id

    def fetch_all(self):
        """Yield AwardRecord. Implemented per source."""
        raise NotImplementedError


# --- shared helpers -------------------------------------------------------

_WS = re.compile(r"[ \t   ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


#: Characters some sources use as a bullet/line separator instead of a newline,
#: plus the replacement character. SFU's award database has lost its bullets to
#: a historical encoding accident and now serves literal U+FFFD (the server
#: declares utf-8 and the bad bytes are already in its data, so this is not a
#: decoding bug on our side) separated by U+000B VERTICAL TAB. Mapping the
#: vertical tab to a newline actually recovers the list structure that was
#: there, which matters because those bullets are the eligibility criteria.
_VERTICAL_SEPARATORS = ("\x0b", "\x0c", "\u2028", "\u2029")
_DROPPED_CHARS = ("\ufffd",)


def clean_text(value):
    """Collapse whitespace, normalize unicode, return None for empties.

    Keeps newlines: UW's eligibility criteria arrive as newline-separated
    bullet lines and downstream extraction reads better with that structure
    intact, so this is deliberately not a full whitespace collapse.
    """
    if value is None:
        return None
    value = unicodedata.normalize("NFC", value)
    for ch in _DROPPED_CHARS:
        value = value.replace(ch, "")
    for ch in _VERTICAL_SEPARATORS:
        value = value.replace(ch, "\n")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _WS.sub(" ", value)
    value = "\n".join(line.strip() for line in value.split("\n"))
    value = _BLANK_LINES.sub("\n\n", value).strip()
    return value or None


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value, max_length=80):
    """Stable, URL-safe id derived from a title.

    Used as a native_id fallback when a source gives an award no identifier of
    its own — Alberta Student Aid's Alexander Rutherford card, for instance,
    has no detail link at all. A title-derived slug is not ideal (a renamed
    award becomes a new record) but it is deterministic, which matters more
    than elegance for a primary key.
    """
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = _SLUG_STRIP.sub("-", value).strip("-")
    return value[:max_length] or "unknown"


# A dollar figure, allowing either separator for thousands. Some sources use a
# SPACE -- Lethbridge writes "$1 000 minimum" for 344 of its awards and NAIT
# writes "Up to $2 000" -- and a pattern that stops at whitespace reads those as
# $1 and $2. A $2,000 scholarship showing as worth two dollars is not a cosmetic
# problem: it sinks below every threshold a student sets on the value filter.
#
# Groups are matched as 1-3 digits followed by runs of exactly 3, so the
# separator cannot swallow the following word ("$1 000 minimum" stops before
# "minimum") and two figures in one string stay separate ("$500 to $1 000" is
# 500 and 1000, not 5001000).
_MONEY = re.compile(r"\$\s?(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")

#: Figures at or below this are placeholders, not award values. These tenants
#: publish "$0.00" and "$0.01" to mean "not stated", and recording that as a real
#: value of zero makes an award look worthless instead of unspecified -- and
#: unstated has to stay null, because that is what keeps the range filter from
#: judging it.
_MIN_MEANINGFUL_AMOUNT = 1

#: Longest text still treated as being *the* amount string rather than prose
#: that merely mentions money. Alberta Student Aid's "Award" cell averages 57
#: characters and carries meaning the bare figures lose ("up to", "per year"),
#: so short input is kept verbatim. Anything longer is a description.
RAW_VERBATIM_LIMIT = 120


def parse_amounts(text):
    """(amount_min, amount_max, amount_raw) from prose. All may be None.

    Deliberately conservative about the NUMBERS: it reports the range of dollar
    figures it can see and makes no attempt to work out which one a given
    student would receive. "Varies from $4,000 to $6,000" and "up to 10
    scholarships of $2,500 each" mean very different things, and guessing
    between them would put a wrong number in front of someone deciding where to
    spend their time.

    Deliberately conservative about the RAW STRING too, which it did not used
    to be. It returned clean_text() of its whole input, which is correct for
    the field it was written against — a short amount cell — and badly wrong
    once AcademicWorks started calling it on a full detail body. One UofA award
    ended up with 3,407 characters of description in amount_raw, a verbatim
    second copy of prose already carried in award_description; across one
    tenant that is ~6 MB of duplication shipped to every visitor. So long input
    contributes only the figures it actually matched, and long input with no
    figure at all contributes nothing.
    """
    if not text:
        return None, None, None

    matches = list(_MONEY.finditer(text))
    values = []
    for match in matches:
        try:
            digits = match.group(1).replace(",", "").replace(" ", "").replace("\u00a0", "")
            value = int(float(digits))
        except ValueError:
            continue
        if value >= _MIN_MEANINGFUL_AMOUNT:
            values.append(value)

    cleaned = clean_text(text)
    short = cleaned is not None and len(cleaned) <= RAW_VERBATIM_LIMIT

    if not values:
        # "Varies" is a real answer to "how much?" and must survive. A whole
        # description that never names a figure is not an amount at all.
        return None, None, cleaned if short else None

    if short:
        raw = cleaned
    else:
        # Preserve each figure exactly as written — "$1,000", not "1000" — so
        # the string stays auditable against the source page.
        seen, spans = set(), []
        for match in matches:
            span = match.group(0).strip()
            if span not in seen:
                seen.add(span)
                spans.append(span)
        raw = "; ".join(spans)[:RAW_VERBATIM_LIMIT]

    return min(values), max(values), raw
