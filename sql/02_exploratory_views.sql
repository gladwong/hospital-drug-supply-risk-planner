-- Step 2: Exploratory SQL views
--
-- Sits between the raw pulled tables (shortages, shortage_categories,
-- seasonal_surveillance) and the Step 3 risk model. Three jobs:
--   1. Reduce seasonal_surveillance to a clean national weekly series per network.
--   2. Compute whole-series volatility (std, CV, peak amplification) per network.
--   3. Map therapeutic_category -> the surveillance network whose seasonal
--      curve plausibly drives demand for that category, then join shortage
--      status onto that signal.
--
-- Run: duckdb db/supply_risk.duckdb < sql/02_exploratory_views.sql


-- 1. National weekly rate per network (site/sex/race/age rolled up to the
--    "everyone, nationally" row).
--    NOTE (found running this against a real pull, 2026-08-25): sex and
--    race_ethnicity use the literal value 'All' for the rolled-up row, not
--    'Overall' -- only age_group and site use 'Overall'. The original
--    version of this view filtered on 'Overall' for all four columns, which
--    never matched anything (confirmed live: 0 rows), silently leaving
--    every downstream seasonal number NULL. Verified against real data that
--    this corrected filter returns the expected 4-network national series
--    (1,385 rows, 2018-2026).
CREATE OR REPLACE VIEW v_seasonal_national_weekly AS
SELECT surveillance_network, season, week_ending_date, mmwr_year, mmwr_week, weekly_rate
FROM seasonal_surveillance
WHERE age_group = 'Overall' AND sex = 'All'
  AND race_ethnicity = 'All' AND site = 'Overall'
  AND rate_type = 'Observed'
ORDER BY surveillance_network, week_ending_date;


-- 2. 12-week trailing volatility per network (for eyeballing how volatility
--    moves through a season, not directly consumed by Step 3).
CREATE OR REPLACE VIEW v_seasonal_volatility AS
SELECT
    surveillance_network,
    week_ending_date,
    weekly_rate,
    AVG(weekly_rate) OVER w AS rolling_mean_12wk,
    STDDEV_SAMP(weekly_rate) OVER w AS rolling_std_12wk,
    CASE WHEN AVG(weekly_rate) OVER w > 0
         THEN STDDEV_SAMP(weekly_rate) OVER w / AVG(weekly_rate) OVER w
         ELSE NULL END AS rolling_cv_12wk
FROM v_seasonal_national_weekly
WINDOW w AS (PARTITION BY surveillance_network ORDER BY week_ending_date
             ROWS BETWEEN 11 PRECEDING AND CURRENT ROW)
ORDER BY surveillance_network, week_ending_date;


-- 3. Whole-series summary per network -- what Step 3 actually consumes.
CREATE OR REPLACE VIEW v_seasonal_network_summary AS
SELECT
    surveillance_network,
    COUNT(*) AS n_weeks,
    MIN(week_ending_date) AS first_week,
    MAX(week_ending_date) AS last_week,
    ROUND(AVG(weekly_rate), 3) AS avg_weekly_rate,
    ROUND(STDDEV_SAMP(weekly_rate), 3) AS std_weekly_rate,
    ROUND(MAX(weekly_rate), 3) AS peak_weekly_rate,
    ROUND(STDDEV_SAMP(weekly_rate) / NULLIF(AVG(weekly_rate), 0), 3) AS cv,
    ROUND(1.0 + (MAX(weekly_rate) - AVG(weekly_rate)) / NULLIF(AVG(weekly_rate), 0), 3)
        AS peak_to_mean_amplification
FROM v_seasonal_national_weekly
GROUP BY surveillance_network;


-- 4. Category -> surveillance network mapping.
--    therapeutic_category values below are the REAL, COMPLETE vocabulary
--    openFDA uses on the shortages endpoint -- confirmed live via
--    `count=therapeutic_category` on api.fda.gov/drug/shortages.json
--    (24 distinct values, snapshot 2026-08-22). An earlier version of this
--    table used invented category names ("Emergency Medicine", "Central
--    Nervous System", "Endocrine") that don't actually exist in openFDA's
--    vocabulary -- those rows would never have matched a real pull and
--    would have silently gotten zero seasonal adjustment. Fixed here.
--
--    Only categories whose drugs are plausibly driven by respiratory-
--    illness season get a mapped network; everything else is left
--    unmapped on purpose, so Step 3 applies no seasonal adjustment there.
--    This is still a coarse, manually curated judgment call -- revisit it
--    for your formulary.
CREATE OR REPLACE TABLE category_surveillance_map (
    therapeutic_category VARCHAR PRIMARY KEY,
    mapped_surveillance_network VARCHAR,
    rationale VARCHAR
);

INSERT INTO category_surveillance_map VALUES
    ('Antiviral',                  'Combined',    'Directly used to treat flu/COVID/RSV (oseltamivir, nirmatrelvir, etc.) -- the most direct seasonal link of any category'),
    ('Anti-Infective',             'Combined',    'Antibiotics: primary + secondary bacterial infection treatment during flu/RSV/COVID season'),
    ('Pulmonary/Allergy',          'Combined',    'Bronchodilators/respiratory support drugs track overall respiratory hospitalization burden'),
    ('Pediatric',                  'RSV-NET',     'Pediatric-labeled shortages skew toward RSV, which hits infants/young children hardest -- low-confidence, cross-cutting category'),
    ('Anesthesia',                 NULL, 'Procedural volume driven, not respiratory-season driven'),
    ('Psychiatry',                 NULL, 'Not respiratory-season driven'),
    ('Gastroenterology',           NULL, 'Not respiratory-season driven'),
    ('Neurology',                  NULL, 'Not respiratory-season driven'),
    ('Analgesia/Addiction',        NULL, 'Not respiratory-season driven'),
    ('Cardiovascular',             NULL, 'Chronic-disease maintenance drugs; not respiratory-season driven'),
    ('Endocrinology/Metabolism',   NULL, 'Not respiratory-season driven'),
    ('Oncology',                   NULL, 'Chemotherapy demand is not respiratory-season driven'),
    ('Other',                      NULL, 'Catch-all bucket, no consistent seasonal driver'),
    ('Rheumatology',               NULL, 'Not respiratory-season driven'),
    ('Hematology',                 NULL, 'Not respiratory-season driven'),
    ('Renal',                      NULL, 'Not respiratory-season driven'),
    ('Ophthalmology',              NULL, 'Not respiratory-season driven'),
    ('Dermatology',                NULL, 'Not respiratory-season driven'),
    ('Medical Imaging',            NULL, 'Contrast agents etc.; procedural volume driven, not respiratory-season driven'),
    ('Total Parenteral Nutrition', NULL, 'Not respiratory-season driven'),
    ('Transplant',                 NULL, 'Not respiratory-season driven'),
    ('Musculoskeletal',            NULL, 'Not respiratory-season driven'),
    ('Reproductive',               NULL, 'Not respiratory-season driven'),
    ('Urology',                    NULL, 'Not respiratory-season driven');


-- 5. Shortage summary per category: how many active/resolved/discontinuing
--    records, most recent posting, and its mapped seasonal signal.
CREATE OR REPLACE VIEW v_category_shortage_summary AS
SELECT
    sc.therapeutic_category,
    COUNT(*) AS n_shortage_records,
    COUNT(*) FILTER (WHERE s.status = 'Current') AS n_current,
    COUNT(*) FILTER (WHERE s.status = 'Resolved') AS n_resolved,
    COUNT(*) FILTER (WHERE s.status = 'To Be Discontinued') AS n_discontinuing,
    MAX(s.initial_posting_date) AS most_recent_posting,
    m.mapped_surveillance_network,
    m.rationale AS mapping_rationale,
    ns.avg_weekly_rate,
    ns.std_weekly_rate,
    ns.cv AS seasonal_cv,
    ns.peak_to_mean_amplification
FROM shortage_categories sc
JOIN shortages s ON s.record_id = sc.record_id
LEFT JOIN category_surveillance_map m ON sc.therapeutic_category = m.therapeutic_category
LEFT JOIN v_seasonal_network_summary ns ON m.mapped_surveillance_network = ns.surveillance_network
GROUP BY sc.therapeutic_category, m.mapped_surveillance_network, m.rationale,
         ns.avg_weekly_rate, ns.std_weekly_rate, ns.cv, ns.peak_to_mean_amplification
ORDER BY n_current DESC, therapeutic_category;


-- 6. One row per drug (record_id) with its category, shortage status, and
--    mapped seasonal signal -- direct input to Step 3's per-drug reorder
--    point calculation. A record can carry more than one category; this
--    view picks the first alphabetically so each drug appears once.
CREATE OR REPLACE VIEW v_drug_seasonal_risk_inputs AS
SELECT
    s.record_id,
    s.generic_name,
    s.proprietary_name,
    s.dosage_form,
    s.strength,
    s.company_name,
    s.status AS shortage_status,
    s.availability,
    s.initial_posting_date,
    s.update_date,
    s.discontinued_date,
    cat.therapeutic_category,
    m.mapped_surveillance_network,
    ns.avg_weekly_rate,
    ns.std_weekly_rate,
    ns.cv AS seasonal_cv,
    ns.peak_to_mean_amplification
FROM shortages s
LEFT JOIN (
    SELECT record_id, MIN(therapeutic_category) AS therapeutic_category
    FROM shortage_categories GROUP BY record_id
) cat ON cat.record_id = s.record_id
LEFT JOIN category_surveillance_map m ON cat.therapeutic_category = m.therapeutic_category
LEFT JOIN v_seasonal_network_summary ns ON m.mapped_surveillance_network = ns.surveillance_network
ORDER BY cat.therapeutic_category, s.generic_name;


-- 7. Shortage postings per quarter by category/status -- "is this getting
--    worse" trend check.
CREATE OR REPLACE VIEW v_shortage_trend_quarterly AS
SELECT
    sc.therapeutic_category,
    s.status,
    DATE_TRUNC('quarter', s.initial_posting_date) AS quarter,
    COUNT(*) AS n_postings
FROM shortages s
JOIN shortage_categories sc ON sc.record_id = s.record_id
WHERE s.initial_posting_date IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1, 3;
