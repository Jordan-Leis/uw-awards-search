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

    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_awards = SITE_DATA_DIR / "awards.json.tmp"
    tmp_meta = SITE_DATA_DIR / "meta.json.tmp"

    with open(tmp_awards, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, separators=(",", ":"))

    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_awards": total,
        "scraped_awards": scraped,
        "scrape_error_count": errors,
        "source_url": "https://uwaterloo.ca/awards-directory/",
    }
    with open(tmp_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # Atomic-ish: only replace the real files once both writes succeeded.
    tmp_awards.replace(SITE_DATA_DIR / "awards.json")
    tmp_meta.replace(SITE_DATA_DIR / "meta.json")

    print(f"Wrote {len(records)} awards to {SITE_DATA_DIR / 'awards.json'}")
    print(f"Wrote metadata to {SITE_DATA_DIR / 'meta.json'}: {meta}")
    conn.close()


if __name__ == "__main__":
    main()
