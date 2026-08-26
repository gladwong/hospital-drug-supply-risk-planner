"""
risk_model.py — Step 3: category risk scoring + volatility-adjusted safety
stock / reorder points.

Writes two output tables to the database:

  category_risk_scores   -- one row per therapeutic_category: how exposed
                             is this category right now, combining current
                             shortage severity with its seasonal signal.
  drug_risk_scores        -- one row per drug (record_id): safety stock and
                             reorder point, both a plain baseline and a
                             volatility-adjusted version.

SAFETY STOCK / REORDER POINT FORMULA
--------------------------------------
Standard combined demand/lead-time-variability safety stock formula:

    SS = Z * sqrt( LT_avg * sigma_D^2  +  D_avg^2 * sigma_LT^2 )
    ROP = D_avg * LT_avg + SS

Z is the service-level z-score; LT_avg/sigma_LT are lead time mean/std (in
weeks, matching weekly demand units); D_avg/sigma_D are weekly demand
mean/std, all supplied by the hospital via --demand (this tool has no
access to real pharmacy/ERP consumption data -- demand_inputs_sample.csv
is illustrative only).

`base_*` uses the raw CSV inputs unmodified. `volatility_adjusted_*` uses:
    sigma_D_effective  = sigma_D * seasonal_amplification_factor
    LT_avg_effective   = LT_avg  * supply_lead_time_multiplier
    sigma_LT_effective = sigma_LT * supply_risk_multiplier

seasonal_amplification_factor = 1 + seasonal_cv of the drug's mapped CDC
network (only for categories mapped in category_surveillance_map -- see
sql/02_exploratory_views.sql). supply_risk_multiplier/lead-time multipliers
come from the drug's openFDA shortage status: an active shortage manifests
mainly as longer, less predictable replenishment, so it inflates the
lead-time side of the formula rather than the demand side.

CATEGORY RISK SCORE
----------------------
category_risk_score (0-100) blends, per therapeutic_category:
  - current-shortage severity: share of that category's tracked shortage
    records that are Current or To Be Discontinued
  - seasonal exposure: the mapped network's coefficient of variation
    (0 if the category has no mapped network)

LIMITATIONS
-------------
- Planning heuristic, not a validated supply-chain optimization or
  clinical model -- use for triage, not automatic reordering.
- category_surveillance_map is a coarse, manually curated mapping; review
  it for your formulary.
- National CDC rates are a proxy for local demand pressure.

Usage:
    python3 src/risk_model.py [--db db/supply_risk.duckdb] [--demand demand_inputs_sample.csv]
"""

import argparse
import csv
import math
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "supply_risk.duckdb"
SQL_PATH = ROOT / "sql" / "02_exploratory_views.sql"
DEFAULT_DEMAND = ROOT / "demand_inputs_sample.csv"

RISK_TABLES_SQL = """
CREATE OR REPLACE TABLE category_risk_scores (
    therapeutic_category    VARCHAR PRIMARY KEY,
    n_shortage_records        INTEGER,
    n_current                  INTEGER,
    n_discontinuing              INTEGER,
    shortage_severity              DOUBLE,   -- (n_current + n_discontinuing) / n_shortage_records
    mapped_surveillance_network      VARCHAR,
    seasonal_cv                       DOUBLE,
    category_risk_score                DOUBLE
);

CREATE OR REPLACE TABLE drug_risk_scores (
    record_id                          VARCHAR PRIMARY KEY,
    generic_name                         VARCHAR,
    therapeutic_category                   VARCHAR,
    shortage_status                          VARCHAR,
    mapped_surveillance_network                VARCHAR,
    seasonal_amplification_factor                DOUBLE,
    supply_risk_multiplier                         DOUBLE,
    avg_weekly_demand                                DOUBLE,
    demand_std                                         DOUBLE,
    lead_time_weeks                                      DOUBLE,
    lead_time_std_weeks                                    DOUBLE,
    service_level                                            DOUBLE,
    z_score                                                    DOUBLE,
    base_safety_stock                                            DOUBLE,
    base_reorder_point                                             DOUBLE,
    volatility_adjusted_safety_stock                                 DOUBLE,
    volatility_adjusted_reorder_point                                  DOUBLE,
    composite_risk_score                                                 DOUBLE
);
"""


# ---- inverse normal CDF (Acklam's rational approximation, no scipy needed) ----
def norm_ppf(p):
    if not (0.0 < p < 1.0):
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    elif p <= p_high:
        q = p - 0.5
        r = q*q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


# Supply-risk multipliers, keyed by openFDA shortage status. "Current" = an
# active shortage: replenishment lead times run long and unpredictable.
# "To Be Discontinued" is treated as worse -- the product is going away, so
# safety stock alone doesn't solve it (should trigger a manual "find an
# alternate supplier / therapeutic substitute" flag, not just more stock).
SUPPLY_RISK_PARAMS = {
    "Current":            {"lt_mean_mult": 1.3, "lt_std_mult": 1.8, "risk_component": 0.7},
    "To Be Discontinued": {"lt_mean_mult": 1.5, "lt_std_mult": 2.2, "risk_component": 1.0},
    "Resolved":           {"lt_mean_mult": 1.0, "lt_std_mult": 1.0, "risk_component": 0.2},
}
DEFAULT_SUPPLY_RISK = {"lt_mean_mult": 1.0, "lt_std_mult": 1.0, "risk_component": 0.0}


def load_demand_csv(path):
    rows = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows[r["record_id"]] = {
                "avg_weekly_demand_units": float(r["avg_weekly_demand_units"]),
                "demand_std_units": float(r["demand_std_units"]),
                "lead_time_weeks": float(r["lead_time_weeks"]),
                "lead_time_std_weeks": float(r["lead_time_std_weeks"]),
                "service_level": float(r.get("service_level") or 0.95),
            }
    return rows


def compute_category_scores(con):
    rows = con.execute(
        """SELECT therapeutic_category, n_shortage_records, n_current, n_discontinuing,
                  mapped_surveillance_network, seasonal_cv
           FROM v_category_shortage_summary"""
    ).fetchall()
    out = []
    for cat, n_total, n_current, n_disc, network, cv in rows:
        severity = (n_current + n_disc) / n_total if n_total else 0.0
        seasonal_norm = min(cv, 1.0) if cv is not None else 0.0
        score = 100.0 * (0.6 * severity + 0.4 * seasonal_norm)
        out.append((cat, n_total, n_current, n_disc, round(severity, 4),
                     network, cv, round(score, 1)))
    return out


def compute_drug_scores(con, demand):
    cols_cur = con.execute(
        """SELECT record_id, generic_name, therapeutic_category, shortage_status,
                  mapped_surveillance_network, seasonal_cv
           FROM v_drug_seasonal_risk_inputs"""
    )
    cols = [c[0] for c in cols_cur.description]
    by_id = {dict(zip(cols, r))["record_id"]: dict(zip(cols, r)) for r in cols_cur.fetchall()}

    out = []
    unmatched = []
    for record_id, d in demand.items():
        info = by_id.get(record_id)
        if info is None:
            unmatched.append(record_id)
            info = {"generic_name": None, "therapeutic_category": None,
                     "shortage_status": None, "mapped_surveillance_network": None,
                     "seasonal_cv": None}

        z = norm_ppf(d["service_level"])
        d_avg, d_std = d["avg_weekly_demand_units"], d["demand_std_units"]
        lt_avg, lt_std = d["lead_time_weeks"], d["lead_time_std_weeks"]

        base_ss = z * math.sqrt(lt_avg * d_std**2 + d_avg**2 * lt_std**2)
        base_rop = d_avg * lt_avg + base_ss

        seasonal_cv = info.get("seasonal_cv")
        seasonal_amp = (1.0 + seasonal_cv) if (info.get("mapped_surveillance_network") and seasonal_cv is not None) else 1.0

        supply = SUPPLY_RISK_PARAMS.get(info.get("shortage_status"), DEFAULT_SUPPLY_RISK)
        d_std_eff = d_std * seasonal_amp
        lt_avg_eff = lt_avg * supply["lt_mean_mult"]
        lt_std_eff = lt_std * supply["lt_std_mult"]

        vol_ss = z * math.sqrt(lt_avg_eff * d_std_eff**2 + d_avg**2 * lt_std_eff**2)
        vol_rop = d_avg * lt_avg_eff + vol_ss

        ss_inflation_norm = min(max((vol_ss / base_ss - 1.0) if base_ss > 0 else 0.0, 0.0), 1.0)
        seasonal_norm = min(max(seasonal_amp - 1.0, 0.0), 1.0)
        composite = 100.0 * (0.45 * supply["risk_component"] + 0.30 * seasonal_norm + 0.25 * ss_inflation_norm)

        out.append((
            record_id, info.get("generic_name"), info.get("therapeutic_category"),
            info.get("shortage_status"), info.get("mapped_surveillance_network"),
            round(seasonal_amp, 4), round(supply["lt_std_mult"], 4),
            d_avg, d_std, lt_avg, lt_std, d["service_level"], round(z, 4),
            round(base_ss, 1), round(base_rop, 1),
            round(vol_ss, 1), round(vol_rop, 1), round(composite, 1),
        ))
    return out, unmatched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--demand", default=str(DEFAULT_DEMAND),
                     help="CSV of hospital-supplied demand/lead-time stats keyed by record_id. "
                          "Defaults to the illustrative sample -- pass your real consumption "
                          "data for meaningful output.")
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    con.execute(SQL_PATH.read_text())
    con.execute(RISK_TABLES_SQL)

    cat_scores = compute_category_scores(con)
    con.execute("DELETE FROM category_risk_scores")
    con.executemany(
        "INSERT INTO category_risk_scores VALUES (?,?,?,?,?,?,?,?)", cat_scores
    )

    demand = load_demand_csv(args.demand)
    drug_scores, unmatched = compute_drug_scores(con, demand)
    con.execute("DELETE FROM drug_risk_scores")
    con.executemany(
        "INSERT INTO drug_risk_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        drug_scores,
    )

    print(f"Computed {len(cat_scores)} category risk scores and {len(drug_scores)} drug risk scores.")
    if unmatched:
        print(f"  (no shortage record found for record_id(s): {', '.join(unmatched)} "
              f"-- treated as no active shortage, no category mapping)")

    print("\nCategory risk scores:")
    for row in sorted(cat_scores, key=lambda r: -r[-1]):
        print(f"  {row[7]:>5.1f}  {row[0]}")

    top_drugs = con.execute(
        """SELECT generic_name, therapeutic_category, shortage_status,
                  base_safety_stock, volatility_adjusted_safety_stock,
                  base_reorder_point, volatility_adjusted_reorder_point,
                  composite_risk_score
           FROM drug_risk_scores ORDER BY composite_risk_score DESC LIMIT 10"""
    ).fetchdf()
    print("\nTop 10 drugs by composite risk score:")
    print(top_drugs.to_string(index=False))

    con.close()


if __name__ == "__main__":
    main()
