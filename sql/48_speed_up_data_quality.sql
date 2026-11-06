/*
Speed up the Data quality tab (irbs.vw_etl_issue_queue and friends)
==================================================================
Adds indexes only. No data is changed, and the ETL scripts keep working
as before. Safe to re-run (each index is created only if missing).

Why: every rerun of an ETL phase appends new rows to etl_reject /
etl_quality_issue, and the issue view keeps only the latest run of each phase.
Without an index on etl_run_id, SQL Server reads every old run too.

Run in Illinois_River_Biological_Station. Takes seconds.
At the bottom there is a timing check you can run before and after.
*/
SET NOCOUNT ON;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_etl_reject_run' AND object_id = OBJECT_ID(N'irbs.etl_reject'))
    CREATE INDEX IX_etl_reject_run
        ON irbs.etl_reject (etl_run_id)
        INCLUDE (source_table, source_record_id, error_code, created_at);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_etl_quality_issue_run' AND object_id = OBJECT_ID(N'irbs.etl_quality_issue'))
    CREATE INDEX IX_etl_quality_issue_run
        ON irbs.etl_quality_issue (etl_run_id)
        INCLUDE (severity, source_table, source_record_id, source_business_key,
                 issue_code, target_table, target_record_id, created_at);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_etl_run_latest' AND object_id = OBJECT_ID(N'irbs.etl_run'))
    CREATE INDEX IX_etl_run_latest
        ON irbs.etl_run (source_database, module, phase, etl_run_id DESC);
GO

/* lineage look-ups in the issue detail panel */
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_source_record_map_target' AND object_id = OBJECT_ID(N'irbs.source_record_map'))
    CREATE INDEX IX_source_record_map_target
        ON irbs.source_record_map (target_table, target_record_id)
        INCLUDE (source_table, source_record_id, resolution_type);
GO

UPDATE STATISTICS irbs.etl_reject;
UPDATE STATISTICS irbs.etl_quality_issue;
UPDATE STATISTICS irbs.etl_run;
GO
PRINT 'Data quality indexes in place.';
GO


/* ---------------------------------------------------------------------
   TIMING CHECK - run this block on its own (select it, then F5).
   Shows how many old runs are being skipped and how long the view takes.
--------------------------------------------------------------------- */
/*
SELECT phase, COUNT(*) AS runs_for_phase FROM irbs.etl_run GROUP BY phase ORDER BY phase;

SELECT
    (SELECT COUNT(*) FROM irbs.etl_reject)        AS reject_rows_all_runs,
    (SELECT COUNT(*) FROM irbs.etl_quality_issue) AS quality_rows_all_runs;

DECLARE @t DATETIME2 = SYSDATETIME();
SELECT COUNT(*) AS issues_in_queue FROM irbs.vw_etl_issue_queue;
SELECT DATEDIFF(millisecond, @t, SYSDATETIME()) / 1000.0 AS view_seconds;
*/
