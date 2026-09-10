import re
import time
import logging

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout

from .config import ENTRY_PAGE_URL, AWARD_DEEPLINK_TEMPLATE

log = logging.getLogger("waterloo_awards")

ROWCOUNT_SELECTOR = '#win0divG3SEARCH_RESULTrowcnt\\$0 span.ps-text'
GRID_ROW_SELECTOR = 'tr.ps_grid-row'


def wait_for_modal_gone(page, timeout_ms=25000):
    """PeopleSoft shows a full-page overlay (#pt_modalMask) while a postback
    is processing, which blocks pointer events on everything underneath it.
    Confirmed live: this is the actual cause behind nearly all the flaky
    click/select timeouts seen during development — Playwright's own click
    retries against the blocked element for its full timeout and gives up,
    when simply waiting for the overlay to clear first resolves it. Best
    -effort: if the mask selector itself isn't found, proceed anyway (the
    caller's own action still has Playwright's built-in actionability wait
    as a fallback)."""
    try:
        page.locator("#pt_modalMask").wait_for(state="hidden", timeout=timeout_ms)
    except Exception:
        pass


def open_search_page(context):
    """Navigate the real click-through path (uwaterloo.ca -> Find Awards) and
    return a page sitting on the search form. Direct navigation to the
    internal GBL search URL redirects to a PeopleSoft sign-in wall; only the
    click-through from the public entry page (or, for detail pages, the
    UW_AWARD_ID deep link) resolves as guest."""
    page = context.new_page()
    page.goto(ENTRY_PAGE_URL, wait_until="load", timeout=60000)
    find_awards = page.get_by_role("link", name="Find Awards", exact=False)
    if find_awards.count() == 0:
        find_awards = page.locator('a:has-text("FIND AWARDS")')
    wait_for_modal_gone(page)
    find_awards.first.click()
    page.wait_for_load_state("load", timeout=60000)
    page.wait_for_selector('select[aria-label="Career"]', timeout=20000)
    return page


def current_rowcount_text(page):
    try:
        return page.locator(ROWCOUNT_SELECTOR).first.inner_text(timeout=1000)
    except PWTimeout:
        return None


def wait_for_rowcount_change(page, prev_text, timeout_ms=30000):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        cur = current_rowcount_text(page)
        if cur is not None and cur != prev_text:
            return cur
        page.wait_for_timeout(250)
    raise TimeoutError(f"row count text did not change from {prev_text!r} within {timeout_ms}ms")


def parse_rowcount_text(text, row_cap):
    """Returns (shown, total_or_None, is_capped).
    - "Showing X of possible Y rows" -> X shown, Y true total; capped iff X < Y
    - "N rows" (bare) -> the "of possible Y" clause hasn't been appended yet
      (confirmed live: this always upgrades to the full format ~1s later, so
      run_search() polls for that upgrade before returning). If we still only
      have the bare form here, fall back to treating N >= row_cap as capped
      with an unknown true total.
    """
    if text is None:
        return 0, None, False
    m = re.search(r"Showing (\d+) of possible (\d+)", text)
    if m:
        shown, total = int(m.group(1)), int(m.group(2))
        return shown, total, shown < total
    m = re.search(r"(\d+)\s*rows?", text)
    if m:
        n = int(m.group(1))
        return n, None, n >= row_cap
    return 0, None, False


def set_select_filter(page, aria_label, value, attempts=5):
    """Changing one dropdown (esp. Career, which rebuilds the Level option
    list server-side as a dependent dropdown) can transiently remove/replace
    a sibling <select> node mid-postback; retry through that window rather
    than letting a single slow postback hard-fail the whole run."""
    selector = f'select[aria-label="{aria_label}"]'
    last_err = None
    for attempt in range(attempts):
        try:
            page.wait_for_selector(selector, timeout=15000)
            wait_for_modal_gone(page)
            page.select_option(selector, value=value, timeout=15000)
            page.wait_for_timeout(1500)
            actual = page.locator(selector).input_value(timeout=10000)
            if actual != value:
                # Confirmed live: a select_option call can return without
                # error yet not actually take effect (e.g. mid dependent-
                # dropdown rebuild), silently leaving a stale/blank filter
                # and corrupting the resulting row count. Treat as a
                # retryable failure rather than trusting a clean return.
                raise PWTimeout(f"value is {actual!r} after select_option, expected {value!r}")
            return
        except Exception as e:
            last_err = e
            log.warning(f"set_select_filter({aria_label!r}, {value!r}) attempt {attempt+1} failed ({e}), retrying...")
            page.wait_for_timeout(2000)
    raise last_err


def clear_select_filter(page, aria_label):
    selector = f'select[aria-label="{aria_label}"]'
    try:
        page.select_option(selector, value="")
        page.wait_for_timeout(700)
    except Exception:
        pass


CANONICAL_FILTER_ORDER = ["Career", "Level", "Award type", "Selection process"]


def clear_all_filters(page, attempts=3):
    """Click the Clear link to reset every filter before starting a fresh
    top-level Career/Level combo (filters persist across searches within the
    same page otherwise)."""
    last_err = None
    for attempt in range(attempts):
        try:
            clear_link = page.locator("#G3SEARCH_DRV_G3CLEAR_PB")
            if clear_link.count() == 0:
                clear_link = page.get_by_role("link", name="Clear", exact=True).first
            wait_for_modal_gone(page)
            clear_link.click(timeout=10000)
            page.wait_for_timeout(900)
            return
        except Exception as e:
            last_err = e
            log.warning(f"clear_all_filters attempt {attempt+1} failed ({e}), retrying...")
            page.wait_for_timeout(1500)
    raise last_err


def apply_filters(page, filters: dict):
    """filters: {aria_label: code}. Applied in a fixed order (Career before
    Level, since Level's option list depends on Career) regardless of dict
    insertion order."""
    for label in CANONICAL_FILTER_ORDER:
        if label in filters:
            set_select_filter(page, label, filters[label])


def run_search(page):
    prev = current_rowcount_text(page)
    search_link = page.locator("#G3SEARCH_DRV_G3SEARCH_PB")
    if search_link.count() == 0:
        search_link = page.get_by_role("link", name="Search", exact=True).first
    clicked = False
    last_err = None
    for attempt in range(3):
        try:
            wait_for_modal_gone(page)
            search_link.click(timeout=15000)
            clicked = True
            break
        except Exception as e:
            last_err = e
            log.warning(f"run_search: Search click attempt {attempt+1} failed ({e}), retrying...")
            page.wait_for_timeout(1500)
    if not clicked:
        raise last_err
    try:
        # A genuine 0-result search removes the rowcount element from the DOM
        # entirely rather than showing "0 rows", so current_rowcount_text()
        # stays None forever in that case — 8s is comfortably above the ~5-7s
        # a real update takes, so we don't burn 30s waiting on every empty
        # result (there are many, across dozens of narrow sub-searches).
        text = wait_for_rowcount_change(page, prev, timeout_ms=8000)
    except TimeoutError:
        page.wait_for_timeout(500)
        text = current_rowcount_text(page)

    if text is None:
        return None  # 0 results — nothing to settle/poll further

    # The "of possible Y" total is appended asynchronously slightly after the
    # row count first updates (confirmed live: a bare "300 rows" reading
    # reliably upgrades to "Showing 300 of possible 336 rows" ~1s later).
    # Poll briefly for that upgrade so we get the true total when available.
    settle_deadline = time.time() + 3.0
    while time.time() < settle_deadline:
        page.wait_for_timeout(400)
        cur = current_rowcount_text(page)
        if cur and cur != text:
            text = cur
        if text and "possible" in text:
            break
    return text


def scrape_grid_rows(page):
    """Returns a list of dicts: award_id, award_name, level, career, application_selection."""
    soup = BeautifulSoup(page.content(), "lxml")
    rows = []
    for tr in soup.select(GRID_ROW_SELECTOR):
        def cell_text(col_class):
            td = tr.select_one(f"td.{col_class} a")
            return td.get_text(strip=True) if td else None

        award_name = cell_text("G3KEYVAL_1_BTN")
        level = cell_text("G3KEYVAL_2_BTN")
        career = cell_text("G3KEYVAL_3_BTN")
        application_selection = cell_text("G3KEYVAL_4_BTN")
        award_id = cell_text("G3KEYVAL_5_BTN")
        if award_id:
            rows.append({
                "award_id": award_id,
                "award_name": award_name,
                "level": level,
                "career": career,
                "application_selection": application_selection,
            })
    return rows


def goto_award_detail(page, award_id, timeout_ms=30000):
    url = AWARD_DEEPLINK_TEMPLATE.format(award_id=award_id)
    page.goto(url, wait_until="load", timeout=timeout_ms)
    page.wait_for_selector('#fld-PAGEREC-UW_AWARD_DISPLAYNM-editor', timeout=timeout_ms)


BLOCK_TAGS = ["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "tr"]


def extract_block_text(root):
    """Flatten one field's rich-text HTML to plain text.

    Block elements (list items, paragraphs) become separate lines, while
    inline elements (links, spans, emphasis) flow naturally inside their
    sentence. Using BeautifulSoup's get_text(separator=...) instead would
    put a separator at *every* text-node boundary, which mangles sentences
    containing inline links, e.g. "please contact the applicable |
    Department Graduate Co-ordinator | ." — the separator can't tell a real
    list boundary from a mid-sentence link.
    """
    for br in root.find_all("br"):
        br.replace_with("\n")

    blocks = [b for b in root.find_all(BLOCK_TAGS) if not b.find(BLOCK_TAGS)]
    if not blocks:
        # No block structure (plain text in the editor div) — take it whole.
        return re.sub(r"[ \t]+", " ", root.get_text()).strip()

    lines = []
    for b in blocks:
        # No separator: the source's own whitespace between inline nodes is
        # preserved, then collapsed. Don't strip per-node or words fuse together.
        text = re.sub(r"[ \t]+", " ", b.get_text()).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def read_detail_fields(page):
    """Generic extraction: every fld-PAGEREC-<NAME>-editor element present on
    the page, keyed by <NAME>. Which fields appear varies per award (only
    populated fields render), so this deliberately doesn't assume a fixed set."""
    soup = BeautifulSoup(page.content(), "lxml")
    fields = {}
    for el in soup.select('[id^="fld-PAGEREC-"][id$="-editor"]'):
        field_name = el["id"][len("fld-PAGEREC-"):-len("-editor")]
        text = extract_block_text(el)
        if text:
            fields[field_name] = text
    return fields
