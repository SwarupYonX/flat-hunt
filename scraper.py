"""
scraper.py - Facebook Marketplace rental listing scraper

Uses Playwright to scroll through FB Marketplace search results for rentals
in Bengaluru (JP Nagar / Jayanagar). Extracts listings, scores them,
and sends Telegram alerts for good matches.

Flow:
    1. Load saved cookies (so we don't need to log in every time)
    2. Navigate to Marketplace search URL
    3. Scroll through results page, extracting listing cards
    4. For each new listing: score it → save to DB → alert if good
    5. (Optional) Visit individual listing pages for full description

Usage:
    python scraper.py               # normal run
    python scraper.py --login       # open browser for manual login, then save cookies
    python scraper.py --dry-run     # scrape but don't send Telegram alerts
"""

import argparse
import hashlib
import json
import logging
import os
import random
import re
import time
from urllib.parse import urlencode, quote_plus

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from db import init_db, is_seen, save_listing, mark_alerted
from scorer import score_listing, should_alert, format_score_breakdown, extract_price, detect_bhk
from notifier import send_listing_alert, send_summary

# ── Config ─────────────────────────────────────────────────────────────────

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

SCRAPER_CFG = CONFIG["scraper"]
SEARCH_CFG = CONFIG["search"]
COOKIES_PATH = os.path.join(os.path.dirname(__file__), SCRAPER_CFG["cookies_file"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────

def build_search_url(keyword: str, city: str = "Bengaluru") -> str:
    """
    Build a Facebook Marketplace search URL for rentals.
    Example: https://www.facebook.com/marketplace/bangalore/search?query=2bhk+rent
    """
    city_slug = city.lower().replace(" ", "")
    query = quote_plus(keyword)
    # category_id=257581301112477 is "Property Rentals" on FB Marketplace India
    return (
        f"https://www.facebook.com/marketplace/{city_slug}/search"
        f"?query={query}&category_id=257581301112477"
    )


def random_delay(base: float, jitter: float = 1.5):
    """Sleep for base ± jitter seconds to mimic human behaviour."""
    time.sleep(max(0.5, base + random.uniform(-jitter, jitter)))


def load_cookies(context):
    """Load saved session cookies into the browser context."""
    if not os.path.exists(COOKIES_PATH):
        logger.warning(
            "No cookies file found at %s. Run with --login first.", COOKIES_PATH
        )
        return False
    with open(COOKIES_PATH) as f:
        cookies = json.load(f)
    context.add_cookies(cookies)
    logger.info("Loaded %d cookies from %s", len(cookies), COOKIES_PATH)
    return True


def save_cookies(context):
    """Save current browser session cookies to disk."""
    cookies = context.cookies()
    with open(COOKIES_PATH, "w") as f:
        json.dump(cookies, f, indent=2)
    logger.info("Saved %d cookies to %s", len(cookies), COOKIES_PATH)


# ── Login flow ─────────────────────────────────────────────────────────────

def do_manual_login():
    """
    Open a visible browser, let the user log in manually,
    then save cookies for future headless runs.
    """
    logger.info("Opening browser for manual login...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=50)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto("https://www.facebook.com/login")
        logger.info(
            "Log in manually in the browser window. "
            "After login, navigate to https://www.facebook.com/marketplace "
            "and wait for it to load fully. Then press ENTER here."
        )
        input("Press ENTER after you are logged in and Marketplace is open...")
        save_cookies(context)
        browser.close()
    logger.info("Login complete. You can now run the scraper without --login.")


# ── Listing extraction ─────────────────────────────────────────────────────

def extract_listing_from_card_data(card: dict) -> dict | None:
    """
    Extract listing data from a plain dict collected via JS eval.
    card keys: id, href, text, img
    This avoids stale element handle errors after page navigation.
    """
    try:
        listing_id = card.get("id", "")
        if not listing_id:
            return None

        url = f"https://www.facebook.com/marketplace/item/{listing_id}/"
        raw_text = card.get("text", "")
        lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

        price = None
        title = ""
        location = ""

        for line in lines:
            if price is None and ("₹" in line or re.search(r"\brs\.?\s*\d{4,}", line, re.I)):
                price = extract_price(line)
            elif not title and len(line) > 3 and "₹" not in line:
                title = line
            elif not location and len(line) > 3 and title and line != title:
                location = line

        return {
            "id": listing_id,
            "title": title,
            "price": price,
            "location": location,
            "description": "",
            "url": url,
            "image_url": card.get("img", ""),
        }
    except Exception as e:
        logger.debug("Card data extraction error: %s", e)
        return None


def extract_listing_from_link(link_el) -> dict | None:
    """
    Extract listing data from a Marketplace <a> link element.
    FB renders each card as an <a href="/marketplace/item/ID/"> whose
    closest div parent contains all the text we need (price, title, location).
    """
    try:
        href = link_el.get_attribute("href") or ""
        id_match = re.search(r"/marketplace/item/(\d+)", href)
        if not id_match:
            return None
        listing_id = id_match.group(1)
        url = f"https://www.facebook.com/marketplace/item/{listing_id}/"

        # All visible text in the card, split by lines
        raw_text = link_el.evaluate(
            "el => el.closest('div[class]')?.innerText || el.innerText || ''"
        )
        lines = [l.strip() for l in (raw_text or "").splitlines() if l.strip()]

        # Image
        image_url = ""
        img_el = link_el.query_selector("img")
        if img_el:
            image_url = img_el.get_attribute("src") or ""

        # Parse lines: typically [price, title, location, ...]
        price = None
        title = ""
        location = ""

        for line in lines:
            if price is None and ("₹" in line or re.search(r"\brs\.?\s*\d{4,}", line, re.I)):
                price = extract_price(line)
            elif not title and len(line) > 3 and "₹" not in line:
                title = line
            elif not location and len(line) > 3 and title and line != title:
                location = line

        return {
            "id": listing_id,
            "title": title,
            "price": price,
            "location": location,
            "description": "",
            "url": url,
            "image_url": image_url,
        }

    except Exception as e:
        logger.debug("Link extraction error: %s", e)
        return None


def fetch_listing_detail(page, url: str) -> str:
    """
    Visit the individual listing page and extract the full description + location.
    Returns combined text string (may be empty on failure).
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        random_delay(2, 1)

        # Try clicking "See more" to expand truncated descriptions
        try:
            see_more = page.locator('div[role="button"]:has-text("See more")').first
            if see_more.is_visible(timeout=2000):
                see_more.click()
                random_delay(0.5, 0.3)
        except Exception:
            pass

        # Get full page text and extract the Description section
        full_text = page.inner_text("body")
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]

        # Find "Description" header and grab the text after it
        desc = ""
        for i, line in enumerate(lines):
            if line.lower() == "description":
                # Next non-empty lines are the description
                desc_lines = []
                for j in range(i + 1, min(i + 10, len(lines))):
                    if lines[j].lower() in ("seller information", "seller details", "sponsored", "today's picks"):
                        break
                    desc_lines.append(lines[j])
                desc = " ".join(desc_lines).strip()
                break

        # Extract location: FB detail pages show location in several places.
        # Strategy 1: Look for a line with "Bengaluru" that also has a locality
        # Strategy 2: Scan all lines for known area keywords
        # Strategy 3: Look near "Listed X ago" for address
        full_address = ""

        # Known areas to look for (both target and reject — we want to capture
        # the area regardless so the scorer can judge it)
        all_areas = (
            SEARCH_CFG.get("primary_area_aliases", [])
            + SEARCH_CFG.get("secondary_areas", [])
            + SEARCH_CFG.get("reject_areas", [])
        )

        # First pass: find any line containing a Bengaluru locality
        for line in lines[:50]:
            low = line.lower()
            if "bengaluru" in low and len(line) > 14:
                # This is likely a full address line like "JP Nagar 4th Phase, Bengaluru, KA"
                full_address = line
                break

        # If generic "Bengaluru, KA", look for a better line with specific locality
        if not full_address or full_address.lower().strip() in ("bengaluru, ka", "bengaluru, karnataka", "bengaluru"):
            for line in lines[:50]:
                low = line.lower()
                if any(a.lower() in low for a in all_areas) and len(line) > 5:
                    full_address = line
                    break

        # Fallback: search near "Listed X ago"
        if not full_address:
            for i, line in enumerate(lines):
                if "listed" in line.lower() and "ago" in line.lower():
                    for j in range(i - 1, max(i - 6, 0), -1):
                        if "bengaluru" in lines[j].lower() and len(lines[j]) > 8:
                            full_address = lines[j]
                            break
                    break

        combined = " ".join(filter(None, [desc, full_address]))
        return combined if len(combined) > 5 else ""

    except PlaywrightTimeout:
        logger.debug("Timeout fetching detail for %s", url)
    except Exception as e:
        logger.debug("Detail fetch error for %s: %s", url, e)
    return ""


# ── Main scrape loop ────────────────────────────────────────────────────────

def scrape_keyword(page, keyword: str, dry_run: bool = False) -> tuple[int, int]:
    """
    Scrape one search keyword. Returns (total_seen, total_alerted).
    """
    url = build_search_url(keyword, SEARCH_CFG["city"])
    logger.info("Searching: %s  →  %s", keyword, url)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        logger.warning("Timeout loading search URL for '%s'. Skipping.", keyword)
        return 0, 0

    random_delay(SCRAPER_CFG["delay_between_scrolls_sec"], 1)

    # Scroll to load more listings
    for i in range(SCRAPER_CFG["scroll_times"]):
        page.mouse.wheel(0, 1500)
        random_delay(SCRAPER_CFG["delay_between_scrolls_sec"], 1)
        logger.debug("Scroll %d/%d", i + 1, SCRAPER_CFG["scroll_times"])

    # Collect ALL card data from the search page in one JS call
    # BEFORE navigating anywhere — element handles go stale after navigation
    raw_cards = page.eval_on_selector_all(
        "a[href*='/marketplace/item/']",
        """els => els.map(el => ({
            href: el.getAttribute('href') || '',
            text: (el.closest('div[class]')?.innerText || el.innerText || '').trim(),
            img:  (el.querySelector('img')?.getAttribute('src') || '')
        }))"""
    )

    # Deduplicate by listing ID
    seen_ids = set()
    cards = []
    for card in raw_cards:
        m = re.search(r"/marketplace/item/(\d+)", card["href"])
        if m and m.group(1) not in seen_ids:
            seen_ids.add(m.group(1))
            cards.append({**card, "id": m.group(1)})

    logger.info("Found %d unique listings", len(cards))

    seen_count = 0
    alerted_count = 0

    for card in cards:
        listing = extract_listing_from_card_data(card)
        if not listing:
            continue

        if is_seen(listing["id"]):
            logger.debug("Already seen listing %s", listing["id"])
            continue

        seen_count += 1
        logger.info("New listing: %s | ₹%s | %s", listing["id"], listing.get("price"), listing.get("title", "")[:50])

        # Do NOT auto-tag area from search keyword — FB search is not geo-filtered
        # and returns listings from all over Bengaluru. Area points are only
        # awarded if the listing's actual description/location mentions the area.

        # Always fetch the detail page — search cards only show "Bengaluru, KA"
        # and the specific locality (JP Nagar etc.) is only on the detail page
        price = listing.get("price")
        bhk = detect_bhk(listing.get("title", ""))
        max_allowed = SEARCH_CFG["max_price_2bhk"] if bhk == "2bhk" else SEARCH_CFG["max_price_1bhk"]
        if price is None or price <= max_allowed:
            listing["description"] = fetch_listing_detail(page, listing["url"])
            random_delay(SCRAPER_CFG["delay_between_listings_sec"], 1)

        # Score the listing
        listing["score"] = score_listing(listing)
        logger.info("Score: %d  (threshold: %d)", listing["score"], CONFIG["scoring"]["alert_threshold"])

        # Save to DB
        save_listing(listing)

        # Alert if good enough
        if should_alert(listing):
            breakdown = format_score_breakdown(listing)
            logger.info("ALERT: Sending Telegram notification for listing %s", listing["id"])
            if not dry_run:
                ok = send_listing_alert(listing, breakdown)
                if ok:
                    mark_alerted(listing["id"])
                    alerted_count += 1
            else:
                logger.info("[DRY RUN] Would send alert: %s", breakdown)
                alerted_count += 1

    return seen_count, alerted_count


# ── Group scraping ───────────────────────────────────────────────────────

def ensure_group_member(page, group_name: str) -> bool:
    """
    Check if the current FB account is a member of the group.
    If not, click the 'Join group' button to request membership.
    Returns True if we appear to have access to posts.
    """
    body_text = page.inner_text("body")[:2000].lower()

    # Already a member — check for typical member indicators
    if "join group" not in body_text:
        return True

    # Try clicking the Join button
    try:
        join_btn = page.locator('div[role="button"]:has-text("Join group")').first
        if join_btn.is_visible(timeout=3000):
            logger.info("Not a member of '%s'. Clicking Join...", group_name)
            join_btn.click()
            random_delay(2, 1)

            # Some groups show a questionnaire or are instant-join.
            # If there's a confirmation dialog, just close it.
            # Check if we now have post access
            page.reload(wait_until="domcontentloaded", timeout=20000)
            random_delay(2, 1)
            new_text = page.inner_text("body")[:2000].lower()
            if "join group" not in new_text:
                logger.info("Successfully joined group '%s'", group_name)
                return True
            else:
                logger.info("Join request sent for '%s' (may need admin approval)", group_name)
                return False
    except Exception as e:
        logger.debug("Could not click Join for '%s': %s", group_name, e)

    return False


def fetch_commerce_listing_detail(page, listing_url: str) -> dict:
    """
    Visit a /commerce/listing/ID/ page and extract title, price, description.
    Returns dict with keys: title, price, description, location, image_url.
    """
    result = {"title": "", "price": None, "description": "", "location": "", "image_url": ""}
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=20000)
        random_delay(2, 1)

        full_text = page.inner_text("body")
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]

        # Extract price (first line with ₹ or "Rs")
        for line in lines:
            if "₹" in line or re.search(r"\brs\.?\s*\d{4,}", line, re.I):
                result["price"] = extract_price(line)
                break

        # Title is usually near the top — first meaningful line after price
        skip_words = {
            "marketplace", "facebook", "listed in", "seller details",
            "seller information", "description", "notifications",
            "unread chats", "chats", "search", "home", "watch",
            "groups", "menu", "create", "profile",
        }
        for line in lines[:20]:
            low = line.lower().strip()
            if (len(line) > 5 and "₹" not in line
                    and not any(sw in low for sw in skip_words)
                    and not re.match(r"^\d+$", line.strip())):
                result["title"] = line[:100]
                break

        # Description section
        for i, line in enumerate(lines):
            if line.lower() == "description":
                desc_lines = []
                for j in range(i + 1, min(i + 10, len(lines))):
                    if lines[j].lower() in ("seller information", "seller details",
                                             "sponsored", "today's picks"):
                        break
                    desc_lines.append(lines[j])
                result["description"] = " ".join(desc_lines).strip()
                break

        # Location — look for Bengaluru mention
        for line in lines[:30]:
            if "bengaluru" in line.lower() and len(line) > 8:
                result["location"] = line
                break

        # Image
        try:
            img = page.query_selector('img[src*="scontent"]')
            if img:
                result["image_url"] = img.get_attribute("src") or ""
        except Exception:
            pass

    except PlaywrightTimeout:
        logger.debug("Timeout fetching commerce listing %s", listing_url)
    except Exception as e:
        logger.debug("Commerce listing fetch error for %s: %s", listing_url, e)

    return result


def scrape_group(page, group: dict, dry_run: bool = False) -> tuple[int, int]:
    """
    Scrape a Facebook Group for rental posts.
    Handles both traditional discussion posts AND Buy & Sell (commerce) listings.
    group dict has 'name' and 'url' keys.
    Returns (total_seen, total_alerted).
    """
    name = group["name"]
    url = group["url"]
    logger.info("Scraping group: %s  →  %s", name, url)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        logger.warning("Timeout loading group '%s'. Skipping.", name)
        return 0, 0

    random_delay(3, 1)

    # Ensure we're a member — auto-join if not
    if not ensure_group_member(page, name):
        logger.info("Skipping group '%s' — not yet a member (pending approval).", name)
        return 0, 0

    # Scroll to load content
    for i in range(8):
        page.mouse.wheel(0, 2000)
        random_delay(2, 1)

    # Extract BOTH traditional posts and commerce listings in one JS call
    raw_items = page.evaluate("""() => {
        const items = [];
        const seen = new Set();

        // Method 1: Traditional group posts (div[role="article"])
        const articles = document.querySelectorAll('div[role="article"]');
        articles.forEach(art => {
            const text = art.innerText || '';
            if (text.length < 30) return;

            let permalink = '';
            const links = art.querySelectorAll('a[href]');
            for (const a of links) {
                const h = a.getAttribute('href') || '';
                if (h.includes('/posts/') || h.includes('/permalink/')) {
                    permalink = h;
                    break;
                }
            }
            const img = art.querySelector('img[src*="scontent"]');
            const imgSrc = img ? img.getAttribute('src') : '';

            items.push({
                type: 'post',
                text: text.substring(0, 2000),
                permalink: permalink,
                img: imgSrc,
                commerceId: ''
            });
        });

        // Method 2: Buy & Sell commerce listings
        const commerceLinks = document.querySelectorAll('a[href*="/commerce/listing/"]');
        commerceLinks.forEach(a => {
            const href = a.getAttribute('href') || '';
            const match = href.match(/\\/commerce\\/listing\\/(\\d+)/);
            if (match && !seen.has(match[1])) {
                seen.add(match[1]);
                items.push({
                    type: 'commerce',
                    text: '',
                    permalink: '',
                    img: '',
                    commerceId: match[1]
                });
            }
        });

        return items;
    }""")

    posts = [i for i in raw_items if i["type"] == "post"]
    commerce = [i for i in raw_items if i["type"] == "commerce"]
    logger.info("Found %d posts + %d commerce listings in group '%s'",
                len(posts), len(commerce), name)

    seen_count = 0
    alerted_count = 0

    # Process traditional posts (text already available)
    for post in posts:
        text = post.get("text", "")
        permalink = post.get("permalink", "")

        if permalink:
            post_id = "grp_" + re.sub(r"[^0-9]", "", permalink)[-15:]
        else:
            post_id = "grp_" + hashlib.md5(text[:200].encode()).hexdigest()[:12]

        if is_seen(post_id):
            continue

        price = extract_price(text)
        bhk = detect_bhk(text.lower())
        title_lines = [l.strip() for l in text.splitlines() if l.strip()]
        title = title_lines[0][:80] if title_lines else "Group post"

        if permalink.startswith("/"):
            post_url = "https://www.facebook.com" + permalink
        elif permalink.startswith("http"):
            post_url = permalink
        else:
            post_url = url

        listing = {
            "id": post_id,
            "title": title,
            "price": price,
            "location": "",
            "description": text[:500],
            "url": post_url,
            "image_url": post.get("img", ""),
        }

        if price:
            max_allowed = SEARCH_CFG["max_price_2bhk"] if bhk == "2bhk" else SEARCH_CFG["max_price_1bhk"]
            if price > max_allowed:
                save_listing({**listing, "score": 0})
                continue

        seen_count += 1
        listing["score"] = score_listing(listing)
        logger.info("Group post: %s | ₹%s | Score: %d | %s",
                     post_id, price, listing["score"], title[:40])

        save_listing(listing)

        if should_alert(listing):
            breakdown = format_score_breakdown(listing)
            logger.info("ALERT: Group post %s", post_id)
            if not dry_run:
                ok = send_listing_alert(listing, breakdown)
                if ok:
                    mark_alerted(post_id)
                    alerted_count += 1
            else:
                logger.info("[DRY RUN] Would alert: %s", breakdown)
                alerted_count += 1

    # Process commerce listings (need to visit detail pages)
    for item in commerce[:20]:  # cap at 20 per group to avoid rate limits
        cid = item["commerceId"]
        post_id = "com_" + cid

        if is_seen(post_id):
            continue

        listing_url = f"https://www.facebook.com/commerce/listing/{cid}/"
        detail = fetch_commerce_listing_detail(page, listing_url)
        random_delay(SCRAPER_CFG["delay_between_listings_sec"], 1)

        price = detail["price"]
        title = detail["title"] or f"Commerce listing {cid}"
        bhk = detect_bhk((title + " " + detail["description"]).lower())

        listing = {
            "id": post_id,
            "title": title,
            "price": price,
            "location": detail["location"],
            "description": detail["description"][:500],
            "url": listing_url,
            "image_url": detail["image_url"],
        }

        # Quick reject: over budget
        if price:
            max_allowed = SEARCH_CFG["max_price_2bhk"] if bhk == "2bhk" else SEARCH_CFG["max_price_1bhk"]
            if price > max_allowed:
                save_listing({**listing, "score": 0})
                continue

        seen_count += 1
        listing["score"] = score_listing(listing)
        logger.info("Commerce: %s | ₹%s | Score: %d | %s",
                     post_id, price, listing["score"], title[:40])

        save_listing(listing)

        if should_alert(listing):
            breakdown = format_score_breakdown(listing)
            logger.info("ALERT: Commerce listing %s", post_id)
            if not dry_run:
                ok = send_listing_alert(listing, breakdown)
                if ok:
                    mark_alerted(post_id)
                    alerted_count += 1
            else:
                logger.info("[DRY RUN] Would alert: %s", breakdown)
                alerted_count += 1

    return seen_count, alerted_count


def run(dry_run: bool = False):
    """Main scraper run: iterate over all search keywords and groups."""
    init_db()

    total_scraped = 0
    total_alerted = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=SCRAPER_CFG["headless"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )

        # Load saved session cookies
        cookies_loaded = load_cookies(context)
        if not cookies_loaded:
            logger.error(
                "Cannot proceed without cookies. Run: python scraper.py --login"
            )
            browser.close()
            return

        page = context.new_page()

        # Stealth: remove navigator.webdriver flag
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        for keyword in SEARCH_CFG["search_keywords"]:
            try:
                scraped, alerted = scrape_keyword(page, keyword, dry_run=dry_run)
                total_scraped += scraped
                total_alerted += alerted
                # Pause between keyword searches
                random_delay(5, 2)
            except Exception as e:
                logger.exception("Error scraping keyword '%s': %s", keyword, e)

        # Scrape Facebook Groups
        for group in CONFIG.get("groups", []):
            try:
                scraped, alerted = scrape_group(page, group, dry_run=dry_run)
                total_scraped += scraped
                total_alerted += alerted
                random_delay(5, 2)
            except Exception as e:
                logger.exception("Error scraping group '%s': %s", group["name"], e)

        # Save updated cookies (session may have refreshed)
        save_cookies(context)
        browser.close()

    logger.info(
        "Run complete. Scraped: %d new listings, Alerted: %d",
        total_scraped, total_alerted
    )

    if not dry_run and (total_scraped > 0 or total_alerted > 0):
        send_summary(total_scraped, total_alerted)


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FB Marketplace Rental Scraper")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open browser for manual Facebook login and save cookies",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape and score but do not send Telegram alerts",
    )
    args = parser.parse_args()

    if args.login:
        do_manual_login()
    else:
        run(dry_run=args.dry_run)
