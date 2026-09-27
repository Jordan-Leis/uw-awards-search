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

Do not make `site/js/*` fetch anything beyond `data/awards.json`,
`data/meta.json`, and the AI Worker's `/interpret` endpoint (which is
optional — `app.js` guards on it and the page must work identically without
it). The other files under `site/data/` are for external consumers, and a
missing one would blank the page for every visitor.

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
