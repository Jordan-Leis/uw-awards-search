/**
 * Parity test: site/js/search.js must agree with tools/facets.py.
 *
 *   node tests/test_search_js.js
 *
 * The browser matcher and the Python matcher are separate implementations of
 * the same rules, and they have drifted before: search.js treated affiliation
 * as an OR facet while find_awards.py treated it as an eligibility gate, so
 * the website and the CLI disagreed about who qualified for 94 awards. Syntax
 * checks do not catch that, and they did not catch `filterControls` being
 * declared const and then reassigned either.
 *
 * Runs search.js in a bare VM with a stub Fuse, so no browser is needed.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const REPO = path.join(__dirname, "..");

// --- load search.js in a sandbox -----------------------------------------
const sandbox = {
  // search.js only uses Fuse for free-text search; structured filtering never
  // touches it, so a pass-through stub keeps this test dependency-free.
  Fuse: class {
    constructor(items) { this.items = items; }
    search() { return this.items.map((item) => ({ item })); }
  },
  Profile: { matchesAward: () => true },
  console,
};
vm.createContext(sandbox);
// `const AwardSearch = ...` at VM top level is a lexical binding and does not
// attach to the sandbox global, so the export is made explicit.
const source = fs.readFileSync(path.join(REPO, "site/js/search.js"), "utf8");
vm.runInContext(source + "\n;globalThis.AwardSearch = AwardSearch;", sandbox);
const AwardSearch = sandbox.AwardSearch;

const SPEC = {
  version: 1,
  groups: ["Study", "Award", "Eligibility"],
  facets: [
    { key: "career", filter_key: "career", label: "Career", kind: "scalar", semantics: "facet", group: "Study" },
    { key: "levels", filter_key: "level", label: "Year", kind: "list", semantics: "facet", group: "Study" },
    { key: "areas_of_study", filter_key: "areaOfStudy", label: "Program", kind: "list", semantics: "facet", group: "Study" },
    { key: "affiliations", filter_key: "affiliation", label: "Group", kind: "list", semantics: "gate", group: "Eligibility" },
    { key: "amount_max", filter_key: "amount", label: "Value", kind: "range", semantics: "facet", group: "Award" },
    { key: "deadline_date", filter_key: "hasDeadline", label: "Deadline", kind: "bool", semantics: "facet", group: "Award" },
  ],
};

const award = (over = {}) => Object.assign({
  award_id: "x", award_name: "Test", career: null, levels: [],
  areas_of_study: [], affiliations: [], amount_max: null, deadline_date: null,
}, over);

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; }
  catch (e) { failed++; console.error(`  FAIL ${name}\n        ${e.message}`); }
}

AwardSearch.init([], SPEC);
const facet = (key) => SPEC.facets.find((f) => f.filter_key === key);
const m = (a, key, sel) => AwardSearch.matchesFacet(a, facet(key), sel);

console.log("search.js filter semantics");

// --- "unstated is unconstrained" -----------------------------------------
test("untagged award passes a program filter", () =>
  assert.strictEqual(m(award(), "areaOfStudy", ["Electrical Engineering"]), true));
test("untagged award passes a level filter", () =>
  assert.strictEqual(m(award(), "level", ["UG Year 3"]), true));
test("award with no career passes a career filter", () =>
  assert.strictEqual(m(award({ career: null }), "career", ["Undergraduate"]), true));

// --- stated constraints still bind ---------------------------------------
test("wrong program is excluded", () =>
  assert.strictEqual(m(award({ areas_of_study: ["History"] }), "areaOfStudy", ["Electrical Engineering"]), false));
test("matching program passes", () =>
  assert.strictEqual(m(award({ areas_of_study: ["Electrical Engineering"] }), "areaOfStudy", ["Electrical Engineering"]), true));
test("wrong career is excluded", () =>
  assert.strictEqual(m(award({ career: "Graduate" }), "career", ["Undergraduate"]), false));

// --- affiliation is a GATE, not a facet ----------------------------------
test("untagged award is open to everyone", () =>
  assert.strictEqual(m(award({ affiliations: [] }), "affiliation", []), true));
// The gate only closes on an actual declaration: an empty selection means
// "not answered", not "none of these apply". Excluding on silence hid 169
// awards from any visitor who had not touched the filter.
test("no declaration does not exclude a restricted award", () =>
  assert.strictEqual(m(award({ affiliations: ["Women"] }), "affiliation", []), true));
test("tagged award admits a student who declares it", () =>
  assert.strictEqual(m(award({ affiliations: ["Women"] }), "affiliation", ["Women"]), true));
test("a declaration that does not match still excludes", () =>
  assert.strictEqual(m(award({ affiliations: ["Women"] }), "affiliation", ["Indigenous"]), false));

// --- range ---------------------------------------------------------------
// Deliberate exception to "unstated is unconstrained": award value is a
// preference, not an eligibility criterion, and letting nulls through made the
// filter match 2,795 of 4,016 awards.
test("a SET value range excludes awards with no stated amount", () =>
  assert.strictEqual(m(award({ amount_max: null }), "amount", { min: 5000, max: null }), false));
test("an UNSET value range constrains nothing", () => {
  assert.strictEqual(m(award({ amount_max: null }), "amount", { min: null, max: null }), true);
  assert.strictEqual(m(award({ amount_max: null }), "amount", null), true);
});
test("amount below the floor is excluded", () =>
  assert.strictEqual(m(award({ amount_max: 500 }), "amount", { min: 5000, max: null }), false));
test("amount inside the range passes", () =>
  assert.strictEqual(m(award({ amount_max: 6000 }), "amount", { min: 5000, max: 10000 }), true));
test("empty range is no constraint", () =>
  assert.strictEqual(m(award({ amount_max: 1 }), "amount", null), true));

// --- bool ----------------------------------------------------------------
test("bool 'any' matches everything", () =>
  assert.strictEqual(m(award({ deadline_date: null }), "hasDeadline", null), true));
test("bool true requires the field", () =>
  assert.strictEqual(m(award({ deadline_date: null }), "hasDeadline", true), false));
test("bool true matches when present", () =>
  assert.strictEqual(m(award({ deadline_date: "2027-01-15" }), "hasDeadline", true), true));

// --- facets with nothing behind them are hidden --------------------------
test("activeFacets hides facets the corpus has no values for", () => {
  AwardSearch.init([award({ career: "Undergraduate" })], SPEC);
  const keys = AwardSearch.activeFacets().map((f) => f.filter_key);
  assert.ok(keys.includes("career"), "career should be active");
  assert.ok(!keys.includes("affiliation"), "affiliation has no values and should be hidden");
  assert.ok(!keys.includes("amount"), "amount has no values and should be hidden");
});

// --- parity with the published corpus ------------------------------------
const awardsPath = path.join(REPO, "site/data/awards.json");
if (fs.existsSync(awardsPath)) {
  const awards = JSON.parse(fs.readFileSync(awardsPath, "utf8"));
  AwardSearch.init(awards, SPEC);
  test("real corpus: a program filter never hides an untagged award", () => {
    const untagged = awards.filter((a) => !(a.areas_of_study || []).length);
    for (const a of untagged) {
      assert.strictEqual(m(a, "areaOfStudy", ["Electrical Engineering"]), true);
    }
  });
  test("real corpus: no award is hidden from a visitor who declared nothing", () => {
    const tagged = awards.filter((a) => (a.affiliations || []).length);
    assert.ok(tagged.length > 0, "expected some affiliation-tagged awards");
    for (const a of tagged) assert.strictEqual(m(a, "affiliation", []), true);
  });
  test("real corpus: a mismatched declaration still gates", () => {
    const women = awards.filter((a) => (a.affiliations || []).includes("Women"));
    assert.ok(women.length > 0);
    for (const a of women) {
      if (!(a.affiliations || []).includes("Indigenous")) {
        assert.strictEqual(m(a, "affiliation", ["Indigenous"]), false);
      }
    }
  });
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
