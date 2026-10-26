-- 003_indexes.sql — agent_runtime indexes (design Section 13.2).

CREATE INDEX IF NOT EXISTS ix_session_owner_recent
    ON agent_runtime.runtime_session (owner_sub, last_message_at DESC);

CREATE INDEX IF NOT EXISTS ix_message_session_created
    ON agent_runtime.runtime_message (session_id, created_at);

CREATE INDEX IF NOT EXISTS ix_run_session_started
    ON agent_runtime.runtime_run (session_id, started_at DESC);

CREATE INDEX IF NOT EXISTS ix_run_active_status
    ON agent_runtime.runtime_run (status)
    WHERE status IN ('queued', 'running', 'waiting_input');

CREATE INDEX IF NOT EXISTS ix_run_owner_active
    ON agent_runtime.runtime_run (owner_sub)
    WHERE status IN ('queued', 'running', 'waiting_input');

CREATE INDEX IF NOT EXISTS ix_run_tenant_active
    ON agent_runtime.runtime_run (tenant_id)
    WHERE status IN ('queued', 'running', 'waiting_input');

-- One active run per session (settings.runs.maxActivePerSession = 1, the default). sp_run_start
-- checks the setting itself; this index is the backstop. Drop it before raising the setting above 1.
CREATE UNIQUE INDEX IF NOT EXISTS ux_run_one_active_per_session
    ON agent_runtime.runtime_run (session_id)
    WHERE status IN ('queued', 'running', 'waiting_input');

CREATE UNIQUE INDEX IF NOT EXISTS ux_run_idempotency
    ON agent_runtime.runtime_run (owner_sub, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- The durable queue: claims and expiry scans read only live rows (design Section 9.5).
CREATE INDEX IF NOT EXISTS ix_run_queue
    ON agent_runtime.runtime_run (tenant_id, priority DESC, queued_at)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS ix_run_lease_expiry
    ON agent_runtime.runtime_run (lease_expires_at)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS ix_interrupt_status_expiry
    ON agent_runtime.runtime_interrupt (status, expires_at);

CREATE INDEX IF NOT EXISTS ix_interrupt_run
    ON agent_runtime.runtime_interrupt (run_id);

CREATE INDEX IF NOT EXISTS ix_dataset_run
    ON agent_runtime.runtime_dataset (run_id);

CREATE INDEX IF NOT EXISTS ix_dataset_expiry
    ON agent_runtime.runtime_dataset (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_checkpoint_expiry
    ON agent_runtime.runtime_checkpoint (expires_at)
    WHERE expires_at IS NOT NULL;

INSERT INTO agent_runtime.schema_migrations (version) VALUES ('003_indexes') ON CONFLICT (version) DO NOTHING;
