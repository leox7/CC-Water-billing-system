-- Removes audit_log rows older than 12 months (AUDIT_LOG.md rule 12, AUD-3).
--
-- Run by hand, about once a year, never from the app. The app's own database user cannot
-- delete audit rows (DEPLOYMENT.md section 9.2), so connect with the migration login
-- (water_migrate, from /srv/water-billing/.env.migrate), in MySQL Workbench or the mysql
-- command line, and choose the water_billing database first.
--
-- Take a backup first (DEPLOYMENT.md section 14.2). Run the first statement on its own and
-- check the number looks sensible, then run the second.

-- 1. How many rows will go
SELECT COUNT(*) AS rows_to_remove, MIN(created_at) AS oldest, MAX(created_at) AS newest
FROM audit_log
WHERE created_at < NOW() - INTERVAL 12 MONTH;

-- 2. Remove them. created_at is indexed, so this is quick and Workbench's safe mode allows it.
DELETE FROM audit_log
WHERE created_at < NOW() - INTERVAL 12 MONTH;
