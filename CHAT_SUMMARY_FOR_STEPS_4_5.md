# Hospital Drug Supply Risk & Inventory Planner — handoff summary for Steps 4 & 5

Project folder (on Glad's machine, connected via Cowork device bridge):
`C:\Users\gladw\OneDrive\Desktop\supply-risk-planner`

Steps 1 through 3 are fully done and verified against **100% real data** (no
synthetic/demo data left in the pipeline except where explicitly noted).
This doc is a handoff so a new chat can start directly on Step 4 (Power BI)
and Step 5 (write-up) without re-deriving context.

## Status table

| Step | What | Status |
|---|---|---|
| 1 | openFDA shortage pull + DuckDB load | Done. 1,581 real shortage records, 24 real therapeutic categories. |
| 1.5 | CDC RESP-NET seasonal pull + load | Done. 59,525 real rows across 4 networks (FluSurv-NET, RSV-NET, COVID-NET, Combined), 2018–2026. |
| 1.6 | NDC directory pull + `proprietary_name` backfill | Done. 1,676 real NDC directory rows; 1,369/1,581 shortages backfilled. |
| 1.7 | Real per-drug demand data from MIMIC-IV | Done, using the free 100-patient **MIMIC-IV Clinical Database Demo**. 71 real per-drug weekly demand rows (1,374 of 18,087 real prescription rows matched a real shortage NDC). |
| 2 | Exploratory SQL views + Excel export | Done. `reports/exploratory_summary.xlsx`. |
| 3 | Risk model (category + drug risk scores, safety stock/reorder point) | Done, rerun with real MIMIC demand data. `category_risk_scores`: 24 rows. `drug_risk_scores`: 71 real rows (was 20 placeholder rows before this session). |
| 4 | Power BI dashboard | **Not started — data export is ready, walkthrough not yet begun.** Glad wants this taught hands-on, not built for him — see "How Glad wants this done" below. |
| 5 | Write-up / case study, publish to GitHub + portfolio | Not started. |

## What happened this session (most recent chat)

1. **Fixed a DuckDB WAL/permission deadlock** specific to the Cowork device
   bridge: any `duckdb.connect()` after a prior write session leaves a
   `.wal` file the bridge can't delete (confirmed: this is a hard OS-level
   restriction on the bridge's sandbox, unrelated to disk space or
   checkpointing — `CHECKPOINT` itself fails the same way). Fix pattern: do
   ALL remaining DB work in ONE long-lived `duckdb.connect()`, closing
   exactly once at the end; never reopen mid-script. If a stuck `.wal`
   blocks even that first connect, dump tables via a `read_only=True`
   connection to Parquet, `os.rename()` (not delete — renames work,
   deletes don't) the blocking `.wal` into `db/_stale_wal_files_safe_to_delete/`,
   then reopen read-write and restore from Parquet. This is why any future
   backend script run against the real db needs to follow this same
   single-connection pattern rather than calling each script's own `main()`
   (which each independently calls `duckdb.connect()`).

2. **Found and fixed a real bug** in `sql/02_exploratory_views.sql`:
   `v_seasonal_national_weekly` filtered `sex = 'Overall'` and
   `race_ethnicity = 'Overall'`, but CDC's live data actually uses `'All'`
   for those two fields (only `age_group`/`site` use `'Overall'`). This
   silently zeroed every seasonal number downstream. Before the fix,
   Antiviral/Anti-Infective/Pulmonary-Allergy/Pediatric all scored a flat
   60.0 baseline risk with no seasonal component; after the fix they score
   84–98, correctly ranking above categories with no seasonal driver.

3. **Chose MIMIC-IV over other public hospital datasets** (Glad wanted a
   well-known hospital linked to a well-known university, then asked for a
   comparison against alternatives). Researched and compared:
   - **MIMIC-IV (chosen)** — Beth Israel Deaconess Medical Center, a
     Harvard Medical School teaching hospital, maintained by MIT's Lab for
     Computational Physiology on PhysioNet. Real US NDC codes that join
     directly to `shortages.record_id`, no crosswalk needed.
   - eICU-CRD — bigger (200k+ admissions) but hospital identities are
     stripped for privacy (spans 208 anonymized hospitals), and its
     medication table uses HICL/GTC codes (proprietary First Databank),
     not NDC — would need a paid crosswalk. Ruled out.
   - AmsterdamUMCdb / HiRID — also named, prestigious academic hospitals
     (Amsterdam UMC, Bern University Hospital) but European, coded in ATC
     not NDC. Ruled out — this project's shortages table is US-NDC-keyed.

4. **Built `src/build_demand_from_mimic.py`** — parses MIMIC's
   `hosp/prescriptions.csv(.gz)`, matches `ndc` to real `shortages.record_id`
   (with a segment-aware NDC normalizer — openFDA package_ndc has three
   different dash layouts: 4-4-2, 5-3-2, 5-4-1; naively left-padding the
   digit string gives the WRONG 11-digit NDC for the 5-3-2/5-4-1 cases, so
   the normalizer pads whichever segment is actually short), estimates
   quantity per order (`doses_per_24_hrs × form_val_disp × duration_days`),
   buckets by ISO week, computes per-drug weekly mean/std. Unit-tested
   against `fixtures/mimic_prescriptions_sample.csv` and
   `tests/test_build_demand_from_mimic.py` (7/7 passing), using three real
   NDCs already in this project's fixtures, one from each dash layout.
   Ran clean against the real downloaded file with **zero code changes**
   needed — documented schema held up exactly.

5. **Downloaded the real MIMIC-IV Clinical Database Demo** (100 real
   de-identified patients) via Claude-in-Chrome browser automation —
   navigated to https://physionet.org/content/mimic-iv-demo/2.2/, clicked
   "Download the ZIP file" (15.4 MB, no login/credentialing needed, open
   license), landed in Glad's `Downloads` folder, staged into the
   container, unzipped, and ran the real pipeline end to end.

6. **Real Step 3 results** (`reports/drug_risk_scores_mimic_demo.csv`,
   `reports/category_risk_scores.csv`, `demand_inputs_mimic_demo.csv` — all
   in the project folder): top of the real `composite_risk_score` ranking
   is Metronidazole Injection (81.8 — active shortage + Anti-Infective
   seasonal amplification) and Clindamycin Phosphate Injection (76.8, same
   combo), followed by a cluster of `To Be Discontinued` drugs (Belladonna
   and Opium Suppository, Lidocaine HCl Injection, Doxercalciferol
   Injection) correctly scoring high on discontinuation risk even at low
   volume.

7. **Exported `reports/powerbi_export.xlsx`** — four clean sheets ready for
   Power BI: `drug_risk_scores` (71 rows), `category_risk_scores` (24
   rows), `seasonal_national_weekly` (1,385 rows, real time series per CDC
   network), `shortage_trend_quarterly` (213 rows). This is the "export
   clean tables to CSV/Parquet" step the README always called for — Power
   BI has no native DuckDB connector.

## In progress, not blocking Step 4/5

**Full MIMIC-IV access application** — Glad is applying for full
credentialed access (364,627 patients, 546,028 hospitalizations — vs. the
demo's 100) via PhysioNet. He's logged in and filling out the application
himself (reference: edwinlkee@gmail.com, "Other" category; I drafted the
"Research Topic" field text for him, in his own MBA-paper voice per his
`mba-writing-voice` skill on request). Typically several days to about a
week for approval. **He said: "if in a week I get access I'll let you know,
let's move forward"** — meaning don't wait on this. When he does get
access, the swap is trivial: rerun `build_demand_from_mimic.py` with
`--prescriptions` pointed at the full download instead of the demo file,
same script, same columns, no code changes, then rerun `risk_model.py`.

**Freed disk space for Power BI Desktop install** — found
`PBIDesktopSetup_x64.exe` (662 MB) already sitting in his Downloads folder
(no need to redownload), found and had him delete `Downloads\Installers`
(3.6 GB of already-used, easily-redownloadable app installers) plus two
junk files (a Mac `.dmg` on his Windows machine, an incomplete
`.crdownload`) to clear room. He emptied the Recycle Bin. **Unconfirmed
whether he's actually run the Power BI Desktop installer yet** — that's the
first thing to check in the next chat.

## How Glad wants this done (important — read before starting Step 4)

Glad has flipped between two modes this project, and the split matters:

- **Backend engineering (Steps 1–3, data pipeline, bug fixes):** he
  explicitly said *"ok can you just do all these coding stuff its boring"*
  — take over and execute directly, don't make him type commands.
- **Power BI (Step 4) specifically:** he explicitly asked for the opposite
  — *"i want to learn power bi so lets walk through that when we come to
  it"* and earlier, for the whole project setup, *"walk me through this
  project rather than do it for me after all i want to learn the
  skills."* **Step 4 should be a genuine hands-on walkthrough**: he clicks
  and builds in Power BI Desktop, the assistant explains each step and
  waits for confirmation before moving on — small steps, not a data dump
  of the whole tutorial at once. Do NOT just build the .pbix file and hand
  it over.

Step 5 (write-up/case study) has no stated preference yet — ask, but
"do the coding/writing, I'll review" is a reasonable default given the
Step 1–3 pattern, unless Glad says otherwise.

## Key file locations

- Project root: `supply-risk-planner/` (README.md has the full project
  README with all bug-fix writeups, schema docs, and quickstart commands —
  read that first for anything not covered here)
- Real database: `db/supply_risk.duckdb` (WAL caveat above applies to any
  further scripted edits via the device bridge)
- Power BI source data: `reports/powerbi_export.xlsx`
- Real Step 3 outputs: `reports/drug_risk_scores_mimic_demo.csv`,
  `reports/category_risk_scores.csv`
- MIMIC pipeline: `src/build_demand_from_mimic.py`,
  `demand_inputs_mimic_demo.csv`, `data/raw/mimic_demo/` (the extracted
  real demo download)
- `db/_stale_wal_files_safe_to_delete/` — accumulated harmless WAL/backup
  clutter from this session's troubleshooting; safe for Glad to delete
  manually whenever, not touched by any script

## Suggested first message for the next chat

"Continue the Hospital Drug Supply Risk & Inventory Planner — Steps 1–3
are done with real data (see CHAT_SUMMARY_FOR_STEPS_4_5.md in the project
folder for full details). Let's do Step 4: Power BI, walked through
hands-on like I asked for. Check first whether I've installed Power BI
Desktop yet."
