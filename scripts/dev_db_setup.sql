-- Creates the development and test databases and a limited MySQL user for them.
--
-- The developer runs this ONCE, as the MySQL admin. The coding agent never
-- uses root and never runs this file.
--
--   1. Replace both 'change-me' passwords below with a password of your own.
--   2. Run it:   mysql -u root -p < scripts/dev_db_setup.sql
--   3. Put the same password in .env (DATABASE_URL and TEST_DATABASE_URL).
--
-- The user has rights on these two databases only: nothing on other databases
-- (such as the payroll system's) and nothing on the server itself (DB-2).
-- It is safe to run again; existing objects are left alone.

CREATE DATABASE IF NOT EXISTS water_billing_dev
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS water_billing_test
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Both host spellings, because MySQL on Windows may see 127.0.0.1 as localhost.
CREATE USER IF NOT EXISTS 'water_dev'@'127.0.0.1' IDENTIFIED BY 'change-me';
CREATE USER IF NOT EXISTS 'water_dev'@'localhost' IDENTIFIED BY 'change-me';

-- Development may change table structure (Alembic runs as this user locally).
-- Production is different: see DEPLOYMENT.md section 9.2.
GRANT ALL PRIVILEGES ON water_billing_dev.*  TO 'water_dev'@'127.0.0.1';
GRANT ALL PRIVILEGES ON water_billing_test.* TO 'water_dev'@'127.0.0.1';
GRANT ALL PRIVILEGES ON water_billing_dev.*  TO 'water_dev'@'localhost';
GRANT ALL PRIVILEGES ON water_billing_test.* TO 'water_dev'@'localhost';

FLUSH PRIVILEGES;
