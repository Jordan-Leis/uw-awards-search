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
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import facets as facet_spec  # noqa: E402  single definition of what is filterable
from waterloo_awards import sources as source_registry
from waterloo_awards.config import DB_PATH

SITE_DATA_DIR = Path(__file__).parent.parent / "site" / "data"

# Publish gates now come from each source's registry entry (min_awards /
# min_scraped_fraction in sources/*.yaml) rather than from one global constant.
# With several sources a single global floor is useless: an adapter could break
# completely and still clear a corpus-wide threshold carried by the others.
DEFAULT_MIN_SCRAPED_FRACTION = 0.95

MULTI_VALUE_COLUMNS = {
    "level": "levels",
    "application_selection": "terms",
    "award_type": "award_types",
    "affiliation": "affiliations",
    "area_of_study": "areas_of_study",
}

# Dollar figures stated in prose. Most sources do not publish a numeric award
# value at all: only 54 of 4,016 records arrive with amount_min/amount_max set
# by their adapter, while 1,322 UW awards state a figure somewhere in their
# text. Without this backfill the "award value" filter matches almost
# everything and is useless.
_MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")

# Ranked by how likely the field is to hold the AWARD's value rather than an
# incidental figure. A description can mention the size of the endowment that
# funds the award, so it is only consulted when the value field says nothing.
_AMOUNT_SOURCE_FIELDS = ("award_value_description", "award_description")


def derive_amounts(record):
    """Fill amount_min/amount_max from prose when the adapter did not set them.

    Deliberately conservative: it reports the range of figures it can see and
    does not try to work out which one a given student would receive. Awards
    that state nothing keep null, and the range filter never hides a null.
    """
    if record.get("amount_max") is not None:
        return record
    for field in _AMOUNT_SOURCE_FIELDS:
        text = record.get(field)
        if not text:
            continue
        values = []
        for match in _MONEY_RE.finditer(text):
            try:
                values.append(int(float(match.group(1).replace(",", ""))))
            except ValueError:
                continue
        # A lone "$1" or similar is noise, not an award value.
        values = [v for v in values if v >= 50]
        if values:
            record["amount_min"] = min(values)
            record["amount_max"] = max(values)
            if not record.get("amount_raw"):
                record["amount_raw"] = text
            return record
    return record


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
        # award_id stays the source's own id and keeps its original name and
        # position so existing consumers of the published endpoints — and the
        # live site — are unaffected. award_uid is the new globally unique key.
        "award_id": row["native_id"],
        "source_id": row["source_id"],
        "award_uid": row["award_uid"],
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

    # Schema v2 fields. No adapter populates these yet, so they export as null
    # for every record; they are emitted unconditionally so the shape of a
    # record does not change when the first adapter starts filling them in.
    record["source_url"] = clean_scalar(row["source_url"])
    record["deadline_raw"] = clean_scalar(row["deadline_raw"])
    record["deadline_date"] = clean_scalar(row["deadline_date"])
    record["amount_min"] = row["amount_min"]
    record["amount_max"] = row["amount_max"]
    record["amount_raw"] = clean_scalar(row["amount_raw"])
    record["renewable"] = None if row["renewable"] is None else bool(row["renewable"])
    record["application_type"] = clean_scalar(row["application_type"])
    record["eligibility"] = (
        json.loads(row["eligibility_json"]) if row["eligibility_json"] else None
    )

    derive_amounts(record)
    return record


# The long prose fields are ~1.4 MB of the 2.6 MB total, so dropping them
# gives external consumers a ~440 KB index they can fetch cheaply. The
# comma-joined display duplicates (level/area_of_study/...) are dropped too:
# the array forms carry the same information.
SLIM_KEY_MAP = {
    "award_id": "id",
    "award_uid": "uid",
    "source_id": "src",
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
    """Delegates to tools/facets.py — the one place facets are defined.

    This used to be a hand-maintained list of six keys, duplicated verbatim in
    find_awards.py and again in search.js. Those copies drifted.
    """
    return facet_spec.count_values(records)


def build_manifest(records, meta, registry):
    """A small self-describing entry point for anyone consuming this data."""
    disclaimers = [
        entry["disclaimer"].strip()
        for entry in source_registry.active(registry).values()
        if entry.get("disclaimer")
    ]
    return {
        "schema_version": 2,
        "generated_at_utc": meta["generated_at_utc"],
        "last_updated": meta["last_updated"],
        "total_awards": meta["total_awards"],
        "sources": meta["sources"],
        "disclaimer": (
            "Unofficial aggregation of publicly listed Canadian student awards. "
            "Not an official API and not affiliated with or endorsed by any "
            "listed institution. Verify anything time-sensitive against the "
            "original source. Refreshed ~3x/year — please cache rather than "
            "polling. " + " ".join(disclaimers)
        ).strip(),
        "endpoints": {
            "awards": {"path": "awards.json", "description": "all awards, every field"},
            "awards_slim": {"path": "awards.slim.json",
                            "description": "all awards without long prose fields",
                            "keys": list(SLIM_KEY_MAP.values())},
            "meta": {"path": "meta.json", "description": "freshness and counts"},
            "sources": {"path": "sources.json",
                        "description": "source registry, including sources deliberately not ingested"},
            "filters": {"path": "filters.json",
                        "description": "the filter/facet spec the UI builds itself from"},
        },
        "facets": facet_counts(records),
    }


def validate_sources(conn, registry):
    """Per-source publish gate. Returns the per-source counts for the manifest.

    Fails the whole export on: an unknown source_id in the database, an active
    source below its min_awards floor, or an active source whose detail scrape
    is too incomplete. Each of those means the published dataset would be
    quietly worse than the last-known-good one committed in git.
    """
    rows = conn.execute(
        """
        SELECT source_id,
               COUNT(*) AS total,
               SUM(CASE WHEN scraped_at IS NOT NULL THEN 1 ELSE 0 END) AS scraped,
               SUM(CASE WHEN scrape_error IS NOT NULL THEN 1 ELSE 0 END) AS errors
        FROM awards GROUP BY source_id ORDER BY source_id
        """
    ).fetchall()
    counts = {r["source_id"]: dict(r) for r in rows}

    failures = []

    unknown = sorted(set(counts) - set(registry))
    if unknown:
        failures.append(
            f"database contains source_id(s) {unknown} with no entry in sources/. "
            f"Every source must be declared before its data can be published."
        )

    active = source_registry.active(registry)
    for source_id, entry in sorted(active.items()):
        row = counts.get(source_id)
        if not row:
            failures.append(
                f"[{source_id}] active in the registry but has zero rows in the database."
            )
            continue

        total, scraped = row["total"], row["scraped"] or 0
        min_awards = entry["min_awards"]
        if total < min_awards:
            failures.append(
                f"[{source_id}] {total} rows is below its min_awards floor of "
                f"{min_awards}. This looks like a broken or partial scrape."
            )

        min_fraction = entry.get("min_scraped_fraction", DEFAULT_MIN_SCRAPED_FRACTION)
        fraction = scraped / total if total else 0
        if fraction < min_fraction:
            failures.append(
                f"[{source_id}] only {fraction:.1%} of rows have details scraped "
                f"(needs >= {min_fraction:.0%})."
            )

    # A blocked source must never contribute rows — that would mean an adapter
    # ran against a source we decided not to crawl.
    for source_id in source_registry.blocked(registry):
        if counts.get(source_id, {}).get("total"):
            failures.append(
                f"[{source_id}] is marked blocked in the registry but has "
                f"{counts[source_id]['total']} rows in the database."
            )

    if failures:
        print("VALIDATION FAILED — refusing to publish:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)

    return counts


def main():
    registry = source_registry.load_all()
    print(f"Source registry: {len(source_registry.active(registry))} active, "
          f"{len(source_registry.blocked(registry))} blocked")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    per_source = validate_sources(conn, registry)
    for source_id, row in per_source.items():
        print(f"  [{source_id}] indexed: {row['total']}, detail-scraped: {row['scraped']}, "
              f"with scrape_error: {row['errors']}")

    total = sum(r["total"] for r in per_source.values())
    scraped = sum(r["scraped"] or 0 for r in per_source.values())
    errors = sum(r["errors"] or 0 for r in per_source.values())
    print(f"Total indexed: {total}, detail-scraped: {scraped}, with scrape_error: {errors}")

    rows = conn.execute(
        "SELECT * FROM awards ORDER BY award_name COLLATE NOCASE, award_uid"
    ).fetchall()
    records = [build_award_record(r) for r in rows]

    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_awards": total,
        "scraped_awards": scraped,
        "scrape_error_count": errors,
        "sources": {
            source_id: {
                "total": row["total"],
                "scraped": row["scraped"],
                "errors": row["errors"],
                "source_url": registry[source_id].get("source_url") or registry[source_id].get("url"),
            }
            for source_id, row in per_source.items()
        },
        # Retained for backward compatibility with consumers of the published
        # meta.json from when this was a single-source dataset.
        "source_url": "https://uwaterloo.ca/awards-directory/",
    }
    slim = [build_slim_record(r) for r in records]
    manifest = build_manifest(records, meta, registry)
    sources_json = source_registry.to_public_json(registry)
    filters_json = facet_spec.to_spec()

    # Sanity-check the derived outputs before anything is swapped into place.
    assert len(slim) == len(records), "slim export lost records"
    uids = [r["award_uid"] for r in records]
    assert all(uids), "award_uid missing"
    if len(set(uids)) != len(uids):
        dupes = sorted({u for u in uids if uids.count(u) > 1})[:10]
        raise AssertionError(f"award_uid duplicated, e.g. {dupes}")

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
        ("sources.json", sources_json, False),
        ("filters.json", filters_json, False),
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
    print(f"Wrote {len(sources_json)} source registry entries to {SITE_DATA_DIR / 'sources.json'}")
    print(f"Wrote {len(filters_json['facets'])} filter definitions to {SITE_DATA_DIR / 'filters.json'}")
    print(f"Wrote metadata to {SITE_DATA_DIR / 'meta.json'}: {meta}")
    conn.close()


if __name__ == "__main__":
    main()
