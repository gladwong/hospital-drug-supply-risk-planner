"""
Unit + integration tests for src/build_seasonal.py, run against a real
data.cdc.gov RESP-NET response sample (fixtures/seasonal_surveillance_sample.json,
captured live from data.cdc.gov/resource/kvib-3txy.json on 2026-08-25 --
after CDC reshaped this dataset's columns, see the schema-change note at
the top of build_seasonal.py and fetch_seasonal.py).

Run with: python3 tests/test_build_seasonal.py
"""
import datetime as dt
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import build_seasonal as bs

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def test_to_date():
    assert bs.to_date("2018-10-06T00:00:00.000") == "2018-10-06"
    assert bs.to_date(None) is None


def test_to_float():
    assert bs.to_float("5.5") == 5.5
    assert bs.to_float("") is None
    assert bs.to_float(None) is None


def test_to_int():
    assert bs.to_int("2018") == 2018
    assert bs.to_int("") is None


def test_full_pipeline_against_real_fixture():
    """
    The fixture has 5 raw (long-format) records: two complete
    (Weekly Rate + Cumulative Rate) pairs -- race='Black, NH' and
    race='White, NH', both for RSV-NET/2017-01-07 -- plus one lone
    Cumulative Rate record for race='Hispanic' with no matching Weekly
    Rate. That should pivot down to exactly 3 rows: two with both
    weekly_rate and cumulative_rate populated, one with only
    cumulative_rate and weekly_rate left NULL.
    """
    import tempfile
    import duckdb

    scratch = tempfile.mkdtemp()
    raw_dir = os.path.join(scratch, "data", "raw")
    os.makedirs(raw_dir)
    fixture_path = os.path.join(FIXTURES, "seasonal_surveillance_sample.json")
    shutil.copy(fixture_path, os.path.join(raw_dir, "seasonal_surveillance_2026-08-24.json"))

    old_raw_dir, old_db_path = bs.RAW_DIR, bs.DB_PATH
    bs.RAW_DIR = type(old_raw_dir)(raw_dir)
    bs.DB_PATH = type(old_db_path)(os.path.join(scratch, "db", "supply_risk.duckdb"))
    try:
        bs.main()
        con = duckdb.connect(str(bs.DB_PATH))
        rows = con.execute(
            "SELECT race_ethnicity, surveillance_network, week_ending_date, mmwr_week, "
            "weekly_rate, cumulative_rate FROM seasonal_surveillance ORDER BY race_ethnicity"
        ).fetchall()
        con.close()
    finally:
        bs.RAW_DIR, bs.DB_PATH = old_raw_dir, old_db_path

    assert len(rows) == 3  # pivoted down from 5 raw long-format records
    by_race = {r[0]: r for r in rows}

    black = by_race["Black, NH"]
    assert black[1:4] == ("RSV-NET", dt.date(2017, 1, 7), None)  # mmwr_week no longer provided
    assert abs(black[4] - 0.5647148823644536) < 1e-9   # weekly_rate
    assert abs(black[5] - 4.832417410959497) < 1e-9    # cumulative_rate

    white = by_race["White, NH"]
    assert abs(white[4] - 0.6740588946482082) < 1e-9
    assert abs(white[5] - 2.353070401540484) < 1e-9

    hispanic = by_race["Hispanic"]
    assert hispanic[4] is None                          # no Weekly Rate record for this one
    assert abs(hispanic[5] - 3.261602380853412) < 1e-9


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
