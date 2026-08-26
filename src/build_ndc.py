"""
build_ndc.py

Loads the raw openFDA NDC directory JSON into the database (one row per
package NDC, expanded out of each product's `packaging` list), then
backfills `shortages.proprietary_name` by joining on product_ndc -- the
one column build_database.py could never populate on its own, since the
shortages endpoint doesn't carry a brand name (see the fix note at the
top of build_database.py).

Run after fetch_ndc.py has produced a file in data/raw/.
"""

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "db" / "supply_risk.duckdb"

SCHEMA_SQL = """
CREATE OR REPLACE TABLE ndc_directory (
    product_ndc            VARCHAR,
    package_ndc              VARCHAR,
    generic_name                VARCHAR,
    brand_name                    VARCHAR,
    brand_name_base                 VARCHAR,
    labeler_name                      VARCHAR,
    dosage_form                        VARCHAR,
    route                                VARCHAR,  -- pipe-joined list
    product_type                          VARCHAR,
    application_number                      VARCHAR,
    marketing_category                        VARCHAR,
    pharm_class                                 VARCHAR,  -- pipe-joined list
    rxcui                                         VARCHAR,  -- pipe-joined list
    unii                                            VARCHAR,  -- pipe-joined list
    manufacturer_name                                 VARCHAR,
    PRIMARY KEY (package_ndc)
);
"""


def latest_raw_file() -> Path:
    files = sorted(RAW_DIR.glob("ndc_directory_*.json"))
    if not files:
        sys.exit("No raw NDC data found. Run fetch_ndc.py first.")
    return files[-1]


def to_rows(records: list[dict]) -> list[tuple]:
    rows = []
    seen = set()
    for r in records:
        openfda = r.get("openfda", {}) or {}
        product_ndc = r.get("product_ndc")
        packaging = r.get("packaging") or [{"package_ndc": product_ndc}]
        for p in packaging:
            package_ndc = p.get("package_ndc") or product_ndc
            if package_ndc in seen:
                continue
            seen.add(package_ndc)
            rows.append((
                product_ndc,
                package_ndc,
                r.get("generic_name"),
                r.get("brand_name"),
                r.get("brand_name_base"),
                r.get("labeler_name"),
                r.get("dosage_form"),
                "|".join(r.get("route", []) or []),
                r.get("product_type"),
                r.get("application_number"),
                r.get("marketing_category"),
                "|".join(r.get("pharm_class", []) or []),
                "|".join(openfda.get("rxcui", []) or []),
                "|".join(openfda.get("unii", []) or []),
                "|".join(openfda.get("manufacturer_name", []) or []),
            ))
    return rows


def main():
    raw_path = latest_raw_file()
    print(f"Loading {raw_path.name} ...")
    records = json.loads(raw_path.read_text())

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA_SQL)

    rows = to_rows(records)
    con.execute("DELETE FROM ndc_directory")
    con.executemany(
        "INSERT INTO ndc_directory VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )

    # Backfill proprietary_name on `shortages`. shortages.record_id IS a
    # package_ndc (that's how build_database.py derives it), and
    # ndc_directory.package_ndc is the NDC directory's own primary key --
    # so this is an exact 1:1 join, no product_ndc-level ambiguity (a
    # single product_ndc can cover several packages with slightly
    # different brand_name casing/labeling, e.g. "Carboplatin" vs
    # "CARBOplatin" for the same drug -- joining on the full package_ndc
    # avoids picking an arbitrary one of those).
    con.execute("""
        UPDATE shortages
        SET proprietary_name = nd.brand_name
        FROM ndc_directory AS nd
        WHERE nd.package_ndc = shortages.record_id
          AND nd.brand_name IS NOT NULL
    """)

    n_ndc = con.execute("SELECT COUNT(*) FROM ndc_directory").fetchone()[0]
    n_backfilled = con.execute(
        "SELECT COUNT(*) FROM shortages WHERE proprietary_name IS NOT NULL"
    ).fetchone()[0]
    n_total = con.execute("SELECT COUNT(*) FROM shortages").fetchone()[0]
    print(f"Loaded {n_ndc} NDC directory rows.")
    print(f"Backfilled proprietary_name on {n_backfilled}/{n_total} shortage records.")
    print(f"Database: {DB_PATH}")

    con.close()


if __name__ == "__main__":
    main()
