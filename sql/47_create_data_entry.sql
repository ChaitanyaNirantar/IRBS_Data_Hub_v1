/*
IRBS Harvest - Data entry with review ("holding area")
======================================================
New field data entered in the IRBS Data Hub does NOT go straight into the
master Harvest tables. It is held in schema [entry] as a PENDING submission.
A reviewer (Toby) approves or rejects it. Approval loads the submission into
the irbs.* Harvest tables in ONE transaction, with lineage in
irbs.source_record_map. Rejection sends it back with a comment.

One submission = one sampling event (Site Data) with its environment,
nets (Net Data), fish counts (Fish Count) and optional weights (Weight Data),
stored as JSON so nothing is lost before review.

Objects
  entry.submission             one row per submission (status, who, when, payload)
  entry.submission_log         every action (submitted, withdrawn, approved, rejected)
  entry.vw_submission          list/detail for the app
  entry.vw_species / vw_gear / vw_reach   pick-lists for the entry form
  entry.usp_check_harvest_event   validation only (errors + warnings), no writes
  entry.usp_submit_harvest_event  validate + save as PENDING
  entry.usp_withdraw_submission   submitter withdraws a PENDING submission
  entry.usp_review_submission     APPROVE (load into irbs.*) or REJECT  - reviewer only

Permissions (bottom of file): the team gets EXECUTE on check/submit/withdraw
and SELECT on the views; only the reviewer gets EXECUTE on usp_review_submission.
No one needs INSERT/UPDATE on any table.

Run in Illinois_River_Biological_Station after the Harvest ETL tables exist.
Safe to re-run. SQL Server 2017+.
*/
SET NOCOUNT ON;
SET XACT_ABORT ON;
GO

IF OBJECT_ID(N'irbs.harvest_event', N'U') IS NULL
BEGIN
    RAISERROR('irbs.harvest_event not found. Run this in Illinois_River_Biological_Station. Nothing was created.', 16, 1);
    SET NOEXEC ON;      -- skip every remaining batch
END;
GO

/* Create schema [entry] with the SAME OWNER as schema [irbs].
   Same owner = ownership chaining, so the entry procedures can write to the
   irbs.* tables without anyone needing table permissions. */
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'entry')
BEGIN
    DECLARE @owner SYSNAME = (SELECT USER_NAME(principal_id) FROM sys.schemas WHERE name = N'irbs');
    DECLARE @sql NVARCHAR(400) = N'CREATE SCHEMA [entry] AUTHORIZATION ' + QUOTENAME(@owner);
    BEGIN TRY
        EXEC (@sql);
    END TRY
    BEGIN CATCH
        DECLARE @m NVARCHAR(1000) = CONCAT(
            N'Could not create schema [entry] owned by ', QUOTENAME(@owner), N' (the owner of [irbs]). ',
            N'Ask the DBA to run:  CREATE SCHEMA [entry] AUTHORIZATION ', QUOTENAME(@owner),
            N';  then run this script again. Original error: ', ERROR_MESSAGE());
        THROW 50402, @m, 1;
    END CATCH;
END;
GO

/* Stop here if the schema is missing or owned by someone else. */
IF SCHEMA_ID(N'entry') IS NULL
BEGIN
    RAISERROR('Schema [entry] does not exist - see the message above. Nothing else was created.', 16, 1);
    SET NOEXEC ON;      -- skip every remaining batch
END;
IF (SELECT principal_id FROM sys.schemas WHERE name = N'entry') <> (SELECT principal_id FROM sys.schemas WHERE name = N'irbs')
    PRINT 'WARNING: [entry] and [irbs] have different owners. Approvals will fail with permission errors unless the reviewer also has INSERT on the irbs tables. Fix with: ALTER AUTHORIZATION ON SCHEMA::[entry] TO <owner of irbs>;';
GO


/* =====================================================================
   1. TABLES
===================================================================== */
IF OBJECT_ID(N'entry.submission', N'U') IS NULL
BEGIN
    CREATE TABLE entry.submission (
        submission_id      INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_entry_submission PRIMARY KEY,
        submission_type    NVARCHAR(40)  NOT NULL CONSTRAINT DF_entry_submission_type DEFAULT N'HARVEST_EVENT',
        status             NVARCHAR(20)  NOT NULL CONSTRAINT DF_entry_submission_status DEFAULT N'PENDING',

        /* summary for lists (copied from payload at submit) */
        site_code          NVARCHAR(150) NULL,
        sample_date        DATE          NULL,
        reach              NVARCHAR(150) NULL,
        net_count          INT           NULL,
        catch_rows         INT           NULL,
        total_catch        INT           NULL,

        payload            NVARCHAR(MAX) NOT NULL,
        submitter_note     NVARCHAR(MAX) NULL,

        submitted_by       NVARCHAR(150) NOT NULL,       -- person's name typed in the app
        submitted_login    NVARCHAR(150) NOT NULL,       -- SQL login used
        submitted_at       DATETIME2(0)  NOT NULL CONSTRAINT DF_entry_submission_at DEFAULT SYSUTCDATETIME(),

        reviewed_by        NVARCHAR(150) NULL,
        reviewed_login     NVARCHAR(150) NULL,
        reviewed_at        DATETIME2(0)  NULL,
        review_comment     NVARCHAR(MAX) NULL,

        loaded_harvest_event_id BIGINT   NULL,

        CONSTRAINT CK_entry_submission_status
            CHECK (status IN (N'PENDING', N'APPROVED', N'REJECTED', N'WITHDRAWN')),
        CONSTRAINT CK_entry_submission_payload CHECK (ISJSON(payload) = 1)
    );
    CREATE INDEX IX_entry_submission_status ON entry.submission(status, submitted_at);
END;
GO

IF OBJECT_ID(N'entry.submission_log', N'U') IS NULL
BEGIN
    CREATE TABLE entry.submission_log (
        submission_log_id  BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_entry_submission_log PRIMARY KEY,
        submission_id      INT           NOT NULL,
        [action]           NVARCHAR(20)  NOT NULL,       -- SUBMITTED / WITHDRAWN / APPROVED / REJECTED
        action_by          NVARCHAR(150) NOT NULL,
        action_login       NVARCHAR(150) NOT NULL,
        action_at          DATETIME2(0)  NOT NULL CONSTRAINT DF_entry_log_at DEFAULT SYSUTCDATETIME(),
        comment            NVARCHAR(MAX) NULL,
        CONSTRAINT FK_entry_log_submission FOREIGN KEY (submission_id)
            REFERENCES entry.submission(submission_id)
    );
    CREATE INDEX IX_entry_log_submission ON entry.submission_log(submission_id);
END;
GO


/* =====================================================================
   2. VIEWS
===================================================================== */
CREATE OR ALTER VIEW entry.vw_submission
AS
SELECT s.*,
       DATEDIFF(day, s.submitted_at, SYSUTCDATETIME()) AS days_waiting
FROM entry.submission s;
GO

CREATE OR ALTER VIEW entry.vw_submission_log
AS
SELECT * FROM entry.submission_log;
GO

CREATE OR ALTER VIEW entry.vw_species
AS
SELECT species_id, fishcode_4 AS species_code, common_name, scientific_name
FROM irbs.species
WHERE fishcode_4 IS NOT NULL;
GO

CREATE OR ALTER VIEW entry.vw_gear
AS
SELECT gear_id, gear_code, gear_name
FROM irbs.gear
WHERE gear_code IS NOT NULL;
GO

CREATE OR ALTER VIEW entry.vw_reach
AS
SELECT DISTINCT river_other AS reach
FROM irbs.generated_location
WHERE river_other IS NOT NULL;
GO


/* =====================================================================
   3. VALIDATION  (returns one row per problem; level = ERROR or WARNING)
===================================================================== */
CREATE OR ALTER PROCEDURE entry.usp_check_harvest_event
    @payload        NVARCHAR(MAX),
    @submission_id  INT = NULL          -- ignore this submission when looking for duplicates
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @p TABLE (level NVARCHAR(10), section NVARCHAR(20), message NVARCHAR(400));

    IF ISJSON(@payload) <> 1
    BEGIN
        SELECT N'ERROR' AS level, N'form' AS section, N'The form data is not valid JSON.' AS message;
        RETURN;
    END;

    DECLARE @site_code NVARCHAR(150) = NULLIF(LTRIM(RTRIM(JSON_VALUE(@payload, '$.site.site_code'))), N''),
            @date_txt  NVARCHAR(40)  = JSON_VALUE(@payload, '$.site.sample_date'),
            @date      DATE;
    SET @date = TRY_CONVERT(date, @date_txt, 23);

    /* ---- site ---- */
    IF @site_code IS NULL
        INSERT @p VALUES (N'ERROR', N'site', N'Site code is required.');
    IF @date IS NULL
        INSERT @p VALUES (N'ERROR', N'site', N'Sample date is required (YYYY-MM-DD).');
    ELSE IF @date > CAST(SYSDATETIME() AS date)
        INSERT @p VALUES (N'ERROR', N'site', N'Sample date is in the future.');

    IF JSON_VALUE(@payload, '$.site.sample_time') IS NOT NULL
       AND TRY_CONVERT(time, JSON_VALUE(@payload, '$.site.sample_time')) IS NULL
        INSERT @p VALUES (N'ERROR', N'site', N'Sample time is not a valid time (HH:MM).');

    INSERT @p
    SELECT N'ERROR', N'site', N'"' + f.name + N'" must be a number.'
    FROM (VALUES (N'latitude'), (N'longitude'), (N'depth'), (N'secchi_cm'),
                 (N'water_temp_c'), (N'dissolved_oxygen'), (N'conductivity')) f(name)
    WHERE JSON_VALUE(@payload, N'$.site.' + f.name) IS NOT NULL
      AND TRY_CONVERT(decimal(18,6), JSON_VALUE(@payload, N'$.site.' + f.name)) IS NULL;

    IF TRY_CONVERT(decimal(10,6), JSON_VALUE(@payload, '$.site.latitude')) NOT BETWEEN 36 AND 43.6
        INSERT @p VALUES (N'WARNING', N'site', N'Latitude is outside Illinois (36 - 43.6).');
    IF TRY_CONVERT(decimal(10,6), JSON_VALUE(@payload, '$.site.longitude')) NOT BETWEEN -92 AND -87
        INSERT @p VALUES (N'WARNING', N'site', N'Longitude is outside Illinois (-92 to -87). Did you forget the minus sign?');
    IF TRY_CONVERT(decimal(10,3), JSON_VALUE(@payload, '$.site.water_temp_c')) NOT BETWEEN -1 AND 40
        INSERT @p VALUES (N'WARNING', N'site', N'Water temperature looks unusual (expected -1 to 40 °C).');

    IF @site_code IS NOT NULL AND EXISTS (SELECT 1 FROM irbs.harvest_event WHERE source_site_code = @site_code)
        INSERT @p VALUES (N'WARNING', N'site', N'Site code ' + @site_code + N' already exists in the master database.');
    IF @site_code IS NOT NULL AND EXISTS (SELECT 1 FROM entry.submission
                                          WHERE site_code = @site_code AND status = N'PENDING'
                                            AND (@submission_id IS NULL OR submission_id <> @submission_id))
        INSERT @p VALUES (N'WARNING', N'site', N'Another pending submission already uses site code ' + @site_code + N'.');

    /* ---- nets ---- */
    DECLARE @nets TABLE (idx INT, code NVARCHAR(150), gear NVARCHAR(150), set_dt NVARCHAR(40), pull_dt NVARCHAR(40),
                         soak NVARCHAR(40), yardage NVARCHAR(40), mesh NVARCHAR(40));
    INSERT @nets
    SELECT CAST([key] AS INT) + 1,
           NULLIF(LTRIM(RTRIM(JSON_VALUE(value, '$.net_number_code'))), N''),
           NULLIF(JSON_VALUE(value, '$.gear_type'), N''),
           JSON_VALUE(value, '$.set_datetime'), JSON_VALUE(value, '$.pull_datetime'),
           JSON_VALUE(value, '$.soak_time_hours'), JSON_VALUE(value, '$.yardage'), JSON_VALUE(value, '$.mesh_size')
    FROM OPENJSON(@payload, '$.nets');

    INSERT @p SELECT N'ERROR', N'nets', CONCAT(N'Net row ', idx, N': net number/code is required.') FROM @nets WHERE code IS NULL;
    INSERT @p SELECT N'ERROR', N'nets', N'Net code ' + code + N' is entered more than once.'
              FROM @nets WHERE code IS NOT NULL GROUP BY code HAVING COUNT(*) > 1;
    INSERT @p SELECT N'ERROR', N'nets', CONCAT(N'Net ', COALESCE(code, CONCAT(N'row ', idx)), N': ', x.label, N' must be a number.')
              FROM @nets CROSS APPLY (VALUES (N'soak time', soak), (N'yardage', yardage), (N'mesh size', mesh)) x(label, v)
              WHERE x.v IS NOT NULL AND TRY_CONVERT(decimal(18,4), x.v) IS NULL;
    INSERT @p SELECT N'ERROR', N'nets', CONCAT(N'Net ', COALESCE(code, CONCAT(N'row ', idx)), N': pull time is before set time.')
              FROM @nets WHERE TRY_CONVERT(datetime2, pull_dt) < TRY_CONVERT(datetime2, set_dt);
    INSERT @p SELECT N'WARNING', N'nets', CONCAT(N'Net ', code, N': soak time ', soak, N' h differs from set/pull times (',
                     CAST(DATEDIFF(minute, TRY_CONVERT(datetime2, set_dt), TRY_CONVERT(datetime2, pull_dt)) / 60.0 AS DECIMAL(10,1)), N' h).')
              FROM @nets
              WHERE TRY_CONVERT(datetime2, set_dt) IS NOT NULL AND TRY_CONVERT(datetime2, pull_dt) IS NOT NULL
                AND TRY_CONVERT(decimal(18,4), soak) IS NOT NULL
                AND ABS(DATEDIFF(minute, TRY_CONVERT(datetime2, set_dt), TRY_CONVERT(datetime2, pull_dt)) / 60.0
                        - TRY_CONVERT(decimal(18,4), soak)) > 0.5;
    INSERT @p SELECT N'WARNING', N'nets', N'Gear type ' + gear + N' is not in the gear list; it will be added on approval.'
              FROM @nets WHERE gear IS NOT NULL AND NOT EXISTS (SELECT 1 FROM irbs.gear g WHERE g.gear_code = gear);

    /* ---- catch ---- */
    DECLARE @catch TABLE (idx INT, species NVARCHAR(150), cnt NVARCHAR(40), net NVARCHAR(150), removed NVARCHAR(20));
    INSERT @catch
    SELECT CAST([key] AS INT) + 1,
           NULLIF(LTRIM(RTRIM(JSON_VALUE(value, '$.species_code'))), N''),
           JSON_VALUE(value, '$.catch_count'),
           NULLIF(LTRIM(RTRIM(JSON_VALUE(value, '$.net_number_code'))), N''),
           NULLIF(JSON_VALUE(value, '$.removed'), N'')
    FROM OPENJSON(@payload, '$.catch');

    IF NOT EXISTS (SELECT 1 FROM @catch) AND NOT EXISTS (SELECT 1 FROM @nets)
        INSERT @p VALUES (N'WARNING', N'catch', N'No nets and no fish counts entered. Is that intended?');
    INSERT @p SELECT N'ERROR', N'catch', CONCAT(N'Catch row ', idx, N': species is required.') FROM @catch WHERE species IS NULL;
    INSERT @p SELECT N'ERROR', N'catch', CONCAT(N'Catch row ', idx, N': count must be a whole number of 0 or more.')
              FROM @catch WHERE TRY_CONVERT(int, cnt) IS NULL OR TRY_CONVERT(int, cnt) < 0;
    INSERT @p SELECT N'ERROR', N'catch', CONCAT(N'Catch row ', idx, N': net ', net, N' is not one of the nets entered above.')
              FROM @catch c WHERE net IS NOT NULL AND NOT EXISTS (SELECT 1 FROM @nets n WHERE n.code = c.net);
    INSERT @p SELECT N'ERROR', N'catch', CONCAT(N'Catch row ', idx, N': Removed must be Y, N or blank.')
              FROM @catch WHERE removed IS NOT NULL AND removed NOT IN (N'Y', N'N');
    INSERT @p SELECT N'WARNING', N'catch', N'Species ' + species + N' is not in the species list.'
              FROM (SELECT DISTINCT species FROM @catch WHERE species IS NOT NULL) c
              WHERE NOT EXISTS (SELECT 1 FROM irbs.species s WHERE s.fishcode_4 = c.species);
    INSERT @p SELECT N'WARNING', N'catch', CONCAT(N'Catch row ', idx, N': no net selected, so it will be stored at site level (no net effort).')
              FROM @catch WHERE net IS NULL AND EXISTS (SELECT 1 FROM @nets);
    INSERT @p SELECT N'WARNING', N'catch', N'Species ' + species + N' appears more than once for net ' + COALESCE(net, N'(site level)') + N'.'
              FROM @catch WHERE species IS NOT NULL GROUP BY species, net HAVING COUNT(*) > 1;

    /* ---- weights ---- */
    INSERT @p
    SELECT N'ERROR', N'weights', N'Weight "' + f.name + N'" must be a number of 0 or more.'
    FROM (VALUES (N'bhcp_kg'), (N'svcp_kg'), (N'gscp_kg'), (N'hybrid_kg'), (N'unsorted_kg')) f(name)
    WHERE JSON_VALUE(@payload, N'$.weights.' + f.name) IS NOT NULL
      AND (TRY_CONVERT(decimal(18,6), JSON_VALUE(@payload, N'$.weights.' + f.name)) IS NULL
           OR TRY_CONVERT(decimal(18,6), JSON_VALUE(@payload, N'$.weights.' + f.name)) < 0);

    SELECT level, section, message FROM @p
    ORDER BY CASE level WHEN N'ERROR' THEN 0 ELSE 1 END, section;
END;
GO


/* =====================================================================
   4. SUBMIT  (validate, then save as PENDING)
===================================================================== */
CREATE OR ALTER PROCEDURE entry.usp_submit_harvest_event
    @payload       NVARCHAR(MAX),
    @submitted_by  NVARCHAR(150),
    @note          NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF NULLIF(LTRIM(RTRIM(@submitted_by)), N'') IS NULL
        THROW 50410, 'Your name is required.', 1;

    DECLARE @chk TABLE (level NVARCHAR(10), section NVARCHAR(20), message NVARCHAR(400));
    INSERT @chk EXEC entry.usp_check_harvest_event @payload = @payload;
    IF EXISTS (SELECT 1 FROM @chk WHERE level = N'ERROR')
    BEGIN
        DECLARE @msg NVARCHAR(2000) = N'The submission has errors: ' +
            (SELECT STRING_AGG(message, N' | ') FROM @chk WHERE level = N'ERROR');
        THROW 50411, @msg, 1;
    END;

    DECLARE @id INT;
    BEGIN TRANSACTION;
        INSERT entry.submission (site_code, sample_date, reach, net_count, catch_rows, total_catch,
                                 payload, submitter_note, submitted_by, submitted_login)
        VALUES (
            LTRIM(RTRIM(JSON_VALUE(@payload, '$.site.site_code'))),
            TRY_CONVERT(date, JSON_VALUE(@payload, '$.site.sample_date'), 23),
            JSON_VALUE(@payload, '$.site.reach'),
            (SELECT COUNT(*) FROM OPENJSON(@payload, '$.nets')),
            (SELECT COUNT(*) FROM OPENJSON(@payload, '$.catch')),
            (SELECT SUM(TRY_CONVERT(int, JSON_VALUE(value, '$.catch_count'))) FROM OPENJSON(@payload, '$.catch')),
            @payload, NULLIF(@note, N''), LTRIM(RTRIM(@submitted_by)), SUSER_SNAME());
        SET @id = SCOPE_IDENTITY();
        INSERT entry.submission_log (submission_id, [action], action_by, action_login, comment)
        VALUES (@id, N'SUBMITTED', LTRIM(RTRIM(@submitted_by)), SUSER_SNAME(), NULLIF(@note, N''));
    COMMIT;

    SELECT @id AS submission_id;
END;
GO


/* =====================================================================
   5. WITHDRAW  (only PENDING; only the login that submitted it)
===================================================================== */
CREATE OR ALTER PROCEDURE entry.usp_withdraw_submission
    @submission_id INT,
    @by            NVARCHAR(150),
    @comment       NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    BEGIN TRANSACTION;
        UPDATE entry.submission WITH (UPDLOCK)
        SET status = N'WITHDRAWN'
        WHERE submission_id = @submission_id AND status = N'PENDING'
          AND submitted_login = SUSER_SNAME();
        IF @@ROWCOUNT = 0
            THROW 50420, 'Only a pending submission made with your login can be withdrawn.', 1;
        INSERT entry.submission_log (submission_id, [action], action_by, action_login, comment)
        VALUES (@submission_id, N'WITHDRAWN', @by, SUSER_SNAME(), NULLIF(@comment, N''));
    COMMIT;
END;
GO


/* =====================================================================
   6. REVIEW  (APPROVE = load into irbs.*; REJECT = send back)
===================================================================== */
CREATE OR ALTER PROCEDURE entry.usp_review_submission
    @submission_id INT,
    @decision      NVARCHAR(10),          -- APPROVE or REJECT
    @reviewer      NVARCHAR(150),
    @comment       NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF @decision NOT IN (N'APPROVE', N'REJECT')
        THROW 50430, '@decision must be APPROVE or REJECT.', 1;
    IF NULLIF(LTRIM(RTRIM(@reviewer)), N'') IS NULL
        THROW 50431, 'Reviewer name is required.', 1;
    IF @decision = N'REJECT' AND NULLIF(LTRIM(RTRIM(@comment)), N'') IS NULL
        THROW 50432, 'Please give a reason when rejecting, so the submitter knows what to fix.', 1;

    DECLARE @payload NVARCHAR(MAX), @status NVARCHAR(20);
    DECLARE @src_db NVARCHAR(150) = N'IRBS Data Entry';
    DECLARE @sid NVARCHAR(150) = CAST(@submission_id AS NVARCHAR(20));

    BEGIN TRANSACTION;

    SELECT @payload = payload, @status = status
    FROM entry.submission WITH (UPDLOCK, HOLDLOCK)
    WHERE submission_id = @submission_id;

    IF @payload IS NULL
        THROW 50433, 'Submission not found.', 1;
    IF @status <> N'PENDING'
        THROW 50434, 'This submission is no longer pending (already reviewed or withdrawn).', 1;

    IF @decision = N'REJECT'
    BEGIN
        UPDATE entry.submission
        SET status = N'REJECTED', reviewed_by = @reviewer, reviewed_login = SUSER_SNAME(),
            reviewed_at = SYSUTCDATETIME(), review_comment = @comment
        WHERE submission_id = @submission_id;
        INSERT entry.submission_log (submission_id, [action], action_by, action_login, comment)
        VALUES (@submission_id, N'REJECTED', @reviewer, SUSER_SNAME(), @comment);
        COMMIT;
        SELECT @submission_id AS submission_id, N'REJECTED' AS status, CAST(NULL AS BIGINT) AS harvest_event_id;
        RETURN;
    END;

    /* re-validate at approval time (data may have changed since submission) */
    DECLARE @chk TABLE (level NVARCHAR(10), section NVARCHAR(20), message NVARCHAR(400));
    INSERT @chk EXEC entry.usp_check_harvest_event @payload = @payload, @submission_id = @submission_id;
    IF EXISTS (SELECT 1 FROM @chk WHERE level = N'ERROR')
    BEGIN
        DECLARE @emsg NVARCHAR(2000) = N'Cannot approve, the submission has errors: ' +
            (SELECT STRING_AGG(message, N' | ') FROM @chk WHERE level = N'ERROR');
        THROW 50435, @emsg, 1;
    END;

    /* ---------- site / event ---------- */
    DECLARE @site_code NVARCHAR(150) = LTRIM(RTRIM(JSON_VALUE(@payload, '$.site.site_code'))),
            @date DATE = TRY_CONVERT(date, JSON_VALUE(@payload, '$.site.sample_date'), 23),
            @time TIME(0) = TRY_CONVERT(time(0), NULLIF(JSON_VALUE(@payload, '$.site.sample_time'), N'')),
            @location_id BIGINT, @occ_id BIGINT, @event_id BIGINT;

    INSERT irbs.generated_location (source_database, source_table, source_record_id, location_code, [year], river_other)
    VALUES (@src_db, N'entry.submission', @sid, @site_code, YEAR(@date),
            NULLIF(JSON_VALUE(@payload, '$.site.reach'), N''));
    SET @location_id = SCOPE_IDENTITY();

    INSERT irbs.sampling_occurrence (location_id, occurrence_start_datetime, occurrence_time_basis, occurrence_precision,
                                     is_time_inferred, actual_latitude, actual_longitude, location_description, occurrence_notes)
    VALUES (@location_id,
            CASE WHEN @time IS NULL THEN CAST(@date AS datetime2(0)) ELSE DATEADD(second, DATEDIFF(second, '00:00', @time), CAST(@date AS datetime2(0))) END,
            CASE WHEN @time IS NULL THEN N'DATE_ONLY' ELSE N'DATE_AND_TIME' END,
            CASE WHEN @time IS NULL THEN N'DAY' ELSE N'MINUTE' END,
            0,
            TRY_CONVERT(decimal(10,7), JSON_VALUE(@payload, '$.site.latitude')),
            TRY_CONVERT(decimal(10,7), JSON_VALUE(@payload, '$.site.longitude')),
            NULLIF(JSON_VALUE(@payload, '$.site.site_description'), N''),
            CONCAT(N'Entered via IRBS Data Hub, submission ', @sid));
    SET @occ_id = SCOPE_IDENTITY();

    INSERT irbs.harvest_event (sampling_occurrence_id, source_database, source_table, source_record_id,
                               source_site_code, source_site_name, source_site_description, source_fishermen,
                               data_collector, tagging_activity, data_taken_with_tablet, comments)
    VALUES (@occ_id, @src_db, N'entry.submission', @sid, @site_code,
            NULLIF(JSON_VALUE(@payload, '$.site.site_name'), N''),
            NULLIF(JSON_VALUE(@payload, '$.site.site_description'), N''),
            NULLIF(JSON_VALUE(@payload, '$.site.fishermen'), N''),
            NULLIF(JSON_VALUE(@payload, '$.site.data_collector'), N''),
            TRY_CONVERT(bit, JSON_VALUE(@payload, '$.site.tagging_activity')),
            NULL,
            NULLIF(JSON_VALUE(@payload, '$.site.comments'), N''));
    SET @event_id = SCOPE_IDENTITY();

    INSERT irbs.harvest_environment (harvest_event_id, water_depth_m, secchi_depth_cm, water_temp_c,
                                     dissolved_oxygen, dissolved_oxygen_unit,
                                     specific_conductivity, specific_conductivity_unit, comments)
    VALUES (@event_id,
            TRY_CONVERT(decimal(14,4), JSON_VALUE(@payload, '$.site.depth')),
            TRY_CONVERT(decimal(14,4), JSON_VALUE(@payload, '$.site.secchi_cm')),
            TRY_CONVERT(decimal(14,4), JSON_VALUE(@payload, '$.site.water_temp_c')),
            TRY_CONVERT(decimal(14,4), JSON_VALUE(@payload, '$.site.dissolved_oxygen')),
            CASE WHEN JSON_VALUE(@payload, '$.site.dissolved_oxygen') IS NOT NULL THEN N'mg/L' END,
            TRY_CONVERT(decimal(18,6), JSON_VALUE(@payload, '$.site.conductivity')),
            CASE WHEN JSON_VALUE(@payload, '$.site.conductivity') IS NOT NULL THEN N'uS/cm' END,
            NULL);

    INSERT irbs.source_record_map (source_database, source_table, source_record_id, source_business_key,
                                   target_table, target_record_id, resolution_type, notes)
    VALUES (@src_db, N'entry.submission', @sid, @site_code, N'harvest_event', @event_id, N'DATA_ENTRY_APPROVED',
            CONCAT(N'Approved by ', @reviewer));

    /* ---------- gear (add codes that are new) ---------- */
    INSERT irbs.gear (gear_code, gear_name, gear_category, description)
    SELECT DISTINCT g.gear, g.gear, NULL, N'Added from IRBS Data Hub entry'
    FROM (SELECT NULLIF(LTRIM(RTRIM(JSON_VALUE(value, '$.gear_type'))), N'') AS gear FROM OPENJSON(@payload, '$.nets')) g
    WHERE g.gear IS NOT NULL AND NOT EXISTS (SELECT 1 FROM irbs.gear x WHERE x.gear_code = g.gear);

    /* ---------- nets ---------- */
    DECLARE @netmap TABLE (idx INT, code NVARCHAR(150), net_id BIGINT);

    MERGE irbs.harvest_net_set AS t
    USING (
        SELECT CAST(j.[key] AS INT) + 1 AS idx,
               LTRIM(RTRIM(JSON_VALUE(j.value, '$.net_number_code'))) AS code,
               JSON_VALUE(j.value, '$.gear_type')       AS gear,
               JSON_VALUE(j.value, '$.net_number')      AS net_number,
               JSON_VALUE(j.value, '$.set_datetime')    AS set_dt,
               JSON_VALUE(j.value, '$.pull_datetime')   AS pull_dt,
               JSON_VALUE(j.value, '$.soak_time_hours') AS soak,
               JSON_VALUE(j.value, '$.mesh_size')       AS mesh,
               JSON_VALUE(j.value, '$.yardage')         AS yardage,
               JSON_VALUE(j.value, '$.net_depth')       AS depth,
               JSON_VALUE(j.value, '$.color')           AS color,
               JSON_VALUE(j.value, '$.twine')           AS twine,
               JSON_VALUE(j.value, '$.ply')             AS ply,
               JSON_VALUE(j.value, '$.set_type')        AS set_type,
               JSON_VALUE(j.value, '$.comments')        AS comments,
               g.gear_id
        FROM OPENJSON(@payload, '$.nets') j
        LEFT JOIN irbs.gear g ON g.gear_code = NULLIF(LTRIM(RTRIM(JSON_VALUE(j.value, '$.gear_type'))), N'')
    ) AS s
    ON 1 = 0
    WHEN NOT MATCHED THEN INSERT (
        harvest_event_id, gear_id, source_database, source_table, source_record_id,
        source_net_number_code, net_number, set_datetime, pull_datetime, soak_time_hours,
        mesh_size, mesh_size_unit, yardage, yardage_unit, net_depth, net_depth_unit,
        twine, ply, color, set_type, comments)
    VALUES (
        @event_id,
        s.gear_id,
        @src_db, N'entry.submission.nets', CONCAT(@sid, N'-N', s.idx),
        s.code, TRY_CONVERT(int, s.net_number),
        TRY_CONVERT(datetime2(0), NULLIF(s.set_dt, N'')), TRY_CONVERT(datetime2(0), NULLIF(s.pull_dt, N'')),
        TRY_CONVERT(decimal(12,4), s.soak),
        TRY_CONVERT(decimal(14,4), s.mesh), CASE WHEN s.mesh IS NOT NULL THEN N'in' END,
        TRY_CONVERT(decimal(14,4), s.yardage), CASE WHEN s.yardage IS NOT NULL THEN N'yd' END,
        TRY_CONVERT(decimal(14,4), s.depth), CASE WHEN s.depth IS NOT NULL THEN N'ft' END,
        NULLIF(s.twine, N''), NULLIF(s.ply, N''), NULLIF(s.color, N''), NULLIF(s.set_type, N''), NULLIF(s.comments, N''))
    OUTPUT s.idx, s.code, inserted.harvest_net_set_id INTO @netmap (idx, code, net_id);

    INSERT irbs.source_record_map (source_database, source_table, source_record_id, source_business_key,
                                   target_table, target_record_id, resolution_type)
    SELECT @src_db, N'entry.submission.nets', CONCAT(@sid, N'-N', idx), code, N'harvest_net_set', net_id, N'DATA_ENTRY_APPROVED'
    FROM @netmap;

    /* ---------- catch ---------- */
    DECLARE @catchmap TABLE (idx INT, catch_id BIGINT);

    MERGE irbs.harvest_catch_summary AS t
    USING (
        SELECT CAST(c.[key] AS INT) + 1 AS idx,
               LTRIM(RTRIM(JSON_VALUE(c.value, '$.species_code'))) AS species,
               TRY_CONVERT(int, JSON_VALUE(c.value, '$.catch_count')) AS cnt,
               NULLIF(LTRIM(RTRIM(JSON_VALUE(c.value, '$.net_number_code'))), N'') AS net,
               NULLIF(JSON_VALUE(c.value, '$.removed'), N'') AS removed,
               NULLIF(JSON_VALUE(c.value, '$.spawning_scars'), N'') AS scars,
               NULLIF(JSON_VALUE(c.value, '$.condition'), N'') AS cond,
               NULLIF(JSON_VALUE(c.value, '$.comments'), N'') AS comments,
               nm.net_id,
               sp.species_id
        FROM OPENJSON(@payload, '$.catch') c
        LEFT JOIN @netmap nm ON nm.code = NULLIF(LTRIM(RTRIM(JSON_VALUE(c.value, '$.net_number_code'))), N'')
        OUTER APPLY (SELECT TOP (1) x.species_id FROM irbs.species x
                     WHERE x.fishcode_4 = LTRIM(RTRIM(JSON_VALUE(c.value, '$.species_code')))
                     ORDER BY x.species_id) sp
    ) AS s
    ON 1 = 0
    WHEN NOT MATCHED THEN INSERT (
        harvest_event_id, harvest_net_set_id, species_id, source_species_code, catch_count,
        source_removed, removed, spawning_scars, [condition],
        parent_resolution_level, net_resolution_status,
        source_database, source_table, source_record_id, source_site_code, source_net_number_code, comments)
    VALUES (
        @event_id, s.net_id,
        s.species_id,
        s.species, s.cnt,
        s.removed, CASE s.removed WHEN N'Y' THEN 1 WHEN N'N' THEN 0 END, s.scars, s.cond,
        CASE WHEN s.net_id IS NULL THEN N'EVENT' ELSE N'NET' END,
        CASE WHEN s.net_id IS NULL THEN N'ENTERED_WITHOUT_NET' ELSE N'RESOLVED' END,
        @src_db, N'entry.submission.catch', CONCAT(@sid, N'-C', s.idx), @site_code, s.net, s.comments)
    OUTPUT s.idx, inserted.harvest_catch_summary_id INTO @catchmap (idx, catch_id);

    INSERT irbs.source_record_map (source_database, source_table, source_record_id, source_business_key,
                                   target_table, target_record_id, resolution_type)
    SELECT @src_db, N'entry.submission.catch', CONCAT(@sid, N'-C', idx), @site_code, N'harvest_catch_summary', catch_id, N'DATA_ENTRY_APPROVED'
    FROM @catchmap;

    /* ---------- weights (optional) ---------- */
    IF EXISTS (
        SELECT 1 FROM (VALUES (N'bhcp_kg'), (N'svcp_kg'), (N'gscp_kg'), (N'hybrid_kg'), (N'unsorted_kg')) f(name)
        WHERE TRY_CONVERT(decimal(18,6), JSON_VALUE(@payload, N'$.weights.' + f.name)) IS NOT NULL)
    BEGIN
        DECLARE @wrec BIGINT;
        INSERT irbs.harvest_weight_record (harvest_event_id, source_database, source_table, source_record_id, source_site_code, comments)
        VALUES (@event_id, @src_db, N'entry.submission.weights', CONCAT(@sid, N'-W'), @site_code,
                NULLIF(JSON_VALUE(@payload, '$.weights.comments'), N''));
        SET @wrec = SCOPE_IDENTITY();

        INSERT irbs.harvest_weight_summary (harvest_weight_record_id, species_id, source_species_or_group,
                                            weight_kg, weight_type, source_field_name)
        SELECT @wrec,
               (SELECT TOP (1) species_id FROM irbs.species sp WHERE sp.fishcode_4 = f.code ORDER BY species_id),
               f.grp, TRY_CONVERT(decimal(18,6), JSON_VALUE(@payload, N'$.weights.' + f.name)),
               N'SPECIES_GROUP', f.name
        FROM (VALUES (N'bhcp_kg', N'BHCP', N'BHCP'), (N'svcp_kg', N'SVCP', N'SVCP'), (N'gscp_kg', N'GSCP', N'GSCP'),
                     (N'hybrid_kg', N'Hybrid', NULL), (N'unsorted_kg', N'Unsorted', NULL)) f(name, grp, code)
        WHERE TRY_CONVERT(decimal(18,6), JSON_VALUE(@payload, N'$.weights.' + f.name)) IS NOT NULL;

        INSERT irbs.source_record_map (source_database, source_table, source_record_id, source_business_key,
                                       target_table, target_record_id, resolution_type)
        VALUES (@src_db, N'entry.submission.weights', CONCAT(@sid, N'-W'), @site_code,
                N'harvest_weight_record', @wrec, N'DATA_ENTRY_APPROVED');
    END;

    /* ---------- close the submission ---------- */
    UPDATE entry.submission
    SET status = N'APPROVED', reviewed_by = @reviewer, reviewed_login = SUSER_SNAME(),
        reviewed_at = SYSUTCDATETIME(), review_comment = NULLIF(@comment, N''),
        loaded_harvest_event_id = @event_id
    WHERE submission_id = @submission_id;

    INSERT entry.submission_log (submission_id, [action], action_by, action_login, comment)
    VALUES (@submission_id, N'APPROVED', @reviewer, SUSER_SNAME(),
            CONCAT(NULLIF(@comment, N''), CASE WHEN NULLIF(@comment, N'') IS NOT NULL THEN N' | ' END,
                   N'Loaded as harvest_event ', @event_id));
    COMMIT;

    SELECT @submission_id AS submission_id, N'APPROVED' AS status, @event_id AS harvest_event_id;
END;
GO


/* =====================================================================
   7. PERMISSIONS  (DBA)
   Create two roles, add logins to them. Ownership chaining lets the
   procedures write to the tables without granting table permissions.
===================================================================== */
-- CREATE ROLE irbs_data_entry;
-- CREATE ROLE irbs_data_reviewer;
-- GRANT SELECT  ON entry.vw_submission             TO irbs_data_entry;
-- GRANT SELECT  ON entry.vw_submission_log         TO irbs_data_entry;
-- GRANT SELECT  ON entry.vw_species                TO irbs_data_entry;
-- GRANT SELECT  ON entry.vw_gear                   TO irbs_data_entry;
-- GRANT SELECT  ON entry.vw_reach                  TO irbs_data_entry;
-- GRANT EXECUTE ON entry.usp_check_harvest_event   TO irbs_data_entry;
-- GRANT EXECUTE ON entry.usp_submit_harvest_event  TO irbs_data_entry;
-- GRANT EXECUTE ON entry.usp_withdraw_submission   TO irbs_data_entry;
-- GRANT EXECUTE ON entry.usp_review_submission     TO irbs_data_reviewer;
-- ALTER ROLE irbs_data_entry    ADD MEMBER [irbs-posit];      -- team login(s)
-- ALTER ROLE irbs_data_entry    ADD MEMBER [<toby's login>];
-- ALTER ROLE irbs_data_reviewer ADD MEMBER [<toby's login>];  -- reviewer only

PRINT 'Data entry / review objects created or updated.';
GO

SET NOEXEC OFF;     -- always restore normal execution
GO
