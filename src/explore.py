"""
explore.py — Step 2

Applies sql/02_exploratory_views.sql to the database, prints a quick
summary to the console, and exports an Excel validation workbook so the
underlying numbers can be spot-checked by hand before Step 3 builds on
top of them.

Usage:
    python3 src/explore.py [--out reports/exploratory_summary.xlsx]
"""

import argparse
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "supply_risk.duckdb"
SQL_PATH = ROOT / "sql" / "02_exploratory_views.sql"
DEFAULT_OUT = ROOT / "reports" / "exploratory_summary.xlsx"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    con.execute(SQL_PATH.read_text())

    network_summary = con.execute("SELECT * FROM v_seasonal_network_summary ORDER BY 1").fetchdf()
    category_summary = con.execute("SELECT * FROM v_category_shortage_summary").fetchdf()
    drug_inputs = con.execute("SELECT * FROM v_drug_seasonal_risk_inputs").fetchdf()
    trend = con.execute("SELECT * FROM v_shortage_trend_quarterly").fetchdf()

    print("Seasonal network summary:")
    print(network_summary.to_string(index=False))
    print("\nShortage summary by therapeutic category:")
    print(category_summary.to_string(index=False))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as xl:
        network_summary.to_excel(xl, sheet_name="seasonal_network_summary", index=False)
        category_summary.to_excel(xl, sheet_name="category_shortage_summary", index=False)
        drug_inputs.to_excel(xl, sheet_name="drug_risk_inputs", index=False)
        trend.to_excel(xl, sheet_name="shortage_trend_quarterly", index=False)

    print(f"\nExcel validation summary written to: {out_path}")
    con.close()


if __name__ == "__main__":
    main()
