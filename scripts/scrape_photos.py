import os
import time
import requests

LISTING_ID = 38260
CDN_BASE = "https://resources.homes.hdb.gov.sg"
API_BASE = "https://homes.hdb.gov.sg"


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


def main() -> None:
    output_dir = os.path.join(os.path.dirname(__file__), "..", "data", str(LISTING_ID))

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; hdb-tracker/1.0)"})

    print(f"Fetching image list for listing {LISTING_ID} ...")
    image_paths = fetch_image_paths(session, LISTING_ID)
    print(f"  Total images returned: {len(image_paths)}")

    photos, floor_plans = filter_images(image_paths)
    print(f"  Full-size photos : {len(photos)}")
    print(f"  Floor plans      : {len(floor_plans)}")

    all_to_download = photos + floor_plans
    if not all_to_download:
        print("Nothing to download.")
        return

    print(f"\nSaving to: {os.path.abspath(output_dir)}\n")
    download_images(all_to_download, output_dir)
    print(f"\nDone. {len(all_to_download)} file(s) saved to {output_dir}")


if __name__ == "__main__":
    main()
