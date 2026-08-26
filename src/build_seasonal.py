"""
build_seasonal.py

Loads the raw CDC RESP-NET JSON into the same DuckDB database as
build_database.py, adding one table:

  seasonal_surveillance   -- one row per (network, week, age group, sex,
                              race/ethnicity, site, rate type)

Run after fetch_seasonal.py has produced a file in data/raw/.

--------------------------------------------------------------------------
SCHEMA CHANGE NOTE (discovered live, 2026-08-25): see the matching note at
the top of fetch_seasonal.py. CDC's live field names today are:
surveillance_network, season, date_type, date, age_category, race, sex,
state, data_type, estimate_type, rate_type, estimate -- a "long" layout
with one row per metric instead of one row per week with separate rate
columns. This build step:

  - renames date -> week_ending_date, age_category -> age_group,
    race -> race_ethnicity, state -> site (keeping our column names
    stable so sql/02_exploratory_views.sql and risk_model.py don't need
    to change at all)
  - pivots data_type ('Weekly Rate' / 'Cumulative Rate') + estimate back
    into separate weekly_rate/cumulative_rate columns, since
    fetch_seasonal.py's $where already restricts the raw pull to
    date_type='Week Ending Date' -- every record at that point is one or
    the other of those two data_types
  - drops mmwr_year/mmwr_week: the source no longer provides these at
    all (confirmed by inspecting a full raw record -- the fields are
    just gone). Rather than fake epidemiological-week math from `date`
    ourselves and have it look more precise than it is, these columns
    are kept in the schema for compatibility but always NULL now.
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
CREATE OR REPLACE TABLE seasonal_surveillance (
    surveillance_network    VARCHAR,   -- FluSurv-NET | RSV-NET | COVID-NET | Combined
    season                    VARCHAR,
    week_ending_date           DATE,
    mmwr_year                   INTEGER,  -- no longer provided by the source; always NULL, see note above
    mmwr_week                    INTEGER,  -- no longer provided by the source; always NULL, see note above
    age_group                     VARCHAR,
    sex                            VARCHAR,
    race_ethnicity                  VARCHAR,
    site                              VARCHAR,   -- 'Overall' = national combined catchment
    weekly_rate                       DOUBLE,    -- hospitalizations per 100,000 population
    cumulative_rate                    DOUBLE,
    rate_type                           VARCHAR  -- Observed | Predicted, etc.
);
"""


def latest_raw_file() -> Path:
    files = sorted(RAW_DIR.glob("seasonal_surveillance_*.json"))
    if not files:
        sys.exit("No raw seasonal data found. Run fetch_seasonal.py first.")
    return files[-1]


def to_date(value):
    """Socrata floating_timestamp values look like '2018-10-06T00:00:00.000'."""
    if not value:
        return None
    return value.split("T")[0]


def to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def load_records(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def main():
    raw_path = latest_raw_file()
    print(f"Loading {raw_path.name} ...")
    records = load_records(raw_path)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA_SQL)

    # Pivot: each raw record carries ONE number (`estimate`) plus a
    # `data_type` saying whether it's the "Weekly Rate" or "Cumulative
    # Rate" for a given (network, date, age, sex, race, site, rate_type)
    # combination. Group by that combination and merge the two data_types
    # back into one row with both a weekly_rate and cumulative_rate column.
    groups = {}
    for r in records:
        key = (
            r.get("surveillance_network"), r.get("date"),
            r.get("age_category"), r.get("sex"), r.get("race"),
            r.get("state"), r.get("rate_type"), r.get("season"),
        )
        g = groups.setdefault(key, {})
        data_type = r.get("data_type")
        value = to_float(r.get("estimate"))
        if data_type == "Weekly Rate":
            g["weekly_rate"] = value
        elif data_type == "Cumulative Rate":
            g["cumulative_rate"] = value
        # any other data_type shouldn't appear here -- fetch_seasonal.py's
        # $where already restricts the pull to date_type='Week Ending Date',
        # which (confirmed live) only ever carries these two data_types.

    rows = []
    for (network, raw_date, age, sex, race, site, rate_type, season), vals in groups.items():
        rows.append((
            network,
            season,
            to_date(raw_date),
            None,  # mmwr_year -- no longer provided by the source
            None,  # mmwr_week -- no longer provided by the source
            age,
            sex,
            race,
            site,
            vals.get("weekly_rate"),
            vals.get("cumulative_rate"),
            rate_type,
        ))

    con.executemany(
        "INSERT INTO seasonal_surveillance VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )

    n = con.execute("SELECT COUNT(*) FROM seasonal_surveillance").fetchone()[0]
    by_net = con.execute(
        """SELECT surveillance_network, MIN(week_ending_date), MAX(week_ending_date), COUNT(*)
           FROM seasonal_surveillance GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    print(f"Loaded {n} seasonal_surveillance rows (pivoted from {len(records)} raw Weekly/Cumulative Rate records).")
    for net, mn, mx, c in by_net:
        print(f"  {net:<12} {mn} -> {mx}  ({c} rows)")
    print(f"Database: {DB_PATH}")

    con.close()


if __name__ == "__main__":
    main()
