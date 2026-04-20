#!/usr/bin/env python3
"""
Scrapes house photos for 4-room HDB resale listings from homes.hdb.gov.sg.

For each listing, navigates to its detail page and extracts all gallery images
from the ngx-gallery thumbnail strip.

Requirements:  pip install playwright && playwright install chromium
Usage:         python scrape_photos.py [--limit N] [--delay SECS]
"""
import argparse
import json
import os
import re
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

DATA_FILE   = os.path.join(os.path.dirname(__file__), "..", "data", "hdb.json")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "photos.json")
LISTING_URL = "https://homes.hdb.gov.sg/home/resale/{listing_id}"
FLAT_TYPE   = "4-Room"


def load_four_room_listings():
    with open(os.path.abspath(DATA_FILE)) as f:
        data = json.load(f)
    listings = []
    for item in data:
        props = item.get("properties", {})
        if props.get("listingType") != "Resale":
            continue
        for desc in props.get("description", []):
            if desc.get("flatType", "").startswith(FLAT_TYPE):
                listings.append({
                    "listingId": desc["listingId"],
                    "address":   props.get("address", ""),
                    "price":     desc.get("price", ""),
                    "flatType":  desc.get("flatType", ""),
                })
    return listings


def extract_photos(page, listing_id):
    url = LISTING_URL.format(listing_id=listing_id)
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if resp and resp.status >= 400:
            print(f"  HTTP {resp.status}")
            return []
        # Wait for Angular to render the gallery thumbnails
        page.wait_for_selector("div.ngx-gallery-thumbnail", timeout=20_000)
    except PlaywrightTimeout:
        print(f"  timeout — no gallery rendered")
        return []
    except Exception as exc:
        print(f"  error: {exc}")
        return []

    thumbnails = page.query_selector_all("div.ngx-gallery-thumbnail")
    photos = []
    for thumb in thumbnails:
        style = thumb.get_attribute("style") or ""
        # style contains: background-image: url("https://...")
        m = re.search(r'background-image:\s*url\(["\']?([^"\')\s]+)["\']?\)', style)
        if m:
            url = m.group(1)
            if url not in photos:
                photos.append(url)
    return photos


def main():
    parser = argparse.ArgumentParser(description="Scrape photos for 4-room HDB listings")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N listings (0 = all)")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Seconds between requests (default: 1.5)")
    args = parser.parse_args()

    listings = load_four_room_listings()
    print(f"Found {len(listings)} 4-room resale listings")

    out_path = os.path.abspath(OUTPUT_FILE)
    if os.path.exists(out_path):
        with open(out_path) as f:
            results = json.load(f)
        print(f"Resuming: {len(results)} listings already scraped")
    else:
        results = {}

    done      = set(results.keys())
    remaining = [l for l in listings if l["listingId"] not in done]
    if args.limit:
        remaining = remaining[: args.limit]
    print(f"To scrape: {len(remaining)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        for i, listing in enumerate(remaining, 1):
            lid = listing["listingId"]
            print(f"[{i}/{len(remaining)}] {lid} — {listing['address']}", end=" ", flush=True)

            photos = extract_photos(page, lid)
            print(f"→ {len(photos)} photo(s)")

            results[lid] = {
                "address":  listing["address"],
                "price":    listing["price"],
                "flatType": listing["flatType"],
                "photos":   photos,
            }

            # Persist after every listing so progress isn't lost
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w") as f:
                json.dump(results, f, indent=2)

            if i < len(remaining):
                time.sleep(args.delay)

        browser.close()

    total_photos = sum(len(v["photos"]) for v in results.values())
    print(f"\nDone. {len(results)} listings, {total_photos} photos → {out_path}")


if __name__ == "__main__":
    main()
