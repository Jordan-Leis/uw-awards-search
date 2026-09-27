"""
Alberta Student Aid — https://studentaid.alberta.ca/scholarships/?c=all

Small (55 awards) but the highest-relevance source in the project for an
Alberta resident, and the cheapest to parse: one static index page plus one
static detail page each, no JavaScript, no pagination, no authentication.

Two things here are load-bearing and easy to get wrong:

1. Detail links do NOT all share a path prefix. Most sit under /scholarships/,
   but the Alexander Rutherford Scholarship — the single most valuable award in
   this source for a student studying outside Alberta — lives under
   /scholarships-and-awards/. A parser that matches on "/scholarships/" drops
   it silently.

2. The detail pages are where eligibility actually lives. The index card only
   carries a one-line "Requirements" summary; citizenship, Alberta residency
   and the year-of-study rules are only on the detail page, as are the
   deadlines. Indexing cards alone would produce records that look complete
   and are not.
"""
import re
from urllib.parse import urljoin, urlsplit

from . import minidom
from .base import Adapter, AwardRecord, clean_text, parse_amounts, slugify

INDEX_URL = "https://studentaid.alberta.ca/scholarships/?c=all"

# The card's italic "group" line -> UW's career vocabulary, which the facets,
# the frontend filters and the Worker's enum all speak.
CAREER_BY_GROUP = {
    "undergraduate students": "Undergraduate",
    "graduate": "Graduate",
    "graduate students": "Graduate",
    # High-school applicants are applying for post-secondary study, so they are
    # entering undergraduates rather than a separate career.
    "high school students": "Undergraduate",
}
LEVEL_BY_GROUP = {
    "high school students": "UG Entering Year 1",
}

# Section headings on a detail page, rendered as <p><strong>Heading</strong></p>.
# Both spellings occur: "Eligibility Criteria" on the /scholarships/ pages,
# bare "Eligibility" on the /scholarships-and-awards/ ones.
SECTION_ELIGIBILITY = ("eligibility criteria", "eligibility")
SECTION_VALUE = "value"
SECTION_APPLY = "how to apply"
SECTION_DEADLINE = "deadline"


# Page furniture that shares the content container's classes.
_CHROME_MARKERS = ("On this page:", "Need Help?", "Student Aid Journey",
                   "\u00a9 2026 Government of Alberta")


INDEX_HOST = urlsplit(INDEX_URL).netloc


def _is_offsite(url):
    return urlsplit(url).netloc.lower() != INDEX_HOST


def _is_chrome(text):
    head = text[:60]
    return any(marker in head for marker in _CHROME_MARKERS)


def _split_sections(container):
    """Detail-page content -> {lowercased heading: text}.

    The page has no semantic headings (no <h2>/<h3>); sections are marked only
    by a bold paragraph, so splitting on <strong> is the structure available.
    Text before the first heading is returned under "" as the description.
    """
    sections, current, buffer = {}, "", []

    def flush():
        if buffer:
            text = clean_text("\n".join(buffer))
            if text:
                sections[current] = (sections.get(current, "") + "\n" + text).strip()
        buffer.clear()

    for child in container.children:
        if isinstance(child, str):
            buffer.append(child)
            continue
        strong = child.find("strong") if child.tag == "p" else None
        # A heading is a <p> whose entire content is one <strong>.
        if strong and clean_text(child.get_text()) == clean_text(strong.get_text()):
            flush()
            # Headings are inconsistently punctuated across the site:
            # "Eligibility Criteria" on some pages, "Eligibility Criteria:" on
            # others. Normalising the trailing colon here avoids a second
            # near-duplicate entry in SECTION_ELIGIBILITY for every heading.
            current = (clean_text(strong.get_text()) or "").lower().strip().rstrip(":").strip()
        else:
            buffer.append(child.get_text())
    flush()
    return sections


class AlbertaStudentAidAdapter(Adapter):
    source_id = "alberta-student-aid"

    def fetch_all(self):
        index_html = self.fetcher.get(INDEX_URL)
        cards = minidom.parse(index_html).find_all("div", class_="scholarship")
        if not cards:
            raise RuntimeError(
                f"{INDEX_URL}: found no .scholarship cards. The page layout has "
                f"probably changed; fix the selector rather than publishing zero rows."
            )

        seen = set()
        for i, card in enumerate(cards):
            if self.limit and i >= self.limit:
                break
            record = self._parse_card(card)
            if record.native_id in seen:
                # Two cards genuinely share a title in this source (the two
                # Louise McKinney variants differ only by suffix), so a
                # collision means the slug was truncated, not that the page
                # repeated itself.
                record.native_id = f"{record.native_id}-{i}"
            seen.add(record.native_id)

            if record.source_url and _is_offsite(record.source_url):
                # A meaningful minority of cards link straight off the site —
                # to alberta.ca, and in one case to the Edmonton Community
                # Foundation. Those are separate sources with their own
                # layouts, so running this parser over them would produce
                # nothing useful. Recorded as external rather than reported as
                # a parse failure, because they are not broken, just elsewhere.
                record.raw_fields["external_detail"] = record.source_url
            elif record.source_url:
                try:
                    self._enrich_from_detail(record)
                except Exception as e:
                    # A failed detail fetch must not drop the award; the card
                    # data alone is still worth publishing, flagged as thin.
                    record.raw_fields["detail_error"] = str(e)
            yield record

    # -- index card ------------------------------------------------------
    def _parse_card(self, card):
        title_el = card.find(class_="scholarship-title")
        title = clean_text(title_el.get_text()) if title_el else None
        if not title:
            raise RuntimeError("scholarship card with no title")

        group_el = card.find(class_="scholarship-group")
        group = (clean_text(group_el.get_text()) or "").lower() if group_el else ""

        fields = {}
        for block in card.find_all("div", class_="p-1"):
            text = clean_text(block.get_text()) or ""
            # Blocks render as "Label:\nvalue" or "Label: value".
            match = re.match(r"^([A-Za-z ]{2,30}):\s*(.*)$", text, re.S)
            if match:
                fields[match.group(1).strip().lower()] = clean_text(match.group(2))

        link = card.find("a")
        href = link.get("href") if link else None
        source_url = urljoin(INDEX_URL, href) if href else None

        native_id = slugify(href.strip("/").split("/")[-1]) if href else slugify(title)

        amount_min, amount_max, amount_raw = parse_amounts(fields.get("value"))

        return AwardRecord(
            source_id=self.source_id,
            native_id=native_id,
            award_name=title,
            career=CAREER_BY_GROUP.get(group),
            level=LEVEL_BY_GROUP.get(group),
            award_value_description=fields.get("value"),
            eligibility_selection_criteria=fields.get("requirements"),
            source_url=source_url,
            amount_min=amount_min,
            amount_max=amount_max,
            amount_raw=amount_raw,
            application_type="open",
            raw_fields={"group": group, **fields},
        )

    # -- detail page -----------------------------------------------------
    def _enrich_from_detail(self, record):
        html = self.fetcher.get(record.source_url)
        root = minidom.parse(html)

        containers = root.find_all("div", class_={"col-md-8", "offset-md-2"})
        if not containers:
            record.raw_fields["detail_warning"] = "no content container found"
            return

        # Content is spread across SEVERAL containers on the longer pages, and
        # is NOT always in the biggest one. The Alexander Rutherford page keeps
        # its Eligibility section in a 3k container while a 7k FAQ container
        # sits right beside it, so taking the longest silently returned the FAQ
        # and left the award with only its one-line index summary. Every
        # content container is parsed and their sections merged.
        sections = {}
        for container in containers:
            text = container.get_text()
            if len(text) < 40 or _is_chrome(text):
                continue
            for heading, body_text in _split_sections(container).items():
                if body_text:
                    sections[heading] = (sections.get(heading, "") + "\n" + body_text).strip()

        record.raw_fields["detail_sections"] = sorted(sections)

        description = sections.get("")
        if description:
            record.award_description = description

        eligibility = next((sections[k] for k in SECTION_ELIGIBILITY if sections.get(k)), None)
        if eligibility:
            # Detail-page eligibility is strictly richer than the card's
            # one-line summary, so it replaces rather than appends.
            record.eligibility_selection_criteria = eligibility

        value = sections.get(SECTION_VALUE)
        if value:
            record.award_value_description = value
            lo, hi, raw = parse_amounts(value)
            if lo is not None:
                record.amount_min, record.amount_max, record.amount_raw = lo, hi, raw

        apply_text = sections.get(SECTION_APPLY)
        if apply_text:
            record.application_details = apply_text

        deadline = sections.get(SECTION_DEADLINE)
        if deadline:
            # Stored verbatim only. These read like "Apply for this scholarship
            # between August 1 and January 15" — a window with no year, so
            # deriving an ISO date would mean inventing one.
            record.deadline_raw = deadline

        if not record.eligibility_selection_criteria or not eligibility:
            # Loudly, not silently: falling back to the index card's one-line
            # summary means this award's real criteria were never captured.
            record.raw_fields["detail_warning"] = (
                f"no eligibility section found; sections were {sorted(sections)}")
