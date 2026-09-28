"""
Adapter-level normalization: amounts, application status, career and faculty
names.

These all guard the same class of bug — a source's raw text leaking into a
structured field. Each one was found by dry-running the AcademicWorks adapter
against the live University of Alberta tenant before shipping it, and each one
would have been invisible in the UI while quietly costing megabytes or hiding
awards.

    python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scraper"))

from adapters.base import parse_amounts, RAW_VERBATIM_LIMIT  # noqa: E402
from adapters.academicworks import (  # noqa: E402
    canonical_category, infer_career, normalize_status,
)


class TestParseAmountsKeepsRawShort(unittest.TestCase):
    """amount_raw is "the verbatim amount string", not "the text we searched".

    parse_amounts returned clean_text(its whole input) as amount_raw, which is
    right for the field it was written against (Alberta's short "Award" cell,
    mean 57 chars) and badly wrong for the AcademicWorks detail body. Award
    21944's amount_raw was 3,407 characters -- a verbatim second copy of its
    description. Across one tenant that is ~6 MB of pure duplication shipped to
    every visitor, and an amount_raw that renders as an essay in the UI.
    """

    SHORT = "Up to $2,500 per year"
    PROSE = (
        "The Jason Lang Scholarship recognizes outstanding academic achievement "
        "of Alberta post-secondary students and encourages them to continue in "
        "their undergraduate programs of study.\n"
        "Jason Lang Scholarship: Value: $1,000\n"
        "Louise McKinney Scholarship: Value: $2,500. To be considered, applicants "
        "must apply by the Fall Application Deadline.\n"
        "Eligibility Candidates must be a Canadian Citizen or Permanent Resident."
    )

    def test_short_cell_is_kept_verbatim(self):
        """A dedicated amount cell carries meaning the figures alone lose --
        "per year" and "up to" both change what the number means."""
        lo, hi, raw = parse_amounts(self.SHORT)
        self.assertEqual((lo, hi), (2500, 2500))
        self.assertEqual(raw, self.SHORT)

    def test_long_prose_yields_only_the_figures(self):
        lo, hi, raw = parse_amounts(self.PROSE)
        self.assertEqual((lo, hi), (1000, 2500))
        self.assertLessEqual(len(raw), RAW_VERBATIM_LIMIT)
        self.assertIn("$1,000", raw)
        self.assertIn("$2,500", raw)
        self.assertNotIn("Canadian Citizen", raw)

    def test_long_prose_with_no_figure_has_no_amount_raw(self):
        """The worst case of the old behaviour: a description with no dollar
        figure anywhere still became the amount_raw for that award."""
        prose = "Based on academic standing and demonstrated community " * 8
        self.assertGreater(len(prose), RAW_VERBATIM_LIMIT)
        self.assertEqual(parse_amounts(prose), (None, None, None))

    def test_short_text_with_no_figure_is_still_kept(self):
        """"Varies" is a real answer to "how much?" and must survive."""
        self.assertEqual(parse_amounts("Varies"), (None, None, "Varies"))

    def test_empty(self):
        self.assertEqual(parse_amounts(None), (None, None, None))
        self.assertEqual(parse_amounts(""), (None, None, None))


class TestParseAmountsSpaceSeparators(unittest.TestCase):
    """Some sources use a SPACE as the thousands separator -- "$1 000", not
    "$1,000". Lethbridge writes 344 of its award values that way and NAIT writes
    "Up to $2 000".

    Read naively the regex stops at the space and reports $1, so a $2,000
    scholarship shows as being worth two dollars and the award-value range filter
    puts it below every threshold a student would set.
    """

    def test_space_thousands_separator(self):
        self.assertEqual(parse_amounts("$1 000 minimum")[:2], (1000, 1000))
        self.assertEqual(parse_amounts("Up to $2 000")[:2], (2000, 2000))

    def test_comma_separator_still_works(self):
        self.assertEqual(parse_amounts("$2,700.00")[:2], (2700, 2700))
        self.assertEqual(parse_amounts("$15,707.96")[:2], (15707, 15707))

    def test_plain_number(self):
        self.assertEqual(parse_amounts("$500")[:2], (500, 500))

    def test_two_figures_in_one_string(self):
        """The range must come from the two real figures, not their leading
        digits: this used to report (12, 25)."""
        self.assertEqual(
            parse_amounts("$25 000 ($12 500 in Fall; $12 500 in Winter)")[:2],
            (12500, 25000))

    def test_a_range_is_not_merged_across_the_word_between_it(self):
        self.assertEqual(parse_amounts("$500 to $1 000")[:2], (500, 1000))

    def test_millions_style_grouping(self):
        self.assertEqual(parse_amounts("$1 250 000")[:2], (1250000, 1250000))

    def test_zero_is_not_an_award_value(self):
        """"$0.00" and "$0.01" are placeholders these tenants use for "not
        stated". Recording them as a real value of zero makes an award look
        worthless rather than unspecified, and unstated must stay null so the
        range filter does not judge it."""
        for text in ("$0.00", "$0.01", "$0"):
            self.assertEqual(parse_amounts(text)[:2], (None, None), text)

    def test_zero_alongside_a_real_figure_keeps_the_real_one(self):
        self.assertEqual(parse_amounts("$0.00 up to $1 500")[:2], (1500, 1500))


class TestAmountSourcePrecedence(unittest.TestCase):
    """A dedicated amount cell outranks the description.

    AcademicWorks listings carry an "Award" cell holding the value, and the
    detail page carries prose that may mention an endowment, a tuition figure or
    a token "$1". Letting the prose overwrite the cell produced records whose two
    amount fields contradicted each other -- NAIT's Frances Camyre Memorial
    Bursary had amount_raw "$1,000.00" and amount_max 1.
    """

    def test_cell_value_survives_a_stray_figure_in_the_prose(self):
        from adapters.base import AwardRecord
        record = AwardRecord(source_id="nait", native_id="1", award_name="x")
        record.amount_min, record.amount_max, record.amount_raw = parse_amounts("$1,000.00")
        self.assertEqual((record.amount_max, record.amount_raw), (1000, "$1,000.00"))

        # what _enrich_from_detail now does
        body = "Donated in memory of a graduate. A token $1 is payable on award."
        if record.amount_max is None:
            record.amount_min, record.amount_max, record.amount_raw = parse_amounts(body)
        self.assertEqual(record.amount_max, 1000, "prose overwrote the dedicated cell")

    def test_prose_fills_in_when_the_cell_is_empty(self):
        from adapters.base import AwardRecord
        record = AwardRecord(source_id="nait", native_id="2", award_name="x")
        self.assertIsNone(record.amount_max)
        if record.amount_max is None:
            record.amount_min, record.amount_max, record.amount_raw = parse_amounts(
                "The award is valued at $2 500 per year.")
        self.assertEqual(record.amount_max, 2500)


class TestNormalizeStatus(unittest.TestCase):
    """2,164 of UofA's 2,428 awards are "Ended". The adapter parsed that into
    raw_fields and dropped it, so shipping the tenant would have put 2,164
    closed awards in front of a student looking for something to apply to."""

    def test_known_states(self):
        self.assertEqual(normalize_status("Open"), "Open")
        self.assertEqual(normalize_status("ENDED"), "Ended")
        self.assertEqual(normalize_status("closed"), "Ended")
        self.assertEqual(normalize_status("Upcoming"), "Upcoming")

    def test_absent_status_stays_unknown(self):
        """Most tenants render no status cell at all -- measured: zero "Open"
        across Manitoba, Trent, Winnipeg and Langara. Absence is unknown, and
        unknown must never be read as closed."""
        for value in (None, "", "   ", "Whatever"):
            self.assertIsNone(normalize_status(value))


class TestInferCareer(unittest.TestCase):
    """UofA mixes graduate awards into the same feed; 105 say so in the title
    alone. career is an existing facet, so this is a mapping job."""

    def test_graduate_titles(self):
        for name in ("Indigenous Student Graduate Award",
                     "Doctoral Recruitment Scholarship",
                     "PhD Excellence Award",
                     "Master's Entrance Scholarship"):
            self.assertEqual(infer_career(name, None), "Graduate", name)

    def test_undergraduate_titles(self):
        for name in ("Undergraduate Research Stipend",
                     "First Year Entrance Bursary"):
            self.assertEqual(infer_career(name, None), "Undergraduate", name)

    def test_ambiguous_stays_none(self):
        """A wrong Graduate tag HIDES an undergraduate award once the career
        filter is applied, so ambiguity must resolve to None, not to a guess."""
        for name in ("David Johnston Law Scholarship",
                     "Jason Lang Scholarships (And Louise McKinney)"):
            self.assertIsNone(infer_career(name, None), name)

    def test_body_is_consulted_when_the_title_is_silent(self):
        self.assertEqual(
            infer_career("Smith Memorial Award",
                         "Open to students enrolled in a graduate program."),
            "Graduate")

    def test_title_wins_over_body(self):
        """A body mentioning both is common boilerplate; the title is the
        stronger signal and must not be overridden by it."""
        self.assertEqual(
            infer_career("Undergraduate Travel Award",
                         "Graduate students should apply to the other program."),
            "Undergraduate")


class TestCanonicalCategory(unittest.TestCase):
    """UofA's category column yields 51 distinct values including four pairs
    that differ only in punctuation. Left alone each pair becomes two separate
    entries in the Program filter, so a student picking one silently misses the
    awards filed under the other."""

    CASES = [
        ("Faculty of Agricultural, Life & Environmental Sciences",
         "Faculty of Agricultural, Life and Environmental Sciences"),
        ("Faculty of Medicine & Dentistry", "Faculty of Medicine and Dentistry"),
        ("Faculty of Kinesiology, Sport and Recreation",
         "Faculty of Kinesiology, Sport, and Recreation"),
        ("Alberta School of Business", "Faculty of Business"),
    ]

    def test_collision_pairs_collapse(self):
        for a, b in self.CASES:
            self.assertEqual(canonical_category(a), canonical_category(b),
                             f"{a!r} vs {b!r}")

    def test_unrelated_faculties_stay_distinct(self):
        names = ["Faculty of Law", "Faculty of Science", "Faculty of Arts",
                 "Faculty of Engineering", "Augustana Campus"]
        canon = [canonical_category(n) for n in names]
        self.assertEqual(len(set(canon)), len(names))

    def test_currency_is_not_a_category(self):
        """The "Award" column sometimes holds a dollar figure. area_of_study is
        comma-split downstream, so "$2,700.00" became the two bogus programs
        "$2" and "700.00" in the Program filter."""
        for value in ("$2,700.00", "$500", "1,250.00"):
            self.assertIsNone(canonical_category(value), value)

    def test_an_award_value_is_never_a_program(self):
        """Lethbridge puts its award value in the column this adapter reads as a
        category -- 344 rows -- so the Program filter filled up with entries like
        "$1 000 minimum" and "$500 to $1 000: PSI entrance...". No real faculty
        name contains a dollar sign, which makes this an easy and total rule."""
        for value in ("$1 000 minimum", "Up to $2 000", "$500 minimum",
                      "$2 000 per semester to a maximum of $4 000 per academic year",
                      "$500 to $1 000: PSI entrance $500; PSII $500"):
            self.assertIsNone(canonical_category(value), value)

    def test_empty(self):
        self.assertIsNone(canonical_category(None))
        self.assertIsNone(canonical_category("  "))


class TestCategoryCommaSafety(unittest.TestCase):
    """area_of_study is comma-split downstream, so a facet value may not contain
    a comma of its own.

    Two UofA faculties are spelled with internal commas ("Faculty of
    Kinesiology, Sport and Recreation") and a handful of cells list several
    faculties at once. Split naively, one such cell yields the browsable
    "programs" `Sport` and `and Recreation` -- junk in the filter the student is
    supposed to trust.
    """

    def setUp(self):
        sys.path.insert(0, str(REPO / "scraper"))
        from export_data import split_multi_value
        self.split = split_multi_value

    def test_single_faculty_has_no_internal_comma(self):
        for raw in ("Faculty of Kinesiology, Sport and Recreation",
                    "Faculty of Agricultural, Life & Environmental Sciences"):
            self.assertNotIn(",", canonical_category(raw), raw)

    def test_multi_faculty_cell_splits_into_whole_faculties(self):
        raw = ("Faculty of Kinesiology, Sport, and Recreation, "
               "Faculty of Science, Alberta School of Business")
        parts = self.split(canonical_category(raw))
        self.assertEqual(len(parts), 3, parts)
        self.assertNotIn("Sport", parts)
        self.assertNotIn("and Recreation", parts)
        self.assertIn("Faculty of Science", parts)
        self.assertIn("Alberta School of Business", parts)

    def test_two_faculty_cell(self):
        parts = self.split(canonical_category(
            "Faculty of Science, Alberta School of Business"))
        self.assertEqual(sorted(parts),
                         ["Alberta School of Business", "Faculty of Science"])

    def test_a_plain_name_with_no_marker_survives(self):
        self.assertEqual(self.split(canonical_category("Business")), ["Business"])

    def test_canonicalization_is_order_independent(self):
        """The published facet value must not depend on which spelling the
        tenant happened to page first.

        An earlier version learned "first spelling wins" at run time, so the
        same data could export as "Medicine & Dentistry" one refresh and
        "Medicine and Dentistry" the next -- churning VOCAB_VERSION and
        invalidating saved filter selections for no reason.
        """
        for a, b in TestCanonicalCategory.CASES:
            forward = (canonical_category(a), canonical_category(b))
            backward = (canonical_category(b), canonical_category(a))
            self.assertEqual(forward, backward[::-1])
            self.assertEqual(len(set(forward)), 1, f"{a} vs {b} -> {forward}")

    def test_or_between_two_faculties_splits(self):
        """Real UofA values. Left joined, they put
        "Alberta School of Business or Faculty of Arts" in the Program filter as
        a single browsable option that matches neither faculty."""
        parts = self.split(canonical_category(
            "College of Natural & Applied Sciences or Alberta School of Business"))
        self.assertEqual(sorted(parts),
                         ["Alberta School of Business", "College of Natural & Applied Sciences"])

    def test_or_inside_one_faculty_name_does_not_split(self):
        """"or" only separates units when BOTH sides name one; otherwise it is
        ordinary prose and splitting would shred the name."""
        self.assertEqual(canonical_category("Faculty of Arts or Humanities"),
                         "Faculty of Arts or Humanities")

    def test_commas_and_or_together(self):
        parts = self.split(canonical_category(
            "Faculty of Education, Alberta School of Business or Faculty of Arts"))
        self.assertEqual(sorted(parts), ["Alberta School of Business",
                                         "Faculty of Arts", "Faculty of Education"])

    def test_a_non_faculty_name_with_a_comma_stays_one_value(self):
        """Not every comma is a list. These are real UofA cells."""
        for raw, expected in [
            ("Sustainability Council, Energy & Climate Action",
             "Sustainability Council Energy & Climate Action"),
            ("Office of the Vice President, Research",
             "Office of the Vice President Research"),
        ]:
            self.assertEqual(self.split(canonical_category(raw)), [expected], raw)

    def test_collision_pairs_still_collapse_after_comma_stripping(self):
        pairs = TestCanonicalCategory.CASES
        for a, b in pairs:
            self.assertEqual(canonical_category(a), canonical_category(b), f"{a} vs {b}")


class TestCleanTextSeparators(unittest.TestCase):
    """SFU's award database serves literal U+FFFD where its bullets used to be,
    separated by U+000B VERTICAL TAB. The server declares utf-8 and the bad
    bytes are already in its data, so this is not a decoding bug on our side.
    Mapping the vertical tab to a newline recovers the list structure -- which
    matters, because those bullets ARE the eligibility criteria."""

    def test_vertical_tab_becomes_a_newline(self):
        from adapters.base import clean_text
        self.assertEqual(clean_text("a\x0bb"), "a\nb")

    def test_replacement_character_is_dropped(self):
        from adapters.base import clean_text
        self.assertEqual(
            clean_text("who:\x0b\ufffd is enrolled\x0b\ufffd is in good standing"),
            "who:\nis enrolled\nis in good standing")

    def test_ordinary_text_is_unaffected(self):
        from adapters.base import clean_text
        self.assertEqual(clean_text("  hello   world  "), "hello world")


class TestWesternRowParsing(unittest.TestCase):
    """Western returns all 2,021 awards with full descriptions in ONE GET, so
    row parsing is the entire adapter -- there is no detail pass to recover from
    a mistake here."""

    NAME = "MacKay-Lassonde Award in Computer Engineering"
    BODY = ("Available to students enrolled in the computer or software engineering "
            "programs. Value: 1 at $2,000.")

    def row(self, *extra):
        from adapters.western import parse_row
        return parse_row([self.NAME, self.NAME + " " + self.BODY, *extra])

    def test_name_prefix_is_stripped_from_the_description(self):
        """Cell [1] repeats the name before the description. Left in place it
        would also double-weight every award's name in the Fuse index."""
        r = self.row()
        self.assertEqual(r["name"], self.NAME)
        self.assertEqual(r["description"], self.BODY)
        self.assertFalse(r["description"].startswith(self.NAME))

    def test_deadline_is_recognised_but_not_dated(self):
        r = self.row("SEPTEMBER 30", "")
        self.assertEqual(r["deadline_raw"], "SEPTEMBER 30")

    def test_essay_flag_is_captured(self):
        r = self.row("", "ESSAY REQUIRED")
        self.assertEqual(r["supporting_documents"], "ESSAY REQUIRED")
        self.assertIsNone(r["deadline_raw"])

    def test_cells_are_classified_by_content_not_position(self):
        """Same two values, swapped. Reading positionally would put the essay
        flag in deadline_raw."""
        r = self.row("ESSAY REQUIRED", "SEPTEMBER 30")
        self.assertEqual(r["deadline_raw"], "SEPTEMBER 30")
        self.assertEqual(r["supporting_documents"], "ESSAY REQUIRED")

    def test_a_row_with_no_name_is_not_an_award(self):
        from adapters.western import parse_row
        self.assertIsNone(parse_row([]))
        self.assertIsNone(parse_row(["", "something"]))


class TestWesternNativeIds(unittest.TestCase):
    """The page carries no id of any kind -- no row key, no per-award link -- and
    20 of its 2,021 names are duplicated, so id assignment is load-bearing."""

    def ids(self, rows):
        from adapters.western import assign_native_ids
        return [assign_native_ids(rows)[id(r)] for r in rows]

    @staticmethod
    def row(name, description):
        return {"name": name, "description": description,
                "deadline_raw": None, "supporting_documents": None}

    def test_unique_names_get_a_plain_slug(self):
        rows = [self.row("Alpha Award", "a"), self.row("Beta Award", "b")]
        self.assertEqual(self.ids(rows), ["alpha-award", "beta-award"])

    def test_duplicate_names_get_distinct_ids(self):
        rows = [self.row("A.M.F.G. Award in Nursing", "first"),
                self.row("A.M.F.G. Award in Nursing", "second")]
        got = self.ids(rows)
        self.assertEqual(len(set(got)), 2, got)
        self.assertTrue(all(g.startswith("a-m-f-g-award-in-nursing") for g in got), got)

    def test_ids_do_not_depend_on_page_order(self):
        """The load-bearing case. Ties break on the description, not position,
        because the page is regenerated and a reordering would otherwise
        repoint every permalink after the moved row at a different award."""
        a = self.row("Shared Name", "alpha description")
        b = self.row("Shared Name", "beta description")
        forward = self.ids([a, b])
        backward = self.ids([b, a])
        self.assertEqual(forward, backward[::-1],
                         "native_id changed when the rows were reordered")


class TestElectroFedCategories(unittest.TestCase):
    """EFC files the same scholarships under themed pages, so membership of
    "for-women" or "-for-western-canada" is a PUBLISHED fact rather than a
    reading of prose. That makes it the one place in the corpus where structured
    eligibility can be recorded without an LLM pass -- so it must carry the same
    provenance any extracted value would."""

    def setUp(self):
        from adapters.electrofed import eligibility_from_categories
        self.fn = eligibility_from_categories

    def test_no_categories_means_no_claims(self):
        """An untagged scholarship is open, not restricted. Inventing a
        constraint here would exclude students from a national award."""
        self.assertEqual(self.fn(set()), ({}, []))

    def test_identity_reuses_the_existing_vocabulary(self):
        el, aff = self.fn({"efc-scholarships-for-women"})
        self.assertEqual(aff, ["Women"])
        self.assertEqual(el["identity"]["value"], ["women"])

    def test_region_maps_to_provinces(self):
        el, aff = self.fn({"efc-scholarships-for-western-canada"})
        self.assertEqual(el["residency"]["value"], ["AB", "BC", "SK", "MB"])
        self.assertEqual(aff, [], "a region is not an affiliation")

    def test_atlantic_expands_to_four_provinces(self):
        el, _ = self.fn({"efc-scholarships-for-atlantic-canada"})
        self.assertEqual(sorted(el["residency"]["value"]), ["NB", "NL", "NS", "PE"])

    def test_several_regions_are_unioned_not_intersected(self):
        """A scholarship listed under both Ontario and Quebec is open to
        either. Intersecting would leave it open to nobody."""
        el, _ = self.fn({"efc-scholarships-for-ontario", "efc-scholarships-for-quebec"})
        self.assertEqual(sorted(el["residency"]["value"]), ["ON", "QC"])

    def test_identity_and_region_coexist(self):
        el, aff = self.fn({"efc-scholarships-for-women", "efc-scholarships-for-ontario"})
        self.assertEqual(el["residency"]["value"], ["ON"])
        self.assertEqual(el["identity"]["value"], ["women"])
        self.assertEqual(aff, ["Women"])

    def test_every_value_carries_a_checkable_src(self):
        """tools/eligibility.py rejects a value without `src`, and the point of
        the field is that a student can verify the match themselves."""
        el, _ = self.fn({"efc-scholarships-for-women", "efc-scholarships-for-western-canada"})
        for field, payload in el.items():
            self.assertIn("src", payload, field)
            self.assertIn("electrofed.com", payload["src"], field)
            self.assertLess(payload["confidence"], 1.0, field)

    def test_unknown_category_is_ignored_not_fatal(self):
        """A new themed page appearing upstream must not break the crawl."""
        el, aff = self.fn({"efc-scholarships-for-something-new"})
        self.assertEqual((el, aff), ({}, []))

    def test_result_is_order_independent(self):
        a = self.fn({"efc-scholarships-for-women", "efc-scholarships-for-ontario"})
        b = self.fn({"efc-scholarships-for-ontario", "efc-scholarships-for-women"})
        self.assertEqual(a, b)


class TestElectroFedSlugs(unittest.TestCase):
    def test_slugs_are_extracted_and_deduplicated(self):
        from adapters.electrofed import slugs_in
        html = ('<a href="/efc-scholarship/abb-scholarship/">ABB</a>'
                '<a href="/efc-scholarship/abb-scholarship/">ABB again</a>'
                '<a href="/efc-scholarship/nexans-canada-inc-scholarship/">Nexans</a>'
                '<a href="/about/efc-scholarship-program/">not an award</a>')
        self.assertEqual(slugs_in(html),
                         ["abb-scholarship", "nexans-canada-inc-scholarship"])

    def test_no_links_yields_nothing(self):
        from adapters.electrofed import slugs_in
        self.assertEqual(slugs_in("<p>nothing here</p>"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
