"""
db.py - SQLite storage layer
Stores seen listings to avoid duplicate Telegram alerts.
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "rentals.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id          TEXT PRIMARY KEY,
            title       TEXT,
            price       INTEGER,
            location    TEXT,
            description TEXT,
            url         TEXT,
            image_url   TEXT,
            score       INTEGER,
            alerted     INTEGER DEFAULT 0,
            seen_at     TEXT
        )
    """)
    conn.commit()
    conn.close()


def is_seen(listing_id: str) -> bool:
    """Return True if we've already stored this listing."""
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM listings WHERE id = ?", (listing_id,)
    ).fetchone()
    conn.close()
    return row is not None


def save_listing(listing: dict):
    """Insert a new listing. Ignore if already exists."""
    conn = get_connection()
    conn.execute("""
        INSERT OR IGNORE INTO listings
            (id, title, price, location, description, url, image_url, score, alerted, seen_at)
        VALUES
            (:id, :title, :price, :location, :description, :url, :image_url, :score, 0, :seen_at)
    """, {**listing, "seen_at": datetime.now().isoformat()})
    conn.commit()
    conn.close()


def mark_alerted(listing_id: str):
    """Mark that we've sent a Telegram alert for this listing."""
    conn = get_connection()
    conn.execute(
        "UPDATE listings SET alerted = 1 WHERE id = ?", (listing_id,)
    )
    conn.commit()
    conn.close()


def get_recent_listings(limit: int = 20) -> list:
    """Fetch recently seen listings, for debugging."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM listings ORDER BY seen_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
