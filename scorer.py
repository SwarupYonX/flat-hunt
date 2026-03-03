"""
scorer.py - Rental listing scoring logic

Scores a listing from 0-100 based on:
- Price vs budget
- Property type (2BHK > 1BHK)
- Location (JP Nagar > Jayanagar)
- Positive keywords (gated society, lift, parking, etc.)
- Negative keywords (family only, no bachelors, etc.)

Only listings above the threshold in config.json trigger Telegram alerts.
"""

import json
import os
import re

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

SCORING = CONFIG["scoring"]
SEARCH = CONFIG["search"]


def extract_price(text: str) -> int | None:
    """
    Try to extract a monthly rent price from text.
    Handles formats like: Rs. 14,000 / ₹14000 / 14k / 14,000
    Returns the integer price or None if not found.
    """
    if not text:
        return None

    text_lower = text.lower()

    # Normalise text: remove commas, currency symbols
    cleaned = re.sub(r"[₹,]", "", text)
    cleaned = re.sub(r"rs\.?\s*", "", cleaned, flags=re.IGNORECASE)

    # Look for "Xk" shorthand (e.g. 14k, 14.5k)
    k_match = re.search(r"(\d+(?:\.\d+)?)\s*k\b", text_lower)
    if k_match:
        return int(float(k_match.group(1)) * 1000)

    # Look for plain numbers that look like rent amounts (5000–99999)
    numbers = re.findall(r"\b(\d{4,6})\b", cleaned)
    for n in numbers:
        val = int(n)
        if 3000 <= val <= 99999:
            return val

    return None


def detect_bhk(text: str) -> str | None:
    """Return '2bhk', '1bhk', or None."""
    t = text.lower()
    if any(x in t for x in ["2bhk", "2 bhk", "2-bhk", "2 bed", "2 beds", "2bed", "two bed", "two bhk"]):
        return "2bhk"
    if any(x in t for x in ["1bhk", "1 bhk", "1-bhk", "1 bed", "1 beds", "1bed", "one bed", "one bhk"]):
        return "1bhk"
    return None


def effective_share(price: int, bhk: str | None) -> int:
    """
    Return the user's share of the rent.
    For 2BHK: split 50/50 with a flatmate.
    For 1BHK or unknown: full price.
    """
    if bhk == "2bhk":
        return price // 2
    return price


def score_listing(listing: dict) -> int:
    """
    Score a listing dict. Returns integer score 0-100+.

    Expected listing keys:
        price (int|None), title (str), description (str), location (str)

    Price logic:
        2BHK — max total rent ₹32,000  (user pays ~₹16k, splits with flatmate)
        1BHK — max total rent ₹16,000  (user takes it alone)
        Unknown BHK — treated as 1BHK (conservative)
    """
    score = 0
    text = " ".join([
        listing.get("title", ""),
        listing.get("description", ""),
        listing.get("location", ""),
    ]).lower()

    bhk = detect_bhk(text)
    listing["bhk"] = bhk  # store for use in alert formatting

    # ── Price scoring ──────────────────────────────────────────────
    price = listing.get("price") or extract_price(
        listing.get("title", "") + " " + listing.get("description", "")
    )

    if price is None:
        # Can't verify price — give neutral score but don't reject
        score += 5
    else:
        # Hard reject: over the budget ceiling for this property type
        max_allowed = SEARCH["max_price_2bhk"] if bhk == "2bhk" else SEARCH["max_price_1bhk"]
        if price > max_allowed:
            return 0

        # Score based on what the user will actually pay
        share = effective_share(price, bhk)
        listing["price_share"] = share  # store for alert formatting
        for bracket in SCORING["price_brackets_per_person"]:
            if share <= bracket["max"]:
                score += bracket["points"]
                break

    # ── Property type scoring ──────────────────────────────────────
    for prop_type, points in SCORING["property_type_points"].items():
        if prop_type in text:
            score += points
            break  # Count only the best match

    # ── Area scoring ───────────────────────────────────────────────
    best_area_score = 0
    for area, points in SCORING["area_points"].items():
        if area in text:
            best_area_score = max(best_area_score, points)
    score += best_area_score

    # ── Positive keyword scoring ───────────────────────────────────
    keyword_points = 0
    for keyword, points in SCORING["positive_keywords"].items():
        if keyword in text:
            keyword_points += points
    score += min(keyword_points, SCORING["max_positive_keyword_points"])

    # ── Negative keyword scoring ───────────────────────────────────
    for keyword, penalty in SCORING["negative_keywords"].items():
        if keyword in text:
            score += penalty  # penalty is already negative

    return max(0, score)


def should_alert(listing: dict) -> bool:
    """
    Returns True if the listing should trigger a Telegram alert.

    Hard rules (must pass ALL):
      1. Score >= alert_threshold
      2. Listing text must explicitly mention JP Nagar or Jayanagar
         (no area mention = hard reject, regardless of score)
      3. Jayanagar listings require a higher score than JP Nagar ones
    """
    score = listing.get("score", 0)
    if score < SCORING["alert_threshold"]:
        return False

    text = " ".join([
        listing.get("title", ""),
        listing.get("description", ""),
        listing.get("location", ""),
    ]).lower()

    # Hard reject: listing is in a far-away area (>3km from office)
    reject_areas = SEARCH.get("reject_areas", [])
    in_rejected = any(a.lower() in text for a in reject_areas)
    if in_rejected:
        return False

    # Check all area aliases from config
    in_primary = any(a.lower() in text for a in SEARCH["primary_area_aliases"])
    in_secondary = any(a.lower() in text for a in SEARCH["secondary_areas"])

    # Hard reject: not in any target area
    if not in_primary and not in_secondary:
        return False

    # Secondary area requires higher score
    if not in_primary and in_secondary:
        return score >= SEARCH.get("secondary_min_score", 55)

    return True


def format_score_breakdown(listing: dict) -> str:
    """Return a short human-readable explanation of the score."""
    lines = []
    text = " ".join([
        listing.get("title", ""),
        listing.get("description", ""),
        listing.get("location", ""),
    ]).lower()

    price = listing.get("price")
    bhk = listing.get("bhk") or detect_bhk(text)
    share = listing.get("price_share")

    if price:
        if bhk == "2bhk" and share and share != price:
            lines.append(f"Total ₹{price:,} → your share ~₹{share:,}/mo")
        else:
            lines.append(f"₹{price:,}/mo")

    for prop_type in SCORING["property_type_points"]:
        if prop_type in text:
            lines.append(prop_type.upper())
            break

    for area in SCORING["area_points"]:
        if area in text:
            lines.append(area.title())
            break

    good_tags = [kw for kw in SCORING["positive_keywords"] if kw in text]
    if good_tags:
        lines.append(" | ".join(t.title() for t in good_tags[:4]))

    return " · ".join(lines)
