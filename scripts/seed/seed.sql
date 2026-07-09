-- =============================================================================
-- AgentForge synthetic clinical seed (demo data only — never real PHI)
-- -----------------------------------------------------------------------------
-- Populates a rounding-list-sized inpatient cohort with the clinical data the
-- MVP audit + hospitalist agent depend on: encounters, vitals trends, labs
-- with reference ranges and abnormal flags, meds in BOTH `lists` and
-- `prescriptions` (with an intentional mismatch), problems, allergies (with a
-- drug-allergy conflict), SOAP notes, and one "overnight change" patient with
-- a critical lab + documented event stamped in the last few hours.
--
-- Idempotent: every seed row is tagged `external_id='SEED'` and wiped on
-- re-run. Fixed patient IDs 1001..1015 so re-seeds are stable across runs.
-- =============================================================================

-- Relax strict mode so NOT-NULL columns without defaults (a legacy OpenEMR
-- pattern — e.g. prescriptions.usage_category_title) accept an implicit ''.
SET SESSION sql_mode = REPLACE(REPLACE(@@SESSION.sql_mode, 'STRICT_TRANS_TABLES', ''), 'STRICT_ALL_TABLES', '');

SET @NOW = NOW();
SET @T_MINUS_2H = DATE_SUB(@NOW, INTERVAL 2 HOUR);
SET @T_MINUS_18H = DATE_SUB(@NOW, INTERVAL 18 HOUR);
SET @T_MINUS_1D = DATE_SUB(@NOW, INTERVAL 1 DAY);
SET @T_MINUS_2D = DATE_SUB(@NOW, INTERVAL 2 DAY);
SET @T_MINUS_3D = DATE_SUB(@NOW, INTERVAL 3 DAY);

-- -----------------------------------------------------------------------------
-- 0. Wipe prior seed rows (marker: external_id='SEED') so this is re-runnable.
-- -----------------------------------------------------------------------------
-- order_code first, while the SEED orders it references still exist.
DELETE FROM procedure_order_code WHERE procedure_order_id IN (SELECT procedure_order_id FROM procedure_order WHERE external_id='SEED');
DELETE FROM procedure_result WHERE procedure_report_id IN (
  SELECT procedure_report_id FROM procedure_report
   WHERE procedure_order_id IN (SELECT procedure_order_id FROM procedure_order WHERE external_id='SEED')
);
DELETE FROM procedure_report WHERE procedure_order_id IN (SELECT procedure_order_id FROM procedure_order WHERE external_id='SEED');
DELETE FROM procedure_order  WHERE external_id='SEED';
DELETE FROM procedure_type   WHERE procedure_code LIKE 'SEED-%';

DELETE FROM form_vitals    WHERE external_id='SEED';
DELETE FROM form_soap      WHERE pid BETWEEN 1001 AND 1099;
DELETE FROM forms          WHERE pid BETWEEN 1001 AND 1099;
DELETE FROM form_encounter WHERE external_id='SEED';

DELETE FROM prescriptions  WHERE external_id='SEED';
DELETE FROM lists          WHERE external_id='SEED';
DELETE FROM pnotes         WHERE pid BETWEEN 1001 AND 1099;

DELETE FROM patient_data   WHERE pid BETWEEN 1001 AND 1099;

DELETE FROM uuid_registry  WHERE table_name IN (
  'patient_data','form_encounter','form_vitals','form_soap','forms',
  'lists','prescriptions','procedure_order','procedure_report','procedure_result'
) AND table_id BETWEEN 1000 AND 999999;

-- -----------------------------------------------------------------------------
-- 1. Provider user + inpatient facility (idempotent upserts)
-- -----------------------------------------------------------------------------
INSERT INTO users (id, username, fname, lname, authorized, active, npi, title, specialty, facility_id, source, taxonomy)
VALUES (101, 'dr_chen', 'Grace', 'Chen', 1, 1, '1234567890', 'MD', 'Hospitalist', 3, 0, '207R00000X')
ON DUPLICATE KEY UPDATE fname=VALUES(fname), lname=VALUES(lname), active=1, authorized=1;

INSERT INTO facility (id, name, phone, street, city, state, postal_code, country_code,
                      service_location, billing_location, accepts_assignment, pos_code,
                      tax_id_type, color, primary_business_entity, extra_validation,
                      oid, organization_type)
VALUES (101, 'AgentForge General Hospital', '555-0100', '1 Hospital Way', 'Austin', 'TX',
        '78701', 'USA', 1, 0, 1, 21, 'EI', '#4a90e2', 0, 1, '', 'prov')
ON DUPLICATE KEY UPDATE name=VALUES(name);

-- -----------------------------------------------------------------------------
-- 2. Patients (fixed pids 1001..1015; last one 1015 is the "overnight change")
-- -----------------------------------------------------------------------------
INSERT INTO patient_data
  (pid, id, uuid, fname, lname, sex, DOB, ss, street, city, state, postal_code,
   country_code, status, ethnicity, race, language, financial, title, hipaa_notice,
   hipaa_message, hipaa_allowsms, hipaa_allowemail, squad, referral_source,
   pricelevel, vfc, mothersname, allow_imm_reg_use, allow_imm_info_share,
   allow_health_info_ex, allow_patient_portal, cmsportal_login, county, deceased_reason)
VALUES
  (1001, 1001, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001001','-','')), 'Marcus','Alvarez','Male','1948-03-12','','101 Oak St','Austin','TX','78701','USA','married','not_hisp_latin','white','English','','Mr.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1002, 1002, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001002','-','')), 'Beatrice','Bennett','Female','1952-07-29','','202 Elm St','Austin','TX','78702','USA','widowed','not_hisp_latin','black_afri_amer','English','','Mrs.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1003, 1003, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001003','-','')), 'Chidi','Okafor','Male','1965-11-04','','303 Pine St','Austin','TX','78703','USA','married','not_hisp_latin','black_afri_amer','English','','Mr.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1004, 1004, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001004','-','')), 'Dolores','Chen','Female','1937-01-18','','404 Cedar Ln','Austin','TX','78704','USA','widowed','not_hisp_latin','asian','English','','Mrs.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1005, 1005, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001005','-','')), 'Ephraim','Diaz','Male','1974-05-22','','505 Birch Ct','Austin','TX','78705','USA','single','hisp_latin','white','English','','Mr.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1006, 1006, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001006','-','')), 'Fatima','Eze','Female','1990-09-08','','606 Maple Dr','Austin','TX','78706','USA','married','not_hisp_latin','black_afri_amer','English','','Ms.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1007, 1007, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001007','-','')), 'Gustavo','Ferrari','Male','1958-12-30','','707 Walnut Way','Austin','TX','78707','USA','married','hisp_latin','white','English','','Mr.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1008, 1008, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001008','-','')), 'Harper','Gupta','Female','1981-04-14','','808 Willow St','Austin','TX','78708','USA','divorced','not_hisp_latin','asian','English','','Ms.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1009, 1009, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001009','-','')), 'Isaac','Hoffman','Male','1941-10-02','','909 Spruce Ave','Austin','TX','78709','USA','married','not_hisp_latin','white','English','','Mr.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1010, 1010, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001010','-','')), 'Jenna','Iverson','Female','1969-06-17','','1010 Aspen Rd','Austin','TX','78710','USA','married','not_hisp_latin','white','English','','Mrs.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1011, 1011, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001011','-','')), 'Kenji','Jaeger','Male','1955-02-25','','1111 Redwood Ln','Austin','TX','78711','USA','married','not_hisp_latin','asian','English','','Mr.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1012, 1012, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001012','-','')), 'Lucia','Kowalski','Female','1946-08-11','','1212 Fir St','Austin','TX','78712','USA','widowed','not_hisp_latin','white','English','','Mrs.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1013, 1013, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001013','-','')), 'Mateo','Larsson','Male','1978-03-05','','1313 Poplar Way','Austin','TX','78713','USA','single','hisp_latin','white','English','','Mr.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1014, 1014, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001014','-','')), 'Nia','Mensah','Female','1962-11-19','','1414 Sycamore Blvd','Austin','TX','78714','USA','divorced','not_hisp_latin','black_afri_amer','English','','Ms.','NO','','NO','NO','','','standard','','',
    '','','','','','',''),
  (1015, 1015, UNHEX(REPLACE('a1000000-0000-0000-0000-000000001015','-','')), 'Oren','Novak','Male','1959-07-07','','1515 Magnolia Ct','Austin','TX','78715','USA','married','not_hisp_latin','white','English','','Mr.','NO','','NO','NO','','','standard','','',
    '','','','','','','');

INSERT INTO uuid_registry (uuid, table_name, table_id, table_vertical, couchdb, document_drive, mapped, created)
SELECT uuid, 'patient_data', CAST(pid AS CHAR), '', '', 0, 0, NOW() FROM patient_data WHERE pid BETWEEN 1001 AND 1015;

-- Bump the sequences table so future manual patient adds don't collide.
UPDATE sequences SET id = GREATEST(id, 1015);

-- -----------------------------------------------------------------------------
-- 3. Inpatient encounters (class IMP), one per patient, admitted 2-3 days ago.
--    encounter numbers 2001..2015 (parallel to pids 1001..1015).
-- -----------------------------------------------------------------------------
INSERT INTO form_encounter
  (id, uuid, date, reason, facility, facility_id, pid, encounter, onset_date,
   sensitivity, pc_catid, provider_id, billing_facility, external_id, pos_code,
   class_code, encounter_type_code, encounter_type_description)
VALUES
  (2001, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002001','-','')), @T_MINUS_2D, 'CHF exacerbation',                    'AgentForge General Hospital', 101, 1001, 2001, @T_MINUS_2D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2002, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002002','-','')), @T_MINUS_3D, 'Community-acquired pneumonia',         'AgentForge General Hospital', 101, 1002, 2002, @T_MINUS_3D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2003, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002003','-','')), @T_MINUS_2D, 'Diabetic ketoacidosis',                 'AgentForge General Hospital', 101, 1003, 2003, @T_MINUS_2D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2004, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002004','-','')), @T_MINUS_3D, 'UTI with sepsis',                       'AgentForge General Hospital', 101, 1004, 2004, @T_MINUS_3D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2005, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002005','-','')), @T_MINUS_2D, 'Acute pancreatitis',                    'AgentForge General Hospital', 101, 1005, 2005, @T_MINUS_2D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2006, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002006','-','')), @T_MINUS_2D, 'Cellulitis, left lower extremity',       'AgentForge General Hospital', 101, 1006, 2006, @T_MINUS_2D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2007, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002007','-','')), @T_MINUS_3D, 'COPD exacerbation',                     'AgentForge General Hospital', 101, 1007, 2007, @T_MINUS_3D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2008, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002008','-','')), @T_MINUS_2D, 'Acute kidney injury',                   'AgentForge General Hospital', 101, 1008, 2008, @T_MINUS_2D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2009, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002009','-','')), @T_MINUS_2D, 'GI bleed, upper',                       'AgentForge General Hospital', 101, 1009, 2009, @T_MINUS_2D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2010, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002010','-','')), @T_MINUS_3D, 'Pulmonary embolism',                    'AgentForge General Hospital', 101, 1010, 2010, @T_MINUS_3D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2011, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002011','-','')), @T_MINUS_2D, 'Atrial fibrillation with RVR',           'AgentForge General Hospital', 101, 1011, 2011, @T_MINUS_2D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2012, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002012','-','')), @T_MINUS_3D, 'Ischemic stroke',                       'AgentForge General Hospital', 101, 1012, 2012, @T_MINUS_3D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2013, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002013','-','')), @T_MINUS_2D, 'Alcohol withdrawal',                    'AgentForge General Hospital', 101, 1013, 2013, @T_MINUS_2D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2014, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002014','-','')), @T_MINUS_2D, 'Hyponatremia, symptomatic',              'AgentForge General Hospital', 101, 1014, 2014, @T_MINUS_2D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care'),
  (2015, UNHEX(REPLACE('e1000000-0000-0000-0000-000000002015','-','')), @T_MINUS_2D, 'Chest pain, rule-out ACS',              'AgentForge General Hospital', 101, 1015, 2015, @T_MINUS_2D, 'normal', 5, 101, 101, 'SEED', 21, 'IMP', '99223', 'Initial hospital care');

INSERT INTO uuid_registry (uuid, table_name, table_id, table_vertical, couchdb, document_drive, mapped, created)
SELECT uuid, 'form_encounter', CAST(id AS CHAR), '', '', 0, 0, NOW() FROM form_encounter WHERE external_id='SEED';

-- -----------------------------------------------------------------------------
-- 4. Vitals trends (3 timepoints per patient: admission, +1d, most recent).
-- -----------------------------------------------------------------------------
INSERT INTO form_vitals
  (id, uuid, date, pid, user, groupname, authorized, activity, bps, bpd,
   weight, height, temperature, pulse, respiration, oxygen_saturation, note, external_id)
VALUES
  -- Pt 1001 (CHF) — mild trend toward improvement, still tachy
  (3001, UNHEX(REPLACE('af000000-0000-0000-0000-000000003001','-','')), @T_MINUS_2D,  1001, 'dr_chen', 'Default', 1, 1, '158','92',  195.0, 70.0, 98.4, 108, 22, 91.00, 'On 4L NC', 'SEED'),
  (3002, UNHEX(REPLACE('af000000-0000-0000-0000-000000003002','-','')), @T_MINUS_1D,  1001, 'dr_chen', 'Default', 1, 1, '142','86',  192.4, 70.0, 98.2, 96,  20, 94.00, 'On 2L NC', 'SEED'),
  (3003, UNHEX(REPLACE('af000000-0000-0000-0000-000000003003','-','')), @T_MINUS_2H,  1001, 'dr_chen', 'Default', 1, 1, '134','82',  190.2, 70.0, 98.1, 88,  18, 96.00, 'RA',       'SEED'),
  -- Pt 1002 (PNA) — febrile trend
  (3004, UNHEX(REPLACE('af000000-0000-0000-0000-000000003004','-','')), @T_MINUS_3D,  1002, 'dr_chen', 'Default', 1, 1, '128','78',  145.0, 65.0, 101.8,102, 24, 89.00, 'On 4L NC', 'SEED'),
  (3005, UNHEX(REPLACE('af000000-0000-0000-0000-000000003005','-','')), @T_MINUS_1D,  1002, 'dr_chen', 'Default', 1, 1, '124','76',  144.5, 65.0, 100.4, 92, 22, 93.00, 'On 2L NC', 'SEED'),
  (3006, UNHEX(REPLACE('af000000-0000-0000-0000-000000003006','-','')), @T_MINUS_2H,  1002, 'dr_chen', 'Default', 1, 1, '118','72',  144.0, 65.0,  99.6, 84, 18, 96.00, 'RA',       'SEED'),
  -- Pt 1003 (DKA) — tachy, resolving
  (3007, UNHEX(REPLACE('af000000-0000-0000-0000-000000003007','-','')), @T_MINUS_2D,  1003, 'dr_chen', 'Default', 1, 1, '112','68',  180.0, 71.0, 99.2, 118, 26, 97.00, 'Kussmaul', 'SEED'),
  (3008, UNHEX(REPLACE('af000000-0000-0000-0000-000000003008','-','')), @T_MINUS_1D,  1003, 'dr_chen', 'Default', 1, 1, '118','72',  178.5, 71.0, 98.8, 104, 20, 98.00, '',         'SEED'),
  (3009, UNHEX(REPLACE('af000000-0000-0000-0000-000000003009','-','')), @T_MINUS_2H,  1003, 'dr_chen', 'Default', 1, 1, '124','74',  177.9, 71.0, 98.6,  92, 18, 98.00, '',         'SEED'),
  -- Pt 1004 (UTI/sepsis) — early septic picture resolving
  (3010, UNHEX(REPLACE('af000000-0000-0000-0000-000000003010','-','')), @T_MINUS_3D,  1004, 'dr_chen', 'Default', 1, 1, '92','54',   118.0, 61.0, 102.4,124, 28, 92.00, 'Confused', 'SEED'),
  (3011, UNHEX(REPLACE('af000000-0000-0000-0000-000000003011','-','')), @T_MINUS_1D,  1004, 'dr_chen', 'Default', 1, 1, '108','66',  119.0, 61.0, 100.2,102, 22, 95.00, '',         'SEED'),
  (3012, UNHEX(REPLACE('af000000-0000-0000-0000-000000003012','-','')), @T_MINUS_2H,  1004, 'dr_chen', 'Default', 1, 1, '116','70',  119.2, 61.0,  99.4, 88, 18, 97.00, '',         'SEED'),
  -- Pt 1005 (pancreatitis) — pain-driven tachy
  (3013, UNHEX(REPLACE('af000000-0000-0000-0000-000000003013','-','')), @T_MINUS_2D,  1005, 'dr_chen', 'Default', 1, 1, '138','84',  210.0, 72.0, 99.6, 112, 22, 96.00, '',         'SEED'),
  (3014, UNHEX(REPLACE('af000000-0000-0000-0000-000000003014','-','')), @T_MINUS_1D,  1005, 'dr_chen', 'Default', 1, 1, '132','80',  208.5, 72.0, 99.2, 98,  18, 97.00, '',         'SEED'),
  (3015, UNHEX(REPLACE('af000000-0000-0000-0000-000000003015','-','')), @T_MINUS_2H,  1005, 'dr_chen', 'Default', 1, 1, '128','78',  208.0, 72.0, 98.8, 86,  16, 98.00, '',         'SEED'),
  -- Pt 1006 (cellulitis) — mostly stable
  (3016, UNHEX(REPLACE('af000000-0000-0000-0000-000000003016','-','')), @T_MINUS_2D,  1006, 'dr_chen', 'Default', 1, 1, '124','76',  165.0, 66.0, 100.6, 96, 18, 97.00, '',         'SEED'),
  (3017, UNHEX(REPLACE('af000000-0000-0000-0000-000000003017','-','')), @T_MINUS_1D,  1006, 'dr_chen', 'Default', 1, 1, '122','74',  165.0, 66.0,  99.4, 88, 16, 98.00, '',         'SEED'),
  (3018, UNHEX(REPLACE('af000000-0000-0000-0000-000000003018','-','')), @T_MINUS_2H,  1006, 'dr_chen', 'Default', 1, 1, '120','72',  164.8, 66.0,  98.6, 76, 16, 98.00, '',         'SEED'),
  -- Pt 1007 (COPD) — hypercapnic-looking
  (3019, UNHEX(REPLACE('af000000-0000-0000-0000-000000003019','-','')), @T_MINUS_3D,  1007, 'dr_chen', 'Default', 1, 1, '148','88',  172.0, 68.0, 98.6, 96,  26, 87.00, 'BiPAP',    'SEED'),
  (3020, UNHEX(REPLACE('af000000-0000-0000-0000-000000003020','-','')), @T_MINUS_1D,  1007, 'dr_chen', 'Default', 1, 1, '142','84',  171.6, 68.0, 98.4, 88,  22, 90.00, 'On 3L NC', 'SEED'),
  (3021, UNHEX(REPLACE('af000000-0000-0000-0000-000000003021','-','')), @T_MINUS_2H,  1007, 'dr_chen', 'Default', 1, 1, '138','82',  171.0, 68.0, 98.2, 82,  18, 93.00, 'On 2L NC', 'SEED'),
  -- Pt 1008 (AKI) — normalizing
  (3022, UNHEX(REPLACE('af000000-0000-0000-0000-000000003022','-','')), @T_MINUS_2D,  1008, 'dr_chen', 'Default', 1, 1, '156','94',  140.0, 63.0, 98.8, 92,  18, 97.00, '',         'SEED'),
  (3023, UNHEX(REPLACE('af000000-0000-0000-0000-000000003023','-','')), @T_MINUS_1D,  1008, 'dr_chen', 'Default', 1, 1, '142','88',  138.5, 63.0, 98.6, 84,  16, 98.00, '',         'SEED'),
  (3024, UNHEX(REPLACE('af000000-0000-0000-0000-000000003024','-','')), @T_MINUS_2H,  1008, 'dr_chen', 'Default', 1, 1, '134','82',  138.0, 63.0, 98.4, 78,  16, 98.00, '',         'SEED'),
  -- Pt 1009 (GIB) — HD-fluctuating
  (3025, UNHEX(REPLACE('af000000-0000-0000-0000-000000003025','-','')), @T_MINUS_2D,  1009, 'dr_chen', 'Default', 1, 1, '102','62',  180.0, 69.0, 98.2, 118, 20, 97.00, 'Post-transfusion','SEED'),
  (3026, UNHEX(REPLACE('af000000-0000-0000-0000-000000003026','-','')), @T_MINUS_1D,  1009, 'dr_chen', 'Default', 1, 1, '118','72',  180.0, 69.0, 98.2, 96,  18, 98.00, '',         'SEED'),
  (3027, UNHEX(REPLACE('af000000-0000-0000-0000-000000003027','-','')), @T_MINUS_2H,  1009, 'dr_chen', 'Default', 1, 1, '122','74',  180.0, 69.0, 98.2, 88,  16, 98.00, '',         'SEED'),
  -- Pt 1010 (PE) — on heparin
  (3028, UNHEX(REPLACE('af000000-0000-0000-0000-000000003028','-','')), @T_MINUS_3D,  1010, 'dr_chen', 'Default', 1, 1, '126','78',  150.0, 64.0, 99.0, 108, 24, 90.00, 'On 3L NC', 'SEED'),
  (3029, UNHEX(REPLACE('af000000-0000-0000-0000-000000003029','-','')), @T_MINUS_1D,  1010, 'dr_chen', 'Default', 1, 1, '124','76',  149.5, 64.0, 98.6, 92,  20, 94.00, 'On 2L NC', 'SEED'),
  (3030, UNHEX(REPLACE('af000000-0000-0000-0000-000000003030','-','')), @T_MINUS_2H,  1010, 'dr_chen', 'Default', 1, 1, '122','74',  149.2, 64.0, 98.4, 84,  18, 96.00, 'RA',       'SEED'),
  -- Pt 1011 (AFib/RVR) — rate-controlled
  (3031, UNHEX(REPLACE('af000000-0000-0000-0000-000000003031','-','')), @T_MINUS_2D,  1011, 'dr_chen', 'Default', 1, 1, '132','82',  185.0, 68.0, 98.6, 142, 20, 96.00, 'HR 130s',   'SEED'),
  (3032, UNHEX(REPLACE('af000000-0000-0000-0000-000000003032','-','')), @T_MINUS_1D,  1011, 'dr_chen', 'Default', 1, 1, '128','78',  184.8, 68.0, 98.4, 108, 18, 97.00, '',         'SEED'),
  (3033, UNHEX(REPLACE('af000000-0000-0000-0000-000000003033','-','')), @T_MINUS_2H,  1011, 'dr_chen', 'Default', 1, 1, '124','76',  184.5, 68.0, 98.2, 88,  16, 98.00, '',         'SEED'),
  -- Pt 1012 (CVA) — permissive HTN
  (3034, UNHEX(REPLACE('af000000-0000-0000-0000-000000003034','-','')), @T_MINUS_3D,  1012, 'dr_chen', 'Default', 1, 1, '168','96',  155.0, 63.0, 98.4, 88,  18, 98.00, '',         'SEED'),
  (3035, UNHEX(REPLACE('af000000-0000-0000-0000-000000003035','-','')), @T_MINUS_1D,  1012, 'dr_chen', 'Default', 1, 1, '162','92',  154.5, 63.0, 98.4, 82,  16, 98.00, '',         'SEED'),
  (3036, UNHEX(REPLACE('af000000-0000-0000-0000-000000003036','-','')), @T_MINUS_2H,  1012, 'dr_chen', 'Default', 1, 1, '156','88',  154.5, 63.0, 98.4, 78,  16, 98.00, '',         'SEED'),
  -- Pt 1013 (ETOH withdrawal) — CIWA-driven
  (3037, UNHEX(REPLACE('af000000-0000-0000-0000-000000003037','-','')), @T_MINUS_2D,  1013, 'dr_chen', 'Default', 1, 1, '154','92',  195.0, 71.0, 100.4,124, 22, 98.00, 'CIWA 22',  'SEED'),
  (3038, UNHEX(REPLACE('af000000-0000-0000-0000-000000003038','-','')), @T_MINUS_1D,  1013, 'dr_chen', 'Default', 1, 1, '142','88',  194.5, 71.0,  99.4,102, 18, 98.00, 'CIWA 12',  'SEED'),
  (3039, UNHEX(REPLACE('af000000-0000-0000-0000-000000003039','-','')), @T_MINUS_2H,  1013, 'dr_chen', 'Default', 1, 1, '132','82',  194.2, 71.0,  98.6, 84, 16, 98.00, 'CIWA 6',   'SEED'),
  -- Pt 1014 (hyponatremia) — Na being corrected
  (3040, UNHEX(REPLACE('af000000-0000-0000-0000-000000003040','-','')), @T_MINUS_2D,  1014, 'dr_chen', 'Default', 1, 1, '112','72',  135.0, 64.0, 98.4, 82,  16, 98.00, '',         'SEED'),
  (3041, UNHEX(REPLACE('af000000-0000-0000-0000-000000003041','-','')), @T_MINUS_1D,  1014, 'dr_chen', 'Default', 1, 1, '114','74',  135.4, 64.0, 98.4, 78,  16, 98.00, '',         'SEED'),
  (3042, UNHEX(REPLACE('af000000-0000-0000-0000-000000003042','-','')), @T_MINUS_2H,  1014, 'dr_chen', 'Default', 1, 1, '118','76',  135.6, 64.0, 98.4, 76,  16, 98.00, '',         'SEED'),
  -- Pt 1015 (chest pain r/o ACS) — subtle deterioration at 2h mark
  (3043, UNHEX(REPLACE('af000000-0000-0000-0000-000000003043','-','')), @T_MINUS_2D,  1015, 'dr_chen', 'Default', 1, 1, '138','86',  198.0, 70.0, 98.6, 78,  16, 98.00, 'Chest pain resolved','SEED'),
  (3044, UNHEX(REPLACE('af000000-0000-0000-0000-000000003044','-','')), @T_MINUS_18H, 1015, 'dr_chen', 'Default', 1, 1, '132','82',  197.5, 70.0, 98.4, 76,  16, 98.00, '',         'SEED'),
  (3045, UNHEX(REPLACE('af000000-0000-0000-0000-000000003045','-','')), @T_MINUS_2H,  1015, 'dr_chen', 'Default', 1, 1, '148','94',  197.4, 70.0, 99.2, 112, 22, 95.00, 'New chest pressure, diaphoretic','SEED');

INSERT INTO uuid_registry (uuid, table_name, table_id, table_vertical, couchdb, document_drive, mapped, created)
SELECT uuid, 'form_vitals', CAST(id AS CHAR), '', '', 0, 0, NOW() FROM form_vitals WHERE external_id='SEED';

-- Register the vitals form in the `forms` table so the encounter UI shows them.
INSERT INTO forms (date, encounter, form_name, form_id, pid, user, groupname, authorized, deleted, formdir, provider_id)
SELECT date, (pid + 1000), 'Vitals', id, pid, 'dr_chen', 'Default', 1, 0, 'vitals', 101
FROM form_vitals WHERE external_id='SEED';

-- -----------------------------------------------------------------------------
-- 5. Problems (lists.type='medical_problem') — one primary problem per pt
-- -----------------------------------------------------------------------------
INSERT INTO lists (id, uuid, date, type, subtype, title, begdate, activity, pid, user, groupname, diagnosis, outcome, external_id)
VALUES
  (4001, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004001','-','')), @T_MINUS_2D, 'medical_problem','', 'Congestive heart failure, acute on chronic', DATE_SUB(NOW(), INTERVAL 730 DAY), 1, 1001, 'dr_chen', 'Default', 'ICD10:I50.23', 0, 'SEED'),
  (4002, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004002','-','')), @T_MINUS_3D, 'medical_problem','', 'Community-acquired pneumonia',              @T_MINUS_3D, 1, 1002, 'dr_chen', 'Default', 'ICD10:J18.9',  0, 'SEED'),
  (4003, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004003','-','')), @T_MINUS_2D, 'medical_problem','', 'Type 2 diabetes with ketoacidosis',          DATE_SUB(NOW(), INTERVAL 3650 DAY), 1, 1003, 'dr_chen', 'Default', 'ICD10:E11.10', 0, 'SEED'),
  (4004, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004004','-','')), @T_MINUS_3D, 'medical_problem','', 'Sepsis due to urinary tract infection',      @T_MINUS_3D, 1, 1004, 'dr_chen', 'Default', 'ICD10:A41.51', 0, 'SEED'),
  (4005, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004005','-','')), @T_MINUS_2D, 'medical_problem','', 'Acute pancreatitis',                         @T_MINUS_2D, 1, 1005, 'dr_chen', 'Default', 'ICD10:K85.90', 0, 'SEED'),
  (4006, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004006','-','')), @T_MINUS_2D, 'medical_problem','', 'Cellulitis of left lower limb',              @T_MINUS_2D, 1, 1006, 'dr_chen', 'Default', 'ICD10:L03.116',0, 'SEED'),
  (4007, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004007','-','')), @T_MINUS_3D, 'medical_problem','', 'COPD with acute exacerbation',                DATE_SUB(NOW(), INTERVAL 1825 DAY), 1, 1007, 'dr_chen', 'Default', 'ICD10:J44.1',  0, 'SEED'),
  (4008, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004008','-','')), @T_MINUS_2D, 'medical_problem','', 'Acute kidney injury, unspecified',            @T_MINUS_2D, 1, 1008, 'dr_chen', 'Default', 'ICD10:N17.9',  0, 'SEED'),
  (4009, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004009','-','')), @T_MINUS_2D, 'medical_problem','', 'Upper GI bleed',                              @T_MINUS_2D, 1, 1009, 'dr_chen', 'Default', 'ICD10:K92.2',  0, 'SEED'),
  (4010, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004010','-','')), @T_MINUS_3D, 'medical_problem','', 'Acute pulmonary embolism',                    @T_MINUS_3D, 1, 1010, 'dr_chen', 'Default', 'ICD10:I26.99', 0, 'SEED'),
  (4011, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004011','-','')), @T_MINUS_2D, 'medical_problem','', 'Atrial fibrillation with rapid ventricular response', @T_MINUS_2D, 1, 1011, 'dr_chen', 'Default', 'ICD10:I48.91', 0, 'SEED'),
  (4012, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004012','-','')), @T_MINUS_3D, 'medical_problem','', 'Acute ischemic stroke',                       @T_MINUS_3D, 1, 1012, 'dr_chen', 'Default', 'ICD10:I63.9',  0, 'SEED'),
  (4013, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004013','-','')), @T_MINUS_2D, 'medical_problem','', 'Alcohol withdrawal syndrome',                 @T_MINUS_2D, 1, 1013, 'dr_chen', 'Default', 'ICD10:F10.239',0, 'SEED'),
  (4014, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004014','-','')), @T_MINUS_2D, 'medical_problem','', 'Hyponatremia',                                @T_MINUS_2D, 1, 1014, 'dr_chen', 'Default', 'ICD10:E87.1',  0, 'SEED'),
  (4015, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004015','-','')), @T_MINUS_2D, 'medical_problem','', 'Chest pain, unspecified',                     @T_MINUS_2D, 1, 1015, 'dr_chen', 'Default', 'ICD10:R07.9',  0, 'SEED');

-- -----------------------------------------------------------------------------
-- 6. Allergies (lists.type='allergy') — Pt 1006 has PCN allergy AND active
--    amoxicillin Rx below → intentional drug-allergy conflict.
-- -----------------------------------------------------------------------------
INSERT INTO lists (id, uuid, date, type, subtype, title, begdate, activity, pid, user, groupname, diagnosis, reaction, severity_al, verification, outcome, external_id)
VALUES
  (4101, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004101','-','')), @T_MINUS_2D, 'allergy','', 'Penicillin',            DATE_SUB(NOW(), INTERVAL 3650 DAY), 1, 1001, 'dr_chen', 'Default', '', 'Hives',       'moderate', 'confirmed', 0, 'SEED'),
  (4102, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004102','-','')), @T_MINUS_2D, 'allergy','', 'Sulfa drugs',           DATE_SUB(NOW(), INTERVAL 1825 DAY), 1, 1003, 'dr_chen', 'Default', '', 'Rash',        'mild',     'confirmed', 0, 'SEED'),
  (4103, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004103','-','')), @T_MINUS_2D, 'allergy','', 'Penicillin',            DATE_SUB(NOW(), INTERVAL 900 DAY),  1, 1006, 'dr_chen', 'Default', '', 'Anaphylaxis', 'severe',   'confirmed', 0, 'SEED'),
  (4104, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004104','-','')), @T_MINUS_2D, 'allergy','', 'Iodinated contrast',     DATE_SUB(NOW(), INTERVAL 2200 DAY), 1, 1010, 'dr_chen', 'Default', '', 'Hives',       'moderate', 'confirmed', 0, 'SEED'),
  (4105, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004105','-','')), @T_MINUS_2D, 'allergy','', 'NSAIDs',                 DATE_SUB(NOW(), INTERVAL 1400 DAY), 1, 1014, 'dr_chen', 'Default', '', 'GI bleed',    'severe',   'confirmed', 0, 'SEED'),
  (4106, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004106','-','')), @T_MINUS_2D, 'allergy','', 'No known drug allergies', DATE_SUB(NOW(), INTERVAL 30 DAY),  1, 1002, 'dr_chen', 'Default', '', '',            '',         'confirmed', 0, 'SEED');

-- -----------------------------------------------------------------------------
-- 7. Medications in lists (type='medication')
--    Every active-med intent lives here; a subset also lives in prescriptions.
--    Intentional divergences (below):
--      • Pt 1002: `lists` has metoprolol tartrate 25mg BID; `prescriptions`
--        has metoprolol succinate ER 50mg daily — different formulation.
--      • Pt 1007: `lists` has albuterol MDI PRN; `prescriptions` MISSING.
--      • Pt 1011: `prescriptions` has apixaban 5mg BID; `lists` MISSING (new
--        anticoagulant not yet reconciled onto the problem list).
--    These preserve the real reconciliation problem the verification layer
--    must handle at synthesis time.
-- -----------------------------------------------------------------------------
INSERT INTO lists (id, uuid, date, type, subtype, title, begdate, activity, pid, user, groupname, diagnosis, outcome, external_id)
VALUES
  -- Pt 1001 (CHF)
  (4201, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004201','-','')), @T_MINUS_2D, 'medication','', 'Furosemide 40 mg PO daily',              DATE_SUB(NOW(), INTERVAL 365 DAY), 1, 1001, 'dr_chen', 'Default', '', 0, 'SEED'),
  (4202, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004202','-','')), @T_MINUS_2D, 'medication','', 'Lisinopril 10 mg PO daily',              DATE_SUB(NOW(), INTERVAL 730 DAY), 1, 1001, 'dr_chen', 'Default', '', 0, 'SEED'),
  (4203, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004203','-','')), @T_MINUS_2D, 'medication','', 'Carvedilol 6.25 mg PO BID',              DATE_SUB(NOW(), INTERVAL 365 DAY), 1, 1001, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1002 (PNA) — DIVERGENCE: tartrate 25mg BID here vs succinate ER 50mg qd in prescriptions
  (4204, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004204','-','')), @T_MINUS_3D, 'medication','', 'Ceftriaxone 1 g IV daily',                @T_MINUS_3D, 1, 1002, 'dr_chen', 'Default', '', 0, 'SEED'),
  (4205, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004205','-','')), @T_MINUS_3D, 'medication','', 'Metoprolol tartrate 25 mg PO BID',         DATE_SUB(NOW(), INTERVAL 400 DAY), 1, 1002, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1003 (DKA)
  (4206, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004206','-','')), @T_MINUS_2D, 'medication','', 'Insulin drip 0.1 units/kg/hr',            @T_MINUS_2D, 1, 1003, 'dr_chen', 'Default', '', 0, 'SEED'),
  (4207, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004207','-','')), @T_MINUS_2D, 'medication','', 'Metformin 1000 mg PO BID (held)',          DATE_SUB(NOW(), INTERVAL 1200 DAY), 1, 1003, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1004 (UTI/sepsis)
  (4208, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004208','-','')), @T_MINUS_3D, 'medication','', 'Piperacillin-tazobactam 3.375 g IV q6h',   @T_MINUS_3D, 1, 1004, 'dr_chen', 'Default', '', 0, 'SEED'),
  (4209, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004209','-','')), @T_MINUS_3D, 'medication','', 'Norepinephrine drip (as needed)',           @T_MINUS_3D, 1, 1004, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1005 (pancreatitis)
  (4210, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004210','-','')), @T_MINUS_2D, 'medication','', 'Hydromorphone 0.5 mg IV q4h PRN pain',      @T_MINUS_2D, 1, 1005, 'dr_chen', 'Default', '', 0, 'SEED'),
  (4211, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004211','-','')), @T_MINUS_2D, 'medication','', 'Ondansetron 4 mg IV q6h PRN nausea',        @T_MINUS_2D, 1, 1005, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1006 (cellulitis) — DRUG-ALLERGY CONFLICT: PCN allergy + amoxicillin
  (4212, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004212','-','')), @T_MINUS_2D, 'medication','', 'Amoxicillin-clavulanate 875 mg PO BID',    @T_MINUS_2D, 1, 1006, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1007 (COPD) — DIVERGENCE: albuterol MDI here, MISSING from prescriptions
  (4213, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004213','-','')), @T_MINUS_3D, 'medication','', 'Albuterol MDI 2 puffs q4h PRN',             DATE_SUB(NOW(), INTERVAL 800 DAY), 1, 1007, 'dr_chen', 'Default', '', 0, 'SEED'),
  (4214, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004214','-','')), @T_MINUS_3D, 'medication','', 'Prednisone 40 mg PO daily x5 days',          @T_MINUS_3D, 1, 1007, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1008 (AKI)
  (4215, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004215','-','')), @T_MINUS_2D, 'medication','', 'Lisinopril 20 mg PO daily (held for AKI)',   DATE_SUB(NOW(), INTERVAL 800 DAY), 1, 1008, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1009 (GIB)
  (4216, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004216','-','')), @T_MINUS_2D, 'medication','', 'Pantoprazole 40 mg IV BID',                  @T_MINUS_2D, 1, 1009, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1010 (PE)
  (4217, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004217','-','')), @T_MINUS_3D, 'medication','', 'Heparin drip (aPTT-titrated)',                @T_MINUS_3D, 1, 1010, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1011 (AFib/RVR) — NOTE: apixaban is in prescriptions but MISSING here
  (4218, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004218','-','')), @T_MINUS_2D, 'medication','', 'Diltiazem drip (rate control)',               @T_MINUS_2D, 1, 1011, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1012 (CVA)
  (4219, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004219','-','')), @T_MINUS_3D, 'medication','', 'Aspirin 81 mg PO daily',                       @T_MINUS_3D, 1, 1012, 'dr_chen', 'Default', '', 0, 'SEED'),
  (4220, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004220','-','')), @T_MINUS_3D, 'medication','', 'Atorvastatin 80 mg PO daily',                  @T_MINUS_3D, 1, 1012, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1013 (ETOH withdrawal)
  (4221, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004221','-','')), @T_MINUS_2D, 'medication','', 'Lorazepam 2 mg IV q4h PRN CIWA >=8',           @T_MINUS_2D, 1, 1013, 'dr_chen', 'Default', '', 0, 'SEED'),
  (4222, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004222','-','')), @T_MINUS_2D, 'medication','', 'Thiamine 500 mg IV daily',                     @T_MINUS_2D, 1, 1013, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1014 (hyponatremia)
  (4223, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004223','-','')), @T_MINUS_2D, 'medication','', '3% saline 30 mL/hr (targeted Na correction)',  @T_MINUS_2D, 1, 1014, 'dr_chen', 'Default', '', 0, 'SEED'),
  -- Pt 1015 (r/o ACS)
  (4224, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004224','-','')), @T_MINUS_2D, 'medication','', 'Aspirin 325 mg PO x1, then 81 mg daily',       @T_MINUS_2D, 1, 1015, 'dr_chen', 'Default', '', 0, 'SEED'),
  (4225, UNHEX(REPLACE('bb000000-0000-0000-0000-000000004225','-','')), @T_MINUS_2D, 'medication','', 'Atorvastatin 40 mg PO daily',                  @T_MINUS_2D, 1, 1015, 'dr_chen', 'Default', '', 0, 'SEED');

INSERT INTO uuid_registry (uuid, table_name, table_id, table_vertical, couchdb, document_drive, mapped, created)
SELECT uuid, 'lists', CAST(id AS CHAR), '', '', 0, 0, NOW() FROM lists WHERE external_id='SEED';

-- -----------------------------------------------------------------------------
-- 8. Prescriptions (parallel store; must disagree with lists in specific ways).
-- -----------------------------------------------------------------------------
INSERT INTO prescriptions
  (id, uuid, patient_id, date_added, provider_id, encounter, start_date, drug,
   rxnorm_drugcode, dosage, quantity, route, active, txDate, external_id, note)
VALUES
  (5001, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005001','-','')), 1001, @T_MINUS_2D, 101, 2001, DATE(@T_MINUS_2D), 'Furosemide',                    '4603',  '40 mg',   '30', 'PO',  1, DATE(@T_MINUS_2D), 'SEED', 'Home med, continued inpatient'),
  (5002, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005002','-','')), 1001, @T_MINUS_2D, 101, 2001, DATE(@T_MINUS_2D), 'Lisinopril',                    '29046', '10 mg',   '30', 'PO',  1, DATE(@T_MINUS_2D), 'SEED', 'Home med'),
  (5003, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005003','-','')), 1001, @T_MINUS_2D, 101, 2001, DATE(@T_MINUS_2D), 'Carvedilol',                    '20352', '6.25 mg', '60', 'PO',  1, DATE(@T_MINUS_2D), 'SEED', 'Home med'),
  -- Pt 1002 DIVERGENCE: succinate ER 50mg qd here, but tartrate 25mg BID in lists
  (5004, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005004','-','')), 1002, @T_MINUS_3D, 101, 2002, DATE(@T_MINUS_3D), 'Ceftriaxone',                   '2193',  '1 g IV',  '5',  'IV',  1, DATE(@T_MINUS_3D), 'SEED', 'CAP'),
  (5005, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005005','-','')), 1002, @T_MINUS_3D, 101, 2002, DATE(@T_MINUS_3D), 'Metoprolol Succinate ER',       '866427','50 mg',   '30', 'PO',  1, DATE(@T_MINUS_3D), 'SEED', '*** Divergence: lists shows tartrate 25mg BID ***'),
  (5006, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005006','-','')), 1003, @T_MINUS_2D, 101, 2003, DATE(@T_MINUS_2D), 'Insulin regular (drip)',        '5856',  '0.1 U/kg/hr','1','IV',1, DATE(@T_MINUS_2D), 'SEED', 'DKA protocol'),
  (5007, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005007','-','')), 1004, @T_MINUS_3D, 101, 2004, DATE(@T_MINUS_3D), 'Piperacillin-tazobactam',       '203134','3.375 g q6h','1','IV',1, DATE(@T_MINUS_3D), 'SEED', 'Sepsis'),
  (5008, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005008','-','')), 1005, @T_MINUS_2D, 101, 2005, DATE(@T_MINUS_2D), 'Hydromorphone',                 '3423',  '0.5 mg',  '10', 'IV',  1, DATE(@T_MINUS_2D), 'SEED', 'PRN pain'),
  (5009, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005009','-','')), 1005, @T_MINUS_2D, 101, 2005, DATE(@T_MINUS_2D), 'Ondansetron',                   '26225', '4 mg',    '10', 'IV',  1, DATE(@T_MINUS_2D), 'SEED', 'PRN nausea'),
  -- Pt 1006: amoxicillin-clav (matches lists — the intentional drug-allergy conflict is that this patient is PCN-allergic)
  (5010, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005010','-','')), 1006, @T_MINUS_2D, 101, 2006, DATE(@T_MINUS_2D), 'Amoxicillin-clavulanate',       '19711', '875 mg',  '20', 'PO',  1, DATE(@T_MINUS_2D), 'SEED', '*** Conflict with PCN allergy on chart ***'),
  -- Pt 1007: prednisone only; albuterol MDI is in lists but MISSING here on purpose
  (5011, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005011','-','')), 1007, @T_MINUS_3D, 101, 2007, DATE(@T_MINUS_3D), 'Prednisone',                    '8640',  '40 mg',   '5',  'PO',  1, DATE(@T_MINUS_3D), 'SEED', 'COPD burst x5d'),
  (5012, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005012','-','')), 1009, @T_MINUS_2D, 101, 2009, DATE(@T_MINUS_2D), 'Pantoprazole',                  '40790', '40 mg',   '10', 'IV',  1, DATE(@T_MINUS_2D), 'SEED', 'GIB'),
  (5013, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005013','-','')), 1010, @T_MINUS_3D, 101, 2010, DATE(@T_MINUS_3D), 'Heparin',                       '5224',  'aPTT titrate','1','IV',1, DATE(@T_MINUS_3D), 'SEED', 'PE'),
  -- Pt 1011: apixaban here, MISSING from lists on purpose
  (5014, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005014','-','')), 1011, @T_MINUS_2D, 101, 2011, DATE(@T_MINUS_2D), 'Apixaban',                      '1364430','5 mg',   '60', 'PO',  1, DATE(@T_MINUS_2D), 'SEED', '*** Divergence: not on problem-list med reconciliation ***'),
  (5015, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005015','-','')), 1011, @T_MINUS_2D, 101, 2011, DATE(@T_MINUS_2D), 'Diltiazem',                     '3443',  'titrate', '1',  'IV',  1, DATE(@T_MINUS_2D), 'SEED', 'AFib RVR'),
  (5016, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005016','-','')), 1012, @T_MINUS_3D, 101, 2012, DATE(@T_MINUS_3D), 'Aspirin',                       '1191',  '81 mg',   '30', 'PO',  1, DATE(@T_MINUS_3D), 'SEED', 'Stroke secondary prevention'),
  (5017, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005017','-','')), 1012, @T_MINUS_3D, 101, 2012, DATE(@T_MINUS_3D), 'Atorvastatin',                  '83367', '80 mg',   '30', 'PO',  1, DATE(@T_MINUS_3D), 'SEED', 'High-intensity statin'),
  (5018, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005018','-','')), 1013, @T_MINUS_2D, 101, 2013, DATE(@T_MINUS_2D), 'Lorazepam',                     '6470',  '2 mg',    '10', 'IV',  1, DATE(@T_MINUS_2D), 'SEED', 'CIWA >=8'),
  (5019, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005019','-','')), 1013, @T_MINUS_2D, 101, 2013, DATE(@T_MINUS_2D), 'Thiamine',                      '10403', '500 mg',  '3',  'IV',  1, DATE(@T_MINUS_2D), 'SEED', 'Wernicke ppx'),
  (5020, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005020','-','')), 1015, @T_MINUS_2D, 101, 2015, DATE(@T_MINUS_2D), 'Aspirin',                       '1191',  '325 mg->81','30','PO',  1, DATE(@T_MINUS_2D), 'SEED', 'r/o ACS'),
  (5021, UNHEX(REPLACE('cc000000-0000-0000-0000-000000005021','-','')), 1015, @T_MINUS_2D, 101, 2015, DATE(@T_MINUS_2D), 'Atorvastatin',                  '83367', '40 mg',   '30', 'PO',  1, DATE(@T_MINUS_2D), 'SEED', 'r/o ACS');

INSERT INTO uuid_registry (uuid, table_name, table_id, table_vertical, couchdb, document_drive, mapped, created)
SELECT uuid, 'prescriptions', CAST(id AS CHAR), '', '', 0, 0, NOW() FROM prescriptions WHERE external_id='SEED';

-- -----------------------------------------------------------------------------
-- 9. Procedure catalog (lab types) — code prefix 'SEED-' so cleanup is safe.
--    Note: OpenEMR treats units/range on procedure_result as the source of
--    truth per-result; procedure_type ranges are catalog defaults.
-- -----------------------------------------------------------------------------
INSERT INTO procedure_type
  (procedure_type_id, name, procedure_code, procedure_type, description, standard_code, units, `range`, activity, procedure_type_name)
VALUES
  (6001, 'CBC panel',           'SEED-CBC',   'ord', 'Complete blood count',              'LOINC:58410-2', '',       '',                       1, 'CBC'),
  (6002, 'BMP',                 'SEED-BMP',   'ord', 'Basic metabolic panel',             'LOINC:24320-4', '',       '',                       1, 'BMP'),
  (6003, 'Troponin I',          'SEED-TROP',  'ord', 'Troponin I, high-sensitivity',      'LOINC:6598-7',  'ng/mL',  '<0.04',                  1, 'Troponin'),
  (6004, 'Lactate',             'SEED-LACT',  'ord', 'Venous lactate',                    'LOINC:32693-4', 'mmol/L', '0.5-2.2',                1, 'Lactate'),
  (6005, 'Blood culture',       'SEED-BCX',   'ord', 'Aerobic + anaerobic blood culture', 'LOINC:600-7',   '',       'No growth',              1, 'Blood culture');

-- -----------------------------------------------------------------------------
-- 10. Lab orders + reports + results.
--     Each patient gets a same-morning CBC + BMP. High-acuity pts get extra.
--     Ranges + abnormal flags populated on every numeric result.
--     Abnormal flag values: '' = normal, 'high', 'low', 'critical_high',
--     'critical_low' (OpenEMR-style).
-- -----------------------------------------------------------------------------
-- Reusable macro-ish pattern implemented as one big insert set.
-- Order IDs 7xxx, Report IDs 8xxx, Result IDs 9xxxxx.
INSERT INTO procedure_order
  (procedure_order_id, uuid, provider_id, patient_id, encounter_id, date_collected, date_ordered,
   order_status, activity, procedure_order_type, order_intent, external_id, order_diagnosis, clinical_hx)
VALUES
  -- Pt 1001 CBC + BMP
  (7001, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007001','-','')), 101, 1001, 2001, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','CHF','AM labs'),
  (7002, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007002','-','')), 101, 1001, 2001, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','CHF','AM labs'),
  -- Pt 1002 CBC + BMP + blood cx
  (7003, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007003','-','')), 101, 1002, 2002, @T_MINUS_2D, @T_MINUS_2D, 'complete', 1, 'laboratory_test','order','SEED','CAP','Sepsis workup'),
  (7004, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007004','-','')), 101, 1002, 2002, @T_MINUS_2D, @T_MINUS_2D, 'complete', 1, 'laboratory_test','order','SEED','CAP',''),
  (7005, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007005','-','')), 101, 1002, 2002, @T_MINUS_2D, @T_MINUS_2D, 'preliminary',1,'laboratory_test','order','SEED','CAP',''),
  -- Pt 1003 BMP (DKA-focused)
  (7006, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007006','-','')), 101, 1003, 2003, @T_MINUS_2H, @T_MINUS_2H, 'complete', 1, 'laboratory_test','order','SEED','DKA','q4h glucose/K'),
  -- Pt 1004 CBC + BMP + lactate (sepsis)
  (7007, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007007','-','')), 101, 1004, 2004, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','sepsis',''),
  (7008, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007008','-','')), 101, 1004, 2004, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','sepsis',''),
  (7009, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007009','-','')), 101, 1004, 2004, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','sepsis','MAP goal >=65'),
  -- Pt 1005 BMP + lipase (using CBC row)
  (7010, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007010','-','')), 101, 1005, 2005, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','pancreatitis',''),
  -- Pt 1006 CBC
  (7011, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007011','-','')), 101, 1006, 2006, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','cellulitis',''),
  -- Pt 1007 BMP
  (7012, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007012','-','')), 101, 1007, 2007, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','COPD',''),
  -- Pt 1008 BMP (AKI trend)
  (7013, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007013','-','')), 101, 1008, 2008, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','AKI',''),
  -- Pt 1009 CBC (H/H trend for GIB)
  (7014, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007014','-','')), 101, 1009, 2009, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','GIB',''),
  -- Pt 1010 CBC + BMP
  (7015, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007015','-','')), 101, 1010, 2010, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','PE',''),
  -- Pt 1011 BMP
  (7016, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007016','-','')), 101, 1011, 2011, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','AFib',''),
  -- Pt 1012 BMP
  (7017, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007017','-','')), 101, 1012, 2012, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','CVA',''),
  -- Pt 1013 BMP
  (7018, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007018','-','')), 101, 1013, 2013, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','ETOH withdrawal',''),
  -- Pt 1014 BMP (Na trend)
  (7019, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007019','-','')), 101, 1014, 2014, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','hyponatremia','q6h Na'),
  -- Pt 1015 troponin (baseline t=0)  AND  the "overnight change" trop @ -2h
  (7020, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007020','-','')), 101, 1015, 2015, @T_MINUS_1D, @T_MINUS_1D, 'complete', 1, 'laboratory_test','order','SEED','r/o ACS','baseline'),
  (7021, UNHEX(REPLACE('dd000000-0000-0000-0000-000000007021','-','')), 101, 1015, 2015, @T_MINUS_2H, @T_MINUS_2H, 'complete', 1, 'laboratory_test','order','SEED','r/o ACS','*** overnight recheck: chest pressure recurred ***');

INSERT INTO procedure_report
  (procedure_report_id, uuid, procedure_order_id, procedure_order_seq, date_collected, date_report, source, report_status, review_status)
VALUES
  (8001, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008001','-','')), 7001, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8002, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008002','-','')), 7002, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8003, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008003','-','')), 7003, 1, @T_MINUS_2D, @T_MINUS_2D, 0, 'complete','reviewed'),
  (8004, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008004','-','')), 7004, 1, @T_MINUS_2D, @T_MINUS_2D, 0, 'complete','reviewed'),
  (8005, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008005','-','')), 7005, 1, @T_MINUS_2D, @T_MINUS_2D, 0, 'preliminary','received'),
  (8006, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008006','-','')), 7006, 1, @T_MINUS_2H, @T_MINUS_2H, 0, 'complete','reviewed'),
  (8007, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008007','-','')), 7007, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8008, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008008','-','')), 7008, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8009, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008009','-','')), 7009, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8010, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008010','-','')), 7010, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8011, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008011','-','')), 7011, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8012, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008012','-','')), 7012, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8013, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008013','-','')), 7013, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8014, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008014','-','')), 7014, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8015, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008015','-','')), 7015, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8016, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008016','-','')), 7016, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8017, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008017','-','')), 7017, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8018, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008018','-','')), 7018, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8019, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008019','-','')), 7019, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8020, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008020','-','')), 7020, 1, @T_MINUS_1D, @T_MINUS_1D, 0, 'complete','reviewed'),
  (8021, UNHEX(REPLACE('ee000000-0000-0000-0000-000000008021','-','')), 7021, 1, @T_MINUS_2H, @T_MINUS_2H, 0, 'complete','received'); -- overnight change, not yet reviewed

-- Results per report.  result_data_type='N' numeric, 'S' string.
INSERT INTO procedure_result
  (procedure_result_id, uuid, procedure_report_id, result_data_type, result_code, result_text,
   date, units, result, `range`, abnormal, result_status, comments)
VALUES
  -- 8001 = pt1001 CBC
  (90001, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090001','-','')), 8001, 'N', 'LOINC:6690-2', 'WBC',        @T_MINUS_1D, 'K/uL',  '8.4',  '4.5-11.0',   '',              'final', ''),
  (90002, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090002','-','')), 8001, 'N', 'LOINC:718-7',  'Hemoglobin', @T_MINUS_1D, 'g/dL',  '11.9', '13.5-17.5',  'low',           'final', ''),
  (90003, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090003','-','')), 8001, 'N', 'LOINC:777-3',  'Platelets',  @T_MINUS_1D, 'K/uL',  '212',  '150-400',    '',              'final', ''),
  -- 8002 = pt1001 BMP
  (90004, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090004','-','')), 8002, 'N', 'LOINC:2951-2', 'Sodium',     @T_MINUS_1D, 'mEq/L', '138',  '135-145',    '',              'final', ''),
  (90005, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090005','-','')), 8002, 'N', 'LOINC:2823-3', 'Potassium',  @T_MINUS_1D, 'mEq/L', '3.7',  '3.5-5.0',    '',              'final', ''),
  (90006, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090006','-','')), 8002, 'N', 'LOINC:2160-0', 'Creatinine', @T_MINUS_1D, 'mg/dL', '1.4',  '0.6-1.3',    'high',          'final', ''),
  (90007, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090007','-','')), 8002, 'N', 'LOINC:2345-7', 'Glucose',    @T_MINUS_1D, 'mg/dL', '108',  '70-99',      'high',          'final', ''),
  -- 8003 = pt1002 CBC (febrile PNA — high WBC)
  (90008, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090008','-','')), 8003, 'N', 'LOINC:6690-2', 'WBC',        @T_MINUS_2D, 'K/uL',  '15.2', '4.5-11.0',   'high',          'final', 'Left-shifted'),
  (90009, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090009','-','')), 8003, 'N', 'LOINC:718-7',  'Hemoglobin', @T_MINUS_2D, 'g/dL',  '12.6', '12.0-16.0',  '',              'final', ''),
  (90010, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090010','-','')), 8003, 'N', 'LOINC:777-3',  'Platelets',  @T_MINUS_2D, 'K/uL',  '244',  '150-400',    '',              'final', ''),
  -- 8004 = pt1002 BMP
  (90011, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090011','-','')), 8004, 'N', 'LOINC:2951-2', 'Sodium',     @T_MINUS_2D, 'mEq/L', '134',  '135-145',    'low',           'final', ''),
  (90012, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090012','-','')), 8004, 'N', 'LOINC:2823-3', 'Potassium',  @T_MINUS_2D, 'mEq/L', '3.4',  '3.5-5.0',    'low',           'final', ''),
  (90013, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090013','-','')), 8004, 'N', 'LOINC:2160-0', 'Creatinine', @T_MINUS_2D, 'mg/dL', '0.9',  '0.6-1.3',    '',              'final', ''),
  (90014, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090014','-','')), 8004, 'N', 'LOINC:2345-7', 'Glucose',    @T_MINUS_2D, 'mg/dL', '132',  '70-99',      'high',          'final', ''),
  -- 8005 = pt1002 blood cx (preliminary)
  (90015, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090015','-','')), 8005, 'S', 'LOINC:600-7',  'Blood culture aerobic', @T_MINUS_2D, '', 'No growth to date', 'No growth', '', 'preliminary','48hr pending'),
  -- 8006 = pt1003 BMP (DKA — critical K high, high glucose, low bicarb via note)
  (90016, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090016','-','')), 8006, 'N', 'LOINC:2951-2', 'Sodium',     @T_MINUS_2H, 'mEq/L', '128',  '135-145',    'low',           'final', ''),
  (90017, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090017','-','')), 8006, 'N', 'LOINC:2823-3', 'Potassium',  @T_MINUS_2H, 'mEq/L', '5.7',  '3.5-5.0',    'critical_high', 'final', 'On insulin drip'),
  (90018, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090018','-','')), 8006, 'N', 'LOINC:2345-7', 'Glucose',    @T_MINUS_2H, 'mg/dL', '386',  '70-99',      'critical_high', 'final', 'DKA'),
  (90019, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090019','-','')), 8006, 'N', 'LOINC:1963-8', 'Bicarbonate',@T_MINUS_2H, 'mEq/L', '12',   '22-29',      'critical_low',  'final', 'AG met acidosis'),
  -- 8007 = pt1004 CBC (sepsis)
  (90020, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090020','-','')), 8007, 'N', 'LOINC:6690-2', 'WBC',        @T_MINUS_1D, 'K/uL',  '18.6', '4.5-11.0',   'high',          'final', 'With bands'),
  (90021, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090021','-','')), 8007, 'N', 'LOINC:718-7',  'Hemoglobin', @T_MINUS_1D, 'g/dL',  '10.4', '12.0-16.0',  'low',           'final', ''),
  (90022, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090022','-','')), 8007, 'N', 'LOINC:777-3',  'Platelets',  @T_MINUS_1D, 'K/uL',  '106',  '150-400',    'low',           'final', ''),
  -- 8008 = pt1004 BMP (sepsis, AKI)
  (90023, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090023','-','')), 8008, 'N', 'LOINC:2160-0', 'Creatinine', @T_MINUS_1D, 'mg/dL', '2.1',  '0.6-1.3',    'high',          'final', ''),
  (90024, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090024','-','')), 8008, 'N', 'LOINC:2951-2', 'Sodium',     @T_MINUS_1D, 'mEq/L', '141',  '135-145',    '',              'final', ''),
  -- 8009 = pt1004 lactate (elevated for sepsis)
  (90025, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090025','-','')), 8009, 'N', 'LOINC:32693-4','Lactate',    @T_MINUS_1D, 'mmol/L','4.2',  '0.5-2.2',    'critical_high', 'final', '>4 = severe sepsis marker'),
  -- 8010 = pt1005 BMP
  (90026, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090026','-','')), 8010, 'N', 'LOINC:2160-0', 'Creatinine', @T_MINUS_1D, 'mg/dL', '1.0',  '0.6-1.3',    '',              'final', ''),
  (90027, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090027','-','')), 8010, 'N', 'LOINC:3040-3', 'Lipase',     @T_MINUS_1D, 'U/L',   '842',  '10-190',     'critical_high', 'final', ''),
  -- 8011 = pt1006 CBC
  (90028, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090028','-','')), 8011, 'N', 'LOINC:6690-2', 'WBC',        @T_MINUS_1D, 'K/uL',  '12.4', '4.5-11.0',   'high',          'final', ''),
  (90029, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090029','-','')), 8011, 'N', 'LOINC:718-7',  'Hemoglobin', @T_MINUS_1D, 'g/dL',  '13.1', '12.0-16.0',  '',              'final', ''),
  -- 8012 = pt1007 BMP (COPD)
  (90030, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090030','-','')), 8012, 'N', 'LOINC:2160-0', 'Creatinine', @T_MINUS_1D, 'mg/dL', '0.9',  '0.6-1.3',    '',              'final', ''),
  (90031, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090031','-','')), 8012, 'N', 'LOINC:1963-8', 'Bicarbonate',@T_MINUS_1D, 'mEq/L', '31',   '22-29',      'high',          'final', 'Chronic CO2 retainer'),
  -- 8013 = pt1008 BMP (AKI resolving)
  (90032, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090032','-','')), 8013, 'N', 'LOINC:2160-0', 'Creatinine', @T_MINUS_1D, 'mg/dL', '2.4',  '0.6-1.3',    'high',          'final', 'Down from 3.1 on admission'),
  -- 8014 = pt1009 CBC (GIB — low Hgb after 1u pRBC)
  (90033, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090033','-','')), 8014, 'N', 'LOINC:718-7',  'Hemoglobin', @T_MINUS_1D, 'g/dL',  '7.8',  '13.5-17.5',  'low',           'final', 'Post 1u pRBC'),
  (90034, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090034','-','')), 8014, 'N', 'LOINC:777-3',  'Platelets',  @T_MINUS_1D, 'K/uL',  '164',  '150-400',    '',              'final', ''),
  -- 8015 = pt1010 CBC + BMP (PE, on heparin)
  (90035, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090035','-','')), 8015, 'N', 'LOINC:718-7',  'Hemoglobin', @T_MINUS_1D, 'g/dL',  '12.2', '12.0-16.0',  '',              'final', ''),
  (90036, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090036','-','')), 8015, 'N', 'LOINC:14979-9','aPTT',       @T_MINUS_1D, 's',     '62',   '25-35',      'high',          'final', 'Therapeutic on heparin'),
  -- 8016 = pt1011 BMP
  (90037, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090037','-','')), 8016, 'N', 'LOINC:2823-3', 'Potassium',  @T_MINUS_1D, 'mEq/L', '4.2',  '3.5-5.0',    '',              'final', ''),
  (90038, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090038','-','')), 8016, 'N', 'LOINC:2160-0', 'Creatinine', @T_MINUS_1D, 'mg/dL', '1.1',  '0.6-1.3',    '',              'final', ''),
  -- 8017 = pt1012 BMP
  (90039, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090039','-','')), 8017, 'N', 'LOINC:2951-2', 'Sodium',     @T_MINUS_1D, 'mEq/L', '140',  '135-145',    '',              'final', ''),
  (90040, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090040','-','')), 8017, 'N', 'LOINC:2345-7', 'Glucose',    @T_MINUS_1D, 'mg/dL', '146',  '70-99',      'high',          'final', ''),
  -- 8018 = pt1013 BMP
  (90041, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090041','-','')), 8018, 'N', 'LOINC:2823-3', 'Potassium',  @T_MINUS_1D, 'mEq/L', '3.3',  '3.5-5.0',    'low',           'final', 'Replete'),
  (90042, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090042','-','')), 8018, 'N', 'LOINC:2777-1', 'Magnesium',  @T_MINUS_1D, 'mg/dL', '1.5',  '1.7-2.2',    'low',           'final', 'Replete'),
  -- 8019 = pt1014 BMP (Na trend correction)
  (90043, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090043','-','')), 8019, 'N', 'LOINC:2951-2', 'Sodium',     @T_MINUS_1D, 'mEq/L', '124',  '135-145',    'critical_low',  'final', 'Down from 118 on admit'),
  -- 8020 = pt1015 baseline troponin (yesterday, normal)
  (90044, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090044','-','')), 8020, 'N', 'LOINC:6598-7', 'Troponin I', @T_MINUS_1D, 'ng/mL', '0.02', '<0.04',      '',              'final', 'Baseline negative'),
  -- 8021 = pt1015 OVERNIGHT CHANGE (last 2h) — critical trop
  (90045, UNHEX(REPLACE('fc000000-0000-0000-0000-000000090045','-','')), 8021, 'N', 'LOINC:6598-7', 'Troponin I', @T_MINUS_2H, 'ng/mL', '2.34', '<0.04',      'critical_high', 'final', '*** OVERNIGHT: rise from 0.02 -> 2.34 with recurrent chest pressure ***');

INSERT INTO uuid_registry (uuid, table_name, table_id, table_vertical, couchdb, document_drive, mapped, created)
SELECT uuid, 'procedure_order',  CAST(procedure_order_id AS CHAR),  '', '', 0, 0, NOW() FROM procedure_order  WHERE external_id='SEED';
INSERT INTO uuid_registry (uuid, table_name, table_id, table_vertical, couchdb, document_drive, mapped, created)
SELECT uuid, 'procedure_report', CAST(procedure_report_id AS CHAR), '', '', 0, 0, NOW() FROM procedure_report WHERE procedure_order_id IN (SELECT procedure_order_id FROM procedure_order WHERE external_id='SEED');
INSERT INTO uuid_registry (uuid, table_name, table_id, table_vertical, couchdb, document_drive, mapped, created)
SELECT uuid, 'procedure_result', CAST(procedure_result_id AS CHAR), '', '', 0, 0, NOW() FROM procedure_result WHERE procedure_report_id IN (SELECT procedure_report_id FROM procedure_report WHERE procedure_order_id IN (SELECT procedure_order_id FROM procedure_order WHERE external_id='SEED'));

-- -----------------------------------------------------------------------------
-- 11. SOAP notes — one per encounter (admission H&P).
-- -----------------------------------------------------------------------------
INSERT INTO form_soap (id, date, pid, user, groupname, authorized, activity, subjective, objective, assessment, plan)
VALUES
  (10001, @T_MINUS_2D, 1001, 'dr_chen', 'Default', 1, 1, 'DOE, orthopnea, +2 LE edema x1wk. Missed diuretic doses.',            'JVP 12cm, bibasilar crackles, S3 gallop, 2+ pitting edema. On 4L NC.',                'Acute-on-chronic HFrEF exacerbation.',                                                 'Diurese w/ IV furosemide; strict I/Os; daily weights; continue GDMT.'),
  (10002, @T_MINUS_3D, 1002, 'dr_chen', 'Default', 1, 1, 'Cough, fever, R-sided pleuritic chest pain 3d.',                       'T 101.8, RR 24, SpO2 89%RA. R lower lobe crackles.',                                     'CAP.',                                                                                 'Ceftriaxone + azithromycin per guidelines. Blood cx pending.'),
  (10003, @T_MINUS_2D, 1003, 'dr_chen', 'Default', 1, 1, 'Ran out of insulin 4 days ago. Polyuria, N/V, abd pain.',              'Kussmaul respirations. Dry mucous membranes. Glucose 486, HCO3 10, AG 24.',              'DKA.',                                                                                 'IVF resuscitation; insulin gtt per DKA protocol; q4h electrolytes; K repletion.'),
  (10004, @T_MINUS_3D, 1004, 'dr_chen', 'Default', 1, 1, 'Dysuria, confusion. Hypotensive on arrival.',                          'MAP 60 initially; +suprapubic tenderness.',                                              'Sepsis 2/2 UTI (urosepsis).',                                                          'Broad-spectrum abx (pip-tazo); 30 mL/kg IVF; norepinephrine PRN MAP>=65; ID consult.'),
  (10005, @T_MINUS_2D, 1005, 'dr_chen', 'Default', 1, 1, 'Epigastric pain radiating to back, worse w/ meals x2d. + heavy ETOH.', 'Epigastric tenderness. Lipase 842.',                                                     'Acute pancreatitis (likely ETOH).',                                                    'NPO; aggressive IVF; pain/nausea control; monitor for SIRS/necrosis.'),
  (10006, @T_MINUS_2D, 1006, 'dr_chen', 'Default', 1, 1, 'Warm red LLE x3d, systemic sx.',                                        'LLE erythema/warmth/tenderness to mid-shin. Afebrile.',                                  'Uncomplicated cellulitis, LLE. *NOTE*: chart lists severe PCN allergy.',              'Cephalexin. **Amox-clav in the record is a med-reconciliation risk to flag.**'),
  (10007, @T_MINUS_3D, 1007, 'dr_chen', 'Default', 1, 1, 'Increased SOB, sputum 2 days. Baseline home 2L O2.',                    'Wheezing. SpO2 87% on 2L. Started BiPAP briefly.',                                        'COPD exacerbation.',                                                                   'Duonebs; prednisone 40mg x5d; azithromycin; wean O2 to baseline.'),
  (10008, @T_MINUS_2D, 1008, 'dr_chen', 'Default', 1, 1, 'Fatigue, decreased UOP after starting new NSAID.',                       'Cr 3.1 on admit, 2.4 today. FeUrea suggests pre-renal + NSAID insult.',                    'Community-acquired AKI (pre-renal + medication).',                                     'D/c NSAID; hold ACEi; IVF challenge; renally dose meds; nephro follow-up.'),
  (10009, @T_MINUS_2D, 1009, 'dr_chen', 'Default', 1, 1, 'Melena x2 days, near-syncope.',                                          'HD initially borderline. Hgb 7.8 post 1u pRBC.',                                          'Upper GIB, likely peptic.',                                                            'IV PPI drip -> BID; type & cross; GI consult for EGD.'),
  (10010, @T_MINUS_3D, 1010, 'dr_chen', 'Default', 1, 1, 'Acute pleuritic chest pain + dyspnea after long car ride.',              'RR 24, SpO2 90% on 3L. Wells 6. CTA: segmental PE R lower lobe.',                          'Submassive PE.',                                                                        'Heparin drip; monitor for RV strain; discuss transition to DOAC.'),
  (10011, @T_MINUS_2D, 1011, 'dr_chen', 'Default', 1, 1, 'Palpitations x1d.',                                                       'IRR HR 140s. EKG: AF w/ RVR.',                                                             'AFib w/ RVR (new-onset).',                                                              'Diltiazem drip -> PO; **apixaban 5mg BID (verify on med rec — currently only in prescriptions)**.'),
  (10012, @T_MINUS_3D, 1012, 'dr_chen', 'Default', 1, 1, 'R-sided facial droop and R arm weakness 12h ago.',                         'NIHSS 8. CT head no bleed; MRI: L MCA territory infarct.',                                'Acute ischemic stroke, L MCA.',                                                        'Permissive HTN <220/120; ASA + statin; swallow eval; PT/OT/SLP; stroke workup.'),
  (10013, @T_MINUS_2D, 1013, 'dr_chen', 'Default', 1, 1, 'Last drink 2 days ago. Tremors, anxiety, insomnia.',                       'CIWA 22. Diaphoretic.',                                                                    'Alcohol withdrawal syndrome.',                                                          'CIWA q2h; lorazepam PRN CIWA>=8; thiamine; folate; MVI; monitor for DTs/seizure.'),
  (10014, @T_MINUS_2D, 1014, 'dr_chen', 'Default', 1, 1, 'Confusion, unsteady gait. Chronic HCTZ.',                                  'A&Ox2. Na 118 on admit -> 124 today. Euvolemic on exam.',                                'Symptomatic hyponatremia (likely SIADH-like on HCTZ).',                                'Hold HCTZ; 3% saline @ 30 mL/hr with goal <=8 mEq/L rise per 24h; q6h Na.'),
  (10015, @T_MINUS_2D, 1015, 'dr_chen', 'Default', 1, 1, 'Substernal chest pressure while walking; resolved. HEART score 4.',        'Baseline trop 0.02. Serial trop planned q6h. EKG: no acute changes.',                     'Chest pain, low-int risk r/o ACS.',                                                    'ASA/statin; serial trop; stress test in AM. **Flag: doctor has not yet rounded on this patient today.**');

-- Register SOAP forms in the forms table so they show in the encounter UI
INSERT INTO forms (date, encounter, form_name, form_id, pid, user, groupname, authorized, deleted, formdir, provider_id)
SELECT date, 2000 + (pid - 1000), 'SOAP', id, pid, 'dr_chen', 'Default', 1, 0, 'soap', 101
FROM form_soap WHERE pid BETWEEN 1001 AND 1015;

-- -----------------------------------------------------------------------------
-- 12. Overnight-change documented event: pnotes entry on Pt 1015 within 2h.
-- -----------------------------------------------------------------------------
INSERT INTO pnotes (date, body, pid, user, groupname, activity, authorized, title, assigned_to, deleted, message_status)
VALUES
  (@T_MINUS_2H,
   'RN NOTE @ 04:12 — pt reports recurrent substernal chest pressure, 6/10, radiating to L arm. Diaphoretic. BP 148/94, HR 112, SpO2 95% on RA. STAT EKG obtained, troponin drawn. MD notified.',
   1015, 'dr_chen', 'Default', 1, 1, 'RN progress note — chest pain recurrence', 'dr_chen', 0, 'New');

-- -----------------------------------------------------------------------------
-- procedure_order_code: FHIR lab Observations only export when each order carries
-- an order-code row — OpenEMR's lab query joins report<->result THROUGH this table
-- on (procedure_order_id, procedure_order_seq). One row per SEED order (seq 1),
-- with a representative code/name derived from its results.
-- -----------------------------------------------------------------------------
INSERT INTO procedure_order_code
  (procedure_order_id, procedure_order_seq, procedure_code, procedure_name, procedure_source, do_not_send, procedure_order_title)
SELECT po.procedure_order_id, 1,
       COALESCE(MIN(res.result_code), 'LOINC:UNK'),
       COALESCE(MIN(res.result_text), 'Lab panel'),
       '1', 0, COALESCE(MIN(res.result_text), 'Lab panel')
FROM procedure_order po
JOIN procedure_report rep ON rep.procedure_order_id = po.procedure_order_id
JOIN procedure_result res ON res.procedure_report_id = rep.procedure_report_id
WHERE po.external_id = 'SEED'
GROUP BY po.procedure_order_id;

-- -----------------------------------------------------------------------------
-- Done.  Summary:
-- -----------------------------------------------------------------------------
SELECT '--- SEED SUMMARY ---' AS section;
SELECT 'patients'      AS entity, COUNT(*) AS n FROM patient_data   WHERE pid BETWEEN 1001 AND 1015 UNION ALL
SELECT 'encounters',           COUNT(*)      FROM form_encounter WHERE external_id='SEED' UNION ALL
SELECT 'vitals',               COUNT(*)      FROM form_vitals    WHERE external_id='SEED' UNION ALL
SELECT 'problems',             COUNT(*)      FROM lists          WHERE external_id='SEED' AND type='medical_problem' UNION ALL
SELECT 'allergies',            COUNT(*)      FROM lists          WHERE external_id='SEED' AND type='allergy' UNION ALL
SELECT 'meds (lists)',         COUNT(*)      FROM lists          WHERE external_id='SEED' AND type='medication' UNION ALL
SELECT 'meds (prescriptions)', COUNT(*)      FROM prescriptions  WHERE external_id='SEED' UNION ALL
SELECT 'lab orders',           COUNT(*)      FROM procedure_order WHERE external_id='SEED' UNION ALL
SELECT 'lab reports',          COUNT(*)      FROM procedure_report WHERE procedure_order_id IN (SELECT procedure_order_id FROM procedure_order WHERE external_id='SEED') UNION ALL
SELECT 'lab results',          COUNT(*)      FROM procedure_result WHERE procedure_report_id IN (SELECT procedure_report_id FROM procedure_report WHERE procedure_order_id IN (SELECT procedure_order_id FROM procedure_order WHERE external_id='SEED')) UNION ALL
SELECT 'critical results',     COUNT(*)      FROM procedure_result WHERE abnormal LIKE 'critical%' AND procedure_report_id IN (SELECT procedure_report_id FROM procedure_report WHERE procedure_order_id IN (SELECT procedure_order_id FROM procedure_order WHERE external_id='SEED')) UNION ALL
SELECT 'soap notes',           COUNT(*)      FROM form_soap      WHERE pid BETWEEN 1001 AND 1015 UNION ALL
SELECT 'pnotes',               COUNT(*)      FROM pnotes         WHERE pid BETWEEN 1001 AND 1015;
