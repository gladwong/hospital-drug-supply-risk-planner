"""
fetch_shortages.py

Pulls drug shortage records from the openFDA Drug Shortages API and saves
the raw JSON to data/raw/. No API key is required for moderate usage
(openFDA rate-limits to 240 requests/min, 120k/day without a key).

API docs: https://open.fda.gov/apis/drug/drugshortages/
"""

import json
import time
from pathlib import Path
from datetime import date

import requests

BASE_URL = "https://api.fda.gov/drug/shortages.json"
PAGE_SIZE = 1000  # openFDA max per request
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def fetch_all_shortages(page_size: int = PAGE_SIZE, max_pages: int | None = None) -> list[dict]:
    """
    Paginate through the full openFDA drug shortages dataset.
    Returns a flat list of shortage record dicts.
    """
    records = []
    skip = 0
    page = 0

    while True:
        params = {"limit": page_size, "skip": skip}
        resp = requests.get(BASE_URL, params=params, timeout=30)

        if resp.status_code == 404:
            # openFDA returns 404 when `skip` runs past the end of results
            break
        resp.raise_for_status()

        payload = resp.json()
        batch = payload.get("results", [])
        if not batch:
            break

        records.extend(batch)
        skip += page_size
        page += 1
        print(f"  fetched page {page} ({len(records)} records so far)")

        if max_pages is not None and page >= max_pages:
            break

        # openFDA total available count, stop once we've got it all
        total = payload.get("meta", {}).get("results", {}).get("total")
        if total is not None and skip >= total:
            break

        time.sleep(0.25)  # be polite to the API

    return records


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print("Fetching openFDA drug shortage records...")

    records = fetch_all_shortages()

    out_path = RAW_DIR / f"drug_shortages_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(records, indent=2))

    print(f"Saved {len(records)} records to {out_path}")


if __name__ == "__main__":
    main()
