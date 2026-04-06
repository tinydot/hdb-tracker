#!/usr/bin/env python3
"""
Fetches HDB listing data from the public API.

The HDB API requires a browser session cookie (including the QueueIT
acceptance cookie and HH02 session token set by JavaScript). A plain
HTTP GET to the page cannot collect these — they must come from a real
browser visit.

Cookie source (first match wins):
  1. HDB_COOKIE environment variable  (used by GitHub Actions)
  2. data/.cookie file                (convenient for local use)

To get the cookie string:
  1. Open Chrome → https://homes.hdb.gov.sg/home/finding-a-flat
  2. DevTools (F12) → Network tab → find any getCoordinatesByFilters request
  3. In the request headers, copy the full value of the "cookie:" header
  4. Paste it into data/.cookie  (local)  or  HDB_COOKIE secret  (GH Actions)
"""
import json
import os
import re
import sys
import requests

PAGE_URL = "https://homes.hdb.gov.sg/home/finding-a-flat"
API_URL  = "https://homes.hdb.gov.sg/home-api/public/v1/map/getCoordinatesByFilters"
COOKIE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", ".cookie")

PAYLOAD = {
    "location": "", "coordinates": [["", ""]], "range": "2",
    "priceRangeLower": "0", "priceRangeUpper": "0", "flatType": "",
    "waitingTime": "", "modeOfSale": "", "floorRange": "",
    "remainingLeaseRangeLower": 1, "remainingLeaseRangeUpper": 99,
    "ethnicGroup": "", "citizenship": "", "extension": "", "contra": "",
    "rank": "Location, Price Range, Flat Type, Remaining Lease",
    "salesPerson": False, "town": "", "classification": "",
}


def get_cookie():
    """Load cookie string from env var or local file."""
    cookie = os.environ.get("HDB_COOKIE", "").strip()
    if cookie:
        print("Using cookie from HDB_COOKIE environment variable")
        return cookie
    cookie_path = os.path.abspath(COOKIE_FILE)
    if os.path.exists(cookie_path):
        cookie = open(cookie_path).read().strip()
        if cookie:
            print(f"Using cookie from {cookie_path}")
            return cookie
    return None


def extract_session_id(cookie_str):
    """Extract HH02 value from cookie string to use as the sessionid header."""
    m = re.search(r'(?:^|;\s*)HH02=([^\s;]+)', cookie_str)
    return m.group(1) if m else None


def main():
    cookie = get_cookie()
    if not cookie:
        print(
            "ERROR: No cookie found.\n\n"
            "The HDB API requires a browser session cookie that includes\n"
            "the QueueIT token and HH02 session value (set by JavaScript).\n\n"
            "To fix:\n"
            "  1. Open https://homes.hdb.gov.sg/home/finding-a-flat in Chrome\n"
            "  2. DevTools → Network → any getCoordinatesByFilters request\n"
            "  3. Copy the full 'cookie:' request header value\n\n"
            "  Local use:  paste into  data/.cookie\n"
            "  GitHub Actions:  repo Settings → Secrets → Actions\n"
            "                   Name: HDB_COOKIE  Value: <paste>\n"
        )
        sys.exit(1)

    session_id = extract_session_id(cookie)
    if session_id:
        print(f"Extracted sessionid from HH02: {session_id[:20]}…")
    else:
        print("Warning: HH02 cookie not found — sessionid header will be omitted")

    session = requests.Session()
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "cookie": cookie,
        "origin": PAGE_URL.rsplit("/", 1)[0],
        "referer": PAGE_URL,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "skip-redirect": "1",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    if session_id:
        headers["sessionid"] = session_id

    print(f"POST {API_URL}")
    resp = session.post(API_URL, headers=headers, json=PAYLOAD, timeout=30)

    if resp.status_code == 400:
        print(
            f"ERROR: 400 Bad Request — cookie is likely expired.\n"
            f"Refresh it by repeating the steps above and updating\n"
            f"data/.cookie or the HDB_COOKIE secret."
        )
        sys.exit(1)

    resp.raise_for_status()
    data = resp.json()
    listings = data if isinstance(data, list) else []
    print(f"  → {len(listings)} listings")

    if not listings:
        print("ERROR: API returned 0 listings. Cookie may be expired.")
        sys.exit(1)

    out = os.path.join(os.path.dirname(__file__), "..", "data", "hdb.json")
    with open(out, "w") as f:
        json.dump(listings, f)
    print(f"Saved {len(listings)} listings → {os.path.abspath(out)}")


if __name__ == "__main__":
    main()
