# Quick Start — Photo Labeling

End-to-end: bring up a Gemma-on-Ollama host, scrape HDB listing photos, label them into SQLite.

## 1. Prerequisites on the remote host

- NVIDIA driver installed (`nvidia-smi` works)
- Python 3.10+ (on Windows: install from python.org with "Add to PATH")
- `git` and — on Linux — `curl`; on Windows, PowerShell 5.1+ (preinstalled) or PowerShell 7

Tested on dual RTX 4500 Ada (48 GB total). A single RTX 4500 Ada (24 GB) is enough for `gemma4:31b` (Dense) at Q4.

> **Ollama tag**: the scripts default to `gemma4:31b`. Confirm the exact tag on your Ollama version (`ollama search gemma4`) — variants may be published as `gemma4:31b`, `gemma4:31b-dense`, `gemma4:26b` (MoE), `gemma4:e4b`, or `gemma4:e2b`. Override with `--model` / the `MODEL` / `-Model` argument.

## 2. Clone and bootstrap

### Windows (PowerShell)

```powershell
git clone https://github.com/tinydot/hdb-tracker.git
cd hdb-tracker
powershell -ExecutionPolicy Bypass -File scripts\setup_gemma_host.ps1
```

Single-GPU host, or pick a different model:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_gemma_host.ps1 -Gpus 0
powershell -ExecutionPolicy Bypass -File scripts\setup_gemma_host.ps1 -Model gemma4:26b
```

### Linux (bash)

```bash
git clone https://github.com/tinydot/hdb-tracker.git
cd hdb-tracker
bash scripts/setup_gemma_host.sh
```

Single-GPU host, or pick a different model:

```bash
GPUS=0            bash scripts/setup_gemma_host.sh
MODEL=gemma4:26b  bash scripts/setup_gemma_host.sh   # 26B MoE
MODEL=gemma4:e4b  bash scripts/setup_gemma_host.sh   # edge, ~4B-effective
MODEL=gemma4:e2b  bash scripts/setup_gemma_host.sh   # edge, ~2B-effective
```

### What the bootstrap does (both platforms)

1. Checks `nvidia-smi` and `python`.
2. Installs Ollama if missing (Windows: silent `OllamaSetup.exe`; Linux: official install script).
3. Stops any existing Ollama processes (Windows also kills the tray app).
4. Starts **one `ollama serve` per GPU**, each pinned with `CUDA_VISIBLE_DEVICES`, on ports `11434` and `11435`, with `OLLAMA_KEEP_ALIVE=24h`.
5. Pulls the model into each instance.
6. Installs Python deps.
7. Prints the exact `--hosts` line to pass to the labeler.

## 3. Scrape listing photos

```bash
# One listing:
python3 scripts/scrape_photos.py --listing-id 38260

# Every 4-Room resale listing in data/hdb.json (skips already-downloaded):
python3 scripts/scrape_photos.py --all-4room
```

Photos land in `data/<listing_id>/`.

## 4. Label with Gemma

To label across **both GPUs in parallel**, point the labeler at both Ollama instances (one per GPU). The setup script prints these URLs; you can also set them once via env var.

Linux / macOS:

```bash
export OLLAMA_HOSTS="http://127.0.0.1:11434,http://127.0.0.1:11435"
```

Windows (PowerShell):

```powershell
$env:OLLAMA_HOSTS = "http://127.0.0.1:11434,http://127.0.0.1:11435"
```

Then (use `python` on Windows, `python3` on Linux):

```bash
# Label every unlabelled photo in data/ (fan-out to all hosts in OLLAMA_HOSTS):
python3 scripts/label_photos.py

# Same thing, explicit:
python3 scripts/label_photos.py --hosts http://127.0.0.1:11434,http://127.0.0.1:11435

# Single GPU / single host:
python3 scripts/label_photos.py --hosts http://127.0.0.1:11434

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

One worker thread is spawned per host; photos are round-robined so both GPUs stay busy. Results are written to `data/labels.db` (SQLite) under a single DB lock. Re-running is idempotent — only new photos get sent to the model.

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

| Tag | Size | When to pick |
|---|---|---|
| `gemma4:31b`  | 31B Dense | **Default.** Best label quality; fits a single 24 GB GPU at Q4. |
| `gemma4:26b`  | 26B MoE   | Highest throughput per watt; slightly less consistent on subtle mood tags. |
| `gemma4:e4b`  | ~4B edge  | Low-VRAM hosts; acceptable for room tags, weaker on mood. |
| `gemma4:e2b`  | ~2B edge  | Smoke tests / CPU fallback. |

## Tag vocabulary

**Rooms** (multi-label): `living_room`, `bedroom`, `kitchen`, `toilet`, `dining`, `balcony`, `study`, `corridor`, `entryway`, `storeroom`, `utility_yard`, `exterior`, `floor_plan`, `other`

**Moods** (multi-label, up to 3): `japandi`, `scandinavian`, `minimalist`, `modern`, `industrial`, `retro`, `traditional`, `luxe`, `homey`, `cozy`, `eclectic`, `messy`, `cluttered`, `empty`

Floor plans (filenames containing `-FP-`) are tagged directly from the filename and never sent to the model.

## Performance notes

- `gemma4:31b` (Dense) on one RTX 4500 Ada is roughly 3–5 s/photo. With both GPUs running (default `setup_gemma_host.sh`), the 3,200-photo initial batch should finish in ~1/2 the single-GPU time. Run `--limit 50` first to calibrate.
- `gemma4:26b` (MoE) typically delivers higher throughput at some cost to subtle-mood consistency.
- Dual-GPU throughput is handled automatically: `setup_gemma_host.sh` starts one `ollama serve` per GPU and the labeler round-robins across them.
- `OLLAMA_KEEP_ALIVE=24h` (set by the bootstrap) keeps each model resident in VRAM so batches don't pay cold-load time.
- Gemma 4's built-in reasoning can improve label quality, but latency rises if thinking tokens are uncapped. If Ollama exposes a reasoning toggle for your version, keep it off (or tightly capped) for bulk labeling.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `nvidia-smi not found` | Install the NVIDIA driver first |
| `pull: model not found` for `gemma4:31b` | Upgrade Ollama and confirm the tag with `ollama search gemma4` |
| Labeler hangs on first photo | First call compiles/loads the model into VRAM — expect ~30–60 s cold start, once per GPU |
| Only one GPU shows activity in `nvidia-smi` | Check you passed `--hosts` with two URLs (or that `OLLAMA_HOSTS` is set). Linux: `ps aux \| grep 'ollama serve'` — Windows: `Get-Process ollama`. Both ports `11434` and `11435` should answer `/api/tags`. |
| `model returned invalid JSON after retries` | Rerun — transient. If persistent, try `--model gemma4:26b` or disable built-in reasoning |
| Windows: `running scripts is disabled on this system` | Launch with `powershell -ExecutionPolicy Bypass -File scripts\setup_gemma_host.ps1` as shown above |
| Windows: `ollama` not found after install | Open a new PowerShell window (the installer updates PATH for new sessions) |
| Windows: port 11434 already in use | The tray Ollama app is still running. `Get-Process -Name 'ollama','ollama app' \| Stop-Process -Force`, then re-run the setup script |
| Want to re-label with a different model | `python scripts/label_photos.py --relabel --model gemma4:31b` |
