"""
Filter semantics for tools/find_awards.py.

The rule under test: an award that does not state a facet is UNCONSTRAINED by
it and must pass the filter for it.

This was not a problem while UW was the only source, because UW tags every
award with a program, a level and a type. Alberta Student Aid publishes no such
taxonomy, so all 55 of its awards have empty areas_of_study, terms and
award_types — and the original "empty means no match" behaviour meant that
applying any program filter silently hid the entire source. Adding data made
the tool worse, which is the opposite of the point.

    python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import find_awards as F  # noqa: E402

NO_FILTERS = {"career": [], "level": [], "term": [], "type": [],
              "areas": set(), "affiliation": [], "application_status": [],
              "min_value": None}


def filters(**overrides):
    return {**NO_FILTERS, **overrides}


def award(**fields):
    base = {"award_id": "x", "award_name": "Test Award", "career": None,
            "levels": [], "terms": [], "award_types": [], "affiliations": [],
            "areas_of_study": [], "application_status": None}
    return {**base, **fields}


class TestUntaggedAwardsSurvive(unittest.TestCase):
    """A source that publishes no taxonomy must not vanish under a filter."""

    def test_untagged_award_passes_area_filter(self):
        a = award(areas_of_study=[])
        self.assertTrue(F.passes_filters(a, filters(areas={"Electrical Engineering"})))

    def test_untagged_award_passes_term_filter(self):
        self.assertTrue(F.passes_filters(award(terms=[]), filters(term=["Fall"])))

    def test_untagged_award_passes_level_filter(self):
        self.assertTrue(F.passes_filters(award(levels=[]), filters(level=["UG Year 3"])))

    def test_untagged_award_passes_type_filter(self):
        self.assertTrue(F.passes_filters(award(award_types=[]), filters(type=["Bursary"])))

    def test_award_with_no_career_passes_career_filter(self):
        self.assertTrue(F.passes_filters(award(career=None), filters(career=["Undergraduate"])))


class TestTaggedAwardsStillFilter(unittest.TestCase):
    """Relaxing empty must not relax non-empty: a stated constraint still binds."""

    def test_wrong_area_still_excluded(self):
        a = award(areas_of_study=["History"])
        self.assertFalse(F.passes_filters(a, filters(areas={"Electrical Engineering"})))

    def test_matching_area_passes(self):
        a = award(areas_of_study=["Electrical Engineering"])
        self.assertTrue(F.passes_filters(a, filters(areas={"Electrical Engineering"})))

    def test_wrong_term_still_excluded(self):
        self.assertFalse(F.passes_filters(award(terms=["Winter"]), filters(term=["Fall"])))

    def test_wrong_career_still_excluded(self):
        a = award(career="Graduate")
        self.assertFalse(F.passes_filters(a, filters(career=["Undergraduate"])))


class TestAffiliationGate(unittest.TestCase):
    """Affiliation is a gate, not a plain facet: an award tagged e.g. 'Women'
    is RESTRICTED to that group rather than merely being about it.

    But the gate only closes on an actual declaration. An empty selection means
    the student has not answered, which is not the same as answering "none of
    these apply to me" — and excluding on silence is the false-exclusion
    failure mode the three-state eligibility design exists to prevent. It also
    made the website silently hide 169 awards from any visitor who had not yet
    touched the filter.
    """

    def test_untagged_award_is_open_to_everyone(self):
        self.assertTrue(F.passes_filters(award(affiliations=[]), filters(affiliation=[])))

    def test_no_declaration_does_not_exclude_a_restricted_award(self):
        a = award(affiliations=["Women"])
        self.assertTrue(F.passes_filters(a, filters(affiliation=[])))

    def test_tagged_award_admits_a_student_who_declares_it(self):
        a = award(affiliations=["Women"])
        self.assertTrue(F.passes_filters(a, filters(affiliation=["Women"])))

    def test_a_declaration_that_does_not_match_still_excludes(self):
        """The gate does close — once the student has actually answered."""
        a = award(affiliations=["Women"])
        self.assertFalse(F.passes_filters(a, filters(affiliation=["Indigenous"])))


class TestRangeExcludesUnstated(unittest.TestCase):
    """The one deliberate exception to "unstated is unconstrained".

    Award value is a preference, not an eligibility criterion. Letting awards
    with no stated figure through made "at least $10,000" match 2,795 of 4,016
    awards — a filter that does nothing.
    """

    def setUp(self):
        import facets
        self.facets = facets
        self.facet = facets.BY_FILTER_KEY["amount"]

    def test_unset_range_is_no_constraint(self):
        a = {"amount_max": None}
        self.assertTrue(self.facets.matches(a, self.facet, {"min": None, "max": None}))
        self.assertTrue(self.facets.matches(a, self.facet, None))

    def test_set_range_excludes_unstated(self):
        a = {"amount_max": None}
        self.assertFalse(self.facets.matches(a, self.facet, {"min": 10000, "max": None}))

    def test_set_range_keeps_matching_values(self):
        self.assertTrue(self.facets.matches({"amount_max": 20000}, self.facet, {"min": 10000, "max": None}))
        self.assertFalse(self.facets.matches({"amount_max": 500}, self.facet, {"min": 10000, "max": None}))


class TestTagSemantics(unittest.TestCase):
    """Affiliation is browsed, not gated: picking "Women" shows the 94 women's
    awards, not the 3,941 awards a woman is eligible for. Eligibility is what
    Match-my-profile and tools/eligibility.py are for."""

    def setUp(self):
        import facets
        self.facets = facets
        self.facet = facets.BY_FILTER_KEY["affiliation"]

    def test_no_selection_shows_everything(self):
        for affs in ([], ["Women"]):
            self.assertTrue(self.facets.matches({"affiliations": affs}, self.facet, []))

    def test_selection_shows_only_tagged_awards(self):
        self.assertTrue(self.facets.matches({"affiliations": ["Women"]}, self.facet, ["Women"]))
        self.assertFalse(self.facets.matches({"affiliations": []}, self.facet, ["Women"]))
        self.assertFalse(self.facets.matches({"affiliations": ["Indigenous"]}, self.facet, ["Women"]))


class TestBrowseVersusProfileMode(unittest.TestCase):
    """Affiliation means different things to the filter bar and to a profile.

    Filter bar  "show me awards restricted to Women"  -> the 94 tagged ones.
    Profile     "I am a woman, what can I win?"       -> those plus every
                                                        unrestricted award.

    Treating the profile case as a browse narrowed find_awards from 201 awards
    to 2, because tools/profile.json declares affiliations.
    """

    def setUp(self):
        import facets
        self.facets = facets
        self.facet = facets.BY_FILTER_KEY["affiliation"]

    def test_browse_mode_shows_only_tagged(self):
        m, f = self.facets.matches, self.facet
        self.assertTrue(m({"affiliations": ["Women"]}, f, ["Women"], mode=self.facets.BROWSE))
        self.assertFalse(m({"affiliations": []}, f, ["Women"], mode=self.facets.BROWSE))

    def test_profile_mode_also_keeps_unrestricted_awards(self):
        m, f = self.facets.matches, self.facet
        self.assertTrue(m({"affiliations": ["Women"]}, f, ["Women"], mode=self.facets.PROFILE))
        self.assertTrue(m({"affiliations": []}, f, ["Women"], mode=self.facets.PROFILE))

    def test_profile_mode_still_excludes_a_mismatched_restriction(self):
        m, f = self.facets.matches, self.facet
        self.assertFalse(
            m({"affiliations": ["Indigenous"]}, f, ["Women"], mode=self.facets.PROFILE))

    def test_browse_is_the_default(self):
        self.assertFalse(self.facets.matches({"affiliations": []}, self.facet, ["Women"]))

    def test_find_awards_uses_profile_mode(self):
        """Regression guard for the 201 -> 2 collapse."""
        probe = filters(affiliation=["Women"])
        self.assertTrue(F.passes_filters(award(affiliations=[]), probe))


class TestApplicationStatusDefault(unittest.TestCase):
    """Ended awards are hidden by default without excluding anyone on silence.

    2,164 of UofA's 2,428 awards are "Ended", so shipping them unfiltered puts
    mostly-expired results in front of a student looking for something to apply
    to. But only UofA and a few colleges publish a status at all -- measured
    across the live estate, Manitoba, Trent, Winnipeg and Langara return none --
    so the default has to hide "Ended" while keeping every unstated award.

    That is exactly what a scalar "facet" already does, which is why this needs
    a default in the UI rather than new matcher semantics. The test is here to
    stop anyone "fixing" it into a gate.
    """

    def setUp(self):
        import facets
        self.facets = facets
        self.facet = facets.BY_FILTER_KEY["applicationStatus"]

    def test_default_is_open(self):
        self.assertEqual(self.facet.get("default"), ["Open"])

    def test_unstated_status_survives_the_default(self):
        """The load-bearing case. If this ever fails, several thousand awards
        from sources that publish no status vanish from the site."""
        award = {"application_status": None}
        self.assertTrue(self.facets.matches(award, self.facet, ["Open"]))
        self.assertTrue(
            self.facets.matches(award, self.facet, ["Open"], mode=self.facets.PROFILE))

    def test_default_hides_ended_and_keeps_open(self):
        self.assertTrue(self.facets.matches({"application_status": "Open"}, self.facet, ["Open"]))
        self.assertFalse(self.facets.matches({"application_status": "Ended"}, self.facet, ["Open"]))

    def test_clearing_the_filter_shows_ended_again(self):
        """Annual awards reopen, so "Ended" must be reachable, not deleted."""
        for status in (None, "Open", "Ended", "Upcoming"):
            self.assertTrue(self.facets.matches({"application_status": status}, self.facet, []))

    def test_it_is_a_facet_not_a_gate(self):
        self.assertEqual(self.facet["semantics"], "facet")

    def test_the_cli_applies_the_same_default_as_the_website(self):
        """The site pre-selects "Open"; find_awards has to do the same.

        Both answer "what can this student win?", so a closed award hidden in
        one and shown in the other is the drift CLAUDE.md warns about -- it
        already happened once with affiliation, where the site and the CLI
        disagreed about 94 awards.
        """
        self.assertEqual(F.default_application_status(), ["Open"])
        self.assertEqual(F.default_application_status(include_closed=True), [])

    def test_the_cli_default_hides_ended_but_not_unstated(self):
        probe = filters(application_status=F.default_application_status())
        self.assertTrue(F.passes_filters(award(application_status=None), probe))
        self.assertTrue(F.passes_filters(award(application_status="Open"), probe))
        self.assertFalse(F.passes_filters(award(application_status="Ended"), probe))

    def test_include_closed_shows_everything(self):
        probe = filters(application_status=F.default_application_status(include_closed=True))
        for status in (None, "Open", "Ended", "Upcoming"):
            self.assertTrue(F.passes_filters(award(application_status=status), probe))


class TestRealCorpusIsNotShrunk(unittest.TestCase):
    """Guard the actual published data: whatever else changes, relaxing empty
    facets must never HIDE an award that was previously visible."""

    @classmethod
    def setUpClass(cls):
        import json
        cls.awards = json.loads((REPO / "site/data/awards.json").read_text(encoding="utf-8"))

    @staticmethod
    def _strict_passes(a, f):
        """The original semantics, kept here as the comparison baseline."""
        def any_of(values, selected):
            return True if not selected else bool(set(values or []) & set(selected))
        if f["career"] and a.get("career") not in f["career"]:
            return False
        if not any_of(a.get("levels"), f["level"]):
            return False
        if not any_of(a.get("terms"), f["term"]):
            return False
        if not any_of(a.get("award_types"), f["type"]):
            return False
        if f["areas"] and not (set(a.get("areas_of_study") or []) & f["areas"]):
            return False
        affs = set(a.get("affiliations") or [])
        return not (affs and not (affs & set(f["affiliation"] or [])))

    def test_no_award_is_lost(self):
        probe = filters(career=["Undergraduate"], term=["Fall"], level=["UG Year 3"],
                        areas={"Electrical Engineering", "Engineering Faculty - All Programs",
                               "All Programs"})
        strict = {a["award_id"] for a in self.awards if self._strict_passes(a, probe)}
        relaxed = {a["award_id"] for a in self.awards if F.passes_filters(a, probe)}
        lost = strict - relaxed
        self.assertEqual(lost, set(), f"{len(lost)} award(s) became invisible")
        self.assertGreaterEqual(len(relaxed), len(strict))


if __name__ == "__main__":
    unittest.main(verbosity=2)
