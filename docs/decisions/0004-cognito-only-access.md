# 0004: The Cognito token is the only access control

- **Status:** accepted at the M1 review (2026-10-26)
- **Decides:** the open assumption of P1-04, catalog visibility
- **Amended:** design Sections 5.1 (step 1), 14.3 and case A-01, in both the Markdown and HTML copies

## Context

Design 5.1 lists a workflow when it is "allowed for the caller's groups". P1-04 implemented this by matching the builder's `security.activities[].code` against the `cognito:groups` claim. That was an assumption: the builder stores `security` but never reads it, and nothing names the claim that carries activity codes. It would also give the runtime a second, builder-defined access model beside Cognito.

## Decision

Keep one security model: Cognito.

- A caller with a valid token from the configured Cognito pool can list and run every workflow the catalog lists. The token is verified against the pool's JWKS, issuer and audience (P2-03). This is the pool of plan Q1, `strands-visual-builder-users-docker` in tripleq-development.
- The catalog rules are now only these: the workflow is a root workflow, it is not disabled, it is published to a runtime channel, and its ID is not under `excludedIdPrefixes`.
- The runtime does not read a workflow's `security` (`activities` or anything else). `CatalogAgentRow` no longer selects the column. `v_agent_catalog` still passes it through, so the view and the product database are unchanged.
- Data stays owner-bound. Sessions, runs, messages and datasets are filtered by the token's `sub` (`owner_sub`) and `custom:tenant`, as design 14.3 says.

## Consequences

- `WorkflowCaller` carries only `subject`, and `auth.claims.groups` stays in settings for the request context and traces.
- A workflow that should not reach every Cognito user is kept out of the runtime by not publishing it to a runtime channel. A per-workflow restriction would come back as a Cognito group rule decided here, not as builder activities.
- Today no stored workflow has `security.activities`, so no listing changes.
