-- 005_procedures.sql — the write surface of agent_runtime (design Section 13.3).
--
-- Positional arguments, jsonb arguments as JSON text, INOUT parameters last (the gateway sends
-- trailing nulls for them). Each procedure is one transaction. Procedures run as their owner
-- (SECURITY DEFINER), so the runtime's gateway role needs EXECUTE and SELECT on views only.
--
-- Reason tokens (fn_raise) map to runtime error codes through resources/error-catalog.json:
--   owner_mismatch (23O01), session_not_found, run_not_found, interrupt_not_found, not_found,
--   run_in_progress, run_limit_reached, invalid_transition, stale_attempt, already_finished,
--   version_conflict, already_answered, interrupt_expired, interrupt_closed; invalid_argument (22023).
--
-- Fencing: every write a run makes after its start carries the run's attempt; an older attempt is
-- rejected with stale_attempt (design Section 9.5).

-- --------------------------------------------------------------------------
-- Sessions
-- --------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE agent_runtime.sp_session_create(p_session jsonb, INOUT o_session_id uuid DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
BEGIN
    IF COALESCE(p_session->>'ownerSub', '') = '' OR p_session->>'agenticWorkflowId' IS NULL THEN
        PERFORM fn_raise('invalid_argument', '22023', 'ownerSub and agenticWorkflowId are required');
    END IF;
    INSERT INTO runtime_session (session_id, owner_sub, owner_email, tenant_id, agentic_workflow_id, title, metadata)
    VALUES (COALESCE((p_session->>'sessionId')::uuid, gen_random_uuid()),
            p_session->>'ownerSub',
            COALESCE(p_session->>'ownerEmail', ''),
            COALESCE(p_session->>'tenantId', ''),
            (p_session->>'agenticWorkflowId')::uuid,
            left(COALESCE(p_session->>'title', ''), 200),
            COALESCE(p_session->'metadata', '{}'::jsonb))
    RETURNING session_id INTO o_session_id;
END;
$$;

CREATE OR REPLACE PROCEDURE agent_runtime.sp_session_update(p_session_id uuid, p_owner_sub text, p_patch jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
DECLARE
    v_owner text;
BEGIN
    SELECT owner_sub INTO v_owner FROM runtime_session WHERE session_id = p_session_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM fn_raise('session_not_found', '23R01');
    END IF;
    IF v_owner <> p_owner_sub THEN
        PERFORM fn_raise('owner_mismatch', '23O01');
    END IF;
    IF p_patch ? 'status' AND p_patch->>'status' NOT IN ('active', 'archived') THEN
        PERFORM fn_raise('invalid_argument', '22023', 'status');
    END IF;
    UPDATE runtime_session
       SET title  = CASE WHEN p_patch ? 'title' THEN left(p_patch->>'title', 200) ELSE title END,
           status = COALESCE(p_patch->>'status', status)
     WHERE session_id = p_session_id;
END;
$$;

-- --------------------------------------------------------------------------
-- Runs
-- --------------------------------------------------------------------------

-- One transaction at turn start: idempotency, the session if new, the active-run caps, the run
-- and the user's message (design Sections 8.2, 9.5).
CREATE OR REPLACE PROCEDURE agent_runtime.sp_run_start(
    p_run jsonb,
    p_user_message jsonb,
    INOUT o_run_id uuid DEFAULT NULL,
    INOUT o_session_id uuid DEFAULT NULL,
    INOUT o_message_id uuid DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
DECLARE
    v_owner    text := p_run->>'ownerSub';
    v_tenant   text := COALESCE(p_run->>'tenantId', '');
    v_status   text := COALESCE(p_run->>'status', 'running');
    v_limits   jsonb := COALESCE(p_run->'limits', '{}'::jsonb);
    v_session  runtime_session%ROWTYPE;
    v_existing runtime_run%ROWTYPE;
    v_count    integer;
BEGIN
    IF COALESCE(v_owner, '') = '' OR p_run->>'agenticWorkflowId' IS NULL OR p_run->>'mode' IS NULL THEN
        PERFORM fn_raise('invalid_argument', '22023', 'ownerSub, agenticWorkflowId and mode are required');
    END IF;
    IF v_status NOT IN ('running', 'queued') THEN
        PERFORM fn_raise('invalid_argument', '22023', 'status must be running or queued');
    END IF;

    -- A repeated Idempotency-Key returns the first run (case A-12) within idempotencyTtlSeconds;
    -- an older run gives its key up, so the request starts a new run.
    IF p_run->>'idempotencyKey' IS NOT NULL THEN
        SELECT * INTO v_existing FROM runtime_run
         WHERE owner_sub = v_owner AND idempotency_key = p_run->>'idempotencyKey' FOR UPDATE;
        IF FOUND AND p_run ? 'idempotencyTtlSeconds' AND v_existing.created_at
                < now() - make_interval(secs => (p_run->>'idempotencyTtlSeconds')::integer) THEN
            UPDATE runtime_run SET idempotency_key = NULL WHERE run_id = v_existing.run_id;
        ELSIF FOUND THEN
            o_run_id := v_existing.run_id;
            o_session_id := v_existing.session_id;
            SELECT message_id INTO o_message_id FROM runtime_message
             WHERE run_id = v_existing.run_id AND kind = 'user_input' ORDER BY seq LIMIT 1;
            RETURN;
        END IF;
    END IF;

    IF p_run->>'sessionId' IS NULL THEN
        INSERT INTO runtime_session (owner_sub, owner_email, tenant_id, agentic_workflow_id, title)
        VALUES (v_owner, COALESCE(p_run->>'ownerEmail', ''), v_tenant, (p_run->>'agenticWorkflowId')::uuid,
                left(COALESCE(p_run->>'title', ''), 200))
        RETURNING * INTO v_session;
    ELSE
        SELECT * INTO v_session FROM runtime_session WHERE session_id = (p_run->>'sessionId')::uuid FOR UPDATE;
        IF NOT FOUND THEN
            PERFORM fn_raise('session_not_found', '23R01');
        END IF;
        IF v_session.owner_sub <> v_owner THEN
            PERFORM fn_raise('owner_mismatch', '23O01');
        END IF;
        IF v_session.status = 'archived' THEN
            UPDATE runtime_session SET status = 'active' WHERE session_id = v_session.session_id;
        END IF;
    END IF;

    SELECT count(*) INTO v_count FROM runtime_run
     WHERE session_id = v_session.session_id AND fn_run_is_active(status);
    IF v_count >= COALESCE((v_limits->>'maxActivePerSession')::integer, 1) THEN
        PERFORM fn_raise('run_in_progress', '23R02');
    END IF;
    IF v_limits ? 'maxActivePerUser' THEN
        SELECT count(*) INTO v_count FROM runtime_run WHERE owner_sub = v_owner AND fn_run_is_active(status);
        IF v_count >= (v_limits->>'maxActivePerUser')::integer THEN
            PERFORM fn_raise('run_limit_reached', '23R03', 'user');
        END IF;
    END IF;
    IF v_limits ? 'maxActivePerTenant' THEN
        SELECT count(*) INTO v_count FROM runtime_run WHERE tenant_id = v_tenant AND fn_run_is_active(status);
        IF v_count >= (v_limits->>'maxActivePerTenant')::integer THEN
            PERFORM fn_raise('run_limit_reached', '23R03', 'tenant');
        END IF;
    END IF;

    INSERT INTO runtime_run (
        run_id, session_id, owner_sub, tenant_id, agentic_workflow_id, workflow_revision, document_version,
        status, mode, input, idempotency_key, langfuse_trace_id, started_at, deadline_at, priority, callback,
        lease_owner, lease_address, lease_expires_at, attempt)
    VALUES (
        COALESCE((p_run->>'runId')::uuid, gen_random_uuid()), v_session.session_id, v_owner, v_tenant,
        (p_run->>'agenticWorkflowId')::uuid, COALESCE(p_run->>'workflowRevision', ''),
        COALESCE(p_run->>'documentVersion', ''), v_status, p_run->>'mode', COALESCE(p_run->'input', '{}'::jsonb),
        p_run->>'idempotencyKey', p_run->>'langfuseTraceId',
        CASE WHEN v_status = 'running' THEN now() END,
        (p_run->>'deadlineAt')::timestamptz, COALESCE((p_run->>'priority')::integer, 0), p_run->'callback',
        CASE WHEN v_status = 'running' THEN p_run->>'leaseOwner' END,
        CASE WHEN v_status = 'running' THEN p_run->>'leaseAddress' END,
        CASE WHEN v_status = 'running' AND p_run ? 'leaseSeconds'
             THEN now() + make_interval(secs => (p_run->>'leaseSeconds')::integer) END,
        CASE WHEN v_status = 'running' THEN 1 ELSE 0 END)
    RETURNING run_id INTO o_run_id;
    o_session_id := v_session.session_id;

    IF p_user_message IS NOT NULL AND jsonb_typeof(p_user_message) = 'object' THEN
        o_message_id := fn_append_messages(
            v_session.session_id, o_run_id,
            jsonb_build_array(p_user_message || jsonb_build_object(
                'role', COALESCE(p_user_message->>'role', 'user'),
                'kind', COALESCE(p_user_message->>'kind', 'user_input'))));
    END IF;
END;
$$;

-- A state change checked against the transition table. p_patch may carry ownerSub (owner check),
-- attempt (fencing), result, error, and a new lease (leaseOwner, leaseAddress, leaseSeconds,
-- newAttempt) when a run starts again after a pause.
CREATE OR REPLACE PROCEDURE agent_runtime.sp_run_transition(p_run_id uuid, p_to text, p_patch jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
DECLARE
    v_run      runtime_run%ROWTYPE;
    v_patch    jsonb := COALESCE(p_patch, '{}'::jsonb);
    v_terminal boolean := p_to IN ('succeeded', 'failed', 'cancelled', 'timed_out');
BEGIN
    SELECT * INTO v_run FROM runtime_run WHERE run_id = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM fn_raise('run_not_found', '23R01');
    END IF;
    IF v_patch ? 'ownerSub' AND v_run.owner_sub <> v_patch->>'ownerSub' THEN
        PERFORM fn_raise('owner_mismatch', '23O01');
    END IF;
    IF v_patch ? 'attempt' AND v_run.attempt <> (v_patch->>'attempt')::integer THEN
        PERFORM fn_raise('stale_attempt', '23R04');
    END IF;
    IF v_run.status = p_to THEN
        RETURN;
    END IF;
    IF NOT fn_run_transition_allowed(v_run.status, p_to) THEN
        PERFORM fn_raise('invalid_transition', '23R05', v_run.status || ' -> ' || p_to);
    END IF;

    UPDATE runtime_run
       SET status           = p_to,
           result           = COALESCE(v_patch->'result', result),
           error            = COALESCE(v_patch->'error', error),
           started_at       = CASE WHEN p_to = 'running' THEN COALESCE(started_at, now()) ELSE started_at END,
           queued_at        = CASE WHEN p_to = 'queued' THEN now() ELSE queued_at END,
           finished_at      = CASE WHEN v_terminal THEN now() ELSE finished_at END,
           attempt          = attempt + CASE WHEN (v_patch->>'newAttempt')::boolean THEN 1 ELSE 0 END,
           lease_owner      = CASE WHEN v_terminal OR p_to IN ('queued', 'waiting_input') THEN NULL
                                   WHEN v_patch ? 'leaseOwner' THEN v_patch->>'leaseOwner' ELSE lease_owner END,
           lease_address    = CASE WHEN v_terminal OR p_to IN ('queued', 'waiting_input') THEN NULL
                                   WHEN v_patch ? 'leaseAddress' THEN v_patch->>'leaseAddress' ELSE lease_address END,
           lease_expires_at = CASE WHEN v_terminal OR p_to IN ('queued', 'waiting_input') THEN NULL
                                   WHEN v_patch ? 'leaseSeconds'
                                   THEN now() + make_interval(secs => (v_patch->>'leaseSeconds')::integer)
                                   ELSE lease_expires_at END
     WHERE run_id = p_run_id;

    IF v_terminal THEN
        UPDATE runtime_interrupt SET status = 'cancelled' WHERE run_id = p_run_id AND status = 'pending';
        DELETE FROM runtime_checkpoint WHERE run_id = p_run_id;
    END IF;
END;
$$;

-- The turn-end save (TurnSave, design Section 8.2): final status, result, usage, node timeline,
-- the turn's messages, session variables (version-checked) and dataset metadata, in one
-- transaction. Deletes the checkpoint and clears the lease. Idempotent per (run, attempt): the same
-- save again is a no-op; a different save for a finished run is rejected (case P-05).
CREATE OR REPLACE PROCEDURE agent_runtime.sp_run_finish(p_run_id uuid, p_attempt integer, p_save jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
DECLARE
    v_run      runtime_run%ROWTYPE;
    v_hash     text := md5(p_save::text);
    v_status   text := p_save->>'status';
    v_state    jsonb := p_save->'sessionState';
    v_expected integer;
    v_rows     integer;
BEGIN
    SELECT * INTO v_run FROM runtime_run WHERE run_id = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM fn_raise('run_not_found', '23R01');
    END IF;
    IF v_run.attempt <> p_attempt THEN
        PERFORM fn_raise('stale_attempt', '23R04');
    END IF;
    IF NOT fn_run_is_active(v_run.status) THEN
        IF v_run.finish_hash = v_hash THEN
            RETURN;
        END IF;
        PERFORM fn_raise('already_finished', '23R06');
    END IF;
    IF v_status IS NULL OR v_status NOT IN ('succeeded', 'failed', 'cancelled', 'timed_out') THEN
        PERFORM fn_raise('invalid_argument', '22023', 'save.status must be terminal');
    END IF;
    IF NOT fn_run_transition_allowed(v_run.status, v_status) THEN
        PERFORM fn_raise('invalid_transition', '23R05', v_run.status || ' -> ' || v_status);
    END IF;

    IF v_state IS NOT NULL AND jsonb_typeof(v_state) = 'object' THEN
        v_expected := (v_state->>'expectedVersion')::integer;
        INSERT INTO runtime_session_state AS st (session_id, agentic_workflow_id, flow, version)
        VALUES (v_run.session_id, v_run.agentic_workflow_id, COALESCE(v_state->'flow', '{}'::jsonb), 1)
        ON CONFLICT (session_id, agentic_workflow_id) DO UPDATE
           SET flow = EXCLUDED.flow, version = st.version + 1, updated_at = now()
         WHERE v_expected IS NULL OR st.version = v_expected;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows = 0 THEN
            PERFORM fn_raise('version_conflict', '23R07');
        END IF;
    END IF;

    PERFORM fn_append_messages(v_run.session_id, p_run_id, COALESCE(p_save->'messages', '[]'::jsonb));

    INSERT INTO runtime_dataset (dataset_id, chunk_no, run_id, session_id, node_id, visit, owner_sub, store,
                                 cache_key, kind, content_type, row_count, columns, size_bytes, sha256, expires_at)
    SELECT (d->>'datasetId')::uuid, 0, p_run_id, v_run.session_id, COALESCE(d->>'nodeId', ''),
           COALESCE((d->>'visit')::integer, 1), v_run.owner_sub, d->>'store', d->>'cacheKey', d->>'kind',
           COALESCE(d->>'contentType', 'application/json'), (d->>'rowCount')::bigint, d->'columns',
           COALESCE((d->>'sizeBytes')::bigint, 0), COALESCE(d->>'sha256', ''), (d->>'expiresAt')::timestamptz
      FROM jsonb_array_elements(COALESCE(p_save->'datasets', '[]'::jsonb)) AS d
    ON CONFLICT (dataset_id, chunk_no) DO NOTHING;

    UPDATE runtime_run
       SET status            = v_status,
           result            = p_save->'result',
           error             = p_save->'error',
           usage             = COALESCE(p_save->'usage', usage),
           node_timeline     = COALESCE(p_save->'nodeTimeline', node_timeline),
           langfuse_trace_id = COALESCE(p_save->>'langfuseTraceId', langfuse_trace_id),
           finished_at       = now(),
           lease_owner       = NULL,
           lease_address     = NULL,
           lease_expires_at  = NULL,
           finish_hash       = v_hash
     WHERE run_id = p_run_id;

    UPDATE runtime_interrupt SET status = 'cancelled' WHERE run_id = p_run_id AND status = 'pending';
    DELETE FROM runtime_checkpoint WHERE run_id = p_run_id;
END;
$$;

-- --------------------------------------------------------------------------
-- Interrupts (design Section 8.3)
-- --------------------------------------------------------------------------

-- The pause save: the interrupt(s), the checkpoint row, the turn's messages, the move to
-- waiting_input. p_interrupt is one interrupt or {runId, interrupts: [...]} for parallel pauses.
-- Idempotent per (run, attempt, interrupt).
CREATE OR REPLACE PROCEDURE agent_runtime.sp_interrupt_create(
    p_interrupt jsonb,
    p_checkpoint jsonb,
    p_messages jsonb,
    p_attempt integer,
    INOUT o_interrupt_id uuid DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
DECLARE
    v_items  jsonb := CASE WHEN p_interrupt ? 'interrupts' THEN p_interrupt->'interrupts'
                           ELSE jsonb_build_array(p_interrupt) END;
    v_run_id uuid := COALESCE((p_interrupt->>'runId')::uuid, (v_items->0->>'runId')::uuid);
    v_run    runtime_run%ROWTYPE;
    v_first  uuid := (v_items->0->>'interruptId')::uuid;
BEGIN
    IF v_run_id IS NULL OR jsonb_array_length(v_items) = 0 THEN
        PERFORM fn_raise('invalid_argument', '22023', 'runId and at least one interrupt are required');
    END IF;
    SELECT * INTO v_run FROM runtime_run WHERE run_id = v_run_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM fn_raise('run_not_found', '23R01');
    END IF;
    IF v_run.attempt <> p_attempt THEN
        PERFORM fn_raise('stale_attempt', '23R04');
    END IF;
    IF v_first IS NOT NULL AND EXISTS (SELECT 1 FROM runtime_interrupt WHERE interrupt_id = v_first) THEN
        o_interrupt_id := v_first;
        RETURN;
    END IF;
    IF NOT fn_run_transition_allowed(v_run.status, 'waiting_input') THEN
        PERFORM fn_raise('invalid_transition', '23R05', v_run.status || ' -> waiting_input');
    END IF;

    WITH inserted AS (
        INSERT INTO runtime_interrupt (interrupt_id, run_id, node_id, kind, prompt, answer_schema, ui_schema,
                                       attempt, expires_at)
        SELECT COALESCE((i.value->>'interruptId')::uuid, gen_random_uuid()), v_run_id, i.value->>'nodeId',
               i.value->>'kind', COALESCE(i.value->'prompt', '{}'::jsonb),
               COALESCE(i.value->'answerSchema', '{}'::jsonb), COALESCE(i.value->'uiSchema', '{}'::jsonb),
               p_attempt, (i.value->>'expiresAt')::timestamptz
          FROM jsonb_array_elements(v_items) WITH ORDINALITY AS i (value, ordinality)
         ORDER BY i.ordinality
        RETURNING interrupt_id, requested_at
    )
    SELECT interrupt_id INTO o_interrupt_id FROM inserted LIMIT 1;
    IF v_first IS NOT NULL THEN
        o_interrupt_id := v_first;
    END IF;

    INSERT INTO runtime_checkpoint (run_id, attempt, kind, body, expires_at)
    VALUES (v_run_id, p_attempt, 'pause', COALESCE(p_checkpoint->'body', p_checkpoint),
            (p_checkpoint->>'expiresAt')::timestamptz)
    ON CONFLICT (run_id) DO UPDATE
       SET attempt = EXCLUDED.attempt, kind = 'pause', body = EXCLUDED.body,
           created_at = now(), expires_at = EXCLUDED.expires_at;

    PERFORM fn_append_messages(v_run.session_id, v_run_id, COALESCE(p_messages, '[]'::jsonb));

    UPDATE runtime_run
       SET status = 'waiting_input', lease_owner = NULL, lease_address = NULL, lease_expires_at = NULL
     WHERE run_id = v_run_id;
END;
$$;

-- An answer, once, by the run's owner, before expiry (cases H-02 to H-04).
CREATE OR REPLACE PROCEDURE agent_runtime.sp_interrupt_answer(p_interrupt_id uuid, p_owner_sub text, p_answer jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
DECLARE
    v_status  text;
    v_expires timestamptz;
    v_owner   text;
BEGIN
    SELECT i.status, i.expires_at, r.owner_sub INTO v_status, v_expires, v_owner
      FROM runtime_interrupt i JOIN runtime_run r ON r.run_id = i.run_id
     WHERE i.interrupt_id = p_interrupt_id
       FOR UPDATE OF i;
    IF NOT FOUND THEN
        PERFORM fn_raise('interrupt_not_found', '23R01');
    END IF;
    IF v_owner <> p_owner_sub THEN
        PERFORM fn_raise('owner_mismatch', '23O01');
    END IF;
    IF v_status = 'answered' THEN
        PERFORM fn_raise('already_answered', '23R08');
    ELSIF v_status = 'expired' OR (v_expires IS NOT NULL AND v_expires < now()) THEN
        PERFORM fn_raise('interrupt_expired', '23R09');
    ELSIF v_status <> 'pending' THEN
        PERFORM fn_raise('interrupt_closed', '23R09');
    END IF;
    UPDATE runtime_interrupt
       SET status = 'answered', answer = p_answer, answered_at = now(), answered_by = p_owner_sub
     WHERE interrupt_id = p_interrupt_id;
END;
$$;

-- An expired interrupt answered by the expiry job (onExpiry: reject, case H-06): only while it is
-- still pending and past its expiry, so a racing instance or a late answer leaves it unchanged.
CREATE OR REPLACE PROCEDURE agent_runtime.sp_interrupt_expire(
    p_interrupt_id uuid, p_answer jsonb, INOUT o_expired boolean DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
BEGIN
    UPDATE runtime_interrupt
       SET status = 'answered', answer = p_answer, answered_at = now(), answered_by = 'system:expiry'
     WHERE interrupt_id = p_interrupt_id AND status = 'pending'
       AND expires_at IS NOT NULL AND expires_at < now();
    o_expired := FOUND;
END;
$$;

-- --------------------------------------------------------------------------
-- Feedback, datasets, checkpoints
-- --------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE agent_runtime.sp_feedback_save(p_feedback jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
DECLARE
    v_owner text;
BEGIN
    SELECT s.owner_sub INTO v_owner
      FROM runtime_message m JOIN runtime_session s ON s.session_id = m.session_id
     WHERE m.message_id = (p_feedback->>'messageId')::uuid;
    IF NOT FOUND THEN
        PERFORM fn_raise('not_found', '23R01');
    END IF;
    IF v_owner <> p_feedback->>'ownerSub' THEN
        PERFORM fn_raise('owner_mismatch', '23O01');
    END IF;
    INSERT INTO runtime_feedback AS f (message_id, owner_sub, rating, comment)
    VALUES ((p_feedback->>'messageId')::uuid, p_feedback->>'ownerSub', (p_feedback->>'rating')::smallint,
            COALESCE(p_feedback->>'comment', ''))
    ON CONFLICT (message_id, owner_sub) DO UPDATE
       SET rating = EXCLUDED.rating, comment = EXCLUDED.comment;
END;
$$;

-- Body chunks of database-store datasets: the first items of a transactional turn-end batch.
CREATE OR REPLACE PROCEDURE agent_runtime.sp_dataset_save(p_run_id uuid, p_attempt integer, p_chunks jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
DECLARE
    v_run runtime_run%ROWTYPE;
BEGIN
    SELECT * INTO v_run FROM runtime_run WHERE run_id = p_run_id FOR SHARE;
    IF NOT FOUND THEN
        PERFORM fn_raise('run_not_found', '23R01');
    END IF;
    IF v_run.attempt <> p_attempt THEN
        PERFORM fn_raise('stale_attempt', '23R04');
    END IF;
    INSERT INTO runtime_dataset (dataset_id, chunk_no, run_id, session_id, node_id, visit, owner_sub, store,
                                 kind, content_type, body, expires_at)
    SELECT (c->>'datasetId')::uuid, (c->>'chunkNo')::integer, p_run_id, v_run.session_id,
           COALESCE(c->>'nodeId', ''), COALESCE((c->>'visit')::integer, 1), v_run.owner_sub, 'database',
           COALESCE(c->>'kind', 'rows'), COALESCE(c->>'contentType', 'application/json'), c->'body',
           (c->>'expiresAt')::timestamptz
      FROM jsonb_array_elements(COALESCE(p_chunks, '[]'::jsonb)) AS c
     WHERE (c->>'chunkNo')::integer >= 1
    ON CONFLICT (dataset_id, chunk_no) DO UPDATE SET body = EXCLUDED.body;
END;
$$;

-- Optional background checkpoint of a long async turn; latest wins (design Section 8.2).
CREATE OR REPLACE PROCEDURE agent_runtime.sp_checkpoint_save(p_run_id uuid, p_attempt integer, p_body jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
DECLARE
    v_run runtime_run%ROWTYPE;
BEGIN
    SELECT * INTO v_run FROM runtime_run WHERE run_id = p_run_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM fn_raise('run_not_found', '23R01');
    END IF;
    IF v_run.attempt <> p_attempt THEN
        PERFORM fn_raise('stale_attempt', '23R04');
    END IF;
    IF v_run.status <> 'running' THEN
        PERFORM fn_raise('invalid_transition', '23R05', 'checkpoint of a run that is not running');
    END IF;
    INSERT INTO runtime_checkpoint (run_id, attempt, kind, body)
    VALUES (p_run_id, p_attempt, 'async', p_body)
    ON CONFLICT (run_id) DO UPDATE SET attempt = EXCLUDED.attempt, kind = 'async', body = EXCLUDED.body,
                                       created_at = now();
END;
$$;

-- --------------------------------------------------------------------------
-- The durable queue: claims, leases, release (design Section 9.5)
-- --------------------------------------------------------------------------

-- Claims up to p_limit runs: queued runs, the oldest per tenant first (round-robin across
-- tenants), then by priority; and running runs whose lease expired. Each claim increments attempt.
CREATE OR REPLACE PROCEDURE agent_runtime.sp_run_claim(
    p_instance jsonb, p_limit integer, p_lease_seconds integer, INOUT o_runs jsonb DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
BEGIN
    WITH ranked AS (
        SELECT run_id,
               row_number() OVER (PARTITION BY tenant_id ORDER BY priority DESC, queued_at) AS tenant_rank,
               priority, queued_at
          FROM runtime_run
         WHERE status = 'queued' OR (status = 'running' AND lease_expires_at < now())
    ), picked AS (
        SELECT r.run_id, r.status AS previous_status, k.tenant_rank, k.priority, k.queued_at
          FROM runtime_run r JOIN ranked k ON k.run_id = r.run_id
         WHERE r.status = 'queued' OR (r.status = 'running' AND r.lease_expires_at < now())
         ORDER BY k.tenant_rank, k.priority DESC, k.queued_at
         LIMIT p_limit
           FOR UPDATE OF r SKIP LOCKED
    ), claimed AS (
        UPDATE runtime_run r
           SET status = 'running',
               lease_owner = p_instance->>'instanceId',
               lease_address = p_instance->>'address',
               lease_expires_at = now() + make_interval(secs => p_lease_seconds),
               attempt = r.attempt + 1,
               started_at = COALESCE(r.started_at, now())
          FROM picked
         WHERE r.run_id = picked.run_id
        RETURNING r.run_id, r.attempt, r.mode, r.session_id, picked.previous_status, picked.tenant_rank,
                  picked.priority, picked.queued_at
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
               'runId', run_id, 'attempt', attempt, 'mode', mode, 'sessionId', session_id,
               'previousStatus', previous_status)
             ORDER BY tenant_rank, priority DESC, queued_at), '[]'::jsonb)
      INTO o_runs
      FROM claimed;
END;
$$;

-- Extends the leases the instance still owns; returns the runs it no longer owns.
CREATE OR REPLACE PROCEDURE agent_runtime.sp_run_heartbeat(
    p_instance_id text, p_run_ids jsonb, p_lease_seconds integer, INOUT o_lost jsonb DEFAULT NULL)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
BEGIN
    WITH ids AS (
        SELECT value::uuid AS run_id FROM jsonb_array_elements_text(COALESCE(p_run_ids, '[]'::jsonb))
    ), renewed AS (
        UPDATE runtime_run r
           SET lease_expires_at = now() + make_interval(secs => p_lease_seconds)
          FROM ids
         WHERE r.run_id = ids.run_id AND r.lease_owner = p_instance_id AND r.status = 'running'
        RETURNING r.run_id
    )
    SELECT COALESCE(jsonb_agg(ids.run_id), '[]'::jsonb) INTO o_lost
      FROM ids
     WHERE ids.run_id NOT IN (SELECT run_id FROM renewed);
END;
$$;

-- On drain: async runs back to the queue (their turn restarts elsewhere), the others failed with
-- SHUTDOWN and a retry action (design Section 9.5, step 6).
CREATE OR REPLACE PROCEDURE agent_runtime.sp_run_release(p_instance_id text, p_run_ids jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
BEGIN
    UPDATE runtime_run
       SET status = 'queued', lease_owner = NULL, lease_address = NULL, lease_expires_at = NULL, queued_at = now()
     WHERE run_id IN (SELECT value::uuid FROM jsonb_array_elements_text(COALESCE(p_run_ids, '[]'::jsonb)))
       AND lease_owner = p_instance_id AND status = 'running' AND mode = 'async';

    UPDATE runtime_run
       SET status = 'failed', error = jsonb_build_object('code', 'SHUTDOWN', 'retryable', true),
           finished_at = now(), lease_owner = NULL, lease_address = NULL, lease_expires_at = NULL
     WHERE run_id IN (SELECT value::uuid FROM jsonb_array_elements_text(COALESCE(p_run_ids, '[]'::jsonb)))
       AND lease_owner = p_instance_id AND status = 'running' AND mode <> 'async';
END;
$$;

-- Retention: expired checkpoints (runs that waited past their interrupt expiry) and dataset rows;
-- sessions idle since p_before are archived. Runs and messages are kept (case P-09).
CREATE OR REPLACE PROCEDURE agent_runtime.sp_retention_purge(p_before timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = agent_runtime, pg_temp AS $$
BEGIN
    DELETE FROM runtime_checkpoint WHERE expires_at IS NOT NULL AND expires_at < now();
    DELETE FROM runtime_dataset WHERE expires_at IS NOT NULL AND expires_at < now();
    UPDATE runtime_session SET status = 'archived' WHERE status = 'active' AND last_message_at < p_before;
END;
$$;

INSERT INTO agent_runtime.schema_migrations (version) VALUES ('005_procedures') ON CONFLICT (version) DO NOTHING;
