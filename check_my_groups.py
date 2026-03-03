"""Check which FB groups the user is a member of."""
import json
import time
import re
from playwright.sync_api import sync_playwright

with open("config.json") as f:
    CONFIG = json.load(f)

JS_EXTRACT = """els => els.map(el => ({
    href: el.getAttribute('href') || '',
    text: (el.innerText || '').trim().split('\\n')[0]
}))"""

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
    with open("cookies.json") as f:
        context.add_cookies(json.load(f))

    page = context.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )

    # Visit the user's groups page
    page.goto(
        "https://www.facebook.com/groups/joins/",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    time.sleep(4)

    # Scroll to load all groups
    for i in range(10):
        page.mouse.wheel(0, 2000)
        time.sleep(1.5)

    # Extract all group links
    groups = page.eval_on_selector_all('a[href*="/groups/"]', JS_EXTRACT)

    seen = set()
    my_groups = []
    skip_slugs = {"joins", "feed", "discover", "search", "create", "notifications"}
    for g in groups:
        m = re.search(r"/groups/([^/?]+)", g["href"])
        if m and m.group(1) not in seen and m.group(1) not in skip_slugs:
            seen.add(m.group(1))
            slug = m.group(1)
            name = g["text"][:60] if g["text"] else slug
            my_groups.append(
                {
                    "slug": slug,
                    "name": name,
                    "url": f"https://www.facebook.com/groups/{slug}/",
                }
            )

    # Check which are already tracked
    tracked = set()
    for g in CONFIG.get("groups", []):
        m2 = re.search(r"/groups/([^/?]+)", g["url"])
        if m2:
            tracked.add(m2.group(1))

    print(f"You are a member of {len(my_groups)} groups:\n")
    for g in my_groups:
        marker = "[TRACKED]" if g["slug"] in tracked else "[NOT TRACKED]"
        print(f"  {marker:14s} {g['name'][:50]:50s} | {g['url']}")

    not_tracked = [g for g in my_groups if g["slug"] not in tracked]
    print(f"\n{len(not_tracked)} groups you are in but NOT tracked by the bot.")

    browser.close()
