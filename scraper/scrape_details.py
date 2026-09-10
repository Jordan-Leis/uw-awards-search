"""
Phase 4: fetch full detail fields for every award in the index. Uses the
UW_AWARD_ID deep link discovered from "Copy Award Link" (confirmed live:
resolves cold via a real browser without needing the search flow at all —
see config.AWARD_DEEPLINK_TEMPLATE), so this doesn't re-filter/click through
search results per award like the original plan draft assumed.

Resumable: only processes awards WHERE scraped_at IS NULL, and commits one
row at a time, so re-running after a crash/interruption picks up where it
left off.
"""
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

sys.path.insert(0, str(Path(__file__).parent))
from waterloo_awards import db
from waterloo_awards.browser import goto_award_detail, read_detail_fields
from waterloo_awards.config import DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("logs/scrape_details.log", encoding="utf-8")],
)
log = logging.getLogger("scrape_details")

RATE_LIMIT_SECONDS = 0.3


def fetch_one(page, award_id, attempts=2):
    last_err = None
    for attempt in range(attempts):
        try:
            goto_award_detail(page, award_id, timeout_ms=25000)
            return read_detail_fields(page)
        except (PWTimeout, TimeoutError) as e:
            last_err = e
            log.warning(f"{award_id}: attempt {attempt+1} failed ({e}); retrying...")
            page.wait_for_timeout(1500)
    raise last_err


def main():
    Path("logs").mkdir(exist_ok=True)
    conn = db.connect(DB_PATH)
    todo = db.unscraped_award_ids(conn)
    total_todo = len(todo)
    log.info(f"{total_todo} award(s) need details fetched.")

    if total_todo == 0:
        log.info("Nothing to do.")
        conn.close()
        return

    ok_count = 0
    fail_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        start = time.time()
        for i, award_id in enumerate(todo, 1):
            try:
                fields = fetch_one(page, award_id)
                db.upsert_detail_row(conn, award_id, fields)
                ok_count += 1
            except Exception as e:
                log.error(f"{award_id}: FAILED after retries: {e}")
                db.mark_error(conn, award_id, str(e))
                fail_count += 1
                # A hard failure sometimes means the page/session got into a
                # bad state (e.g. an unexpected redirect); recycle the page.
                try:
                    page.close()
                except Exception:
                    pass
                page = context.new_page()

            if i % 50 == 0 or i == total_todo:
                elapsed = time.time() - start
                rate = i / elapsed if elapsed > 0 else 0
                remaining = (total_todo - i) / rate if rate > 0 else float("inf")
                log.info(f"Progress: {i}/{total_todo} ({ok_count} ok, {fail_count} failed) "
                         f"— {rate:.2f}/s, ~{remaining/60:.1f} min remaining")

            time.sleep(RATE_LIMIT_SECONDS)

        browser.close()

    log.info(f"Done. {ok_count} succeeded, {fail_count} failed.")
    remaining_unscraped = db.count_unscraped(conn)
    if remaining_unscraped:
        log.info(f"{remaining_unscraped} award(s) still unscraped (failures keep scraped_at NULL) "
                 f"— rerun this script to retry them.")
    conn.close()


if __name__ == "__main__":
    main()
