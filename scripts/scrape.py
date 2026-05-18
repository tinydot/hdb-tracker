#!/usr/bin/env python3
"""
Fetches HDB listing data without a headless browser or stored secrets.

The API moved to api.homes.hdb.gov.sg/flatback/... and the response shape
shrank (coords/props/desc/type/id). We request fullResult=true and
normalize the response back to the legacy {coordinates, properties{...}}
shape that index.html and scrape_photos.py consume.

Attempts in order:
  1. Direct POST — no page visit, no cookies.
     Works from any IP if the API doesn't enforce XSRF.
     Tried first so GitHub Actions cron can run without geo issues.
  2. Page visit first to collect XSRF-TOKEN, then POST with it.
     Requires a Singapore IP (the page is IAM-gated outside SG).
  3. Stored cookie fallback (HDB_COOKIE env var or data/.cookie).
     Manual refresh required when it expires.

Requirements:  pip install requests
"""
import json
import os
import re
import subprocess
import sys

import requests

PAGE_URL    = "https://homes.hdb.gov.sg/"
API_URL     = "https://api.homes.hdb.gov.sg/flatback/public/v1/map/getCoordinatesByFilters"
OUT_FILE    = os.path.join(os.path.dirname(__file__), "..", "data", "hdb.json")
COOKIE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", ".cookie")

PAYLOAD = {
    "town": "", "location": "", "range": "2", "classification": "",
    "priceRangeLower": "0", "priceRangeUpper": "0", "flatType": "",
    "waitingTime": "", "modeOfSale": "",
    "remainingLeaseRangeLower": 1, "remainingLeaseRangeUpper": 99,
    "salesPerson": False, "floorRange": "",
    "ethnicGroup": "", "citizenship": "", "extension": "", "contra": "",
    "rank": "Location, Price Range, Flat Type, Remaining Lease",
    "coordinates": [["", ""]],
    "fullResult": True,
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
    "referer": "https://homes.hdb.gov.sg/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": BROWSER_UA,
}


# ── Response normalization ─────────────────────────────────────────────────────
# The new API returns short-key items like:
#   {"coords": "...", "props": {"type": "Resale", "region": "...",
#                               "desc": [{"id": "40105", ...}]}}
# We rewrite to the legacy shape:
#   {"coordinates": "...", "properties": {"listingType": "...", "region": "...",
#                                          "description": [{"listingId": "40105", ...}]}}
def normalize_item(item):
    if not isinstance(item, dict):
        return item
    # Already in the legacy shape — pass through untouched.
    if "properties" in item and "coordinates" in item:
        return item

    coords = item.get("coords") or item.get("coordinates")
    props_in = item.get("props") or item.get("properties") or {}
    listing_type = props_in.get("type") or props_in.get("listingType")

    desc_out = []
    for d in props_in.get("desc") or props_in.get("description") or []:
        if not isinstance(d, dict):
            continue
        d2 = dict(d)
        if "id" in d2:
            id_val = d2.pop("id")
            # Resale items used listingId; BTO/other used projectId.
            if listing_type == "Resale":
                d2.setdefault("listingId", id_val)
            else:
                d2.setdefault("projectId", id_val)
        desc_out.append(d2)

    props_out = {
        "listingType": listing_type,
        "region": props_in.get("region"),
        "description": desc_out,
    }
    # Preserve any other top-level props fields (address, hdbCategory, etc.)
    # that the legacy shape carried at properties top level.
    for k, v in props_in.items():
        if k in ("type", "region", "desc", "description"):
            continue
        # Map a couple of likely renames; keep unknown keys as-is.
        if k == "addr":
            props_out.setdefault("address", v)
        elif k == "category":
            props_out.setdefault("hdbCategory", v)
        else:
            props_out.setdefault(k, v)

    out = {"coordinates": coords, "properties": props_out}
    # Carry through any sibling keys the API tacks onto the item
    # (e.g. resaleMaxCount on the first record in the legacy response).
    for k, v in item.items():
        if k in ("coords", "coordinates", "props", "properties"):
            continue
        out.setdefault(k, v)
    return out


def parse_listings(resp):
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [normalize_item(x) for x in data]


def post_api(session, extra_cookie=None, xsrf=None):
    headers = dict(API_HEADERS)
    if xsrf:
        headers["x-xsrf-token"] = xsrf
    if extra_cookie:
        headers["cookie"] = extra_cookie
    return session.post(API_URL, headers=headers, json=PAYLOAD, timeout=30)


def _summarize(listings):
    types = {}
    for x in listings:
        t = (x.get("properties") or {}).get("listingType") or "Unknown"
        types[t] = types.get(t, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(types.items()))


# ── Attempt 1: direct POST, no page visit ─────────────────────────────────────
def attempt_direct():
    print("Attempt 1: direct POST (no page visit)")
    session = requests.Session()
    resp = post_api(session)
    print(f"  → {resp.status_code}")
    if resp.status_code == 200:
        listings = parse_listings(resp)
        if listings:
            print(f"  parsed {len(listings)} items ({_summarize(listings)})")
            return listings
        print("  empty response — falling through")
    return None


# ── Attempt 2: visit page first (requires SG IP) ──────────────────────────────
def attempt_via_page():
    print("Attempt 2: page visit + XSRF-TOKEN (requires SG IP)")
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

    xsrf = session.cookies.get("XSRF-TOKEN")
    if xsrf:
        print(f"  XSRF-TOKEN: {xsrf[:12]}…")
    else:
        print("  no XSRF-TOKEN cookie — trying without it")

    resp = post_api(session, xsrf=xsrf)
    print(f"  → {resp.status_code}")
    if resp.status_code == 200:
        listings = parse_listings(resp)
        if listings:
            print(f"  parsed {len(listings)} items ({_summarize(listings)})")
            return listings
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
    m = re.search(r'(?:^|;\s*)XSRF-TOKEN=([^\s;]+)', cookie_str)
    xsrf = m.group(1) if m else None
    if xsrf:
        print(f"  XSRF-TOKEN: {xsrf[:12]}…")
    session = requests.Session()
    resp = post_api(session, extra_cookie=cookie_str, xsrf=xsrf)
    print(f"  → {resp.status_code}")
    if resp.status_code == 200:
        listings = parse_listings(resp)
        if listings:
            print(f"  parsed {len(listings)} items ({_summarize(listings)})")
            return listings
    print(
        "  Cookie may be expired. Refresh it:\n"
        "  1. Open https://homes.hdb.gov.sg/home/finding-a-flat in Chrome\n"
        "  2. DevTools → Network → getCoordinatesByFilters → copy 'cookie:' header\n"
        "  3. Paste into data/.cookie  or  HDB_COOKIE env var"
    )
    return None


# ── Git push ───────────────────────────────────────────────────────────────────
def git_push():
    # In GitHub Actions the workflow's "Commit if changed" step handles this
    if os.environ.get("CI"):
        print("CI environment detected — skipping git push (workflow handles it)")
        return
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
