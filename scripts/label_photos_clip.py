"""Label HDB listing photos by room type using SigLIP zero-shot classification.

Fast alternative to label_photos.py for room labels only (no moods): a single
image embedding per photo compared against fixed text prompts, batched on the
M1 GPU via PyTorch MPS. Writes to the same data/labels.db schema; rows are
distinguishable by the model column. Floor plans are still tagged from the
filename and never sent to the model.
"""

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "photos"
DEFAULT_DB = SCRIPT_DIR.parent / "data" / "labels.db"
DEFAULT_HDB_JSON = SCRIPT_DIR.parent / "data" / "hdb.json"
DEFAULT_MODEL = "google/siglip2-base-patch16-224"

# room tag -> zero-shot prompt. Tags must stay within ROOMS in label_photos.py
# so photo-browser.html filters keep working.
ROOM_PROMPTS = {
    "living_room": "a photo of a living room with a sofa or tv console",
    "bedroom": "a photo of a bedroom with a bed",
    "kitchen": "a photo of a kitchen with cabinets and a stove or sink",
    "toilet": "a photo of a bathroom or toilet",
    "dining": "a photo of a dining area with a dining table",
    "balcony": "a photo of an apartment balcony",
    "study": "a photo of a home office or study room with a desk",
    "corridor": "a photo of an empty hallway or corridor inside an apartment",
    "entryway": "a photo of an apartment entrance with a front door",
    "storeroom": "a photo of a storeroom or storage closet",
    "utility_yard": "a photo of a laundry area or service yard with a washing machine",
    "exterior": "a photo of the outside of an apartment building",
    "floor_plan": "a floor plan diagram of an apartment",
    "other": "a photo of something else",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS photo_labels (
  listing_id    INTEGER NOT NULL,
  filename      TEXT    NOT NULL,
  path          TEXT    NOT NULL,
  is_floor_plan INTEGER NOT NULL DEFAULT 0,
  rooms         TEXT    NOT NULL,
  moods         TEXT    NOT NULL,
  justification TEXT,
  confidence    REAL,
  model         TEXT,
  labeled_at    TEXT    NOT NULL,
  PRIMARY KEY (listing_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_photo_labels_listing ON photo_labels(listing_id);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def already_labeled(conn: sqlite3.Connection, listing_id: int, filename: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM photo_labels WHERE listing_id = ? AND filename = ?",
        (listing_id, filename),
    )
    return cur.fetchone() is not None


def upsert_label(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO photo_labels
          (listing_id, filename, path, is_floor_plan, rooms, moods,
           justification, confidence, model, labeled_at)
        VALUES (:listing_id, :filename, :path, :is_floor_plan, :rooms, :moods,
                :justification, :confidence, :model, :labeled_at)
        ON CONFLICT(listing_id, filename) DO UPDATE SET
          path          = excluded.path,
          is_floor_plan = excluded.is_floor_plan,
          rooms         = excluded.rooms,
          moods         = excluded.moods,
          justification = excluded.justification,
          confidence    = excluded.confidence,
          model         = excluded.model,
          labeled_at    = excluded.labeled_at
        """,
        row,
    )


def load_flat_types(hdb_json_path: Path) -> dict[int, str]:
    with open(hdb_json_path) as f:
        data = json.load(f)
    flat_types = {}
    for item in data:
        props = item.get("properties", {})
        if props.get("listingType") != "Resale":
            continue
        desc = props.get("description", [{}])[0]
        listing_id = desc.get("listingId")
        if not listing_id:
            continue
        flat_types[int(listing_id)] = desc.get("flatType")
    return flat_types


def iter_photos(data_dir: Path, only_listing: int | None, flat_type: str | None = None, hdb_json_path: Path | None = None):
    if not data_dir.is_dir():
        return
    flat_types = load_flat_types(hdb_json_path) if flat_type else {}
    for listing_dir in sorted(data_dir.iterdir()):
        if not listing_dir.is_dir() or not listing_dir.name.isdigit():
            continue
        listing_id = int(listing_dir.name)
        if only_listing is not None and listing_id != only_listing:
            continue
        if flat_type and flat_types.get(listing_id) != flat_type:
            continue
        for img in sorted(listing_dir.iterdir()):
            if not img.is_file():
                continue
            if img.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            if "-THUMBNAIL-" in img.name:
                continue
            yield listing_id, img


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_image(path: Path):
    from PIL import Image

    img = Image.open(path)
    # JPEG draft mode decodes directly at reduced scale — much faster than a
    # full decode of a 3 MB photo when the model only needs 224px.
    img.draft("RGB", (448, 448))
    return img.convert("RGB")


class SiglipLabeler:
    def __init__(self, model_name: str):
        import torch
        from transformers import AutoModel, AutoProcessor

        self.torch = torch
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        dtype = torch.float16 if self.device == "mps" else torch.float32
        self.model = AutoModel.from_pretrained(model_name, dtype=dtype).to(self.device).eval()
        self.processor = AutoProcessor.from_pretrained(model_name)

        self.tags = list(ROOM_PROMPTS)
        with torch.no_grad():
            text_inputs = self.processor(
                text=list(ROOM_PROMPTS.values()), padding="max_length", return_tensors="pt"
            ).to(self.device)
            feats = self.model.get_text_features(**text_inputs)
            if not torch.is_tensor(feats):
                feats = feats.pooler_output
            self.text_embeds = feats / feats.norm(dim=-1, keepdim=True)

    def classify_batch(self, images: list) -> list[list[tuple[str, float]]]:
        """Return per-image [(tag, prob), ...] sorted by descending prob."""
        torch = self.torch
        with torch.no_grad():
            inputs = self.processor(images=images, return_tensors="pt")
            pixel_values = inputs["pixel_values"].to(self.device, self.model.dtype)
            feats = self.model.get_image_features(pixel_values=pixel_values)
            if not torch.is_tensor(feats):
                feats = feats.pooler_output
            feats = feats / feats.norm(dim=-1, keepdim=True)
            logits = feats @ self.text_embeds.T
            logits = logits * self.model.logit_scale.exp() + self.model.logit_bias
            probs = torch.sigmoid(logits).float().cpu()
        out = []
        for row in probs:
            scored = sorted(zip(self.tags, row.tolist()), key=lambda x: -x[1])
            out.append(scored)
        return out


def pick_rooms(scored: list[tuple[str, float]], threshold: float) -> tuple[list[str], float]:
    top_tag, top_prob = scored[0]
    rooms = [top_tag] + [t for t, p in scored[1:] if p >= threshold]
    return rooms, top_prob


def main() -> int:
    parser = argparse.ArgumentParser(description="Label HDB listing photos with SigLIP zero-shot room classification")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--listing-id", type=int, default=None, help="Label only this listing")
    parser.add_argument("--4room", dest="four_room", action="store_true", help="Only label 4-Room resale listings")
    parser.add_argument("--5room", dest="five_room", action="store_true", help="Only label 5-Room resale listings")
    parser.add_argument("--hdb-json", type=Path, default=DEFAULT_HDB_JSON, help="Path to hdb.json (default: data/hdb.json)")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N photos")
    parser.add_argument("--relabel", action="store_true", help="Re-label photos already in the DB")
    parser.add_argument("--dry-run", action="store_true", help="Don't call the model; just print what would be done")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--threshold", type=float, default=0.15, help="Sigmoid prob above which extra rooms are included beyond the top match")
    args = parser.parse_args()

    if args.four_room and args.five_room:
        parser.error("--4room and --5room are mutually exclusive")
    flat_type = "4-Room" if args.four_room else "5-Room" if args.five_room else None

    conn = open_db(args.db)

    total = 0
    skipped_existing = 0
    skipped_fp = 0
    labeled = 0
    errors = 0

    # Resolve the full work list up front so floor plans are handled without
    # loading the model, and model photos can be batched.
    pending: list[tuple[int, Path]] = []
    for listing_id, img_path in iter_photos(args.data_dir, args.listing_id, flat_type=flat_type, hdb_json_path=args.hdb_json):
        if args.limit is not None and labeled + len(pending) >= args.limit:
            break
        total += 1
        filename = img_path.name

        if not args.relabel and already_labeled(conn, listing_id, filename):
            skipped_existing += 1
            continue

        rel_path = str(img_path.relative_to(args.data_dir.parent))

        if "-FP-" in filename:
            if args.dry_run:
                print(f"[FP]  {rel_path}")
            else:
                upsert_label(conn, {
                    "listing_id": listing_id,
                    "filename": filename,
                    "path": rel_path,
                    "is_floor_plan": 1,
                    "rooms": json.dumps(["floor_plan"]),
                    "moods": json.dumps([]),
                    "justification": "Detected via filename pattern -FP-.",
                    "confidence": 1.0,
                    "model": "filename-heuristic",
                    "labeled_at": now_iso(),
                })
            skipped_fp += 1
            labeled += 1
            continue

        if args.dry_run:
            print(f"[?]   {rel_path}")
            labeled += 1
            continue

        pending.append((listing_id, img_path))

    conn.commit()

    t0 = time.time()
    if pending:
        print(f"Loading {args.model} ...")
        labeler = SiglipLabeler(args.model)
        print(f"Labeling {len(pending)} photos on {labeler.device} (batch={args.batch_size})")

        batches = [pending[i:i + args.batch_size] for i in range(0, len(pending), args.batch_size)]
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=4) as pool:
            # Decode the next batch on CPU threads while the GPU works.
            futures = [pool.submit(lambda b=b: [load_image(p) for _, p in b]) for b in batches]
            for batch, fut in zip(batches, futures):
                try:
                    images = fut.result()
                    results = labeler.classify_batch(images)
                except Exception:
                    # One bad image shouldn't sink the batch — retry singly.
                    results = []
                    good = []
                    for listing_id, img_path in batch:
                        try:
                            results.append(labeler.classify_batch([load_image(img_path)])[0])
                            good.append((listing_id, img_path))
                        except Exception as e:
                            print(f"ERROR {img_path}: {e}")
                            errors += 1
                    batch = good
                for (listing_id, img_path), scored in zip(batch, results):
                    rooms, conf = pick_rooms(scored, args.threshold)
                    top3 = ", ".join(f"{t}={p:.2f}" for t, p in scored[:3])
                    upsert_label(conn, {
                        "listing_id": listing_id,
                        "filename": img_path.name,
                        "path": str(img_path.relative_to(args.data_dir.parent)),
                        "is_floor_plan": 1 if rooms[0] == "floor_plan" else 0,
                        "rooms": json.dumps(rooms),
                        "moods": json.dumps([]),
                        "justification": f"SigLIP zero-shot: {top3}",
                        "confidence": conf,
                        "model": args.model,
                        "labeled_at": now_iso(),
                    })
                    labeled += 1
                conn.commit()
                done = labeled - skipped_fp
                rate = done / (time.time() - t0)
                print(f"  {done}/{len(pending)}  ({rate:.1f} photos/s)", flush=True)

    elapsed = time.time() - t0
    print()
    print(f"Scanned    : {total}")
    print(f"Labeled    : {labeled}")
    print(f"  of which floor plans : {skipped_fp}")
    print(f"Skipped (already done) : {skipped_existing}")
    print(f"Errors     : {errors}")
    print(f"Elapsed    : {elapsed:.1f}s")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
