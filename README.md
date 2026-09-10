# Waterloo Awards Finder (unofficial)

An independent, unofficial mirror of the University of Waterloo's [Awards Directory](https://uwaterloo.ca/awards-directory/),
rebuilt as a single fast, client-side searchable/filterable page. **Not affiliated with or endorsed by the
University of Waterloo** — see [`site/about.html`](site/about.html) for the full disclaimer and known data gaps.

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
