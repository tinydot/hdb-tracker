#!/usr/bin/env python3
"""
Fetches HDB listing data without a headless browser.

Strategy:
  1. GET the finding-a-flat page with full browser-like headers so
     requests follows the QueueIT safetynet redirect chain (pure HTTP)
     and collects the QueueITAccepted cookie server-side.
  2. Generate the HH02 session token locally — it follows the pattern
     "LO" + Unix-ms-timestamp + 16 random alphanumeric chars, matching
     what the HDB frontend JavaScript produces.
  3. POST to the API with all collected + generated cookies and the
     required sessionid header.

If the server validates HH02 against stored state (which would mean
it's not purely client-generated), this will return 400 and the
fallback is to use the HDB_COOKIE env var or data/.cookie file.

Requirements:  pip install requests
"""
import json
import os
import random
import re
import string
import subprocess
import sys
import time

import requests

PAGE_URL = "https://homes.hdb.gov.sg/home/finding-a-flat"
API_URL  = "https://homes.hdb.gov.sg/home-api/public/v1/map/getCoordinatesByFilters"
OUT_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "hdb.json")
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

# Full browser-like headers — needed for QueueIT and IAM to not reject the request
BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "accept-encoding": "gzip, deflate, br",
    "cache-control": "max-age=0",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


def generate_hh02():
    """Generate HH02 session token matching the browser JS pattern: LO + ms_timestamp + 16 random chars."""
    ts = int(time.time() * 1000)
    rand = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    return f"LO{ts}{rand}"


def load_fallback_cookie():
    """Load full cookie string from env var or local file (fallback if generation fails)."""
    cookie = os.environ.get("HDB_COOKIE", "").strip()
    if cookie:
        return cookie
    path = os.path.abspath(COOKIE_FILE)
    if os.path.exists(path):
        cookie = open(path).read().strip()
        if cookie:
            return cookie
    return None


def extract_hh02(cookie_str):
    m = re.search(r'(?:^|;\s*)HH02=([^\s;]+)', cookie_str)
    return m.group(1) if m else None


def call_api(session, hh02):
    resp = session.post(
        API_URL,
        headers={
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://homes.hdb.gov.sg",
            "referer": PAGE_URL,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sessionid": hh02,
            "skip-redirect": "1",
            "user-agent": BROWSER_HEADERS["user-agent"],
        },
        json=PAYLOAD,
        timeout=30,
    )
    return resp


def try_with_generated_cookie(session):
    """Attempt 1: visit page for server cookies, generate HH02 locally."""
    print(f"GET {PAGE_URL}")
    resp = session.get(PAGE_URL, timeout=60, headers=BROWSER_HEADERS)
    print(f"  → {resp.status_code} (final: {resp.url})")
    print(f"  → server cookies: {list(session.cookies.keys())}")

    if "iam.hdb.gov.sg" in resp.url and resp.status_code >= 400:
        print("  → blocked by IAM (non-SG IP or bot detection)")
        return None

    if "queue-it.net" in resp.url:
        print("  → stuck in QueueIT waiting room")
        return None

    hh02 = generate_hh02()
    print(f"  → generated HH02: {hh02[:20]}…")
    session.cookies.set("HH02", hh02, domain="homes.hdb.gov.sg")

    print(f"POST {API_URL}")
    api_resp = call_api(session, hh02)
    print(f"  → {api_resp.status_code}")
    return api_resp


def try_with_fallback_cookie(session):
    """Attempt 2: use stored browser cookie (HDB_COOKIE env var or data/.cookie)."""
    cookie_str = load_fallback_cookie()
    if not cookie_str:
        return None, None

    print("Using fallback cookie (HDB_COOKIE / data/.cookie)")
    hh02 = extract_hh02(cookie_str)
    if hh02:
        print(f"  → sessionid from HH02: {hh02[:20]}…")
    else:
        print("  → Warning: HH02 not found in cookie string")
        hh02 = generate_hh02()

    # Clear any previously collected cookies and use the full string directly
    session.cookies.clear()
    print(f"POST {API_URL}")
    api_resp = session.post(
        API_URL,
        headers={
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "cookie": cookie_str,
            "origin": "https://homes.hdb.gov.sg",
            "referer": PAGE_URL,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sessionid": hh02,
            "skip-redirect": "1",
            "user-agent": BROWSER_HEADERS["user-agent"],
        },
        json=PAYLOAD,
        timeout=30,
    )
    print(f"  → {api_resp.status_code}")
    return api_resp, cookie_str


def git_push():
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    def run(cmd):
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        if out:
            print(f"  {out}")
        return r.returncode

    print("Committing and pushing…")
    run(["git", "add", "data/hdb.json"])
    rc = run(["git", "commit", "-m", "chore: update HDB listings data"])
    if rc == 0:
        run(["git", "push"])
    else:
        print("  Nothing new to commit")


def main():
    session = requests.Session()
    listings = []

    # Attempt 1: generate HH02 ourselves (no stored cookie needed)
    api_resp = try_with_generated_cookie(session)
    if api_resp is not None and api_resp.status_code == 200:
        data = api_resp.json()
        listings = data if isinstance(data, list) else []

    # Attempt 2: fall back to stored browser cookie
    if not listings:
        if api_resp is not None:
            print(f"\nGenerated HH02 didn't work (status {api_resp.status_code}).")
            print("Falling back to stored cookie…\n")
        api_resp, _ = try_with_fallback_cookie(session)
        if api_resp is not None and api_resp.status_code == 200:
            data = api_resp.json()
            listings = data if isinstance(data, list) else []
        elif api_resp is not None:
            print(
                f"\nERROR: Both attempts failed (status {api_resp.status_code}).\n"
                f"Cookie may be expired. Refresh it:\n"
                f"  1. Open https://homes.hdb.gov.sg/home/finding-a-flat in Chrome\n"
                f"  2. DevTools → Network → getCoordinatesByFilters → copy 'cookie:' header\n"
                f"  3. Paste into data/.cookie  or  HDB_COOKIE env var\n"
            )
            sys.exit(1)
        else:
            print(
                "\nERROR: Could not reach the API and no fallback cookie is set.\n"
                "Are you on a Singapore IP?\n"
            )
            sys.exit(1)

    if not listings:
        print("ERROR: API returned 0 listings.")
        sys.exit(1)

    out = os.path.abspath(OUT_FILE)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(listings, f)
    print(f"\nSaved {len(listings)} listings → {out}")

    git_push()


if __name__ == "__main__":
    main()
