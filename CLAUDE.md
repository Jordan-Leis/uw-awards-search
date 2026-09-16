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
- **Affiliation is an eligibility gate.** An award tagged e.g. `Women` is
  restricted to that group; untagged awards are open to everyone.

## Privacy

`tools/profile.json` holds the user's citizenship, financial need and grade
band. It is gitignored. **Never print its contents, paste it into a message,
or commit it.** `tools/profile.example.json` is the safe, committed template.

## Repo layout

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
