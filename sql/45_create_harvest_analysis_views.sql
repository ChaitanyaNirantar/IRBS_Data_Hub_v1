/*
IRBS Harvest - Analysis-ready views (the "front door" to the Harvest data)
=========================================================================
Replaces the merge everyone does by hand (Site Data + Fish Count + Net Data)
and the AC Master queries:

  View                               Replaces AC Master query
  ---------------------------------  ------------------------------------------
  vw_harvest_site_event              Site Data Query
  vw_harvest_catch                   Wes Query, Modeling_Fish Count,
                                     Daily Fish Count, Query1
  vw_harvest_net_effort              Total Net Yardage, Modeling_Net Yards
  vw_harvest_catch_per_net           fish/yd net
  vw_harvest_weights_by_site         Weight_Site_Summaries
  vw_harvest_individual_fish         (Bycatch / Jaw / Acoustic / SeaCarp rows)
  vw_harvest_tag_deployments         Jaw Tagged Count, Query3 (acoustic)
  vw_harvest_tag_recaptures          Recapture Count
  vw_harvest_translocation           (Tagging Info)

Every view carries the same site/date/location columns, so any of them can be
used on its own without further joins. Read-only. Safe to re-run.
Requires SQL Server 2017+ (STRING_AGG).

Notes for analysts
------------------
* Catch rows whose net could not be identified are kept at EVENT level
  (catch_level = 'EVENT'). They count toward site totals but have no net
  effort. vw_harvest_catch_per_net only includes NET-level catches.
* Source rows that could not be loaded at all (rejects) are not in these
  views; see irbs.vw_etl_issue_queue / the QA report.
* VR100 detections are not yet migrated (raw only), so there is no view yet.
*/
SET NOCOUNT ON;
GO

IF OBJECT_ID(N'irbs.harvest_event', N'U') IS NULL
    THROW 50301, 'irbs.harvest_event not found. Run in the IRBS database after the Harvest ETL.', 1;
GO


/* =====================================================================
   1. SITE / EVENT  (one row per Harvest event = one Site Data record)
===================================================================== */
CREATE OR ALTER VIEW irbs.vw_harvest_site_event
AS
SELECT
    e.harvest_event_id,
    e.source_site_code                                   AS site_code,
    CAST(so.occurrence_start_datetime AS date)           AS sample_date,
    COALESCE(YEAR(so.occurrence_start_datetime), gl.[year]) AS [year],
    MONTH(so.occurrence_start_datetime)                  AS [month],
    DAY(so.occurrence_start_datetime)                    AS [day],
    so.occurrence_start_datetime                         AS sample_datetime,
    so.occurrence_time_basis                             AS time_basis,
    gl.river_other                                       AS reach,
    e.source_site_name                                   AS site_name,
    so.actual_latitude                                   AS latitude,
    so.actual_longitude                                  AS longitude,
    so.location_description                              AS site_description,
    env.water_depth_m                                    AS depth,
    env.secchi_depth_cm                                  AS secchi_cm,
    env.water_temp_c,
    env.dissolved_oxygen,
    env.dissolved_oxygen_unit,
    env.specific_conductivity                            AS conductivity,
    env.specific_conductivity_unit                       AS conductivity_unit,
    e.source_fishermen                                   AS fishermen,
    e.data_collector,
    e.tagging_activity,
    e.comments                                           AS site_comments,
    nets.net_count,
    nets.total_yardage,
    nets.total_soak_hours,
    ct.total_catch,
    ct.total_catch_removed,
    wt.total_weight_kg,
    e.source_record_id                                   AS source_site_id
FROM irbs.harvest_event e
JOIN irbs.sampling_occurrence so
  ON so.sampling_occurrence_id = e.sampling_occurrence_id
JOIN irbs.generated_location gl
  ON gl.location_id = so.location_id
LEFT JOIN irbs.harvest_environment env
  ON env.harvest_event_id = e.harvest_event_id
OUTER APPLY (
    SELECT COUNT(*)              AS net_count,
           SUM(n.yardage)        AS total_yardage,
           SUM(n.soak_time_hours) AS total_soak_hours
    FROM irbs.harvest_net_set n
    WHERE n.harvest_event_id = e.harvest_event_id
) nets
OUTER APPLY (
    SELECT SUM(c.catch_count) AS total_catch,
           SUM(CASE WHEN c.removed = 1 THEN c.catch_count END) AS total_catch_removed
    FROM irbs.harvest_catch_summary c
    WHERE c.harvest_event_id = e.harvest_event_id
) ct
OUTER APPLY (
    SELECT SUM(s.weight_kg) AS total_weight_kg
    FROM irbs.harvest_weight_record r
    JOIN irbs.harvest_weight_summary s
      ON s.harvest_weight_record_id = r.harvest_weight_record_id
    WHERE r.harvest_event_id = e.harvest_event_id
      AND UPPER(s.source_species_or_group) NOT LIKE N'%TOTAL%'
) wt;
GO


/* =====================================================================
   2. NET EFFORT  (one row per net set)
===================================================================== */
CREATE OR ALTER VIEW irbs.vw_harvest_net_effort
AS
SELECT
    n.harvest_net_set_id,
    s.harvest_event_id,
    s.site_code, s.sample_date, s.[year], s.[month], s.reach, s.site_name,
    s.latitude, s.longitude, s.water_temp_c, s.depth, s.secchi_cm,
    n.source_net_number_code AS net_number_code,
    n.net_number,
    g.gear_code              AS gear_type,
    n.set_type,
    n.set_datetime,
    n.pull_datetime,
    n.soak_time_hours,
    n.mesh_size, n.mesh_size_unit,
    n.yardage, n.yardage_unit,
    n.net_depth, n.net_depth_unit,
    n.color, n.twine, n.twine_diameter_mm, n.ply,
    c.net_total_catch,
    c.species_count,
    n.comments               AS net_comments,
    n.source_record_id       AS source_net_id
FROM irbs.harvest_net_set n
JOIN irbs.vw_harvest_site_event s
  ON s.harvest_event_id = n.harvest_event_id
LEFT JOIN irbs.gear g
  ON g.gear_id = n.gear_id
OUTER APPLY (
    SELECT SUM(cs.catch_count) AS net_total_catch,
           COUNT(DISTINCT cs.source_species_code) AS species_count
    FROM irbs.harvest_catch_summary cs
    WHERE cs.harvest_net_set_id = n.harvest_net_set_id
) c;
GO


/* =====================================================================
   3. CATCH  (one row per Fish Count row, with site + environment + net)
   The table the predictive models start from.
===================================================================== */
CREATE OR ALTER VIEW irbs.vw_harvest_catch
AS
SELECT
    c.harvest_catch_summary_id,
    s.harvest_event_id,
    s.site_code, s.sample_date, s.[year], s.[month], s.[day], s.sample_datetime,
    s.reach, s.site_name, s.latitude, s.longitude,
    s.depth, s.secchi_cm, s.water_temp_c, s.dissolved_oxygen, s.conductivity,
    c.source_species_code                  AS species_code,
    sp.common_name                         AS species_name,
    sp.scientific_name,
    c.catch_count,
    c.source_removed                       AS removed_source_value,
    c.removed,
    c.spawning_scars,
    c.[condition],
    c.parent_resolution_level              AS catch_level,      -- NET or EVENT
    c.net_resolution_status,
    c.harvest_net_set_id,
    COALESCE(n.source_net_number_code, c.source_net_number_code) AS net_number_code,
    g.gear_code                            AS gear_type,
    n.yardage,
    n.soak_time_hours,
    n.mesh_size,
    n.set_type,
    c.comments                             AS catch_comments,
    c.source_record_id                     AS source_fish_count_id
FROM irbs.harvest_catch_summary c
JOIN irbs.vw_harvest_site_event s
  ON s.harvest_event_id = c.harvest_event_id
LEFT JOIN irbs.harvest_net_set n
  ON n.harvest_net_set_id = c.harvest_net_set_id
LEFT JOIN irbs.gear g
  ON g.gear_id = n.gear_id
LEFT JOIN irbs.species sp
  ON sp.species_id = c.species_id;
GO


/* =====================================================================
   4. CATCH PER NET  (fish per yard / per net-hour; NET-level catches only)
===================================================================== */
CREATE OR ALTER VIEW irbs.vw_harvest_catch_per_net
AS
SELECT
    ne.harvest_net_set_id,
    ne.harvest_event_id,
    ne.site_code, ne.sample_date, ne.[year], ne.[month], ne.reach, ne.site_name,
    ne.latitude, ne.longitude, ne.water_temp_c,
    ne.net_number_code, ne.gear_type, ne.mesh_size, ne.yardage, ne.soak_time_hours,
    c.source_species_code AS species_code,
    MAX(sp.common_name)   AS species_name,
    SUM(c.catch_count)    AS catch_count,
    CAST(SUM(c.catch_count) * 1.0 / NULLIF(ne.yardage, 0) AS DECIMAL(18,4))
                          AS catch_per_yard,
    CAST(SUM(c.catch_count) * 100.0 / NULLIF(ne.yardage, 0) AS DECIMAL(18,3))
                          AS catch_per_100_yards,
    CAST(SUM(c.catch_count) * 1.0 / NULLIF(ne.soak_time_hours, 0) AS DECIMAL(18,4))
                          AS catch_per_net_hour
FROM irbs.harvest_catch_summary c
JOIN irbs.vw_harvest_net_effort ne
  ON ne.harvest_net_set_id = c.harvest_net_set_id
LEFT JOIN irbs.species sp
  ON sp.species_id = c.species_id
GROUP BY
    ne.harvest_net_set_id, ne.harvest_event_id,
    ne.site_code, ne.sample_date, ne.[year], ne.[month], ne.reach, ne.site_name,
    ne.latitude, ne.longitude, ne.water_temp_c,
    ne.net_number_code, ne.gear_type, ne.mesh_size, ne.yardage, ne.soak_time_hours,
    c.source_species_code;
GO


/* =====================================================================
   5. WEIGHTS BY SITE  (one row per Weight Data record, kg by group)
===================================================================== */
CREATE OR ALTER VIEW irbs.vw_harvest_weights_by_site
AS
SELECT
    r.harvest_weight_record_id,
    s.harvest_event_id,
    s.site_code, s.sample_date, s.[year], s.[month], s.reach, s.site_name,
    s.latitude, s.longitude, s.sample_datetime,
    SUM(CASE WHEN UPPER(w.source_species_or_group) LIKE N'%BHCP%'   THEN w.weight_kg END) AS bighead_kg,
    SUM(CASE WHEN UPPER(w.source_species_or_group) LIKE N'%SVCP%'   THEN w.weight_kg END) AS silver_kg,
    SUM(CASE WHEN UPPER(w.source_species_or_group) LIKE N'%GSCP%'   THEN w.weight_kg END) AS grass_kg,
    SUM(CASE WHEN UPPER(w.source_species_or_group) LIKE N'%HYBRID%' THEN w.weight_kg END) AS hybrid_kg,
    SUM(CASE WHEN UPPER(w.source_species_or_group) LIKE N'%UNSORT%' THEN w.weight_kg END) AS unsorted_kg,
    SUM(CASE WHEN UPPER(w.source_species_or_group) NOT LIKE N'%TOTAL%' THEN w.weight_kg END) AS total_kg,
    MAX(CASE WHEN UPPER(w.source_species_or_group) LIKE N'%TOTAL%'  THEN w.weight_kg END) AS recorded_total_kg,
    r.comments            AS weight_comments,
    r.source_record_id    AS source_weight_id
FROM irbs.harvest_weight_record r
JOIN irbs.vw_harvest_site_event s
  ON s.harvest_event_id = r.harvest_event_id
LEFT JOIN irbs.harvest_weight_summary w
  ON w.harvest_weight_record_id = r.harvest_weight_record_id
GROUP BY
    r.harvest_weight_record_id, s.harvest_event_id,
    s.site_code, s.sample_date, s.[year], s.[month], s.reach, s.site_name,
    s.latitude, s.longitude, s.sample_datetime,
    r.comments, r.source_record_id;
GO


/* =====================================================================
   6. INDIVIDUAL FISH  (bycatch, tagged, recaptured, SeaCarp worked-up)
===================================================================== */
CREATE OR ALTER VIEW irbs.vw_harvest_individual_fish
AS
SELECT
    i.harvest_individual_observation_id,
    i.observation_role,
    i.harvest_event_id,
    s.site_code, s.sample_date, s.[year], s.[month], s.reach, s.site_name,
    s.latitude, s.longitude,
    COALESCE(n.source_net_number_code, i.source_net_number_code) AS net_number_code,
    i.source_species_code      AS species_code,
    sp.common_name             AS species_name,
    i.length_mm, i.fork_length_mm, i.length_etf_mm, i.girth_mm,
    i.weight_g, i.gonad_weight_g,
    i.sex, i.source_sex        AS sex_source_value,
    i.gonad_stage, i.spawning_scars, i.fin_clip,
    i.removed, i.source_removed AS removed_source_value,
    i.disposition, i.source_disposition AS disposition_source_value,
    i.included_in_catch_count,
    ag.age_ps, ag.age_pc, ag.age_combined,
    tg.jaw_tag_numbers,
    tg.acoustic_tag_numbers,
    i.parent_resolution_level,
    i.event_resolution_status,
    i.source_table,
    i.source_record_id,
    COALESCE(s.site_code, i.source_site_code) AS source_site_code,
    i.comments
FROM irbs.harvest_individual_observation i
LEFT JOIN irbs.vw_harvest_site_event s
  ON s.harvest_event_id = i.harvest_event_id
LEFT JOIN irbs.harvest_net_set n
  ON n.harvest_net_set_id = i.harvest_net_set_id
LEFT JOIN irbs.species sp
  ON sp.species_id = i.species_id
OUTER APPLY (
    SELECT
        MAX(CASE WHEN a.aging_method_code = N'PS'             THEN a.age_value END) AS age_ps,
        MAX(CASE WHEN a.aging_method_code = N'PC'             THEN a.age_value END) AS age_pc,
        MAX(CASE WHEN a.aging_method_code = N'PS_PC_COMBINED' THEN a.age_value END) AS age_combined
    FROM irbs.harvest_age_estimate a
    WHERE a.harvest_individual_observation_id = i.harvest_individual_observation_id
) ag
OUTER APPLY (
    SELECT
        STRING_AGG(CASE WHEN t.tag_type = N'JAW'      THEN d.source_tag_number END, N', ') AS jaw_tag_numbers,
        STRING_AGG(CASE WHEN t.tag_type = N'ACOUSTIC' THEN d.source_tag_number END, N', ') AS acoustic_tag_numbers
    FROM irbs.harvest_tag_deployment d
    LEFT JOIN irbs.tag t ON t.tag_id = d.tag_id
    WHERE d.harvest_individual_observation_id = i.harvest_individual_observation_id
) tg;
GO


/* =====================================================================
   7. TAG DEPLOYMENTS  (jaw + acoustic tags put on fish)
===================================================================== */
CREATE OR ALTER VIEW irbs.vw_harvest_tag_deployments
AS
SELECT
    d.harvest_tag_deployment_id,
    COALESCE(t.tag_type, N'UNKNOWN') AS tag_type,
    d.deployment_role,
    COALESCE(t.tag_code, d.source_tag_number) AS tag_number,
    d.deployment_datetime,
    d.deployment_time_basis,
    d.status,
    f.observation_role,
    f.harvest_event_id,
    f.site_code, f.sample_date, f.[year], f.[month], f.reach, f.site_name,
    f.latitude, f.longitude, f.net_number_code,
    f.species_code, f.species_name,
    f.length_mm, f.weight_g, f.sex,
    f.disposition                    AS release_or_kill,
    f.jaw_tag_numbers                AS all_jaw_tags_on_fish,
    rc.recapture_count,
    rc.last_recapture_date,
    d.source_table,
    d.source_record_id,
    d.comments
FROM irbs.harvest_tag_deployment d
LEFT JOIN irbs.tag t
  ON t.tag_id = d.tag_id
LEFT JOIN irbs.vw_harvest_individual_fish f
  ON f.harvest_individual_observation_id = d.harvest_individual_observation_id
OUTER APPLY (
    SELECT COUNT(*) AS recapture_count,
           MAX(CAST(so.occurrence_start_datetime AS date)) AS last_recapture_date
    FROM irbs.harvest_tag_encounter en
    LEFT JOIN irbs.harvest_event re ON re.harvest_event_id = en.harvest_event_id
    LEFT JOIN irbs.sampling_occurrence so ON so.sampling_occurrence_id = re.sampling_occurrence_id
    WHERE en.harvest_tag_deployment_id = d.harvest_tag_deployment_id
) rc;
GO


/* =====================================================================
   8. TAG RECAPTURES  (each recapture with where/when it was first tagged)
===================================================================== */
CREATE OR ALTER VIEW irbs.vw_harvest_tag_recaptures
AS
SELECT
    en.harvest_tag_encounter_id,
    en.encounter_type,
    COALESCE(t.tag_type, N'UNKNOWN')          AS tag_type,
    COALESCE(t.tag_code, en.source_tag_number) AS tag_number,
    /* recapture */
    rs.site_code      AS recapture_site_code,
    rs.sample_date    AS recapture_date,
    rs.[year]         AS recapture_year,
    rs.reach          AS recapture_reach,
    rs.site_name      AS recapture_site_name,
    rs.latitude       AS recapture_latitude,
    rs.longitude      AS recapture_longitude,
    en.tag_removed,
    en.source_removed AS removed_source_value,
    en.tag_status,
    en.source_status  AS status_source_value,
    /* original tagging */
    dp.site_code      AS tagged_site_code,
    dp.sample_date    AS tagged_date,
    dp.reach          AS tagged_reach,
    dp.latitude       AS tagged_latitude,
    dp.longitude      AS tagged_longitude,
    COALESCE(dp.species_code, rf.species_code) AS species_code,
    COALESCE(dp.species_name, rf.species_name) AS species_name,
    dp.length_mm      AS length_at_tagging_mm,
    rf.length_mm      AS length_at_recapture_mm,
    DATEDIFF(day, dp.sample_date, rs.sample_date) AS days_at_large,
    en.tag_resolution_status,
    en.event_resolution_status,
    en.source_table,
    en.source_record_id,
    en.comments
FROM irbs.harvest_tag_encounter en
LEFT JOIN irbs.tag t
  ON t.tag_id = en.tag_id
LEFT JOIN irbs.vw_harvest_site_event rs
  ON rs.harvest_event_id = en.harvest_event_id
LEFT JOIN irbs.vw_harvest_tag_deployments dp
  ON dp.harvest_tag_deployment_id = en.harvest_tag_deployment_id
LEFT JOIN irbs.vw_harvest_individual_fish rf
  ON rf.harvest_individual_observation_id = en.harvest_individual_observation_id;
GO


/* =====================================================================
   9. TRANSLOCATION  (Tagging Info: agency / release pool summaries)
===================================================================== */
CREATE OR ALTER VIEW irbs.vw_harvest_translocation
AS
SELECT
    tr.harvest_translocation_summary_id,
    s.harvest_event_id,
    s.site_code, s.sample_date, s.[year], s.[month], s.reach, s.site_name,
    tr.agency,
    tr.translocated,
    tr.source_translocated     AS translocated_source_value,
    tr.source_release_pool     AS release_pool,
    tr.source_species_code     AS species_code,
    sp.common_name             AS species_name,
    tr.fish_count,
    tr.weight_kg,
    tr.source_record_id,
    tr.comments
FROM irbs.harvest_translocation_summary tr
JOIN irbs.vw_harvest_site_event s
  ON s.harvest_event_id = tr.harvest_event_id
LEFT JOIN irbs.species sp
  ON sp.species_id = tr.species_id;
GO


/* =====================================================================
   Grants (DBA): read-only login used by the data portal script
===================================================================== */
-- GRANT SELECT ON SCHEMA::irbs TO [irbs-posit];      -- simplest, or per view:
-- GRANT SELECT ON irbs.vw_harvest_site_event      TO [irbs-posit];
-- GRANT SELECT ON irbs.vw_harvest_catch           TO [irbs-posit];
-- GRANT SELECT ON irbs.vw_harvest_net_effort      TO [irbs-posit];
-- GRANT SELECT ON irbs.vw_harvest_catch_per_net   TO [irbs-posit];
-- GRANT SELECT ON irbs.vw_harvest_weights_by_site TO [irbs-posit];
-- GRANT SELECT ON irbs.vw_harvest_individual_fish TO [irbs-posit];
-- GRANT SELECT ON irbs.vw_harvest_tag_deployments TO [irbs-posit];
-- GRANT SELECT ON irbs.vw_harvest_tag_recaptures  TO [irbs-posit];
-- GRANT SELECT ON irbs.vw_harvest_translocation   TO [irbs-posit];

PRINT 'Harvest analysis views created/updated.';
GO
