"""
build_database.py

Loads the raw openFDA drug shortage JSON into a DuckDB database with a
clean relational schema:

  shortages              -- one row per shortage record
  shortage_categories    -- junction table (a record can have multiple
                             therapeutic categories)

Run after fetch_shortages.py has produced a file in data/raw/.

--------------------------------------------------------------------------
FIX NOTE (see README "Known issues fixed"): the original version of this
script assumed field names and a date format that don't match the live
openFDA /drug/shortages.json response. Verified directly against the API:

  - Dates come back as "MM/DD/YYYY" (e.g. "04/28/2023"), not "YYYYMMDD".
    The old to_date() only handled 8-digit YYYYMMDD strings, so every date
    silently came back as NULL.
  - There is no "proprietary_name", "strength", or "resolved_note" field
    on this endpoint. r.get() on a missing key just returns None, so those
    three columns were silently 100% NULL for any real pull -- it would
    only have "worked" against a mock fixture that (incorrectly) invented
    those field names.
      * strength is now parsed out of the free-text "presentation" field
        openFDA does return (e.g. "Carboplatin, Injection, 10 mg/1 mL
        (NDC 0703-4246-01)"), since it's embedded there in practice.
      * resolved_note now reads from the real field, "related_info".
      * proprietary_name has no upstream source on this endpoint at all
        (shortages are keyed by generic name / NDC, not brand). Left in
        the schema as NULL for now -- backfilling it means joining the
        NDC Directory endpoint (https://api.fda.gov/drug/ndc.json) on
        product_ndc, which isn't built yet.
  - company_name and contact_info are real fields the old version didn't
    capture at all; added them since they're directly useful for a supply
    risk tool (who manufactures it, who to contact about the shortage).
--------------------------------------------------------------------------
"""

import json
import re
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "db" / "supply_risk.duckdb"

SCHEMA_SQL = """
CREATE OR REPLACE TABLE shortages (
    record_id           VARCHAR PRIMARY KEY,
    generic_name         VARCHAR,
    proprietary_name      VARCHAR,   -- not provided by this endpoint; see fix note above
    strength             VARCHAR,   -- parsed from `presentation`
    dosage_form           VARCHAR,
    package_ndc           VARCHAR,
    company_name           VARCHAR,
    contact_info             VARCHAR,
    status               VARCHAR,   -- 'Current' or 'Resolved' (or similar)
    availability          VARCHAR,
    update_type           VARCHAR,
    initial_posting_date   DATE,
    update_date           DATE,
    discontinued_date       DATE,
    resolved_note          VARCHAR
);

CREATE OR REPLACE TABLE shortage_categories (
    record_id       VARCHAR,
    therapeutic_category VARCHAR
);
"""

# openFDA's `presentation` field looks like:
#   "Carboplatin, Injection, 10 mg/1 mL (NDC 0703-4246-01)"
#   "Amlodipine Besylate; Hydrochlorothiazide; Olmesartan Medoxomil, Tablet, 5 mg; 25 mg; 40 mg (NDC ...)"
# Strength is everything between the second comma and the trailing " (NDC ...)".
PRESENTATION_RE = re.compile(r"^[^,]+,\s*[^,]+,\s*(.+?)\s*\(NDC")


def latest_raw_file() -> Path:
    files = sorted(RAW_DIR.glob("drug_shortages_*.json"))
    if not files:
        sys.exit("No raw data found. Run fetch_shortages.py first.")
    return files[-1]


def to_date(value):
    """
    openFDA shortage dates come back as 'MM/DD/YYYY' strings.
    Returns None if missing/invalid/unparseable.
    """
    if not value:
        return None
    value = value.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", value)
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def extract_strength(presentation: str | None) -> str | None:
    if not presentation:
        return None
    m = PRESENTATION_RE.match(presentation)
    return m.group(1).strip() if m else None


def load_records(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def main():
    raw_path = latest_raw_file()
    print(f"Loading {raw_path.name} ...")
    records = load_records(raw_path)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA_SQL)

    shortage_rows = []
    category_rows = []

    for i, r in enumerate(records):
        record_id = r.get("package_ndc") or r.get("generic_name", f"unknown_{i}") + f"_{i}"
        presentation = r.get("presentation")

        shortage_rows.append((
            record_id,
            r.get("generic_name"),
            r.get("proprietary_name"),          # always None on this endpoint (see fix note)
            extract_strength(presentation),
            r.get("dosage_form"),
            r.get("package_ndc"),
            r.get("company_name"),
            r.get("contact_info"),
            r.get("status"),
            r.get("availability"),
            r.get("update_type"),
            to_date(r.get("initial_posting_date")),
            to_date(r.get("update_date")),
            to_date(r.get("discontinued_date")),
            r.get("related_info"),               # was r.get("resolved_note"), a nonexistent field
        ))

        categories = r.get("therapeutic_category") or []
        if isinstance(categories, str):
            categories = [categories]
        for cat in categories:
            category_rows.append((record_id, cat))

    con.executemany(
        "INSERT OR IGNORE INTO shortages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        shortage_rows,
    )
    con.executemany(
        "INSERT INTO shortage_categories VALUES (?,?)",
        category_rows,
    )

    n_shortages = con.execute("SELECT COUNT(*) FROM shortages").fetchone()[0]
    n_categories = con.execute("SELECT COUNT(DISTINCT therapeutic_category) FROM shortage_categories").fetchone()[0]
    n_dates_missing = con.execute(
        "SELECT COUNT(*) FROM shortages WHERE initial_posting_date IS NULL"
    ).fetchone()[0]
    print(f"Loaded {n_shortages} shortage records across {n_categories} therapeutic categories.")
    if n_dates_missing:
        print(f"  ({n_dates_missing} records have no parseable initial_posting_date)")
    print(f"Database: {DB_PATH}")

    con.close()


if __name__ == "__main__":
    main()
