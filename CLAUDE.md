# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Two loosely-coupled halves that share `data/`:

1. **Listings tracker** — `scripts/scrape.py` fetches HDB resale/BTO listings from `api.homes.hdb.gov.sg` daily (GitHub Actions, `.github/workflows/scrape.yml`) and commits the result to `data/hdb.json`. `index.html` is a single-file Leaflet UI that reads `data/hdb.json` directly. Deployed to GitHub Pages via `.github/workflows/static.yml` (uploads the entire repo on push to `main`).
2. **Photo labeler** — `scripts/scrape_photos.py` downloads listing photos to `photos/<listing_id>/`; `scripts/label_photos.py` runs them through a local Gemma-on-Ollama vision model and writes structured labels to `data/labels.db` (SQLite). `photo-browser.html` loads that SQLite file in-browser via `sql.js`.

The two halves are independent: the labeler reads `data/hdb.json` only to enumerate 4-Room resale listings for `--all-4room`. CI never runs the labeler — that requires a GPU host bootstrapped with `scripts/setup_gemma_host.sh`.

## Common commands

```bash
# Listings (runs in CI daily; manually for local refresh):
python3 scripts/scrape.py

# Photos — one listing or every 4-Room resale (idempotent, skips downloaded):
python3 scripts/scrape_photos.py --listing-id 38260
python3 scripts/scrape_photos.py --all-4room

# Label — idempotent, only new photos hit the model:
python3 scripts/label_photos.py                       # everything unlabelled
python3 scripts/label_photos.py --listing-id 38260
python3 scripts/label_photos.py --limit 10            # smoke test
python3 scripts/label_photos.py --relabel             # re-run on existing rows
python3 scripts/label_photos.py --model gemma4:e4b    # override model

# Frontends are static — open index.html / photo-browser.html, or serve the repo root.
```

Python deps: `pip install -r scripts/requirements.txt` (`requests`, `ollama`).

## Scraper auth fallback chain (scripts/scrape.py)

The HDB API is XSRF-protected. The script tries, in order: (1) direct POST, (2) self-generated UUID as both `XSRF-TOKEN` cookie and `X-XSRF-TOKEN` header (Angular double-submit pattern), (3) page visit to collect server-set cookies, (4) stored cookie from `HDB_COOKIE` env var or `data/.cookie`. When modifying scrape logic, preserve this fallback order — the earlier methods avoid the manual cookie refresh that (4) requires. Also: the API returns minimal records when `modeOfSale` is empty, so the script makes one call per mode (Resale, BTO) to get the rich payload (price, address, photo, area, lease). Don't collapse those into a single call.

## Label vocabulary is fixed

`ROOMS` and `MOODS` lists in `scripts/label_photos.py` are the closed vocabulary baked into the prompt. The model is instructed to use only these values. If you add/remove tags, update both the lists and any consumers (`photo-browser.html` filters, SQL examples in `QUICKSTART.md`). Floor plans (filenames containing `-FP-`) are tagged directly from the filename and never sent to the model.

## Data files not in git

`.gitignore` excludes `/photos`, `data/labels.db`, and `data/.cookie`. The labeled DB and downloaded images live only on the labeling host; `photo-browser.html` expects `data/labels.db` to be present locally when opened. Don't commit these.

## Deployment

Any push to `main` redeploys the whole repo to GitHub Pages. Both HTML files plus `data/hdb.json` are served from there; `data/labels.db` is not (it's gitignored), so the photo browser only works against a local copy.
