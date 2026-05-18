#!/usr/bin/env python3
"""
Fetches HDB listing data.  Attempts in order:

  1. Direct POST — no page visit, no cookies.
     Works if the API doesn't enforce XSRF.
  2. Self-generated XSRF token — Angular uses the double-submit cookie
     pattern: the server only checks that the X-XSRF-TOKEN header equals
     the XSRF-TOKEN cookie, both values supplied by the client.  We send a
     random UUID as both.  Works without a browser or SG IP if the API
     trusts the pattern without a server-side secret.
  3. Page visit via requests — captures any server-set cookies (Set-Cookie).
     The XSRF-TOKEN is normally set by JS, so cookies will be empty, but
     kept here in case HDB ever reverts to a server-set cookie.
  4. Stored cookie (HDB_COOKIE env var or data/.cookie).
     Manual refresh required when it expires.

Requirements:  pip install requests
"""
import json
import os
import re
import subprocess
import sys
import uuid

import requests

PAGE_URL    = "https://homes.hdb.gov.sg/"
API_URL     = "https://api.homes.hdb.gov.sg/flatback/public/v1/map/getCoordinatesByFilters"
OUT_FILE    = os.path.join(os.path.dirname(__file__), "..", "data", "hdb.json")
COOKIE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", ".cookie")

PAYLOAD_BASE = {
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

# The new API returns minimal records (id + region + stage) when modeOfSale is
# empty. To get the rich payload (price, address, flatType, photo, area,
# lease, …) we have to make one call per mode. "Resale" is confirmed. "BTO"
# is a best-effort guess; if it doesn't return anything we still capture BTO
# entries (minimal shape) from the no-mode call below.
RICH_MODES = ["Resale", "BTO"]

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
#   {"coords": "...", "props": {"type": "Resale", "region": "...", "addr": "...",
#                               "hdbCat": "0",
#                               "desc": [{"id": "40105", "price": "...",
#                                         "type": "4-Room", "area": "84.0 sqm",
#                                         "createDt": "...", "dist": "-1",
#                                         "maxType": "04", "maxLease": "60.0",
#                                         "maxPrice": "...", "photo": "..."}]}}
# We rewrite to the legacy shape index.html / scrape_photos.py read:
#   {"coordinates": "...", "properties": {"listingType": "...", "region": "...",
#                                          "address": "...", "hdbCategory": "0",
#                                          "description": [{"listingId": "40105",
#                                              "price": "...", "flatType": "...",
#                                              "floorArea": "...", ...}]}}
PROPS_KEY_MAP = {
    "type": "listingType",
    "addr": "address",
    "hdbCat": "hdbCategory",
    "category": "hdbCategory",
}
DESC_KEY_MAP = {
    "type": "flatType",
    "area": "floorArea",
    "createDt": "creationDate",
    "dist": "distance",
    "maxType": "maxFlatType",
    "maxLease": "maxRemainingLease",
    "bltQr": "ballotQtr",
    "lStartDt": "launchStartDate",
    "class": "classification",
}


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
        d2 = {}
        for k, v in d.items():
            d2.setdefault(DESC_KEY_MAP.get(k, k), v)
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
    # Preserve any other top-level props fields the legacy shape carried at
    # properties top level (address, hdbCategory, …); remap known short keys.
    for k, v in props_in.items():
        if k in ("type", "region", "desc", "description"):
            continue
        props_out.setdefault(PROPS_KEY_MAP.get(k, k), v)

    out = {"coordinates": coords, "properties": props_out}
    # Carry through any sibling keys the API tacks onto the item
    # (e.g. resaleMaxCount on the first record in the legacy response).
    for k, v in item.items():
        if k in ("coords", "coordinates", "props", "properties"):
            continue
        out.setdefault(k, v)
    return out


def _listing_id(item):
    desc = (item.get("properties") or {}).get("description") or []
    if not desc:
        return None
    d = desc[0]
    return d.get("listingId") or d.get("projectId")


def parse_listings(resp):
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [normalize_item(x) for x in data]


def post_api(session, mode="", extra_cookie=None, xsrf=None):
    headers = dict(API_HEADERS)
    if xsrf:
        headers["x-xsrf-token"] = xsrf
    if extra_cookie:
        headers["cookie"] = extra_cookie
    payload = {**PAYLOAD_BASE, "modeOfSale": mode}
    return session.post(API_URL, headers=headers, json=payload, timeout=30)


def fetch_all(session, extra_cookie=None, xsrf=None):
    """Issue one call per mode in RICH_MODES to collect rich-shape items, plus
    a no-mode call to pick up any listing types we didn't request explicitly.
    Returns a merged list (legacy shape) or None if every call failed."""
    by_id = {}            # listing/project id → normalized item (rich wins)
    rich_ids = set()      # ids we already got rich data for
    fallback = []         # items without a usable id (kept verbatim)
    any_ok = False

    def ingest(items, rich):
        for item in items:
            lid = _listing_id(item)
            if not lid:
                fallback.append(item)
                continue
            if rich:
                by_id[lid] = item
                rich_ids.add(lid)
            elif lid not in rich_ids:
                by_id.setdefault(lid, item)

    for mode in RICH_MODES:
        resp = post_api(session, mode=mode, extra_cookie=extra_cookie, xsrf=xsrf)
        print(f"  mode={mode!r:>10} → {resp.status_code}", end="")
        if resp.status_code != 200:
            print()
            continue
        items = parse_listings(resp)
        any_ok = True
        print(f"  ({len(items)} items)")
        ingest(items, rich=True)

    resp = post_api(session, mode="", extra_cookie=extra_cookie, xsrf=xsrf)
    print(f"  mode=''         → {resp.status_code}", end="")
    if resp.status_code == 200:
        items = parse_listings(resp)
        any_ok = True
        print(f"  ({len(items)} items)")
        ingest(items, rich=False)
    else:
        print()

    if not any_ok:
        return None
    return list(by_id.values()) + fallback


def _summarize(listings):
    types = {}
    for x in listings:
        t = (x.get("properties") or {}).get("listingType") or "Unknown"
        types[t] = types.get(t, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(types.items()))


def _finish(listings):
    if not listings:
        return None
    print(f"  → {len(listings)} merged items ({_summarize(listings)})")
    return listings


# ── Attempt 1: direct POST, no page visit ─────────────────────────────────────
def attempt_direct():
    print("Attempt 1: direct POST (no page visit)")
    session = requests.Session()
    return _finish(fetch_all(session))


# ── Attempt 2: self-generated XSRF token ─────────────────────────────────────
def attempt_self_xsrf():
    """Angular double-submit pattern: cookie value must equal header value.
    The server doesn't tie the token to a server-side secret, so we can
    supply our own UUID as both the XSRF-TOKEN cookie and X-XSRF-TOKEN header."""
    print("Attempt 2: self-generated XSRF token (double-submit pattern)")
    token = str(uuid.uuid4())
    print(f"  XSRF-TOKEN: {token[:12]}…")
    session = requests.Session()
    cookie_header = f"XSRF-TOKEN={token}"
    return _finish(fetch_all(session, extra_cookie=cookie_header, xsrf=token))


# ── Attempt 3: visit page via requests (no JS) ────────────────────────────────
def attempt_via_page():
    print("Attempt 3: page visit via requests + XSRF-TOKEN (requires SG IP)")
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

    return _finish(fetch_all(session, xsrf=xsrf))


# ── Attempt 3: stored browser cookie ──────────────────────────────────────────
def attempt_stored_cookie():
    cookie_str = os.environ.get("HDB_COOKIE", "").strip()
    if not cookie_str:
        path = os.path.abspath(COOKIE_FILE)
        if os.path.exists(path):
            cookie_str = open(path).read().strip()
    if not cookie_str:
        return None

    print("Attempt 4: stored cookie (HDB_COOKIE / data/.cookie)")
    m = re.search(r'(?:^|;\s*)XSRF-TOKEN=([^\s;]+)', cookie_str)
    xsrf = m.group(1) if m else None
    if xsrf:
        print(f"  XSRF-TOKEN: {xsrf[:12]}…")
    session = requests.Session()
    listings = fetch_all(session, extra_cookie=cookie_str, xsrf=xsrf)
    out = _finish(listings)
    if out:
        return out
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
        or attempt_self_xsrf()
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
