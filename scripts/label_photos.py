import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import ollama

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"
DEFAULT_DB = DATA_DIR / "labels.db"
DEFAULT_MODEL = "gemma4:31b"
DEFAULT_HOSTS = os.environ.get("OLLAMA_HOSTS", os.environ.get("OLLAMA_HOST", "http://localhost:11434"))

ROOMS = [
    "living_room",
    "bedroom",
    "kitchen",
    "toilet",
    "dining",
    "balcony",
    "study",
    "corridor",
    "entryway",
    "storeroom",
    "utility_yard",
    "exterior",
    "floor_plan",
    "other",
]

MOODS = [
    "japandi",
    "scandinavian",
    "minimalist",
    "modern",
    "industrial",
    "retro",
    "traditional",
    "luxe",
    "homey",
    "cozy",
    "eclectic",
    "messy",
    "cluttered",
    "empty",
]

PROMPT = f"""You are labeling a photograph from a Singapore HDB resale listing.

Return ONLY a single JSON object, no prose, no markdown fences, matching this schema exactly:
{{
  "rooms":   [<zero or more of: {", ".join(ROOMS)}>],
  "moods":   [<zero or more of: {", ".join(MOODS)}>],
  "justification": "<one short sentence citing the main visual cues>",
  "confidence": <float between 0.0 and 1.0>
}}

Rules:
- rooms: include EVERY room/area clearly visible. Open-plan shots often have multiple (e.g. ["living_room","kitchen","dining"]).
- moods: up to 3 tags that best describe the aesthetic. A bare/staged unit with no furniture = ["empty"].
- If the photo is a floor plan diagram, use rooms=["floor_plan"] and moods=[].
- confidence = your overall confidence in the labels (not in any one tag).
- Use ONLY values from the allowed lists. Do not invent new tags.
"""

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
    conn = sqlite3.connect(path, check_same_thread=False)
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
    conn.commit()


def iter_photos(data_dir: Path, only_listing: int | None):
    if not data_dir.is_dir():
        return
    for listing_dir in sorted(data_dir.iterdir()):
        if not listing_dir.is_dir() or not listing_dir.name.isdigit():
            continue
        listing_id = int(listing_dir.name)
        if only_listing is not None and listing_id != only_listing:
            continue
        for img in sorted(listing_dir.iterdir()):
            if not img.is_file():
                continue
            if img.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            if "-THUMBNAIL-" in img.name:
                continue
            yield listing_id, img


def classify_by_filename(filename: str) -> str | None:
    if "-FP-" in filename:
        return "floor_plan"
    if "-IMG-" in filename:
        return "photo"
    return None


def validate_and_clean(data: dict) -> dict:
    rooms_in = data.get("rooms") or []
    moods_in = data.get("moods") or []
    if not isinstance(rooms_in, list) or not isinstance(moods_in, list):
        raise ValueError("rooms/moods must be lists")

    rooms = [r for r in rooms_in if r in ROOMS]
    moods = [m for m in moods_in if m in MOODS]

    conf = data.get("confidence")
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = None
    if conf is not None:
        conf = max(0.0, min(1.0, conf))

    justification = data.get("justification") or ""
    if not isinstance(justification, str):
        justification = str(justification)

    return {
        "rooms": rooms,
        "moods": moods,
        "justification": justification.strip(),
        "confidence": conf,
    }


def call_gemma(client: ollama.Client, model: str, image_path: Path) -> dict:
    resp = client.chat(
        model=model,
        messages=[{"role": "user", "content": PROMPT, "images": [str(image_path)]}],
        format="json",
        options={"temperature": 0.1},
    )
    content = resp["message"]["content"]
    return json.loads(content)


def label_photo(
    client: ollama.Client,
    model: str,
    image_path: Path,
    max_retries: int = 1,
) -> dict:
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            raw = call_gemma(client, model, image_path)
            return validate_and_clean(raw)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"model returned invalid JSON after retries: {last_err}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description="Label HDB listing photos with a local Gemma model via Ollama")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--hosts",
        default=DEFAULT_HOSTS,
        help="Comma-separated Ollama server URLs. One worker per host (e.g. dual-GPU: "
             "'http://localhost:11434,http://localhost:11435')",
    )
    parser.add_argument("--host", default=None, help="(deprecated) alias for --hosts with a single URL")
    parser.add_argument("--listing-id", type=int, default=None, help="Label only this listing")
    parser.add_argument("--limit", type=int, default=None, help="Stop after queueing N photos")
    parser.add_argument("--relabel", action="store_true", help="Re-label photos already in the DB")
    parser.add_argument("--dry-run", action="store_true", help="Don't call the model; just print what would be done")
    args = parser.parse_args()

    hosts_raw = args.host if args.host else args.hosts
    hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
    if not hosts:
        print("ERROR: no Ollama hosts configured", file=sys.stderr)
        return 2
    clients = [ollama.Client(host=h) for h in hosts]

    conn = open_db(args.db)
    db_lock = threading.Lock()
    stats_lock = threading.Lock()
    stats = {"labeled": 0, "errors": 0}

    # Build the work list synchronously so we can write floor plans and skip
    # already-labeled photos without racing the pool.
    work: list[tuple[int, Path]] = []
    scanned = 0
    skipped_existing = 0
    fp_written = 0
    for listing_id, img_path in iter_photos(args.data_dir, args.listing_id):
        scanned += 1
        filename = img_path.name

        if not args.relabel and already_labeled(conn, listing_id, filename):
            skipped_existing += 1
            continue

        rel_path = str(img_path.relative_to(args.data_dir.parent))

        if classify_by_filename(filename) == "floor_plan":
            row = {
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
            }
            if args.dry_run:
                print(f"[FP]  {rel_path}")
            else:
                upsert_label(conn, row)
            fp_written += 1
            continue

        work.append((listing_id, img_path))
        if args.limit is not None and len(work) >= args.limit:
            break

    total_to_label = len(work)
    print(
        f"Scanned {scanned} photos: "
        f"{skipped_existing} already labeled, {fp_written} floor plan(s), "
        f"{total_to_label} to label across {len(hosts)} host(s)."
    )

    if args.dry_run:
        for listing_id, img_path in work:
            print(f"[?]   {img_path.relative_to(args.data_dir.parent)}")
        return 0

    def process(idx: int, listing_id: int, img_path: Path) -> None:
        client = clients[idx % len(clients)]
        filename = img_path.name
        rel_path = str(img_path.relative_to(args.data_dir.parent))
        try:
            result = label_photo(client, args.model, img_path)
        except Exception as e:
            with stats_lock:
                stats["errors"] += 1
            print(f"[host={hosts[idx % len(clients)]}] ERROR {rel_path}: {e}")
            return

        row = {
            "listing_id": listing_id,
            "filename": filename,
            "path": rel_path,
            "is_floor_plan": 0,
            "rooms": json.dumps(result["rooms"]),
            "moods": json.dumps(result["moods"]),
            "justification": result["justification"],
            "confidence": result["confidence"],
            "model": args.model,
            "labeled_at": now_iso(),
        }
        with db_lock:
            upsert_label(conn, row)
        with stats_lock:
            stats["labeled"] += 1
            n = stats["labeled"]
        print(
            f"[{n}/{total_to_label} gpu{idx % len(clients)}] {rel_path} "
            f"rooms={result['rooms']} moods={result['moods']} conf={result['confidence']}"
        )

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futures = [
            pool.submit(process, i, lid, ip)
            for i, (lid, ip) in enumerate(work)
        ]
        for fut in as_completed(futures):
            fut.result()

    elapsed = time.time() - t0
    print()
    print(f"Scanned    : {scanned}")
    print(f"Labeled    : {stats['labeled'] + fp_written}")
    print(f"  of which floor plans : {fp_written}")
    print(f"Skipped (already done) : {skipped_existing}")
    print(f"Errors     : {stats['errors']}")
    print(f"Hosts      : {hosts}")
    print(f"Elapsed    : {elapsed:.1f}s")
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
