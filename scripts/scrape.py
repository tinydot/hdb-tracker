#!/usr/bin/env python3
"""
Fetches HDB listing data by launching a headless browser, visiting the
finding-a-flat page, and intercepting the API response the page makes
itself. This handles QueueIT, IAM redirects, and JS-set cookies
automatically — no manual cookie management needed.

Requirements:
  pip install playwright
  playwright install chromium

Usage:
  python3 scripts/scrape.py

Automate with Windows Task Scheduler (run daily):
  Action: python3 C:\path\to\hdb-tracker\scripts\scrape.py
"""
import asyncio
import json
import os
import subprocess
import sys

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: Playwright is not installed.\nRun: pip install playwright && playwright install chromium")
    sys.exit(1)

PAGE_URL = "https://homes.hdb.gov.sg/home/finding-a-flat"
API_PATH = "getCoordinatesByFilters"
OUT_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "hdb.json")


async def scrape():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        captured = []
        done = asyncio.Event()

        async def on_response(response):
            if API_PATH in response.url:
                try:
                    data = await response.json()
                    listings = data if isinstance(data, list) else []
                    if listings:
                        captured.extend(listings)
                        print(f"  Captured {len(listings)} listings from API")
                        done.set()
                except Exception as e:
                    print(f"  Warning: could not parse API response: {e}")

        page.on("response", on_response)

        print(f"Launching browser → {PAGE_URL}")
        try:
            await page.goto(PAGE_URL, timeout=120_000, wait_until="domcontentloaded")
        except PWTimeout:
            print("ERROR: Timed out loading the page (QueueIT may have a long wait).")
            await browser.close()
            sys.exit(1)

        # Check if we got stuck in the QueueIT waiting room
        if "queue-it.net" in page.url:
            print("ERROR: Stuck in QueueIT waiting room — the site is under load. Try again later.")
            await browser.close()
            sys.exit(1)

        # Wait up to 30s for the API call to be intercepted
        print("Waiting for API call…")
        try:
            await asyncio.wait_for(done.wait(), timeout=30)
        except asyncio.TimeoutError:
            print(
                "ERROR: Page loaded but no API call was intercepted within 30s.\n"
                "The page layout may have changed or the map didn't initialise."
            )
            await browser.close()
            sys.exit(1)

        await browser.close()

    return captured


def git_push(out_file):
    repo = os.path.dirname(os.path.abspath(out_file))
    # Walk up to find repo root (where .git lives)
    path = os.path.abspath(out_file)
    while path != os.path.dirname(path):
        if os.path.isdir(os.path.join(path, ".git")):
            repo = path
            break
        path = os.path.dirname(path)

    def run(cmd):
        result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  git error: {result.stderr.strip()}")
        else:
            print(f"  {result.stdout.strip() or cmd[-1]}")
        return result.returncode

    print("Committing and pushing…")
    run(["git", "add", "data/hdb.json"])
    rc = run(["git", "commit", "-m", f"chore: update HDB listings data"])
    if rc == 0:
        run(["git", "push"])
    else:
        print("  Nothing to commit (data unchanged)")


def main():
    listings = asyncio.run(scrape())

    if not listings:
        print("ERROR: No listings captured.")
        sys.exit(1)

    out = os.path.abspath(OUT_FILE)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(listings, f)
    print(f"Saved {len(listings)} listings → {out}")

    git_push(out)


if __name__ == "__main__":
    main()
