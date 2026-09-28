"""
Western University — studentservices.uwo.ca/secure/Awards/awardMain.cfm

The cheapest large source in the project: **one GET returns all 2,021 awards
with their full descriptions already inline.** There is no detail page to
fetch, no pagination, and no API to reverse-engineer. Measured 3.54 MB, exactly
2,021 <tr>, every row uniformly four cells.

robots.txt on studentservices.uwo.ca allows this path. Verified at fetch time
by adapters/http.PoliteFetcher, not assumed from this comment.

ROW SHAPE
    [0] award name
    [1] award name repeated, then the description
    [2] deadline as "MONTH DD" ("SEPTEMBER 30"), on 1,064 of 2,021 rows
    [3] "ESSAY REQUIRED", on 213 rows

Descriptions are genuinely complete — median 815 characters, max 2,789, and not
one of the 2,021 is empty. 1,874 of them state a dollar figure. That is why
this source needs no detail pass: unlike an AcademicWorks listing, nothing here
is truncated.

TWO THINGS THAT WILL BITE
  * There is no id anywhere in the HTML — no row key, no per-award link, no
    query parameter. native_id has to be derived from the name, and **20 names
    are duplicated**, so collisions are certain and need a deterministic
    suffix. Row order is NOT a safe tiebreaker: the page is regenerated and a
    reordering would silently repoint every permalink after the moved row.
    Ties are broken on the description instead, which is stable content.
  * The deadline has no year. "SEPTEMBER 30" recurs annually, so deadline_date
    stays null rather than inventing 2026-09-30 and telling a student an award
    has closed when it reopens in weeks.
"""
import re
from collections import Counter

from . import minidom
from .base import Adapter, AwardRecord, clean_text, parse_amounts, slugify

LIST_URL = "https://studentservices.uwo.ca/secure/Awards/awardMain.cfm"

EXPECTED_ROWS = 2021

#: Month names as the page spells them, used only to recognise a deadline cell.
#: Deliberately not parsed into a date — see the module docstring.
_DEADLINE = re.compile(
    r"^(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|"
    r"NOVEMBER|DECEMBER)\s+\d{1,2}$", re.I)


def parse_row(cells):
    """One table row's four cells -> a dict, or None if it is not an award row.

    Kept module-level and free of network access so the parsing rules are
    testable from a fixture.
    """
    cells = [clean_text(c) or "" for c in cells]
    if len(cells) < 2 or not cells[0]:
        return None

    name = cells[0]
    body = cells[1]
    # Cell [1] opens by repeating the name. Strip it so the description does
    # not start with its own title, which would also skew the Fuse index by
    # double-weighting every award's name.
    if body.startswith(name):
        body = body[len(name):].strip()

    deadline = None
    essay = None
    for extra in cells[2:]:
        if not extra:
            continue
        if _DEADLINE.match(extra):
            deadline = extra
        elif "ESSAY" in extra.upper():
            essay = extra

    return {"name": name, "description": body or None,
            "deadline_raw": deadline, "supporting_documents": essay}


def parse_listing(html):
    """Every award row on the page, in document order."""
    root = minidom.parse(html)
    rows = []
    for tr in root.find_all("tr"):
        row = parse_row([td.get_text() for td in tr.find_all("td")])
        if row:
            rows.append(row)
    return rows


def assign_native_ids(rows):
    """A stable native_id per row, disambiguating the 20 duplicated names.

    The suffix is ordered by the award's DESCRIPTION, not by its position on
    the page. Position would make every permalink after a reordering point at
    the wrong award, and this page carries no id to fall back on; description
    is the only stable thing that distinguishes two awards sharing a name.
    """
    counts = Counter(r["name"] for r in rows)
    groups = {}
    for row in rows:
        groups.setdefault(row["name"], []).append(row)

    ids = {}
    for name, group in groups.items():
        base = slugify(name)
        if counts[name] == 1:
            ids[id(group[0])] = base
            continue
        for n, row in enumerate(sorted(group, key=lambda r: r["description"] or ""), start=1):
            ids[id(row)] = base if n == 1 else f"{base}-{n}"
    return ids


class WesternAdapter(Adapter):
    source_id = "western"

    def fetch_all(self):
        html = self.fetcher.get(LIST_URL)
        rows = parse_listing(html)

        if not self.limit and len(rows) != EXPECTED_ROWS:
            # Not fatal: the real gate is min_awards in sources/western.yaml.
            # But a page this uniform changing size is worth saying out loud,
            # because the whole source arrives in one response and a layout
            # change would otherwise look like a quiet, plausible number.
            print(f"[western] parsed {len(rows)} rows, expected {EXPECTED_ROWS} "
                  f"— check the table layout")

        ids = assign_native_ids(rows)
        emitted = 0
        for row in rows:
            yield self._build_record(row, ids[id(row)])
            emitted += 1
            if self.limit and emitted >= self.limit:
                return

    def _build_record(self, row, native_id):
        amount_min, amount_max, amount_raw = parse_amounts(row["description"])
        return AwardRecord(
            source_id=self.id,
            native_id=native_id,
            award_name=row["name"],
            award_description=row["description"],
            # The description IS the eligibility text here — there is no
            # separate criteria field — so it is stored in both, matching how
            # adapters/academicworks.py handles its one prose blob.
            eligibility_selection_criteria=row["description"],
            required_supporting_documents=row["supporting_documents"],
            amount_min=amount_min,
            amount_max=amount_max,
            amount_raw=amount_raw,
            source_url=LIST_URL,
            deadline_raw=row["deadline_raw"],
            deadline_date=None,          # "MONTH DD" carries no year
            # career is deliberately left NULL, and neither obvious shortcut is
            # safe. The page declares no scope — its only heading is "WESTERN
            # AWARDS" — so tagging all 2,021 "Undergraduate" would be inventing
            # a fact. And inferring it from the prose is worse: 245 descriptions
            # contain a graduate-level word, but on inspection nearly all are
            # donor biography ("a Western graduate in the Faculty of Law",
            # "PhD'74") or an undergraduate award that funds FUTURE graduate
            # study ("outstanding potential for graduate studies"). Only 2 are
            # genuinely graduate. Inference would therefore mis-tag ~20
            # undergraduate awards as Graduate and hide them from the very
            # students they are for — the expensive direction to be wrong.
            # Unstated is unconstrained, so a NULL career still shows up for an
            # undergraduate search.
            application_type="open",
            raw_fields={k: v for k, v in row.items() if v and k != "name"},
        )
