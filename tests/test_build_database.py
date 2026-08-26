"""
Unit + integration tests for src/build_database.py, run against a real
openFDA response sample (fixtures/drug_shortages_sample.json, captured live
from api.fda.gov/drug/shortages.json).

This sandbox's outbound network is restricted to package registries, so
these tests exercise the pipeline against a saved real fixture rather than
hitting the live API. Run src/fetch_shortages.py directly in an environment
with open internet access to pull the real, current dataset.

Run with: python3 tests/test_build_database.py
"""
import datetime as dt
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import build_database as bd

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def test_to_date_real_format():
    assert bd.to_date("04/28/2023") == "2023-04-28"
    assert bd.to_date("6/1/2025") == "2025-06-01"


def test_to_date_handles_missing_and_bad_input():
    assert bd.to_date(None) is None
    assert bd.to_date("") is None
    assert bd.to_date("20230428") is None  # old (wrong) format should NOT parse as valid


def test_extract_strength_single_ingredient():
    p = "Carboplatin, Injection, 10 mg/1 mL (NDC 0703-4246-01)"
    assert bd.extract_strength(p) == "10 mg/1 mL"


def test_extract_strength_multi_ingredient():
    p = ("Amlodipine Besylate; Hydrochlorothiazide; Olmesartan Medoxomil, Tablet, "
         "5 mg; 25 mg; 40 mg (NDC 13668-385-90)")
    assert bd.extract_strength(p) == "5 mg; 25 mg; 40 mg"


def test_extract_strength_missing():
    assert bd.extract_strength(None) is None
    assert bd.extract_strength("") is None


def test_full_pipeline_against_real_fixture(tmp_path=None):
    """
    Copies the real fixture into a scratch data/raw/ dir, points
    build_database at a scratch DB, runs main(), and checks the loaded
    rows -- including that dates and strength (previously always-NULL
    bugs) now populate correctly.
    """
    import tempfile
    import duckdb

    scratch = tempfile.mkdtemp()
    raw_dir = os.path.join(scratch, "data", "raw")
    os.makedirs(raw_dir)
    fixture_path = os.path.join(FIXTURES, "drug_shortages_sample.json")
    shutil.copy(fixture_path, os.path.join(raw_dir, "drug_shortages_2026-08-24.json"))

    old_raw_dir, old_db_path = bd.RAW_DIR, bd.DB_PATH
    bd.RAW_DIR = type(old_raw_dir)(raw_dir)
    bd.DB_PATH = type(old_db_path)(os.path.join(scratch, "db", "supply_risk.duckdb"))
    try:
        bd.main()
        con = duckdb.connect(str(bd.DB_PATH))
        rows = con.execute(
            "SELECT record_id, generic_name, strength, initial_posting_date, "
            "company_name, resolved_note, status FROM shortages ORDER BY record_id"
        ).fetchall()
        con.close()
    finally:
        bd.RAW_DIR, bd.DB_PATH = old_raw_dir, old_db_path

    assert len(rows) == 3
    by_id = {r[0]: r for r in rows}

    carbo = by_id["0703-4246-01"]
    assert carbo[1] == "Carboplatin Injection"
    assert carbo[2] == "10 mg/1 mL"                      # strength, was always None before the fix
    assert carbo[3] == dt.date(2023, 4, 28)                # date, was always None before the fix
    assert carbo[4] == "Teva Pharmaceuticals USA, Inc."   # company_name, new column
    assert carbo[6] == "Current"

    discontinued = by_id["13668-385-90"]
    assert discontinued[5] == "A business decision was made to discontinue manufacture of the product."


def _run_all():
    import inspect
    failures = 0
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
