import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS awards (
    award_id                       TEXT PRIMARY KEY,   -- UW_AWARD_ID / grid "Award profile ID", e.g. '202600008'
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

    raw_fields_json                TEXT,               -- full {field_name: text} dict, catch-all for anything
                                                        -- not covered by the named columns above

    indexed_at                     TEXT,
    scraped_at                     TEXT,
    scrape_error                   TEXT
);
CREATE INDEX IF NOT EXISTS idx_awards_scraped_at ON awards(scraped_at);
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


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


def upsert_index_row(conn, award_id, award_name, level, career, application_selection):
    conn.execute(
        """
        INSERT INTO awards (award_id, award_name, level, career, application_selection, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(award_id) DO UPDATE SET
            award_name = excluded.award_name,
            level = excluded.level,
            career = excluded.career,
            application_selection = excluded.application_selection,
            indexed_at = excluded.indexed_at
        """,
        (award_id, award_name, level, career, application_selection, now_iso()),
    )
    conn.commit()


def upsert_detail_row(conn, award_id, fields: dict):
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
        WHERE award_id = ?
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
            award_id,
        ),
    )
    conn.commit()


def mark_error(conn, award_id, error_message):
    conn.execute(
        "UPDATE awards SET scrape_error = ? WHERE award_id = ?",
        (error_message, award_id),
    )
    conn.commit()


def unscraped_award_ids(conn):
    rows = conn.execute(
        "SELECT award_id FROM awards WHERE scraped_at IS NULL ORDER BY award_id"
    ).fetchall()
    return [r[0] for r in rows]


def count_all(conn):
    return conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0]


def count_unscraped(conn):
    return conn.execute("SELECT COUNT(*) FROM awards WHERE scraped_at IS NULL").fetchone()[0]
