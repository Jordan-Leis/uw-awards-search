"""
Blackbaud Award Management ("AcademicWorks") — *.academicworks.ca

The biggest single multiplier in the project: one parser covers five Canadian
tenants and roughly 6,400 awards. Each institution gets its own sources/*.yaml
entry pointing at this module, differing only by host.

    alberta.academicworks.ca      ~2,428
    umanitoba.academicworks.ca    ~3,145
    trentu.academicworks.ca         ~770
    usask.academicworks.ca           ~51
    concordia.academicworks.ca        ~3

robots.txt on these hosts is "User-Agent: * / Disallow:" — allow-all, with only
EasouSpider blocked. Verified per-host at fetch time by PoliteFetcher, not
assumed from this comment.

DISCOVERING TENANTS — DNS PROVES NOTHING
Both TLDs answer for every subdomain. An earlier version of this comment said
the wildcard catch-all was a .com problem; it is not. All 100 slugs in a DNS
sweep of `*.academicworks.ca` resolved, including the nonsense control
`redderrer`, so a name that resolves is not a tenant.

Tell them apart over HTTP instead: a real tenant serves `/opportunities` from
its own host and titles the page `All Opportunities - <Institution>`, while
anything else redirects to `www.blackbaud.com`. tools/probe_academicworks.py
does this, and keeps a nonsense slug in its default list so the discriminator
is self-testing. Also `western.academicworks.com` is Western Colorado
University, not Western Ontario.

LISTING VS DETAIL
The listing is cheap: per_page=500 works, so 2,428 awards is five requests.
But its descriptions are truncated with an ellipsis, and truncated text is
exactly how eligibility constraints go missing — the Alberta Student Aid pass
lost two grade floors and nearly lost an Ontario residency clause that way.
Detail pages carry the full text and cost one request each, so a complete run
of the largest tenant is ~2,400 requests. --no-detail exists for a fast
index-only pass; it is not suitable input for eligibility extraction.
"""
import re
from urllib.parse import urljoin, urlsplit

from . import minidom
from .base import Adapter, AwardRecord, clean_text, parse_amounts

PER_PAGE = 500
MAX_PAGES = 60          # 30,000 awards; a runaway pager stops here

_OPPORTUNITY_HREF = re.compile(r"^/opportunities/(\d+)$")
_DEADLINE_DATE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")

# Row-level statuses that are not categories. The first cell holds either the
# award's category ("Faculty of Law") or its application state ("Open").
_STATUS_WORDS = {"open", "ended", "closed", "upcoming", "archived", "draft"}

# The "Award" column usually holds a category ("Faculty of Law") but sometimes
# holds a dollar figure instead. Left unclassified it lands in area_of_study,
# and because that field is comma-split downstream, "$2,700.00" becomes the two
# bogus programs "$2" and "700.00" in the Program filter.
_CURRENCY_ONLY = re.compile(r"^\$?\s*[\d,]+(?:\.\d{2})?\s*$")

# --- normalization ------------------------------------------------------
# All three of these exist because a live dry run against the UofA tenant
# produced field values that would have been wrong in the UI rather than
# obviously broken in the logs.

#: Raw row status -> the value stored in application_status. "Closed" and
#: "Archived" mean the same thing to a student as "Ended", so they collapse.
#: Anything not listed here stays None, i.e. UNKNOWN.
_STATUS_MAP = {
    "open": "Open",
    "ended": "Ended",
    "closed": "Ended",
    "archived": "Ended",
    "upcoming": "Upcoming",
    "draft": None,
}

#: Faculty names that differ only in punctuation or house style, keyed by their
#: normalized form. UofA's 51 distinct category values include four such pairs,
#: and because area_of_study drives the Program filter, each pair would render
#: as two entries -- so a student picking one silently misses every award filed
#: under the other.
#:
#: This table is EXPLICIT rather than learned at run time. An earlier version
#: remembered the first spelling it saw per key, which made the published facet
#: value depend on the order the tenant happened to page its listing: the same
#: data could export as "Medicine & Dentistry" one refresh and "Medicine and
#: Dentistry" the next, churning VOCAB_VERSION and invalidating any saved
#: filter selection for no reason. A pinned table is a pure function of the
#: input.
#:
#: Unlisted names fall through to the generic cleanup, so two spellings of a
#: faculty nobody has noticed yet still produce two values. That is a visible
#: duplicate in the filter, not silent data loss, and adding a line here fixes
#: it.
_CANONICAL_FACULTY = {
    "faculty of business": "Alberta School of Business",
    "alberta school of business": "Alberta School of Business",
    "faculty of medicine and dentistry": "Faculty of Medicine & Dentistry",
    "faculty of agricultural life and environmental sciences":
        "Faculty of Agricultural Life & Environmental Sciences",
    "faculty of kinesiology sport and recreation":
        "Faculty of Kinesiology Sport and Recreation",
}

# Career signals come in two strengths, because they disagree in practice.
# A DEGREE LEVEL is decisive: "Master's" means a master's student. A POSITION
# in a program is only suggestive: "Entrance" usually means an incoming
# undergraduate, but "Master's Entrance Scholarship" is a graduate award, and
# treating the two signals as equals made that award resolve to None.
_GRADUATE_LEVEL = re.compile(
    r"\b(graduate|doctoral|doctorate|ph\.?\s?d|master'?s|masters|postdoctoral|post-doctoral)\b",
    re.I)
_UNDERGRAD_LEVEL = re.compile(r"\b(undergraduate|bachelor'?s|baccalaureate)\b", re.I)
_UNDERGRAD_HINT = re.compile(
    r"\b(first[- ]year|second[- ]year|third[- ]year|fourth[- ]year|entrance|high school)\b",
    re.I)


def normalize_status(value):
    """A row's application state, or None when the source does not say.

    Most tenants render no status cell -- measured across the live estate, only
    UofA and a few colleges do, and every other tenant returns zero "Open".
    Absence therefore has to mean unknown. Reading it as closed would hide
    Manitoba's entire 3,145-award catalogue.
    """
    if not value:
        return None
    return _STATUS_MAP.get(value.strip().lower())


#: A fragment naming an organisational unit starts a new faculty; one without
#: any of these words is a continuation of the previous fragment. This is what
#: lets "Faculty of Kinesiology, Sport, and Recreation, Faculty of Science" be
#: read as two faculties rather than four fragments.
_UNIT_WORD = re.compile(r"\b(faculty|facult[eé]|school|college|campus|department|"
                        r"institute|programme?s?)\b", re.I)


def _split_or(fragment):
    """Split "A or B" only when BOTH sides name an organisational unit.

    "College of Natural & Applied Sciences or Alberta School of Business" is two
    faculties; "Faculty of Kinesiology, Sport and Recreation" is one. Requiring a
    unit word on both sides is what tells them apart, and it leaves ordinary
    prose alone.
    """
    parts = re.split(r"\s+or\s+", fragment)
    if len(parts) > 1 and all(_UNIT_WORD.search(p) for p in parts):
        return [p.strip() for p in parts]
    return [fragment]


def _split_units(text):
    """A multi-faculty cell as a list of whole faculty names.

    The cell is comma-separated, but two UofA faculties are themselves spelled
    with commas, so splitting on comma alone shreds them -- one cell became the
    browsable "programs" `Sport` and `and Recreation`. Fragments carrying no
    organisational-unit word are re-joined to the fragment before them, and
    "A or B" is split only when both sides name a unit.
    """
    units = []
    for fragment in (f.strip() for f in text.split(",")):
        if not fragment:
            continue
        if units and not _UNIT_WORD.search(fragment):
            units[-1] = f"{units[-1]}, {fragment}"
        else:
            units.extend(_split_or(fragment))
    return units or [text]


def _canonical_unit(text):
    """One faculty name, canonicalized and comma-free."""
    # "Faculty of Medicine & Dentistry" vs "... and Dentistry", and the Oxford
    # comma in "Kinesiology, Sport, and Recreation". Normalizing for lookup
    # only; the returned string keeps the source's own "&" spelling, which is
    # the majority form in every colliding pair.
    key = text.replace("&", "and").lower()
    key = re.sub(r",\s*and\b", " and", key)
    key = re.sub(r"[\s,]+", " ", key).strip()

    if key in _CANONICAL_FACULTY:
        return _CANONICAL_FACULTY[key]
    # area_of_study is comma-split downstream, so a value may not contain a
    # comma of its own: "Faculty of Kinesiology, Sport and Recreation" would
    # otherwise become the browsable programs "Faculty of Kinesiology", "Sport"
    # and "and Recreation".
    display = re.sub(r",\s*and\b", " and", text)
    return re.sub(r"\s*,\s*", " ", display).strip()


def canonical_category(value):
    """The category cell as a usable area_of_study, or None.

    Three jobs. Reject dollar figures: the "Award" column sometimes holds one,
    and because area_of_study is comma-split downstream, "$2,700.00" became the
    two bogus programs "$2" and "700.00" in the Program filter. Collapse
    punctuation-only variants of the same faculty name, which UofA has four
    pairs of. And keep each name comma-free so the downstream split yields whole
    faculties.
    """
    text = clean_text(value)
    if not text or _CURRENCY_ONLY.match(text) or "$" in text:
        # A dollar sign is decisive: no faculty or program is named with one, and
        # several tenants put the award VALUE in this column. Lethbridge does it
        # for 344 of its 696 awards, which filled the Program filter with
        # "$1 000 minimum" and friends. The narrower _CURRENCY_ONLY check missed
        # them all because they carry trailing words.
        return None
    return ", ".join(_canonical_unit(u) for u in _split_units(text))


def infer_career(award_name, body):
    """"Undergraduate" / "Graduate" / None from an award's title and prose.

    UofA files graduate awards in the same feed as undergraduate ones -- 105
    say so in the title alone -- and this project is scoped to undergraduates.
    career is an existing facet, so tagging is enough; nothing new is needed to
    filter on it.

    Ambiguity resolves to None on purpose. Once a career filter is applied, a
    wrong "Graduate" tag HIDES an award an undergraduate could win, which is
    the expensive direction to be wrong. Two rules keep that rare:

      * A stated degree level decides, and outranks any positional hint, so
        "Master's Entrance Scholarship" is Graduate rather than a coin toss.
      * The title outranks the body, because "graduate students should apply
        elsewhere" is common boilerplate on an undergraduate award.

    An award naming BOTH levels is open to both and so is restricted to
    neither; it stays None.
    """
    for text in (award_name, body):
        if not text:
            continue
        grad = bool(_GRADUATE_LEVEL.search(text))
        under = bool(_UNDERGRAD_LEVEL.search(text))
        if grad and under:
            return None          # explicitly open to both
        if grad or under:
            return "Graduate" if grad else "Undergraduate"
        if _UNDERGRAD_HINT.search(text):
            return "Undergraduate"
    return None


class AcademicWorksAdapter(Adapter):
    # Multi-tenant: the id comes from the registry entry, not from here.
    source_id = None

    def __init__(self, *args, detail_all=False, **kwargs):
        super().__init__(*args, **kwargs)
        #: Crawl detail pages even for awards whose window has closed.
        self.detail_all = detail_all

    @property
    def host(self):
        host = self.entry.get("academicworks_host")
        if not host:
            raise RuntimeError(
                f"[{self.id}] registry entry needs 'academicworks_host', "
                f"e.g. alberta.academicworks.ca"
            )
        return host

    @property
    def base_url(self):
        return f"https://{self.host}"

    def fetch_all(self):
        seen = set()
        emitted = 0

        for page in range(1, MAX_PAGES + 1):
            url = f"{self.base_url}/opportunities?per_page={PER_PAGE}&page={page}"
            html = self.fetcher.get(url)
            rows = self._parse_listing(html)

            # An empty page is the end. A page whose ids we have all seen means
            # the pager is looping (some tenants clamp `page` silently rather
            # than 404), which would otherwise spin until MAX_PAGES.
            new_rows = [r for r in rows if r["native_id"] not in seen]
            if not rows or not new_rows:
                break

            for row in new_rows:
                seen.add(row["native_id"])
                record = self._build_record(row)
                if self.fetch_detail and self._wants_detail(record):
                    try:
                        self._enrich_from_detail(record)
                    except Exception as e:
                        record.raw_fields["detail_error"] = str(e)
                yield record
                emitted += 1
                if self.limit and emitted >= self.limit:
                    return

            if len(rows) < PER_PAGE:
                break

    def _wants_detail(self, record):
        """Whether to spend a request on this award's detail page.

        A detail page costs one request, and across the estate most awards are
        closed: 2,164 of UofA's 2,428, 741 of Mount Royal's 788. Those are
        hidden by default and carry no deadline to apply to, so crawling them
        buys nothing the listing row does not already give — a name, a blurb, a
        category and a link — while tripling the crawl.

        Skipping them is safe in both directions. Unknown status is always
        crawled, so a source that publishes no status loses nothing. And when a
        closed award reopens, the next refresh sees "Open" and fetches the full
        text then.

        --detail-all overrides this; eligibility extraction over the closed
        archive would need it.
        """
        if self.detail_all or record.application_status != "Ended":
            return True
        record.raw_fields["detail_skipped"] = (
            "application window closed; listing text only")
        return False

    # -- listing ---------------------------------------------------------
    def _parse_listing(self, html):
        root = minidom.parse(html)
        rows = []
        for tr in root.find_all("tr"):
            link = next((a for a in tr.find_all("a")
                         if _OPPORTUNITY_HREF.match(a.get("href") or "")), None)
            if link is None:
                continue
            native_id = _OPPORTUNITY_HREF.match(link.get("href")).group(1)

            # Cells, in document order: [category-or-status] <th>name+blurb</th>
            # [deadline-or-status]. Reading them positionally is brittle, so
            # each is classified by content instead.
            cells = [c.get_text() for c in tr.find_all("td")]
            category = deadline = status = amount = None
            for text in cells:
                text = clean_text(text) or ""
                if not text:
                    continue
                if text.lower() in _STATUS_WORDS:
                    status = text
                elif _CURRENCY_ONLY.match(text) or "$" in text:
                    # Cells are classified by content, not position, and a cell
                    # naming money is an amount however much prose surrounds it
                    # ("$1 000 minimum", "Up to $2 000").
                    amount = text
                elif _DEADLINE_DATE.search(text):
                    # The cell renders as "Deadline 01/15/2027"; the label is
                    # presentation, not data.
                    deadline = re.sub(r"^\s*Deadline\s*", "", text).strip()
                elif category is None:
                    category = text

            th = tr.find("th")
            blurb = None
            if th:
                inner = th.find("div")
                if inner:
                    blurb = clean_text(inner.get_text())

            rows.append({
                "native_id": native_id,
                "name": clean_text(link.get_text()),
                "url": urljoin(self.base_url, link.get("href")),
                "category": category,
                "deadline": deadline,
                "status": status,
                "amount": amount,
                "blurb": blurb,
            })
        return rows

    def _build_record(self, row):
        deadline_raw = row["deadline"]
        deadline_date = None
        if deadline_raw:
            m = _DEADLINE_DATE.search(deadline_raw)
            if m:
                # AcademicWorks renders MM/DD/YYYY. Stored as ISO only because
                # the format here is unambiguous; sources that publish prose
                # windows keep deadline_date null rather than inventing a date.
                month, day, year = m.groups()
                deadline_date = f"{year}-{month}-{day}"

        amount_min, amount_max, amount_raw = parse_amounts(row.get("amount"))

        # Inferred from the title alone here; _enrich_from_detail retries with
        # the full body, which is a much stronger signal.
        career = infer_career(row["name"], None)

        raw_fields = {k: v for k, v in row.items() if v and k not in ("name", "url")}
        if career:
            # Every inference is recorded with what produced it, the same way
            # tools/eligibility.py stores `src`. An unattributed guess in a
            # filterable field is not auditable, and this one can hide awards.
            raw_fields["career_inferred_from"] = "title"

        return AwardRecord(
            source_id=self.id,
            native_id=row["native_id"],
            award_name=row["name"],
            award_description=row["blurb"],
            area_of_study=canonical_category(row["category"]),
            career=career,
            amount_min=amount_min,
            amount_max=amount_max,
            amount_raw=amount_raw,
            source_url=row["url"],
            deadline_raw=deadline_raw,
            deadline_date=deadline_date,
            application_type="open",
            application_status=normalize_status(row.get("status")),
            raw_fields=raw_fields,
        )

    # -- detail ----------------------------------------------------------
    def _enrich_from_detail(self, record):
        html = self.fetcher.get(record.source_url)
        root = minidom.parse(html)

        main = root.find("main") or root.find("div", class_="container") or root
        text = main.get_text()

        # The detail page is a flat run of text: title, full description, then
        # "Award" / category and "Deadline" / date. Everything between the
        # title and the first trailing label is the description.
        body = self._extract_description(text, record.award_name)
        if body:
            record.award_description = body
            # These pages carry one prose blob rather than a labelled
            # eligibility section, and it is where the criteria live.
            record.eligibility_selection_criteria = body
            # The listing cell is a DEDICATED amount field; the body is prose
            # that merely mentions money, and a description can name a donor's
            # endowment, a tuition figure or a token "$1". So the body only
            # fills the amount when the cell gave nothing.
            #
            # This used to overwrite unconditionally, which produced records
            # whose amount_raw and amount_max disagreed: NAIT's Frances Camyre
            # Memorial Bursary carried amount_raw "$1,000.00" from its cell and
            # amount_max 1 from a stray "$1" in its prose.
            if record.amount_max is None:
                lo, hi, raw = parse_amounts(body)
                if lo is not None:
                    record.amount_min, record.amount_max, record.amount_raw = lo, hi, raw

            if record.career is None:
                career = infer_career(None, body)
                if career:
                    record.career = career
                    record.raw_fields["career_inferred_from"] = "detail body"
        else:
            record.raw_fields["detail_warning"] = "no description recovered from detail page"

    @staticmethod
    def _extract_description(text, award_name):
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        # Drop everything up to and including the award's own title line.
        start = 0
        for i, line in enumerate(lines):
            if award_name and award_name in line:
                start = i + 1
                break
        # Stop at the trailing metadata labels.
        stop = len(lines)
        for i in range(start, len(lines)):
            if lines[i] in ("Award", "Deadline", "Donor", "Donors", "Supplemental Questions"):
                stop = i
                break
        body = [ln for ln in lines[start:stop]
                if ln not in ("Skip to Navigation", "Skip to Content", "Sign In",
                              "Opportunities", "Scholarship", "Ours", "Donors")]
        return clean_text("\n".join(body))
