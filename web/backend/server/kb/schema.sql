-- 书籍 RAG 书目与 k 规格（MySQL 8；库由 ensure_schema 创建，表幂等）
CREATE TABLE IF NOT EXISTS kb_books (
  id                BIGINT AUTO_INCREMENT PRIMARY KEY,
  book_id           VARCHAR(16) NOT NULL UNIQUE,
  filename          VARCHAR(255) NOT NULL,
  ext               VARCHAR(10) NOT NULL,
  sha256            CHAR(64) NOT NULL,
  dedup_key         VARCHAR(96) NOT NULL UNIQUE,
  size_bytes        BIGINT NOT NULL,
  status            VARCHAR(24) NOT NULL,
  index_state       VARCHAR(24) NOT NULL DEFAULT 'none',
  current_generation INT NOT NULL DEFAULT 0,
  chunk_count       INT NOT NULL DEFAULT 0,
  retry_count       TINYINT NOT NULL DEFAULT 0,
  error_code        VARCHAR(48) NULL,
  error_detail      VARCHAR(500) NULL,
  title             VARCHAR(200) NULL,
  author            VARCHAR(100) NULL,
  publisher         VARCHAR(100) NULL,
  version           VARCHAR(50) NULL,
  meta_json         JSON NULL,
  mineru_json       JSON NULL,
  ocr_quality_json  JSON NULL,
  original_path     VARCHAR(512) NULL,
  raw_md_path       VARCHAR(512) NULL,
  raw_json_path     VARCHAR(512) NULL,
  cleaned_path      VARCHAR(512) NULL,
  created_at        DATETIME(3) NOT NULL,
  updated_at        DATETIME(3) NOT NULL,
  deleted_at        DATETIME(3) NULL,
  KEY idx_kb_status (status, updated_at),
  KEY idx_kb_sha (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS kb_k_spec (
  artifact_version VARCHAR(64) PRIMARY KEY,
  global_k         SMALLINT NOT NULL,
  labels_present   TINYINT NOT NULL,
  active           TINYINT NOT NULL DEFAULT 0,
  eval_json        JSON NULL,
  created_at       DATETIME(3) NOT NULL,
  KEY idx_k_spec_active (active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS kb_query_clusters (
  artifact_version VARCHAR(64) NOT NULL,
  cluster_id       TINYINT NOT NULL,
  centroid         MEDIUMBLOB NOT NULL,
  k_star           SMALLINT NOT NULL,
  sample_count     INT NOT NULL,
  PRIMARY KEY (artifact_version, cluster_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
