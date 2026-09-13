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

Do not make `site/js/*` fetch anything beyond `data/awards.json` and
`data/meta.json`; the other files under `site/data/` are for external
consumers, and a missing one would blank the page for every visitor.
