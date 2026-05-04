import argparse
import json
import os
import time
import requests

LISTING_ID = 38260
CDN_BASE = "https://resources.homes.hdb.gov.sg"
API_BASE = "https://homes.hdb.gov.sg"
PHOTOS_DIR = os.path.join(os.path.dirname(__file__), "..", "photos")
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def fetch_image_paths(session: requests.Session, listing_id: int) -> list[str]:
    listing_url = f"{API_BASE}/home/resale/{listing_id}"
    session.get(listing_url, timeout=15)

    api_url = f"{API_BASE}/hdbflatportalgcc/public/v1/resale/getAllImagesByListing"
    resp = session.post(
        api_url,
        json={"listingId": listing_id},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": listing_url,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("imageList", [])


def filter_images(image_paths: list[str]) -> tuple[list[str], list[str]]:
    photos = [p for p in image_paths if "-IMG-" in p and "-THUMBNAIL-" not in p]
    floor_plans = [p for p in image_paths if "-FP-" in p and "-THUMBNAIL-" not in p]
    return photos, floor_plans


def download_images(paths: list[str], output_dir: str) -> None:
    os.makedirs(output_dir, exist_ok=True)
    for path in paths:
        url = f"{CDN_BASE}/{path}"
        filename = os.path.basename(path)
        dest = os.path.join(output_dir, filename)

        print(f"Downloading {filename} ...", end=" ", flush=True)
        resp = requests.get(url, stream=True, timeout=30)
        resp.raise_for_status()

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        print(f"saved ({os.path.getsize(dest):,} bytes)")
        time.sleep(1)


def load_4room_listing_ids(hdb_json_path: str) -> list[str]:
    with open(hdb_json_path) as f:
        data = json.load(f)
    ids = []
    for item in data:
        props = item.get("properties", {})
        if props.get("listingType") != "Resale":
            continue
        desc = props.get("description", [{}])[0]
        if desc.get("flatType") == "4-Room" and desc.get("listingId"):
            ids.append(desc["listingId"])
    return ids


def scrape_single(session: requests.Session, listing_id: int) -> None:
    output_dir = os.path.join(PHOTOS_DIR, str(listing_id))

    print(f"Fetching image list for listing {listing_id} ...")
    image_paths = fetch_image_paths(session, listing_id)
    print(f"  Total images returned: {len(image_paths)}")

    photos, floor_plans = filter_images(image_paths)
    print(f"  Full-size photos : {len(photos)}")
    print(f"  Floor plans      : {len(floor_plans)}")

    all_to_download = photos + floor_plans
    if not all_to_download:
        print("  Nothing to download.")
        return

    print(f"  Saving to: {os.path.abspath(output_dir)}\n")
    download_images(all_to_download, output_dir)
    print(f"  Done. {len(all_to_download)} file(s) saved.\n")


def scrape_all_4room(session: requests.Session, hdb_json_path: str, skip_existing: bool) -> None:
    listing_ids = load_4room_listing_ids(hdb_json_path)
    print(f"Found {len(listing_ids)} 4-Room resale listings in {hdb_json_path}\n")

    total_files = 0
    skipped = 0
    errors = 0

    for i, listing_id in enumerate(listing_ids, 1):
        output_dir = os.path.join(PHOTOS_DIR, str(listing_id))
        prefix = f"[{i}/{len(listing_ids)}] Listing {listing_id}"

        if skip_existing and os.path.isdir(output_dir) and os.listdir(output_dir):
            print(f"{prefix}: skipped (already downloaded)")
            skipped += 1
            continue

        print(f"{prefix}:")
        try:
            image_paths = fetch_image_paths(session, int(listing_id))
            photos, floor_plans = filter_images(image_paths)
            all_to_download = photos + floor_plans
            print(f"  {len(photos)} photo(s), {len(floor_plans)} floor plan(s)")

            if not all_to_download:
                print("  Nothing to download.")
                continue

            download_images(all_to_download, output_dir)
            total_files += len(all_to_download)
        except Exception as e:
            print(f"  ERROR: {e}")
            errors += 1
            time.sleep(2)

    print(f"\nAll done. {total_files} file(s) downloaded, {skipped} skipped, {errors} error(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download HDB listing photos")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--listing-id",
        type=int,
        default=LISTING_ID,
        help=f"Single listing ID to scrape (default: {LISTING_ID})",
    )
    group.add_argument(
        "--all-4room",
        action="store_true",
        help="Scrape photos for all 4-Room resale listings in hdb.json",
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
        help="Skip listings whose output directory already exists and is non-empty (default: true)",
    )
    parser.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="Re-download even if output directory already exists",
    )
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; hdb-tracker/1.0)"})

    if args.all_4room:
        scrape_all_4room(session, args.hdb_json, args.skip_existing)
    else:
        scrape_single(session, args.listing_id)


if __name__ == "__main__":
    main()
