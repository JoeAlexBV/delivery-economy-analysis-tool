-- Adds provider tracking for Uber Eats (UE) and DoorDash (DD).
--
-- IMPORTANT:
-- Athena and SQLite use different ALTER TABLE syntax.
-- Run only the section that matches the database you are changing.

-- ---------------------------------------------------------------------------
-- Athena
-- ---------------------------------------------------------------------------
-- Use ADD COLUMNS, plural, with the new columns wrapped in parentheses.
-- Replace/add table names here if you also created a restaurant_history
-- external table in Athena.

ALTER TABLE crestwood_gig_analysis ADD COLUMNS (platform_code string);
ALTER TABLE analytics_restaurant_hourly ADD COLUMNS (platform_code string);
ALTER TABLE analytics_zone_hourly ADD COLUMNS (platform_code string);
ALTER TABLE analytics_zone_shift ADD COLUMNS (platform_code string);

-- If restaurant_history exists as an Athena external table, use:
-- ALTER TABLE restaurant_history ADD COLUMNS (platform_code string);

-- ---------------------------------------------------------------------------
-- SQLite
-- ---------------------------------------------------------------------------
-- Existing local rows are Uber Eats data, so they are backfilled to UE.

ALTER TABLE restaurant_history ADD COLUMN platform_code TEXT DEFAULT 'UE';

UPDATE restaurant_history
SET platform_code = 'UE'
WHERE platform_code IS NULL OR platform_code = '';

CREATE INDEX IF NOT EXISTS idx_restaurant_history_platform_time
ON restaurant_history (platform_code, timestamp);

-- Existing parquet files without platform_code will read the new column as null;
-- the Python pipeline now writes UE/DD on all new exports.
