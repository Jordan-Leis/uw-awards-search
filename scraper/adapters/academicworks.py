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

Note the TLD. The .com hosts are a different, non-Canadian estate with a
wildcard catch-all that redirects *any* subdomain — including nonsense ones —
so probing there returns false positives. Also `western.academicworks.com` is
Western Colorado University, not Western Ontario.

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


class AcademicWorksAdapter(Adapter):
    # Multi-tenant: the id comes from the registry entry, not from here.
    source_id = None

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
                if self.fetch_detail:
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
                elif _CURRENCY_ONLY.match(text):
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

        return AwardRecord(
            source_id=self.id,
            native_id=row["native_id"],
            award_name=row["name"],
            award_description=row["blurb"],
            area_of_study=row["category"],
            amount_min=amount_min,
            amount_max=amount_max,
            amount_raw=amount_raw,
            source_url=row["url"],
            deadline_raw=deadline_raw,
            deadline_date=deadline_date,
            application_type="open",
            raw_fields={k: v for k, v in row.items() if v and k not in ("name", "url")},
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
            lo, hi, raw = parse_amounts(body)
            if lo is not None:
                record.amount_min, record.amount_max, record.amount_raw = lo, hi, raw
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
