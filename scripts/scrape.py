#!/usr/bin/env python3
"""
Fetches HDB listing data from the public API.

Two modes:
  - With HDB_COOKIE env var set (GitHub Actions): uses that cookie string
    directly, skipping the page visit. Needed because the Singapore IAM
    service geo-blocks non-SG IPs (e.g. GitHub's servers).
  - Without HDB_COOKIE (running locally from a SG IP): visits the page
    first so requests follows the IAM/QueueIT redirect chain and collects
    session cookies automatically.

To get the cookie value:
  1. Open Chrome → https://homes.hdb.gov.sg/home/finding-a-flat
  2. DevTools → Network → any getCoordinatesByFilters request
  3. Copy the full "cookie:" request header value
  4. Store it as a GitHub Actions secret named HDB_COOKIE
"""
import json
import os
import sys
import requests

PAGE_URL = "https://homes.hdb.gov.sg/home/finding-a-flat"
API_URL  = "https://homes.hdb.gov.sg/home-api/public/v1/map/getCoordinatesByFilters"

PAYLOAD = {
    "location": "", "coordinates": [["", ""]], "range": "2",
    "priceRangeLower": "0", "priceRangeUpper": "0", "flatType": "",
    "waitingTime": "", "modeOfSale": "", "floorRange": "",
    "remainingLeaseRangeLower": 1, "remainingLeaseRangeUpper": 99,
    "ethnicGroup": "", "citizenship": "", "extension": "", "contra": "",
    "rank": "Location, Price Range, Flat Type, Remaining Lease",
    "salesPerson": False, "town": "", "classification": "",
}

BROWSER_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "accept-language": "en-US,en;q=0.9",
}


def call_api(session, extra_headers=None):
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://homes.hdb.gov.sg",
        "referer": PAGE_URL,
        "skip-redirect": "1",
    }
    if extra_headers:
        headers.update(extra_headers)
    resp = session.post(API_URL, headers=headers, json=PAYLOAD, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def main():
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    hdb_cookie = os.environ.get("HDB_COOKIE", "").strip()

    if hdb_cookie:
        # ── GitHub Actions mode: use stored cookie ──────────────────────────
        print("HDB_COOKIE is set — using it directly (skipping page visit)")
        listings = call_api(session, extra_headers={"cookie": hdb_cookie})
    else:
        # ── Local mode: visit the page to collect cookies via redirects ─────
        print(f"No HDB_COOKIE set — visiting page to collect session cookies")
        print(f"GET {PAGE_URL}")
        page_resp = session.get(PAGE_URL, timeout=60)
        print(f"  → {page_resp.status_code} (final URL: {page_resp.url})")
        print(f"  → cookies collected: {list(session.cookies.keys())}")

        if "iam.hdb.gov.sg" in page_resp.url and page_resp.status_code == 403:
            print(
                "\nERROR: Blocked by Singapore IAM service (403).\n"
                "This usually means the script is running from a non-Singapore IP.\n\n"
                "Fix: add your browser cookie as a GitHub Actions secret:\n"
                "  1. Open https://homes.hdb.gov.sg/home/finding-a-flat in Chrome\n"
                "  2. DevTools → Network → any getCoordinatesByFilters request\n"
                "  3. Copy the full 'cookie:' header value\n"
                "  4. Repo Settings → Secrets → Actions → New secret\n"
                "     Name: HDB_COOKIE  Value: <paste cookie string>\n"
            )
            sys.exit(1)

        if "queue-it.net" in page_resp.url:
            print("ERROR: Stuck in QueueIT waiting room — try again later.")
            sys.exit(1)

        listings = call_api(session)

    print(f"  → {len(listings)} listings")

    if not listings:
        print("ERROR: API returned 0 listings. Cookie may be expired.")
        sys.exit(1)

    out = "data/hdb.json"
    with open(out, "w") as f:
        json.dump(listings, f)
    print(f"Saved {len(listings)} listings → {out}")


if __name__ == "__main__":
    main()
