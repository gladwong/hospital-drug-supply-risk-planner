"""
build_demand_from_mimic.py

Turns real hospital medication-order data from MIMIC-IV (Beth Israel
Deaconess Medical Center, a Harvard Medical School teaching hospital,
maintained by MIT's Laboratory for Computational Physiology on PhysioNet)
into a demand_inputs CSV in the exact shape src/risk_model.py expects
(record_id, avg_weekly_demand_units, demand_std_units, lead_time_weeks,
lead_time_std_weeks, service_level) -- replacing the synthetic
demand_inputs_sample.csv with real per-drug consumption stats keyed by NDC.

WHY MIMIC-IV: this project's shortages table is keyed by real US openFDA
package_ndc codes. MIMIC-IV's hosp/prescriptions table carries a real `ndc`
field on real (de-identified) medication orders from one named, well-known
US academic hospital -- so it joins directly to our existing schema with no
cross-country/cross-coding-system mapping (unlike e.g. AmsterdamUMCdb or
HiRID, which use ATC codes from European hospitals).

Two tiers, same file format, same script:
  - MIMIC-IV Clinical Database Demo (100 patients) -- free, no application,
    download directly: https://physionet.org/content/mimic-iv-demo/2.2/
    file: hosp/prescriptions.csv.gz
  - Full MIMIC-IV (364k+ patients) -- requires PhysioNet credentialing
    (free CITI training + signed Data Use Agreement, ~days to a week).
    Same file, same column names, vastly more volume -> less noisy stats.

--------------------------------------------------------------------------
IMPORTANT -- built against DOCUMENTED schema, not yet run against a real
file (this sandbox cannot reach physionet.org; see README). Columns below
are taken from the official MIMIC-IV docs
(https://mimic.mit.edu/docs/iv/modules/hosp/prescriptions):

  subject_id, hadm_id, pharmacy_id, poe_id, poe_seq, order_provider_id,
  starttime, stoptime, drug_type, drug, formulary_drug_cd, gsn, ndc,
  prod_strength, form_rx, dose_val_rx, dose_unit_rx, form_val_disp,
  form_unit_disp, doses_per_24_hrs, route

This has been unit-tested against a hand-built fixture matching that
schema (fixtures/mimic_prescriptions_sample.csv), but -- exactly like
fetch_seasonal.py's CDC surprise -- real exports can differ from
documentation in ways that only show up against a live file (missing
columns, unexpected NaN encoding for `ndc`, gzip vs plain csv, a quoting
quirk). Run it against your real download and treat any mismatch the same
way we treated the CDC schema change: read the error, fix the parsing,
document it here.
--------------------------------------------------------------------------

NDC MATCHING -- the tricky part
--------------------------------
openFDA's package_ndc (our shortages.record_id) is dash-formatted with
THREE different possible segment layouts: 4-4-2, 5-3-2, or 5-4-1 digits
(labeler-product-package). MIMIC's `ndc` field is a bare digit string with
no dashes, and is usually already normalized to 11 digits (the FDA's
"5-4-2" standard length) -- but may occasionally be 10 digits, or 0/blank
for orders with no assigned NDC (compounded drugs, etc).

Naively left-padding a 10-digit code to 11 digits is WRONG unless the
labeler segment happens to be the short one -- for 5-3-2 or 5-4-1 layouts
the missing zero belongs in a different segment. Since our shortages side
still has its dashes, we can do this correctly there: pad whichever
segment is short based on the actual dash positions, rather than guessing.
On the MIMIC side, 11-digit codes are used as-is; a 10-digit MIMIC code is
logged as unresolved rather than guessed at, since MIMIC doesn't give us
its segment boundaries -- confirm live whether this case actually occurs
in your real file before deciding how to handle it.

USAGE
-------
    python3 src/build_demand_from_mimic.py \\
        --prescriptions data/raw/mimic_prescriptions.csv.gz \\
        --out demand_inputs_mimic.csv \\
        --db db/supply_risk.duckdb

    # then point risk_model.py at the real output:
    python3 src/risk_model.py --demand demand_inputs_mimic.csv

Assumptions this script does NOT get from MIMIC (nothing public tracks
this -- see README):
  - lead_time_weeks / lead_time_std_weeks: no public dataset records
    hospital-to-supplier replenishment lead times. Kept as a clearly
    labeled default assumption (2.0 / 0.5 weeks), same honesty standard
    as the original demand_inputs_sample.csv.
  - service_level: defaulted to 0.95, overridable per-row if you have
    real target service levels for specific drugs.
"""

import argparse
import csv
import gzip
import re
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "supply_risk.duckdb"
DEFAULT_OUT = ROOT / "demand_inputs_mimic.csv"

# Not derivable from any public dataset -- see module docstring.
DEFAULT_LEAD_TIME_WEEKS = 2.0
DEFAULT_LEAD_TIME_STD_WEEKS = 0.5
DEFAULT_SERVICE_LEVEL = 0.95

_DIGITS_RE = re.compile(r"\D+")


def normalize_dashed_ndc(package_ndc: str):
    """
    Correctly convert a DASHED openFDA package_ndc (4-4-2, 5-3-2, or 5-4-1)
    to the standard 11-digit (5-4-2) form by zero-padding whichever segment
    is short -- using the real segment boundaries from the dashes, not a
    naive whole-string pad (which is only correct for the 4-4-2 case).
    """
    if not package_ndc:
        return None
    parts = package_ndc.strip().split("-")
    if len(parts) != 3:
        return None
    labeler, product, pkg = parts
    if not (labeler.isdigit() and product.isdigit() and pkg.isdigit()):
        return None
    labeler = labeler.zfill(5)
    product = product.zfill(4)
    pkg = pkg.zfill(2)
    return labeler + product + pkg


def normalize_mimic_ndc(raw_ndc: str):
    """
    MIMIC's ndc column is a bare digit string, normally already 11 digits.
    0 / blank / non-digit means "no NDC assigned" (e.g. compounded drugs).
    A 10-digit value is logged as unresolved rather than guessed at -- see
    module docstring; confirm live whether this case actually occurs.
    """
    if raw_ndc is None:
        return None, None
    digits = _DIGITS_RE.sub("", str(raw_ndc))
    if not digits or int(digits) == 0:
        return None, None
    if len(digits) == 11:
        return digits, None
    if len(digits) == 10:
        return None, f"unresolved 10-digit NDC (ambiguous segment padding): {raw_ndc}"
    return None, f"unexpected NDC length ({len(digits)} digits): {raw_ndc}"


def parse_dt(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[: len(fmt) + 2].split(".")[0], fmt)
        except ValueError:
            continue
    return None


def iso_week_key(dt):
    y, w, _ = dt.isocalendar()
    return (y, w)


def open_maybe_gz(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", newline="")
    return open(path, "rt", newline="")


def load_shortage_ndcs(db_path):
    """record_id (openFDA package_ndc, dashed) -> normalized 11-digit NDC."""
    con = duckdb.connect(str(db_path), read_only=True)
    ids = [r[0] for r in con.execute("SELECT DISTINCT record_id FROM shortages").fetchall()]
    con.close()
    mapping = {}
    for rid in ids:
        norm = normalize_dashed_ndc(rid)
        if norm:
            mapping.setdefault(norm, rid)  # first record_id wins on collision
    return mapping


def aggregate_weekly_demand(prescriptions_path, shortage_ndc_map):
    """
    For each prescription row with a resolvable NDC matching a real
    shortage record: estimate the quantity dispensed as
        doses_per_24_hrs * form_val_disp * duration_days
    (form_val_disp = units per single dose, e.g. tablets or mL), bucket by
    ISO week of starttime, then compute per-drug weekly mean/std across
    weeks that had any activity.
    """
    weekly_totals = defaultdict(lambda: defaultdict(float))  # record_id -> {week: qty}
    unresolved_ndc_examples = {}
    rows_seen = 0
    rows_matched = 0

    with open_maybe_gz(Path(prescriptions_path)) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_seen += 1
            norm_ndc, issue = normalize_mimic_ndc(row.get("ndc"))
            if issue and len(unresolved_ndc_examples) < 5:
                unresolved_ndc_examples.setdefault(issue, 0)
                unresolved_ndc_examples[issue] += 1
            if not norm_ndc or norm_ndc not in shortage_ndc_map:
                continue

            start = parse_dt(row.get("starttime"))
            stop = parse_dt(row.get("stoptime")) or start
            if not start:
                continue
            duration_days = max((stop - start).total_seconds() / 86400.0, 1.0) if stop else 1.0

            try:
                doses_per_day = float(row.get("doses_per_24_hrs") or 1.0)
            except ValueError:
                doses_per_day = 1.0
            try:
                units_per_dose = float(row.get("form_val_disp") or 1.0)
            except ValueError:
                units_per_dose = 1.0

            qty = doses_per_day * units_per_dose * duration_days
            record_id = shortage_ndc_map[norm_ndc]
            weekly_totals[record_id][iso_week_key(start)] += qty
            rows_matched += 1

    return weekly_totals, rows_seen, rows_matched, unresolved_ndc_examples


def to_demand_rows(weekly_totals):
    rows = []
    for record_id, weeks in weekly_totals.items():
        values = list(weeks.values())
        if not values:
            continue
        avg = statistics.mean(values)
        std = statistics.stdev(values) if len(values) > 1 else 0.0
        rows.append({
            "record_id": record_id,
            "avg_weekly_demand_units": round(avg, 2),
            "demand_std_units": round(std, 2),
            "lead_time_weeks": DEFAULT_LEAD_TIME_WEEKS,
            "lead_time_std_weeks": DEFAULT_LEAD_TIME_STD_WEEKS,
            "service_level": DEFAULT_SERVICE_LEVEL,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prescriptions", required=True,
                     help="Path to MIMIC-IV hosp/prescriptions.csv or .csv.gz "
                          "(demo or full download).")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    print(f"Loading real shortage NDCs from {args.db} ...")
    shortage_ndc_map = load_shortage_ndcs(args.db)
    print(f"  {len(shortage_ndc_map)} distinct normalized NDCs in shortages table")

    print(f"Scanning {args.prescriptions} ...")
    weekly_totals, rows_seen, rows_matched, unresolved = aggregate_weekly_demand(
        args.prescriptions, shortage_ndc_map
    )
    print(f"  {rows_seen} prescription rows scanned, {rows_matched} matched a real shortage NDC")
    if unresolved:
        print("  NDC parsing issues seen (top examples):")
        for issue, count in unresolved.items():
            print(f"    {issue}  (~{count}+ occurrences)")

    demand_rows = to_demand_rows(weekly_totals)
    demand_rows.sort(key=lambda r: -r["avg_weekly_demand_units"])

    out_path = Path(args.out)
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "record_id", "avg_weekly_demand_units", "demand_std_units",
            "lead_time_weeks", "lead_time_std_weeks", "service_level",
        ])
        writer.writeheader()
        writer.writerows(demand_rows)

    print(f"\nWrote {len(demand_rows)} real per-drug demand rows to {out_path}")
    print("(lead_time_weeks/lead_time_std_weeks/service_level are still assumed "
          "defaults -- no public dataset tracks hospital replenishment lead times)")
    if not demand_rows:
        print("\nNo matches -- expected for the 100-patient demo against a large "
              "real-world shortage list; try the full MIMIC-IV dataset for more "
              "coverage, or check the NDC parsing issues above.")


if __name__ == "__main__":
    main()
