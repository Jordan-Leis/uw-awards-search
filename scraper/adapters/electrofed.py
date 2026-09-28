"""
Electro-Federation Canada — electrofed.com

Small and the best-fitting source in the project for an electrical engineering
undergraduate: 46 scholarships from the industry's own trade association, one
application centre covering all of them, sponsored by ABB, Schneider, Hubbell,
Eaton, Southwire, Nexans and the rest. Most are open nationally, which is what
makes them worth carrying in the always-loaded payload while a 2,021-award
university listing is not.

46, not the 60 an earlier count suggested: the master list holds every one, and
the themed pages are subsets of it, so summing the pages double-counts.

THE USEFUL PART — CATEGORY PAGES ARE ELIGIBILITY
EFC files the same scholarships under themed pages, and membership is a
published fact rather than something inferred from prose:

    for-women                 15     for-quebec            5
    for-ontario                9     for-atlantic-canada   3
    for-indigenous-students    8     for-western-canada    2
    for-people-of-colour       6

So the crawl is: master list for the roster, each themed page for tags, then one
detail page per scholarship for the criteria text. 55 requests in total.

robots.txt allows all of this; verified at fetch time by PoliteFetcher.

WHY THE DETAIL PAGES ARE NOT OPTIONAL
The listing entry for a scholarship is 42-123 characters -- a name, "Value:
$3,500", "1 available" and a sponsor link. Everything that decides eligibility
("must have completed two years of study in business administration/commerce",
"minimum cumulative average of 80%") is on the detail page. With 46 awards there
is no reason not to fetch all of them.
"""
import re

from . import minidom
from .base import Adapter, AwardRecord, clean_text, parse_amounts

BASE = "https://www.electrofed.com"
MASTER_URL = f"{BASE}/about/efc-scholarship-program/efc-scholarships-list/"

_DETAIL_HREF = re.compile(r"/efc-scholarship/([a-z0-9\-]+)/")

#: Themed page slug -> what membership of it actually tells us. Confidences are
#: high because these are EFC's own categories, not a reading of prose; the
#: `src` is the page URL, which is the evidence a student can check.
#:
#: `identity` values reuse the vocabulary already in the corpus where one exists
#: ("Women", "Indigenous"). "People of colour" is new: the nearest existing
#: value is "Black", which is narrower than what EFC actually says, and
#: mislabelling a group is worse than adding a value.
CATEGORY_PAGES = {
    "efc-scholarships-for-women": {
        "affiliation": "Women",
        "identity": ["women"],
    },
    "efc-scholarships-for-indigenous-students": {
        "affiliation": "Indigenous",
        "identity": ["indigenous"],
    },
    "efc-scholarships-for-people-of-colour": {
        "affiliation": "People of colour",
        "identity": ["people-of-colour"],
    },
    "efc-scholarships-for-ontario": {"residency": ["ON"]},
    "efc-scholarships-for-quebec": {"residency": ["QC"]},
    "efc-scholarships-for-atlantic-canada": {"residency": ["NB", "NS", "PE", "NL"]},
    "efc-scholarships-for-western-canada": {"residency": ["AB", "BC", "SK", "MB"]},
}

_NUMBER = re.compile(r"\d")
#: A line that is nothing but a number, optionally with a currency symbol. Only
#: meaningful when attributed to the label on the preceding line.
_BARE_NUMBER = re.compile(r"\$?\s*[\d,]+(?:\.\d{2})?")


def category_url(slug):
    return f"{BASE}/about/efc-scholarship-program/{slug}/"


def slugs_in(html):
    """Scholarship slugs linked from a listing page."""
    return sorted(set(_DETAIL_HREF.findall(html)))


def eligibility_from_categories(categories):
    """Provenanced eligibility for the themed pages a scholarship appears on.

    Returns (eligibility_dict, affiliations_list). Residency values from several
    pages are unioned -- a scholarship listed under both Ontario and Quebec is
    open to either, not to neither.
    """
    eligibility = {}
    affiliations = []
    residency, identity, sources = [], [], []

    for slug in sorted(categories):
        mapping = CATEGORY_PAGES.get(slug)
        if not mapping:
            continue
        sources.append(category_url(slug))
        if mapping.get("affiliation") and mapping["affiliation"] not in affiliations:
            affiliations.append(mapping["affiliation"])
        for value in mapping.get("residency", []):
            if value not in residency:
                residency.append(value)
        for value in mapping.get("identity", []):
            if value not in identity:
                identity.append(value)

    if residency:
        eligibility["residency"] = {
            "value": residency, "confidence": 0.9,
            "src": "Listed by Electro-Federation Canada under "
                   + ", ".join(s for s in sources if "canada" in s or "ontario" in s or "quebec" in s),
        }
    if identity:
        eligibility["identity"] = {
            "value": identity, "confidence": 0.9,
            "src": "Listed by Electro-Federation Canada under "
                   + ", ".join(s for s in sources
                               if any(k in s for k in ("women", "indigenous", "colour"))),
        }
    return eligibility, affiliations


class ElectroFedAdapter(Adapter):
    source_id = "electrofed"

    def fetch_all(self):
        master = self.fetcher.get(MASTER_URL)
        roster = slugs_in(master)
        if not roster:
            raise RuntimeError(
                "[electrofed] the master scholarship list yielded no "
                "/efc-scholarship/<slug>/ links; the page layout has changed"
            )

        # Themed pages, purely to tag the roster. A page that has moved is a
        # warning rather than a failure: losing a tag costs a filter value,
        # while aborting would cost all 46 awards.
        categories = {}
        for slug in CATEGORY_PAGES:
            try:
                html = self.fetcher.get(category_url(slug))
            except Exception as e:
                print(f"[electrofed] category page {slug} unavailable ({e}); "
                      f"its awards will carry no tag from it")
                continue
            for award_slug in slugs_in(html):
                categories.setdefault(award_slug, set()).add(slug)

        emitted = 0
        for slug in roster:
            record = self._build_record(slug, categories.get(slug, set()))
            if self.fetch_detail:
                try:
                    self._enrich_from_detail(record)
                except Exception as e:
                    record.raw_fields["detail_error"] = str(e)
            yield record
            emitted += 1
            if self.limit and emitted >= self.limit:
                return

    def _build_record(self, slug, categories):
        eligibility, affiliations = eligibility_from_categories(categories)
        return AwardRecord(
            source_id=self.id,
            native_id=slug,
            # Provisional: the detail page carries the properly-cased title and
            # overwrites this. A slug is a readable fallback if that request
            # fails, rather than an empty name.
            award_name=slug.replace("-", " ").title(),
            source_url=f"{BASE}/efc-scholarship/{slug}/",
            # The corpus already has a value for this: UW files 1,153 awards
            # under "Awards/Scholarships/Prizes". A bare "Scholarship" would be
            # a synonym competing with it in the Program-style filters, so a
            # student picking one would silently miss the other 46 -- the same
            # class of split the AcademicWorks faculty-name pairs caused.
            # Adapters map into the existing vocabulary or leave the field
            # empty; they do not coin near-duplicates.
            award_type="Awards/Scholarships/Prizes",
            # Every EFC scholarship is applied for through one shared
            # application centre, so none of them are nomination-gated.
            application_type="open",
            affiliation=", ".join(affiliations) or None,
            eligibility=eligibility or None,
            raw_fields={"categories": sorted(categories)} if categories else {},
        )

    def _enrich_from_detail(self, record):
        html = self.fetcher.get(record.source_url)
        root = minidom.parse(html)
        main = root.find("main") or root
        lines = [ln.strip() for ln in (clean_text(main.get_text()) or "").split("\n") if ln.strip()]
        if not lines:
            record.raw_fields["detail_warning"] = "no text recovered from detail page"
            return

        # The first line is the properly-cased title.
        record.award_name = lines[0]

        # The page is a flat run of lines in which a label and its value are
        # separate entries:
        #
        #     Scholarship Value: $
        #     3,500
        #     Scholarships Available:
        #     2
        #
        # So a bare number means nothing on its own -- it has to be attributed to
        # whichever label came before it. Collecting every number and joining
        # them instead produced "$3,5002", the award value with the number of
        # awards stuck on the end.
        pending = None
        value_text = None
        available = None
        body = []

        for line in lines[1:]:
            low = line.lower()
            if low.startswith("scholarship value"):
                pending = "value"
                # "Scholarship Value: $3,500" all on one line also happens.
                tail = line.split(":", 1)[1].strip() if ":" in line else ""
                if _NUMBER.search(tail):
                    value_text, pending = tail, None
                continue
            if low.startswith("scholarships available"):
                pending = "available"
                tail = line.split(":", 1)[1].strip() if ":" in line else ""
                if _NUMBER.search(tail):
                    available, pending = tail, None
                continue
            if pending and _BARE_NUMBER.fullmatch(line):
                if pending == "value":
                    value_text = line
                else:
                    available = line
                pending = None
                continue
            pending = None
            if low.startswith("visit the efc scholarship application centre"):
                continue
            body.append(line)

        prose = clean_text("\n".join(body))
        if prose:
            record.award_description = prose
            # These pages carry one prose blob rather than a labelled
            # eligibility section, and it is where the criteria live.
            record.eligibility_selection_criteria = prose
        else:
            record.raw_fields["detail_warning"] = "no description recovered from detail page"

        if value_text:
            lo, hi, raw = parse_amounts(value_text if "$" in value_text else f"${value_text}")
            if lo is not None:
                record.amount_min, record.amount_max, record.amount_raw = lo, hi, raw
        if available:
            record.raw_fields["scholarships_available"] = available
