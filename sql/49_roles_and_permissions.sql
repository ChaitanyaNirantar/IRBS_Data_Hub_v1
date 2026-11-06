/*
IRBS Data Hub - roles and permissions (run by the DBA)
======================================================
One place for every permission the Data Hub needs. Logins get abilities by
being added to ROLES, never by direct table grants. The app shows/hides tabs
based on these permissions.

  Role                  Can do                                         Typical members
  --------------------  ---------------------------------------------  ----------------------
  irbs_hub_reader       Explore data, view Data quality, see entries   everyone on the team
  irbs_qa_reviewer      record decisions on rejects / quality issues   data stewards
  irbs_data_entry       enter + submit new sampling events             field / data staff
  irbs_data_reviewer    approve / reject entries into master tables    Toby (and a backup)

Nobody needs INSERT/UPDATE/DELETE on any table: the procedures write on the
user's behalf through ownership chaining (schemas irbs and entry share an owner).

Prerequisites: scripts 40, 42 (qa_review), 45, 47 and 48 have been run.
Safe to re-run. Run in Illinois_River_Biological_Station.
*/
SET NOCOUNT ON;
GO

/* ---------------- roles ---------------- */
IF DATABASE_PRINCIPAL_ID(N'irbs_hub_reader')    IS NULL CREATE ROLE irbs_hub_reader;
IF DATABASE_PRINCIPAL_ID(N'irbs_qa_reviewer')   IS NULL CREATE ROLE irbs_qa_reviewer;
IF DATABASE_PRINCIPAL_ID(N'irbs_data_entry')    IS NULL CREATE ROLE irbs_data_entry;
IF DATABASE_PRINCIPAL_ID(N'irbs_data_reviewer') IS NULL CREATE ROLE irbs_data_reviewer;
GO

/* ---------------- grants (only for objects that exist) ---------------- */
DECLARE @g TABLE (obj SYSNAME, perm NVARCHAR(20), role SYSNAME);
INSERT @g VALUES
 -- Explore data
 (N'irbs.vw_harvest_site_event',       N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_harvest_catch',            N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_harvest_net_effort',       N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_harvest_catch_per_net',    N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_harvest_weights_by_site',  N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_harvest_individual_fish',  N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_harvest_tag_deployments',  N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_harvest_tag_recaptures',   N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_harvest_translocation',    N'SELECT',  N'irbs_hub_reader'),
 -- Data quality (view)
 (N'irbs.vw_etl_issue_queue',          N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_etl_issue_summary',        N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_etl_run_reconciliation',   N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_etl_issue_lineage',        N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_etl_issue_review_history', N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.vw_etl_issue_review_cleared', N'SELECT',  N'irbs_hub_reader'),
 (N'irbs.etl_reject',                  N'SELECT',  N'irbs_hub_reader'),   -- raw Access record in the issue panel
 (N'irbs.source_record_map',           N'SELECT',  N'irbs_hub_reader'),   -- lineage in the issue panel
 -- Data quality (decisions)
 (N'irbs.usp_set_issue_review',           N'EXECUTE', N'irbs_qa_reviewer'),
 (N'irbs.usp_set_issue_review_by_filter', N'EXECUTE', N'irbs_qa_reviewer'),
 -- Data entry (see + lookups for everyone; submit for entry role)
 (N'entry.vw_submission',              N'SELECT',  N'irbs_hub_reader'),
 (N'entry.vw_submission_log',          N'SELECT',  N'irbs_hub_reader'),
 (N'entry.vw_species',                 N'SELECT',  N'irbs_hub_reader'),
 (N'entry.vw_gear',                    N'SELECT',  N'irbs_hub_reader'),
 (N'entry.vw_reach',                   N'SELECT',  N'irbs_hub_reader'),
 (N'entry.usp_check_harvest_event',    N'EXECUTE', N'irbs_data_entry'),
 (N'entry.usp_submit_harvest_event',   N'EXECUTE', N'irbs_data_entry'),
 (N'entry.usp_withdraw_submission',    N'EXECUTE', N'irbs_data_entry'),
 -- Review / approve into master tables
 (N'entry.usp_check_harvest_event',    N'EXECUTE', N'irbs_data_reviewer'),
 (N'entry.usp_review_submission',      N'EXECUTE', N'irbs_data_reviewer');

DECLARE @obj SYSNAME, @perm NVARCHAR(20), @role SYSNAME, @sql NVARCHAR(600);
DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT obj, perm, role FROM @g;
OPEN c;
FETCH NEXT FROM c INTO @obj, @perm, @role;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF OBJECT_ID(@obj) IS NOT NULL
    BEGIN
        SET @sql = N'GRANT ' + @perm + N' ON ' + QUOTENAME(PARSENAME(@obj, 2)) + N'.' + QUOTENAME(PARSENAME(@obj, 1))
                 + N' TO ' + QUOTENAME(@role) + N';';
        EXEC (@sql);
    END
    ELSE
        PRINT CONCAT(N'skipped (not created yet): ', @obj, N' -> ', @role);
    FETCH NEXT FROM c INTO @obj, @perm, @role;
END;
CLOSE c; DEALLOCATE c;
GO

/* Role nesting: reviewers / entry staff can also read. */
IF NOT EXISTS (SELECT 1 FROM sys.database_role_members WHERE role_principal_id = DATABASE_PRINCIPAL_ID(N'irbs_hub_reader')
               AND member_principal_id = DATABASE_PRINCIPAL_ID(N'irbs_data_entry'))
    ALTER ROLE irbs_hub_reader ADD MEMBER irbs_data_entry;
IF NOT EXISTS (SELECT 1 FROM sys.database_role_members WHERE role_principal_id = DATABASE_PRINCIPAL_ID(N'irbs_hub_reader')
               AND member_principal_id = DATABASE_PRINCIPAL_ID(N'irbs_data_reviewer'))
    ALTER ROLE irbs_hub_reader ADD MEMBER irbs_data_reviewer;
IF NOT EXISTS (SELECT 1 FROM sys.database_role_members WHERE role_principal_id = DATABASE_PRINCIPAL_ID(N'irbs_hub_reader')
               AND member_principal_id = DATABASE_PRINCIPAL_ID(N'irbs_qa_reviewer'))
    ALTER ROLE irbs_hub_reader ADD MEMBER irbs_qa_reviewer;
GO

/* ---------------- members: EDIT these lines, then run them ----------------
   Prefer one SQL login (or AD account) PER PERSON so the audit trail shows who
   did what. Shared logins work, but every action is then recorded under the
   same login name.

ALTER ROLE irbs_hub_reader    ADD MEMBER [irbs-posit];            -- read-only team login
ALTER ROLE irbs_qa_reviewer   ADD MEMBER [<data steward login>];
ALTER ROLE irbs_data_entry    ADD MEMBER [<field staff login>];
ALTER ROLE irbs_data_reviewer ADD MEMBER [<Toby's login>];
*/

/* ---------------- check: who can do what ---------------- */
SELECT r.name AS role_name, m.name AS member
FROM sys.database_role_members rm
JOIN sys.database_principals r ON r.principal_id = rm.role_principal_id
JOIN sys.database_principals m ON m.principal_id = rm.member_principal_id
WHERE r.name LIKE N'irbs[_]%'
ORDER BY r.name, m.name;
GO
