# Waterloo Awards Finder (unofficial)

An independent, unofficial mirror of the University of Waterloo's [Awards Directory](https://uwaterloo.ca/awards-directory/),
rebuilt as a single fast, client-side searchable/filterable page. **Not affiliated with or endorsed by the
University of Waterloo** — see [`site/about.html`](site/about.html) for the full disclaimer and known data gaps.

**Live at https://jordanleis.com/awards-database/** (also mirrored at
https://jordan-leis.github.io/uw-awards-search/).

## Find awards that fit you

```
cp tools/profile.example.json tools/profile.json   # edit with your program/year/interests
python tools/find_awards.py                        # ranked shortlist
python tools/find_awards.py --facets               # valid values for every filter
python tools/find_awards.py --detail 202600074     # full text for one award
```

Stdlib only — no venv needed. It prints a compact shortlist (~150 chars per award) rather than
the 2.6 MB dataset, so results fit in a chat or an AI assistant's context.

**`tools/profile.json` is gitignored and must never be committed** — it holds citizenship,
financial need and grade band. See [`tools/README.md`](tools/README.md) for the full reference,
including why faculty-wide expansion nearly triples the eligible pool.

## Ask it a question (AI search)

Type a sentence into the search box and press Enter — *"bursaries for a 2nd
year software engineering student"* — and the right filters get ticked. It's
free for students (no account, no key) and free to run.

The model **never sees any award data**. A Cloudflare Worker sends Gemini the
question plus the filter vocabulary, Gemini returns which filter values apply,
the Worker discards anything not in the vocabulary, and the browser applies
the rest with the same code the dropdowns use. So it can't invent an award or
a filter, costs ~1.2k tokens a query, and adds the one thing the data lacks:
knowing that a CS student is in the Mathematics faculty and should see those
faculty-wide awards too (which nearly triples their pool).

Runs on Gemini's free tier — two Flash-Lite models at 500 requests/day each,
shared by all visitors — with an edge cache in front. When quota runs out the
box silently falls back to keyword search. Setup, the real quota numbers, and
the abuse model are in [`ai-search/README.md`](ai-search/README.md).

## Machine-readable endpoints (unofficial)

Served from both `https://jordanleis.com/awards-database/data/` and
`https://jordan-leis.github.io/uw-awards-search/data/`:

| file | contents | size |
|---|---|---|
| `index.json` | manifest — freshness, counts, all facet vocabularies | ~6 KB |
| `awards.slim.json` | every award minus the long prose fields | ~455 KB |
| `awards.json` | every award, every field | ~2.6 MB |
| `meta.json` | freshness and counts only | <1 KB |

**Unofficial mirror, not a University of Waterloo API.** No stability or availability guarantee.
Refreshed ~3×/year (Jan 1 / May 1 / Sep 1, plus up to 24h of sync lag to jordanleis.com), so
please cache rather than polling, and verify anything time-sensitive against the official directory.

## How it works

- `scraper/` — a Python + Playwright scraper that drives the official directory's search UI (it's built on
  Oracle PeopleSoft with no plain HTTP API) to enumerate every award and fetch full details into a local
  SQLite database (`awards.db`).
  - `scrape_index.py` — enumerates all award IDs/names/levels/careers via the search grid, recursively
    splitting searches to work around the site's ~300-row-per-search cap.
  - `scrape_details.py` — fetches full details for each award via its direct `UW_AWARD_ID` deep link
    (discovered from the site's own "Copy Award Link" feature). Resumable — safe to re-run.
  - `export_data.py` — exports `awards.db` into `site/data/awards.json` + `meta.json` for the frontend,
    after validating the scrape looks complete and healthy (refuses to publish a broken/partial scrape).
- `site/` — the static frontend (no build step, no framework): plain HTML/CSS/JS, full-text search via
  [Fuse.js](https://www.fusejs.io/), plus a "match my profile" quick filter stored in `localStorage`.
- `.github/workflows/refresh-data.yml` — runs the whole pipeline on a schedule (Jan 1 / May 1 / Sep 1) and
  on demand, then deploys `site/` to GitHub Pages.

## Running the scraper locally

```
cd scraper
python -m venv venv
venv\Scripts\activate       # or source venv/bin/activate on macOS/Linux
pip install -r requirements.txt
python -m playwright install chromium
python scrape_index.py
python scrape_details.py
python export_data.py
```

Then open `site/index.html` (via a local static server, e.g. `python -m http.server` from `site/`) to
browse the freshly-exported data.

## License / data ownership

The award data itself belongs to the University of Waterloo; this project only mirrors what's already
publicly visible on the official guest-accessible directory, to make it easier to search. This repository's
code is provided as-is for the benefit of Waterloo students.
