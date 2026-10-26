"""agent_runtime tables, functions and views on PostgreSQL (P0-08): case P-15, schema part.

One script installs the builder's agent_builder scripts (vendored from
ZonicAgentBuilderServer_FastAPI), applies the runtime scripts twice (idempotency),
runs every check and rolls back. Transitions print ``transition|<from>|<to>``.
"""

from __future__ import annotations

import json

import pytest

from tests.sql.conftest import ROOT, Psql, Rows, builder_schema, passed, run_checks, runtime_schema

pytestmark = pytest.mark.sql

RUN = "'00000000-0000-0000-0000-0000000000f1'"
SESSION = "'00000000-0000-0000-0000-00000000005e'"

CHECKS = f"""
-- Schema shape.
INSERT INTO _check SELECT 'tables', count(*) = 9 FROM information_schema.tables
 WHERE table_schema = 'agent_runtime' AND table_type = 'BASE TABLE';
INSERT INTO _check SELECT 'views', count(*) = 9 FROM information_schema.views
 WHERE table_schema = 'agent_runtime';
INSERT INTO _check SELECT 'procedures', count(*) = 14 FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'agent_runtime' AND p.prokind = 'p';
INSERT INTO _check SELECT 'updated_at_triggers', count(*) = 3 FROM pg_trigger
 WHERE tgname LIKE 'trg_runtime_%_updated_at' AND NOT tgisinternal;
INSERT INTO _check SELECT 'migrations', array_agg(version ORDER BY version)
    = ARRAY['001_schema', '002_functions_triggers', '003_indexes', '004_views', '005_procedures']::varchar[]
  FROM agent_runtime.schema_migrations;

-- Builder rows in the builder's own shape (publishChannels and security as the builder saves them).
INSERT INTO agent_builder."agentBuilder_agentworkflow"
    (agentic_workflow_id, name, is_root, is_disabled, publish_channels, security, nodes, raw_json) VALUES
    ('00000000-0000-0000-0000-0000000000a1', 'two channels', true, false,
     '["api", {{"refId": "qi-ui", "code": "qi-ui", "name": "Qi UI"}}]',
     '{{"activities": [{{"refId": "10100002", "code": "10100002", "name": "Home"}}]}}',
     '[{{"id": "start-1", "type": "start"}}]', '{{"schemaVersion": "2.0"}}'),
    ('00000000-0000-0000-0000-0000000000a2', 'no channels', true, false, '[]', '{{}}', '[]', '{{}}'),
    ('00000000-0000-0000-0000-0000000000a3', 'disabled', true, true, '["api"]', '{{}}', '[]', '{{}}'),
    ('00000000-0000-0000-0000-0000000000a4', 'child', false, false, '["api"]', '{{}}', '[]', '{{}}');

-- v_agent_catalog: one row per channel, both channel shapes, NULL without channels.
INSERT INTO _check SELECT 'catalog_channels', array_agg(runtime_channel ORDER BY runtime_channel)
    = ARRAY['api', 'qi-ui']::text[]
  FROM agent_runtime.v_agent_catalog WHERE name = 'two channels';
INSERT INTO _check SELECT 'catalog_no_channel', count(*) = 1 AND bool_and(runtime_channel IS NULL)
  FROM agent_runtime.v_agent_catalog WHERE name = 'no channels';
INSERT INTO _check SELECT 'catalog_excludes_disabled_and_children', count(*) = 0
  FROM agent_runtime.v_agent_catalog WHERE name IN ('disabled', 'child');
INSERT INTO _check SELECT 'catalog_security_and_nodes',
    bool_and(security #>> '{{activities,0,code}}' = '10100002' AND node_count = 1)
  FROM agent_runtime.v_agent_catalog WHERE name = 'two channels';

-- The workflow document is the builder's v_agent_export overlay of a catalog row.
INSERT INTO _check SELECT 'export_document',
    e.export_json ->> 'schemaVersion' = '2.0'
    AND e.export_json #>> '{{nodes,0,type}}' = 'start'
    AND jsonb_array_length(e.export_json -> 'publishChannels') = 2
  FROM agent_builder.v_agent_export e
  JOIN (SELECT DISTINCT agentic_workflow_id FROM agent_runtime.v_agent_catalog WHERE name = 'two channels') c
    USING (agentic_workflow_id);

INSERT INTO agent_runtime.runtime_session (session_id, owner_sub, tenant_id, agentic_workflow_id)
VALUES ({SESSION}, 'u1', 't1', '00000000-0000-0000-0000-0000000000a1');
INSERT INTO agent_runtime.runtime_run
    (run_id, session_id, owner_sub, tenant_id, agentic_workflow_id, status, mode, attempt)
VALUES ({RUN}, {SESSION}, 'u1', 't1', '00000000-0000-0000-0000-0000000000a1', 'running', 'stream', 3);

-- Checkpoint fields of the LangGraph TurnSaver are read from the body (DR-8, DR-9).
INSERT INTO agent_runtime.runtime_checkpoint (run_id, attempt, kind, body)
VALUES ({RUN}, 3, 'async', '{{"format": "zonic-turn-v1", "langgraphVersion": "1.2.12", "lastEventSeq": 42}}');
INSERT INTO _check SELECT 'v_run_checkpoint_fields', has_checkpoint AND last_event_seq = 42
  FROM agent_runtime.v_run WHERE run_id = {RUN};
INSERT INTO _check SELECT 'v_checkpoint_fields', format = 'zonic-turn-v1' AND langgraph_version = '1.2.12'
    AND last_event_seq = 42 AND run_status = 'running' AND run_attempt = 3 AND owner_sub = 'u1'
  FROM agent_runtime.v_checkpoint WHERE run_id = {RUN};
SELECT pg_temp.expect_error('checkpoint_kind_rejected',
    format('UPDATE agent_runtime.runtime_checkpoint SET kind = %L WHERE run_id = %L', 'other', {RUN}),
    '23514', 'new row for relation "runtime_checkpoint" violates check constraint "runtime_checkpoint_kind_check"')
    \\g /dev/null

-- One active run per session (the backstop index under sp_run_start's own check).
SELECT pg_temp.expect_error('one_active_run_per_session', format(
    'INSERT INTO agent_runtime.runtime_run (session_id, owner_sub, agentic_workflow_id, status, mode) '
    'VALUES (%L, %L, %L, %L, %L)', {SESSION}, 'u1', '00000000-0000-0000-0000-0000000000a1', 'queued', 'async'),
    '23505', 'duplicate key value violates unique constraint "ux_run_one_active_per_session"') \\g /dev/null

-- fn_append_messages allocates seq per session and continues it.
SELECT agent_runtime.fn_append_messages({SESSION}, {RUN},
    '[{{"role": "user", "kind": "user_input", "textPreview": "hi"}},
      {{"role": "assistant", "kind": "answer", "contentParts": [{{"type": "text", "text": "hello"}}]}}]') \\g /dev/null
SELECT agent_runtime.fn_append_messages({SESSION}, {RUN}, '[{{"role": "user", "kind": "user_input"}}]') \\g /dev/null
INSERT INTO _check SELECT 'message_seq', array_agg(seq ORDER BY seq) = ARRAY[1, 2, 3]::bigint[]
  FROM agent_runtime.v_message WHERE session_id = {SESSION} AND owner_sub = 'u1';
INSERT INTO _check SELECT 'session_next_seq', next_seq = 3
  FROM agent_runtime.runtime_session WHERE session_id = {SESSION};
INSERT INTO _check SELECT 'append_empty_is_noop', agent_runtime.fn_append_messages({SESSION}, {RUN}, '[]') IS NULL;

-- Pending interrupts are listed on v_run and v_interrupt_pending, with the run's owner.
INSERT INTO agent_runtime.runtime_interrupt (interrupt_id, run_id, node_id, kind)
VALUES (md5('i1')::uuid, {RUN}, 'approve', 'approval');
INSERT INTO _check SELECT 'v_run_pending_interrupts', pending_interrupts = jsonb_build_array(md5('i1')::uuid)
  FROM agent_runtime.v_run WHERE run_id = {RUN};
INSERT INTO _check SELECT 'v_interrupt_pending_owner', session_id = {SESSION} AND owner_sub = 'u1'
  FROM agent_runtime.v_interrupt_pending WHERE interrupt_id = md5('i1')::uuid;

SELECT 'transition|' || f || '|' || t
  FROM unnest(ARRAY['queued', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled', 'timed_out']) f,
       unnest(ARRAY['queued', 'running', 'waiting_input', 'succeeded', 'failed', 'cancelled', 'timed_out']) t
 WHERE agent_runtime.fn_run_transition_allowed(f, t);
"""

EXPECTED = {
    "tables",
    "views",
    "procedures",
    "updated_at_triggers",
    "migrations",
    "catalog_channels",
    "catalog_no_channel",
    "catalog_excludes_disabled_and_children",
    "catalog_security_and_nodes",
    "export_document",
    "v_run_checkpoint_fields",
    "v_checkpoint_fields",
    "checkpoint_kind_rejected",
    "one_active_run_per_session",
    "message_seq",
    "session_next_seq",
    "append_empty_is_noop",
    "v_run_pending_interrupts",
    "v_interrupt_pending_owner",
}


@pytest.fixture(scope="module")
def results(psql: Psql) -> Rows:
    return run_checks(psql, CHECKS, "\n".join([builder_schema(), runtime_schema(), runtime_schema()]))


def test_P15_scripts_apply_twice_and_every_check_runs(results: Rows) -> None:
    assert set(passed(results)) == EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_P15_schema_check(results: Rows, name: str) -> None:
    assert passed(results).get(name) is True, name


def test_P15_transitions_match_run_states_resource(results: Rows) -> None:
    states = json.loads((ROOT / "resources" / "run-states.json").read_text(encoding="utf-8"))
    expected = {(f, t) for f, targets in states["transitions"].items() for t in targets}
    assert {(f, t) for f, t in results["transition"]} == expected
