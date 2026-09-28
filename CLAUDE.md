# Notes for AI assistants working in this repo

## Finding awards for the user

Use the query tool. **Never read `site/data/awards.json` directly — it is
2.6 MB (~1533 records) and will blow up your context.**

```
python tools/find_awards.py --facets          # valid values for every filter
python tools/find_awards.py "robotics"        # ranked shortlist (~150 chars/row)
python tools/find_awards.py --detail <id>...  # full text, only for finalists
python tools/find_awards.py --stats           # filter funnel, no listing
```

Typical flow: `--facets` if unsure of a value → run a filtered/keyword query →
read the compact shortlist → `--detail` on the handful worth recommending.

`tools/README.md` has the full reference.

## Things that are easy to get wrong

- **Faculty-wide expansion matters.** Awards are tagged with a specific
  program, `<Faculty> Faculty - All Programs`, or `All Programs`. A student
  matches all three. The tool expands by default; `--strict-area` disables it.
  Filtering literally hides most of the eligible pool (161 vs 443 awards for a
  Software Engineering student).
- **Citizenship and GPA are not in the data** and must never be used as
  filters. The tool flags awards whose text mentions them (`!cit`, `!gpa`) so
  they can be checked manually.
- **An award that does not state a facet is unconstrained by it.** Empty
  `areas_of_study` / `terms` / `award_types` must PASS a filter, not fail it.
  UW tags everything, so this was invisible until Alberta Student Aid — which
  publishes no taxonomy at all — arrived and any program filter silently hid
  all 55 of its awards. Guarded by `tests/test_filters.py`.
- **Affiliation is an eligibility gate, but it only closes on a declaration.**
  An award tagged e.g. `Women` is restricted to that group; untagged awards are
  open to everyone. An EMPTY selection means "not answered", not "none of these
  apply", so it hides nothing — excluding on silence is the false-exclusion
  failure mode, and it was hiding 169 awards from any visitor who had not
  touched the filter. A non-matching declaration does still exclude.
- **All filtering goes through `tools/facets.py`.** `find_awards.passes_filters`
  and `site/js/search.js` both delegate to it. They have drifted apart twice;
  do not add a filter rule to only one of them.

## Multi-source — things that are easy to get wrong

The database is being generalized from "the UWaterloo directory" to "Canadian
undergraduate awards, many sources". Schema v2 landed; adapters have not.

- **Awards are keyed on `(source_id, native_id)`, not `award_id`.** The public
  identifier is `award_uid` = `"<source_id>:<native_id>"`, e.g. `uw:202601114`.
  `award_id` is still exported, unchanged, holding the source's own id — it is
  only unique *within* a source, so never use it as a global key. Permalinks
  and `find_awards.py --detail` accept both forms and report bare ids that turn
  out to be ambiguous.
- **`PRAGMA table_info` does not list generated columns.** `award_uid` is
  `GENERATED ALWAYS AS (...) STORED`, so it is invisible there and a v2 table
  can look like a v1 one. Use `PRAGMA table_xinfo`.
- **Every source needs a `sources/*.yaml` entry before its data can publish.**
  `export_data.py` hard-fails on a `source_id` in the database with no registry
  entry, on an active source below its own `min_awards` floor, and on a blocked
  source that somehow produced rows. The old global `MIN_TOTAL_ROWS` is gone:
  with several sources a corpus-wide floor hides a totally broken adapter.
- **Sources we decided not to crawl stay in the registry**, under
  `sources/blocked/` with a `blocked_reason`. Deleting them loses the research
  and invites someone to re-add them. `robots.txt` is respected strictly —
  AwardSpring (~2,100 awards) and ScholarTree are excluded on that basis, and
  ScholarTree's ToS forbids extraction independently of robots.
- **`sources/*.yaml` is build-time only.** `export_data.py` compiles it to
  `site/data/sources.json`, which is what the frontend and `find_awards.py`
  read. Do not add a YAML parser to `tools/` or `site/js/` — they are
  deliberately dependency-free.
- **`awards.json` is NOT the whole corpus any more.** A source declares
  `delivery: core` (ships in `awards.json`, fetched on every page load) or
  `delivery: shard` (ships in `data/sources/<id>.json`, fetched only when a
  visitor asks). The default is `shard`, so a source that forgets the field
  stays out of the always-loaded payload — the failure mode of forgetting is a
  source nobody sees until they ask, not a 25 MB first paint. `awards.json`
  costs ~2.4 KB/award and the AcademicWorks estate is ~10,000 awards, so this
  is not premature: the whole estate in one blob is >25 MB, committed to this
  repo *and* mirrored into `Personal_Website` on every refresh.
  `awards.slim.json` stays complete, and `tools/find_awards.py` merges every
  shard (`--core-only` reproduces what the browser loads first).
- **`application_status` hides closed awards without excluding on silence.**
  2,164 of UofA's 2,428 awards are `Ended`. The facet is a plain scalar
  `facet`, so selecting `Open` still keeps every award that states *no* status
  — which is most of the corpus, because only UofA and a few colleges publish
  one. The UI pre-selects `Open` via the facet's `default`; that is a UI
  default and not matcher semantics, and turning it into a gate would hide
  Manitoba's entire 3,145-award catalogue. Guarded on both sides by
  `tests/test_filters.py` and `tests/test_search_js.js`.
- **The adapter skips detail pages for closed awards.** A detail page is one
  request, and most awards in the estate are closed, so this is what makes a
  full UofA crawl ~264 requests instead of ~2,400. Unknown status is always
  crawled, and a reopened award gets its full text at the next refresh.
  `--detail-all` overrides it.
- **DNS cannot discover AcademicWorks tenants.** *Both* TLDs answer for every
  subdomain — a sweep of 100 `*.academicworks.ca` slugs resolved all 100,
  including a nonsense control. Use `tools/probe_academicworks.py`: a real
  tenant keeps its own host, anything else redirects to `www.blackbaud.com`.
  The probe keeps a nonsense slug in its default list so the discriminator is
  self-testing, and it found six tenants (four in Alberta, ~2,592 awards) that
  hand research had missed.
- **`amount_raw` is the amount string, not the text it was found in.** Both
  `parse_amounts()` and `export_data.derive_amounts()` used to store their
  whole input, so one UofA award carried 3,407 characters of description in
  `amount_raw`. Short input is kept verbatim (`"up to"` and `"per year"` change
  what a number means); longer input contributes only the figures it matched.
- **Adding a facet to `index.json` is safe**; `tools/gen_vocab.py` iterates its
  own `FACET_TO_FILTER_KEY` and ignores the rest. The new `source_id` facet did
  not change `VOCAB_VERSION`, so it needs no Worker redeploy.

## Adapters — adding a source

`python3 scraper/run_adapter.py <source-id> [--limit N] [--dry-run]`

- **One file in `scraper/adapters/` plus one `sources/*.yaml` entry.** If adding
  a source means editing `export_data.py` or the frontend, the abstraction is
  wrong. Adapters emit `AwardRecord` (`adapters/base.py`); nothing downstream
  knows which source a record came from except through `source_id`.
- **robots.txt is enforced in code**, not just documented. Every fetch goes
  through `adapters/http.PoliteFetcher.can_fetch()`, and a disallowed URL
  raises `RobotsDisallowed`. An **unreadable** robots.txt (403) is treated as
  *disallowed* — "I could not read the rules" is not "there are no rules".
  `run_adapter.py` also refuses by id for any source marked `blocked`.
- **Static sources use stdlib only** — `adapters/minidom.py`, not BeautifulSoup.
  bs4 + lxml + Playwright exist for UW because PeopleSoft will not render
  without a browser; none of that applies to a plain HTML table, and
  stdlib-only adapters can be tested anywhere.
- **Map into the existing facet vocabulary; never coin a synonym.** The EFC
  adapter first set `award_type: "Scholarship"`, which competes with the
  `Awards/Scholarships/Prizes` value UW uses for 1,153 awards — a student
  picking either would silently miss the other. Same class of split as the
  AcademicWorks faculty-name pairs. Check `site/data/index.json` `core_facets`
  before introducing a value. Adding one is fine when it is genuinely distinct:
  EFC's `People of colour` stands because the nearest existing value, `Black`,
  is narrower than what the source says.
- **Do not tag a facet the source does not state.** Western's 2,021 awards keep
  `career` NULL. The page declares no scope ("WESTERN AWARDS"), so tagging them
  all `Undergraduate` invents a fact — and inferring from prose is worse: 245
  descriptions contain a graduate-level word but nearly all are donor biography
  ("a Western graduate in the Faculty of Law", "PhD'74") or an undergraduate
  award funding *future* graduate study. Only 2 are actually graduate, so
  inference would hide ~20 awards from the students they are for. Unstated is
  unconstrained, so NULL still shows up for an undergraduate search.
- **A source with no id needs a content-derived one, not a positional one.**
  Western's page has no row key, no per-award link and no query parameter, and
  20 of its names are duplicated. `native_id` is `slugify(name)` with ties
  broken on the **description**, never on row order: the page is regenerated,
  and ordering by position would repoint every permalink after a moved row at a
  different award. `tests/test_adapters.py` reverses the input and asserts the
  ids do not move.
- **One GET can be the whole source.** Western returns all 2,021 awards with
  full descriptions inline — median 815 characters, none empty — so it has no
  detail pass at all. Check for this before writing a per-award crawl: SFU
  (866 rows from an empty POST) and ScholarAB (1,542 from one static page) have
  the same shape.
- **Never assume a URL path prefix.** Alberta's Alexander Rutherford award —
  the most valuable one in that source — sits under `/scholarships-and-awards/`
  while everything else is under `/scholarships/`.
- **Never assume one content container, or that the biggest one is the right
  one.** The Rutherford page keeps its eligibility in a 3 KB container beside a
  7 KB FAQ; taking the longest silently returned the FAQ.
- **Fail loudly, per award.** A detail page that yields no eligibility sets
  `raw_fields["detail_warning"]` rather than quietly falling back to the index
  card's one-line summary. Off-site detail links get `external_detail` instead,
  because they are a different source, not a parse failure.

## Eligibility matching

- **Three states, not two.** `eligible` / `unknown` / `excluded`. Only 7.2% of
  awards mention citizenship and 4.2% mention residency, so silence is the norm
  and must not be read as either a yes or a no. An award with no extraction yet
  is `unknown`, never `eligible`.
- **Exclusion is the expensive direction to be wrong.** A false `excluded`
  silently costs the student money they never learn they were owed; a false
  `unknown` costs them a few seconds. Excluding requires confidence >= 0.75
  (`EXCLUSION_CONFIDENCE_THRESHOLD`); anything softer degrades to `unknown`.
- **Exclusions are first-class fields.** `program_excluded` /
  `institution_excluded` exist because text like "Year Two or Three in the
  Faculty of Math (excluding CFM and Software Engineering)" is common, and an
  extractor that reads only the inclusion marks an SE student eligible.
- **`identity: []` means "not answered"; `identity: ["none"]` means "none
  apply".** Only the explicit sentinel can exclude. Treating `[]` as a
  declaration would mean any profile builder that initialises list fields to
  `[]` silently excludes the student from everything.
- **Every extracted value carries `src`**, the verbatim sentence it came from.
  It is shown in the UI, and it is what makes a wrong match auditable instead
  of invisible. `validate_eligibility()` rejects a value without one.
- `tools/eligibility.py` and `site/js/eligibility.js` are both held to
  `tests/fixtures/match_cases.json`. Add a case there before fixing a matcher
  bug. The v1 code already drifted this way once — `search.js` treats
  affiliation as an OR facet while `find_awards.py` treats it as a gate.
- `tests/golden/eligibility.json` is the extractor's ship gate (precision
  >= 0.95 on `excluded`, recall >= 0.85 on `eligible`). Re-running
  `tools/make_golden_set.py` never destroys existing labels.

Run the tests with `python3 -m unittest discover -s tests` — stdlib only, no
pytest, matching the rest of `tools/`.

## Privacy

`tools/profile.json` holds the user's citizenship, financial need and grade
band. It is gitignored. **Never print its contents, paste it into a message,
or commit it.** `tools/profile.example.json` is the safe, committed template.

## Repo layout

- `sources/` — the source registry: one YAML file per source, including
  sources deliberately **not** ingested (`sources/blocked/`). See below.
- `scraper/` — Playwright scraper for the UWaterloo awards directory
  (`scrape_index.py` → `scrape_details.py` → `export_data.py`). `awards.db` is
  gitignored; CI always scrapes fresh.
- `site/` — the static frontend published to GitHub Pages and mirrored to
  jordanleis.com/awards-database.
- `.github/workflows/refresh-data.yml` — scheduled scrape (Jan/May/Sep 1).
  `deploy-site.yml` — deploy without re-scraping.
- `ai-search/` — Cloudflare Worker that turns a natural-language question
  into filter values via Gemini's free tier. `ai-search/README.md` has the
  deploy runbook and the abuse model.

`site/js/*` may fetch exactly five things: **`data/awards.json`** and
**`data/meta.json`** (required — the page cannot render without them),
**`data/filters.json`**, **`data/sources.json`** and
**`data/sources/<id>.json`** (all optional), plus the AI Worker's
`/interpret` endpoint (also optional). Every optional fetch must be
individually `try`-guarded and must degrade to something usable, never to a
blank page:

| Missing | Result |
|---|---|
| `filters.json` | falls back to `FALLBACK_FILTER_SPEC`, six built-in filters |
| `sources.json` | no "More sources" row; core corpus fully searchable |
| a source shard | that one chip shows as failed; everything else keeps working |

Do not add a sixth. Anything else under `site/data/` is for external
consumers, and making the page depend on it would blank the site for every
visitor if it ever went missing.

## AI search — things that are easy to get wrong

- **Never hand-edit `ai-search/vocab.generated.js`.** It is generated from
  `site/data/index.json` by `tools/gen_vocab.py` and is the validation
  allowlist. The Worker must be redeployed (`npx wrangler deploy`) after a
  data refresh for new filter values to be accepted.
- **Never add a free-text field to `RESPONSE_SCHEMA`** in
  `ai-search/prompt.js`. `keywords` is the only one, and it is capped and
  charset-stripped. A second one turns the endpoint into an open LLM relay.
- **Debug Gemini directly, not through the Worker.** The Worker returns a
  bare 502 by design. `python ai-search/scripts/probe_gemini.py` shows
  Google's real error; `bisect_request.py` finds the rejected field. Never
  change-and-redeploy on a guess.
- **Gemini facts that are measured, not documented:** any single schema
  `enum` over 122 values is rejected (so `areaOfStudy` is a plain string
  array with its vocabulary in the prompt); `response_format` must be
  top-level; on the free tier every full "Flash" model is 20 requests/day
  and only the Flash-**Lite** models (500/day each) are usable. Check
  <https://aistudio.google.com/rate-limit> before changing `GEMINI_MODEL`.
- **Never set `temperature`** on Gemini 3 — the official guide says values
  below 1.0 cause looping; it's what the 6-second timeouts were.
- **`queryOverride` in `app.js` is tri-state.** `null` = search the box,
  `""` = search nothing (filters only). Collapsing them makes an AI query
  with no keywords Fuse-search the student's whole sentence.
- `ai-search.js` must stay optional: `app.js` calls it last, inside a guard,
  and every element it uses ships `hidden`. Renaming the file must be a
  non-event.
