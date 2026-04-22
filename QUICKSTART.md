# Quick Start — Photo Labeling

End-to-end: bring up a Gemma-on-Ollama host, scrape HDB listing photos, label them into SQLite.

## 1. Prerequisites on the remote host

- NVIDIA driver installed (`nvidia-smi` works)
- Python 3.10+
- `git`, `curl`

Tested on dual RTX 4500 Ada (48 GB total). A single RTX 4500 Ada (24 GB) is enough for `gemma4:31b` (Dense) at Q4.

> **Ollama tag**: the scripts default to `gemma4:31b`. Confirm the exact tag on your Ollama version (`ollama search gemma4`) — variants may be published as `gemma4:31b`, `gemma4:31b-dense`, `gemma4:26b` (MoE), `gemma4:e4b`, or `gemma4:e2b`. Override with `--model` / the `MODEL` env var.

## 2. Clone and bootstrap

```bash
git clone https://github.com/tinydot/hdb-tracker.git
cd hdb-tracker
bash scripts/setup_gemma_host.sh
```

This installs Ollama (if missing), starts `ollama serve` with `OLLAMA_SCHED_SPREAD=1`, pulls `gemma4:31b` (Dense), and installs Python deps.

Override the model — e.g. the MoE variant for better throughput, or an edge variant for a low-VRAM host:

```bash
MODEL=gemma4:26b  bash scripts/setup_gemma_host.sh   # 26B MoE
MODEL=gemma4:e4b  bash scripts/setup_gemma_host.sh   # edge, ~4B-effective
MODEL=gemma4:e2b  bash scripts/setup_gemma_host.sh   # edge, ~2B-effective
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

# Use a smaller/faster variant:
python3 scripts/label_photos.py --model gemma4:26b   # MoE
python3 scripts/label_photos.py --model gemma4:e4b   # edge
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

## Model variants

Because the output is constrained to a fixed label vocabulary and temperature is set to 0.1, smaller models perform close to larger ones on this task. **`gemma4:e4b` is a reasonable default** — try it before committing to the heavier variants.

| Tag | Size | When to pick |
|---|---|---|
| `gemma4:e4b`  | ~4B edge  | **Good starting point.** Fixed-vocab classification at low temperature narrows the gap with larger models. Weaker on subtle mood distinctions (`japandi` vs `scandinavian`). |
| `gemma4:26b`  | 26B MoE   | Step up if `e4b` mood tags feel too imprecise; highest throughput per watt among the larger variants. |
| `gemma4:31b`  | 31B Dense | Best overall quality; needed only if mood nuance matters and throughput is not a constraint. Fits a single 24 GB GPU at Q4. |
| `gemma4:e2b`  | ~2B edge  | Smoke tests / CPU fallback only. |

## Tag vocabulary

**Rooms** (multi-label): `living_room`, `bedroom`, `kitchen`, `toilet`, `dining`, `balcony`, `study`, `corridor`, `entryway`, `storeroom`, `utility_yard`, `exterior`, `floor_plan`, `other`

**Moods** (multi-label, up to 3): `japandi`, `scandinavian`, `minimalist`, `modern`, `industrial`, `retro`, `traditional`, `luxe`, `homey`, `cozy`, `eclectic`, `messy`, `cluttered`, `empty`

Floor plans (filenames containing `-FP-`) are tagged directly from the filename and never sent to the model.

## Performance notes

- `gemma4:e4b` on a single RTX 4500 Ada is roughly 0.5–1 s/photo — well under an hour for the initial 3,200-photo batch.
- `gemma4:31b` (Dense) on one RTX 4500 Ada is roughly 3–5 s/photo → a few hours for the same batch. Exact numbers depend on quant level and Ollama version — run `--limit 50` first to measure.
- `gemma4:26b` (MoE) typically delivers higher throughput at some cost to subtle-mood consistency.
- For true dual-GPU throughput, run two `ollama serve` instances pinned via `CUDA_VISIBLE_DEVICES=0` and `CUDA_VISIBLE_DEVICES=1` on different ports, and shard listings between them.
- Gemma 4's built-in reasoning can improve label quality, but latency rises if thinking tokens are uncapped. If Ollama exposes a reasoning toggle for your version, keep it off (or tightly capped) for bulk labeling.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `nvidia-smi not found` | Install the NVIDIA driver first |
| `pull: model not found` for `gemma4:31b` | Upgrade Ollama (`curl -fsSL https://ollama.com/install.sh \| sh`) and confirm the tag with `ollama search gemma4` |
| Labeler hangs on first photo | First call compiles/loads the model into VRAM — expect ~30–60 s cold start |
| `model returned invalid JSON after retries` | Rerun — transient. If persistent, try `--model gemma4:26b` or disable built-in reasoning |
| Want to re-label with a different model | `python3 scripts/label_photos.py --relabel --model gemma4:31b` |
