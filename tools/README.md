# `find_awards.py` — award matching

Filters and ranks the scraped award data locally, printing a shortlist small
enough to paste into a conversation or hand to an AI assistant.

Stdlib only — no venv, no `pip install`. Reads the committed
`site/data/awards.json`, so it works on a fresh clone.

```
python tools/find_awards.py --facets                 # see every valid filter value
python tools/find_awards.py "machine learning"       # rank by keywords
python tools/find_awards.py --detail 202600074       # full text for one award
```

## Your profile

```
cp tools/profile.example.json tools/profile.json
```

**`tools/profile.json` is gitignored — it is private and must never be committed.**
It holds citizenship, financial need and grade band. The template
`profile.example.json` is committed; the real one is not, and `git add -A`
cannot stage it.

Without a profile the tool still runs on CLI filters alone.

## Flags

| | |
|---|---|
| `--career --level --term --type --affiliation --area --faculty` | hard filters, repeatable. Values within one flag are OR'd, different flags AND'd. Added to (not replacing) the profile. |
| `--faculty Engineering` | shorthand for `Engineering Faculty - All Programs` |
| `--strict-area` | turn off faculty-wide expansion (see below) |
| `--min-value 1000` | minimum parsed dollar figure |
| `--keywords a,b` / `--exclude-keywords x,y` | extra positive / negative terms |
| `--limit N` `--min-score F` | trim the output |
| `--format compact\|block\|ids\|json` | default `compact` |
| `--detail ID...` | full record for specific awards |
| `--facets` `--stats` `--explain` | vocabularies / filter funnel / matched terms |
| `--data PATH` `--url BASE` | read a different dataset or a published `/data` URL |

## Faculty-wide expansion (on by default)

Awards are tagged either with a specific program, with
`<Faculty> Faculty - All Programs`, or with `All Programs`. A Software
Engineering student is eligible for all three, but the website's filter only
matches literally. Measured on the current dataset:

```
--area "Software Engineering" --faculty Engineering --strict-area   -> 161 awards
--area "Software Engineering" --faculty Engineering                 -> 443 awards
```

Nearly 3× the pool. Expansion is why this tool doesn't just reuse the site's
filter logic. Use `--strict-area` to reproduce the site's behaviour exactly.

## What it deliberately does not do

**Citizenship and GPA are never filters.** The source directory does not
record either per award (see the note in `site/js/profile.js`), so filtering on
them would silently drop awards. Instead, rows whose eligibility text mentions
citizenship get a `!cit` flag and ones mentioning a grade cutoff get `!gpa` —
check those by hand with `--detail`.

## Scoring

Keyword hits score their field's weight **once per field**, never per
occurrence — `award_description` is far longer than `award_name`, so counting
occurrences would let one verbose award dominate. Weights: name 3.0, area 2.0,
eligibility 1.5, description 1.0, value 0.5, application details 0.25.

Each term is damped by IDF computed over the corpus at runtime, so
"student"/"award"/"Waterloo" neutralise themselves without a stopword list.

Profile bonuses: exact program match +3.0, faculty-wide +1.0, All Programs
+0.4, affiliation match +2.0, preferred type +2.0, bursary when
`financial_need` +1.5, term mismatch −2.0, plus up to +1.5 scaled by dollar
value. Ties break by value then name, so runs are reproducible.

## Output size

The point of `compact` is fitting in a model context: ~150 chars/row, so 40
results is ~7 KB (~1.8K tokens). `--detail` is ~1.5 KB per award. Never read
`site/data/awards.json` directly — it is 2.6 MB.
