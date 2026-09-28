import json
import sqlite3
from datetime import datetime, timezone

# Schema v2. The v1 table keyed awards on award_id alone — UW's PeopleSoft
# UW_AWARD_ID. That is unique within Waterloo and says nothing about any other
# source, so a second source could silently overwrite a UW award with a
# colliding native id. The key is now (source_id, native_id), and award_uid
# ("<source>:<native>") is the stable public identifier used in permalinks and
# in tools/find_awards.py --detail.
#
# Everything added in v2 is nullable. The UW scrape populates none of it yet,
# so the existing export, site and Worker keep working unchanged while later
# phases fill it in.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS awards (
    source_id                      TEXT NOT NULL,      -- registry id, e.g. 'uw'
    native_id                      TEXT NOT NULL,      -- the source's own id; for UW, UW_AWARD_ID e.g. '202600008'
    award_uid                      TEXT GENERATED ALWAYS AS (source_id || ':' || native_id) STORED,

    award_name                     TEXT,               -- from search grid
    level                          TEXT,               -- from search grid, e.g. "UG Year 2, UG Year 3, UG Year 4"
    career                         TEXT,               -- from search grid, e.g. "Undergraduate"
    application_selection          TEXT,               -- from search grid, e.g. "Winter"

    award_type                     TEXT,
    award_description              TEXT,
    award_value_description        TEXT,
    eligibility_selection_criteria TEXT,
    area_of_study                  TEXT,
    detail_level                   TEXT,               -- UW_AWARD_LEVEL as shown on the detail page
    application_details            TEXT,
    required_supporting_documents  TEXT,
    contact_detail                 TEXT,
    affiliation                    TEXT,               -- UW_AWARD_AFFLTNIND, only present for some awards

    -- v2, all nullable. Populated by later phases; no adapter sets them yet.
    source_url                     TEXT,               -- per-award canonical link back to the source
    deadline_raw                   TEXT,               -- deadline exactly as the source stated it
    deadline_date                  TEXT,               -- ISO 8601 date, when one could be parsed unambiguously
    amount_min                     INTEGER,
    amount_max                     INTEGER,
    amount_raw                     TEXT,
    renewable                      INTEGER,            -- 0/1/NULL
    application_type               TEXT,               -- open | nomination | institution_mediated | member_only
    application_status             TEXT,               -- Open | Ended | Upcoming | NULL=unknown (most sources say nothing)
    eligibility_json               TEXT,               -- structured eligibility with per-field confidence + source sentence

    raw_fields_json                TEXT,               -- full {field_name: text} dict, catch-all for anything
                                                        -- not covered by the named columns above

    indexed_at                     TEXT,
    scraped_at                     TEXT,
    scrape_error                   TEXT,

    PRIMARY KEY (source_id, native_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_awards_uid ON awards(award_uid);
CREATE INDEX IF NOT EXISTS idx_awards_scraped_at ON awards(scraped_at);
CREATE INDEX IF NOT EXISTS idx_awards_source ON awards(source_id);
"""

# Maps the detail page's raw field-name id fragment (from
# fld-PAGEREC-<FIELDNAME>-editor) to our named column.
FIELD_COLUMN_MAP = {
    "UW_AWARD_DISPLAYNM": "award_name",
    "UW_AWARD_TYPE": "award_type",
    "UW_AWARD_LDESCR": "award_description",
    "UW_AWARD_VALDESCR": "award_value_description",
    "UW_AWARD_EGSL_CRIT": "eligibility_selection_criteria",
    "UW_AWARD_AS_SUM": "area_of_study",
    "UW_AWARD_LEVEL": "detail_level",
    "UW_AWARD_APPDETAIL": "application_details",
    "UW_AWARD_SPDOCDSCR": "required_supporting_documents",
    "UW_AWARD_CNTCTDETS": "contact_detail",
    "UW_AWARD_AFFLTNIND": "affiliation",
}

# Columns carried over verbatim when migrating a v1 database in place.
_V1_CARRY_COLUMNS = [
    "award_name", "level", "career", "application_selection",
    "award_type", "award_description", "award_value_description",
    "eligibility_selection_criteria", "area_of_study", "detail_level",
    "application_details", "required_supporting_documents", "contact_detail",
    "affiliation", "raw_fields_json", "indexed_at", "scraped_at", "scrape_error",
]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_columns(conn, table):
    # table_xinfo, not table_info: the latter omits generated columns, so
    # award_uid would be invisible here and a v2 table could look like a v1 one.
    return {r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})")}


def migrate_v1_to_v2(conn, default_source_id="uw"):
    """Rebuild a v1 `awards` table into the v2 shape, preserving every row.

    CI always scrapes into a fresh database (awards.db is gitignored), so this
    exists for local working copies. It matters anyway: CREATE TABLE IF NOT
    EXISTS is a no-op against an existing table, so without an explicit
    migration a developer's local database would keep the v1 shape while the
    code assumed v2 — the schema drift would surface as confusing query errors
    rather than as a clear failure.
    """
    columns = _table_columns(conn, "awards")
    if not columns or "source_id" in columns:
        return False  # fresh database, or already v2

    if "award_id" not in columns:
        raise sqlite3.DatabaseError(
            "awards table has neither 'source_id' (v2) nor 'award_id' (v1); "
            "refusing to guess at its shape"
        )

    carried = [c for c in _V1_CARRY_COLUMNS if c in columns]
    col_list = ", ".join(carried)

    conn.executescript("PRAGMA foreign_keys=OFF;")
    conn.execute("ALTER TABLE awards RENAME TO awards_v1;")
    conn.executescript(SCHEMA)
    conn.execute(
        f"""
        INSERT INTO awards (source_id, native_id, {col_list})
        SELECT ?, award_id, {col_list} FROM awards_v1
        """,
        (default_source_id,),
    )
    migrated = conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0]
    original = conn.execute("SELECT COUNT(*) FROM awards_v1").fetchone()[0]
    if migrated != original:
        conn.rollback()
        raise sqlite3.DatabaseError(
            f"migration lost rows: {original} in v1, {migrated} in v2; rolled back"
        )

    conn.execute("DROP TABLE awards_v1;")
    conn.commit()
    return True


#: Columns added to SCHEMA after v2 shipped, in the form ALTER TABLE needs.
#: The migration decision for this project is "additive, UW keeps working at
#: every commit", and CREATE TABLE IF NOT EXISTS does nothing to a table that
#: already exists -- so a new nullable column reaches an existing database only
#: through here. CI always scrapes into a fresh file, but a local working copy
#: is the normal case for development and it must not need deleting.
ADDITIVE_COLUMNS = {
    "application_status": "TEXT",
}


def add_missing_columns(conn, table="awards"):
    """ALTER in any ADDITIVE_COLUMNS the table does not have yet.

    Returns the list added. Uses table_xinfo, not table_info: the latter omits
    generated columns, and award_uid is GENERATED ALWAYS AS (...) STORED.
    """
    existing = _table_columns(conn, table)
    if not existing:
        return []                # no table yet; SCHEMA will create it complete
    added = []
    for column, decl in ADDITIVE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(column)
    if added:
        conn.commit()
    return added


def connect(db_path, default_source_id="uw"):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    if migrate_v1_to_v2(conn, default_source_id):
        print(f"Migrated {db_path} from schema v1 to v2 (source_id='{default_source_id}')")
    added = add_missing_columns(conn)
    if added:
        print(f"Added column(s) to {db_path}: {', '.join(added)}")
    conn.executescript(SCHEMA)
    return conn


def upsert_index_row(conn, source_id, native_id, award_name, level, career, application_selection):
    conn.execute(
        """
        INSERT INTO awards (source_id, native_id, award_name, level, career,
                            application_selection, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, native_id) DO UPDATE SET
            award_name = excluded.award_name,
            level = excluded.level,
            career = excluded.career,
            application_selection = excluded.application_selection,
            indexed_at = excluded.indexed_at
        """,
        (source_id, native_id, award_name, level, career, application_selection, now_iso()),
    )
    conn.commit()


def upsert_detail_row(conn, source_id, native_id, fields: dict):
    """fields is the raw {field_name: text} dict scraped from the detail page."""
    named = {col: None for col in FIELD_COLUMN_MAP.values()}
    for field_name, text in fields.items():
        col = FIELD_COLUMN_MAP.get(field_name)
        if col:
            named[col] = text

    conn.execute(
        """
        UPDATE awards SET
            award_type = ?,
            award_description = ?,
            award_value_description = ?,
            eligibility_selection_criteria = ?,
            area_of_study = ?,
            detail_level = ?,
            application_details = ?,
            required_supporting_documents = ?,
            contact_detail = ?,
            affiliation = ?,
            raw_fields_json = ?,
            scraped_at = ?,
            scrape_error = NULL
        WHERE source_id = ? AND native_id = ?
        """,
        (
            named["award_type"],
            named["award_description"],
            named["award_value_description"],
            named["eligibility_selection_criteria"],
            named["area_of_study"],
            named["detail_level"],
            named["application_details"],
            named["required_supporting_documents"],
            named["contact_detail"],
            named["affiliation"],
            json.dumps(fields, ensure_ascii=False),
            now_iso(),
            source_id,
            native_id,
        ),
    )
    conn.commit()


def mark_error(conn, source_id, native_id, error_message):
    conn.execute(
        "UPDATE awards SET scrape_error = ? WHERE source_id = ? AND native_id = ?",
        (error_message, source_id, native_id),
    )
    conn.commit()


def unscraped_native_ids(conn, source_id):
    rows = conn.execute(
        "SELECT native_id FROM awards WHERE source_id = ? AND scraped_at IS NULL "
        "ORDER BY native_id",
        (source_id,),
    ).fetchall()
    return [r[0] for r in rows]


def count_all(conn, source_id=None):
    if source_id is None:
        return conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM awards WHERE source_id = ?", (source_id,)
    ).fetchone()[0]


def count_unscraped(conn, source_id=None):
    if source_id is None:
        return conn.execute("SELECT COUNT(*) FROM awards WHERE scraped_at IS NULL").fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM awards WHERE source_id = ? AND scraped_at IS NULL",
        (source_id,),
    ).fetchone()[0]


# --- generic adapter path -------------------------------------------------
# The upsert_index_row / upsert_detail_row pair above is UW-shaped: it exists
# because the PeopleSoft scrape enumerates ids first and fetches details in a
# separate resumable pass. Adapters for static sources produce a whole award in
# one go, so they use this instead.

ADAPTER_COLUMNS = [
    "award_name", "career", "level", "application_selection", "award_type",
    "award_description", "award_value_description", "eligibility_selection_criteria",
    "area_of_study", "application_details", "required_supporting_documents",
    "contact_detail", "affiliation",
    "source_url", "deadline_raw", "deadline_date", "amount_min", "amount_max",
    "amount_raw", "renewable", "application_type", "application_status",
    "eligibility_json",
]


def upsert_award(conn, record_row, raw_fields=None):
    """Insert or update one complete award from an adapter.

    record_row is AwardRecord.as_row(): a dict keyed by column name. Unknown
    keys are rejected rather than ignored, so a typo in an adapter surfaces
    immediately instead of silently dropping a field for every award it emits.
    """
    source_id = record_row["source_id"]
    native_id = record_row["native_id"]

    unknown = set(record_row) - set(ADAPTER_COLUMNS) - {"source_id", "native_id",
                                                        "additional_instructions"}
    if unknown:
        raise ValueError(f"upsert_award: unknown column(s) {sorted(unknown)}")

    # additional_instructions has no column of its own — it lives in
    # raw_fields_json and is promoted at export time, matching how UW's
    # UW_AWARD_ADDNLINST is handled.
    raw = dict(raw_fields or {})
    if record_row.get("additional_instructions"):
        raw["UW_AWARD_ADDNLINST"] = record_row["additional_instructions"]

    values = [record_row.get(c) for c in ADAPTER_COLUMNS]
    placeholders = ", ".join("?" for _ in ADAPTER_COLUMNS)
    assignments = ", ".join(f"{c} = excluded.{c}" for c in ADAPTER_COLUMNS)

    conn.execute(
        f"""
        INSERT INTO awards (source_id, native_id, {", ".join(ADAPTER_COLUMNS)},
                            raw_fields_json, indexed_at, scraped_at)
        VALUES (?, ?, {placeholders}, ?, ?, ?)
        ON CONFLICT(source_id, native_id) DO UPDATE SET
            {assignments},
            raw_fields_json = excluded.raw_fields_json,
            scraped_at = excluded.scraped_at,
            scrape_error = NULL
        """,
        [source_id, native_id, *values,
         json.dumps(raw, ensure_ascii=False, default=str) if raw else None,
         now_iso(), now_iso()],
    )
    conn.commit()
