-- 患者画像层（MySQL 8；库由 ensure_schema 创建，表幂等）
CREATE TABLE IF NOT EXISTS patients (
  patient_id      CHAR(12) PRIMARY KEY,
  display_name    VARCHAR(64)  NULL,
  sex             ENUM('male','female','other','unknown') NOT NULL DEFAULT 'unknown',
  birth_year      SMALLINT     NULL,
  phone           VARCHAR(32)  NULL,
  id_no           VARCHAR(32)  NULL,
  note            VARCHAR(500) NULL,
  status          ENUM('active','merged','deleted') NOT NULL DEFAULT 'active',
  merged_into     CHAR(12) NULL,
  created_at      DATETIME(3) NOT NULL,
  updated_at      DATETIME(3) NOT NULL,
  INDEX idx_patients_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS patient_facts (
  fact_id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  patient_id      CHAR(12) NOT NULL,
  category        VARCHAR(32) NOT NULL,
  field           VARCHAR(64) NOT NULL,
  value           VARCHAR(500) NOT NULL,
  normalized_value VARCHAR(255) NOT NULL,
  evidence        VARCHAR(500) NOT NULL DEFAULT '',
  source          ENUM('doctor_modified','doctor_input','case_import','llm_extract') NOT NULL,
  confidence      DECIMAL(3,2) NOT NULL DEFAULT 1.00,
  source_case_id  CHAR(12) NULL,
  source_msg_id   VARCHAR(64) NULL,
  status          ENUM('active','superseded','disputed','deleted') NOT NULL DEFAULT 'active',
  effective_from  DATETIME(3) NOT NULL,
  superseded_by   BIGINT NULL,
  created_at      DATETIME(3) NOT NULL,
  INDEX idx_facts_lookup (patient_id, category, field, status),
  INDEX idx_facts_case (source_case_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS case_patients (
  case_id         CHAR(12) PRIMARY KEY,
  patient_id      CHAR(12) NOT NULL,
  linked_at       DATETIME(3) NOT NULL,
  linked_by       VARCHAR(32) NOT NULL DEFAULT 'doctor',
  INDEX idx_case_patient (patient_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS case_memory_cursor (
  case_id         CHAR(12) PRIMARY KEY,
  upto_seq        INT NOT NULL DEFAULT 0,
  updated_at      DATETIME(3) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS memory_jobs (
  job_id          CHAR(32) PRIMARY KEY,
  case_id         CHAR(12) NOT NULL,
  from_seq        INT NOT NULL,
  upto_seq        INT NOT NULL,
  status          ENUM('queued','running','done','failed') NOT NULL DEFAULT 'queued',
  attempts        TINYINT NOT NULL DEFAULT 0,
  last_error      VARCHAR(500) NOT NULL DEFAULT '',
  created_at      DATETIME(3) NOT NULL,
  updated_at      DATETIME(3) NOT NULL,
  UNIQUE KEY uniq_case_upto (case_id, upto_seq),
  INDEX idx_jobs_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
