"""
Unit + integration tests for src/build_ndc.py, run against a real openFDA
NDC directory response (fixtures/ndc_directory_sample.json, captured live
from api.fda.gov/drug/ndc.json?search=product_ndc:"0703-4246") joined
against the real shortages fixture (fixtures/drug_shortages_sample.json),
which shares the same NDC (Carboplatin, package_ndc 0703-4246-01).

Run with: python3 tests/test_build_ndc.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import build_database as bd
import build_ndc as bn

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def test_to_rows_expands_packaging_and_dedupes():
    import json
    records = json.load(open(os.path.join(FIXTURES, "ndc_directory_sample.json")))
    rows = bn.to_rows(records)
    assert len(rows) == 2  # one row per distinct package_ndc
    package_ndcs = {r[1] for r in rows}
    assert package_ndcs == {"0703-4246-81", "0703-4246-01"}
    brand_by_pkg = {r[1]: r[3] for r in rows}
    assert brand_by_pkg["0703-4246-01"] == "CARBOplatin"
    assert brand_by_pkg["0703-4246-81"] == "Carboplatin"


def test_full_pipeline_backfills_proprietary_name():
    """
    Loads the real shortages fixture through build_database.py, then the
    real NDC fixture through build_ndc.py, and checks that
    shortages.proprietary_name gets backfilled correctly for the one
    matching package_ndc (0703-4246-01, Carboplatin) -- and stays NULL for
    shortage records with no matching NDC directory entry (the other two
    fixture rows), rather than picking an arbitrary brand_name.
    """
    import tempfile
    import duckdb

    scratch = tempfile.mkdtemp()
    raw_dir = os.path.join(scratch, "data", "raw")
    os.makedirs(raw_dir)
    shutil.copy(os.path.join(FIXTURES, "drug_shortages_sample.json"),
                os.path.join(raw_dir, "drug_shortages_2026-08-24.json"))
    shutil.copy(os.path.join(FIXTURES, "ndc_directory_sample.json"),
                os.path.join(raw_dir, "ndc_directory_2026-08-24.json"))

    db_path_str = os.path.join(scratch, "db", "supply_risk.duckdb")

    old_bd_raw, old_bd_db = bd.RAW_DIR, bd.DB_PATH
    old_bn_raw, old_bn_db = bn.RAW_DIR, bn.DB_PATH
    bd.RAW_DIR = type(old_bd_raw)(raw_dir)
    bd.DB_PATH = type(old_bd_db)(db_path_str)
    bn.RAW_DIR = type(old_bn_raw)(raw_dir)
    bn.DB_PATH = type(old_bn_db)(db_path_str)
    try:
        bd.main()
        bn.main()
        con = duckdb.connect(db_path_str)
        rows = con.execute(
            "SELECT record_id, generic_name, proprietary_name FROM shortages ORDER BY record_id"
        ).fetchall()
        con.close()
    finally:
        bd.RAW_DIR, bd.DB_PATH = old_bd_raw, old_bd_db
        bn.RAW_DIR, bn.DB_PATH = old_bn_raw, old_bn_db

    by_id = {r[0]: r for r in rows}
    assert by_id["0703-4246-01"][2] == "CARBOplatin"    # backfilled from the real NDC fixture
    assert by_id["42806-799-60"][2] is None              # no matching NDC record in the fixture
    assert by_id["13668-385-90"][2] is None


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
