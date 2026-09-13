"""
Phase 6: export the scraped awards.db into the JSON the static frontend
loads. Validates the scrape looks complete/healthy BEFORE writing anything
into site/data/ — a bad or partial scrape must fail this script (non-zero
exit) rather than silently publish a broken dataset over the last-known-good
one committed in git.
"""
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from waterloo_awards.config import DB_PATH

SITE_DATA_DIR = Path(__file__).parent.parent / "site" / "data"

# Historical total has consistently been ~1519-1521; guard against a scrape
# that silently ran against a broken/empty search rather than the real site.
MIN_TOTAL_ROWS = 1400
MIN_SCRAPED_FRACTION = 0.95

MULTI_VALUE_COLUMNS = {
    "level": "levels",
    "application_selection": "terms",
    "award_type": "award_types",
    "affiliation": "affiliations",
    "area_of_study": "areas_of_study",
}

# The "no value" placeholder the site renders as a lone dash — both a plain
# hyphen and U+2011 (non-breaking hyphen) have been observed.
BLANK_PLACEHOLDER_RE = re.compile(r"^[\-‑]$")


def clean_scalar(value):
    if value is None:
        return None
    value = value.strip()
    if not value or BLANK_PLACEHOLDER_RE.match(value):
        return None
    return value


def split_multi_value(value):
    cleaned = clean_scalar(value)
    if cleaned is None:
        return []
    return [part.strip() for part in cleaned.split(",") if part.strip()]


def build_award_record(row: sqlite3.Row) -> dict:
    record = {
        "award_id": row["award_id"],
        "award_name": clean_scalar(row["award_name"]),
        "career": clean_scalar(row["career"]),
        "award_type": clean_scalar(row["award_type"]),
        "award_description": clean_scalar(row["award_description"]),
        "award_value_description": clean_scalar(row["award_value_description"]),
        "eligibility_selection_criteria": clean_scalar(row["eligibility_selection_criteria"]),
        "area_of_study": clean_scalar(row["area_of_study"]),
        "application_details": clean_scalar(row["application_details"]),
        "required_supporting_documents": clean_scalar(row["required_supporting_documents"]),
        "contact_detail": clean_scalar(row["contact_detail"]),
        "affiliation": clean_scalar(row["affiliation"]),
        "level": clean_scalar(row["level"]),
        "application_selection": clean_scalar(row["application_selection"]),
    }
    for text_col, array_key in MULTI_VALUE_COLUMNS.items():
        record[array_key] = split_multi_value(row[text_col])

    # UW_AWARD_ADDNLINST ("additional instructions") has no named column —
    # it only ever lives in raw_fields_json. Promote it before we drop the
    # blob so this content isn't silently lost from the public export.
    additional_instructions = None
    if row["raw_fields_json"]:
        try:
            raw_fields = json.loads(row["raw_fields_json"])
            additional_instructions = clean_scalar(raw_fields.get("UW_AWARD_ADDNLINST"))
        except (json.JSONDecodeError, TypeError):
            pass
    record["additional_instructions"] = additional_instructions

    return record


# The long prose fields are ~1.4 MB of the 2.6 MB total, so dropping them
# gives external consumers a ~440 KB index they can fetch cheaply. The
# comma-joined display duplicates (level/area_of_study/...) are dropped too:
# the array forms carry the same information.
SLIM_KEY_MAP = {
    "award_id": "id",
    "award_name": "name",
    "career": "career",
    "levels": "levels",
    "terms": "terms",
    "award_types": "types",
    "affiliations": "affiliations",
    "areas_of_study": "areas",
    "award_value_description": "value",
}


def build_slim_record(record: dict) -> dict:
    return {short: record.get(full) for full, short in SLIM_KEY_MAP.items()}


def facet_counts(records):
    facets = {}
    for key, is_list in [
        ("career", False), ("levels", True), ("terms", True),
        ("award_types", True), ("affiliations", True), ("areas_of_study", True),
    ]:
        counts = {}
        for r in records:
            value = r.get(key)
            for item in (value or []) if is_list else ([value] if value else []):
                counts[item] = counts.get(item, 0) + 1
        facets[key] = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return facets


def build_manifest(records, meta):
    """A small self-describing entry point for anyone consuming this data."""
    return {
        "schema_version": 1,
        "generated_at_utc": meta["generated_at_utc"],
        "last_updated": meta["last_updated"],
        "total_awards": meta["total_awards"],
        "disclaimer": (
            "Unofficial mirror of the University of Waterloo Awards Directory. "
            "Not affiliated with or endorsed by the University of Waterloo, and "
            "not an official API. Verify anything time-sensitive against "
            "https://uwaterloo.ca/awards-directory/. Refreshed ~3x/year — please "
            "cache rather than polling."
        ),
        "endpoints": {
            "awards": {"path": "awards.json", "description": "all awards, every field"},
            "awards_slim": {"path": "awards.slim.json",
                            "description": "all awards without long prose fields",
                            "keys": list(SLIM_KEY_MAP.values())},
            "meta": {"path": "meta.json", "description": "freshness and counts"},
        },
        "facets": facet_counts(records),
    }


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0]
    scraped = conn.execute("SELECT COUNT(*) FROM awards WHERE scraped_at IS NOT NULL").fetchone()[0]
    errors = conn.execute("SELECT COUNT(*) FROM awards WHERE scrape_error IS NOT NULL").fetchone()[0]

    print(f"Total indexed: {total}, detail-scraped: {scraped}, with scrape_error: {errors}")

    if total < MIN_TOTAL_ROWS:
        print(f"VALIDATION FAILED: total rows {total} is below the minimum expected {MIN_TOTAL_ROWS}. "
              f"Refusing to publish — this looks like a broken/partial scrape.", file=sys.stderr)
        sys.exit(1)

    scraped_fraction = scraped / total if total else 0
    if scraped_fraction < MIN_SCRAPED_FRACTION:
        print(f"VALIDATION FAILED: only {scraped_fraction:.1%} of rows have details scraped "
              f"(need >= {MIN_SCRAPED_FRACTION:.0%}). Refusing to publish.", file=sys.stderr)
        sys.exit(1)

    rows = conn.execute("SELECT * FROM awards ORDER BY award_name COLLATE NOCASE").fetchall()
    records = [build_award_record(r) for r in rows]

    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_awards": total,
        "scraped_awards": scraped,
        "scrape_error_count": errors,
        "source_url": "https://uwaterloo.ca/awards-directory/",
    }
    slim = [build_slim_record(r) for r in records]
    manifest = build_manifest(records, meta)

    # Sanity-check the derived outputs before anything is swapped into place.
    assert len(slim) == len(records), "slim export lost records"
    ids = [r["award_id"] for r in records]
    assert all(ids) and len(set(ids)) == len(ids), "award_id missing or duplicated"

    # Write every output to a temp file first, then swap them all together at
    # the end. Previously awards.json was replaced before meta.json, so a
    # failure between the two left site/data half-updated; with four outputs
    # that window matters more.
    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    pending = [
        ("awards.json", records, True),
        ("meta.json", meta, False),
        ("awards.slim.json", slim, True),
        ("index.json", manifest, False),
    ]
    for name, payload, minified in pending:
        tmp = SITE_DATA_DIR / (name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            if minified:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(payload, f, ensure_ascii=False, indent=2)

    for name, _, _ in pending:
        (SITE_DATA_DIR / (name + ".tmp")).replace(SITE_DATA_DIR / name)

    print(f"Wrote {len(records)} awards to {SITE_DATA_DIR / 'awards.json'}")
    print(f"Wrote {len(slim)} slim records to {SITE_DATA_DIR / 'awards.slim.json'}")
    print(f"Wrote endpoint manifest to {SITE_DATA_DIR / 'index.json'}")
    print(f"Wrote metadata to {SITE_DATA_DIR / 'meta.json'}: {meta}")
    conn.close()


if __name__ == "__main__":
    main()
