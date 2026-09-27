"""
Run one source adapter into awards.db.

    python3 scraper/run_adapter.py alberta-student-aid
    python3 scraper/run_adapter.py alberta-student-aid --limit 5 --dry-run

The UW scrape keeps its own entry points (scrape_index.py -> scrape_details.py)
because it needs a browser and a two-pass resumable enumeration. Every other
source goes through here.
"""
import argparse
import importlib
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from adapters.base import Adapter  # noqa: E402
from adapters.http import PoliteFetcher, RobotsDisallowed  # noqa: E402
from waterloo_awards import db, sources as source_registry  # noqa: E402
from waterloo_awards.config import DB_PATH  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("run_adapter")


def load_adapter_class(adapter_name):
    module = importlib.import_module(f"adapters.{adapter_name}")
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Adapter) and value is not Adapter:
            return value
    raise RuntimeError(f"adapters/{adapter_name}.py defines no Adapter subclass")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source_id", help="registry id, e.g. alberta-student-aid")
    ap.add_argument("--limit", type=int, help="stop after N awards (for development)")
    ap.add_argument("--dry-run", action="store_true", help="parse and print; write nothing")
    ap.add_argument("--no-detail", action="store_true",
                    help="index only, skip per-award detail pages (fast, but descriptions "
                         "stay truncated and eligibility extraction will be poor)")
    ap.add_argument("--rate-limit", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--db", default=None, help="override the database path")
    args = ap.parse_args()

    registry = source_registry.load_all()
    entry = source_registry.get(registry, args.source_id)

    # A blocked source must be unrunnable, not merely discouraged. The crawl
    # policy is enforced twice: here by id, and again per-URL in PoliteFetcher
    # against the live robots.txt.
    if entry["status"] == "blocked":
        log.error("[%s] is blocked in the registry: %s", args.source_id,
                  entry["blocked_reason"].strip())
        return 2
    if entry["status"] != "active":
        log.error("[%s] status is %r, not 'active'", args.source_id, entry["status"])
        return 2

    adapter_cls = load_adapter_class(entry["adapter"])
    # A single-tenant adapter pins its own id; a multi-tenant one (one parser,
    # several institutions) leaves it None and takes the id from the registry.
    if adapter_cls.source_id is not None and adapter_cls.source_id != args.source_id:
        raise RuntimeError(
            f"adapter {entry['adapter']} declares source_id "
            f"{adapter_cls.source_id!r}, registry says {args.source_id!r}"
        )

    fetcher = PoliteFetcher(rate_limit=args.rate_limit)
    adapter = adapter_cls(fetcher, entry=entry, limit=args.limit,
                          fetch_detail=not args.no_detail)

    conn = None
    if not args.dry_run:
        conn = db.connect(args.db or DB_PATH)

    count = errors = 0
    try:
        for record in adapter.fetch_all():
            count += 1
            if record.raw_fields.get("detail_error"):
                errors += 1
                log.warning("%s: detail fetch failed: %s",
                            record.native_id, record.raw_fields["detail_error"])
            if conn:
                db.upsert_award(conn, record.as_row(), record.raw_fields)
            else:
                log.info("%s | %s | %s", record.native_id, record.award_name[:52],
                         record.amount_raw or "")
            if count % 10 == 0:
                log.info("  %d awards so far (%d HTTP requests)", count, fetcher.fetch_count)
    except RobotsDisallowed as e:
        log.error("%s", e)
        return 3

    minimum = entry.get("min_awards", 0)
    log.info("Done: %d awards, %d detail errors, %d HTTP requests",
             count, errors, fetcher.fetch_count)

    # A deliberately truncated run is not a broken source, so the floor only
    # applies to a full one.
    if args.limit or args.dry_run:
        log.info("skipping the min_awards floor (%d): this was a partial run", minimum)
    elif count < minimum:
        # Same reasoning as export_data's publish gate: a source that suddenly
        # yields far less than it should has broken, and saying so loudly beats
        # quietly writing a smaller dataset.
        log.error("[%s] produced %d awards, below its min_awards floor of %d",
                  args.source_id, count, minimum)
        return 1

    if conn:
        log.info("[%s] now has %d rows in %s", args.source_id,
                 db.count_all(conn, args.source_id), args.db or DB_PATH)
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
