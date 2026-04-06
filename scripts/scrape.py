#!/usr/bin/env python3
"""
Fetches HDB listing data without a headless browser or stored secrets.

Attempts in order:
  1. Direct POST with generated HH02 only — no page visit.
     Works from any IP if the API doesn't require the page-visit cookies.
     This is tried first so GitHub Actions cron can run without geo issues.
  2. Page visit first, then POST with generated HH02.
     Collects server-set cookies (requires a Singapore IP).
  3. Stored cookie fallback (HDB_COOKIE env var or data/.cookie).
     Manual refresh required when it expires.

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

PAGE_URL    = "https://homes.hdb.gov.sg/home/finding-a-flat"
API_URL     = "https://homes.hdb.gov.sg/home-api/public/v1/map/getCoordinatesByFilters"
OUT_FILE    = os.path.join(os.path.dirname(__file__), "..", "data", "hdb.json")
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

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": BROWSER_UA,
}

API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": "https://homes.hdb.gov.sg",
    "referer": PAGE_URL,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "skip-redirect": "1",
    "user-agent": BROWSER_UA,
}


def generate_hh02():
    ts   = int(time.time() * 1000)
    rand = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    return f"LO{ts}{rand}"


def post_api(session, hh02, extra_cookie=None):
    headers = {**API_HEADERS, "sessionid": hh02}
    if extra_cookie:
        headers["cookie"] = extra_cookie
    return session.post(API_URL, headers=headers, json=PAYLOAD, timeout=30)


def parse_listings(resp):
    data = resp.json()
    return data if isinstance(data, list) else []


# ── Attempt 1: direct POST, no page visit ─────────────────────────────────────
def attempt_direct():
    print("Attempt 1: direct POST (no page visit)")
    session = requests.Session()
    hh02 = generate_hh02()
    session.cookies.set("HH02", hh02, domain="homes.hdb.gov.sg")
    print(f"  generated HH02: {hh02[:24]}…")
    resp = post_api(session, hh02)
    print(f"  → {resp.status_code}")
    if resp.status_code == 200:
        return parse_listings(resp)
    return None


# ── Attempt 2: visit page first (requires SG IP) ──────────────────────────────
def attempt_via_page():
    print("Attempt 2: page visit + generated HH02 (requires SG IP)")
    session = requests.Session()
    resp = session.get(PAGE_URL, headers=PAGE_HEADERS, timeout=60)
    print(f"  GET {PAGE_URL} → {resp.status_code} (final: {resp.url})")
    print(f"  cookies: {list(session.cookies.keys())}")

    if "iam.hdb.gov.sg" in resp.url and resp.status_code >= 400:
        print("  → blocked by IAM (non-SG IP)")
        return None
    if "queue-it.net" in resp.url:
        print("  → stuck in QueueIT")
        return None

    hh02 = generate_hh02()
    session.cookies.set("HH02", hh02, domain="homes.hdb.gov.sg")
    print(f"  generated HH02: {hh02[:24]}…")
    resp = post_api(session, hh02)
    print(f"  → {resp.status_code}")
    if resp.status_code == 200:
        return parse_listings(resp)
    return None


# ── Attempt 3: stored browser cookie ──────────────────────────────────────────
def attempt_stored_cookie():
    cookie_str = os.environ.get("HDB_COOKIE", "").strip()
    if not cookie_str:
        path = os.path.abspath(COOKIE_FILE)
        if os.path.exists(path):
            cookie_str = open(path).read().strip()
    if not cookie_str:
        return None

    print("Attempt 3: stored cookie (HDB_COOKIE / data/.cookie)")
    m = re.search(r'(?:^|;\s*)HH02=([^\s;]+)', cookie_str)
    hh02 = m.group(1) if m else generate_hh02()
    print(f"  sessionid: {hh02[:24]}…")
    session = requests.Session()
    resp = post_api(session, hh02, extra_cookie=cookie_str)
    print(f"  → {resp.status_code}")
    if resp.status_code == 200:
        return parse_listings(resp)
    print(
        "  Cookie may be expired. Refresh it:\n"
        "  1. Open https://homes.hdb.gov.sg/home/finding-a-flat in Chrome\n"
        "  2. DevTools → Network → getCoordinatesByFilters → copy 'cookie:' header\n"
        "  3. Paste into data/.cookie  or  HDB_COOKIE env var"
    )
    return None


# ── Git push ───────────────────────────────────────────────────────────────────
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
    if run(["git", "commit", "-m", "chore: update HDB listings data"]) == 0:
        run(["git", "push"])
    else:
        print("  Nothing new to commit")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    listings = (
        attempt_direct()
        or attempt_via_page()
        or attempt_stored_cookie()
    )

    if not listings:
        print("\nERROR: All attempts failed. No listings retrieved.")
        sys.exit(1)

    out = os.path.abspath(OUT_FILE)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(listings, f)
    print(f"\nSaved {len(listings)} listings → {out}")
    git_push()


if __name__ == "__main__":
    main()
