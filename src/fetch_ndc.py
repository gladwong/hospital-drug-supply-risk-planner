"""
fetch_ndc.py

Pulls openFDA NDC Directory records (https://api.fda.gov/drug/ndc.json) for
every product referenced by a shortage record already loaded into the
database, and saves the raw JSON to data/raw/ -- mirroring
fetch_shortages.py / fetch_seasonal.py.

Unlike those two scripts, this one needs input from the database (the set
of product_ndc values to look up), so run it after build_database.py.

API docs: https://open.fda.gov/apis/drug/ndc/
"""

import json
import time
from pathlib import Path
from datetime import date

import duckdb
import requests

BASE_URL = "https://api.fda.gov/drug/ndc.json"
PAGE_SIZE = 1000  # openFDA max per request
BATCH_SIZE = 20   # product_ndc values per search query (keeps URL length sane)
ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "db" / "supply_risk.duckdb"


def product_ndcs_from_db(db_path: Path) -> list[str]:
    """record_id in `shortages` is a package_ndc like '0703-4246-01';
    product_ndc is its first two segments, '0703-4246'."""
    con = duckdb.connect(str(db_path))
    ids = [r[0] for r in con.execute("SELECT DISTINCT record_id FROM shortages").fetchall()]
    con.close()

    product_ndcs = set()
    for record_id in ids:
        parts = (record_id or "").split("-")
        if len(parts) >= 2:
            product_ndcs.add(f"{parts[0]}-{parts[1]}")
    return sorted(product_ndcs)


def fetch_ndc_records(product_ndcs: list[str], batch_size: int = BATCH_SIZE) -> list[dict]:
    records = []
    for i in range(0, len(product_ndcs), batch_size):
        batch = product_ndcs[i : i + batch_size]
        terms = " ".join(f'"{p}"' for p in batch)
        search = f"product_ndc:({terms})"
        skip = 0
        while True:
            resp = requests.get(BASE_URL, params={"search": search, "limit": PAGE_SIZE, "skip": skip}, timeout=30)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            batch_results = resp.json().get("results", [])
            if not batch_results:
                break
            records.extend(batch_results)
            skip += PAGE_SIZE
            if len(batch_results) < PAGE_SIZE:
                break
            time.sleep(0.25)
        print(f"  {min(i + batch_size, len(product_ndcs))}/{len(product_ndcs)} products queried, "
              f"{len(records)} NDC records collected so far")
        time.sleep(0.25)
    return records


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    product_ndcs = product_ndcs_from_db(DB_PATH)
    if not product_ndcs:
        raise SystemExit("No shortage records found in the database. Run fetch_shortages.py + "
                          "build_database.py first.")

    print(f"Fetching openFDA NDC directory records for {len(product_ndcs)} products referenced "
          f"by tracked shortages...")
    records = fetch_ndc_records(product_ndcs)

    out_path = RAW_DIR / f"ndc_directory_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(records, indent=2))
    print(f"Saved {len(records)} records to {out_path}")


if __name__ == "__main__":
    main()
