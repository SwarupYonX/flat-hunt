"""
scheduler.py - Runs the scraper on a fixed interval

Usage:
    python scheduler.py              # runs every N hours (set in config.json)
    python scheduler.py --once       # run once immediately and exit
    python scheduler.py --interval 1 # override interval (hours)

Keep this running in the background (tmux, screen, or nohup).
"""

import argparse
import logging
import time
from datetime import datetime

from scraper import run as scrape_run
from db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Rental Bot Scheduler")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the scraper once and exit",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Override scrape interval in hours (e.g. 0.5 for 30 min)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass dry-run to scraper (no Telegram alerts sent)",
    )
    args = parser.parse_args()

    # Load interval from config if not overridden
    if args.interval is None:
        import json, os
        cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(cfg_path) as f:
            cfg = json.load(f)
        interval_hours = cfg["scheduler"]["interval_hours"]
    else:
        interval_hours = args.interval

    interval_sec = interval_hours * 3600

    init_db()

    if args.once:
        logger.info("Running scraper once...")
        scrape_run(dry_run=args.dry_run)
        return

    logger.info(
        "Rental bot started. Scraping every %.1f hours. Press Ctrl+C to stop.",
        interval_hours,
    )

    while True:
        logger.info("=== Scrape run starting at %s ===", datetime.now().strftime("%Y-%m-%d %H:%M"))
        try:
            scrape_run(dry_run=args.dry_run)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break
        except Exception as e:
            logger.exception("Unexpected error during scrape run: %s", e)

        next_run = datetime.fromtimestamp(time.time() + interval_sec)
        logger.info("Next run at %s. Sleeping...", next_run.strftime("%H:%M"))

        try:
            time.sleep(interval_sec)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break


if __name__ == "__main__":
    main()
