"""Search Facebook for rental groups in Bangalore."""
import json
import time
import re
from playwright.sync_api import sync_playwright

CONFIG_PATH = "config.json"
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

COOKIES_PATH = CONFIG["scraper"]["cookies_file"]

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
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
    with open(COOKIES_PATH) as f:
        context.add_cookies(json.load(f))

    page = context.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )

    searches = [
        "bangalore flat rent no broker",
        "south bangalore house rent",
        "bangalore flat flatmates",
        "bangalore rental apartment owner",
    ]

    all_groups = {}
    for query in searches:
        url = f"https://www.facebook.com/search/groups/?q={query.replace(' ', '%20')}"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        for _ in range(3):
            page.mouse.wheel(0, 1500)
            time.sleep(1.5)

        # Get page text and extract group info
        body = page.inner_text("body")
        lines = [l.strip() for l in body.splitlines() if l.strip()]

        # Extract all group href links
        hrefs = page.eval_on_selector_all(
            'a[href*="/groups/"]',
            """els => els.map(el => ({
                href: el.getAttribute('href') || '',
                text: (el.innerText || '').trim().split('\\n')[0]
            }))"""
        )

        for item in hrefs:
            href = item["href"]
            m = re.search(r"/groups/([^/?]+)", href)
            if m and m.group(1) not in ("search", "feed", "discover"):
                slug = m.group(1)
                if slug not in all_groups:
                    all_groups[slug] = {
                        "slug": slug,
                        "url": f"https://www.facebook.com/groups/{slug}/",
                        "name": item["text"][:60] if item["text"] else slug,
                    }

    # Already have these
    existing = set()
    for g in CONFIG.get("groups", []):
        m = re.search(r"/groups/([^/?]+)", g["url"])
        if m:
            existing.add(m.group(1))

    print(f"\nFound {len(all_groups)} unique groups total.")
    print(f"Already tracking: {len(existing)}")
    print(f"\n--- NEW groups (not yet tracked) ---")
    new_count = 0
    for slug, g in all_groups.items():
        marker = "  [ALREADY TRACKED]" if slug in existing else "  [NEW]"
        if slug not in existing:
            new_count += 1
        print(f"{marker} {g['name'][:50]:50s} | {g['url']}")

    print(f"\n{new_count} new groups found.")
    browser.close()
