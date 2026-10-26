-- 002_functions_triggers.sql — helpers and triggers for agent_runtime.
--
-- Errors the runtime must recognise are raised with a reason token as the message and a class 23
-- SQLSTATE, so the gateway returns 409 and the runtime's error catalog (databaseReasons) maps the
-- token to an error code. owner_mismatch uses 23O01, as the builder schema does.

CREATE OR REPLACE FUNCTION agent_runtime.set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['runtime_session', 'runtime_run', 'runtime_feedback'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%1$s_updated_at ON agent_runtime.%1$I', t);
        EXECUTE format(
            'CREATE TRIGGER trg_%1$s_updated_at BEFORE UPDATE ON agent_runtime.%1$I '
            'FOR EACH ROW EXECUTE FUNCTION agent_runtime.set_updated_at()', t);
    END LOOP;
END;
$$;

-- Raises a reason token the runtime translates (see the header).
CREATE OR REPLACE FUNCTION agent_runtime.fn_raise(p_reason text, p_sqlstate text DEFAULT '23R00', p_detail text DEFAULT '')
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING MESSAGE = p_reason, ERRCODE = p_sqlstate, DETAIL = p_detail;
END;
$$;

-- Allowed run transitions; the same table as resources/run-states.json (a contract test compares them).
CREATE OR REPLACE FUNCTION agent_runtime.fn_run_transition_allowed(p_from text, p_to text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM (VALUES
            ('queued', 'running'), ('queued', 'failed'), ('queued', 'cancelled'),
            ('running', 'waiting_input'), ('running', 'succeeded'), ('running', 'failed'),
            ('running', 'cancelled'), ('running', 'timed_out'), ('running', 'queued'),
            ('waiting_input', 'running'), ('waiting_input', 'failed'),
            ('waiting_input', 'cancelled'), ('waiting_input', 'timed_out')
        ) AS t (from_status, to_status)
        WHERE t.from_status = p_from AND t.to_status = p_to
    );
$$;

CREATE OR REPLACE FUNCTION agent_runtime.fn_run_is_active(p_status text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT p_status IN ('queued', 'running', 'waiting_input');
$$;

-- Appends messages to a session, allocating seq values in order. Returns the last message ID.
CREATE OR REPLACE FUNCTION agent_runtime.fn_append_messages(p_session_id uuid, p_run_id uuid, p_messages jsonb)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
    v_count  integer := COALESCE(jsonb_array_length(p_messages), 0);
    v_first  bigint;
    v_last   uuid;
BEGIN
    IF v_count = 0 THEN
        RETURN NULL;
    END IF;
    UPDATE agent_runtime.runtime_session
       SET next_seq = next_seq + v_count, last_message_at = now()
     WHERE session_id = p_session_id
    RETURNING next_seq - v_count + 1 INTO v_first;

    WITH items AS (
        SELECT m.value AS item, m.ordinality - 1 AS idx
          FROM jsonb_array_elements(p_messages) WITH ORDINALITY AS m (value, ordinality)
    ), inserted AS (
        INSERT INTO agent_runtime.runtime_message
            (message_id, session_id, run_id, node_id, visit, seq, role, kind, visibility, thread,
             content_parts, text_preview)
        SELECT COALESCE((item->>'messageId')::uuid, gen_random_uuid()), p_session_id, p_run_id,
               item->>'nodeId', (item->>'visit')::integer, v_first + idx,
               item->>'role', item->>'kind', COALESCE(item->>'visibility', 'chat'),
               COALESCE(item->>'thread', 'messages'), COALESCE(item->'contentParts', '[]'::jsonb),
               left(COALESCE(item->>'textPreview', ''), 2000)
          FROM items
        ON CONFLICT (message_id) DO NOTHING
        RETURNING message_id, seq
    )
    SELECT message_id INTO v_last FROM inserted ORDER BY seq DESC LIMIT 1;
    RETURN v_last;
END;
$$;

INSERT INTO agent_runtime.schema_migrations (version) VALUES ('002_functions_triggers') ON CONFLICT (version) DO NOTHING;
