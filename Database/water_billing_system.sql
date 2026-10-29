-- Reference copy of the C&C H2O water billing schema.
--
-- Written from docs/PRD.md section 6, docs/AUDIT_LOG.md section 3 and
-- docs/DEPLOYMENT.md. The runnable source of truth is the Alembic migrations
-- in alembic/versions. This file is regenerated from them and matches the
-- schema at head (migration 0004: the phone method's ready and cancelled statuses,
-- and who marked a message sent).
--
-- MySQL 8.0, utf8mb4. Money columns are DECIMAL, never float.

CREATE TABLE users (
  user_id        INT AUTO_INCREMENT PRIMARY KEY,
  first_name     VARCHAR(50)  NOT NULL,
  last_name      VARCHAR(50)  NOT NULL,
  phone          VARCHAR(12)  NULL,
  email          VARCHAR(120) NULL,
  password_hash  VARCHAR(255) NOT NULL,
  role           ENUM('owner','member')      NOT NULL,
  status         ENUM('active','inactive')   NOT NULL DEFAULT 'active',
  created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_users_phone (phone),
  UNIQUE KEY uq_users_email (email),
  -- AUTH-1: at least one of phone or email on every account
  CONSTRAINT ck_users_phone_or_email CHECK (phone IS NOT NULL OR email IS NOT NULL)
);

CREATE TABLE customers (
  customer_id     INT AUTO_INCREMENT PRIMARY KEY,
  first_name      VARCHAR(50)  NOT NULL,
  last_name       VARCHAR(50)  NOT NULL,
  phone           VARCHAR(12)  NOT NULL,
  location        VARCHAR(100) NOT NULL,
  meter_number    VARCHAR(20)  NOT NULL,
  rate_per_unit   DECIMAL(10,2) NOT NULL DEFAULT 130.00,
  service_charge  DECIMAL(10,2) NOT NULL DEFAULT 150.00,
  status          ENUM('active','inactive') NOT NULL DEFAULT 'active',
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_customers_meter_number (meter_number)
);

CREATE TABLE meter_readings (
  reading_id        INT AUTO_INCREMENT PRIMARY KEY,
  customer_id       INT NOT NULL,
  billing_period    CHAR(7) NOT NULL,                 -- YYYY-MM, taken from reading_date (RD-5)
  previous_reading  DECIMAL(10,2) NOT NULL,
  current_reading   DECIMAL(10,2) NOT NULL,
  units_consumed    DECIMAL(10,2) NOT NULL,
  reading_date      DATE NOT NULL,
  recorded_by       INT NOT NULL,                     -- RD-7: who took the reading
  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  -- RD-4: one reading per customer per period
  UNIQUE KEY uq_readings_customer_period (customer_id, billing_period),
  CONSTRAINT fk_readings_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
  CONSTRAINT fk_readings_user     FOREIGN KEY (recorded_by) REFERENCES users (user_id),
  -- RD-3: a reading never goes down (meter rollover is out of scope)
  CONSTRAINT ck_readings_not_lower CHECK (current_reading >= previous_reading)
);

CREATE TABLE bills (
  bill_id             INT AUTO_INCREMENT PRIMARY KEY,
  reading_id          INT NOT NULL,
  customer_id         INT NOT NULL,
  rate_per_unit       DECIMAL(10,2) NOT NULL,         -- BILL-5: copied from the customer
  service_charge      DECIMAL(10,2) NOT NULL,         -- BILL-5: copied from the customer
  units_consumed      DECIMAL(10,2) NOT NULL,
  consumption_charge  DECIMAL(12,2) NOT NULL,
  previous_balance    DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  total_due           DECIMAL(12,2) NOT NULL,
  amount_paid         DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  status              ENUM('unpaid','partial','paid') NOT NULL DEFAULT 'unpaid',
  created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  -- BILL-6: one bill per reading
  UNIQUE KEY uq_bills_reading (reading_id),
  INDEX idx_bills_customer (customer_id, created_at),
  CONSTRAINT fk_bills_reading  FOREIGN KEY (reading_id)  REFERENCES meter_readings (reading_id),
  CONSTRAINT fk_bills_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
);

CREATE TABLE payments (
  payment_id       INT AUTO_INCREMENT PRIMARY KEY,
  bill_id          INT NOT NULL,
  amount           DECIMAL(12,2) NOT NULL,
  payment_date     DATE NOT NULL,
  method           VARCHAR(20) NOT NULL DEFAULT 'mpesa',   -- PAY-1: M-Pesa only
  mpesa_reference  VARCHAR(20) NULL,                       -- PAY-8: unique when given
  recorded_by      INT NOT NULL,
  created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_payments_mpesa_reference (mpesa_reference),
  INDEX idx_payments_bill (bill_id),
  INDEX idx_payments_date (payment_date),
  CONSTRAINT fk_payments_bill FOREIGN KEY (bill_id)     REFERENCES bills (bill_id),
  CONSTRAINT fk_payments_user FOREIGN KEY (recorded_by) REFERENCES users (user_id),
  -- PAY-4: amount must be greater than 0
  CONSTRAINT ck_payments_amount_positive CHECK (amount > 0)
);

CREATE TABLE messages_log (
  message_id           INT AUTO_INCREMENT PRIMARY KEY,
  -- COR-5: emptied, not deleted, when a bill is deleted, so SMS history survives
  bill_id              INT NULL,
  customer_id          INT NOT NULL,
  message_type         ENUM('bill','correction') NOT NULL,
  content              TEXT NOT NULL,
  -- SMS-14, SMS-20: ready and cancelled are the phone method's (migration 0004)
  status               ENUM('pending','sent','failed','ready','cancelled') NOT NULL DEFAULT 'pending',
  provider_message_id  VARCHAR(100) NULL,
  created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  -- SMS-3: a plain reason chosen by the app, empty unless the message failed (migration 0003)
  failure_reason       VARCHAR(255) NULL,
  -- SMS-17: who marked a phone-method message sent; no cascade, AUTH-4 (migration 0004)
  sent_by_user_id      INT NULL,
  INDEX idx_messages_bill (bill_id, created_at),
  INDEX idx_messages_customer (customer_id),
  CONSTRAINT fk_messages_bill     FOREIGN KEY (bill_id)     REFERENCES bills (bill_id) ON DELETE SET NULL,
  CONSTRAINT fk_messages_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
  CONSTRAINT fk_messages_sent_by  FOREIGN KEY (sent_by_user_id) REFERENCES users (user_id)
);

-- Developer only. No foreign keys on purpose (AUDIT_LOG.md section 3), so a
-- record of "reading 41 was deleted" survives after the reading is gone.
CREATE TABLE audit_log (
  audit_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
  created_at     TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  level          ENUM('info','warning','error') NOT NULL DEFAULT 'info',
  event_type     VARCHAR(50)  NOT NULL,
  summary        VARCHAR(255) NOT NULL,
  actor_user_id  INT NULL,
  actor_label    VARCHAR(100) NULL,
  entity_type    VARCHAR(30) NULL,
  entity_id      INT NULL,
  customer_id    INT NULL,
  request_id     VARCHAR(36) NULL,
  ip_address     VARCHAR(45) NULL,
  details        JSON NULL,
  INDEX idx_audit_created  (created_at),
  INDEX idx_audit_event    (event_type, created_at),
  INDEX idx_audit_entity   (entity_type, entity_id),
  INDEX idx_audit_customer (customer_id, created_at),
  INDEX idx_audit_request  (request_id)
);
