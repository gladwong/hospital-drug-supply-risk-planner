"""
Unit + integration tests for src/build_demand_from_mimic.py, run against a
hand-built fixture matching MIMIC-IV's documented prescriptions.csv schema
(fixtures/mimic_prescriptions_sample.csv) and three REAL openFDA package_ndc
values already used elsewhere in this project's fixtures
(fixtures/drug_shortages_sample.json), chosen specifically to cover all
three NDC dash-segment layouts:

  0703-4246-01   (4-4-2 -- short segment is the labeler)
  42806-799-60   (5-3-2 -- short segment is the product code; this is the
                  case where naively left-padding the whole digit string
                  gives the WRONG 11-digit NDC, so it's an important case
                  to cover)
  13668-385-90   (5-3-2 again, different labeler length pattern)

Run with: python3 tests/test_build_demand_from_mimic.py
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import build_demand_from_mimic as bdm

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def test_normalize_dashed_ndc_442():
    # labeler segment (4 digits) is the short one -- pads to 5
    assert bdm.normalize_dashed_ndc("0703-4246-01") == "00703424601"


def test_normalize_dashed_ndc_532_product_short():
    # product segment (3 digits) is the short one -- naive whole-string
    # left-pad of "4280679960" (10 digits) would give "04280679960", which
    # is WRONG. The correct, segment-aware answer pads the product segment:
    assert bdm.normalize_dashed_ndc("42806-799-60") == "42806079960"
    naive_wrong = "4280679960".zfill(11)
    assert bdm.normalize_dashed_ndc("42806-799-60") != naive_wrong


def test_normalize_dashed_ndc_532_second_case():
    assert bdm.normalize_dashed_ndc("13668-385-90") == "13668038590"


def test_normalize_dashed_ndc_malformed():
    assert bdm.normalize_dashed_ndc("") is None
    assert bdm.normalize_dashed_ndc("not-an-ndc-value") is None
    assert bdm.normalize_dashed_ndc("12345") is None  # no dashes at all


def test_normalize_mimic_ndc():
    assert bdm.normalize_mimic_ndc("00703424601") == ("00703424601", None)
    norm, issue = bdm.normalize_mimic_ndc("0")
    assert norm is None and issue is None  # "no NDC assigned" is not an error
    norm, issue = bdm.normalize_mimic_ndc(None)
    assert norm is None and issue is None
    norm, issue = bdm.normalize_mimic_ndc("1234567890")  # 10 digits -- ambiguous
    assert norm is None and issue is not None and "10-digit" in issue
    norm, issue = bdm.normalize_mimic_ndc("123")  # implausible length
    assert norm is None and issue is not None


def _build_shortage_ndc_map():
    # Mirrors load_shortage_ndcs() without needing a real DuckDB file --
    # exercises the same normalize_dashed_ndc() path against the three
    # real record_ids this project's shortages table actually contains.
    real_record_ids = ["0703-4246-01", "42806-799-60", "13668-385-90"]
    mapping = {}
    for rid in real_record_ids:
        norm = bdm.normalize_dashed_ndc(rid)
        mapping[norm] = rid
    return mapping


def test_aggregate_weekly_demand_against_real_fixture():
    """
    The fixture has 8 prescription rows:
      - 3 Carboplatin rows (NDC 0703-4246-01) across 2 different ISO weeks
      - 1 Rifampin row (NDC 42806-799-60), 1 week only
      - 1 Amlodipine-combo row (NDC 13668-385-90), 1 week only
      - 1 row with ndc=0 (no NDC assigned -- must be silently skipped)
      - 1 row with a valid-looking 11-digit NDC that isn't a real shortage
        (must be skipped, not matched)
      - 1 row with an ambiguous 10-digit NDC (must be logged as unresolved,
        not matched, and must not crash)
    """
    shortage_ndc_map = _build_shortage_ndc_map()
    fixture_path = os.path.join(FIXTURES, "mimic_prescriptions_sample.csv")

    weekly_totals, rows_seen, rows_matched, unresolved = bdm.aggregate_weekly_demand(
        fixture_path, shortage_ndc_map
    )

    assert rows_seen == 8
    assert rows_matched == 5  # 3 Carboplatin + 1 Rifampin + 1 Amlodipine
    assert any("10-digit" in issue for issue in unresolved)

    assert set(weekly_totals.keys()) == {"0703-4246-01", "42806-799-60", "13668-385-90"}

    carbo_weeks = weekly_totals["0703-4246-01"]
    assert len(carbo_weeks) == 2  # two distinct ISO weeks
    week_totals = sorted(carbo_weeks.values())
    # row1: 2 doses/day * 5 mL/dose * 2 days = 20
    # row2: 1 dose/day * 10 mL/dose * 1 day (stop==start, clamped) = 10  -> same week as row1: 30
    # row3: 3 doses/day * 2 mL/dose * 1 day = 6  -> different week
    assert abs(sum(week_totals) - 36.0) < 1e-9
    assert abs(min(week_totals) - 6.0) < 1e-9
    assert abs(max(week_totals) - 30.0) < 1e-9

    rif_weeks = weekly_totals["42806-799-60"]
    assert len(rif_weeks) == 1
    assert abs(list(rif_weeks.values())[0] - 2.0) < 1e-9  # 2 doses/day * 1 CAP * 1 day

    amlo_weeks = weekly_totals["13668-385-90"]
    assert len(amlo_weeks) == 1
    assert abs(list(amlo_weeks.values())[0] - 1.0) < 1e-9  # 1 dose/day * 1 TAB * 1 day


def test_to_demand_rows_stats():
    shortage_ndc_map = _build_shortage_ndc_map()
    fixture_path = os.path.join(FIXTURES, "mimic_prescriptions_sample.csv")
    weekly_totals, _, _, _ = bdm.aggregate_weekly_demand(fixture_path, shortage_ndc_map)
    rows = {r["record_id"]: r for r in bdm.to_demand_rows(weekly_totals)}

    assert set(rows.keys()) == {"0703-4246-01", "42806-799-60", "13668-385-90"}

    carbo = rows["0703-4246-01"]
    expected_avg = statistics.mean([30.0, 6.0])
    expected_std = statistics.stdev([30.0, 6.0])
    assert abs(carbo["avg_weekly_demand_units"] - round(expected_avg, 2)) < 1e-6
    assert abs(carbo["demand_std_units"] - round(expected_std, 2)) < 1e-6

    rif = rows["42806-799-60"]
    assert abs(rif["avg_weekly_demand_units"] - 2.0) < 1e-9
    assert rif["demand_std_units"] == 0.0  # only one week of data -> no variance

    # every row carries the documented default assumptions, unchanged
    for r in rows.values():
        assert r["lead_time_weeks"] == bdm.DEFAULT_LEAD_TIME_WEEKS
        assert r["lead_time_std_weeks"] == bdm.DEFAULT_LEAD_TIME_STD_WEEKS
        assert r["service_level"] == bdm.DEFAULT_SERVICE_LEVEL


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
