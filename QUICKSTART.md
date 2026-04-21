# Quick Start — Photo Labeling

End-to-end: bring up a Gemma-on-Ollama host, scrape HDB listing photos, label them into SQLite.

## 1. Prerequisites on the remote host

- NVIDIA driver installed (`nvidia-smi` works)
- Python 3.10+
- `git`, `curl`

Tested on dual RTX 4500 Ada (48 GB total). A single RTX 4500 Ada (24 GB) is enough for `gemma3:27b` Q4.

## 2. Clone and bootstrap

```bash
git clone https://github.com/tinydot/hdb-tracker.git
cd hdb-tracker
bash scripts/setup_gemma_host.sh
```

This installs Ollama (if missing), starts `ollama serve` with `OLLAMA_SCHED_SPREAD=1`, pulls `gemma3:27b`, and installs Python deps.

Override the model if you want the faster 12B variant:

```bash
MODEL=gemma3:12b bash scripts/setup_gemma_host.sh
```

## 3. Scrape listing photos

```bash
# One listing:
python3 scripts/scrape_photos.py --listing-id 38260

# Every 4-Room resale listing in data/hdb.json (skips already-downloaded):
python3 scripts/scrape_photos.py --all-4room
```

Photos land in `data/<listing_id>/`.

## 4. Label with Gemma

```bash
# Label every unlabelled photo in data/:
python3 scripts/label_photos.py

# Just one listing:
python3 scripts/label_photos.py --listing-id 38260

# Cap how many photos to process (useful for a smoke test):
python3 scripts/label_photos.py --limit 10

# Re-label photos already in the DB:
python3 scripts/label_photos.py --relabel

# Use the smaller/faster model:
python3 scripts/label_photos.py --model gemma3:12b
```

Results are written to `data/labels.db` (SQLite). Re-running is idempotent — only new photos get sent to the model.

## 5. Incremental runs (new listings each week)

```bash
python3 scripts/scrape_photos.py --all-4room      # downloads only new listings
python3 scripts/label_photos.py                   # labels only new photos
```

## 6. Querying results

```bash
sqlite3 data/labels.db
```

```sql
-- Japandi kitchens:
SELECT listing_id, filename, confidence
FROM photo_labels
WHERE EXISTS (SELECT 1 FROM json_each(rooms) WHERE value = 'kitchen')
  AND EXISTS (SELECT 1 FROM json_each(moods) WHERE value = 'japandi')
ORDER BY confidence DESC;

-- Listings that have at least one "messy" photo:
SELECT DISTINCT listing_id
FROM photo_labels
WHERE EXISTS (SELECT 1 FROM json_each(moods) WHERE value = 'messy');

-- Low-confidence labels to spot-check:
SELECT listing_id, filename, rooms, moods, justification, confidence
FROM photo_labels
WHERE confidence < 0.6
ORDER BY confidence ASC
LIMIT 50;
```

## Tag vocabulary

**Rooms** (multi-label): `living_room`, `bedroom`, `kitchen`, `toilet`, `dining`, `balcony`, `study`, `corridor`, `entryway`, `storeroom`, `utility_yard`, `exterior`, `floor_plan`, `other`

**Moods** (multi-label, up to 3): `japandi`, `scandinavian`, `minimalist`, `modern`, `industrial`, `retro`, `traditional`, `luxe`, `homey`, `cozy`, `eclectic`, `messy`, `cluttered`, `empty`

Floor plans (filenames containing `-FP-`) are tagged directly from the filename and never sent to the model.

## Performance notes

- `gemma3:27b` on one RTX 4500 Ada ≈ 3–5 s/photo → ~3–4 h for the initial 3,200-photo batch.
- `gemma3:12b` ≈ ~2× faster with a small accuracy trade-off on subtler mood tags.
- For true dual-GPU throughput, run two `ollama serve` instances pinned via `CUDA_VISIBLE_DEVICES=0` and `CUDA_VISIBLE_DEVICES=1` on different ports, and shard listings between them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `nvidia-smi not found` | Install the NVIDIA driver first |
| `pull: model not found` for `gemma3:27b` | Upgrade Ollama (`curl -fsSL https://ollama.com/install.sh \| sh`) or use `gemma3:12b` |
| Labeler hangs on first photo | First call compiles/loads the model into VRAM — expect ~30–60 s cold start |
| `model returned invalid JSON after retries` | Rerun — a transient decoding artifact. If persistent, lower `--model` to 12B |
| Want to re-label with a newer model | `python3 scripts/label_photos.py --relabel --model gemma3:27b` |
