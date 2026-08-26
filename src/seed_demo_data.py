"""
seed_demo_data.py — DEMO / SAMPLE DATA ONLY.

This cloud sandbox's outbound network is restricted to package registries,
so api.fda.gov and data.cdc.gov aren't reachable from here. This script
writes a small, realistic-shaped synthetic dataset directly into the same
DuckDB tables fetch_shortages.py/build_database.py and
fetch_seasonal.py/build_seasonal.py produce, so Step 2 (exploratory SQL +
Excel summary) and Step 3 (risk model) can be built and sanity-checked
end-to-end in this environment.

Field names and value vocabularies match what the real fetch/build scripts
actually observed from the live APIs during development (see fixtures/ and
the "FIX NOTE" in build_database.py).

Run fetch_shortages.py + build_database.py and fetch_seasonal.py +
build_seasonal.py (anywhere with normal internet access) to replace this
with real data -- nothing downstream needs to change.
"""

import math
import random
from pathlib import Path
from datetime import date, timedelta

import duckdb

random.seed(42)

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "supply_risk.duckdb"

NETWORKS = ["FluSurv-NET", "RSV-NET", "COVID-NET", "Combined"]
NETWORK_PARAMS = {
    "FluSurv-NET": {"base": 3.0, "amp": 28.0, "peak_week": 2, "noise": 3.0},
    "RSV-NET":     {"base": 2.0, "amp": 18.0, "peak_week": 51, "noise": 2.5},
    "COVID-NET":   {"base": 4.0, "amp": 10.0, "peak_week": 3, "noise": 2.0},
    "Combined":    {"base": 9.0, "amp": 45.0, "peak_week": 2, "noise": 5.0},
}

DRUGS = [
    # (generic_name, dosage_form, strength, therapeutic_category, company)
    # therapeutic_category values are the real openFDA vocabulary (verified
    # live via count=therapeutic_category on api.fda.gov/drug/shortages.json)
    ("Oseltamivir Phosphate", "Capsule", "75 mg", "Antiviral", "Hetero Labs Limited"),
    ("Ceftriaxone Sodium", "Injection", "1 g/1", "Anti-Infective", "Asclemed USA, Inc."),
    ("Albuterol Sulfate", "Inhalation Solution", "0.083%", "Pulmonary/Allergy", "Nephron Pharmaceuticals"),
    ("Amoxicillin", "Suspension", "400 mg/5 mL", "Anti-Infective", "Amneal Pharmaceuticals"),
    ("Azithromycin", "Tablet", "250 mg", "Anti-Infective", "Teva Pharmaceuticals USA, Inc."),
    ("Sodium Chloride", "Injection", "0.9%", "Pulmonary/Allergy", "ICU Medical, Inc."),
    ("Carboplatin", "Injection", "10 mg/1 mL", "Oncology", "Teva Pharmaceuticals USA, Inc."),
    ("Cisplatin", "Injection", "1 mg/1 mL", "Oncology", "Fresenius Kabi USA"),
    ("Amlodipine Besylate", "Tablet", "10 mg", "Cardiovascular", "Torrent Pharma Inc."),
    ("Metoprolol Tartrate", "Injection", "1 mg/1 mL", "Cardiovascular", "Amphastar Pharmaceuticals"),
    ("Lorazepam", "Injection", "2 mg/1 mL", "Neurology", "Hikma Pharmaceuticals"),
    ("Levetiracetam", "Injection", "500 mg/5 mL", "Neurology", "Fresenius Kabi USA"),
    ("Insulin Human", "Injection", "100 units/mL", "Endocrinology/Metabolism", "Novo Nordisk"),
    ("Hydrocortisone Sodium Succinate", "Injection", "100 mg", "Endocrinology/Metabolism", "Pfizer Inc."),
    ("Propofol", "Injection", "10 mg/mL", "Anesthesia", "Fresenius Kabi USA"),
    ("Succinylcholine Chloride", "Injection", "20 mg/mL", "Anesthesia", "Hospira, Inc."),
    ("Vancomycin", "Injection", "1 g", "Anti-Infective", "Fresenius Kabi USA"),
    ("Piperacillin; Tazobactam", "Injection", "4.5 g", "Anti-Infective", "Fresenius Kabi USA"),
    ("Heparin Sodium", "Injection", "5000 units/mL", "Hematology", "Pfizer Inc."),
    ("Epoetin Alfa", "Injection", "4000 units/mL", "Hematology", "Amgen Inc."),
]
STATUSES = ["Current", "Current", "Resolved", "To Be Discontinued"]


def gen_seasonal_rows():
    rows = []
    start, end = date(2019, 10, 1), date(2025, 9, 30)
    for net in NETWORKS:
        p = NETWORK_PARAMS[net]
        d = start
        while d <= end:
            mmwr_week = int(d.strftime("%U")) or 52
            delta = min(abs(mmwr_week - p["peak_week"]), 52 - abs(mmwr_week - p["peak_week"]))
            seasonal = p["amp"] * math.exp(-(delta ** 2) / (2 * 8.0 ** 2))
            weekly_rate = max(0.0, p["base"] + seasonal + random.gauss(0, p["noise"]))
            season_label = f"{d.year % 100:02d}{(d.year + 1) % 100:02d}" if d.month >= 10 else f"{(d.year - 1) % 100:02d}{d.year % 100:02d}"
            rows.append((net, season_label, d.isoformat(), d.year, mmwr_week,
                         "Overall", "Overall", "Overall", "Overall",
                         round(weekly_rate, 2), None, "Observed"))
            d += timedelta(days=7)
    return rows


def gen_shortage_rows():
    shortage_rows, category_rows = [], []
    for i, (name, form, strength, cat, company) in enumerate(DRUGS):
        record_id = f"{50000+i}-{100+i}-01"
        status = STATUSES[i % len(STATUSES)]
        initial_post = date(2024, 1, 1) + timedelta(days=i * 17)
        update = initial_post + timedelta(days=random.randint(10, 400))
        discontinued = update if status == "To Be Discontinued" else None
        shortage_rows.append((
            record_id, name, None, strength, form, record_id, company,
            "800-000-0000", status,
            "Available" if status != "To Be Discontinued" else "Limited Availability",
            "New" if i % 3 == 0 else "Revised",
            initial_post.isoformat(), update.isoformat(),
            discontinued.isoformat() if discontinued else None,
            "Increased seasonal demand" if status == "Current" else None,
        ))
        category_rows.append((record_id, cat))
    return shortage_rows, category_rows


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))

    # reuse the real schemas so this stays a drop-in stand-in
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    import build_database as bd
    import build_seasonal as bs
    con.execute(bd.SCHEMA_SQL)
    con.execute(bs.SCHEMA_SQL)

    seasonal = gen_seasonal_rows()
    con.execute("DELETE FROM seasonal_surveillance")
    con.executemany("INSERT INTO seasonal_surveillance VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", seasonal)

    shortage_rows, category_rows = gen_shortage_rows()
    con.execute("DELETE FROM shortages")
    con.execute("DELETE FROM shortage_categories")
    con.executemany(
        "INSERT INTO shortages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", shortage_rows
    )
    con.executemany("INSERT INTO shortage_categories VALUES (?,?)", category_rows)

    print(f"Seeded DEMO data: {len(seasonal)} seasonal_surveillance rows, "
          f"{len(shortage_rows)} shortages, {len(category_rows)} shortage_categories rows.")
    con.close()


if __name__ == "__main__":
    main()
