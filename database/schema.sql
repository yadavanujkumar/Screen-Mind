-- Screen-Mind PostgreSQL schema
-- Requires PostgreSQL 13+ (gen_random_uuid from pgcrypto, JSONB support)

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ------------------------------------------------------------------ --
-- Users                                                               --
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS users (
    id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    email      VARCHAR(255) UNIQUE NOT NULL,
    api_key    VARCHAR(512) UNIQUE NOT NULL,
    role       VARCHAR(50)  NOT NULL DEFAULT 'operator',
    created_at TIMESTAMP    NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------------ --
-- Tasks                                                               --
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS tasks (
    id               UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID    REFERENCES users(id) ON DELETE SET NULL,
    task_description TEXT    NOT NULL,
    status           VARCHAR(50)  NOT NULL DEFAULT 'pending',
    start_time       TIMESTAMP,
    end_time         TIMESTAMP,
    created_at       TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------------ --
-- Actions                                                             --
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS actions (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id     UUID         REFERENCES tasks(id) ON DELETE CASCADE,
    action_type VARCHAR(100) NOT NULL,
    coordinates JSONB,
    text        TEXT,
    status      VARCHAR(50),
    timestamp   TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_actions_task_id ON actions(task_id);

-- ------------------------------------------------------------------ --
-- Logs                                                                --
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS logs (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      UUID         REFERENCES tasks(id) ON DELETE CASCADE,
    service_name VARCHAR(100),
    log_level    VARCHAR(20),
    message      TEXT,
    timestamp    TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_logs_task_id ON logs(task_id);

-- ------------------------------------------------------------------ --
-- Memory                                                              --
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS memory (
    id              UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID    REFERENCES tasks(id) ON DELETE CASCADE,
    content         TEXT    NOT NULL,
    embedding       JSONB,
    importance_score FLOAT  NOT NULL DEFAULT 0.5,
    memory_type     VARCHAR(50) NOT NULL DEFAULT 'general',
    timestamp       TIMESTAMP   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_task_id ON memory(task_id);

-- ------------------------------------------------------------------ --
-- Metrics                                                             --
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS metrics (
    id           UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      UUID    REFERENCES tasks(id) ON DELETE CASCADE,
    step_time    FLOAT,
    model_latency FLOAT,
    success_rate  FLOAT,
    step_number   INT,
    timestamp     TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_metrics_task_id ON metrics(task_id);

-- ------------------------------------------------------------------ --
-- Explainability logs                                                 --
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS explainability_logs (
    id                UUID   PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id           UUID   REFERENCES tasks(id) ON DELETE CASCADE,
    step_number       INT,
    screen_text       TEXT,
    detected_elements JSONB,
    goal              TEXT,
    decision          TEXT,
    reason            TEXT,
    alternatives      JSONB,
    confidence_score  FLOAT,
    timestamp         TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_explainability_logs_task_id ON explainability_logs(task_id);

-- ------------------------------------------------------------------ --
-- Audit logs                                                          --
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS audit_logs (
    id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID         REFERENCES users(id) ON DELETE SET NULL,
    action     VARCHAR(255),
    resource   VARCHAR(255),
    ip_address VARCHAR(45),
    timestamp  TIMESTAMP    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
