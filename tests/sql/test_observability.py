"""agent_observability on PostgreSQL (P0-09): cases O-02, O-03, O-04 and O-07.

The schema (001 to 005, vendored from ZonicAgentRuntimeServer_Nodejs) is applied twice
(idempotency); the procedures are called the way the gateway exporter will call them.
Trace IDs are 32 hexadecimal characters and observation IDs 16, as OpenTelemetry and
Langfuse use them. Everything runs in one transaction that is rolled back.
"""

from __future__ import annotations

import pytest

from tests.sql.conftest import Psql, Rows, observability_schema, passed, run_checks

pytestmark = pytest.mark.sql

T1 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"
T2 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2"
T3 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa3"
T4 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa4"
T5 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa5"
T9 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa9"
WF = "00000000-0000-0000-0000-0000000000a1"

CHECKS = (
    r"""
SET LOCAL TIME ZONE 'UTC';

INSERT INTO _check SELECT 'tables', count(*) = 5 FROM pg_tables
 WHERE schemaname = 'agent_observability' AND tablename IN
       ('obs_trace', 'obs_observation', 'obs_score', 'obs_model_price', 'obs_partition');
INSERT INTO _check SELECT 'views', count(*) = 6 FROM information_schema.views
 WHERE table_schema = 'agent_observability';
INSERT INTO _check SELECT 'materialized_views', count(*) = 2 FROM pg_matviews
 WHERE schemaname = 'agent_observability';
INSERT INTO _check SELECT 'procedures', count(*) = 6 FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'agent_observability' AND p.prokind = 'p';

-- O-02: one batch of a trace, three observations and a score, then the same batch again.
DO $$
DECLARE
    t integer; o integer; s integer;
    l_traces jsonb := '[{"traceId": "@T1", "runId": "@T1", "name": "wf", "userId": "u1", "tenantId": "t1",
                          "agenticWorkflowId": "@WF", "tags": ["api"], "status": "succeeded",
                          "startTime": "2026-10-10T10:00:00Z", "endTime": "2026-10-10T10:00:05Z"}]';
    l_observations jsonb := '[
        {"observationId": "b000000000000001", "traceId": "@T1", "type": "agent", "name": "Agent",
         "startTime": "2026-10-10T10:00:01Z", "endTime": "2026-10-10T10:00:04Z",
         "nodeId": "agent-1", "nodeType": "agent", "nodeName": "Agent", "visit": 1},
        {"observationId": "b000000000000002", "traceId": "@T1", "parentObservationId": "b000000000000001",
         "type": "generation", "name": "ChatBedrockConverse", "model": "anthropic.claude-sonnet",
         "usageDetails": {"input": 100, "output": 20}, "totalCost": 0.5,
         "startTime": "2026-10-10T10:00:02Z", "endTime": "2026-10-10T10:00:03Z"},
        {"observationId": "b000000000000003", "traceId": "@T1", "type": "event", "name": "hook failed",
         "level": "ERROR", "startTime": "2026-10-10T10:00:03Z"}]';
    l_scores jsonb := '[{"scoreId": "00000000-0000-0000-0000-0000000005c1", "traceId": "@T1",
                         "name": "user_feedback", "dataType": "NUMERIC", "value": 1}]';
BEGIN
    CALL agent_observability.sp_obs_ingest(l_traces, l_observations, l_scores, t, o, s);
    INSERT INTO _check VALUES ('O02_counts_returned', t = 1 AND o = 3 AND s = 1);
    INSERT INTO _check SELECT 'O02_trace_totals', observation_count = 3 AND error_count = 1
        AND input_tokens = 100 AND output_tokens = 20 AND total_cost = 0.5 AND latency_ms = 5000
      FROM agent_observability.v_obs_trace WHERE trace_id = '@T1';
    INSERT INTO _check SELECT 'O02_observation_shape', latency_ms = 1000 AND user_id = 'u1'
        AND parent_observation_id = 'b000000000000001' AND type = 'generation'
      FROM agent_observability.v_obs_observation WHERE observation_id = 'b000000000000002';

    CALL agent_observability.sp_obs_ingest(l_traces, l_observations, l_scores, t, o, s);
    INSERT INTO _check SELECT 'O02_same_batch_twice_no_duplicates',
        (SELECT count(*) FROM agent_observability.v_obs_observation WHERE trace_id = '@T1') = 3
        AND (SELECT count(*) FROM agent_observability.v_obs_score WHERE trace_id = '@T1') = 1
        AND (SELECT observation_count = 3 AND total_cost = 0.5 AND tags = '{api}'
               FROM agent_observability.obs_trace WHERE trace_id = '@T1');

    -- One transaction: a batch with an invalid observation leaves nothing behind.
    PERFORM pg_temp.expect_error('O02_invalid_batch_rejected', format(
        'CALL agent_observability.sp_obs_ingest(%L, %L, NULL, NULL, NULL, NULL)',
        '[{"traceId": "@T9", "startTime": "2026-10-10T10:00:00Z"}]',
        '[{"observationId": "c000000000000001", "traceId": "@T9", "type": "bogus", "name": "x",
           "startTime": "2026-10-10T10:00:00Z"}]'), '23514', NULL);
    INSERT INTO _check SELECT 'O02_invalid_batch_leaves_nothing', count(*) = 0
      FROM agent_observability.obs_trace WHERE trace_id = '@T9';
END;
$$;

-- O-03: observations and a score before their trace header; the header across two batches.
DO $$
DECLARE
    t integer; o integer; s integer;
BEGIN
    CALL agent_observability.sp_obs_ingest(NULL,
        '[{"observationId": "b000000000000011", "traceId": "@T2", "type": "span", "name": "Start",
           "startTime": "2026-10-11T09:00:00Z", "endTime": "2026-10-11T09:00:01Z"}]',
        '[{"scoreId": "00000000-0000-0000-0000-0000000005c2", "traceId": "@T2", "name": "eval",
           "dataType": "BOOLEAN", "value": 1, "source": "EVAL"}]', t, o, s);
    INSERT INTO _check SELECT 'O03_minimal_trace_row', name = '' AND run_id = '@T2'::uuid
        AND start_time = '2026-10-11T09:00:00Z' AND observation_count = 1 AND user_id IS NULL
      FROM agent_observability.obs_trace WHERE trace_id = '@T2';

    CALL agent_observability.sp_obs_ingest(
        '[{"traceId": "@T2", "name": "wf", "userId": "u1", "tags": ["api"], "metadata": {"a": 1},
           "startTime": "2026-10-11T09:00:05Z"}]', NULL, NULL, t, o, s);
    CALL agent_observability.sp_obs_ingest(
        '[{"traceId": "@T2", "tags": ["qi-ui", "api"], "metadata": {"b": 2}, "status": "succeeded",
           "startTime": "2026-10-11T09:00:05Z", "endTime": "2026-10-11T09:01:00Z"}]',
        '[{"observationId": "b000000000000012", "traceId": "@T2", "type": "tool", "name": "search",
           "level": "ERROR", "startTime": "2026-10-11T09:00:30Z", "endTime": "2026-10-11T09:00:31Z"}]',
        NULL, t, o, s);
    INSERT INTO _check SELECT 'O03_header_completes_trace', name = 'wf' AND user_id = 'u1'
        AND status = 'succeeded' AND end_time = '2026-10-11T09:01:00Z'
      FROM agent_observability.obs_trace WHERE trace_id = '@T2';
    INSERT INTO _check SELECT 'O03_tags_unioned_metadata_merged', tags = '{api,qi-ui}'
        AND metadata = '{"a": 1, "b": 2}'::jsonb
      FROM agent_observability.obs_trace WHERE trace_id = '@T2';
    INSERT INTO _check SELECT 'O03_earliest_start_kept', start_time = '2026-10-11T09:00:00Z'
      FROM agent_observability.obs_trace WHERE trace_id = '@T2';
    INSERT INTO _check SELECT 'O03_totals_recomputed', observation_count = 2 AND error_count = 1
      FROM agent_observability.obs_trace WHERE trace_id = '@T2';
    INSERT INTO _check SELECT 'O03_score_kept', count(*) = 1 AND bool_and(user_id = 'u1')
      FROM agent_observability.v_obs_score WHERE trace_id = '@T2';
END;
$$;

-- O-04: cost from obs_model_price with a price change on the 15th; the longest matching
-- pattern wins; a cost the exporter supplied is kept.
DO $$
DECLARE
    n integer; t integer; o integer; s integer;
BEGIN
    CALL agent_observability.sp_obs_model_price_upsert('[
        {"modelPattern": "^anthropic[.]claude", "usageKey": "input", "pricePerUnit": 0.000003,
         "validFrom": "2026-10-01T00:00:00Z"},
        {"modelPattern": "^anthropic[.]claude", "usageKey": "output", "pricePerUnit": 0.000015,
         "validFrom": "2026-10-01T00:00:00Z"},
        {"modelPattern": "^anthropic[.]claude", "usageKey": "input", "pricePerUnit": 0.000004,
         "validFrom": "2026-10-15T00:00:00Z"},
        {"modelPattern": "^anthropic[.]claude-opus", "usageKey": "input", "pricePerUnit": 0.00001,
         "validFrom": "2026-10-01T00:00:00Z"}]', n);
    INSERT INTO _check VALUES ('O04_prices_upserted', n = 4);
    INSERT INTO _check SELECT 'O04_old_price_closed', valid_to = '2026-10-15T00:00:00Z'
      FROM agent_observability.obs_model_price
     WHERE model_pattern = '^anthropic[.]claude' AND usage_key = 'input' AND valid_from = '2026-10-01T00:00:00Z';

    CALL agent_observability.sp_obs_ingest(
        '[{"traceId": "@T3", "name": "wf", "startTime": "2026-10-10T00:00:00Z"}]',
        '[{"observationId": "b000000000000021", "traceId": "@T3", "type": "generation", "name": "m",
           "model": "anthropic.claude-sonnet", "usageDetails": {"input": 1000, "output": 100},
           "startTime": "2026-10-10T12:00:00Z"},
          {"observationId": "b000000000000022", "traceId": "@T3", "type": "generation", "name": "m",
           "model": "anthropic.claude-sonnet", "usageDetails": {"input": 1000, "output": 100},
           "startTime": "2026-10-20T12:00:00Z"},
          {"observationId": "b000000000000023", "traceId": "@T3", "type": "generation", "name": "m",
           "model": "anthropic.claude-opus", "usageDetails": {"input": 1000, "output": 100},
           "startTime": "2026-10-20T12:00:00Z"},
          {"observationId": "b000000000000024", "traceId": "@T3", "type": "generation", "name": "m",
           "model": "anthropic.claude-sonnet", "usageDetails": {"input": 1000}, "totalCost": 0.9,
           "startTime": "2026-10-20T12:00:00Z"}]', NULL, t, o, s);
    INSERT INTO _check SELECT 'O04_price_before_change', total_cost = 0.0045
        AND cost_details = '{"input": 0.003, "output": 0.0015}'::jsonb
      FROM agent_observability.obs_observation WHERE observation_id = 'b000000000000021';
    INSERT INTO _check SELECT 'O04_price_after_change', total_cost = 0.0055
      FROM agent_observability.obs_observation WHERE observation_id = 'b000000000000022';
    INSERT INTO _check SELECT 'O04_most_specific_pattern', total_cost = 0.0115
      FROM agent_observability.obs_observation WHERE observation_id = 'b000000000000023';
    INSERT INTO _check SELECT 'O04_exporter_cost_kept', total_cost = 0.9
      FROM agent_observability.obs_observation WHERE observation_id = 'b000000000000024';
    INSERT INTO _check SELECT 'O04_trace_cost', total_cost = 0.9215 AND input_tokens = 4000
      FROM agent_observability.obs_trace WHERE trace_id = '@T3';
END;
$$;

-- O-07: partitions ahead and on demand, rollups equal to raw rows, retention by partition.
DO $$
DECLARE
    n integer; t integer; o integer; s integer; dropped integer; deleted integer;
    i integer;
    l_raw record;
BEGIN
    CALL agent_observability.sp_obs_partition_ensure(2, n);
    INSERT INTO _check SELECT 'O07_partitions_ahead', count(*) = 3 FROM generate_series(0, 2) AS m (i)
      JOIN agent_observability.obs_partition p
        ON p.range_start = date_trunc('month', now() + make_interval(months => m.i))
     WHERE to_regclass('agent_observability.' || p.partition_name) IS NOT NULL;

    CALL agent_observability.sp_obs_ingest(
        '[{"traceId": "@T4", "name": "old", "startTime": "2026-01-15T00:00:00Z"}]',
        '[{"observationId": "b000000000000031", "traceId": "@T4", "type": "span", "name": "old",
           "startTime": "2026-01-15T00:00:00Z"}]', NULL, t, o, s);
    INSERT INTO _check SELECT 'O07_partition_on_demand',
        to_regclass('agent_observability.obs_observation_y2026m01') IS NOT NULL;

    CALL agent_observability.sp_obs_ingest(
        '[{"traceId": "@T5", "name": "wf", "agenticWorkflowId": "@WF", "tenantId": "t5",
           "startTime": "2026-10-20T08:00:00Z"}]',
        '[{"observationId": "b000000000000041", "traceId": "@T5", "type": "agent", "name": "Agent",
           "nodeId": "agent-1", "nodeType": "agent", "nodeName": "Agent", "visit": 1,
           "startTime": "2026-10-20T08:00:00Z", "endTime": "2026-10-20T08:00:01Z"},
          {"observationId": "b000000000000042", "traceId": "@T5", "type": "agent", "name": "Agent",
           "nodeId": "agent-1", "nodeType": "agent", "nodeName": "Agent", "visit": 2,
           "startTime": "2026-10-20T08:00:02Z", "endTime": "2026-10-20T08:00:05Z"},
          {"observationId": "b000000000000043", "traceId": "@T5", "type": "agent", "name": "Agent",
           "nodeId": "agent-1", "nodeType": "agent", "nodeName": "Agent", "visit": 3, "level": "ERROR",
           "startTime": "2026-10-20T08:00:06Z", "endTime": "2026-10-20T08:00:08Z"},
          {"observationId": "b000000000000044", "traceId": "@T5", "type": "generation", "name": "m",
           "model": "anthropic.claude-opus", "usageDetails": {"input": 10, "output": 5}, "totalCost": 0.01,
           "startTime": "2026-10-20T08:00:03Z"}]', NULL, t, o, s);

    CALL agent_observability.sp_obs_refresh_rollups(n);
    SELECT count(*) AS executions, count(*) FILTER (WHERE level = 'ERROR') AS errors,
           round(avg(latency_ms)::numeric, 1) AS avg_latency_ms
      INTO l_raw
      FROM agent_observability.v_obs_observation
     WHERE trace_id = '@T5' AND node_id = 'agent-1';
    INSERT INTO _check SELECT 'O07_node_rollup_matches_raw', n = 2 AND executions = l_raw.executions
        AND errors = l_raw.errors AND avg_latency_ms = l_raw.avg_latency_ms AND error_rate = 0.3333
      FROM agent_observability.v_obs_node_stats_daily
     WHERE day = '2026-10-20' AND agentic_workflow_id = '@WF' AND node_name = 'Agent';
    INSERT INTO _check SELECT 'O07_model_rollup_matches_raw', calls = 1 AND input_tokens = 10
        AND output_tokens = 5 AND total_cost = 0.01
      FROM agent_observability.v_obs_model_usage_daily
     WHERE day = '2026-10-20' AND model = 'anthropic.claude-opus' AND tenant_id = 't5';

    CALL agent_observability.sp_obs_retention_purge('2026-02-01T00:00:00Z', dropped, deleted);
    INSERT INTO _check SELECT 'O07_retention_drops_partition', dropped >= 1 AND deleted >= 1
        AND to_regclass('agent_observability.obs_observation_y2026m01') IS NULL
        AND NOT EXISTS (SELECT 1 FROM agent_observability.obs_trace WHERE trace_id = '@T4')
        AND EXISTS (SELECT 1 FROM agent_observability.obs_trace WHERE trace_id = '@T5');
END;
$$;
""".replace("@T1", T1)
    .replace("@T2", T2)
    .replace("@T3", T3)
    .replace("@T4", T4)
    .replace("@T5", T5)
    .replace("@T9", T9)
    .replace("@WF", WF)
)

O02 = {
    "O02_counts_returned",
    "O02_trace_totals",
    "O02_observation_shape",
    "O02_same_batch_twice_no_duplicates",
    "O02_invalid_batch_rejected",
    "O02_invalid_batch_leaves_nothing",
}
O03 = {
    "O03_minimal_trace_row",
    "O03_header_completes_trace",
    "O03_tags_unioned_metadata_merged",
    "O03_earliest_start_kept",
    "O03_totals_recomputed",
    "O03_score_kept",
}
O04 = {
    "O04_prices_upserted",
    "O04_old_price_closed",
    "O04_price_before_change",
    "O04_price_after_change",
    "O04_most_specific_pattern",
    "O04_exporter_cost_kept",
    "O04_trace_cost",
}
O07 = {
    "O07_partitions_ahead",
    "O07_partition_on_demand",
    "O07_node_rollup_matches_raw",
    "O07_model_rollup_matches_raw",
    "O07_retention_drops_partition",
}
SHAPE = {"tables", "views", "materialized_views", "procedures"}


@pytest.fixture(scope="module")
def results(psql: Psql) -> Rows:
    return run_checks(psql, CHECKS, "\n".join([observability_schema(), observability_schema()]))


def test_O07_scripts_apply_twice_and_every_check_runs(results: Rows) -> None:
    assert set(passed(results)) == SHAPE | O02 | O03 | O04 | O07


@pytest.mark.parametrize("name", sorted(SHAPE | O07))
def test_O07_schema_partitions_retention_rollups(results: Rows, name: str) -> None:
    assert passed(results).get(name) is True, name


@pytest.mark.parametrize("name", sorted(O02))
def test_O02_ingest_batch_is_idempotent(results: Rows, name: str) -> None:
    assert passed(results).get(name) is True, name


@pytest.mark.parametrize("name", sorted(O03))
def test_O03_observations_before_header(results: Rows, name: str) -> None:
    assert passed(results).get(name) is True, name


@pytest.mark.parametrize("name", sorted(O04))
def test_O04_cost_with_price_change(results: Rows, name: str) -> None:
    assert passed(results).get(name) is True, name
