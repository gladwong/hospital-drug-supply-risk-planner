"""
fetch_seasonal.py

Pulls weekly seasonal respiratory-illness hospitalization surveillance data
from CDC's RESP-NET dataset on data.cdc.gov (Socrata Open Data API) and
saves the raw JSON to data/raw/, mirroring fetch_shortages.py.

Dataset: "Rates of Laboratory-Confirmed RSV, COVID-19, and Flu
Hospitalizations from the RESP-NET Surveillance Systems"
https://data.cdc.gov/Public-Health-Surveillance/Rates-of-Laboratory-Confirmed-RSV-COVID-19-and-Flu/kvib-3txy

RESP-NET combines three CDC hospitalization surveillance networks
(FluSurv-NET, RSV-NET, COVID-NET, plus a "Combined" series) into weekly
hospitalization rates per 100,000 population, back to 2018, broken out by
season, age group, sex, race/ethnicity, and surveillance site. This is the
seasonal-demand-pressure signal for the risk model -- CDC's own FluView
portal has no comparably stable, documented machine-readable API, so this
Socrata dataset is used instead.

No auth is required for reasonable volumes. Set the SOCRATA_APP_TOKEN env
var to raise Socrata's per-IP throttling limits if you hit them.

API docs: https://dev.socrata.com/foundry/data.cdc.gov/kvib-3txy

--------------------------------------------------------------------------
SCHEMA CHANGE NOTE (discovered live, 2026-08-25): CDC reshaped this
dataset's columns at some point after this project was first built against
it. The field names below are what the endpoint returns TODAY, confirmed
live via a $group query:

  surveillance_network, season, date_type, date, age_category, race, sex,
  state, data_type, estimate_type, rate_type, estimate

That's a "long" (tidy) layout: instead of one row per (network, week) with
separate weekly_rate/cumulative_rate columns, each row now carries ONE
number in `estimate`, and `data_type` says what it is
("Weekly Rate" / "Cumulative Rate" / four other clinical-severity metrics
this dataset apparently also tracks now: "Admitted to Intensive Care Unit",
"Invasive Mechanical Ventilation", "In-Hospital Death",
"≥1 Underlying Condition" -- all reported as "Percent" rather than
"Rate per 100,000", and keyed by `date_type` = "season"/"Season" rather
than "Week Ending Date").

This fetch only wants the weekly rate series, so it filters server-side to
`date_type = 'Week Ending Date'` -- that alone is ~119k of the ~121.5k
total rows, leaving out the season-level severity metrics entirely.
build_seasonal.py is the one that pivots `data_type` back into separate
weekly_rate/cumulative_rate columns, to keep everything downstream (the
Step 2 views, the risk model) working against the original wide schema.
--------------------------------------------------------------------------
"""

import json
import os
import time
from pathlib import Path
from datetime import date

import requests

DATASET_URL = "https://data.cdc.gov/resource/kvib-3txy.json"
PAGE_SIZE = 10000  # Socrata allows large pages; paginate in chunks anyway
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN")


def fetch_all_seasonal(page_size: int = PAGE_SIZE, max_pages: int | None = None) -> list[dict]:
    """
    Paginate through the full RESP-NET dataset via Socrata's $limit/$offset.
    Returns a flat list of record dicts.
    """
    headers = {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}
    records = []
    offset = 0
    page = 0

    while True:
        params = {
            "$limit": page_size,
            "$offset": offset,
            "$where": "date_type = 'Week Ending Date'",
            "$order": "surveillance_network,date",
        }
        resp = requests.get(DATASET_URL, params=params, headers=headers, timeout=60)
        resp.raise_for_status()

        batch = resp.json()
        if not batch:
            break

        records.extend(batch)
        offset += page_size
        page += 1
        print(f"  fetched page {page} ({len(records)} records so far)")

        if max_pages is not None and page >= max_pages:
            break
        if len(batch) < page_size:
            break

        time.sleep(0.2)  # be polite to the API

    return records


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print("Fetching CDC RESP-NET seasonal surveillance records...")

    records = fetch_all_seasonal()

    out_path = RAW_DIR / f"seasonal_surveillance_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(records, indent=2))

    print(f"Saved {len(records)} records to {out_path}")


if __name__ == "__main__":
    main()
