"""
Phase 3: enumerate every unique award via the search grid, capturing
{award_id, award_name, level, career, application_selection}. The results
grid caps at ~300 rows per search, so this recursively narrows any combo
that comes back capped: Career+Level (7 valid pairs) -> + Award type (6
values) -> + Selection process (2 values). See config.py CAREER_LEVEL_PAIRS
for why Citizenship/Affiliation aren't used as split dimensions (they'd
silently drop awards with no value set for that field).

Safe to re-run: every award row is upserted (ON CONFLICT DO UPDATE) into the
awards table, keyed by award_id, so a rerun just refreshes the index columns.
"""
import logging
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

sys.path.insert(0, str(Path(__file__).parent))
from waterloo_awards import db
from waterloo_awards.browser import (
    open_search_page, clear_all_filters, apply_filters, set_select_filter,
    clear_select_filter, run_search, scrape_grid_rows, parse_rowcount_text,
)
from waterloo_awards.config import (
    DB_PATH, CAREER_LEVEL_PAIRS, AWARD_TYPE_CODES, SELECTION_PROCESS_CODES, ROW_CAP,
)

# Must precede basicConfig: its FileHandler opens the log file at import
# time, which fails on a fresh checkout where logs/ does not exist yet.
Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("logs/scrape_index.log", encoding="utf-8")],
)
log = logging.getLogger("scrape_index")

incomplete_leaves = []  # combos that were still capped even at the deepest split


def enumerate_combo(page, conn, filters: dict, remaining_dims: list, depth=0, apply_all=False):
    """filters is used for labeling/logging; the actual dropdown state is
    applied incrementally (apply_all=True only for the top-level Career+Level
    call — deeper recursion only ever sets the ONE new dimension it adds,
    since Career/Level/etc. from the parent are already active on the page).
    Each recursive dimension is explicitly cleared after its loop finishes,
    so a sibling at the parent level never inherits a stale child filter
    (confirmed live: without this, e.g. a leftover Selection process=STSA
    from a finished Award-type=ASP sub-search silently corrupted the row
    counts for the next Award-type sibling)."""
    if apply_all:
        apply_filters(page, filters)
    rc_text = run_search(page)
    shown, total, capped = parse_rowcount_text(rc_text, ROW_CAP)
    rows = scrape_grid_rows(page)

    label = ", ".join(f"{k}={v}" for k, v in filters.items())
    if not capped:
        log.info(f"{'  '*depth}{label}: {rc_text!r} -> {len(rows)} rows (complete)")
        for r in rows:
            db.upsert_index_row(conn, r["award_id"], r["award_name"], r["level"], r["career"], r["application_selection"])
        return len(rows)

    if not remaining_dims:
        log.warning(
            f"{'  '*depth}{label}: STILL CAPPED at deepest split — {rc_text!r}, "
            f"only captured {len(rows)} of {total or 'unknown total'}. Storing what we have."
        )
        incomplete_leaves.append((dict(filters), rc_text, len(rows), total))
        for r in rows:
            db.upsert_index_row(conn, r["award_id"], r["award_name"], r["level"], r["career"], r["application_selection"])
        return len(rows)

    log.info(f"{'  '*depth}{label}: {rc_text!r} -> capped, splitting further by {remaining_dims[0][0]!r}")
    (next_label, next_codes), *rest = remaining_dims
    captured = 0
    for code in next_codes:
        set_select_filter(page, next_label, code)
        sub_filters = {**filters, next_label: code}
        captured += enumerate_combo(page, conn, sub_filters, rest, depth + 1, apply_all=False)
    clear_select_filter(page, next_label)
    return captured


def run_combo_with_recovery(context, conn, career, level, max_retries=5):
    for attempt in range(max_retries + 1):
        try:
            page = open_search_page(context)
            clear_all_filters(page)
            n = enumerate_combo(
                page, conn,
                {"Career": career, "Level": level},
                [("Award type", AWARD_TYPE_CODES), ("Selection process", SELECTION_PROCESS_CODES)],
                apply_all=True,
            )
            page.close()
            return n
        except Exception as e:
            log.warning(f"Career={career} Level={level}: attempt {attempt+1} failed ({e}); "
                        f"reopening a fresh search page and retrying this combo.")
            try:
                page.close()
            except Exception:
                pass
    log.error(f"Career={career} Level={level}: giving up after {max_retries+1} attempts.")
    return 0


def main():
    conn = db.connect(DB_PATH)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        grand_total = 0
        for career, level in CAREER_LEVEL_PAIRS:
            n = run_combo_with_recovery(context, conn, career, level)
            grand_total += n
            log.info(f"=== Career={career} Level={level}: captured {n} rows so far this combo ===")

        browser.close()

    unique_count = db.count_all(conn)
    log.info(f"Sum of per-combo captures (with overlap from multi-level awards): {grand_total}")
    log.info(f"Unique awards in index after dedup: {unique_count}")

    if incomplete_leaves:
        log.warning(f"{len(incomplete_leaves)} leaf combo(s) were still capped even at the deepest split:")
        for filters, rc_text, shown, total in incomplete_leaves:
            log.warning(f"  {filters} -> {rc_text!r} ({shown} captured" + (f" of {total} total)" if total else ")"))
        log.warning("These awards are under-represented in the index; see plan notes for further splitting options "
                     "(e.g. Award name prefix search) if this gap matters.")
    else:
        log.info("No leaf combo remained capped — index should be complete.")

    conn.close()


if __name__ == "__main__":
    main()
