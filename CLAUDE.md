# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Two loosely-coupled halves that share `data/`:

1. **Listings tracker** — `scripts/scrape.py` fetches HDB resale/BTO listings from `api.homes.hdb.gov.sg` daily (GitHub Actions, `.github/workflows/scrape.yml`) and commits the result to `data/hdb.json`. `index.html` is a single-file Leaflet UI that reads `data/hdb.json` directly. Deployed to GitHub Pages via `.github/workflows/static.yml` (uploads the entire repo on push to `main`).
2. **Photo labeler** — `scripts/scrape_photos.py` downloads listing photos to `photos/<listing_id>/`; two interchangeable labelers write structured labels to `data/labels.db` (SQLite), and `photo-browser.html` loads that SQLite file in-browser via `sql.js`:
   - `scripts/label_photos_clip.py` — **the default**: SigLIP 2 zero-shot room classification (PyTorch MPS on Apple Silicon, batched). Rooms only, `moods` empty. ~26 photos/s on a base M1; the full photo set relabels in ~10 min. Deps: `scripts/requirements-clip.txt` in a Python 3.10+ venv (`.venv/`, gitignored).
   - `scripts/label_photos.py` — Gemma-on-Ollama vision model. Adds moods + justification but needs a GPU host bootstrapped with `scripts/setup_gemma_host.sh` (or hours/days on a laptop). Keep for mood labeling only.

Both share the same `photo_labels` schema and idempotent skip logic; rows are distinguished by the `model` column. The current `labels.db` is uniformly SigLIP-labeled (plus `filename-heuristic` floor-plan rows).

The two halves are independent: the labelers read `data/hdb.json` only to enumerate 4-Room resale listings. CI never runs them.

## Common commands

```bash
# Listings (runs in CI daily; manually for local refresh):
python3 scripts/scrape.py

# Photos — one listing or every 4-Room resale (idempotent, skips downloaded):
python3 scripts/scrape_photos.py --listing-id 38260
python3 scripts/scrape_photos.py --all-4room

# Label with SigLIP (default) — idempotent, only new photos hit the model:
.venv/bin/python scripts/label_photos_clip.py                  # everything unlabelled
.venv/bin/python scripts/label_photos_clip.py --listing-id 38260
.venv/bin/python scripts/label_photos_clip.py --limit 50       # smoke test
.venv/bin/python scripts/label_photos_clip.py --relabel        # re-run on existing rows
.venv/bin/python scripts/label_photos_clip.py --threshold 0.05 # multi-room tags (default 0.15 ≈ top-1)

# Label with Gemma (rooms + moods; needs Ollama):
python3 scripts/label_photos.py                       # same flags as above
python3 scripts/label_photos.py --model gemma4:e4b    # override model

# Frontends are static — open index.html / photo-browser.html, or serve the repo root.
```

Python deps: `pip install -r scripts/requirements.txt` (`requests`, `ollama`); SigLIP labeler: `python3.10+ -m venv .venv && .venv/bin/pip install -r scripts/requirements-clip.txt` (`torch`, `transformers`, `pillow`).

## Scraper shape (scripts/scrape.py)

The HDB API returns minimal records when `modeOfSale` is empty, so the script makes one call per mode (Resale, BTO) to get the rich payload (price, address, photo, area, lease). Don't collapse those into a single call.

## Label vocabulary is fixed

`ROOMS` and `MOODS` lists in `scripts/label_photos.py` are the closed vocabulary baked into the prompt. The model is instructed to use only these values. The SigLIP labeler's room tags come from the `ROOM_PROMPTS` dict in `scripts/label_photos_clip.py`, whose keys must stay a subset of `ROOMS`. If you add/remove tags, update the lists, `ROOM_PROMPTS`, and any consumers (`photo-browser.html` filters, SQL examples in `QUICKSTART.md`). Floor plans (filenames containing `-FP-`) are tagged directly from the filename and never sent to the model (both labelers).

## Data files not in git

`.gitignore` excludes `/photos`, `data/labels.db`, and `data/.cookie`. The labeled DB and downloaded images live only on the labeling host; `photo-browser.html` expects `data/labels.db` to be present locally when opened. Don't commit these.

## Deployment

Any push to `main` redeploys the whole repo to GitHub Pages. Both HTML files plus `data/hdb.json` are served from there; `data/labels.db` is not (it's gitignored), so the photo browser only works against a local copy.
