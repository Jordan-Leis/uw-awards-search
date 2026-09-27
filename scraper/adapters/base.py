"""
The adapter contract.

An adapter turns one source into AwardRecords. Everything source-specific —
HTML shape, pagination, id scheme, whether a browser is needed — lives inside
one adapter; everything downstream (the database, the export, the matcher, the
site) only ever sees AwardRecord.

Adding a source should mean writing one file here and one sources/*.yaml entry.
If it means touching export_data.py or the frontend, the abstraction is wrong.
"""
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

    raw_fields: dict = field(default_factory=dict)

    def as_row(self):
        d = asdict(self)
        d.pop("raw_fields")
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


def clean_text(value):
    """Collapse whitespace, normalize unicode, return None for empties.

    Keeps newlines: UW's eligibility criteria arrive as newline-separated
    bullet lines and downstream extraction reads better with that structure
    intact, so this is deliberately not a full whitespace collapse.
    """
    if value is None:
        return None
    value = unicodedata.normalize("NFC", value)
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


_MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")


def parse_amounts(text):
    """(amount_min, amount_max, amount_raw) from prose. All may be None.

    Deliberately conservative: it reports the range of dollar figures it can
    see and makes no attempt to work out which one a given student would
    receive. "Varies from $4,000 to $6,000" and "up to 10 scholarships of
    $2,500 each" mean very different things, and guessing between them would
    put a wrong number in front of someone deciding where to spend their time.
    """
    if not text:
        return None, None, None
    values = []
    for match in _MONEY.finditer(text):
        try:
            values.append(int(float(match.group(1).replace(",", ""))))
        except ValueError:
            continue
    if not values:
        return None, None, clean_text(text)
    return min(values), max(values), clean_text(text)
