"""
The eligibility matcher's contract test.

Stdlib unittest, because tools/ is deliberately dependency-free:
    python3 -m unittest discover -s tests -v
    python3 tests/test_eligibility.py

Every case lives in tests/fixtures/match_cases.json rather than in this file,
because site/js/eligibility.js has to be held to exactly the same cases. When
the matcher is wrong, add the case to the fixture first.
"""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))

import eligibility as E  # noqa: E402

FIXTURE = json.loads((REPO / "tests/fixtures/match_cases.json").read_text(encoding="utf-8"))
PROFILES = {k: {pk: pv for pk, pv in v.items() if not pk.startswith("_")}
            for k, v in FIXTURE["profiles"].items()}


class TestMatchCases(unittest.TestCase):
    """One subTest per fixture case, so a failure names the case."""

    def test_fixture_cases(self):
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                profile = PROFILES[case["profile"]]
                result = E.match_award(case["award"], profile)
                expect = case["expect"]
                context = (
                    f"\ncase:  {case['name']}"
                    f"\nwhy:   {case.get('why', '(none given)')}"
                    f"\ngot:   {json.dumps(result, indent=2)}"
                )

                self.assertEqual(result["verdict"], expect["verdict"], context)

                for key in ("excluded_fields", "unknown_fields"):
                    if key in expect:
                        self.assertEqual(sorted(result[key]), sorted(expect[key]), context)

                if "reason_contains" in expect:
                    joined = " ".join(result["reasons"])
                    self.assertIn(expect["reason_contains"], joined, context)

    def test_every_case_has_a_rationale(self):
        """A case nobody can explain is a case nobody can safely change."""
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                self.assertTrue(
                    case.get("why") or case.get("grounded"),
                    f"{case['name']}: needs a 'why' or a 'grounded' reference",
                )


class TestExclusionIsConservative(unittest.TestCase):
    """The asymmetry the whole design rests on: a false EXCLUDED silently costs
    the student money, a false UNKNOWN only costs them a few seconds reading."""

    def test_confidence_below_threshold_downgrades_to_unknown(self):
        profile = PROFILES["jordan"]
        for confidence in (0.0, 0.3, E.EXCLUSION_CONFIDENCE_THRESHOLD - 0.01):
            with self.subTest(confidence=confidence):
                award = {"eligibility": {"residency": {
                    "value": ["ON"], "confidence": confidence, "src": "Ontario residents."}}}
                self.assertEqual(E.match_award(award, profile)["verdict"], E.UNKNOWN)

    def test_confidence_at_threshold_can_exclude(self):
        award = {"eligibility": {"residency": {
            "value": ["ON"], "confidence": E.EXCLUSION_CONFIDENCE_THRESHOLD,
            "src": "Ontario residents."}}}
        self.assertEqual(E.match_award(award, PROFILES["jordan"])["verdict"], E.EXCLUDED)

    def test_empty_profile_is_never_excluded_from_anything(self):
        """A student who has answered nothing should see 'needs checking',
        never 'not eligible' — we have no basis to rule them out."""
        for case in FIXTURE["cases"]:
            award = case["award"]
            with self.subTest(case=case["name"]):
                self.assertNotEqual(
                    E.match_award(award, {})["verdict"], E.EXCLUDED,
                    f"{case['name']}: excluded a student who declared nothing",
                )


class TestValidation(unittest.TestCase):
    def test_accepts_every_fixture_award(self):
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                problems = E.validate_eligibility(case["award"].get("eligibility"))
                self.assertEqual(problems, [], f"{case['name']}: {problems}")

    def test_rejects_unknown_field(self):
        problems = E.validate_eligibility(
            {"favourite_colour": {"value": ["blue"], "confidence": 1.0, "src": "x"}})
        self.assertTrue(any("unknown field" in p for p in problems), problems)

    def test_rejects_out_of_vocabulary_value(self):
        problems = E.validate_eligibility(
            {"residency": {"value": ["Alberta"], "confidence": 1.0, "src": "x"}})
        self.assertTrue(any("not in" in p for p in problems), problems)

    def test_rejects_missing_source_sentence(self):
        problems = E.validate_eligibility({"residency": {"value": ["AB"], "confidence": 1.0}})
        self.assertTrue(any("src" in p for p in problems), problems)

    def test_rejects_out_of_range_confidence(self):
        for bad in (-0.1, 1.5, "high", None):
            with self.subTest(confidence=bad):
                problems = E.validate_eligibility(
                    {"residency": {"value": ["AB"], "confidence": bad, "src": "x"}})
                self.assertTrue(any("confidence" in p for p in problems), problems)

    def test_rejects_nonsense_gpa(self):
        problems = E.validate_eligibility(
            {"gpa_min": {"value": 400, "confidence": 1.0, "src": "x"}})
        self.assertTrue(any("gpa_min" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)
