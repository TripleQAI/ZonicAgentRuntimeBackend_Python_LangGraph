-- 012_builder_records.sql — user settings, AgentCore deployments and gateways.
--
-- These records used to live elsewhere: user settings in a DynamoDB table, and
-- deployments and gateways nowhere at all — the builder rediscovered them on
-- every request by listing AWS and filtering on tags and name patterns.
--
-- Unlike the tables in 002, these belong to this project rather than to Django,
-- so this file holds their tables, indexes, triggers, views and procedures
-- together. The rules of 009 and 010 still hold: views shape, procedures persist
-- a decision Python has already made, and every insert supplies its id and
-- timestamps explicitly.
--
-- Ownership follows 006: owner_sub is the Cognito subject, owner_email is kept
-- for display. Deployments are shared across users (the Deployments page lists
-- every runtime the builder created), so they record who deployed rather than
-- who owns.
--
-- Run after 010. Idempotent.

SET search_path TO agent_builder, public;

-- ==========================================================================
-- Tables
-- ==========================================================================

CREATE TABLE IF NOT EXISTS agent_builder.user_settings (
    id           uuid          NOT NULL PRIMARY KEY,
    owner_sub    varchar(64)   NOT NULL UNIQUE,
    owner_email  varchar(320)  NOT NULL DEFAULT '',
    settings     jsonb         NOT NULL DEFAULT '{}'::jsonb,
    version      integer       NOT NULL DEFAULT 1,
    created_at   timestamptz   NOT NULL DEFAULT now(),
    updated_at   timestamptz   NOT NULL DEFAULT now()
);

COMMENT ON TABLE agent_builder.user_settings IS
    'One row per user: the builder settings page (models/settings_models.py)';

-- A redeploy of the same agent name updates the same runtime ARN, so the ARN is
-- the key and deploy_count records how often it was deployed.
CREATE TABLE IF NOT EXISTS agent_builder.agent_deployment (
    id                   uuid          NOT NULL PRIMARY KEY,
    agent_runtime_arn    text          NOT NULL UNIQUE,
    agent_name           varchar(255)  NOT NULL DEFAULT '',
    deployment_type      varchar(20)   NOT NULL DEFAULT 'agent',
    description          text          NOT NULL DEFAULT '',
    region               varchar(32)   NOT NULL DEFAULT '',
    agentic_workflow_id  uuid,
    deployed_by_sub      varchar(64)   NOT NULL,
    deployed_by_email    varchar(320)  NOT NULL DEFAULT '',
    deploy_count         integer       NOT NULL DEFAULT 1,
    first_deployed_at    timestamptz   NOT NULL DEFAULT now(),
    last_deployed_at     timestamptz   NOT NULL DEFAULT now()
);

COMMENT ON COLUMN agent_builder.agent_deployment.agentic_workflow_id IS
    'The workflow the code was generated from, when the caller sent one';

CREATE TABLE IF NOT EXISTS agent_builder.agentcore_gateway (
    id           uuid          NOT NULL PRIMARY KEY,
    gateway_id   varchar(100)  NOT NULL UNIQUE,
    name         varchar(100)  NOT NULL DEFAULT '',
    description  text          NOT NULL DEFAULT '',
    endpoint     text          NOT NULL DEFAULT '',
    role_arn     text          NOT NULL DEFAULT '',
    region       varchar(32)   NOT NULL DEFAULT '',
    status       varchar(30)   NOT NULL DEFAULT '',
    owner_sub    varchar(64)   NOT NULL,
    owner_email  varchar(320)  NOT NULL DEFAULT '',
    created_at   timestamptz   NOT NULL DEFAULT now(),
    updated_at   timestamptz   NOT NULL DEFAULT now()
);

COMMENT ON COLUMN agent_builder.agentcore_gateway.status IS
    'Status when last recorded; AgentCore stays the source of truth for the live value';

-- No foreign key to agentcore_gateway: a target can be added to a gateway created
-- before this table existed. The view joins the two, so such a target becomes
-- visible once its gateway is recorded (scripts/backfill_builder_records.py).
CREATE TABLE IF NOT EXISTS agent_builder.agentcore_gateway_target (
    id            uuid          NOT NULL PRIMARY KEY,
    gateway_id    varchar(100)  NOT NULL,
    target_id     varchar(100)  NOT NULL,
    name          varchar(100)  NOT NULL DEFAULT '',
    lambda_arn    text          NOT NULL DEFAULT '',
    tool_schemas  jsonb         NOT NULL DEFAULT '[]'::jsonb,
    created_at    timestamptz   NOT NULL DEFAULT now(),
    updated_at    timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (gateway_id, target_id)
);

-- ==========================================================================
-- Indexes and triggers
-- ==========================================================================

CREATE INDEX IF NOT EXISTS idx_agent_deployment_deployed_by
    ON agent_builder.agent_deployment (deployed_by_sub);

CREATE INDEX IF NOT EXISTS idx_agent_deployment_workflow
    ON agent_builder.agent_deployment (agentic_workflow_id)
    WHERE agentic_workflow_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agentcore_gateway_owner
    ON agent_builder.agentcore_gateway (owner_sub);

-- updated_at maintenance, as 003 does for the Django tables.
DROP TRIGGER IF EXISTS trg_user_settings_updated_at ON agent_builder.user_settings;
CREATE TRIGGER trg_user_settings_updated_at
    BEFORE UPDATE ON agent_builder.user_settings
    FOR EACH ROW EXECUTE FUNCTION agent_builder.set_updated_at();

DROP TRIGGER IF EXISTS trg_agentcore_gateway_updated_at ON agent_builder.agentcore_gateway;
CREATE TRIGGER trg_agentcore_gateway_updated_at
    BEFORE UPDATE ON agent_builder.agentcore_gateway
    FOR EACH ROW EXECUTE FUNCTION agent_builder.set_updated_at();

DROP TRIGGER IF EXISTS trg_agentcore_gateway_target_updated_at ON agent_builder.agentcore_gateway_target;
CREATE TRIGGER trg_agentcore_gateway_target_updated_at
    BEFORE UPDATE ON agent_builder.agentcore_gateway_target
    FOR EACH ROW EXECUTE FUNCTION agent_builder.set_updated_at();

-- ==========================================================================
-- Views — the read surface. Per-user views end with their owner column.
-- ==========================================================================

CREATE OR REPLACE VIEW agent_builder.v_user_settings AS
SELECT
    s.owner_email,
    s.settings,
    s.version,
    s.created_at,
    s.updated_at,
    s.owner_sub
FROM agent_builder.user_settings s;

CREATE OR REPLACE VIEW agent_builder.v_agent_deployment AS
SELECT
    d.agent_runtime_arn,
    d.agent_name,
    d.deployment_type,
    d.description,
    d.region,
    d.agentic_workflow_id,
    d.deployed_by_sub,
    d.deployed_by_email,
    d.deploy_count,
    d.first_deployed_at,
    d.last_deployed_at
FROM agent_builder.agent_deployment d;

CREATE OR REPLACE VIEW agent_builder.v_agentcore_gateway AS
SELECT
    g.gateway_id,
    g.name,
    g.description,
    g.endpoint,
    g.role_arn,
    g.region,
    g.status,
    g.owner_email,
    g.created_at,
    g.updated_at,
    g.owner_sub
FROM agent_builder.agentcore_gateway g;

CREATE OR REPLACE VIEW agent_builder.v_agentcore_gateway_target AS
SELECT
    t.gateway_id,
    t.target_id,
    t.name,
    t.lambda_arn,
    t.tool_schemas,
    t.created_at,
    t.updated_at,
    g.owner_sub
FROM agent_builder.agentcore_gateway_target t
JOIN agent_builder.agentcore_gateway g ON g.gateway_id = t.gateway_id;

-- ==========================================================================
-- Procedures — the write surface. One CALL, one transaction.
-- ==========================================================================

-- Save replaces the whole settings document; version counts the saves.
CREATE OR REPLACE PROCEDURE agent_builder.sp_user_settings_save(
    p_owner_sub   text,
    p_owner_email text,
    p_settings    jsonb,
    INOUT o_version    integer     DEFAULT NULL,
    INOUT o_updated_at timestamptz DEFAULT NULL
) AS $$
BEGIN
    INSERT INTO agent_builder.user_settings
        (id, owner_sub, owner_email, settings, version, created_at, updated_at)
    VALUES
        (gen_random_uuid(), p_owner_sub, COALESCE(p_owner_email, ''),
         COALESCE(p_settings, '{}'::jsonb), 1, now(), now())
    ON CONFLICT (owner_sub) DO UPDATE SET
        owner_email = EXCLUDED.owner_email,
        settings    = EXCLUDED.settings,
        version     = agent_builder.user_settings.version + 1,
        updated_at  = now()
    RETURNING version, updated_at INTO o_version, o_updated_at;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

CREATE OR REPLACE PROCEDURE agent_builder.sp_user_settings_delete(
    p_owner_sub text,
    INOUT o_deleted boolean DEFAULT NULL
) AS $$
BEGIN
    DELETE FROM agent_builder.user_settings WHERE owner_sub = p_owner_sub;
    o_deleted := FOUND;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

-- Payload keys match the columns (010's convention). A redeploy keeps the
-- workflow link it already had when the caller sends none.
CREATE OR REPLACE PROCEDURE agent_builder.sp_agent_deployment_record(
    p_deployment jsonb,
    INOUT o_deploy_count      integer     DEFAULT NULL,
    INOUT o_first_deployed_at timestamptz DEFAULT NULL
) AS $$
BEGIN
    INSERT INTO agent_builder.agent_deployment
        (id, agent_runtime_arn, agent_name, deployment_type, description, region,
         agentic_workflow_id, deployed_by_sub, deployed_by_email, deploy_count,
         first_deployed_at, last_deployed_at)
    SELECT
        gen_random_uuid(), d.agent_runtime_arn, COALESCE(d.agent_name, ''),
        COALESCE(d.deployment_type, 'agent'), COALESCE(d.description, ''),
        COALESCE(d.region, ''), d.agentic_workflow_id, d.deployed_by_sub,
        COALESCE(d.deployed_by_email, ''), 1, now(), now()
    FROM jsonb_to_record(p_deployment) AS d(
        agent_runtime_arn text, agent_name text, deployment_type text,
        description text, region text, agentic_workflow_id uuid,
        deployed_by_sub text, deployed_by_email text
    )
    ON CONFLICT (agent_runtime_arn) DO UPDATE SET
        agent_name          = EXCLUDED.agent_name,
        deployment_type     = EXCLUDED.deployment_type,
        description         = EXCLUDED.description,
        region              = EXCLUDED.region,
        agentic_workflow_id = COALESCE(EXCLUDED.agentic_workflow_id,
                                       agent_builder.agent_deployment.agentic_workflow_id),
        deployed_by_sub     = EXCLUDED.deployed_by_sub,
        deployed_by_email   = EXCLUDED.deployed_by_email,
        deploy_count        = agent_builder.agent_deployment.deploy_count + 1,
        last_deployed_at    = now()
    RETURNING deploy_count, first_deployed_at INTO o_deploy_count, o_first_deployed_at;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

CREATE OR REPLACE PROCEDURE agent_builder.sp_agentcore_gateway_record(
    p_gateway jsonb,
    INOUT o_created_at timestamptz DEFAULT NULL
) AS $$
BEGIN
    INSERT INTO agent_builder.agentcore_gateway
        (id, gateway_id, name, description, endpoint, role_arn, region, status,
         owner_sub, owner_email, created_at, updated_at)
    SELECT
        gen_random_uuid(), g.gateway_id, COALESCE(g.name, ''),
        COALESCE(g.description, ''), COALESCE(g.endpoint, ''),
        COALESCE(g.role_arn, ''), COALESCE(g.region, ''), COALESCE(g.status, ''),
        g.owner_sub, COALESCE(g.owner_email, ''), now(), now()
    FROM jsonb_to_record(p_gateway) AS g(
        gateway_id text, name text, description text, endpoint text,
        role_arn text, region text, status text, owner_sub text, owner_email text
    )
    ON CONFLICT (gateway_id) DO UPDATE SET
        name        = EXCLUDED.name,
        description = EXCLUDED.description,
        endpoint    = EXCLUDED.endpoint,
        role_arn    = EXCLUDED.role_arn,
        region      = EXCLUDED.region,
        status      = EXCLUDED.status,
        owner_sub   = EXCLUDED.owner_sub,
        owner_email = EXCLUDED.owner_email,
        updated_at  = now()
    RETURNING created_at INTO o_created_at;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

CREATE OR REPLACE PROCEDURE agent_builder.sp_agentcore_gateway_target_record(
    p_target jsonb,
    INOUT o_created_at timestamptz DEFAULT NULL
) AS $$
BEGIN
    INSERT INTO agent_builder.agentcore_gateway_target
        (id, gateway_id, target_id, name, lambda_arn, tool_schemas, created_at, updated_at)
    SELECT
        gen_random_uuid(), t.gateway_id, t.target_id, COALESCE(t.name, ''),
        COALESCE(t.lambda_arn, ''), COALESCE(t.tool_schemas, '[]'::jsonb), now(), now()
    FROM jsonb_to_record(p_target) AS t(
        gateway_id text, target_id text, name text, lambda_arn text, tool_schemas jsonb
    )
    ON CONFLICT (gateway_id, target_id) DO UPDATE SET
        name         = EXCLUDED.name,
        lambda_arn   = EXCLUDED.lambda_arn,
        tool_schemas = EXCLUDED.tool_schemas,
        updated_at   = now()
    RETURNING created_at INTO o_created_at;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_builder, public, pg_temp;

INSERT INTO agent_builder.schema_migrations (version) VALUES ('012_builder_records')
    ON CONFLICT (version) DO UPDATE SET applied_at = now();
