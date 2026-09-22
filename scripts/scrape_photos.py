import argparse
import json
import os
import time
import uuid
import requests

CDN_BASE = "https://resource.homes.hdb.gov.sg"
SITE_BASE = "https://homes.hdb.gov.sg"
API_BASE = "https://api.homes.hdb.gov.sg"
PHOTOS_DIR = os.path.join(os.path.dirname(__file__), "..", "photos")
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _extract_image_list(payload: dict) -> list[str]:
    """Pull the image paths out of the response.

    The flatback API returns image paths split across "scannedList" and
    "unscannedList"; merge both. ("imageList" is the legacy key, kept as a
    fallback in case the endpoint ever reverts.)
    """
    if not isinstance(payload, dict):
        return []
    paths: list[str] = []
    for key in ("scannedList", "unscannedList", "imageList"):
        value = payload.get(key)
        if isinstance(value, list):
            paths.extend(value)
    return paths


def fetch_image_paths(session: requests.Session, listing_id: int) -> list[str]:
    # Angular double-submit XSRF pattern (same as scripts/scrape.py): the API
    # only checks that the X-XSRF-TOKEN header equals the XSRF-TOKEN cookie, both
    # client-supplied. A self-generated UUID satisfies it — no page visit needed.
    token = str(uuid.uuid4())
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": SITE_BASE,
        "referer": f"{SITE_BASE}/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": BROWSER_UA,
        "cookie": f"XSRF-TOKEN={token}",
        "x-xsrf-token": token,
    }

    api_url = f"{API_BASE}/flatback/public/v1/resale/getAllImagesByListing"
    resp = session.post(
        api_url,
        json={"listingId": str(listing_id)},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    if os.environ.get("HDB_DEBUG"):
        print(f"  [debug] response: {json.dumps(payload)[:500]}")
    return _extract_image_list(payload)


def filter_images(image_paths: list[str]) -> tuple[list[str], list[str]]:
    photos = [p for p in image_paths if "-IMG-" in p and "-THUMBNAIL-" not in p]
    floor_plans = [p for p in image_paths if "-FP-" in p and "-THUMBNAIL-" not in p]
    return photos, floor_plans


def is_complete_image(path: str) -> bool:
    """Cheap corruption check without decoding: verify the file is non-empty and
    ends with its format's end-of-file marker. Catches truncated downloads (the
    common failure) without pulling in Pillow.
    """
    try:
        if os.path.getsize(path) == 0:
            return False
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            if ext in (".jpg", ".jpeg"):
                f.seek(-2, os.SEEK_END)
                return f.read(2) == b"\xff\xd9"  # JPEG End-Of-Image marker
            if ext == ".png":
                f.seek(-8, os.SEEK_END)
                return f.read(8) == b"\x49\x45\x4e\x44\xae\x42\x60\x82"  # PNG IEND chunk
    except OSError:
        return False
    # Unknown/other extension: non-empty is the best we can cheaply assert.
    return True


def download_images(session: requests.Session, paths: list[str], output_dir: str, retries: int = 2) -> None:
    os.makedirs(output_dir, exist_ok=True)
    for path in paths:
        url = f"{CDN_BASE}/{path}"
        filename = os.path.basename(path)
        dest = os.path.join(output_dir, filename)

        print(f"Downloading {filename} ...", end=" ", flush=True)
        for attempt in range(retries + 1):
            resp = session.get(
                url,
                stream=True,
                timeout=30,
                headers={"Referer": f"{SITE_BASE}/"},
            )
            resp.raise_for_status()

            written = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
                    written += len(chunk)

            expected = resp.headers.get("Content-Length")
            short = expected is not None and written != int(expected)
            if not short and is_complete_image(dest):
                print(f"saved ({os.path.getsize(dest):,} bytes)")
                break

            reason = "size mismatch" if short else "truncated/corrupt"
            if attempt < retries:
                print(f"{reason}, retrying ({attempt + 1}/{retries}) ...", end=" ", flush=True)
                time.sleep(1)
            else:
                print(f"{reason}; gave up after {retries} retr{'y' if retries == 1 else 'ies'}")
        time.sleep(1)


def load_listing_ids(hdb_json_path: str, flat_types: list[str] = None) -> list[str]:
    wanted = set(flat_types) if flat_types else None
    with open(hdb_json_path) as f:
        data = json.load(f)
    ids = []
    for item in data:
        props = item.get("properties", {})
        if props.get("listingType") != "Resale":
            continue
        desc = props.get("description", [{}])[0]
        if not desc.get("listingId"):
            continue
        if wanted and desc.get("flatType") not in wanted:
            continue
        ids.append(desc["listingId"])
    return ids


def scrape_single(session: requests.Session, listing_id: int, skip_existing: bool = True) -> bool:
    """Returns True if files were downloaded, False if skipped/empty."""
    output_dir = os.path.join(PHOTOS_DIR, str(listing_id))

    print(f"Fetching image list for listing {listing_id} ...")
    image_paths = fetch_image_paths(session, listing_id)
    print(f"  Total images returned: {len(image_paths)}")

    photos, floor_plans = filter_images(image_paths)
    print(f"  Full-size photos : {len(photos)}")
    print(f"  Floor plans      : {len(floor_plans)}")

    all_to_download = photos + floor_plans
    expected = len(all_to_download)
    if not all_to_download:
        print("  Nothing to download.")
        return False

    if skip_existing and os.path.isdir(output_dir):
        existing = [f for f in os.listdir(output_dir)
                    if os.path.isfile(os.path.join(output_dir, f))]
        corrupt = [f for f in existing
                   if not is_complete_image(os.path.join(output_dir, f))]
        if len(existing) == expected and not corrupt:
            print(f"  skipped ({expected} file(s) already present)")
            return False
        if corrupt:
            print(f"  {len(corrupt)} corrupt file(s) detected; re-downloading")
        else:
            print(f"  count mismatch (have {len(existing)}, expect {expected}); re-downloading")
        for f in existing:
            os.remove(os.path.join(output_dir, f))

    print(f"  Saving to: {os.path.abspath(output_dir)}\n")
    download_images(session, all_to_download, output_dir)
    print(f"  Done. {expected} file(s) saved.\n")
    return True


def scrape_all(session: requests.Session, hdb_json_path: str, skip_existing: bool, flat_types: list[str]) -> None:
    listing_ids = load_listing_ids(hdb_json_path, flat_types=flat_types)
    label = f"{', '.join(flat_types)} resale" if flat_types else "resale"
    print(f"Found {len(listing_ids)} {label} listings in {hdb_json_path}\n")

    processed = 0
    errors = 0

    for i, listing_id in enumerate(listing_ids, 1):
        print(f"[{i}/{len(listing_ids)}] ", end="")
        try:
            if scrape_single(session, int(listing_id), skip_existing=skip_existing):
                processed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            errors += 1
            time.sleep(2)

    print(f"\nAll done. {processed} listing(s) downloaded, {errors} error(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download HDB listing photos")
    parser.add_argument(
        "--listing-id",
        type=int,
        default=None,
        help="Scrape a single listing ID instead of all listings",
    )
    parser.add_argument(
        "--3room",
        dest="three_room",
        action="store_true",
        help="Include 3-Room resale listings",
    )
    parser.add_argument(
        "--4room",
        dest="four_room",
        action="store_true",
        help="Include 4-Room resale listings",
    )
    parser.add_argument(
        "--5room",
        dest="five_room",
        action="store_true",
        help="Include 5-Room resale listings",
    )
    parser.add_argument(
        "--345room",
        dest="all_345",
        action="store_true",
        help="Include 3-, 4- and 5-Room resale listings (shorthand for all three)",
    )
    parser.add_argument(
        "--hdb-json",
        default=os.path.join(DATA_DIR, "hdb.json"),
        help="Path to hdb.json (default: data/hdb.json)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Skip listings whose file count matches the API (default: true)",
    )
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="Re-download even if output directory already matches",
    )
    args = parser.parse_args()

    flat_types = []
    if args.three_room or args.all_345:
        flat_types.append("3-Room")
    if args.four_room or args.all_345:
        flat_types.append("4-Room")
    if args.five_room or args.all_345:
        flat_types.append("5-Room")
    flat_types = flat_types or None

    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA})

    if args.listing_id is not None:
        scrape_single(session, args.listing_id, skip_existing=args.skip_existing)
    else:
        scrape_all(session, args.hdb_json, args.skip_existing, flat_types=flat_types)


if __name__ == "__main__":
    main()
