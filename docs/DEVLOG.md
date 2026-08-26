# Hospital Drug Supply Risk & Inventory Planner

A tool that flags which drug categories a hospital system is at elevated risk
of stocking out on, and recommends safety-stock / reorder levels — combining
real FDA shortage signals with seasonal demand modeling.

## Status

| Step | What | Status |
|---|---|---|
| 1 | openFDA shortage pull (`src/fetch_shortages.py`) + DuckDB load (`src/build_database.py`) | **Done — two correctness bugs fixed, see below.** |
| 1.5 | CDC seasonal surveillance pull (`src/fetch_seasonal.py`) + load (`src/build_seasonal.py`) | **Done.** |
| 1.6 | NDC directory pull (`src/fetch_ndc.py`) + load, backfills `proprietary_name` (`src/build_ndc.py`) | **Done.** |
| 1.7 | Real per-drug demand data from MIMIC-IV (`src/build_demand_from_mimic.py`), replacing the synthetic `demand_inputs_sample.csv` | **Done — run against the real MIMIC-IV Clinical Database Demo (100 patients, Beth Israel Deaconess): 1,374 of 18,087 real prescription rows matched a real shortage NDC, 71 real per-drug demand rows.** |
| 3 (rerun) | `risk_model.py` rerun with 100% real inputs (openFDA + CDC + MIMIC-IV) | **Done — `drug_risk_scores` now has 71 real, meaningful rows (was 20 placeholder rows). See results below.** |
| 2 | Exploratory SQL views (`sql/02_exploratory_views.sql`) + Excel validation summary (`src/explore.py`) | **Done — category mapping corrected + a real seasonal-join bug fixed, see below. Run against real data: 1,581 shortages / 59,525 seasonal rows.** |
| 3 | Risk model: category risk scores + volatility-adjusted safety stock/reorder points (`src/risk_model.py`) | **Done — run against real data: 24 category risk scores, e.g. Pediatric 98.4, Antiviral 93.8, Anti-Infective 93.2 (all three correctly picking up real seasonal amplification once the bug below was fixed).** |
| 4 | Power BI dashboard | Not started — planned as a guided walkthrough (you want to learn Power BI, not just receive files) |
| 5 | Write-up / case study, publish to GitHub + portfolio | Not started |

### Bugs fixed in `build_database.py`

The original version was tested only against a mock sample, and that mock
sample didn't match the real `api.fda.gov/drug/shortages.json` response
shape. Verified directly against the live API (see `fixtures/` and
`tests/test_build_database.py`, all passing):

- **Dates were always NULL.** `to_date()` only parsed 8-digit `YYYYMMDD`
  strings; the real API returns `MM/DD/YYYY` (e.g. `"04/28/2023"`). Fixed.
- **`strength` and `resolved_note` were always NULL.** Neither field name
  exists on this endpoint — `r.get()` on a missing key just silently
  returns `None`, so the mock must have invented them. `strength` is now
  parsed out of the real `presentation` field (e.g. `"Carboplatin,
  Injection, 10 mg/1 mL (NDC 0703-4246-01)"` → `"10 mg/1 mL"`).
  `resolved_note` now reads from the real field, `related_info`.
- **`proprietary_name` was always NULL** for the same reason (no brand
  name on this endpoint at all — shortages are keyed by generic
  name/NDC). Now backfilled via `src/fetch_ndc.py` + `src/build_ndc.py`,
  which join the NDC Directory endpoint on the exact `package_ndc`.
- Added `company_name` and `contact_info` columns — real fields the
  original didn't capture, both directly useful for a supply-risk tool.

### Category mapping corrected in `sql/02_exploratory_views.sql`

`category_surveillance_map` originally used invented `therapeutic_category`
values (`"Emergency Medicine"`, `"Central Nervous System"`, `"Endocrine"`)
that don't exist in openFDA's real vocabulary — confirmed live via
`count=therapeutic_category` on the shortages endpoint (24 real values).
Rows keyed on a nonexistent category never match anything in a real pull,
so those categories would have silently gotten zero seasonal adjustment.
The map now covers all 24 real values, including `Antiviral` — a category
that didn't even exist under the old (wrong) list and is the single most
direct seasonal signal available (oseltamivir, nirmatrelvir, etc. are
literally flu/COVID treatments).

### Seasonal-join bug fixed in `sql/02_exploratory_views.sql`

`v_seasonal_national_weekly` (the view everything else in Step 2/3 depends
on) filtered `sex = 'Overall'` and `race_ethnicity = 'Overall'` to pick out
the national roll-up row. Run against the real `seasonal_surveillance`
table, that returned **zero rows** — CDC's live data uses the literal value
`'All'` for `sex` and `race_ethnicity` (only `age_group`/`site` actually use
`'Overall'`). Every seasonal number downstream (`v_seasonal_network_summary`,
the seasonal columns in `v_category_shortage_summary` and
`v_drug_seasonal_risk_inputs`, and therefore every category/drug risk score's
seasonal component) was silently `NULL`/`0` until this was fixed. Confirmed
against real data both ways: before the fix, Antiviral/Anti-Infective/
Pulmonary-Allergy/Pediatric (the categories this project's whole premise is
built around picking up seasonal signal for) all scored a flat baseline risk
of 60.0 with no seasonal component; after the fix they score 84–98,
correctly ranking above every category with no seasonal driver.

**This sandbox's outbound network is restricted to package registries**
(pypi, npm, etc.) — it can't reach `api.fda.gov` or `data.cdc.gov`. So all
six fetch/build scripts are unit- and integration-tested against real API
response fixtures captured live during development (`fixtures/`,
`tests/`, all passing), but were not run live here. Run them yourself
anywhere with normal internet access; see **Quickstart**. Steps 2 and 3
were built and verified against `src/seed_demo_data.py`, a synthetic
dataset shaped exactly like the real tables (same fields, same
vocabularies) — clearly separate from the real fetch/build scripts, safe
to ignore once you've done a real pull.

## Data sources

- **openFDA Drug Shortages** — `https://api.fda.gov/drug/shortages.json`
  ([docs](https://open.fda.gov/apis/drug/drugshortages/)).
- **CDC RESP-NET** — `https://data.cdc.gov/resource/kvib-3txy.json`, dataset
  ["Rates of Laboratory-Confirmed RSV, COVID-19, and Flu Hospitalizations
  from the RESP-NET Surveillance Systems"](https://data.cdc.gov/Public-Health-Surveillance/Rates-of-Laboratory-Confirmed-RSV-COVID-19-and-Flu/kvib-3txy).
  Weekly hospitalization rates per 100,000 population from three CDC
  networks (FluSurv-NET, RSV-NET, COVID-NET) plus a Combined series, back
  to 2018. Chosen over CDC's own FluView portal because FluView Interactive
  has no stable, documented machine-readable API, while this Socrata
  dataset does (standard `$select`/`$where`/`$limit`/`$offset`, no auth
  required for reasonable volumes).

## Setup

```bash
pip install -r requirements.txt
```

## Quickstart

```bash
# Step 1 — real pull (needs internet access to api.fda.gov)
python src/fetch_shortages.py
python src/build_database.py

# Step 1.5 — real pull (needs internet access to data.cdc.gov)
python src/fetch_seasonal.py
python src/build_seasonal.py

# Step 1.6 — real pull, backfills proprietary_name (needs api.fda.gov, run after build_database.py)
python src/fetch_ndc.py
python src/build_ndc.py

# --- OR, to explore Steps 2/3 without hitting the live APIs ---
python src/seed_demo_data.py

# Step 2 — exploratory views + Excel validation workbook
python src/explore.py            # writes reports/exploratory_summary.xlsx

# Step 3 — replace demand_inputs_sample.csv with your hospital's real
# per-drug weekly consumption + lead-time stats first (keyed by record_id,
# i.e. package_ndc — see the shortages table).
python src/risk_model.py --demand demand_inputs_sample.csv

# sanity-check the pipeline logic any time
python tests/test_build_database.py
python tests/test_build_seasonal.py
python tests/test_build_ndc.py
```

Query the result:

```bash
duckdb db/supply_risk.duckdb -c "
  SELECT generic_name, therapeutic_category, shortage_status,
         composite_risk_score, base_safety_stock,
         volatility_adjusted_safety_stock, volatility_adjusted_reorder_point
  FROM drug_risk_scores
  ORDER BY composite_risk_score DESC
  LIMIT 15;
"
```

## Schema

- **`shortages`** — one row per shortage record (`record_id` = package
  NDC): generic name, proprietary (brand) name, strength, dosage form,
  company, status (`Current`/`Resolved`/`To Be Discontinued`), dates,
  resolution note.
- **`shortage_categories`** — junction table (a record can have more than
  one therapeutic category).
- **`seasonal_surveillance`** — one row per (network, week, age group, sex,
  race/ethnicity, site, rate type): weekly and cumulative hospitalization
  rate per 100,000.
- **`ndc_directory`** — one row per package NDC from the NDC Directory
  endpoint: brand/generic name, dosage form, route, manufacturer, RxCUI.
  Used only to backfill `shortages.proprietary_name`.
- **`category_risk_scores`**, **`drug_risk_scores`** — Step 3 output,
  written by `risk_model.py`.

## Step 2 — exploratory views (`sql/02_exploratory_views.sql`)

- `v_seasonal_national_weekly` / `v_seasonal_volatility` — national weekly
  rate and 12-week trailing volatility per network.
- `v_seasonal_network_summary` — whole-series stats per network (avg, std,
  CV, peak-to-mean amplification) — what Step 3 consumes.
- `category_surveillance_map` — manually curated table mapping all 24 real
  `therapeutic_category` values (verified live via
  `count=therapeutic_category` on the shortages endpoint) → the CDC
  network whose seasonal curve plausibly drives that category's demand
  (`Antiviral`, `Anti-Infective`, `Pulmonary/Allergy` → `Combined`;
  `Pediatric` → `RSV-NET`, flagged low-confidence). Categories with no
  seasonal driver (Oncology, Cardiovascular, ...) are intentionally left
  unmapped. **Review the mappings for your formulary** — they're a
  judgment call, not a validated clinical model.
- `v_category_shortage_summary` — shortages per category joined to its
  mapped seasonal signal.
- `v_drug_seasonal_risk_inputs` — one row per drug, the direct input to
  Step 3.
- `v_shortage_trend_quarterly` — postings per quarter by category/status.

`src/explore.py` runs all of this and exports `reports/exploratory_summary.xlsx`
(one sheet per view) so the numbers can be spot-checked by hand.

## Step 3 — risk model (`src/risk_model.py`)

**Category risk score** (0–100) blends, per `therapeutic_category`: the
share of tracked shortage records that are `Current`/`To Be Discontinued`,
and the mapped CDC network's coefficient of variation (0 if unmapped).

**Safety stock / reorder point** (standard combined demand/lead-time
variability formula):

```
SS  = Z × sqrt( LT_avg × σ_D² + D_avg² × σ_LT² )
ROP = D_avg × LT_avg + SS
```

`base_*` uses your raw demand-CSV inputs unmodified. For the
`volatility_adjusted_*` columns:

- **σ_D** (demand std) is inflated by a `seasonal_amplification_factor` =
  `1 + CV` of the drug's mapped network — only for categories mapped in
  Step 2.
- **LT_avg and σ_LT** (lead time mean/std) are inflated by a
  `supply_risk_multiplier` from the drug's openFDA shortage status: active
  shortages, and especially `To Be Discontinued`, get longer and less
  predictable lead-time assumptions, since that's how a shortage actually
  manifests operationally.

`composite_risk_score` (0–100) is a weighted blend of shortage severity,
seasonal amplification, and the resulting safety-stock inflation — for
ranking/triage, not as a precise probability.

**You must supply real demand data.** `demand_inputs_sample.csv` (weekly
consumption mean/std and replenishment lead-time mean/std, keyed by
`record_id`) is illustrative only. Point `--demand` at your own CSV.

### Known limitations

- Planning heuristic, not a validated supply-chain optimization or
  clinical model — use for triage, not automatic reordering.
- The category→network mapping is coarse and manually curated; review it
  for your formulary.
- National CDC rates are a proxy for local demand pressure and can diverge
  from what a specific hospital or region is seeing.

## Step 1.7 — real demand data from MIMIC-IV (`src/build_demand_from_mimic.py`)

`demand_inputs_sample.csv` was always synthetic (record_ids like
`50000-100-01` that don't match any real `package_ndc`), which is why
`drug_risk_scores` currently has 20 rows but all `NULL`/`0` — see the
comparison below for why this is genuinely hard to fix with public data,
and what we picked instead.

**Why MIMIC-IV, not a hospital's own published numbers.** Hospitals don't
publish per-drug purchasing/consumption volumes anywhere — that's
competitively sensitive and not required by any transparency rule (the
federal price-transparency files that hospitals *are* required to publish
list prices, not quantities consumed). So "real hospital consumption data,
publicly available" doesn't exist in the form this project originally
assumed; the closest real substitute is de-identified EHR medication-order
data from a research dataset. Compared against the other named-hospital
options:

| Dataset | Hospital / university | Scale | NDC coding | Access |
|---|---|---|---|---|
| **MIMIC-IV (chosen)** | Beth Israel Deaconess Medical Center — Harvard Medical School teaching hospital | 364,627 patients, 546,028 hospitalizations | Real US NDC — joins directly to our openFDA `shortages.record_id`, no crosswalk | Free CITI training + signed Data Use Agreement, ~days to a week; a free 100-patient demo needs no application at all |
| eICU-CRD | 208 US hospitals, but **identities stripped** for privacy | 200,000+ admissions (bigger) | Real US NDC | Same PhysioNet credentialing |
| AmsterdamUMCdb / HiRID | Amsterdam UMC / Bern University Hospital — also named, prestigious academic hospitals | 20–34k admissions | **ATC**, not NDC — European hospitals don't use the US coding scheme our shortages table is keyed on | DUA (Amsterdam) or PhysioNet credentialing (HiRID) |

MIMIC-IV is the only option that's both a single well-known, named
hospital *and* uses the same NDC coding system as our existing schema, so
nothing needs a lossy cross-coding-system match.

**What the script does.** `src/build_demand_from_mimic.py` reads MIMIC's
`hosp/prescriptions.csv(.gz)`, matches each row's `ndc` to a real
`shortages.record_id`, estimates quantity dispensed per order
(`doses_per_24_hrs × form_val_disp × order_duration_days`), buckets by ISO
week, and computes per-drug weekly mean/std — the same
`avg_weekly_demand_units`/`demand_std_units` columns `risk_model.py`
already expects. `lead_time_weeks`/`lead_time_std_weeks` stay a documented
default assumption (2.0 / 0.5 weeks) either way — no public dataset
anywhere tracks hospital-to-supplier replenishment lead times, so this was
never going to be "real" regardless of which demand source we used.

**NDC matching, done carefully.** openFDA's `package_ndc` (our
`shortages.record_id`) is dash-formatted in three different segment
layouts (4-4-2, 5-3-2, or 5-4-1 digits). Converting to the standard
11-digit form requires zero-padding whichever segment is short — e.g.
`42806-799-60` (5-3-2) correctly becomes `42806079960`, whereas naively
left-padding the whole digit string would wrongly give `04280679960`. Unit
tests (`tests/test_build_demand_from_mimic.py`) cover this against three
real NDCs already used elsewhere in this project's fixtures, one from each
layout.

**Not yet run against a real file.** Built and unit-tested against MIMIC's
documented schema and a hand-built fixture matching it
(`fixtures/mimic_prescriptions_sample.csv`) — this sandbox can't reach
physionet.org, same restriction as the openFDA/CDC scripts. Real exports
can differ from docs (that's exactly what happened with the CDC seasonal
data — see above); run it against your actual download and fix whatever
surprises turn up the same way.

```bash
# Free, no application needed — 100 real de-identified patients:
# https://physionet.org/content/mimic-iv-demo/2.2/ -> hosp/prescriptions.csv.gz

# Full dataset (364k+ patients) needs PhysioNet credentialing first:
# https://physionet.org/content/mimiciv/3.1/

python src/build_demand_from_mimic.py --prescriptions data/raw/mimic_prescriptions.csv.gz --out demand_inputs_mimic.csv
python src/risk_model.py --demand demand_inputs_mimic.csv

python tests/test_build_demand_from_mimic.py
```

### Real results — 100% real data, every stage (2026-08-25)

Ran end-to-end against the real MIMIC-IV Clinical Database Demo
(https://physionet.org/content/mimic-iv-demo/2.2/, 100 real de-identified
Beth Israel Deaconess patients, downloaded directly, no code changes
needed — the script held up against the real file exactly as documented).
1,374 of 18,087 real prescription rows matched a real openFDA shortage
NDC, producing 71 real per-drug demand rows
(`reports/drug_risk_scores_mimic_demo.csv`,
`reports/category_risk_scores.csv`). Top of the real
`composite_risk_score` ranking: Metronidazole Injection (81.8, Current
shortage + Anti-Infective seasonal amplification), Clindamycin Phosphate
Injection (76.8, same combo), then a cluster of `To Be Discontinued`
drugs (Belladonna and Opium Suppository, Lidocaine HCl Injection,
Doxercalciferol Injection) correctly scoring high on the
discontinuation-risk component even at low volume — exactly the "both a
volume risk and a discontinuation risk" pattern this model was designed
to surface.

## Next steps

- **Apply for full MIMIC-IV access** (in progress) for far more of the
  1,581-drug real shortage list to match against — 71 real matches from
  just 100 demo patients out of 546,028 real hospitalizations in the full
  dataset suggests the full version will cover a large share of it. Once
  approved, rerun `build_demand_from_mimic.py` with `--prescriptions`
  pointed at the full download — same script, same columns, no code
  changes.
- Step 4: Power BI dashboard connected to the DuckDB output — planned as a
  guided walkthrough (Power BI has no native DuckDB connector, so this
  starts with exporting clean tables/views to CSV or Parquet, then
  building the report in Power BI Desktop).
- Step 5: write-up / case study, publish to GitHub + portfolio site.
