"""
notifier.py - Telegram alert sender

Sends a formatted message to your Telegram chat when a good listing is found.
Uses the Telegram Bot HTTP API (no extra library needed beyond requests).
"""

import json
import os
import logging
import requests

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

TG = CONFIG["telegram"]
BOT_TOKEN = TG["bot_token"]
CHAT_ID = TG["chat_id"]

logger = logging.getLogger(__name__)


def _send_message(text: str) -> bool:
    """Send a plain-text or HTML message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Telegram sendMessage failed: %s", e)
        return False


def _send_photo(image_url: str, caption: str) -> bool:
    """Send a photo with caption via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": CHAT_ID,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.warning("Telegram sendPhoto failed (%s), falling back to text.", e)
        return False


def send_listing_alert(listing: dict, breakdown: str = "") -> bool:
    """
    Send a formatted Telegram alert for a rental listing.

    listing keys used: title, price, location, description, url, image_url, score
    """
    price = listing.get("price")
    share = listing.get("price_share")
    bhk = listing.get("bhk")

    if not price:
        price_str = "Price not listed"
    elif bhk == "2bhk" and share and share != price:
        price_str = f"₹{price:,}/mo total  →  your share ~₹{share:,}/mo"
    else:
        price_str = f"₹{price:,}/mo"

    score = listing.get("score", 0)

    # Score bar (visual)
    bar_filled = min(int(score / 10), 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    # Truncate description to keep message readable
    desc = listing.get("description", "").strip()
    if len(desc) > 300:
        desc = desc[:297] + "..."

    caption = (
        f"<b>🏠 New Rental — {listing.get('title', 'Listing')}</b>\n\n"
        f"💰 <b>{price_str}</b>\n"
        f"📍 {listing.get('location', 'Location unknown')}\n"
        f"⭐ Score: <b>{score}/100</b>  [{bar}]\n"
    )

    if breakdown:
        caption += f"✅ {breakdown}\n"

    if desc:
        caption += f"\n<i>{desc}</i>\n"

    caption += f"\n<a href=\"{listing.get('url', '')}\">View on Facebook →</a>"

    # Try with photo first, fall back to text-only
    image_url = listing.get("image_url", "")
    if image_url and image_url.startswith("http"):
        success = _send_photo(image_url, caption)
        if success:
            return True

    return _send_message(caption)


def send_summary(total_scraped: int, total_alerted: int):
    """Send a brief run-summary message (optional, called after each scrape run)."""
    msg = (
        f"<b>Rental Bot — Run Complete</b>\n"
        f"Scraped: {total_scraped} listings\n"
        f"Alerted: {total_alerted} new matches"
    )
    _send_message(msg)


def test_connection() -> bool:
    """
    Verify your bot token and chat ID work.
    Run this once: python notifier.py
    """
    return _send_message(
        "Rental bot connected! I'll alert you when I find good listings in JP Nagar."
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ok = test_connection()
    if ok:
        print("Telegram connection OK")
    else:
        print("Telegram connection FAILED — check bot_token and chat_id in config.json")
