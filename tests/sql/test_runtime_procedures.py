"""agent_runtime procedures on PostgreSQL (P0-08): cases P-01, P-02 and the procedure part of P-15.

The procedures (005, vendored from ZonicAgentRuntimeServer_Nodejs) are called the way
the gateway calls them: positional arguments, jsonb documents, INOUT outputs last.
Errors are checked by SQLSTATE and reason token, the contract the gateway adapter maps.
Everything runs in one transaction that is rolled back.
"""

from __future__ import annotations

import pytest

from tests.sql.conftest import Psql, Rows, passed, run_checks

pytestmark = pytest.mark.sql

CHECKS = r"""
INSERT INTO agent_builder."agentBuilder_agentworkflow" (agentic_workflow_id, name, is_root, publish_channels)
VALUES ('00000000-0000-0000-0000-0000000000a1', 'wf', true, '["api"]');

-- P-01: sp_run_start opens a stream run with the user's message; sp_run_finish saves the turn.
-- P-02: owner mismatch, invalid transition, second finish, run in progress, stale attempt.
DO $$
DECLARE
    l_run uuid; l_session uuid; l_msg uuid;
    l_run2 uuid; l_session2 uuid; l_msg2 uuid;
    l_other uuid; l_other_session uuid; l_other_msg uuid;
    r agent_runtime.runtime_run;
    l_start jsonb := '{"ownerSub": "u1", "tenantId": "t1", "agenticWorkflowId": "00000000-0000-0000-0000-0000000000a1",
                       "mode": "stream", "workflowRevision": "rev1", "documentVersion": "2", "idempotencyKey": "k1",
                       "leaseOwner": "i1", "leaseAddress": "http://10.0.0.1:9000", "leaseSeconds": 30,
                       "limits": {"maxActivePerSession": 1, "maxActivePerUser": 5, "maxActivePerTenant": 50}}';
    l_user jsonb := '{"contentParts": [{"type": "text", "text": "hi"}], "textPreview": "hi"}';
    l_save jsonb := '{"status": "succeeded", "result": {"text": "hello"}, "usage": {"totalTokens": 3},
                      "nodeTimeline": [{"nodeId": "agent-1", "status": "succeeded"}],
                      "messages": [{"role": "assistant", "kind": "answer", "textPreview": "hello"}],
                      "sessionState": {"flow": {"topic": "x"}},
                      "datasets": [{"datasetId": "00000000-0000-0000-0000-0000000000d1", "nodeId": "db-1",
                                    "store": "cache", "kind": "rows", "cacheKey": "c1", "rowCount": 2}]}';
BEGIN
    CALL agent_runtime.sp_run_start(l_start, l_user, l_run, l_session, l_msg);
    SELECT * INTO r FROM agent_runtime.runtime_run WHERE run_id = l_run;
    INSERT INTO _check VALUES ('P01_start_running', r.status = 'running' AND r.attempt = 1
        AND r.session_id = l_session AND r.lease_owner = 'i1' AND r.lease_expires_at > now()
        AND r.started_at IS NOT NULL);
    INSERT INTO _check SELECT 'P01_start_user_message', m.kind = 'user_input' AND m.role = 'user'
        AND m.seq = 1 AND m.run_id = l_run
      FROM agent_runtime.runtime_message m WHERE m.message_id = l_msg;
    CALL agent_runtime.sp_run_start(l_start, l_user, l_run2, l_session2, l_msg2);
    INSERT INTO _check VALUES ('P01_start_idempotency_key', l_run2 = l_run AND l_session2 = l_session AND l_msg2 = l_msg);

    PERFORM pg_temp.expect_error('P02_run_in_progress', format(
        'CALL agent_runtime.sp_run_start(%L, NULL, NULL, NULL, NULL)',
        jsonb_build_object('ownerSub', 'u1', 'agenticWorkflowId', '00000000-0000-0000-0000-0000000000a1',
                           'mode', 'stream', 'sessionId', l_session)),
        '23R02', 'run_in_progress');
    PERFORM pg_temp.expect_error('P02_owner_mismatch_start', format(
        'CALL agent_runtime.sp_run_start(%L, NULL, NULL, NULL, NULL)',
        jsonb_build_object('ownerSub', 'u2', 'agenticWorkflowId', '00000000-0000-0000-0000-0000000000a1',
                           'mode', 'stream', 'sessionId', l_session)),
        '23O01', 'owner_mismatch');
    PERFORM pg_temp.expect_error('P02_owner_mismatch_session_update', format(
        'CALL agent_runtime.sp_session_update(%L, %L, %L)', l_session, 'u2', '{"title": "mine"}'),
        '23O01', 'owner_mismatch');
    PERFORM pg_temp.expect_error('P02_owner_mismatch_transition', format(
        'CALL agent_runtime.sp_run_transition(%L, %L, %L)', l_run, 'cancelled', '{"ownerSub": "u2"}'),
        '23O01', 'owner_mismatch');
    PERFORM pg_temp.expect_error('P02_stale_attempt_finish', format(
        'CALL agent_runtime.sp_run_finish(%L, 0, %L)', l_run, l_save), '23R04', 'stale_attempt');

    CALL agent_runtime.sp_run_finish(l_run, 1, l_save);
    SELECT * INTO r FROM agent_runtime.runtime_run WHERE run_id = l_run;
    INSERT INTO _check VALUES ('P01_finish_succeeded', r.status = 'succeeded' AND r.finished_at IS NOT NULL
        AND r.lease_owner IS NULL AND r.lease_expires_at IS NULL AND r.result->>'text' = 'hello'
        AND r.usage->>'totalTokens' = '3' AND jsonb_array_length(r.node_timeline) = 1);
    INSERT INTO _check SELECT 'P01_finish_messages', array_agg(seq ORDER BY seq) = ARRAY[1, 2]::bigint[]
      FROM agent_runtime.runtime_message WHERE session_id = l_session;
    INSERT INTO _check SELECT 'P01_finish_session_state', version = 1 AND flow->>'topic' = 'x'
      FROM agent_runtime.v_session_state WHERE session_id = l_session AND owner_sub = 'u1';
    INSERT INTO _check SELECT 'P01_finish_dataset_metadata', count(*) = 1 FROM agent_runtime.v_dataset
     WHERE dataset_id = '00000000-0000-0000-0000-0000000000d1' AND chunk_no = 0 AND owner_sub = 'u1';

    CALL agent_runtime.sp_run_finish(l_run, 1, l_save);
    INSERT INTO _check SELECT 'P02_second_finish_same_save_is_noop', count(*) = 2
      FROM agent_runtime.runtime_message WHERE session_id = l_session;
    PERFORM pg_temp.expect_error('P02_second_finish_different_save', format(
        'CALL agent_runtime.sp_run_finish(%L, 1, %L)', l_run, l_save || '{"status": "failed"}'),
        '23R06', 'already_finished');
    PERFORM pg_temp.expect_error('P02_invalid_transition', format(
        'CALL agent_runtime.sp_run_transition(%L, %L, %L)', l_run, 'running', '{}'),
        '23R05', 'invalid_transition');

    -- The session is free again, so a new turn starts in it.
    CALL agent_runtime.sp_run_start(
        jsonb_build_object('ownerSub', 'u1', 'agenticWorkflowId', '00000000-0000-0000-0000-0000000000a1',
                           'mode', 'sync', 'sessionId', l_session, 'leaseOwner', 'i1', 'leaseSeconds', 30),
        '{"textPreview": "again"}', l_other, l_other_session, l_other_msg);
    INSERT INTO _check SELECT 'P01_next_turn_same_session', l_other_session = l_session AND m.seq = 3
      FROM agent_runtime.runtime_message m WHERE m.message_id = l_other_msg;
    PERFORM pg_temp.expect_error('P02_stale_attempt_transition', format(
        'CALL agent_runtime.sp_run_transition(%L, %L, %L)', l_other, 'succeeded', '{"attempt": 7}'),
        '23R04', 'stale_attempt');
END;
$$;

-- P-15 (DR-8): an async run is queued, claimed, checkpointed on drain, released with its
-- checkpoint kept, and claimed again with a new attempt that fences the old one.
DO $$
DECLARE
    l_run uuid; l_session uuid; l_msg uuid; l_runs jsonb; l_lost jsonb;
BEGIN
    CALL agent_runtime.sp_run_start(
        '{"ownerSub": "u3", "tenantId": "t3", "agenticWorkflowId": "00000000-0000-0000-0000-0000000000a1",
          "mode": "async", "status": "queued"}', NULL, l_run, l_session, l_msg);
    INSERT INTO _check SELECT 'P15_async_queued', status = 'queued' AND attempt = 0 AND lease_owner IS NULL
      FROM agent_runtime.runtime_run WHERE run_id = l_run;

    CALL agent_runtime.sp_run_claim('{"instanceId": "i1", "address": "http://10.0.0.1:9000"}', 1000, 30, l_runs);
    INSERT INTO _check SELECT 'P15_claim', EXISTS (
        SELECT 1 FROM jsonb_array_elements(l_runs) x
         WHERE x->>'runId' = l_run::text AND (x->>'attempt')::integer = 1 AND x->>'previousStatus' = 'queued');
    INSERT INTO _check SELECT 'P15_claimed_without_checkpoint', status = 'running' AND NOT has_checkpoint
        AND lease_owner = 'i1' AND lease_address = 'http://10.0.0.1:9000'
      FROM agent_runtime.v_run WHERE run_id = l_run;

    CALL agent_runtime.sp_run_heartbeat('i1', jsonb_build_array(l_run), 30, l_lost);
    INSERT INTO _check VALUES ('P15_heartbeat_renews', l_lost = '[]'::jsonb);
    CALL agent_runtime.sp_run_heartbeat('i2', jsonb_build_array(l_run), 30, l_lost);
    INSERT INTO _check VALUES ('P15_heartbeat_reports_lost', l_lost = jsonb_build_array(l_run));

    CALL agent_runtime.sp_checkpoint_save(l_run, 1,
        '{"format": "zonic-turn-v1", "langgraphVersion": "1.2.12", "lastEventSeq": 17, "namespaces": []}');
    CALL agent_runtime.sp_run_release('i1', jsonb_build_array(l_run));
    INSERT INTO _check SELECT 'P15_release_keeps_drained_checkpoint', status = 'queued' AND has_checkpoint
        AND last_event_seq = 17 AND lease_owner IS NULL AND lease_expires_at IS NULL
      FROM agent_runtime.v_run WHERE run_id = l_run;

    CALL agent_runtime.sp_run_claim('{"instanceId": "i2", "address": "http://10.0.0.2:9000"}', 1000, 30, l_runs);
    INSERT INTO _check SELECT 'P15_reclaim_sees_checkpoint', format = 'zonic-turn-v1'
        AND langgraph_version = '1.2.12' AND last_event_seq = 17 AND attempt = 1
        AND run_attempt = 2 AND run_status = 'running'
      FROM agent_runtime.v_checkpoint WHERE run_id = l_run;
    PERFORM pg_temp.expect_error('P15_old_attempt_fenced', format(
        'CALL agent_runtime.sp_checkpoint_save(%L, 1, %L)', l_run, '{}'), '23R04', 'stale_attempt');
END;
$$;

-- P-15 (DR-9) with design H-cases: a pause stores the interrupt (LangGraph's 32-hex id as a
-- uuid), the checkpoint with lastEventSeq and the turn's messages; the owner answers once;
-- the resume takes a new attempt and keeps the checkpoint until the turn finishes.
DO $$
DECLARE
    l_run uuid; l_session uuid; l_msg uuid; l_int uuid; l_again uuid;
    l_lg uuid := md5('langgraph interrupt')::uuid;
    l_interrupt jsonb;
    l_checkpoint jsonb := '{"body": {"format": "zonic-turn-v1", "langgraphVersion": "1.2.12", "lastEventSeq": 9}}';
    l_messages jsonb := '[{"role": "assistant", "kind": "agent_turn", "visibility": "internal", "textPreview": "checking"}]';
BEGIN
    CALL agent_runtime.sp_run_start(
        '{"ownerSub": "u4", "tenantId": "t4", "agenticWorkflowId": "00000000-0000-0000-0000-0000000000a1",
          "mode": "stream", "leaseOwner": "i1", "leaseSeconds": 30}', '{"textPreview": "approve?"}',
        l_run, l_session, l_msg);
    l_interrupt := jsonb_build_object('runId', l_run, 'interruptId', l_lg, 'nodeId', 'approval-1',
                                      'kind', 'approval', 'prompt', jsonb_build_object('message', 'ok?'));

    CALL agent_runtime.sp_interrupt_create(l_interrupt, l_checkpoint, l_messages, 1, l_int);
    INSERT INTO _check SELECT 'P15_pause_waiting_input', l_int = l_lg AND status = 'waiting_input'
        AND lease_owner IS NULL AND has_checkpoint AND last_event_seq = 9
        AND pending_interrupts = jsonb_build_array(l_lg)
      FROM agent_runtime.v_run WHERE run_id = l_run;
    INSERT INTO _check SELECT 'P15_pause_checkpoint_kind', kind = 'pause' AND attempt = 1
      FROM agent_runtime.v_checkpoint WHERE run_id = l_run;
    INSERT INTO _check SELECT 'P15_pause_interrupt_listed', owner_sub = 'u4' AND session_id = l_session
        AND prompt->>'message' = 'ok?'
      FROM agent_runtime.v_interrupt_pending WHERE interrupt_id = l_lg;
    CALL agent_runtime.sp_interrupt_create(l_interrupt, l_checkpoint, l_messages, 1, l_again);
    INSERT INTO _check SELECT 'P15_pause_save_idempotent', l_again = l_lg AND count(*) = 2
      FROM agent_runtime.runtime_message WHERE session_id = l_session;

    PERFORM pg_temp.expect_error('P02_owner_mismatch_answer', format(
        'CALL agent_runtime.sp_interrupt_answer(%L, %L, %L)', l_lg, 'u5', '{"approved": true}'),
        '23O01', 'owner_mismatch');
    CALL agent_runtime.sp_interrupt_answer(l_lg, 'u4', '{"approved": true}');
    INSERT INTO _check SELECT 'P15_answer_recorded', status = 'answered' AND answer->>'approved' = 'true'
        AND answered_by = 'u4'
      FROM agent_runtime.runtime_interrupt WHERE interrupt_id = l_lg;
    PERFORM pg_temp.expect_error('P02_second_answer', format(
        'CALL agent_runtime.sp_interrupt_answer(%L, %L, %L)', l_lg, 'u4', '{"approved": false}'),
        '23R08', 'already_answered');

    CALL agent_runtime.sp_run_transition(l_run, 'running',
        '{"ownerSub": "u4", "attempt": 1, "newAttempt": true, "leaseOwner": "i2", "leaseSeconds": 30}');
    INSERT INTO _check SELECT 'P15_resume_new_attempt', status = 'running' AND attempt = 2
        AND lease_owner = 'i2' AND has_checkpoint AND last_event_seq = 9
      FROM agent_runtime.v_run WHERE run_id = l_run;

    CALL agent_runtime.sp_run_finish(l_run, 2, '{"status": "succeeded", "result": {"text": "done"}}');
    INSERT INTO _check SELECT 'P15_finish_deletes_checkpoint', status = 'succeeded' AND NOT has_checkpoint
      FROM agent_runtime.v_run WHERE run_id = l_run;
END;
$$;
"""

P01 = {
    "P01_start_running",
    "P01_start_user_message",
    "P01_start_idempotency_key",
    "P01_finish_succeeded",
    "P01_finish_messages",
    "P01_finish_session_state",
    "P01_finish_dataset_metadata",
    "P01_next_turn_same_session",
}
P02 = {
    "P02_run_in_progress",
    "P02_owner_mismatch_start",
    "P02_owner_mismatch_session_update",
    "P02_owner_mismatch_transition",
    "P02_owner_mismatch_answer",
    "P02_stale_attempt_finish",
    "P02_stale_attempt_transition",
    "P02_second_finish_same_save_is_noop",
    "P02_second_finish_different_save",
    "P02_invalid_transition",
    "P02_second_answer",
}
P15 = {
    "P15_async_queued",
    "P15_claim",
    "P15_claimed_without_checkpoint",
    "P15_heartbeat_renews",
    "P15_heartbeat_reports_lost",
    "P15_release_keeps_drained_checkpoint",
    "P15_reclaim_sees_checkpoint",
    "P15_old_attempt_fenced",
    "P15_pause_waiting_input",
    "P15_pause_checkpoint_kind",
    "P15_pause_interrupt_listed",
    "P15_pause_save_idempotent",
    "P15_answer_recorded",
    "P15_resume_new_attempt",
    "P15_finish_deletes_checkpoint",
}


@pytest.fixture(scope="module")
def results(psql: Psql) -> Rows:
    return run_checks(psql, CHECKS)


def test_P15_every_procedure_check_runs(results: Rows) -> None:
    assert set(passed(results)) == P01 | P02 | P15


@pytest.mark.parametrize("name", sorted(P01))
def test_P01_run_start_and_finish(results: Rows, name: str) -> None:
    assert passed(results).get(name) is True, name


@pytest.mark.parametrize("name", sorted(P02))
def test_P02_conflicts_are_rejected(results: Rows, name: str) -> None:
    assert passed(results).get(name) is True, name


@pytest.mark.parametrize("name", sorted(P15))
def test_P15_claim_drain_pause_and_resume(results: Rows, name: str) -> None:
    assert passed(results).get(name) is True, name
