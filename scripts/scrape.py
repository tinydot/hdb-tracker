#!/usr/bin/env python3
"""
Fetches HDB listing data from the public API.

Flow:
  1. GET the finding-a-flat page so requests follows any QueueIT redirects
     and collects session cookies automatically.
  2. POST to the API with those cookies.
  3. Validate and save to data/hdb.json.
"""
import json
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


def call_api(session):
    resp = session.post(
        API_URL,
        headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://homes.hdb.gov.sg",
            "referer": PAGE_URL,
            "skip-redirect": "1",
        },
        json=PAYLOAD,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def main():
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    # Visit the page first — requests follows QueueIT redirects automatically
    # and stores all resulting cookies in the session.
    print(f"GET {PAGE_URL}")
    page_resp = session.get(PAGE_URL, timeout=60)
    print(f"  → {page_resp.status_code} (final URL: {page_resp.url})")
    print(f"  → cookies: {list(session.cookies.keys())}")

    if "queue-it.net" in page_resp.url:
        print(
            "ERROR: Landed on QueueIT waiting room — queue is not empty.\n"
            "The site is under load. Try running the workflow again later."
        )
        sys.exit(1)

    print(f"POST {API_URL}")
    listings = call_api(session)
    print(f"  → {len(listings)} listings")

    if not listings:
        print(
            "ERROR: API returned 0 listings.\n"
            "The session may not have passed QueueIT validation.\n"
            "Response cookies: " + str(list(session.cookies.keys()))
        )
        sys.exit(1)

    out = "data/hdb.json"
    with open(out, "w") as f:
        json.dump(listings, f)
    print(f"Saved {len(listings)} listings → {out}")


if __name__ == "__main__":
    main()
